"""
Обработчик команды /start.

Регистрирует пользователя в базе данных (если его там ещё нет)
и отправляет приветственное сообщение от маскота — котика Thomas Koszaczy.
"""

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

import texts
from database.requests import get_or_create_user
from keyboards import main_menu_keyboard

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    # Создаём пользователя в БД, если его ещё нет.
    # get_or_create_user сам решает — создавать новую запись или нет.
    await get_or_create_user(
        user_id=message.from_user.id,
        username=message.from_user.username,
    )

    await message.answer(
        texts.welcome_text(),
        # Постоянная кнопка "📋 Задачи" будет теперь видна под полем ввода
        reply_markup=main_menu_keyboard,
    )