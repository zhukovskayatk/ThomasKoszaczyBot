"""
Скрытые команды владелицы бота: /grant_premium и /revoke_premium — ручные
исключения из платной подписки (см. Settings.owner_user_id в config.py и
database.requests.grant_lifetime_premium/revoke_premium/extend_premium).

Доступ проверяется ТОЛЬКО по совпадению message.from_user.id с
settings.owner_user_id из .env — если это поле не задано в .env вообще,
обе команды тихо ничего не делают ни для кого (безопасный вариант по
умолчанию, а не "открыто всем"). Намеренно НЕ отвечаем посторонним, что
такая команда вообще существует и почему она не сработала — сообщение об
ошибке видела бы только сама владелица, а для всех остальных команда
просто "не существует" (Telegram ничего не покажет на неизвестную команду).
"""

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from config import settings
from database.requests import extend_premium, get_user, grant_lifetime_premium, is_premium_active, revoke_premium
from texts import date_ru

router = Router(name="admin")


def _is_owner(message: Message) -> bool:
    return settings.owner_user_id is not None and message.from_user.id == settings.owner_user_id


@router.message(Command("grant_premium"))
async def cmd_grant_premium(message: Message, command: CommandObject) -> None:
    if not _is_owner(message):
        return

    args = (command.args or "").split()
    if not args:
        await message.answer(
            "Использование:\n"
            "<code>/grant_premium USER_ID</code> — выдать Premium навсегда\n"
            "<code>/grant_premium USER_ID ДНЕЙ</code> — выдать на N дней "
            "(складывается с уже имеющимся сроком, как обычная оплата)"
        )
        return

    try:
        target_user_id = int(args[0])
    except ValueError:
        await message.answer("USER_ID должен быть числом (это Telegram id, не username).")
        return

    if len(args) >= 2:
        try:
            days = int(args[1])
        except ValueError:
            await message.answer("Количество дней должно быть числом.")
            return
        until = await extend_premium(target_user_id, days=days)
    else:
        until = await grant_lifetime_premium(target_user_id)

    await message.answer(f"✅ Premium для <code>{target_user_id}</code> действует до {date_ru(until)}.")


@router.message(Command("revoke_premium"))
async def cmd_revoke_premium(message: Message, command: CommandObject) -> None:
    if not _is_owner(message):
        return

    args = (command.args or "").split()
    if not args:
        await message.answer("Использование: <code>/revoke_premium USER_ID</code>")
        return

    try:
        target_user_id = int(args[0])
    except ValueError:
        await message.answer("USER_ID должен быть числом (это Telegram id, не username).")
        return

    success = await revoke_premium(target_user_id)
    if not success:
        await message.answer("Такого пользователя нет в базе 🤔")
        return

    await message.answer(f"🔓 Premium для <code>{target_user_id}</code> снят.")


@router.message(Command("check_premium"))
async def cmd_check_premium(message: Message, command: CommandObject) -> None:
    """Быстрая проверка статуса — удобно свериться после /grant_premium,
    не заходя в БД руками."""
    if not _is_owner(message):
        return

    args = (command.args or "").split()
    if not args:
        await message.answer("Использование: <code>/check_premium USER_ID</code>")
        return

    try:
        target_user_id = int(args[0])
    except ValueError:
        await message.answer("USER_ID должен быть числом (это Telegram id, не username).")
        return

    user = await get_user(target_user_id)
    if user is None:
        await message.answer("Такого пользователя нет в базе 🤔")
        return

    if is_premium_active(user):
        await message.answer(f"💎 Активен, до {date_ru(user.premium_until)}.")
    else:
        await message.answer("Premium не активен.")
