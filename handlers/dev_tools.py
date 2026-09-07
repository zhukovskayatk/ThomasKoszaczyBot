"""
Служебные хэндлеры для наполнения services/meme_manager.STICKER_IDS.

Два способа собрать стикеры:
1. Прислать боту ОДИН стикер — в ответ придёт его file_id.
2. Прислать команду /addpack <короткое_имя_набора> — бот сам заберёт
   ВСЕ стикеры из этого набора одним запросом и пришлёт готовый список
   file_id, который останется только целиком вставить в STICKER_IDS.

Короткое имя набора — это то, что идёт после t.me/addstickers/ в ссылке
на набор (её можно получить, открыв инфо о наборе в Telegram и нажав
«Поделиться» / Share).
"""

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

router = Router(name="dev_tools")


@router.message(F.sticker)
async def catch_sticker(message: Message) -> None:
    file_id = message.sticker.file_id
    await message.answer(
        "🆔 file_id этого стикера:\n"
        f"<code>{file_id}</code>\n\n"
        "Скопируй строку в <code>services/meme_manager.py</code>, "
        "в список <code>STICKER_IDS</code>.\n\n"
        "💡 Совет: если хочешь забрать сразу весь набор, а не по одному "
        "стикеру — используй команду /addpack <короткое имя набора>."
    )


@router.message(Command("addpack"))
async def cmd_addpack(message: Message, command: CommandObject) -> None:
    pack_name = command.args

    if not pack_name:
        await message.answer(
            "Напиши короткое имя набора стикеров после команды.\n"
            "Например: <code>/addpack SlothZoZo</code>\n\n"
            "Где его взять: открой нужный набор стикеров в Telegram → "
            "нажми на его название вверху → «Поделиться» — короткое имя "
            "это то, что идёт после <code>t.me/addstickers/</code> в ссылке."
        )
        return

    try:
        sticker_set = await message.bot.get_sticker_set(pack_name)
    except TelegramBadRequest:
        await message.answer(
            "Не нашла набор с таким именем 🤔 Проверь, что скопировала "
            "короткое имя без ошибок (без t.me/addstickers/, только сама "
            "часть после слэша)."
        )
        return

    file_ids = [sticker.file_id for sticker in sticker_set.stickers]

    if not file_ids:
        await message.answer("В этом наборе почему-то нет стикеров 🤷")
        return

    await message.answer(
        f"Нашла набор «{sticker_set.title}», в нём {len(file_ids)} "
        "стикеров 🎉\n\nСейчас пришлю их одним (или несколькими, если "
        "набор большой) блоком — просто скопируй и вставь в STICKER_IDS "
        "в meme_manager.py."
    )

    # Разбиваем на порции, чтобы не упереться в лимит длины сообщения
    # Telegram (~4096 символов) на больших наборах (до 120 стикеров).
    CHUNK_SIZE = 30
    for i in range(0, len(file_ids), CHUNK_SIZE):
        chunk = file_ids[i : i + CHUNK_SIZE]
        ids_block = ",\n    ".join(f'"{fid}"' for fid in chunk)
        await message.answer(f"<pre>{ids_block}</pre>")
