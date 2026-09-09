"""
Профиль игрока: команда /profile и кнопка "🏆 Профиль".

Показывает красивую карточку профиля (см. texts.profile_text) — уровень,
звание, XP и шкалу прогресса до следующего уровня, а также статус серии
активности с учётом защиты от сгорания: автоматическая недельная заморозка
("Выходной для кота") и ручное спасение за XP (см. database.requests
::get_streak_status / rescue_streak_with_xp).

Под карточкой — отдельным сообщением кнопка "🔔 Уведомления", ведущая на
экран трёх переключателей (утренний чек-лист в 09:00, напоминания по
задачам, тихие часы 22:00–08:00, см. notif_open/notif_toggle ниже и
services/scheduler.py, где эти настройки реально применяются).
"""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

import texts
from database.requests import (
    count_completed_tasks,
    get_or_create_user,
    get_streak_status,
    get_user,
    rescue_streak_with_xp,
    set_user_utc_offset,
    toggle_checklist_evening_push_enabled,
    toggle_checklist_morning_push_enabled,
    toggle_morning_checklist_enabled,
    toggle_quiet_hours_enabled,
    toggle_reminders_enabled,
)
from keyboards import (
    LEGACY_PROFILE_BUTTON_TEXTS,
    PROFILE_BUTTON_TEXT,
    main_menu_keyboard,
    notification_settings_keyboard,
    profile_actions_keyboard,
    streak_rescue_keyboard,
    timezone_settings_keyboard,
)
from services.leveling import get_level_info

router = Router(name="profile")


async def show_profile(message: Message) -> None:
    # get_or_create_user на случай, если профиль запросили без /start
    # (например, после сброса базы данных) — тогда просто создаём запись
    # с нулевым XP вместо падения с ошибкой.
    user = await get_or_create_user(
        user_id=message.from_user.id,
        username=message.from_user.username,
    )

    level_info = get_level_info(user.xp_points)
    completed_count = await count_completed_tasks(user_id=message.from_user.id)

    # get_streak_status (а не "сырое" user.streak_days!) — чинит баг, когда
    # серия показывала старое число даже после нескольких дней полного
    # бездействия, и заодно применяет защиту от сгорания: если пропущен
    # ровно один день, автоматически списывает недельную заморозку
    # ("Выходной для кота"), либо — если она уже потрачена на этой неделе —
    # предлагает ручное спасение за XP.
    status = await get_streak_status(user_id=message.from_user.id)

    # reply_markup=main_menu_keyboard — на случай, если постоянная клавиатура
    # внизу экрана у человека всё ещё старая (например, после переименования
    # кнопки): любое обращение к профилю заодно её освежит.
    await message.answer(
        texts.profile_text(level_info, user.xp_points, completed_count, status.streak_days, status.frozen_today),
        reply_markup=main_menu_keyboard,
    )

    if status.rescue_available:
        # Отдельным сообщением с инлайн-кнопкой — чтобы предложение спасти
        # серию сразу бросалось в глаза, а не терялось среди текста самой
        # карточки профиля.
        await message.answer(
            texts.streak_rescue_prompt_text(status.at_risk_days, status.rescue_cost),
            reply_markup=streak_rescue_keyboard(status.rescue_cost).as_markup(),
        )
    elif status.just_reset:
        # Серия по-настоящему прервалась (пропуск 2+ дней, заморозку и
        # спасение уже не предложить) — мягкая реплика без упрёков вместо
        # молчаливого "Серия: 0 дней". Показывается отдельным сообщением
        # под карточкой, чтобы не перегружать саму карточку профиля.
        await message.answer(texts.streak_reset_text())

    # Три инлайн-кнопки отдельным сообщением — т.к. у карточки профиля уже
    # занят reply_markup под постоянную клавиатуру (main_menu_keyboard), а
    # инлайн-кнопки на неё "поверх" не помещаются — Telegram допускает
    # только один reply_markup на сообщение. Сюда же переехали 💎 Premium
    # и 👥 Партнёр из нижнего меню (см. keyboards.profile_actions_keyboard).
    await message.answer(
        texts.profile_actions_text(),
        reply_markup=profile_actions_keyboard().as_markup(),
    )


@router.message(Command("profile"))
async def cmd_profile(message: Message) -> None:
    await show_profile(message)


@router.message(F.text.in_({PROFILE_BUTTON_TEXT, *LEGACY_PROFILE_BUTTON_TEXTS}))
async def profile_button(message: Message) -> None:
    await show_profile(message)


@router.callback_query(F.data == "profile_partner")
async def profile_partner(callback: CallbackQuery) -> None:
    """"👥 Мой партнёр" под карточкой профиля — тот же экран, что раньше
    открывался отдельной кнопкой в нижнем меню (см. handlers/partner.py)."""
    from handlers import partner  # локальный импорт — без цикла (partner не знает про profile)

    await callback.answer()
    await partner.show_partner_screen(callback.message, callback.from_user.id)


@router.callback_query(F.data == "profile_premium")
async def profile_premium(callback: CallbackQuery) -> None:
    """"💎 Подписка" под карточкой профиля — тот же экран, что раньше
    открывался отдельной кнопкой в нижнем меню (см. handlers/subscription.py)."""
    from handlers import subscription  # локальный импорт — без цикла

    await callback.answer()
    await subscription.show_premium_screen(callback.message, callback.from_user.id, callback.from_user.username)


@router.callback_query(F.data == "streak_rescue")
async def streak_rescue(callback: CallbackQuery) -> None:
    """
    Кнопка "💎 Спасти серию за N XP" — ручное спасение серии, когда
    авто-заморозка уже использована на этой неделе (см.
    database.requests.rescue_streak_with_xp). Может не сработать, если
    пока человек тянул с решением, ситуация уже изменилась (не хватает XP,
    либо прошло больше суток и спасать стало поздно) — тогда просто
    сообщаем об этом, не трогая сообщение.
    """
    ok = await rescue_streak_with_xp(user_id=callback.from_user.id)
    if not ok:
        await callback.answer("Не получилось — либо не хватает XP, либо спасать уже нечего 🤔", show_alert=True)
        return

    status = await get_streak_status(user_id=callback.from_user.id)
    await callback.answer("Серия спасена! 💎")
    await callback.message.edit_text(texts.streak_rescued_text(status.streak_days))


# --- Экран "🔔 Уведомления" -----------------------------------------------------

@router.callback_query(F.data == "notif_open")
async def notif_open(callback: CallbackQuery) -> None:
    """Кнопка "🔔 Уведомления" под карточкой профиля — открывает экран трёх
    переключателей (см. keyboards.notification_settings_keyboard)."""
    user = await get_or_create_user(
        user_id=callback.from_user.id,
        username=callback.from_user.username,
    )
    await callback.answer()
    await callback.message.edit_text(
        texts.notifications_settings_text(),
        reply_markup=notification_settings_keyboard(
            user.reminders_enabled,
            user.quiet_hours_enabled,
            user.morning_checklist_enabled,
            user.checklist_morning_push_enabled,
            user.checklist_evening_push_enabled,
        ).as_markup(),
    )


@router.callback_query(F.data.startswith("notif_toggle:"))
async def notif_toggle(callback: CallbackQuery) -> None:
    """
    Клик по одному из переключателей — сразу меняет значение в БД и
    перерисовывает галочку на месте (edit_message_reply_markup), без
    отдельной кнопки "Сохранить": изменения применяются мгновенно, то же
    самое поведение, что и у мультивыбора напоминаний (rmd_toggle).
    """
    field = callback.data.split(":", maxsplit=1)[1]

    if field == "morning":
        await toggle_morning_checklist_enabled(callback.from_user.id)
    elif field == "reminders":
        await toggle_reminders_enabled(callback.from_user.id)
    elif field == "quiet":
        await toggle_quiet_hours_enabled(callback.from_user.id)
    elif field == "checklist_morning":
        await toggle_checklist_morning_push_enabled(callback.from_user.id)
    elif field == "checklist_evening":
        await toggle_checklist_evening_push_enabled(callback.from_user.id)

    user = await get_user(callback.from_user.id)
    await callback.answer("Обновлено ✅")

    if user is None:
        return

    await callback.message.edit_reply_markup(
        reply_markup=notification_settings_keyboard(
            user.reminders_enabled,
            user.quiet_hours_enabled,
            user.morning_checklist_enabled,
            user.checklist_morning_push_enabled,
            user.checklist_evening_push_enabled,
        ).as_markup()
    )


# --- Экран "🌍 Часовой пояс" -----------------------------------------------------

@router.callback_query(F.data == "tz_open")
async def tz_open(callback: CallbackQuery) -> None:
    """Кнопка "🌍 Часовой пояс" под карточкой профиля — от него зависит,
    во сколько РЕАЛЬНО приходят напоминания и утренние/вечерние сводки
    (см. services/timeutils.py)."""
    user = await get_or_create_user(
        user_id=callback.from_user.id,
        username=callback.from_user.username,
    )
    await callback.answer()
    await callback.message.edit_text(
        texts.timezone_settings_text(user.utc_offset_minutes),
        reply_markup=timezone_settings_keyboard(user.utc_offset_minutes).as_markup(),
    )


@router.callback_query(F.data.startswith("tz_adjust:"))
async def tz_adjust(callback: CallbackQuery) -> None:
    """Клик по ➖/➕ 1 ч — сразу меняет значение в БД и перерисовывает
    экран на месте (edit_message_text/edit_message_reply_markup), без
    отдельной кнопки "Сохранить" — та же идея, что и у переключателей
    уведомлений (notif_toggle)."""
    delta = int(callback.data.split(":", maxsplit=1)[1])
    user = await get_user(callback.from_user.id)
    current = user.utc_offset_minutes if user is not None else 180

    updated = await set_user_utc_offset(callback.from_user.id, current + delta)
    if updated is None:
        await callback.answer("Не удалось найти профиль 🤔", show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_text(
        texts.timezone_settings_text(updated.utc_offset_minutes),
        reply_markup=timezone_settings_keyboard(updated.utc_offset_minutes).as_markup(),
    )


@router.callback_query(F.data == "tz_back")
async def tz_back(callback: CallbackQuery) -> None:
    """"◀️ Назад к профилю" — экран часового пояса был отдельным
    сообщением (см. tz_open), просто убираем его, как и notif_back."""
    await callback.answer()
    await callback.message.delete()


@router.callback_query(F.data == "notif_back")
async def notif_back(callback: CallbackQuery) -> None:
    """"◀️ Назад к профилю" — экран настроек был отдельным сообщением
    (см. notif_open), просто убираем его, ничего дополнительно открывать
    не нужно, карточка профиля уже выше в ленте чата."""
    await callback.answer()
    await callback.message.delete()
