"""
Экран "👥 Партнёр" — партнёрский режим (общие задачи для двоих),
Premium-фича (см. handlers/subscription.py).

Как устроена сама пара — см. подробности в database/requests.py над
get_partner/create_partner_invite/accept_partner_invite/unlink_partner.
Коротко: приглашение — это одноразовый код, из которого строится диплинк
t.me/<bot>?start=pair_<code>; переход по нему обрабатывает /start (см.
handlers/start.py), а не этот роутер — здесь только сам экран "👥 Партнёр"
и разрыв пары.
"""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

import texts
from database.requests import (
    create_partner_invite,
    get_or_create_user,
    get_partner,
    is_premium_active,
    unlink_partner,
)
from keyboards import PARTNER_BUTTON_TEXT, partner_screen_keyboard, partner_unlink_confirm_keyboard

router = Router(name="partner")


async def show_partner_screen(message: Message, user_id: int) -> None:
    user = await get_or_create_user(user_id=user_id, username=message.from_user.username)
    is_premium = is_premium_active(user)
    partner = await get_partner(user_id) if is_premium else None
    await message.answer(
        texts.partner_screen_text(is_premium, partner),
        reply_markup=partner_screen_keyboard(is_premium, partner is not None).as_markup(),
    )


@router.message(F.text == PARTNER_BUTTON_TEXT)
async def partner_button(message: Message) -> None:
    await show_partner_screen(message, message.from_user.id)


@router.message(Command("partner"))
async def cmd_partner(message: Message) -> None:
    await show_partner_screen(message, message.from_user.id)


@router.callback_query(F.data == "partner_go_premium")
async def partner_go_premium(callback: CallbackQuery) -> None:
    """"💎 Оформить Premium" на экране "👥 Партнёр" — просто открывает
    полноценный экран 💎 Premium (не дублируем его логику здесь)."""
    from handlers import subscription  # локальный импорт — без цикла (subscription не знает про partner)

    await callback.answer()
    await subscription.show_premium_screen(callback.message)


@router.callback_query(F.data == "partner_invite")
async def partner_invite(callback: CallbackQuery) -> None:
    """"🔗 Получить ссылку-приглашение" — только для Premium и только пока
    пары ещё нет (обе проверки — на случай, если экран успел устареть,
    например Premium истёк или пара уже образовалась в другой вкладке)."""
    user_id = callback.from_user.id
    user = await get_or_create_user(user_id=user_id, username=callback.from_user.username)

    if not is_premium_active(user):
        await callback.answer("Нужен Premium 💎", show_alert=True)
        return

    if await get_partner(user_id) is not None:
        await callback.answer("У тебя уже есть партнёр 🤔", show_alert=True)
        return

    code = await create_partner_invite(user_id)
    bot_info = await callback.bot.get_me()
    link = f"https://t.me/{bot_info.username}?start=pair_{code}"

    await callback.answer()
    await callback.message.answer(texts.partner_invite_link_text(link))


@router.callback_query(F.data == "partner_unlink")
async def partner_unlink(callback: CallbackQuery) -> None:
    """"🔓 Отвязать партнёра" — показывает подтверждение (действие
    необратимо разрывает пару, поэтому не выполняем его сразу по одному клику)."""
    partner = await get_partner(callback.from_user.id)
    if partner is None:
        await callback.answer("Партнёр не найден 🤔", show_alert=True)
        return

    await callback.answer()
    await callback.message.answer(
        texts.partner_unlink_confirm_text(partner),
        reply_markup=partner_unlink_confirm_keyboard().as_markup(),
    )


@router.callback_query(F.data == "partner_unlink_yes")
async def partner_unlink_yes(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    partner = await unlink_partner(user_id)

    if partner is None:
        await callback.answer("Партнёр не найден 🤔", show_alert=True)
        return

    await callback.answer("Отвязано 🔓")
    await callback.message.edit_text(texts.partner_unlinked_text())

    # Второй стороне пары — отдельное уведомление, раз разрыв инициировал
    # не он: иначе он ещё долго не узнал бы, что общий доступ пропал.
    try:
        me = await get_or_create_user(user_id=user_id, username=callback.from_user.username)
        await callback.bot.send_message(partner.user_id, texts.partner_unlinked_by_other_text(me))
    except Exception:
        pass


@router.callback_query(F.data == "partner_unlink_no")
async def partner_unlink_no(callback: CallbackQuery) -> None:
    await callback.answer("Отменено")
    await callback.message.edit_text("Хорошо, партнёр остаётся 👥")
