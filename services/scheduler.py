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
- quiet_hours_enabled — "Тихие часы" 22:00–08:00 (ПО ЛИЧНОМУ часовому
  поясу пользователя, см. services/timeutils.py): время срабатывания
  сдвигается на 09:00 ещё на этапе постановки таймера (см.
  _apply_quiet_hours / schedule_reminder), а не проверяется в момент
  самой отправки — так in-memory расписание APScheduler сразу честное.
- morning_checklist_enabled — ежедневная сводка дел на сегодня в 09:00
  по личному времени каждого (см. _daily_ticker/_send_one_morning_checklist).

Четыре ежедневных пуша/сброса (утренний текстовый чек-лист, приглашение и
вечерняя сводка интерактивного чек-листа дня, полуночный сброс привычек)
раньше были отдельными cron-задачами по ЕДИНОМУ времени СЕРВЕРА — то есть
"09:00" на деле означало 09:00 там, где физически стоит бот, что могло
оказаться совсем другим часом у конкретного человека. Теперь вместо этого
одна задача-"тикер" (_daily_ticker) проверяет КАЖДУЮ минуту, не наступило
ли у кого-то из пользователей ЕГО личное целевое время (User.
utc_offset_minutes, настройка "🌍 Часовой пояс" в профиле) — раз в минуту
на пользователя это очень небольшая нагрузка (пользователей мало).

Отдельно — "второй шанс" (см. _maybe_send_second_chance): через 2 часа
после обычного напоминания, если задача так и осталась не закрыта,
приходит ОДИН мягкий разовый повтор вместо повторного тревожного пуша —
чтобы дело не потерялось из виду, но и не было ощущения, что бот "долбит".
Это ОТНОСИТЕЛЬНАЯ задержка от текущего реального момента — часовые пояса
тут ни при чём, поэтому datetime.now() здесь не меняем.
"""

from datetime import datetime, time, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import texts
from database.models import ReminderOffset, Status, TaskCategory
from database.requests import (
    get_active_tasks,
    get_all_users,
    get_checklist_tasks_for_today,
    get_pending_reminders,
    get_reminder_with_task,
    get_tasks_completed_today,
    get_tasks_due_today_or_overdue,
    get_user,
    get_users_with_checklist_evening_push_enabled,
    get_users_with_checklist_morning_push_enabled,
    get_users_with_morning_checklist_enabled,
    get_users_with_shopping_reminder_enabled,
    get_visible_habits_today,
    has_effective_premium,
    mark_reminder_second_chance_sent,
    mark_reminder_sent,
    reschedule_reminder,
    reset_daily_habits_for_user,
)
from keyboards import checklist_push_open_keyboard, reminder_notification_keyboard, shopping_reminder_keyboard
from services import timeutils
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

# Тихие часы, время ежедневного утреннего чек-листа, время пушей
# интерактивного "☀️ Чек-лист дня" (утро/вечер) и время еженедельного
# напоминания про "🛒 Покупки" РАНЬШЕ были захардкожены здесь одними и
# теми же константами на всех (22:00–08:00 / 09:00 / 09:00 / 21:00). По
# просьбе "хочу сама выбирать тихие часы и время чек-листов" это теперь
# ПЕРСОНАЛЬНЫЕ настройки каждого пользователя (см. database/models.py::
# User.quiet_hours_start_minutes/quiet_hours_end_minutes/
# morning_checklist_time_minutes/checklist_morning_push_time_minutes/
# checklist_evening_push_time_minutes/shopping_reminder_*) — читаются
# прямо из объекта user на каждой проверке (см. _minutes_to_time ниже),
# а не из модульных констант. Полуночный сброс привычек остаётся общим
# для всех, ровно 00:00 — отдельной настройки под него не просили.
_HABITS_RESET_TIME = time(0, 0)


def _minutes_to_time(minutes: int) -> time:
    """
    Переводит "минуты от полуночи" (как хранятся все персональные времена
    уведомлений, см. комментарий выше) обратно в datetime.time для
    сравнения в _matches_local_time. minutes ожидается уже в диапазоне
    0..1439 (см. database.requests._adjust_time_field, где значение
    крутится по кругу) — % 1440 здесь просто подстраховка на случай
    "сырого" значения прямо из БД.
    """
    minutes = minutes % (24 * 60)
    return time(minutes // 60, minutes % 60)


def _job_id(task_id: int, offset: ReminderOffset) -> str:
    return f"reminder_{task_id}_{offset.value}"


def _second_chance_job_id(reminder_id: int) -> str:
    return f"second_chance_{reminder_id}"


def _apply_quiet_hours(
    remind_at: datetime, utc_offset_minutes: int, start_minutes: int, end_minutes: int
) -> datetime:
    """
    Если remind_at (время СЕРВЕРА) попадает в ПЕРСОНАЛЬНОЕ окно "Тихих
    часов" [start_minutes; end_minutes) ПО ЛИЧНОМУ часовому поясу
    пользователя (оба — минуты от полуночи, настраиваются в профиле, см.
    User.quiet_hours_start_minutes/quiet_hours_end_minutes) — переносит
    его РОВНО на момент end_minutes ближайшего подходящего дня, иначе
    возвращает время как есть.

    Обычный случай — окно "через полночь" (например 22:00–08:00,
    start > end): с полуночи и до end — на end ТОГО ЖЕ дня, с start и
    позже — на end СЛЕДУЮЩЕГО дня. Если человек всё же выставил окно
    ВНУТРИ одного дня (start < end, например "13:00–14:00" — редкий, но
    не запрещённый выбор степпером) — попадание проверяется как
    start <= local_minutes < end, перенос на end этого же дня. start ==
    end — вырожденный случай (окно нулевой длины), ничего не сдвигаем.

    Раньше окно было одно на всех и жёстко 22:00–08:00, резюме всегда
    09:00 (с часовым запасом "на всякий случай"); теперь, когда конец
    окна настраивается сама пользователем, естественнее возобновлять
    ровно в НЕЁ указанный момент, а не с дополнительным отступом.

    Раньше окно проверялось по времени СЕРВЕРА — если его часовой пояс не
    совпадал с личным, "тихие часы" реально приходились на совсем другие
    часы суток у конкретного человека. Теперь remind_at сначала переводится
    в личное время (см. services/timeutils.py), там же и проверяется/
    сдвигается, а результат переводится обратно в серверное — само
    хранение и APScheduler по-прежнему ничего не знают о часовых поясах.
    """
    local = timeutils.to_user(remind_at, utc_offset_minutes)
    local_minutes = local.hour * 60 + local.minute
    resume_time = _minutes_to_time(end_minutes)

    if start_minutes > end_minutes:
        if local_minutes >= start_minutes:
            local_resume = datetime.combine(local.date() + timedelta(days=1), resume_time)
        elif local_minutes < end_minutes:
            local_resume = datetime.combine(local.date(), resume_time)
        else:
            return remind_at
    elif start_minutes < end_minutes:
        if start_minutes <= local_minutes < end_minutes:
            local_resume = datetime.combine(local.date(), resume_time)
        else:
            return remind_at
    else:
        return remind_at

    return timeutils.to_server(local_resume, utc_offset_minutes)


async def schedule_reminder(task_id: int, offset: ReminderOffset, reminder_id: int, remind_at: datetime) -> None:
    """
    Ставит (или переставляет — replace_existing) таймер на конкретное
    напоминание. Если у пользователя включены "Тихие часы" (по умолчанию
    да) и remind_at попадает в его персональное окно (см.
    User.quiet_hours_start_minutes/quiet_hours_end_minutes) — фактическое
    время срабатывания сдвигается на конец этого окна (см.
    _apply_quiet_hours); в БД сама запись Reminder.remind_at при этом не
    трогается — сдвиг чисто на уровне таймера, чтобы при выключении
    "Тихих часов" не потребовался отдельный пересчёт всех уже сохранённых
    записей.
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
            effective_time = _apply_quiet_hours(
                remind_at, user.utc_offset_minutes,
                user.quiet_hours_start_minutes, user.quiet_hours_end_minutes,
            )

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

    # Дедлайн в тексте уведомления (для НЕ-точных смещений, см.
    # texts.reminder_notification_text) должен показывать личное время
    # ВЛАДЕЛЬЦА задачи (получателя этого пуша), а не время сервера — иначе
    # напоминание пришло бы вовремя, но с "неправильными" часами в тексте.
    if user is not None:
        timeutils.localize_task(task, user.utc_offset_minutes)

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


async def _send_one_morning_checklist(user) -> None:
    """
    Утренний текстовый чек-лист ДЛЯ ОДНОГО пользователя — короткий список
    дел на сегодня (и просроченных). Раньше перебор пользователей и
    проверка времени жили прямо в этой функции (единый cron-job на 09:00
    по времени сервера) — теперь тем, "наступило ли у него 09:00", ведает
    _daily_ticker (личное время каждого, см. services/timeutils.py), сюда
    приходит уже конкретный user. Специально не завязан на "Тихие часы" —
    это и есть уже сдвинутое на утро время, а не ночной пуш.
    """
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


async def _send_one_checklist_morning_brief(user) -> None:
    """
    Пуш-приглашение в интерактивный чек-лист ДЛЯ ОДНОГО пользователя —
    короткий тизер с кнопкой, ведущей прямо в экран (см.
    handlers/checklist.py::chk_open). В отличие от _send_one_morning_checklist
    (тот шлёт готовый текстовый список), здесь только число дел и
    приглашение заглянуть самому. См. _send_one_morning_checklist про то,
    почему это теперь функция под одного user, а не цикл по всем.
    """
    tasks_today = await get_checklist_tasks_for_today(user.user_id)
    try:
        await _bot.send_message(
            chat_id=user.user_id,
            text=texts.checklist_morning_push_text(len(tasks_today)),
            reply_markup=checklist_push_open_keyboard().as_markup(),
        )
    except TelegramBadRequest:
        pass


async def _send_one_checklist_evening_summary(user) -> None:
    """
    Мягкая вечерняя сводка по чек-листу дня ДЛЯ ОДНОГО пользователя.
    Намеренно БЕЗ чувства вины, даже если закрыто мало пунктов (см.
    texts.checklist_evening_push_text) — просто честная цифра. См.
    _send_one_morning_checklist про то, почему это функция под одного
    user, а не цикл по всем.
    """
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


async def _send_one_shopping_reminder(user) -> None:
    """
    Еженедельный мягкий пинг "загляни в список покупок" ДЛЯ ОДНОГО
    пользователя (см. User.shopping_reminder_*, _daily_ticker ниже). Если
    в "🛒 Покупки" на данный момент нет ни одного активного пункта — не
    шлём вообще: пуш "загляни в пустой список" не имеет смысла и был бы
    просто лишним шумом (см. get_active_tasks — тот же список, что видит
    сама вкладка "🛒 Покупки", включая общие с партнёром пункты).
    Случайная фраза — та же идея, что и у остальных "живых" реплик Томаса
    (см. texts.random_shopping_reminder_phrase).
    """
    active_tasks = await get_active_tasks(user.user_id)
    purchases_count = sum(1 for t in active_tasks if t.category == TaskCategory.purchases)
    if purchases_count == 0:
        return

    try:
        await _bot.send_message(
            chat_id=user.user_id,
            text=texts.random_shopping_reminder_phrase(),
            reply_markup=shopping_reminder_keyboard().as_markup(),
        )
    except TelegramBadRequest:
        pass


def _matches_local_time(user, target: time) -> bool:
    """Наступила ли у КОНКРЕТНОГО пользователя (по его личному часовому
    поясу, User.utc_offset_minutes) ровно указанная минута суток —
    используется _daily_ticker сразу для всех ежедневных/еженедельных
    пушей/сбросов."""
    local_now = timeutils.user_now(user.utc_offset_minutes)
    return (local_now.hour, local_now.minute) == (target.hour, target.minute)


async def _daily_ticker() -> None:
    """
    Тикает раз в минуту (см. init_scheduler) и заменяет собой отдельные
    cron-задачи по ЕДИНОМУ времени сервера, которые были здесь раньше:
    утренний текстовый чек-лист, приглашение и вечерняя сводка
    интерактивного чек-листа дня, полуночный сброс привычек, а теперь ещё
    и еженедельное напоминание про "🛒 Покупки". Время каждого из них —
    ПЕРСОНАЛЬНАЯ настройка КАЖДОГО пользователя (User.*_time_minutes, см.
    database/models.py и профиль → "🔔 Уведомления"), проверяется в его
    ЛИЧНОМ часовом поясе (см. _matches_local_time) — иначе, например,
    "утренний чек-лист в 09:00" на самом деле приходил в 09:00 по времени
    СЕРВЕРА, что могло оказаться совсем другим часом у реального человека,
    если часовой пояс сервера не совпадает с его собственным. Полуночный
    сброс привычек — единственный без отдельной настройки, всегда 00:00.
    """
    if _bot is None:
        return

    for user in await get_users_with_morning_checklist_enabled():
        if _matches_local_time(user, _minutes_to_time(user.morning_checklist_time_minutes)):
            await _send_one_morning_checklist(user)

    for user in await get_users_with_checklist_morning_push_enabled():
        # Premium-фича (см. roadmap_premium.html) — утренний бриф и
        # вечерняя сводка чек-листа теперь платные; сам переключатель в
        # настройках уведомлений при этом не трогаем, чтобы после
        # оформления Premium ничего не пришлось включать заново.
        if (
            _matches_local_time(user, _minutes_to_time(user.checklist_morning_push_time_minutes))
            and await has_effective_premium(user.user_id)
        ):
            await _send_one_checklist_morning_brief(user)

    for user in await get_users_with_checklist_evening_push_enabled():
        if (
            _matches_local_time(user, _minutes_to_time(user.checklist_evening_push_time_minutes))
            and await has_effective_premium(user.user_id)
        ):
            await _send_one_checklist_evening_summary(user)

    for user in await get_users_with_shopping_reminder_enabled():
        local_now = timeutils.user_now(user.utc_offset_minutes)
        if (
            local_now.weekday() == user.shopping_reminder_weekday
            and _matches_local_time(user, _minutes_to_time(user.shopping_reminder_time_minutes))
        ):
            await _send_one_shopping_reminder(user)

    for user in await get_all_users():
        if _matches_local_time(user, _HABITS_RESET_TIME):
            await reset_daily_habits_for_user(user.user_id)


def init_scheduler(bot: Bot) -> None:
    """
    Запускает планировщик. Вызывается один раз при старте бота (main.py),
    ДО resync_reminders(). Заодно регистрирует ежеминутный тикер
    ежедневных пушей/сброса привычек (см. _daily_ticker) — в отличие от
    напоминаний по конкретным задачам, он не привязан к записи в БД и его
    не нужно заново переставлять в resync_reminders, достаточно
    зарегистрировать один раз здесь.
    """
    global _bot
    _bot = bot
    scheduler.add_job(
        _daily_ticker,
        trigger="cron",
        minute="*",
        id="daily_ticker",
        replace_existing=True,
    )
    scheduler.start()
