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
from database.models import ReminderOffset
from database.requests import (
    adjust_checklist_evening_push_time,
    adjust_checklist_morning_push_time,
    adjust_morning_checklist_time,
    adjust_quiet_hours_end,
    adjust_quiet_hours_start,
    adjust_shopping_reminder_time,
    count_completed_tasks,
    get_default_reminder_offsets,
    get_or_create_user,
    get_streak_status,
    get_user,
    rescue_streak_with_xp,
    set_default_reminder_offsets,
    set_shopping_reminder_weekday,
    set_user_utc_offset,
    toggle_checklist_evening_push_enabled,
    toggle_checklist_morning_push_enabled,
    toggle_morning_checklist_enabled,
    toggle_quiet_hours_enabled,
    toggle_reminders_enabled,
    toggle_shopping_reminder_enabled,
)
from keyboards import (
    LEGACY_PROFILE_BUTTON_TEXTS,
    PROFILE_BUTTON_TEXT,
    default_reminder_presets_keyboard,
    main_menu_keyboard,
    notif_time_screen_keyboard,
    notification_settings_keyboard,
    profile_actions_keyboard,
    quiet_hours_screen_keyboard,
    shopping_reminder_screen_keyboard,
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
    """Кнопка "🔔 Уведомления" под карточкой профиля — открывает главный
    экран уведомлений (см. keyboards.notification_settings_keyboard).
    Пункты со своим настраиваемым временем (утренний чек-лист/приглашение
    и вечерняя сводка интерактивного чек-листа/тихие часы/напоминание о
    покупках) отсюда ведут на ОТДЕЛЬНЫЕ экраны (mctime_open/cmtime_open/
    cetime_open/qh_open/shprmd_open ниже) — "Напоминания по задачам"
    остаётся прямым тумблером прямо здесь."""
    user = await get_or_create_user(
        user_id=callback.from_user.id,
        username=callback.from_user.username,
    )
    await callback.answer()
    await callback.message.edit_text(
        texts.notifications_settings_text(),
        reply_markup=notification_settings_keyboard(user).as_markup(),
    )


@router.callback_query(F.data.startswith("notif_toggle:"))
async def notif_toggle(callback: CallbackQuery) -> None:
    """
    Клик по переключателю ПРЯМО НА главном экране уведомлений — сейчас
    это только "Напоминания по задачам" (единственный пункт без своего
    отдельного экрана времени). Остальные поля здесь тоже поддерживаются
    ради обратной совместимости — если у кого-то в чате ещё открыто
    старое сообщение с кнопками ДО этого обновления (когда все пункты
    переключались прямо тут), клик по нему всё ещё сработает, просто
    вернёт на главный экран уведомлений, а не на отдельный (у НОВЫХ,
    отдельных экранов — свои собственные callback'и: mctime_toggle/
    cmtime_toggle/cetime_toggle/qh_toggle/shprmd_toggle ниже).
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
    elif field == "shopping":
        await toggle_shopping_reminder_enabled(callback.from_user.id)

    user = await get_user(callback.from_user.id)
    await callback.answer("Обновлено ✅")

    if user is None:
        return

    await callback.message.edit_reply_markup(reply_markup=notification_settings_keyboard(user).as_markup())


# --- Экран "⏰ Утренний чек-лист" ------------------------------------------------

async def _render_mctime_screen(callback: CallbackQuery, toast: str | None = None) -> None:
    user = await get_or_create_user(user_id=callback.from_user.id, username=callback.from_user.username)
    if toast is not None:
        await callback.answer(toast)
    else:
        await callback.answer()
    await callback.message.edit_text(
        texts.morning_checklist_time_text(user.morning_checklist_enabled, user.morning_checklist_time_minutes),
        reply_markup=notif_time_screen_keyboard(
            user.morning_checklist_enabled, user.morning_checklist_time_minutes, "mctime_toggle", "mctime_adj",
        ).as_markup(),
    )


@router.callback_query(F.data == "mctime_open")
async def mctime_open(callback: CallbackQuery) -> None:
    """"⏰ Утренний чек-лист" в "🔔 Уведомления" — переключатель + степпер
    времени (см. keyboards.notif_time_screen_keyboard)."""
    await _render_mctime_screen(callback)


@router.callback_query(F.data == "mctime_toggle")
async def mctime_toggle(callback: CallbackQuery) -> None:
    await toggle_morning_checklist_enabled(callback.from_user.id)
    await _render_mctime_screen(callback, "Обновлено ✅")


@router.callback_query(F.data.startswith("mctime_adj:"))
async def mctime_adjust(callback: CallbackQuery) -> None:
    delta = int(callback.data.split(":", maxsplit=1)[1])
    await adjust_morning_checklist_time(callback.from_user.id, delta)
    await _render_mctime_screen(callback)


# --- Экран "☀️ Приглашение в чек-лист" (Premium) --------------------------------

async def _render_cmtime_screen(callback: CallbackQuery, toast: str | None = None) -> None:
    user = await get_or_create_user(user_id=callback.from_user.id, username=callback.from_user.username)
    if toast is not None:
        await callback.answer(toast)
    else:
        await callback.answer()
    await callback.message.edit_text(
        texts.checklist_morning_push_time_text(
            user.checklist_morning_push_enabled, user.checklist_morning_push_time_minutes
        ),
        reply_markup=notif_time_screen_keyboard(
            user.checklist_morning_push_enabled, user.checklist_morning_push_time_minutes,
            "cmtime_toggle", "cmtime_adj",
        ).as_markup(),
    )


@router.callback_query(F.data == "cmtime_open")
async def cmtime_open(callback: CallbackQuery) -> None:
    await _render_cmtime_screen(callback)


@router.callback_query(F.data == "cmtime_toggle")
async def cmtime_toggle(callback: CallbackQuery) -> None:
    await toggle_checklist_morning_push_enabled(callback.from_user.id)
    await _render_cmtime_screen(callback, "Обновлено ✅")


@router.callback_query(F.data.startswith("cmtime_adj:"))
async def cmtime_adjust(callback: CallbackQuery) -> None:
    delta = int(callback.data.split(":", maxsplit=1)[1])
    await adjust_checklist_morning_push_time(callback.from_user.id, delta)
    await _render_cmtime_screen(callback)


# --- Экран "🌙 Вечерняя сводка чек-листа" (Premium) ------------------------------

async def _render_cetime_screen(callback: CallbackQuery, toast: str | None = None) -> None:
    user = await get_or_create_user(user_id=callback.from_user.id, username=callback.from_user.username)
    if toast is not None:
        await callback.answer(toast)
    else:
        await callback.answer()
    await callback.message.edit_text(
        texts.checklist_evening_push_time_text(
            user.checklist_evening_push_enabled, user.checklist_evening_push_time_minutes
        ),
        reply_markup=notif_time_screen_keyboard(
            user.checklist_evening_push_enabled, user.checklist_evening_push_time_minutes,
            "cetime_toggle", "cetime_adj",
        ).as_markup(),
    )


@router.callback_query(F.data == "cetime_open")
async def cetime_open(callback: CallbackQuery) -> None:
    await _render_cetime_screen(callback)


@router.callback_query(F.data == "cetime_toggle")
async def cetime_toggle(callback: CallbackQuery) -> None:
    await toggle_checklist_evening_push_enabled(callback.from_user.id)
    await _render_cetime_screen(callback, "Обновлено ✅")


@router.callback_query(F.data.startswith("cetime_adj:"))
async def cetime_adjust(callback: CallbackQuery) -> None:
    delta = int(callback.data.split(":", maxsplit=1)[1])
    await adjust_checklist_evening_push_time(callback.from_user.id, delta)
    await _render_cetime_screen(callback)


# --- Экран "🌙 Тихие часы" -------------------------------------------------------

async def _render_qh_screen(callback: CallbackQuery, toast: str | None = None) -> None:
    user = await get_or_create_user(user_id=callback.from_user.id, username=callback.from_user.username)
    if toast is not None:
        await callback.answer(toast)
    else:
        await callback.answer()
    await callback.message.edit_text(
        texts.quiet_hours_settings_text(
            user.quiet_hours_enabled, user.quiet_hours_start_minutes, user.quiet_hours_end_minutes
        ),
        reply_markup=quiet_hours_screen_keyboard(
            user.quiet_hours_enabled, user.quiet_hours_start_minutes, user.quiet_hours_end_minutes
        ).as_markup(),
    )


@router.callback_query(F.data == "qh_open")
async def qh_open(callback: CallbackQuery) -> None:
    """"🌙 Тихие часы" в "🔔 Уведомления" — переключатель + два независимых
    степпера (начало/конец окна, см. keyboards.quiet_hours_screen_keyboard) —
    раньше окно было жёстко зашито 22:00–08:00 на всех."""
    await _render_qh_screen(callback)


@router.callback_query(F.data == "qh_toggle")
async def qh_toggle(callback: CallbackQuery) -> None:
    await toggle_quiet_hours_enabled(callback.from_user.id)
    await _render_qh_screen(callback, "Обновлено ✅")


@router.callback_query(F.data.startswith("qhs_adj:"))
async def qhs_adjust(callback: CallbackQuery) -> None:
    """Степпер НАЧАЛА окна (см. database.requests.adjust_quiet_hours_start)."""
    delta = int(callback.data.split(":", maxsplit=1)[1])
    await adjust_quiet_hours_start(callback.from_user.id, delta)
    await _render_qh_screen(callback)


@router.callback_query(F.data.startswith("qhe_adj:"))
async def qhe_adjust(callback: CallbackQuery) -> None:
    """Степпер КОНЦА окна (см. database.requests.adjust_quiet_hours_end)."""
    delta = int(callback.data.split(":", maxsplit=1)[1])
    await adjust_quiet_hours_end(callback.from_user.id, delta)
    await _render_qh_screen(callback)


# --- Экран "🛒 Напоминание о покупках" -------------------------------------------

async def _render_shprmd_screen(callback: CallbackQuery, toast: str | None = None) -> None:
    user = await get_or_create_user(user_id=callback.from_user.id, username=callback.from_user.username)
    if toast is not None:
        await callback.answer(toast)
    else:
        await callback.answer()
    await callback.message.edit_text(
        texts.shopping_reminder_settings_text(
            user.shopping_reminder_enabled, user.shopping_reminder_weekday, user.shopping_reminder_time_minutes
        ),
        reply_markup=shopping_reminder_screen_keyboard(
            user.shopping_reminder_enabled, user.shopping_reminder_weekday, user.shopping_reminder_time_minutes
        ).as_markup(),
    )


@router.callback_query(F.data == "shprmd_open")
async def shprmd_open(callback: CallbackQuery) -> None:
    """"🛒 Напоминание о покупках" в "🔔 Уведомления" — новая, по умолчанию
    выключенная еженедельная фича: переключатель + день недели + степпер
    времени (см. keyboards.shopping_reminder_screen_keyboard)."""
    await _render_shprmd_screen(callback)


@router.callback_query(F.data == "shprmd_toggle")
async def shprmd_toggle(callback: CallbackQuery) -> None:
    await toggle_shopping_reminder_enabled(callback.from_user.id)
    await _render_shprmd_screen(callback, "Обновлено ✅")


@router.callback_query(F.data.startswith("shday_set:"))
async def shprmd_day(callback: CallbackQuery) -> None:
    """Выбор дня недели (радио-кнопки Пн–Вс, см.
    database.requests.set_shopping_reminder_weekday)."""
    weekday = int(callback.data.split(":", maxsplit=1)[1])
    await set_shopping_reminder_weekday(callback.from_user.id, weekday)
    await _render_shprmd_screen(callback)


@router.callback_query(F.data.startswith("shtime_adj:"))
async def shprmd_adjust(callback: CallbackQuery) -> None:
    delta = int(callback.data.split(":", maxsplit=1)[1])
    await adjust_shopping_reminder_time(callback.from_user.id, delta)
    await _render_shprmd_screen(callback)


# --- Экран "⏱ Стандартные пресеты напоминаний" (Time Management Module v2) ----

@router.callback_query(F.data == "defrmd_open")
async def defrmd_open(callback: CallbackQuery) -> None:
    """Кнопка "⏱ Изменить стандартные пресеты" на экране "🔔 Уведомления" —
    что автоматически отмечается в меню напоминаний у КАЖДОЙ новой задачи
    (см. handlers/tasks.py::_apply_default_reminders)."""
    selected = set(await get_default_reminder_offsets(callback.from_user.id))
    await callback.answer()
    await callback.message.edit_text(
        texts.default_reminder_presets_text(sorted(selected, key=lambda o: list(ReminderOffset).index(o))),
        reply_markup=default_reminder_presets_keyboard(selected).as_markup(),
    )


@router.callback_query(F.data.startswith("defrmd_toggle:"))
async def defrmd_toggle(callback: CallbackQuery) -> None:
    """Клик по одному чекбоксу на экране стандартных пресетов — сразу
    сохраняет в БД, та же мгновенная модель, что и у rmd_toggle/notif_toggle."""
    offset = ReminderOffset(callback.data.split(":", maxsplit=1)[1])
    selected = set(await get_default_reminder_offsets(callback.from_user.id))

    if offset in selected:
        selected.discard(offset)
    else:
        selected.add(offset)

    await set_default_reminder_offsets(callback.from_user.id, list(selected))
    await callback.answer("Обновлено ✅")
    await callback.message.edit_text(
        texts.default_reminder_presets_text(sorted(selected, key=lambda o: list(ReminderOffset).index(o))),
        reply_markup=default_reminder_presets_keyboard(selected).as_markup(),
    )


@router.callback_query(F.data == "defrmd_back")
async def defrmd_back(callback: CallbackQuery) -> None:
    """"◀️ Назад к уведомлениям" — возвращает обычный экран "🔔 Уведомления"."""
    await notif_open(callback)


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
