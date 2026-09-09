"""
Модуль "☀️ Чек-лист дня" — интерактивный экран фокуса на сегодня.

Автономная архитектура (по требованию — никакого ручного дублирования
между списком задач и чек-листом):
- Обычные задачи с дедлайном СЕГОДНЯ попадают в чек-лист САМИ, без
  отдельного действия (см. database.requests.get_checklist_tasks_for_today).
- Привычки/рутины (Habit) — отдельная сущность: живут в чек-листе
  постоянно, не пересоздаются каждый день, только сбрасывают
  done_today/hidden_today в полночь (см. services.scheduler._reset_daily_habits).

Экраны:
- Главный экран (chk_open / кнопка "☀️ Чек-лист") — чекбоксы привычек и
  задач на сегодня, тап переключает состояние ПРЯМО на месте.
- Режим настройки "⚙️ Настроить фокус дня" (chk_edit) — полное ручное
  управление составом: скрыть/удалить привычку, убрать задачу из
  сегодняшнего фокуса (без удаления самой задачи).

Быстрое добавление рутины/дела на сегодня — единственное место в этом
модуле, где не избежать свободного текстового ввода: под это заведён
простой словарь _pending_add в памяти процесса, тем же способом, что и
_pending_rename в handlers/tasks.py. Сам перехват текста происходит ТАМ
же, в handlers/tasks.py::add_task_from_text (единственном месте, которое
ловит "любой обычный текст") — try_handle_pending_text ниже вызывается
оттуда самым первым, до проверки переименования задачи.
"""

from datetime import date, datetime

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

import services.scheduler as scheduler_service
import texts
from database.requests import (
    add_task,
    add_xp,
    can_create_habit,
    can_create_task,
    clear_task_reminders,
    create_habit,
    delete_habit,
    get_checklist_tasks_for_today,
    get_habit,
    get_habits,
    get_visible_habits_today,
    set_task_deadline,
    toggle_habit_done,
    toggle_habit_hidden_today,
)
from keyboards import (
    CHECKLIST_BUTTON_TEXT,
    checklist_dashboard_keyboard,
    checklist_edit_keyboard,
    habit_delete_confirm_keyboard,
)
from services import timeutils
from services.task_actions import complete_task_core

router = Router(name="checklist")

# user_id -> ("habit" | "task", "dashboard" | "edit") — что именно создаём
# следующим текстовым сообщением, и на какой экран вернуться после (см.
# try_handle_pending_text, вызывается из handlers/tasks.py::add_task_from_text).
_pending_add: dict[int, tuple[str, str]] = {}


async def _dashboard_payload(user_id: int):
    """Текст + инлайн-клавиатура главного экрана чек-листа — общая сборка
    для всех мест, которые его показывают/перерисовывают. Дедлайны задач
    переводятся в личный часовой пояс user_id перед отрисовкой (см.
    services/timeutils.py) — иначе точное время на кнопке (например,
    "18:15") показывалось бы по времени сервера."""
    habits = await get_visible_habits_today(user_id)
    tasks = await get_checklist_tasks_for_today(user_id)
    timeutils.localize_tasks(tasks, await timeutils.viewer_offset_minutes(user_id))
    return texts.checklist_dashboard_text(habits, tasks), checklist_dashboard_keyboard(habits, tasks).as_markup()


async def _edit_payload(user_id: int):
    """Текст + инлайн-клавиатура экрана "⚙️ Настроить фокус дня" — в
    отличие от главного экрана здесь показываются ВСЕ привычки, включая
    временно скрытые (иначе их нельзя было бы вернуть обратно). См.
    _dashboard_payload про перевод дедлайнов в личный часовой пояс."""
    habits = await get_habits(user_id)
    tasks = await get_checklist_tasks_for_today(user_id)
    timeutils.localize_tasks(tasks, await timeutils.viewer_offset_minutes(user_id))
    return texts.checklist_edit_screen_text(), checklist_edit_keyboard(habits, tasks).as_markup()


async def try_handle_pending_text(message: Message) -> bool:
    """
    Если ждём текст нового пункта чек-листа (рутина или быстрое дело на
    сегодня, см. chk_add_routine/chk_add_task/chked_addtask ниже) —
    создаёт его и возвращает True. Иначе просто возвращает False, ничего
    не трогая — тогда вызывающий код (handlers/tasks.py) продолжает свою
    обычную обработку текста (переименование задачи либо создание новой).
    """
    pending = _pending_add.pop(message.from_user.id, None)
    if pending is None:
        return False

    kind, return_to = pending
    user_id = message.from_user.id
    blocked_text: str | None = None

    if kind == "habit":
        if await can_create_habit(user_id):
            await create_habit(user_id=user_id, title=message.text)
        else:
            # Бесплатный лимит привычек исчерпан (см.
            # database.requests.FREE_HABITS_LIMIT) — ничего не создаём,
            # просто объясняем почему и всё равно перерисовываем экран ниже.
            blocked_text = texts.free_habit_limit_reached_text()
    else:
        if await can_create_task(user_id):
            # Быстрое дело на сегодня — обычная задача с дедлайном "сегодня, в
            # течение дня" (тот же формат, что и пресет "☀️ В течение дня" в
            # мастере срока), проставляется сразу, без отдельного шага мастера.
            task = await add_task(user_id=user_id, title=message.text)
            today_end = datetime.combine(date.today(), datetime.max.time().replace(microsecond=0))
            await set_task_deadline(task_id=task.task_id, user_id=user_id, deadline=today_end, all_day=True)
        else:
            blocked_text = texts.free_task_limit_reached_text()

    if return_to == "edit":
        text, keyboard = await _edit_payload(user_id)
    else:
        text, keyboard = await _dashboard_payload(user_id)

    if blocked_text:
        await message.answer(blocked_text)
    await message.answer(text, reply_markup=keyboard)
    return True


# --- Главный экран ---------------------------------------------------------

@router.message(F.text == CHECKLIST_BUTTON_TEXT)
async def checklist_button(message: Message) -> None:
    text, keyboard = await _dashboard_payload(message.from_user.id)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == "chk_open")
async def chk_open(callback: CallbackQuery) -> None:
    """Кнопка "☀️ Открыть Чек-лист дня" под утренним пуш-приглашением (см.
    services.scheduler._send_checklist_morning_briefs) — открывает экран
    НОВЫМ сообщением, сам пуш остаётся в ленте как есть."""
    await callback.answer()
    text, keyboard = await _dashboard_payload(callback.from_user.id)
    await callback.message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("chk_htoggle:"))
async def chk_htoggle(callback: CallbackQuery) -> None:
    """
    Тап по привычке на главном экране — мгновенно переключает ▫️ ↔ ✅
    (см. database.requests.toggle_habit_done) прямо в этом же сообщении.
    XP начисляется при отметке "сделано" и СПИСЫВАЕТСЯ обратно при
    случайном повторном тапе (снятии галочки) — так нельзя накрутить очки
    двойным нажатием, но можно спокойно поправить ошибочный тап.
    """
    habit_id = int(callback.data.split(":", maxsplit=1)[1])
    habit = await toggle_habit_done(habit_id, callback.from_user.id)
    if habit is None:
        await callback.answer("Не удалось найти эту рутину 🤔", show_alert=True)
        return

    if habit.done_today:
        await add_xp(callback.from_user.id, habit.xp_reward)
        await callback.answer(f"✅ +{habit.xp_reward} XP 🐾")
    else:
        await add_xp(callback.from_user.id, -habit.xp_reward)
        await callback.answer("Отменено")

    text, keyboard = await _dashboard_payload(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("chk_ttoggle:"))
async def chk_ttoggle(callback: CallbackQuery) -> None:
    """
    Тап по задаче на главном экране чек-листа — закрывает её насовсем (та
    же логика закрытия, что и везде в боте, см.
    services.task_actions.complete_task_core), после чего задача
    закономерно пропадает из чек-листа (она больше не in_progress).
    """
    task_id = int(callback.data.split(":", maxsplit=1)[1])
    result = await complete_task_core(user_id=callback.from_user.id, task_id=task_id)

    if result is None:
        await callback.answer("Эта задача уже закрыта или не найдена 🤔", show_alert=True)
        return

    await callback.answer(f"✅ +{result.xp_amount} XP 🐾")
    if result.leveled_up:
        await callback.message.answer(texts.level_up_text(result.new_level))

    if result.partner_notified is not None:
        try:
            await callback.bot.send_message(
                result.partner_notified,
                texts.partner_task_done_push_text(result.task_title, result.xp_amount),
            )
        except TelegramBadRequest:
            pass

    text, keyboard = await _dashboard_payload(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(F.data == "chk_add_routine")
async def chk_add_routine(callback: CallbackQuery) -> None:
    """Кнопка "➕ Добавить рутину" — ждём текст следующим сообщением (см.
    try_handle_pending_text) и возвращаемся на главный экран чек-листа."""
    _pending_add[callback.from_user.id] = ("habit", "dashboard")
    await callback.answer()
    await callback.message.edit_text(texts.checklist_add_habit_prompt_text())


@router.callback_query(F.data == "chk_add_task")
async def chk_add_task(callback: CallbackQuery) -> None:
    """Кнопка "➕ Дело на сегодня" — ждём текст, дедлайн проставится сам
    (см. try_handle_pending_text)."""
    _pending_add[callback.from_user.id] = ("task", "dashboard")
    await callback.answer()
    await callback.message.edit_text(texts.checklist_add_task_prompt_text())


@router.callback_query(F.data == "chk_edit")
async def chk_edit(callback: CallbackQuery) -> None:
    """Кнопка "⚙️ Настроить фокус дня" — переключает то же сообщение на
    режим ручного управления составом чек-листа."""
    await callback.answer()
    text, keyboard = await _edit_payload(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=keyboard)


# --- Режим "⚙️ Настроить фокус дня" -----------------------------------------

@router.callback_query(F.data.startswith("chked_hhide:"))
async def chked_hhide(callback: CallbackQuery) -> None:
    """"👁 Скрыть на сегодня" / "🙈 Показать" — временно убирает привычку с
    главного экрана НА СЕГОДНЯ, сама привычка никуда не девается (см.
    database.requests.toggle_habit_hidden_today)."""
    habit_id = int(callback.data.split(":", maxsplit=1)[1])
    habit = await toggle_habit_hidden_today(habit_id, callback.from_user.id)
    if habit is None:
        await callback.answer("Не удалось найти эту рутину 🤔", show_alert=True)
        return

    await callback.answer("Скрыто на сегодня" if habit.hidden_today else "Снова видно")
    habits = await get_habits(callback.from_user.id)
    tasks = await get_checklist_tasks_for_today(callback.from_user.id)
    timeutils.localize_tasks(tasks, await timeutils.viewer_offset_minutes(callback.from_user.id))
    await callback.message.edit_reply_markup(reply_markup=checklist_edit_keyboard(habits, tasks).as_markup())


@router.callback_query(F.data.startswith("chked_hdel:"))
async def chked_hdel(callback: CallbackQuery) -> None:
    """"🗑" рядом с привычкой — необратимое удаление, поэтому сначала
    просим подтверждение (см. keyboards.habit_delete_confirm_keyboard)."""
    habit_id = int(callback.data.split(":", maxsplit=1)[1])
    habit = await get_habit(habit_id, callback.from_user.id)
    if habit is None:
        await callback.answer("Не удалось найти эту рутину 🤔", show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_text(
        texts.habit_delete_confirm_text(habit.title),
        reply_markup=habit_delete_confirm_keyboard(habit_id).as_markup(),
    )


@router.callback_query(F.data.startswith("chked_hdel_yes:"))
async def chked_hdel_yes(callback: CallbackQuery) -> None:
    """Подтверждено — привычка удаляется насовсем, возвращаемся в режим
    настройки (уже без неё)."""
    habit_id = int(callback.data.split(":", maxsplit=1)[1])
    await delete_habit(habit_id, callback.from_user.id)
    await callback.answer("Удалено 🗑")
    text, keyboard = await _edit_payload(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("chked_hdel_no:"))
async def chked_hdel_no(callback: CallbackQuery) -> None:
    """"◀️ Отмена" — просто возвращаемся в режим настройки без изменений."""
    await callback.answer("Отменено")
    text, keyboard = await _edit_payload(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("chked_trm:"))
async def chked_trm(callback: CallbackQuery) -> None:
    """
    "🚫 Убрать из дня" — снимает сегодняшний дедлайн задачи целиком (та же
    операция, что и кнопка "⚪️ Без срока" в календаре, и тумблер
    "🌙 Убрать из Чек-листа дня" в карточке задачи, см. handlers/tasks.py::
    card_chk_out). Задача НЕ удаляется — просто возвращается в бэклог без
    срока, откуда её всегда можно достать заново.
    """
    task_id = int(callback.data.split(":", maxsplit=1)[1])
    user_id = callback.from_user.id

    await set_task_deadline(task_id=task_id, user_id=user_id, deadline=None, all_day=False)
    await clear_task_reminders(task_id)
    scheduler_service.unschedule_all_for_task(task_id)

    await callback.answer("Убрано из чек-листа 🚫")
    habits = await get_habits(user_id)
    tasks = await get_checklist_tasks_for_today(user_id)
    timeutils.localize_tasks(tasks, await timeutils.viewer_offset_minutes(user_id))
    await callback.message.edit_reply_markup(reply_markup=checklist_edit_keyboard(habits, tasks).as_markup())


@router.callback_query(F.data == "chked_addtask")
async def chked_addtask(callback: CallbackQuery) -> None:
    """"➕ Добавить быстрый пункт" прямо в режиме настройки — то же самое
    быстрое добавление, что и chk_add_task, только возвращаемся потом
    обратно в режим настройки, а не на главный экран."""
    _pending_add[callback.from_user.id] = ("task", "edit")
    await callback.answer()
    await callback.message.edit_text(texts.checklist_add_task_prompt_text())


@router.callback_query(F.data == "chked_save")
async def chked_save(callback: CallbackQuery) -> None:
    """"💾 Сохранить и вернуться в Чек-лист" — все изменения в режиме
    настройки применяются мгновенно по каждому клику (без отдельного шага
    сохранения), эта кнопка просто возвращает на главный экран."""
    await callback.answer()
    text, keyboard = await _dashboard_payload(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=keyboard)
