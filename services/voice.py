"""
Распознавание речи в голосовых сообщениях через OpenAI (модель
gpt-transcribe — актуальная замена более старой Whisper, дешевле и
точнее, см. https://developers.openai.com/api/docs/guides/speech-to-text).

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

import asyncio
import logging

import aiohttp

from config import settings

logger = logging.getLogger(__name__)

_API_URL = "https://api.openai.com/v1/audio/transcriptions"
_MODEL = "gpt-transcribe"
_TIMEOUT = aiohttp.ClientTimeout(total=30)


async def _convert_ogg_to_mp3(ogg_bytes: bytes) -> bytes | None:
    """
    Голосовые Telegram приходят в .ogg/opus — этого формата НЕТ в
    официально документированном списке OpenAI (mp3/mp4/mpeg/mpga/m4a/
    wav/webm), поэтому конвертируем в mp3 через ffmpeg перед отправкой,
    а не полагаемся на недокументированное поведение "а вдруг и так
    сработает". None — если ffmpeg не установлен на сервере или
    конвертация не удалась; тогда transcribe_voice ниже просто пробует
    отправить исходные байты как есть (не идеально, но не падает).
    """
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", "pipe:0", "-f", "mp3", "-loglevel", "error", "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate(input=ogg_bytes)
        if process.returncode != 0 or not stdout:
            logger.warning("ffmpeg не смог сконвертировать голосовое сообщение: %s", stderr[:300])
            return None
        return stdout
    except FileNotFoundError:
        logger.warning("ffmpeg не найден на сервере — отправляю голосовое как есть, в .ogg")
        return None
    except Exception:
        logger.exception("Ошибка при конвертации голосового сообщения через ffmpeg")
        return None


async def transcribe_voice(audio_bytes: bytes, filename: str = "voice.ogg") -> str | None:
    """
    Отправляет аудио в OpenAI (см. _MODEL) и возвращает распознанный
    текст. None — при любой проблеме (нет ключа, нет сети, неожиданный
    ответ); вызывающий код воспринимает это как "распознавание недоступно
    сейчас", не как ошибку.
    """
    if not settings.openai_api_key:
        return None

    mp3_bytes = await _convert_ogg_to_mp3(audio_bytes)
    if mp3_bytes is not None:
        upload_bytes, upload_filename, content_type = mp3_bytes, "voice.mp3", "audio/mpeg"
    else:
        upload_bytes, upload_filename, content_type = audio_bytes, filename, "audio/ogg"

    form = aiohttp.FormData()
    form.add_field("file", upload_bytes, filename=upload_filename, content_type=content_type)
    form.add_field("model", _MODEL)

    headers = {"Authorization": f"Bearer {settings.openai_api_key}"}

    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(_API_URL, data=form, headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("OpenAI (%s) вернул статус %s: %s", _MODEL, resp.status, body[:300])
                    return None
                data = await resp.json()
    except Exception:
        logger.exception("Не удалось обратиться к OpenAI (%s)", _MODEL)
        return None

    text = str(data.get("text") or "").strip()
    return text or None
