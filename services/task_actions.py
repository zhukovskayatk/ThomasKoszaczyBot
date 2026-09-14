"""
Общее "ядро" закрытия задачи — вынесено отдельно от handlers/tasks.py,
чтобы им могли пользоваться сразу НЕСКОЛЬКО разных экранов (кнопка
"✅ Сделано!" под пуш-уведомлением, накопительный чек-ин "✅ Я сделал!" и
теперь ещё и тап по задаче в "☀️ Чек-лист дня", см. handlers/checklist.py),
не дублируя логику начисления XP/уровня/серии в каждом месте по отдельности.

Здесь — только чистая логика (работа с БД и планировщиком), никакой
отправки сообщений в Telegram: КАК показать результат (отдельная награда,
короткий тост, перерисованный список чек-лист) решает вызывающий код.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

import services.scheduler as scheduler_service
from database.models import RecurrenceRule, ReminderOffset
from database.requests import (
    REMINDER_OFFSET_DELTAS,
    add_reminder,
    add_xp,
    clear_task_reminders,
    get_partner,
    get_task,
    get_task_reminders,
    mark_task_done,
    spawn_next_recurrence,
    update_streak,
)
from services.leveling import LevelInfo, XP_BY_PRIORITY, XP_PER_TASK, get_level_info


@dataclass
class TaskCompletionResult:
    task_title: str
    xp_amount: int
    new_xp: int
    old_level: LevelInfo
    new_level: LevelInfo
    streak_days: int
    # user_id второй стороны пары, если задача была общей — кому нужно
    # отдельно отправить пуш "партнёр закрыл общее дело" (см.
    # texts.partner_task_done_push_text); None для обычных личных задач.
    partner_notified: int | None = None
    # Дедлайн НОВОЙ задачи-повтора, если у закрытой задачи был включён
    # автоповтор (Task.recurrence_rule != none, категория "💳 Оплата") —
    # вызывающий код (handlers/tasks.py::complete_task, handlers/checklist.py)
    # использует это, чтобы показать короткую приписку вроде "Следующий
    # платёж: 12 октября" (см. texts.recurring_task_created_text). None —
    # обычная неповторяющаяся задача, ничего показывать не нужно.
    recurring_next_deadline: datetime | None = None

    @property
    def leveled_up(self) -> bool:
        return self.new_level.level > self.old_level.level


async def _copy_reminders_to_next_occurrence(
    old_reminders: list, new_task_id: int, new_deadline: datetime
) -> None:
    """
    Переносит уже настроенные напоминания старой задачи на её "потомка"-
    повтор (см. spawn_next_recurrence), пересчитывая remind_at относительно
    НОВОГО дедлайна — чтобы не пришлось заново тыкать все галочки при
    каждом закрытии регулярного платежа.

    Обычные пресеты и ОТНОСИТЕЛЬНЫЙ "свой вариант" (custom_offset_seconds
    не None) переносятся: смещение то же самое, просто отсчитанное уже от
    нового дедлайна. АБСОЛЮТНЫЙ "свой вариант" (custom_offset_seconds is
    None, например "в пятницу 18:30") сознательно НЕ переносится — это
    разовая привязка к конкретной дате, у новой задачи другой дедлайн и
    "та же" абсолютная дата не имеет смысла сама по себе.

    Напоминания, чьё новое время уже оказалось бы в прошлом (маловероятно,
    но возможно при позднем закрытии задачи), просто пропускаются, а не
    создаются "протухшими".
    """
    now = datetime.now()
    for reminder in old_reminders:
        if reminder.offset == ReminderOffset.custom:
            if reminder.custom_offset_seconds is None:
                continue  # абсолютное время — переносить нечем и незачем
            delta = timedelta(seconds=reminder.custom_offset_seconds)
        else:
            delta = REMINDER_OFFSET_DELTAS[reminder.offset]

        new_remind_at = new_deadline - delta
        if new_remind_at <= now:
            continue

        new_reminder = await add_reminder(
            task_id=new_task_id,
            offset=reminder.offset,
            remind_at=new_remind_at,
            custom_offset_seconds=reminder.custom_offset_seconds,
            custom_label=reminder.custom_label,
        )
        await scheduler_service.schedule_reminder(
            new_task_id, reminder.offset, new_reminder.reminder_id, new_remind_at
        )


async def complete_task_core(user_id: int, task_id: int) -> TaskCompletionResult | None:
    """
    Отмечает задачу выполненной, начисляет XP, обновляет серию активности
    и снимает все ещё не сработавшие напоминания (и в БД, и в планировщике).
    Возвращает None, если задача не найдена или уже была закрыта раньше —
    вызывающий код должен на этом остановиться.

    Если задача была общей (Task.shared) — XP начисляется ОБОИМ, а не
    только тому, кто нажал "Выполнено": второй стороне пары (владельцу,
    если закрыл партнёр, или партнёру, если закрыл сам владелец) тоже
    капает та же сумма. partner_notified в результате называет, кому
    именно — вызывающий код (handlers/tasks.py, handlers/checklist.py)
    сам отправляет ему пуш, здесь только логика БД, без Telegram-сообщений.
    """
    # Напоминания старой задачи нужно прочитать ДО mark_task_done/
    # clear_task_reminders ниже — тому, кто закрывает регулярный платёж,
    # эти же самые галочки должны перекочевать на следующий (см.
    # _copy_reminders_to_next_occurrence), а clear_task_reminders их
    # безвозвратно сотрёт.
    old_reminders = await get_task_reminders(task_id)

    success = await mark_task_done(task_id=task_id, user_id=user_id)
    if not success:
        return None

    completed_task = await get_task(task_id=task_id, user_id=user_id)
    xp_amount = XP_BY_PRIORITY.get(completed_task.priority, XP_PER_TASK) if completed_task else XP_PER_TASK
    task_title = completed_task.title if completed_task else "задача"

    new_xp = await add_xp(user_id=user_id, amount=xp_amount)
    old_level = get_level_info(new_xp - xp_amount)
    new_level = get_level_info(new_xp)

    streak_days = await update_streak(user_id=user_id)

    partner_notified: int | None = None
    if completed_task is not None and completed_task.shared:
        if completed_task.user_id != user_id:
            # Закрыл партнёр чужую (владельца) общую задачу — бонус и пуш
            # уходят владельцу.
            other_id = completed_task.user_id
        else:
            # Закрыл сам владелец — бонус и пуш уходят его партнёру.
            other = await get_partner(user_id)
            other_id = other.user_id if other else None
        if other_id is not None:
            await add_xp(other_id, xp_amount)
            partner_notified = other_id

    recurring_next_deadline: datetime | None = None
    if (
        completed_task is not None
        and completed_task.recurrence_rule != RecurrenceRule.none
        and completed_task.deadline is not None
    ):
        next_task = await spawn_next_recurrence(completed_task)
        if next_task is not None:
            recurring_next_deadline = next_task.deadline
            await _copy_reminders_to_next_occurrence(old_reminders, next_task.task_id, next_task.deadline)

    await clear_task_reminders(task_id)
    scheduler_service.unschedule_all_for_task(task_id)

    return TaskCompletionResult(
        task_title=task_title,
        xp_amount=xp_amount,
        new_xp=new_xp,
        old_level=old_level,
        new_level=new_level,
        streak_days=streak_days,
        partner_notified=partner_notified,
        recurring_next_deadline=recurring_next_deadline,
    )
