"""
Служебный хэндлер для наполнения services/meme_manager.STICKER_IDS.

Как пользоваться: пришли боту в чат любой понравившийся стикер — в ответ
придёт его file_id одной строкой в моноширинном тексте (удобно нажать
и скопировать). Дальше просто вставь эту строку в список STICKER_IDS
в services/meme_manager.py — перезапуск бота не нужен, список файлов
и стикеров пересчитывается при каждой награде.
"""

from aiogram import F, Router
from aiogram.types import Message

router = Router(name="dev_tools")


@router.message(F.sticker)
async def catch_sticker(message: Message) -> None:
    file_id = message.sticker.file_id
    await message.answer(
        "🆔 file_id этого стикера:\n"
        f"<code>{file_id}</code>\n\n"
        "Скопируй строку в <code>services/meme_manager.py</code>, "
        "в список <code>STICKER_IDS</code>."
    )