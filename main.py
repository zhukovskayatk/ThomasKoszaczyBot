"""
Точка входа в приложение.

Здесь мы:
1. Настраиваем логирование.
2. Инициализируем базу данных (создаём таблицы, если их нет).
3. Создаём бота и диспетчер aiogram.
4. Запускаем планировщик напоминаний (APScheduler) и заново ставим в него
   все ещё не отправленные напоминания из БД (services/scheduler.py).
5. Подключаем роутеры (обработчики) из папки handlers.
6. Запускаем polling (бот начинает опрашивать Telegram на новые сообщения).
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from config import settings
from database.models import init_db
from handlers import checklist, dev_tools, profile, start, tasks
from services.scheduler import init_scheduler, resync_reminders


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Создаём таблицы в БД при старте (если их ещё нет)
    await init_db()

    # DefaultBotProperties(parse_mode=...) — на будущее, чтобы можно было
    # использовать HTML-разметку в сообщениях (жирный текст и т.д.)
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    # Планировщик напоминаний: сначала запускаем сам AsyncIOScheduler и
    # даём ему ссылку на bot (нужна внутри, чтобы отправлять уведомления),
    # затем перечитываем из БД все ещё не отправленные напоминания и
    # заново ставим для них таймеры — сам APScheduler хранит расписание
    # только в памяти процесса и "забывает" его при каждом перезапуске.
    init_scheduler(bot)
    await resync_reminders()

    # Подключаем роутеры из handlers/. Порядок ВАЖЕН: в tasks.router есть
    # "ловец" любого обычного текста (превращает его в новую задачу) — если
    # его подключить раньше profile.router/checklist.router, их кнопки
    # ("🏆 Профиль", "☀️ Чек-лист") будут перехвачены этим ловцом и
    # превратятся в задачу вместо открытия своего экрана. Поэтому
    # tasks.router всегда подключаем последним. dev_tools.router ловит
    # только стикеры и команду /addpack, с текстовыми хэндлерами
    # tasks.router не пересекается, поэтому его место среди остальных
    # не критично.
    dp.include_router(start.router)
    dp.include_router(profile.router)
    dp.include_router(checklist.router)
    dp.include_router(dev_tools.router)
    dp.include_router(tasks.router)

    # Регистрируем список команд — тогда в Telegram рядом с полем ввода
    # появится кнопка "Menu" со списком команд, по нажатию сразу отправляются.
    await bot.set_my_commands([
        BotCommand(command="start", description="Запустить бота / перезапустить"),
        BotCommand(command="add", description="Добавить задачу: /add текст задачи"),
        BotCommand(command="tasks", description="Показать список задач"),
        BotCommand(command="profile", description="Профиль: уровень и XP"),
        BotCommand(command="addpack", description="Забрать весь набор стикеров одним разом"),
    ])

    # На всякий случай сбрасываем накопившиеся апдейты перед стартом polling
    await bot.delete_webhook(drop_pending_updates=True)

    logging.info("Бот запущен, начинаю polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())