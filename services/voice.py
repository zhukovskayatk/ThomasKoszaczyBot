"""
Распознавание речи в голосовых сообщениях (Whisper API от OpenAI).

Та же философия, что и у services/ai_parser.py: если ключ не задан
(OPENAI_API_KEY в .env) или сам запрос не удался (нет сети, таймаут,
невалидный ответ) — возвращаем None, и вызывающий код (handlers/tasks.py::
add_task_from_voice) тихо просит написать текстом вместо падения. Это
единственный внешний сервис в проекте, который стоит денег на КАЖДОЕ
голосовое сообщение (в отличие от DeepSeek, который можно просто не
вызывать) — поэтому вызывающий код обязан проверить бесплатный лимит
(database.requests.can_use_ai_parse) ДО вызова transcribe_voice, а не
после: не тратим деньги на распознавание, если пользователю всё равно
откажем.
"""

from __future__ import annotations

import logging

import aiohttp

from config import settings

logger = logging.getLogger(__name__)

_API_URL = "https://api.openai.com/v1/audio/transcriptions"
_MODEL = "whisper-1"
_TIMEOUT = aiohttp.ClientTimeout(total=30)


async def transcribe_voice(audio_bytes: bytes, filename: str = "voice.ogg") -> str | None:
    """
    Отправляет аудио (как есть, .ogg/opus прямо из Telegram — Whisper API
    принимает его без перекодирования) в OpenAI Whisper и возвращает
    распознанный текст. None — при любой проблеме (нет ключа, нет сети,
    неожиданный ответ); вызывающий код воспринимает это как "распознавание
    недоступно сейчас", не как ошибку.
    """
    if not settings.openai_api_key:
        return None

    form = aiohttp.FormData()
    form.add_field("file", audio_bytes, filename=filename, content_type="audio/ogg")
    form.add_field("model", _MODEL)
    # Подсказка языка — Whisper и без неё обычно угадывает верно, но с
    # подсказкой точнее и чуть быстрее на коротких голосовых.
    form.add_field("language", "ru")

    headers = {"Authorization": f"Bearer {settings.openai_api_key}"}

    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(_API_URL, data=form, headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("OpenAI Whisper вернул статус %s: %s", resp.status, body[:300])
                    return None
                data = await resp.json()
    except Exception:
        logger.exception("Не удалось обратиться к OpenAI Whisper")
        return None

    text = str(data.get("text") or "").strip()
    return text or None
