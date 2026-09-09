"""
Распознавание задачи из свободного текста через DeepSeek API.

Идея: когда пользователь пишет боту одним сообщением что-то вроде
"позвонить маме завтра в 19:00, это срочно" — вместо того, чтобы всегда
создавать задачу с этим текстом как есть (см. handlers/tasks.py::
add_task_from_text), пробуем сначала спросить ИИ: это похоже на дело с
датой/временем/срочностью, или просто короткая формулировка без всего
этого?

Если DEEPSEEK_API_KEY не задан в .env, или сам запрос к DeepSeek не удался
(нет сети, таймаут, невалидный/неожиданный ответ) — возвращаем None, и
вызывающий код тихо откатывается к старому поведению: создаёт задачу с
исходным текстом как есть, без дедлайна. Это НАМЕРЕННО тихий отказ, а не
ошибка — распознавание тут "бонус", а не обязательная часть создания
задачи, и падать из-за него бот не должен.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime

import aiohttp

from config import settings
from database.models import Priority

logger = logging.getLogger(__name__)

_API_URL = "https://api.deepseek.com/chat/completions"
_MODEL = "deepseek-chat"
_TIMEOUT = aiohttp.ClientTimeout(total=12)

_WEEKDAY_NAMES_RU = [
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
]

_PRIORITY_BY_CODE = {
    "low": Priority.low,
    "medium": Priority.medium,
    "high": Priority.high,
}


@dataclass
class ParsedTask:
    """Результат распознавания одного сообщения."""
    title: str
    deadline: datetime | None
    all_day: bool
    priority: Priority | None
    # True, если в тексте явно звучит "мы"/"нам"/"вместе"/"партнёру" и т.п.
    # (см. _system_prompt) — вызывающий код (handlers/tasks.py::
    # _create_task_with_ai) применяет это ТОЛЬКО если у автора уже есть
    # Premium и привязанный партнёр, иначе поле просто игнорируется, как
    # будто его не было: партнёрский режим в самом ai_parser не проверяем
    # намеренно, это забота вызывающего кода, а не разбора текста.
    is_shared: bool


def _system_prompt(now: datetime) -> str:
    return (
        "Ты помогаешь Telegram-боту «Thomas Koszaczy» распознавать задачи "
        "из сообщений пользователя на русском языке.\n"
        f"Сейчас: {now.strftime('%Y-%m-%d %H:%M')} ({_WEEKDAY_NAMES_RU[now.weekday()]}).\n\n"
        "В ответ пришли СТРОГО один JSON-объект без пояснений и без "
        "markdown-разметки, ровно с такими полями:\n"
        "{\n"
        '  "title": строка — короткая суть дела, без слов про дату, время '
        "или срочность (\"позвонить маме\", а не \"позвонить маме завтра "
        "срочно\"),\n"
        '  "deadline": строка "YYYY-MM-DD HH:MM" или null, если в '
        "сообщении вообще нет ни даты, ни времени,\n"
        '  "all_day": true, если из сообщения понятен только день, но не '
        "конкретный час (тогда в deadline поставь 23:59 этого дня), иначе "
        "false,\n"
        '  "priority": одно из "low", "medium", "high" — ТОЛЬКО если '
        "срочность явно названа словами (\"срочно\", \"важно\", \"не "
        "горит\", \"когда-нибудь\"), иначе null — не угадывай важность по "
        "смыслу дела.\n"
        '  "is_shared": true, ТОЛЬКО если в сообщении явно звучат слова '
        "про двоих — \"мы\", \"нам\", \"вместе\", \"партнёру\", \"нам с "
        "партнёром\", \"общая задача\" — иначе false. Не угадывай по смыслу "
        "дела (\"купить корм котам\" — это НЕ автоматически общее, только "
        "если так и сказано словами).\n"
        "}\n\n"
        "Относительные даты (\"завтра\", \"в пятницу\", \"через 2 часа\") "
        "переводи в абсолютную дату относительно текущего момента выше."
    )


async def parse_task_message(text: str) -> ParsedTask | None:
    """
    Пытается распознать название/дедлайн/приоритет из сообщения.
    Возвращает None при любой проблеме (ключ не задан, нет сети, ИИ вернул
    что-то нечитаемое) — вызывающий код должен воспринимать это как
    "распознавание недоступно сейчас", а не как ошибку.
    """
    if not settings.deepseek_api_key:
        return None

    payload = {
        "model": _MODEL,
        "messages": [
            {"role": "system", "content": _system_prompt(datetime.now())},
            {"role": "user", "content": text},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": 300,
    }
    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(_API_URL, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("DeepSeek вернул статус %s: %s", resp.status, body[:300])
                    return None
                data = await resp.json()
    except Exception:
        logger.exception("Не удалось обратиться к DeepSeek")
        return None

    try:
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)

        title = str(parsed.get("title") or "").strip()
        if not title:
            return None

        deadline: datetime | None = None
        deadline_str = parsed.get("deadline")
        if deadline_str:
            try:
                deadline = datetime.strptime(str(deadline_str), "%Y-%m-%d %H:%M")
            except ValueError:
                deadline = None

        all_day = bool(parsed.get("all_day")) and deadline is not None
        priority = _PRIORITY_BY_CODE.get(parsed.get("priority"))
        is_shared = bool(parsed.get("is_shared"))

        return ParsedTask(title=title, deadline=deadline, all_day=all_day, priority=priority, is_shared=is_shared)
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        logger.exception("Не удалось разобрать ответ DeepSeek: %r", data)
        return None
