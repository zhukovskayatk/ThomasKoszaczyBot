"""
Фоновый планировщик напоминаний о дедлайнах (APScheduler).

Как это устроено:
- База данных (таблица reminders) — источник истины: там хранится, КОГДА
  и по какой задаче должно прийти напоминание, и отправлено ли оно уже.
- AsyncIOScheduler — просто "будильник поверх БД": в момент срабатывания
  таймера он читает из БД конкретное напоминание и шлёт сообщение.
- Планировщик хранит расписание ТОЛЬКО в памяти процесса — при
  перезапуске бота оно исчезает. Поэтому при каждом старте бота (см.
  main.py) мы заново перечитываем из БД все ещё не отправленные
  напоминания и заново ставим для них таймеры (см. resync_reminders).

id job'ов строится как f"reminder_{task_id}_{offset.value}" (см. _job_id) —
НЕ из id записи в БД. Так у каждой задачи на каждый тип напоминания
("за 1 день", "за 1 час" и т.д.) всегда ровно один job с предсказуемым и
уникальным именем: напоминания разных задач никогда не перезапишут друг
друга, даже если случайно совпадут по времени срабатывания, а замена
(reschedule_reminder + schedule_reminder) просто переиспользует тот же
job_id через replace_existing=True.

Три настройки из экрана "🔔 Уведомления" (Профиль и Настройки →
🔔 Уведомления, см. handlers/profile.py и database/models.py::User)
применяются именно здесь:
- reminders_enabled — общий рубильник: выключен, пуш просто не уходит
  (см. _fire_reminder).
- quiet_hours_enabled — "Тихие часы" 22:00–08:00: время срабатывания
  сдвигается на 09:00 ещё на этапе постановки таймера (см.
  _apply_quiet_hours / schedule_reminder), а не проверяется в момент
  самой отправки — так in-memory расписание APScheduler сразу честное.
- morning_checklist_enabled — ежедневная сводка дел на сегодня в 09:00
  (см. _send_morning_checklists, регистрируется как cron-job в
  init_scheduler).

Отдельно — "второй шанс" (см. _maybe_send_second_chance): через 2 часа
после обычного напоминания, если задача так и осталась не закрыта,
приходит ОДИН мягкий разовый повтор вместо повторного тревожного пуша —
чтобы дело не потерялось из виду, но и не было ощущения, что бот "долбит".
"""

from datetime import datetime, time, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import texts
from database.models import ReminderOffset, Status
from database.requests import (
    get_checklist_tasks_for_today,
    get_pending_reminders,
    get_reminder_with_task,
    get_tasks_completed_today,
    get_tasks_due_today_or_overdue,
    get_user,
    get_users_with_checklist_evening_push_enabled,
    get_users_with_checklist_morning_push_enabled,
    get_users_with_morning_checklist_enabled,
    get_visible_habits_today,
    mark_reminder_second_chance_sent,
    mark_reminder_sent,
    reschedule_reminder,
    reset_daily_habits,
)
from keyboards import checklist_push_open_keyboard, reminder_notification_keyboard
from services.leveling import XP_BY_PRIORITY, XP_PER_TASK

scheduler = AsyncIOScheduler()

# Ссылка на бота нужна внутри задачи планировщика (send_message). Бот в
# приложении один на весь процесс, поэтому храним его один раз при
# старте (init_scheduler), а не пробрасываем через каждый add_job.
_bot: Bot | None = None

# Если бот был выключен дольше этого времени — просроченные напоминания
# при старте молча помечаем отправленными, а не шлём все разом пачкой
# (человеку не нужен "привет из прошлого" на десяток старых уведомлений).
_STALE_REMINDER_THRESHOLD = timedelta(hours=6)

# Через сколько после обычного напоминания присылать мягкий "второй шанс",
# если задача так и осталась не закрыта (см. _maybe_send_second_chance).
_SECOND_CHANCE_DELAY = timedelta(hours=2)

# Тихие часы: с 22:00 и до 08:00 звуковых пушей быть не должно (см.
# _apply_quiet_hours) — всё, что попадает в это окно, откладывается до
# ровно 09:00 (совпадает с временем утреннего чек-листа не случайно —
# это то же самое "утро уже началось, можно присылать").
_QUIET_HOURS_START = 22
_QUIET_HOURS_END = 8
_QUIET_HOURS_RESUME = time(9, 0)

# Время ежедневного утреннего чек-листа (см. _send_morning_checklists).
_MORNING_CHECKLIST_TIME = time(9, 0)

# Время пушей интерактивного модуля "☀️ Чек-лист дня" (см.
# _send_checklist_morning_briefs/_send_checklist_evening_summaries) — не то
# же самое, что _MORNING_CHECKLIST_TIME выше (тот — пассивный текстовый
# дайджест, эти два — приглашения в интерактивный экран), хоть утреннее
# время и совпадает намеренно (оба про "начало дня").
_CHECKLIST_MORNING_PUSH_TIME = time(9, 0)
_CHECKLIST_EVENING_PUSH_TIME = time(21, 0)

# Полуночный сброс "сделано сегодня"/"скрыто сегодня" у всех привычек (см.
# _reset_daily_habits) — новый день должен начинаться с чистого листа без
# какого-либо ручного действия пользователя.
_HABITS_RESET_TIME = time(0, 0)


def _job_id(task_id: int, offset: ReminderOffset) -> str:
    return f"reminder_{task_id}_{offset.value}"


def _second_chance_job_id(reminder_id: int) -> str:
    return f"second_chance_{reminder_id}"


def _apply_quiet_hours(remind_at: datetime) -> datetime:
    """
    Если remind_at попадает в окно "Тихих часов" (22:00–08:00) —
    переносит его на 09:00 ближайшего подходящего дня: с полуночи до
    восьми утра — на 09:00 ТОГО ЖЕ дня, с 22:00 и позже — на 09:00
    СЛЕДУЮЩЕГО дня. Иначе возвращает время как есть.
    """
    hour = remind_at.hour
    if hour >= _QUIET_HOURS_START:
        return datetime.combine(remind_at.date() + timedelta(days=1), _QUIET_HOURS_RESUME)
    if hour < _QUIET_HOURS_END:
        return datetime.combine(remind_at.date(), _QUIET_HOURS_RESUME)
    return remind_at


async def schedule_reminder(task_id: int, offset: ReminderOffset, reminder_id: int, remind_at: datetime) -> None:
    """
    Ставит (или переставляет — replace_existing) таймер на конкретное
    напоминание. Если у пользователя включены "Тихие часы" (по умолчанию
    да) и remind_at попадает в окно 22:00–08:00 — фактическое время
    срабатывания сдвигается на 09:00 (см. _apply_quiet_hours); в БД сама
    запись Reminder.remind_at при этом не трогается — сдвиг чисто на
    уровне таймера, чтобы при выключении "Тихих часов" не потребовался
    отдельный пересчёт всех уже сохранённых записей.
    misfire_grace_time — если процесс на секунды/минуты "притормозил"
    ровно в момент срабатывания, уведомление всё равно уйдёт, а не
    потеряется молча.
    """
    effective_time = remind_at
    found = await get_reminder_with_task(reminder_id)
    if found is not None:
        _, task = found
        user = await get_user(task.user_id)
        if user is not None and user.quiet_hours_enabled:
            effective_time = _apply_quiet_hours(remind_at)

    scheduler.add_job(
        _fire_reminder,
        trigger="date",
        run_date=effective_time,
        args=[reminder_id],
        id=_job_id(task_id, offset),
        replace_existing=True,
        misfire_grace_time=3600,
    )


def unschedule_reminder(task_id: int, offset: ReminderOffset) -> None:
    """Снимает таймер конкретного напоминания задачи — например, если
    пользователь убрал галочку в меню напоминаний."""
    job = scheduler.get_job(_job_id(task_id, offset))
    if job is not None:
        job.remove()


def unschedule_all_for_task(task_id: int) -> None:
    """
    Снимает таймеры ВСЕХ возможных напоминаний задачи разом — используется
    при удалении задачи, при выборе "⚪️ Без срока"/"☀️ В течение дня" и при
    закрытии задачи. Не нужно заранее знать, какие именно смещения были
    выбраны — job_id для несуществующего job'а просто ничего не найдёт.
    """
    for offset in ReminderOffset:
        unschedule_reminder(task_id, offset)


async def _fire_reminder(reminder_id: int) -> None:
    """
    Срабатывает в момент напоминания: шлёт сообщение и отмечает
    напоминание отправленным. Если у пользователя выключен общий рубильник
    "☑️ Напоминания по задачам" (🔔 Уведомления в профиле) — просто
    отмечает как отправленное и ничего не шлёт, второй шанс по нему тоже
    не планируется (нечего продолжать, раз пуши выключены вовсе).
    """
    if _bot is None:
        return

    found = await get_reminder_with_task(reminder_id)
    if found is None:
        return
    reminder, task = found

    if reminder.sent:
        return  # подстраховка от повторного срабатывания

    user = await get_user(task.user_id)
    if user is not None and not user.reminders_enabled:
        await mark_reminder_sent(reminder_id)
        return

    try:
        await _bot.send_message(
            chat_id=task.user_id,
            text=texts.reminder_notification_text(task, reminder.offset),
            reply_markup=reminder_notification_keyboard(reminder.reminder_id, task).as_markup(),
        )
    except TelegramBadRequest:
        # Например, пользователь заблокировал бота — тихо пропускаем,
        # не роняя весь планировщик из-за одного неудачного уведомления.
        pass

    await mark_reminder_sent(reminder_id)

    # "Второй шанс" через 2 часа — см. _maybe_send_second_chance. Ставим
    # его здесь же, а не отдельным шагом при resync_reminders: если бот
    # перезапустится в эти 2 часа, мягкий повтор по этому конкретному
    # напоминанию может не прийти — сознательный компромисс (это лишь
    # необязательная подстраховка, а не основное напоминание, которое уже
    # ушло и учтено).
    scheduler.add_job(
        _maybe_send_second_chance,
        trigger="date",
        run_date=datetime.now() + _SECOND_CHANCE_DELAY,
        args=[reminder_id],
        id=_second_chance_job_id(reminder_id),
        replace_existing=True,
        misfire_grace_time=3600,
    )


async def _maybe_send_second_chance(reminder_id: int) -> None:
    """
    Срабатывает через _SECOND_CHANCE_DELAY после обычного напоминания.
    Если задача так и осталась не закрыта (и второй шанс по этому
    напоминанию ещё не отправляли) — присылает ОДИН тихий, негромкий
    повтор (см. texts.reminder_second_chance_text) вместо того, чтобы
    напоминать снова тем же тревожным пушем. Если задача уже закрыта,
    удалена, либо второй шанс уже был — молча ничего не делает: пуш,
    который никогда не отправляется на "спам", не может быть проблемой.
    """
    if _bot is None:
        return

    found = await get_reminder_with_task(reminder_id)
    if found is None:
        return
    reminder, task = found

    if reminder.second_chance_sent or task.status != Status.in_progress:
        return

    user = await get_user(task.user_id)
    if user is None or not user.reminders_enabled:
        return

    try:
        await _bot.send_message(
            chat_id=task.user_id,
            text=texts.reminder_second_chance_text(task),
            reply_markup=reminder_notification_keyboard(reminder.reminder_id, task).as_markup(),
        )
    except TelegramBadRequest:
        pass

    await mark_reminder_second_chance_sent(reminder_id)


async def reschedule(reminder_id: int, new_time: datetime) -> bool:
    """
    Переносит напоминание на конкретное новое время — общая функция под
    все варианты меню "💤 Отложить" (быстрый сдвиг на N минут, "на завтра
    утро/вечер", и полная перенастройка через календарь и барабан времени).
    Возвращает True, если напоминание нашлось и было перенесено.
    """
    found = await get_reminder_with_task(reminder_id)
    if found is None:
        return False
    reminder, task = found

    updated = await reschedule_reminder(reminder_id, new_time)
    if updated is None:
        return False

    await schedule_reminder(task.task_id, reminder.offset, reminder_id, new_time)
    return True


async def resync_reminders() -> None:
    """
    Перечитывает из БД все ещё не отправленные напоминания и заново
    ставит для них таймеры. Вызывается один раз при старте бота — без
    этого шага все напоминания, назначенные ДО перезапуска бота, молча
    пропали бы (память APScheduler при старте пуста).
    """
    now = datetime.now()
    pending = await get_pending_reminders()

    for reminder, task in pending:
        if reminder.remind_at < now - _STALE_REMINDER_THRESHOLD:
            # Слишком старое, "протухшее" напоминание — не шлём с большим
            # опозданием, просто списываем его как отправленное.
            await mark_reminder_sent(reminder.reminder_id)
            continue
        await schedule_reminder(task.task_id, reminder.offset, reminder.reminder_id, reminder.remind_at)


async def _send_morning_checklists() -> None:
    """
    Ежедневный пуш в 09:00 (см. _MORNING_CHECKLIST_TIME, регистрируется
    как cron-job в init_scheduler) — короткий чек-лист дел на сегодня
    (и просроченных) тем, у кого включён переключатель "☑️ Утренний
    чек-лист (09:00)" в 🔔 Уведомления. Специально не завязан на "Тихие
    часы" — это и есть уже сдвинутое на утро время, а не ночной пуш.
    """
    if _bot is None:
        return

    users = await get_users_with_morning_checklist_enabled()
    for user in users:
        tasks_today = await get_tasks_due_today_or_overdue(user.user_id)
        try:
            await _bot.send_message(
                chat_id=user.user_id,
                text=texts.morning_checklist_text(tasks_today),
            )
        except TelegramBadRequest:
            # Заблокировал бота или похожая проблема с конкретным
            # получателем — не должна прерывать рассылку остальным.
            pass


async def _reset_daily_habits() -> None:
    """
    Полуночный cron-job (см. _HABITS_RESET_TIME, регистрируется в
    init_scheduler): сбрасывает done_today/hidden_today у ВСЕХ привычек
    всех пользователей разом — именно это и делает модуль "☀️ Чек-лист
    дня" автономным, привычки не нужно пересоздавать вручную каждый день.
    """
    await reset_daily_habits()


async def _send_checklist_morning_briefs() -> None:
    """
    Ежедневный пуш-приглашение в интерактивный чек-лист (см.
    _CHECKLIST_MORNING_PUSH_TIME) тем, у кого включён переключатель
    "☑️ ☀️ Приглашение в чек-лист (09:00)" в 🔔 Уведомления. Короткий тизер
    с кнопкой, ведущей прямо в экран (см. handlers/checklist.py::chk_open) —
    в отличие от _send_morning_checklists (тот шлёт готовый текстовый
    список), здесь только число дел и приглашение заглянуть самому.
    """
    if _bot is None:
        return

    users = await get_users_with_checklist_morning_push_enabled()
    for user in users:
        tasks_today = await get_checklist_tasks_for_today(user.user_id)
        try:
            await _bot.send_message(
                chat_id=user.user_id,
                text=texts.checklist_morning_push_text(len(tasks_today)),
                reply_markup=checklist_push_open_keyboard().as_markup(),
            )
        except TelegramBadRequest:
            pass


async def _send_checklist_evening_summaries() -> None:
    """
    Мягкая вечерняя сводка по чек-листу дня (см. _CHECKLIST_EVENING_PUSH_TIME)
    тем, у кого включён переключатель "☑️ 🌙 Вечерняя сводка чек-листа
    (21:00)". Намеренно БЕЗ чувства вины, даже если закрыто мало пунктов
    (см. texts.checklist_evening_push_text) — просто честная цифра.
    """
    if _bot is None:
        return

    users = await get_users_with_checklist_evening_push_enabled()
    for user in users:
        habits_today = await get_visible_habits_today(user.user_id)
        tasks_open = await get_checklist_tasks_for_today(user.user_id)
        tasks_done = await get_tasks_completed_today(user.user_id)

        habits_done_count = sum(1 for h in habits_today if h.done_today)
        total_count = len(habits_today) + len(tasks_open) + len(tasks_done)
        done_count = habits_done_count + len(tasks_done)

        xp_earned = sum(h.xp_reward for h in habits_today if h.done_today)
        xp_earned += sum(XP_BY_PRIORITY.get(t.priority, XP_PER_TASK) for t in tasks_done)

        try:
            await _bot.send_message(
                chat_id=user.user_id,
                text=texts.checklist_evening_push_text(done_count, total_count, xp_earned),
            )
        except TelegramBadRequest:
            pass


def init_scheduler(bot: Bot) -> None:
    """
    Запускает планировщик. Вызывается один раз при старте бота (main.py),
    ДО resync_reminders(). Заодно регистрирует ежедневные cron-job'ы
    утреннего чек-листа, полуночного сброса привычек и обоих пушей модуля
    "☀️ Чек-лист дня" — в отличие от напоминаний по конкретным задачам, эти
    job'ы не привязаны к записи в БД и их не нужно заново переставлять в
    resync_reminders, достаточно зарегистрировать один раз здесь.
    """
    global _bot
    _bot = bot
    scheduler.add_job(
        _send_morning_checklists,
        trigger="cron",
        hour=_MORNING_CHECKLIST_TIME.hour,
        minute=_MORNING_CHECKLIST_TIME.minute,
        id="morning_checklist_digest",
        replace_existing=True,
    )
    scheduler.add_job(
        _reset_daily_habits,
        trigger="cron",
        hour=_HABITS_RESET_TIME.hour,
        minute=_HABITS_RESET_TIME.minute,
        id="habits_daily_reset",
        replace_existing=True,
    )
    scheduler.add_job(
        _send_checklist_morning_briefs,
        trigger="cron",
        hour=_CHECKLIST_MORNING_PUSH_TIME.hour,
        minute=_CHECKLIST_MORNING_PUSH_TIME.minute,
        id="checklist_morning_brief",
        replace_existing=True,
    )
    scheduler.add_job(
        _send_checklist_evening_summaries,
        trigger="cron",
        hour=_CHECKLIST_EVENING_PUSH_TIME.hour,
        minute=_CHECKLIST_EVENING_PUSH_TIME.minute,
        id="checklist_evening_summary",
        replace_existing=True,
    )
    scheduler.start()