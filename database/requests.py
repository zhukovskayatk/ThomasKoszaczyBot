"""
Функции для работы с базой данных ("запросы").

Хендлеры (handlers/*) не должны напрямую работать с SQLAlchemy —
вместо этого они вызывают простые асинхронные функции из этого файла.
Это удобно: если завтра поменяется структура БД или сама БД (например,
переедем на PostgreSQL), хендлеры менять не придётся.
"""

import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, or_, select, update

from database.models import Habit, Priority, Reminder, ReminderOffset, Status, Task, User, async_session


async def get_or_create_user(user_id: int, username: str | None) -> User:
    """
    Возвращает пользователя из БД. Если пользователя ещё нет — создаёт его.
    Используется в обработчике команды /start.
    """
    async with async_session() as session:
        user = await session.get(User, user_id)
        if user is None:
            user = User(user_id=user_id, username=username)
            session.add(user)
            await session.commit()
        return user


async def add_task(user_id: int, title: str) -> Task:
    """
    Создаёт новую задачу для пользователя со статусом "в процессе".
    Приоритет по умолчанию — Medium (его можно будет менять на
    следующих шагах, например, через отдельную команду).
    """
    async with async_session() as session:
        task = Task(user_id=user_id, title=title, status=Status.in_progress)
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task


async def get_active_tasks(user_id: int) -> list[Task]:
    """
    Возвращает список невыполненных задач пользователя, в порядке
    создания (используется, например, для меню "🎉 Я сделал!", где важен
    порядок "новые сверху", а не порядок дедлайнов).

    Партнёрский режим (Premium): если у пользователя есть привязанный
    партнёр (User.family_id, см. get_partner), в список ДОБАВЛЯЮТСЯ ещё и
    задачи партнёра, которые тот явно пометил "общими" (Task.shared=True,
    см. toggle_task_shared) — просто как единый список того же вида,
    никакого отдельного UI не нужно. Это единственное место, где нужно
    было "влить" партнёрские задачи — get_active_tasks_by_deadline ниже
    просто сортирует уже полученный отсюда список, а меню "🎉 Я сделал!"
    использует эту же функцию напрямую, так что оба места получают общие
    задачи автоматически, без дублирования логики.
    """
    async with async_session() as session:
        user = await session.get(User, user_id)

        if user is not None and user.family_id is not None:
            partner_ids_result = await session.execute(
                select(User.user_id).where(User.family_id == user.family_id, User.user_id != user_id)
            )
            partner_ids = [row[0] for row in partner_ids_result.all()]
        else:
            partner_ids = []

        if partner_ids:
            result = await session.execute(
                select(Task)
                .where(
                    Task.status == Status.in_progress,
                    or_(
                        Task.user_id == user_id,
                        (Task.user_id.in_(partner_ids)) & (Task.shared.is_(True)),
                    ),
                )
                .order_by(Task.task_id)
            )
        else:
            result = await session.execute(
                select(Task)
                .where(Task.user_id == user_id, Task.status == Status.in_progress)
                .order_by(Task.task_id)
            )
        return list(result.scalars().all())


def _deadline_sort_key(task: Task) -> tuple[int, datetime]:
    """
    Ключ сортировки для /tasks (см. get_active_tasks_by_deadline):
    0 — просрочено или дедлайн сегодня (самое горящее, наверх списка);
    1 — дедлайн в будущем (позже сегодняшнего дня), по возрастанию даты;
    2 — задач вообще без дедлайна — в самый конец, отдельной группой.
    """
    if task.deadline is None:
        return (2, datetime.max)

    today_end = datetime.combine(date.today(), datetime.max.time())
    if task.deadline <= today_end:
        return (0, task.deadline)
    return (1, task.deadline)


async def get_active_tasks_by_deadline(user_id: int) -> list[Task]:
    """
    То же самое, что get_active_tasks, но отсортировано специально под
    команду /tasks: сначала просроченные и "горящие" сегодня задачи,
    затем остальные по возрастанию дедлайна, и в конце — задачи совсем
    без срока.
    """
    tasks = await get_active_tasks(user_id)
    return sorted(tasks, key=_deadline_sort_key)


async def _authorized_task(session, task_id: int, user_id: int) -> Task | None:
    """
    Общая проверка доступа к задаче — используется ВМЕСТО прямого
    "Task.user_id == user_id" везде, где раньше был единоличный доступ
    только у владельца (get_task, mark_task_done, set_task_priority,
    update_task_title, delete_task, set_task_deadline).

    Доступ разрешён, если:
    1. user_id — настоящий владелец задачи (как и было всегда), ЛИБО
    2. задача явно помечена "общей" (Task.shared=True, см.
       toggle_task_shared) И user_id — партнёр владельца (то есть у обоих
       один и тот же User.family_id, см. get_partner/accept_partner_invite).

    Партнёр получает ПОЛНЫЕ права редактирования на общую задачу (менять
    текст, приоритет, срок, отмечать выполненной, удалять) — ровно как
    сам владелец, никаких урезанных прав "только просмотр". Владелец при
    этом остаётся единственным, кто решает, сделать ли задачу общей
    вообще (см. toggle_task_shared) — эта проверка того НЕ затрагивает.
    """
    task = await session.get(Task, task_id)
    if task is None:
        return None
    if task.user_id == user_id:
        return task
    if not task.shared:
        return None

    owner = await session.get(User, task.user_id)
    acting_user = await session.get(User, user_id)
    if owner is None or acting_user is None:
        return None
    if owner.family_id is None or acting_user.family_id is None:
        return None
    if owner.family_id != acting_user.family_id:
        return None
    return task


async def mark_task_done(task_id: int, user_id: int) -> bool:
    """
    Отмечает задачу как выполненную.

    Проверка доступа — через _authorized_task (владелец или партнёр по
    общей задаче, см. её докстринг). Отдельно проверяем status ==
    in_progress — чтобы ПОВТОРНОЕ нажатие на кнопку "Выполнено" (например,
    если она случайно нажалась дважды подряд, пока Telegram ещё не успел
    перерисовать сообщение) не засчитывалось второй раз и не начисляло XP
    повторно за одну и ту же задачу.

    Возвращает True, только если задача была найдена, доступна этому
    пользователю И ещё не была выполнена раньше.
    """
    async with async_session() as session:
        task = await _authorized_task(session, task_id, user_id)
        if task is None or task.status != Status.in_progress:
            return False
        task.status = Status.done
        task.completed_at = datetime.now()
        await session.commit()
        return True


async def set_task_priority(task_id: int, user_id: int, priority: Priority) -> bool:
    """
    Устанавливает приоритет задачи (используется после нажатия на кнопку
    🟢/🟡/🔴 под сообщением о добавленной задаче). Доступ — через
    _authorized_task (владелец или партнёр по общей задаче).
    """
    async with async_session() as session:
        task = await _authorized_task(session, task_id, user_id)
        if task is None:
            return False
        task.priority = priority
        await session.commit()
        return True


async def update_task_title(task_id: int, user_id: int, title: str) -> bool:
    """Меняет текст задачи (карточка задачи → "✏️ Изменить текст"). Доступ —
    через _authorized_task (владелец или партнёр по общей задаче)."""
    async with async_session() as session:
        task = await _authorized_task(session, task_id, user_id)
        if task is None:
            return False
        task.title = title
        await session.commit()
        return True


async def delete_task(task_id: int, user_id: int) -> bool:
    """
    Удаляет задачу целиком вместе со всеми её напоминаниями (relationship
    Task.reminders объявлена с cascade="all, delete-orphan" — SQLAlchemy
    сам удалит связанные строки reminders). Постановку/снятие таймеров в
    APScheduler делает вызывающий код (services/scheduler.py) — здесь
    только работа с базой. Доступ — через _authorized_task (владелец или
    партнёр по общей задаче — партнёр тоже может удалить общую задачу, у
    него полные права на неё).
    """
    async with async_session() as session:
        task = await _authorized_task(session, task_id, user_id)
        if task is None:
            return False
        await session.delete(task)
        await session.commit()
        return True


async def get_task(task_id: int, user_id: int) -> Task | None:
    """
    Возвращает задачу по id, если она доступна указанному пользователю —
    владельцу, либо партнёру по общей задаче (см. _authorized_task),
    иначе None. Используется, чтобы показать свежий текст задачи после
    изменения приоритета/дедлайна.
    """
    async with async_session() as session:
        return await _authorized_task(session, task_id, user_id)


async def add_xp(user_id: int, amount: int) -> int:
    """
    Прибавляет пользователю очки опыта (XP) и возвращает НОВОЕ общее
    количество XP. Используется при отметке задачи выполненной
    (геймификация, Шаг 2).
    """
    async with async_session() as session:
        user = await session.get(User, user_id)
        if user is None:
            # В норме такого быть не должно — пользователь уже должен
            # существовать после /start. Подстраховываемся, чтобы бот
            # не упал, если запись почему-то отсутствует.
            user = User(user_id=user_id, xp_points=0)
            session.add(user)
        user.xp_points += amount
        await session.commit()
        return user.xp_points


async def get_user(user_id: int) -> User | None:
    """Возвращает пользователя по id (или None, если его нет в БД)."""
    async with async_session() as session:
        return await session.get(User, user_id)


# --- Подписка Premium (оплата звёздами Telegram, см. handlers/subscription.py) ---

def is_premium_active(user: User) -> bool:
    """
    Действует ли Premium прямо сейчас. Намеренно чистая функция без
    похода в БД — принимает уже загруженного User (см. User.premium_until),
    чтобы код, который и так уже получил пользователя, не делал лишний
    запрос только ради этой проверки.
    """
    return user.premium_until is not None and user.premium_until > datetime.now()


async def extend_premium(user_id: int, days: int = 30) -> datetime:
    """
    Продлевает Premium на `days` дней и возвращает новую дату окончания.

    Если подписка ещё активна — прибавляет дни К ТЕКУЩЕЙ дате окончания
    (несколько платежей подряд честно складываются, а не "теряют" уже
    оплаченный остаток). Если подписки нет или она уже истекла — отсчёт
    идёт от текущего момента.
    """
    async with async_session() as session:
        user = await session.get(User, user_id)
        if user is None:
            user = User(user_id=user_id)
            session.add(user)

        base = user.premium_until if (user.premium_until and user.premium_until > datetime.now()) else datetime.now()
        user.premium_until = base + timedelta(days=days)
        await session.commit()
        return user.premium_until


# Дата "бесконечного" Premium для ручных исключений (см. grant_lifetime_premium)
# — просто далёкое будущее, а не отдельный флаг "is_lifetime": is_premium_active
# как обычно сравнивает premium_until с datetime.now(), никакой отдельной
# ветки логики под "вечный" Premium не нужно вообще нигде в коде.
_LIFETIME_PREMIUM_UNTIL = datetime(2099, 1, 1)


async def grant_lifetime_premium(user_id: int) -> datetime:
    """
    Выдаёт Premium "навсегда" вручную — единственный способ сделать
    личное исключение для конкретного человека (см. handlers/admin.py::
    cmd_grant_premium, доступно только владелице бота). Не отдельная
    система прав, а просто premium_until в очень далёком будущем — так
    это исключение автоматически участвует во всех проверках Premium
    (партнёрский режим и т.д.) наравне с обычной платной подпиской.
    """
    async with async_session() as session:
        user = await session.get(User, user_id)
        if user is None:
            user = User(user_id=user_id)
            session.add(user)
        user.premium_until = _LIFETIME_PREMIUM_UNTIL
        await session.commit()
        return _LIFETIME_PREMIUM_UNTIL


async def revoke_premium(user_id: int) -> bool:
    """
    Снимает Premium немедленно (и оплаченный, и выданный вручную через
    grant_lifetime_premium/extend_premium) — обратная операция для
    /revoke_premium (см. handlers/admin.py). Возвращает False, если
    пользователь не найден вообще.
    """
    async with async_session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return False
        user.premium_until = None
        await session.commit()
        return True


# --- Партнёрский режим (экран "👥 Партнёр", Premium-фича) -----------------------
#
# Пара хранится максимально просто, без отдельной таблицы: у обоих
# участников пары User.family_id проставляется в ОДНО И ТО ЖЕ значение
# (id того, кто первым создал приглашение) — этого достаточно, чтобы
# симметрично находить партнёра друг друга (см. get_partner) и проверять
# доступ к общим задачам (см. _authorized_task выше). Само приглашение —
# это одноразовый код в User.partner_invite_code, из которого строится
# диплинк t.me/<bot>?start=pair_<code> (см. handlers/partner.py,
# handlers/start.py).


@dataclass
class PairResult:
    """Результат попытки принять приглашение в пару (см. accept_partner_invite)."""
    ok: bool
    reason: str = ""  # "invalid_code" / "self_invite" / "already_paired" / "inviter_already_paired"
    partner: User | None = None  # тот, с кем только что образовалась пара (при ok=True)


async def get_partner(user_id: int) -> User | None:
    """
    Возвращает текущего партнёра пользователя, если пара уже образована
    (см. User.family_id), иначе None. Симметрично работает для ОБЕИХ
    сторон пары — не важно, кто именно когда-то был инициатором.
    """
    async with async_session() as session:
        user = await session.get(User, user_id)
        if user is None or user.family_id is None:
            return None
        result = await session.execute(
            select(User).where(User.family_id == user.family_id, User.user_id != user_id)
        )
        return result.scalar_one_or_none()


async def create_partner_invite(user_id: int) -> str:
    """
    Выпускает новый код приглашения для диплинка (кнопка "🔗 Получить
    ссылку-приглашение" на экране "👥 Партнёр") — вызывающий код
    (handlers/partner.py) сам проверяет Premium и отсутствие уже
    привязанного партнёра ДО вызова этой функции. Каждый вызов
    перезаписывает предыдущий код — старая ссылка (если её кто-то не успел
    открыть) автоматически становится недействительной.
    """
    code = secrets.token_urlsafe(6)
    async with async_session() as session:
        user = await session.get(User, user_id)
        if user is None:
            user = User(user_id=user_id)
            session.add(user)
        user.partner_invite_code = code
        await session.commit()
        return code


async def accept_partner_invite(code: str, acceptor_user_id: int) -> PairResult:
    """
    Принимает приглашение по коду из диплинка (/start pair_<code>, см.
    handlers/start.py). Проверки — по порядку:
    1. код существует (кто-то с таким partner_invite_code найден);
    2. это не сам инициатор (нельзя пригласить самого себя той же ссылкой);
    3. у принимающего ещё нет своего партнёра;
    4. у пригласившего тоже ещё нет партнёра (на случай, если он успел
       где-то ещё создать пару, пока ссылка гуляла).

    При успехе — оба получают одинаковый family_id (id пригласившего), а
    код приглашения сбрасывается, чтобы им нельзя было воспользоваться
    повторно.
    """
    async with async_session() as session:
        result = await session.execute(select(User).where(User.partner_invite_code == code))
        inviter = result.scalar_one_or_none()
        if inviter is None:
            return PairResult(ok=False, reason="invalid_code")
        if inviter.user_id == acceptor_user_id:
            return PairResult(ok=False, reason="self_invite")

        acceptor = await session.get(User, acceptor_user_id)
        if acceptor is None:
            acceptor = User(user_id=acceptor_user_id)
            session.add(acceptor)
            await session.flush()

        if acceptor.family_id is not None:
            return PairResult(ok=False, reason="already_paired")
        if inviter.family_id is not None:
            return PairResult(ok=False, reason="inviter_already_paired")

        family_id = inviter.user_id
        inviter.family_id = family_id
        acceptor.family_id = family_id
        inviter.partner_invite_code = None
        await session.commit()
        await session.refresh(inviter)
        return PairResult(ok=True, partner=inviter)


async def unlink_partner(user_id: int) -> User | None:
    """
    Разрывает пару (кнопка "🔓 Отвязать партнёра") — снимает family_id у
    ОБОИХ участников сразу (иначе один считался бы всё ещё в паре, пока
    сам не отвяжется тоже). Общие задачи (Task.shared) при этом НЕ
    трогаем и не разделяем обратно — они просто перестают быть видны
    партнёру, т.к. доступ по _authorized_task проверяет живую пару через
    family_id, а не хранит его на самой задаче. Возвращает отвязанного
    партнёра (для прощального уведомления ему), либо None, если пары и не
    было.
    """
    async with async_session() as session:
        user = await session.get(User, user_id)
        if user is None or user.family_id is None:
            return None
        result = await session.execute(
            select(User).where(User.family_id == user.family_id, User.user_id != user_id)
        )
        partner = result.scalar_one_or_none()
        user.family_id = None
        if partner is not None:
            partner.family_id = None
        await session.commit()
        return partner


# --- Настройки уведомлений (экран "🔔 Уведомления" в профиле) -------------------

async def toggle_reminders_enabled(user_id: int) -> bool:
    """Переключает общий рубильник напоминаний по задачам и возвращает
    новое значение (см. User.reminders_enabled)."""
    async with async_session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return True
        user.reminders_enabled = not user.reminders_enabled
        await session.commit()
        return user.reminders_enabled


async def toggle_quiet_hours_enabled(user_id: int) -> bool:
    """Переключает "Тихие часы" (22:00–08:00) и возвращает новое значение
    (см. User.quiet_hours_enabled)."""
    async with async_session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return True
        user.quiet_hours_enabled = not user.quiet_hours_enabled
        await session.commit()
        return user.quiet_hours_enabled


async def toggle_morning_checklist_enabled(user_id: int) -> bool:
    """Переключает утренний чек-лист (09:00) и возвращает новое значение
    (см. User.morning_checklist_enabled)."""
    async with async_session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return True
        user.morning_checklist_enabled = not user.morning_checklist_enabled
        await session.commit()
        return user.morning_checklist_enabled


async def get_users_with_morning_checklist_enabled() -> list[User]:
    """Все пользователи, у кого включён утренний чек-лист — используется
    ежедневной сводкой в 09:00 (см. services.scheduler._send_morning_checklists)."""
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.morning_checklist_enabled.is_(True))
        )
        return list(result.scalars().all())


async def toggle_checklist_morning_push_enabled(user_id: int) -> bool:
    """Переключает утренний пуш-приглашение в интерактивный "☀️ Чек-лист
    дня" (не путать с toggle_morning_checklist_enabled — это ДРУГОЙ,
    пассивный текстовый дайджест) и возвращает новое значение."""
    async with async_session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return True
        user.checklist_morning_push_enabled = not user.checklist_morning_push_enabled
        await session.commit()
        return user.checklist_morning_push_enabled


async def toggle_checklist_evening_push_enabled(user_id: int) -> bool:
    """Переключает вечернюю мягкую сводку по чек-листу дня и возвращает
    новое значение (см. User.checklist_evening_push_enabled)."""
    async with async_session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return True
        user.checklist_evening_push_enabled = not user.checklist_evening_push_enabled
        await session.commit()
        return user.checklist_evening_push_enabled


async def get_users_with_checklist_morning_push_enabled() -> list[User]:
    """Пользователи с включённым утренним пушем-приглашением в чек-лист
    дня (см. services.scheduler._send_checklist_morning_briefs)."""
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.checklist_morning_push_enabled.is_(True))
        )
        return list(result.scalars().all())


async def get_users_with_checklist_evening_push_enabled() -> list[User]:
    """Пользователи с включённой вечерней сводкой по чек-листу дня (см.
    services.scheduler._send_checklist_evening_summaries)."""
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.checklist_evening_push_enabled.is_(True))
        )
        return list(result.scalars().all())


async def get_tasks_due_today_or_overdue(user_id: int) -> list[Task]:
    """
    Активные задачи с дедлайном сегодня или раньше (просроченные) —
    ровно то же самое, что категория 0 в texts.task_urgency_category
    ("🔥 горят сегодня"), просто как отдельный запрос к БД для утреннего
    чек-листа (см. services.scheduler._send_morning_checklists), где нужен
    список задач ОДНОГО конкретного пользователя, а не список из уже
    полученных объектов.
    """
    today_end = datetime.combine(date.today(), datetime.max.time())
    async with async_session() as session:
        result = await session.execute(
            select(Task)
            .where(
                Task.user_id == user_id,
                Task.status == Status.in_progress,
                Task.deadline.is_not(None),
                Task.deadline <= today_end,
            )
            .order_by(Task.deadline)
        )
        return list(result.scalars().all())


async def count_completed_tasks(user_id: int) -> int:
    """
    Считает, сколько задач пользователь ЗАКРЫЛ за всё время (используется
    в карточке профиля — "✅ Закрыто задач: N").
    """
    async with async_session() as session:
        result = await session.execute(
            select(func.count()).select_from(Task).where(
                Task.user_id == user_id, Task.status == Status.done
            )
        )
        return result.scalar_one()


async def update_streak(user_id: int) -> int:
    """
    Обновляет "серию активности" (сколько дней подряд пользователь закрывает
    хотя бы одну задачу) и возвращает актуальное значение. Вызывается при
    КАЖДОМ успешном закрытии задачи (см. complete_task в handlers/tasks.py).

    Логика:
    - Если сегодня уже засчитано (last_active_date == сегодня) — ничего не
      меняем, чтобы несколько задач за один день не "накручивали" серию.
    - Если последняя активность была ровно ВЧЕРА — увеличиваем серию на 1.
    - Если пропущен РОВНО один день, но доступна недельная заморозка
      ("Выходной для кота", см. _freeze_available) — она списывается
      автоматически, и серия продолжается как ни в чём не бывало. Это
      подстраховка на случай, если человек закрыл задачу, ни разу не
      заглянув в профиль (иначе заморозка сработала бы только там, см.
      get_streak_status).
    - Иначе (самый первый раз, либо пропуск в 2+ дня без доступной
      заморозки) — серия начинается заново, с 1.

    ВАЖНО: это поле обновляется только в момент закрытия задачи, поэтому
    само по себе оно может быть "устаревшим" (см. get_streak_status
    ниже — именно она используется для ОТОБРАЖЕНИЯ в профиле, с учётом
    заморозки и предложения ручного спасения серии).
    """
    today = date.today()

    async with async_session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return 0

        if user.last_active_date == today:
            pass  # сегодняшний день уже учтён — ничего не меняем
        elif user.last_active_date == today - timedelta(days=1):
            user.streak_days += 1
        elif (
            user.last_active_date is not None
            and (today - user.last_active_date).days == 2
            and _freeze_available(user, today)
        ):
            user.last_freeze_used_date = today
            user.streak_days += 1
        else:
            user.streak_days = 1

        user.last_active_date = today
        await session.commit()
        return user.streak_days


def effective_streak_days(user: User) -> int:
    """
    "Живое" значение серии для ОТОБРАЖЕНИЯ в профиле (в отличие от
    user.streak_days — сырого значения из БД).

    Баг, который это чинит: streak_days обновляется только в момент
    закрытия задачи. Если человек несколько дней подряд не открывал бота
    и ничего не закрывал, столбец в БД так и останется старым числом
    (например, "1") — и профиль наврёт, что серия ещё жива, хотя на самом
    деле она уже прервалась.

    Здесь же на лету проверяем: если последняя активность была СЕГОДНЯ или
    ВЧЕРА — серия действительно ещё жива, показываем как есть (человек
    ещё может сегодня закрыть задачу и продолжить её). Если разрыв больше
    одного дня — серия прервалась, показываем 0. Реальное обновление
    streak_days = 1 в БД произойдёт при следующем закрытии задачи (см.
    update_streak выше) — здесь мы ничего не пишем в БД, только считаем
    значение для показа.
    """
    if user.last_active_date is None:
        return 0

    gap_days = (date.today() - user.last_active_date).days
    if gap_days <= 1:
        return user.streak_days
    return 0


# --- Защита серии от сгорания (Streak Freeze) ----------------------------------
#
# Два механизма сразу, по твоему выбору "оба варианта вместе":
# 1. "Выходной для кота" — автоматическая заморозка, не чаще раза в 7 дней:
#    если пропущен РОВНО один день, серия не рвётся сама по себе, без
#    каких-либо действий пользователя.
# 2. Ручное спасение за XP — подстраховка на случай, если авто-заморозка
#    уже потрачена на этой неделе, а человек всё равно не хочет терять
#    серию: можно "докупить" сохранение за фиксированную цену в XP.
#
# Оба варианта работают только для пропуска РОВНО в один день (gap_days == 2:
# сегодня минюс последняя активность). Более длинные разрывы — серия
# считается по-настоящему прерванной, ни заморозка, ни спасение её уже не
# вернут (иначе это обесценило бы саму идею "серии подряд").

_FREEZE_COOLDOWN_DAYS = 7
STREAK_RESCUE_XP_COST = 50


def _freeze_available(user: User, today: date) -> bool:
    """Доступна ли автоматическая недельная заморозка прямо сейчас —
    то есть прошло ли уже 7 дней с прошлого её использования (или она ещё
    ни разу не использовалась)."""
    return (
        user.last_freeze_used_date is None
        or (today - user.last_freeze_used_date).days >= _FREEZE_COOLDOWN_DAYS
    )


@dataclass
class StreakStatus:
    streak_days: int  # что показывать как текущее число в профиле
    frozen_today: bool  # авто-заморозка только что сработала — показать плашку об этом
    rescue_available: bool  # можно предложить кнопку "Спасти серию за N XP"
    rescue_cost: int  # цена ручного спасения (см. STREAK_RESCUE_XP_COST)
    at_risk_days: int = 0  # сколько дней серии "на кону", если предложено спасение
    just_reset: bool = False  # серия ТОЛЬКО ЧТО обнаружена по-настоящему прервавшейся
    # (пропуск 2+ дней, заморозка и спасение уже не действуют) И до этого
    # реально была живая серия — тогда handlers/profile.py покажет мягкую
    # реплику про сброс (texts.streak_reset_text) вместо молчаливого 0.
    # Если серии и не было (streak_days уже был 0), лишний раз "утешать"
    # не нужно — там и утешать не о чем.


async def get_streak_status(user_id: int) -> StreakStatus:
    """
    "Живой" статус серии для карточки профиля — то, что раньше делала
    effective_streak_days(), но с учётом заморозки. Отличие от update_streak
    (тот трогает streak_days только в момент закрытия задачи): эта функция
    вызывается при КАЖДОМ открытии профиля, поэтому именно здесь заморозка
    может сработать даже ДО следующего закрытия задачи — иначе человек
    увидел бы серию уже обнулённой в день пропуска, хотя заморозка как раз
    должна была его прикрыть.
    """
    today = date.today()
    async with async_session() as session:
        user = await session.get(User, user_id)
        if user is None or user.last_active_date is None:
            return StreakStatus(
                streak_days=0, frozen_today=False, rescue_available=False, rescue_cost=STREAK_RESCUE_XP_COST
            )

        gap_days = (today - user.last_active_date).days

        if gap_days <= 1:
            return StreakStatus(
                streak_days=user.streak_days,
                frozen_today=False,
                rescue_available=False,
                rescue_cost=STREAK_RESCUE_XP_COST,
            )

        if gap_days == 2:
            if _freeze_available(user, today):
                # "Чиним" разрыв — как будто вчера пользователь тоже был
                # активен. Дальше (в т.ч. в update_streak) серия ведёт себя
                # как обычная, без пропуска.
                user.last_freeze_used_date = today
                user.last_active_date = today - timedelta(days=1)
                await session.commit()
                return StreakStatus(
                    streak_days=user.streak_days,
                    frozen_today=True,
                    rescue_available=False,
                    rescue_cost=STREAK_RESCUE_XP_COST,
                )

            rescue_available = user.xp_points >= STREAK_RESCUE_XP_COST
            return StreakStatus(
                streak_days=0,
                frozen_today=False,
                rescue_available=rescue_available,
                rescue_cost=STREAK_RESCUE_XP_COST,
                at_risk_days=user.streak_days,
            )

        # Разрыв в 2+ полных дня — заморозка и спасение уже не действуют.
        # just_reset=True только если ДО этого была реальная серия (>0) —
        # тогда стоит мягко сказать об этом человеку (см. streak_reset_text),
        # а не молчать, будто ничего не произошло.
        return StreakStatus(
            streak_days=0,
            frozen_today=False,
            rescue_available=False,
            rescue_cost=STREAK_RESCUE_XP_COST,
            just_reset=user.streak_days > 0,
        )


async def rescue_streak_with_xp(user_id: int) -> bool:
    """
    Кнопка "💎 Спасти серию за N XP" в профиле — ручное спасение, когда
    авто-заморозка уже использована на этой неделе. Работает в тех же
    условиях, что и авто-заморозка (пропущен РОВНО один день) — списывает
    XP и "чинит" last_active_date, как будто вчера пользователь был активен.

    Возвращает False, если спасать уже нечего (гэп не в 1 день) или не
    хватает XP — тогда вызывающий код (handlers/profile.py) покажет
    соответствующее сообщение.
    """
    today = date.today()
    async with async_session() as session:
        user = await session.get(User, user_id)
        if user is None or user.last_active_date is None:
            return False

        gap_days = (today - user.last_active_date).days
        if gap_days != 2:
            return False
        if user.xp_points < STREAK_RESCUE_XP_COST:
            return False

        user.xp_points -= STREAK_RESCUE_XP_COST
        user.last_active_date = today - timedelta(days=1)
        await session.commit()
        return True


# --- Дедлайны и напоминания (Time Management Module) --------------------------

# Смещение каждого варианта напоминания относительно дедлайна задачи.
# ReminderOffset.exact — особый случай, "смещение" равно нулю (напомнить
# ровно в момент дедлайна).
REMINDER_OFFSET_DELTAS: dict[ReminderOffset, timedelta] = {
    ReminderOffset.days_7: timedelta(days=7),
    ReminderOffset.days_5: timedelta(days=5),
    ReminderOffset.days_3: timedelta(days=3),
    ReminderOffset.days_1: timedelta(days=1),
    ReminderOffset.hour_1: timedelta(hours=1),
    ReminderOffset.minutes_15: timedelta(minutes=15),
    ReminderOffset.exact: timedelta(0),
}


def available_reminder_offsets(deadline: datetime) -> list[ReminderOffset]:
    """
    Отбирает варианты напоминания, которые ещё физически МОГУТ сработать
    для данного дедлайна — то есть (дедлайн - смещение) всё ещё в будущем.

    Нужно, чтобы не предлагать в меню напоминаний варианты, которые уже
    "прошли": если дедлайн сегодня в 19:00, а сейчас 17:50, то "За 7 дней"/
    "За 5 дней"/"За 1 день" сработали бы уже в прошлом, и показывать их
    смысла нет (см. keyboards.reminders_keyboard, handlers/tasks.py).
    ReminderOffset.exact всегда доступен, пока сам дедлайн в будущем (его
    смещение — timedelta(0), то есть remind_at == deadline).
    """
    now = datetime.now()
    return [
        offset for offset in ReminderOffset
        if deadline - REMINDER_OFFSET_DELTAS[offset] > now
    ]


async def set_task_deadline(
    task_id: int, user_id: int, deadline: datetime | None, all_day: bool = False
) -> bool:
    """
    Устанавливает (или снимает, если deadline=None) дедлайн задачи.
    all_day=True — дедлайн выбран кнопкой "☀️ В течение дня" (без точного
    часа, см. Task.deadline_all_day). Доступ — через _authorized_task
    (владелец или партнёр по общей задаче).
    """
    async with async_session() as session:
        task = await _authorized_task(session, task_id, user_id)
        if task is None:
            return False
        task.deadline = deadline
        task.deadline_all_day = all_day
        await session.commit()
        return True


async def set_task_shared(task_id: int, user_id: int, shared: bool) -> Task | None:
    """
    Явно выставляет "личная/общая" (Task.shared) в конкретное значение —
    в отличие от toggle_task_shared (переключает наоборот), используется
    шагом "Личное/Партнёр" в мастере создания задачи (handlers/tasks.py::
    _offer_sharing_or_finish/share_choice), где выбор всегда явный
    ("🔒 Личное" или "👥 Партнёру"), а не переключение состояния. Как и
    toggle_task_shared — доступно ТОЛЬКО настоящему владельцу задачи.
    Возвращает обновлённую задачу, либо None, если задача не найдена или
    user_id не её владелец.
    """
    async with async_session() as session:
        task = await session.get(Task, task_id)
        if task is None or task.user_id != user_id:
            return None
        task.shared = shared
        await session.commit()
        await session.refresh(task)
        return task


async def toggle_task_shared(task_id: int, user_id: int) -> Task | None:
    """
    Переключает "личная/общая" (Task.shared) — В ОТЛИЧИЕ от остальных
    операций выше, доступно ТОЛЬКО настоящему владельцу задачи, не через
    _authorized_task: партнёр не может решать за другого, каким его
    задачам "быть общими" — это выбор делает только тот, кому задача
    принадлежит. Возвращает обновлённую задачу, либо None, если задача не
    найдена или user_id не её владелец.
    """
    async with async_session() as session:
        task = await session.get(Task, task_id)
        if task is None or task.user_id != user_id:
            return None
        task.shared = not task.shared
        await session.commit()
        await session.refresh(task)
        return task


async def get_task_reminders(task_id: int) -> list[Reminder]:
    """Возвращает все ещё НЕ отправленные напоминания задачи (используется,
    чтобы понять, какие чекбоксы должны быть отмечены в меню напоминаний,
    и что показать в карточке задачи)."""
    async with async_session() as session:
        result = await session.execute(
            select(Reminder).where(Reminder.task_id == task_id, Reminder.sent.is_(False))
        )
        return list(result.scalars().all())


async def add_reminder(task_id: int, offset: ReminderOffset, remind_at: datetime) -> Reminder:
    """
    Создаёт запись напоминания в БД. Постановку в APScheduler делает
    вызывающий код (services/scheduler.py::schedule_reminder) — здесь
    только работа с базой.
    """
    async with async_session() as session:
        reminder = Reminder(task_id=task_id, offset=offset, remind_at=remind_at, sent=False)
        session.add(reminder)
        await session.commit()
        await session.refresh(reminder)
        return reminder


async def remove_reminder(task_id: int, offset: ReminderOffset) -> bool:
    """
    Удаляет напоминание задачи с указанным смещением (если оно есть) —
    срабатывает, когда пользователь СНИМАЕТ галочку в меню напоминаний.
    Возвращает True, если запись действительно была и её удалили (тогда
    вызывающий код должен снять соответствующий таймер в APScheduler).
    """
    async with async_session() as session:
        result = await session.execute(
            select(Reminder).where(Reminder.task_id == task_id, Reminder.offset == offset)
        )
        reminder = result.scalar_one_or_none()
        if reminder is None:
            return False
        await session.delete(reminder)
        await session.commit()
        return True


async def clear_task_reminders(task_id: int) -> None:
    """
    Удаляет ВСЕ ещё не отправленные напоминания задачи разом — используется
    кнопкой "🔕 Без напоминаний", выбором "⚪️ Без срока"/"☀️ В течение дня",
    удалением задачи и её закрытием (не нужно напоминать про уже сделанное).

    Снятие соответствующих таймеров в APScheduler — забота вызывающего
    кода (services/scheduler.py::unschedule_all_for_task) — job_id там
    строится из task_id и offset'а, а не из id записи в БД, поэтому знать
    заранее, какие именно записи были удалены, не нужно.
    """
    async with async_session() as session:
        result = await session.execute(
            select(Reminder).where(Reminder.task_id == task_id, Reminder.sent.is_(False))
        )
        for reminder in result.scalars().all():
            await session.delete(reminder)
        await session.commit()


async def recompute_reminders_for_new_deadline(
    task_id: int, new_deadline: datetime
) -> tuple[list[Reminder], list[ReminderOffset]]:
    """
    После правки дедлайна уже существующей задачи (карточка → "📅 Изменить
    дату / время") пересчитывает remind_at всех уже выбранных напоминаний
    под новый дедлайн — сами напоминания (и то, что было выбрано) остаются,
    просто "едут" вместе с новой датой/временем.

    Если пересчитанное время оказалось в прошлом — такое напоминание молча
    удаляется целиком (бессмысленно напоминать о том, что уже наступило).

    Возвращает (оставшиеся_напоминания, удалённые_offset'ы) — второе нужно
    вызывающему коду, чтобы снять соответствующие таймеры в APScheduler.
    """
    now = datetime.now()
    async with async_session() as session:
        result = await session.execute(
            select(Reminder).where(Reminder.task_id == task_id, Reminder.sent.is_(False))
        )
        reminders = list(result.scalars().all())

        kept: list[Reminder] = []
        removed_offsets: list[ReminderOffset] = []
        for reminder in reminders:
            new_remind_at = new_deadline - REMINDER_OFFSET_DELTAS[reminder.offset]
            if new_remind_at <= now:
                removed_offsets.append(reminder.offset)
                await session.delete(reminder)
            else:
                reminder.remind_at = new_remind_at
                kept.append(reminder)

        await session.commit()
        for reminder in kept:
            await session.refresh(reminder)
        return kept, removed_offsets


async def get_reminder_with_task(reminder_id: int) -> tuple[Reminder, Task] | None:
    """
    Возвращает напоминание вместе с его задачей одним запросом. Нужно
    и планировщику (services/scheduler.py) в момент срабатывания таймера,
    и меню "💤 Отложить" — там под рукой есть только reminder_id, а для
    текста нужны название задачи, её дедлайн и user_id (chat_id).
    """
    async with async_session() as session:
        reminder = await session.get(Reminder, reminder_id)
        if reminder is None:
            return None
        task = await session.get(Task, reminder.task_id)
        if task is None:
            return None
        return reminder, task


async def mark_reminder_sent(reminder_id: int) -> None:
    """Помечает напоминание отправленным — чтобы не отправить его повторно
    (например, если бот перезапустится сразу после отправки)."""
    async with async_session() as session:
        await session.execute(
            update(Reminder).where(Reminder.reminder_id == reminder_id).values(sent=True)
        )
        await session.commit()


async def mark_reminder_second_chance_sent(reminder_id: int) -> None:
    """Помечает, что мягкий "второй шанс" по этому напоминанию уже
    отправлен — чтобы не прислать его повторно (см.
    services.scheduler._maybe_send_second_chance)."""
    async with async_session() as session:
        await session.execute(
            update(Reminder).where(Reminder.reminder_id == reminder_id).values(second_chance_sent=True)
        )
        await session.commit()


async def get_pending_reminders() -> list[tuple[Reminder, Task]]:
    """
    Возвращает ВСЕ ещё не отправленные напоминания вместе с их задачами.
    Используется один раз при старте бота (main.py → services.scheduler.
    resync_reminders), чтобы заново поставить таймеры в APScheduler — сам
    планировщик хранит расписание только в памяти процесса и "забывает"
    его при каждом перезапуске бота.
    """
    async with async_session() as session:
        result = await session.execute(
            select(Reminder, Task)
            .join(Task, Task.task_id == Reminder.task_id)
            .where(Reminder.sent.is_(False))
        )
        return [(reminder, task) for reminder, task in result.all()]


async def reschedule_reminder(reminder_id: int, new_time: datetime) -> Reminder | None:
    """
    Переносит СУЩЕСТВУЮЩЕЕ напоминание на конкретное новое время — общая
    функция под все варианты меню "💤 Отложить" (быстрый сдвиг на N минут,
    "на завтра утро/вечер", и полная перенастройка через календарь и
    барабан времени). Не создаёт новую запись, переиспользует ту же самую.
    Возвращает обновлённое напоминание, либо None, если такого не нашлось.
    """
    async with async_session() as session:
        reminder = await session.get(Reminder, reminder_id)
        if reminder is None:
            return None
        reminder.remind_at = new_time
        reminder.sent = False
        await session.commit()
        await session.refresh(reminder)
        return reminder


# --- Модуль "☀️ Чек-лист дня" ---------------------------------------------------
#
# Автономная архитектура (по твоему требованию — никакого ручного
# дублирования между списком задач и чек-листом):
# 1. Обычные задачи с дедлайном СЕГОДНЯ попадают в чек-лист сами, без
#    отдельного действия пользователя (см. get_checklist_tasks_for_today).
# 2. Привычки/рутины (Habit) — отдельная сущность, не задача: не имеют
#    дедлайна и не пересоздаются каждый день, только сбрасывают
#    done_today/hidden_today в полночь (см. services.scheduler.
#    _reset_daily_habits).
# "Убрать из дня" для задачи — это НЕ удаление, а просто снятие сегодняшнего
# дедлайна (set_task_deadline(..., None)) — та же самая функция, что и
# кнопка "⚪️ Без срока" в календаре, здесь отдельная функция не нужна,
# вызывающий код (handlers/checklist.py) переиспользует set_task_deadline
# напрямую.


async def get_checklist_tasks_for_today(user_id: int) -> list[Task]:
    """
    Задачи, автоматически попадающие в сегодняшний "☀️ Чек-лист дня" —
    активные задачи с дедлайном РОВНО сегодня (независимо от того, был ли
    дедлайн выбран точным временем или пресетом "☀️ В течение дня", см.
    Task.deadline_all_day). В отличие от get_tasks_due_today_or_overdue
    (утренний ТЕКСТОВЫЙ дайджест) сюда просроченные из ПРОШЛЫХ дней не
    попадают — фокус дня это именно сегодня, просроченные задачи видно в
    обычном списке "📋 Мои задачи" (там они всегда наверху).

    Порядок: сначала задачи с точным временем (по возрастанию часа) —
    они "горят" конкретнее, — потом задачи без часа ("В течение дня").
    """
    today = date.today()
    day_start = datetime.combine(today, datetime.min.time())
    day_end = datetime.combine(today, datetime.max.time())
    async with async_session() as session:
        result = await session.execute(
            select(Task)
            .where(
                Task.user_id == user_id,
                Task.status == Status.in_progress,
                Task.deadline.is_not(None),
                Task.deadline >= day_start,
                Task.deadline <= day_end,
            )
            .order_by(Task.deadline_all_day, Task.deadline)
        )
        return list(result.scalars().all())


async def get_tasks_completed_today(user_id: int) -> list[Task]:
    """
    Задачи, закрытые СЕГОДНЯ и при этом бывшие частью сегодняшнего чек-
    листа (дедлайн тоже был на сегодня — тот же критерий, что и
    get_checklist_tasks_for_today, только уже для статуса done). Двойное
    условие (completed_at сегодня И deadline сегодня), а не просто
    "закрыто сегодня", — иначе в вечернюю сводку чек-листа дня попали бы и
    задачи, вообще не имевшие отношения к чек-листу (например, просроченная
    задача с дедлайном на прошлой неделе, закрытая только что). Используется
    только вечерней сводкой (см. services.scheduler._send_checklist_evening_summaries).
    """
    today = date.today()
    day_start = datetime.combine(today, datetime.min.time())
    day_end = datetime.combine(today, datetime.max.time())
    async with async_session() as session:
        result = await session.execute(
            select(Task)
            .where(
                Task.user_id == user_id,
                Task.status == Status.done,
                Task.completed_at.is_not(None),
                Task.completed_at >= day_start,
                Task.completed_at <= day_end,
                Task.deadline.is_not(None),
                Task.deadline >= day_start,
                Task.deadline <= day_end,
            )
        )
        return list(result.scalars().all())


async def create_habit(user_id: int, title: str, xp_reward: int = 5) -> Habit:
    """Создаёт новую постоянную привычку/рутину (кнопка "➕ Добавить
    рутину" на экране чек-листа)."""
    async with async_session() as session:
        habit = Habit(user_id=user_id, title=title, xp_reward=xp_reward)
        session.add(habit)
        await session.commit()
        await session.refresh(habit)
        return habit


async def get_habits(user_id: int) -> list[Habit]:
    """Все привычки пользователя (включая скрытые на сегодня — нужно для
    экрана настройки, где скрытые тоже должны быть видны и переключаемы),
    в порядке создания."""
    async with async_session() as session:
        result = await session.execute(
            select(Habit).where(Habit.user_id == user_id).order_by(Habit.habit_id)
        )
        return list(result.scalars().all())


async def get_visible_habits_today(user_id: int) -> list[Habit]:
    """Привычки, которые нужно показать на ГЛАВНОМ экране чек-листа сегодня
    (без временно скрытых через "👁 Скрыть на сегодня", см. Habit.hidden_today)."""
    async with async_session() as session:
        result = await session.execute(
            select(Habit)
            .where(Habit.user_id == user_id, Habit.hidden_today.is_(False))
            .order_by(Habit.habit_id)
        )
        return list(result.scalars().all())


async def get_habit(habit_id: int, user_id: int) -> Habit | None:
    """Возвращает привычку по id, если она принадлежит указанному
    пользователю (иначе None) — та же защита, что и у get_task."""
    async with async_session() as session:
        habit = await session.get(Habit, habit_id)
        if habit is None or habit.user_id != user_id:
            return None
        return habit


async def toggle_habit_done(habit_id: int, user_id: int) -> Habit | None:
    """
    Переключает чекбокс привычки на СЕГОДНЯ (▫️ ↔ ✅) прямо на главном
    экране чек-листа. Возвращает обновлённую привычку (по НОВОМУ
    done_today вызывающий код в handlers/checklist.py решает, начислять
    XP или, наоборот, списать его обратно — если человек случайно тапнул
    дважды).
    """
    async with async_session() as session:
        habit = await session.get(Habit, habit_id)
        if habit is None or habit.user_id != user_id:
            return None
        habit.done_today = not habit.done_today
        await session.commit()
        await session.refresh(habit)
        return habit


async def toggle_habit_hidden_today(habit_id: int, user_id: int) -> Habit | None:
    """Кнопка "👁 Скрыть на сегодня" / "👁 Показать" в режиме настройки чек-
    листа — временно убирает привычку из ГЛАВНОГО экрана на сегодняшний
    день, не удаляя саму привычку (см. Habit.hidden_today)."""
    async with async_session() as session:
        habit = await session.get(Habit, habit_id)
        if habit is None or habit.user_id != user_id:
            return None
        habit.hidden_today = not habit.hidden_today
        await session.commit()
        await session.refresh(habit)
        return habit


async def delete_habit(habit_id: int, user_id: int) -> bool:
    """Кнопка "🗑 Удалить привычку" в режиме настройки — насовсем удаляет
    привычку из системы (в отличие от "👁 Скрыть на сегодня", это
    необратимо, поэтому вызывающий код сначала показывает подтверждение,
    см. keyboards.habit_delete_confirm_keyboard)."""
    async with async_session() as session:
        habit = await session.get(Habit, habit_id)
        if habit is None or habit.user_id != user_id:
            return False
        await session.delete(habit)
        await session.commit()
        return True


async def reset_daily_habits() -> None:
    """
    Полуночный сброс ВСЕХ привычек всех пользователей разом: done_today и
    hidden_today возвращаются в False — новый день начинается с чистого
    листа (см. services.scheduler._reset_daily_habits, cron-job в 00:00).
    Именно за счёт этого сброса привычки не нужно пересоздавать каждый
    день вручную — это и есть автономность модуля.
    """
    async with async_session() as session:
        await session.execute(update(Habit).values(done_today=False, hidden_today=False))
        await session.commit()
