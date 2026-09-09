"""
Обработчик команды /start.

Регистрирует пользователя в базе данных (если его там ещё нет)
и отправляет приветственное сообщение от маскота — котика Thomas Koszaczy.

Также ловит диплинк-приглашение в партнёрский режим: переход по ссылке
t.me/<bot>?start=pair_<code> (см. handlers/partner.py::partner_invite)
приходит сюда же как обычный /start с payload в command.args — Telegram
сам передаёт всё, что после "?start=", именно так.
"""

from aiogram import Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import Message

import texts
from database.requests import accept_partner_invite, get_or_create_user
from keyboards import main_menu_keyboard

router = Router(name="start")

_PAIR_PAYLOAD_PREFIX = "pair_"


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject) -> None:
    # Создаём пользователя в БД, если его ещё нет.
    # get_or_create_user сам решает — создавать новую запись или нет.
    await get_or_create_user(
        user_id=message.from_user.id,
        username=message.from_user.username,
    )

    payload = command.args
    if payload and payload.startswith(_PAIR_PAYLOAD_PREFIX):
        await _handle_pair_deep_link(message, payload[len(_PAIR_PAYLOAD_PREFIX):])
        return

    await message.answer(
        texts.welcome_text(),
        # Постоянная кнопка "📋 Задачи" будет теперь видна под полем ввода
        reply_markup=main_menu_keyboard,
    )


async def _handle_pair_deep_link(message: Message, code: str) -> None:
    """
    Обрабатывает переход по ссылке-приглашению в пару (см.
    database.requests.accept_partner_invite для всех проверок и причин
    отказа). При успехе — подтверждение обеим сторонам: тому, кто перешёл
    по ссылке, отвечаем прямо здесь, а пригласившему шлём отдельное
    сообщение (он мог отправить ссылку и полностью забыть про бота, пока
    ждёт — важно, чтобы он тоже узнал о новой паре, не заходя в 👥 Партнёр
    самому).
    """
    result = await accept_partner_invite(code, acceptor_user_id=message.from_user.id)

    if not result.ok:
        await message.answer(texts.partner_invite_error_text(result.reason), reply_markup=main_menu_keyboard)
        return

    acceptor = await get_or_create_user(user_id=message.from_user.id, username=message.from_user.username)
    await message.answer(texts.partner_paired_text(result.partner), reply_markup=main_menu_keyboard)

    try:
        await message.bot.send_message(result.partner.user_id, texts.partner_paired_text(acceptor))
    except Exception:
        pass
