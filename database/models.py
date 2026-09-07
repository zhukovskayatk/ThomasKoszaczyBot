"""
Описание структуры базы данных (ORM-модели SQLAlchemy).

Используется асинхронный движок SQLite (через aiosqlite), поэтому все
операции с БД в проекте — асинхронные (async/await).

Таблицы заложены сразу с прицелом на будущие шаги:
- Users.family_id — понадобится для функции "совместный доступ для пар".
- Users.xp_points — понадобится для геймификации.
- Task.priority / Task.status — уже используются на этом шаге,
  но такие Enum-поля легко расширять (например, добавить статус
  'overdue' в будущем).
- Task.deadline / Task.deadline_all_day / Reminder — модуль дедлайнов и
  напоминаний (Time Management Module): у задачи может быть срок (точный
  момент времени, либо просто "в течение дня" без конкретного часа), а у
  срока — несколько запланированных напоминаний (см. database/requests.py
  и services/scheduler.py).
"""

import enum
from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Text, event
from sqlalchemy import Enum as SAEnum
from sqlalchemy.ext.asyncio import AsyncAttrs, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# --- Подключение к базе данных ---------------------------------------------

# sqlite+aiosqlite:///focus_bot.db — файл базы данных будет создан
# автоматически рядом с проектом при первом запуске.
engine = create_async_engine("sqlite+aiosqlite:///focus_bot.db", echo=False)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record) -> None:
    """
    Настройки SQLite на каждое новое соединение.

    - journal_mode=WAL и busy_timeout — чтобы параллельные записи (например,
      несколько задач подряд) не падали с ошибкой "database is locked".
    - foreign_keys=ON — включает реальную проверку внешних ключей и
      ON DELETE CASCADE. Нужно для Reminder.task_id: при удалении задачи
      (карточка задачи → "🗑 Удалить") все её напоминания удаляются сами,
      без отдельного кода.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


# Фабрика асинхронных сессий. expire_on_commit=False нужен, чтобы можно
# было спокойно читать атрибуты объекта после commit() (без лишнего
# похода в БД).
async_session = async_sessionmaker(engine, expire_on_commit=False)


# --- Базовый класс для всех моделей -----------------------------------------

class Base(AsyncAttrs, DeclarativeBase):
    """Базовый класс, от которого наследуются все таблицы."""
    pass


# --- Перечисления (Enum) -----------------------------------------------------

class Priority(str, enum.Enum):
    """
    Приоритет задачи.

    none — "⚪️ Без приоритета" (см. keyboards.priority_keyboard, 4-я кнопка
    на экране выбора приоритета): человек осознанно не хочет выбирать
    важность прямо сейчас. Хранится как обычное значение перечисления (а
    не NULL в колонке) — колонка использует native_enum=False, то есть
    физически это просто TEXT, и добавление нового значения не требует
    никакой миграции существующей таблицы. По умолчанию при создании
    задачи всё ещё используется Priority.medium (см. Task.priority ниже) —
    "Без приоритета" можно выбрать только явным кликом.
    """
    low = "Low"
    medium = "Medium"
    high = "High"
    none = "None"


class Status(str, enum.Enum):
    """Статус выполнения задачи."""
    in_progress = "in_progress"
    done = "done"


class ReminderOffset(str, enum.Enum):
    """
    Смещение напоминания относительно дедлайна задачи.

    .value — короткий код, который используется и в callback_data инлайн-
    кнопок (Telegram ограничивает callback_data 64 байтами), и в id job'ов
    планировщика (см. services/scheduler.py::_job_id). Порядок объявления —
    это же порядок кнопок в меню напоминаний.
    """
    days_7 = "7d"
    days_5 = "5d"
    days_3 = "3d"
    days_1 = "1d"
    hour_1 = "1h"
    minutes_15 = "15m"
    exact = "exact"  # напомнить ровно в момент дедлайна


# --- Таблица пользователей ---------------------------------------------------

class User(Base):
    __tablename__ = "users"

    # ID пользователя из Telegram — используем его же как первичный ключ,
    # BigInteger, т.к. id в Telegram могут быть больше, чем влезает в Integer.
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # Telegram username (может отсутствовать у пользователя, поэтому Nullable)
    username: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Очки опыта для будущей геймификации, по умолчанию 0
    xp_points: Mapped[int] = mapped_column(default=0)

    # ID "семьи"/пары для совместного доступа. Пока не используется,
    # но поле заложено заранее, чтобы не переделывать таблицу позже.
    family_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # --- "Серия активности" для карточки профиля -----------------------------
    # last_active_date — дата последнего дня, когда пользователь закрыл хотя
    # бы одну задачу. streak_days — сколько дней подряд это происходит.
    # Оба поля обновляются в database.requests.update_streak() при закрытии
    # задачи (см. handlers/tasks.py::complete_task). Для ОТОБРАЖЕНИЯ в
    # профиле используется database.requests.effective_streak_days() —
    # она же на лету "гасит" серию до 0, если последняя активность была
    # раньше, чем вчера (см. подробное пояснение там).
    last_active_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    streak_days: Mapped[int] = mapped_column(default=0)

    # Дата последнего использования автоматической недельной заморозки
    # серии ("Выходной для кота", не чаще раза в 7 дней) — см.
    # database.requests.get_streak_status / update_streak. None — ещё ни
    # разу не использовалась.
    last_freeze_used_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # --- Настройки уведомлений (экран "👤 Профиль и Настройки" →
    # "🔔 Уведомления", см. handlers/profile.py) — все три по умолчанию
    # включены, бот "из коробки" ведёт себя как задумано (напоминает,
    # присылает утреннюю сводку, не шумит ночью), а не молчит, пока
    # человек сам не найдёт настройки и всё не включит.
    #
    # reminders_enabled — общий рубильник обычных напоминаний по задачам
    # (см. services.scheduler._fire_reminder). Выключен — пуши по
    # конкретным задачам не приходят вообще (ни сами напоминания, ни
    # "второй шанс" через 2 часа).
    reminders_enabled: Mapped[bool] = mapped_column(default=True)

    # quiet_hours_enabled — "Тихие часы" (22:00–08:00): любое напоминание,
    # которое должно было бы прийти в этот промежуток, сдвигается на 09:00
    # (см. services.scheduler._apply_quiet_hours / schedule_reminder).
    quiet_hours_enabled: Mapped[bool] = mapped_column(default=True)

    # morning_checklist_enabled — ежедневная сводка дел на сегодня в 09:00
    # (см. services.scheduler._send_morning_checklists).
    morning_checklist_enabled: Mapped[bool] = mapped_column(default=True)

    # --- Пуши интерактивного модуля "☀️ Чек-лист дня" ------------------------
    # Отдельные от morning_checklist_enabled выше: тот — пассивная ТЕКСТОВАЯ
    # сводка просроченных/сегодняшних задач ("вот что горит"). Эти два —
    # мягкие приглашения именно в НОВЫЙ интерактивный экран чек-листа (см.
    # services.scheduler._send_checklist_morning_briefs/_send_checklist_evening_summaries,
    # handlers/checklist.py). Раз это разные сообщения с разным смыслом —
    # у них и разные переключатели, оба по умолчанию включены (тот же
    # принцип "из коробки работает как задумано", что и у остальных трёх).
    checklist_morning_push_enabled: Mapped[bool] = mapped_column(default=True)
    checklist_evening_push_enabled: Mapped[bool] = mapped_column(default=True)

    # Связь "один пользователь — много задач"
    tasks: Mapped[list["Task"]] = relationship(back_populates="user")


# --- Таблица задач ------------------------------------------------------------

class Task(Base):
    __tablename__ = "tasks"

    task_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Внешний ключ на пользователя-владельца задачи
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.user_id"))

    # Текст задачи
    title: Mapped[str] = mapped_column(Text)

    # Приоритет задачи (Low / Medium / High), по умолчанию Medium
    priority: Mapped[Priority] = mapped_column(
        SAEnum(Priority, native_enum=False), default=Priority.medium
    )

    # Статус задачи (in_progress / done), по умолчанию in_progress
    status: Mapped[Status] = mapped_column(
        SAEnum(Status, native_enum=False), default=Status.in_progress
    )

    # Дедлайн задачи (дата + время). None — задача без срока, лежит в
    # бэклоге (кнопка "⚪️ Без срока" в инлайн-календаре). Заполняется
    # через database.requests.set_task_deadline().
    deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)

    # True — дедлайн выбран кнопкой "☀️ В течение дня", т.е. это просто
    # ДЕНЬ без привязки к конкретному часу (deadline при этом хранится как
    # конец этого дня — нужно для сортировки /tasks). При отображении в
    # этом случае час не показываем (см. texts.format_deadline).
    deadline_all_day: Mapped[bool] = mapped_column(default=False)

    # Когда задача была закрыта (mark_task_done) — None, пока задача ещё
    # активна. Нужно исключительно для вечерней сводки чек-листа дня (см.
    # database.requests.get_tasks_completed_today,
    # services.scheduler._send_checklist_evening_summaries): без этой
    # метки невозможно честно посчитать, сколько дел закрыто ИМЕННО
    # сегодня — Task.status=done сам по себе не говорит, когда именно это
    # произошло.
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)

    user: Mapped["User"] = relationship(back_populates="tasks")

    # Связь "одна задача — много запланированных напоминаний". При удалении
    # задачи (карточка → "🗑 Удалить") все её напоминания удаляются
    # автоматически вместе с ней.
    reminders: Mapped[list["Reminder"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )


# --- Таблица напоминаний -------------------------------------------------------

class Reminder(Base):
    """
    Одно запланированное напоминание по одной задаче. У задачи может быть
    несколько напоминаний одновременно (например, "за 1 день" И "за 1 час") —
    это и есть тот самый мультивыбор из меню напоминаний.

    sent — отправлено ли уже это напоминание. Нужно по двум причинам:
    1) чтобы не отправить его повторно, если бот вдруг перезапустится
       ровно в момент срабатывания;
    2) чтобы при старте бота (services.scheduler.resync_reminders)
       заново ставить в APScheduler только ещё АКТУАЛЬНЫЕ напоминания,
       а не все подряд за всю историю.
    """
    __tablename__ = "reminders"

    reminder_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.task_id", ondelete="CASCADE"))

    offset: Mapped[ReminderOffset] = mapped_column(SAEnum(ReminderOffset, native_enum=False))

    # Точное время, когда нужно прислать уведомление (уже вычислено как
    # "дедлайн минус смещение" — see database.requests.REMINDER_OFFSET_DELTAS).
    remind_at: Mapped[datetime] = mapped_column(DateTime)

    sent: Mapped[bool] = mapped_column(default=False)

    # Отправлен ли уже "второй шанс" по этому напоминанию — мягкий
    # разовый повтор через 2 часа после основного пуша, если задача так и
    # осталась не закрыта (см. services.scheduler._maybe_send_second_chance).
    # Нужно, чтобы не отправить этот мягкий повтор дважды, даже если бот
    # перезапустится ровно в этом промежутке.
    second_chance_sent: Mapped[bool] = mapped_column(default=False)

    task: Mapped["Task"] = relationship(back_populates="reminders")


# --- Таблица привычек / рутин (модуль "☀️ Чек-лист дня") -----------------------

class Habit(Base):
    """
    Ежедневная привычка/рутина (витамины, вода, растяжка) — в отличие от
    обычной Task, НЕ пересоздаётся каждый день заново и не имеет
    дедлайна: она просто живёт в чек-листе постоянно, а её статус
    "сделано" на сегодня сбрасывается автоматически в полночь (см.
    done_today ниже и services.scheduler._reset_daily_habits). Это и есть
    та самая "автономная" часть чек-листа дня — пользователю не нужно
    руками пересоздавать привычку каждое утро.
    """
    __tablename__ = "habits"

    habit_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.user_id"))

    title: Mapped[str] = mapped_column(Text)

    # Сколько XP начисляется за отметку "сделано" — фиксированная награда
    # за привычку (не зависит от приоритета, у привычек его просто нет).
    xp_reward: Mapped[int] = mapped_column(default=5)

    # "Сделано СЕГОДНЯ" — сбрасывается на False каждую полночь (см.
    # services.scheduler._reset_daily_habits), в отличие от Task.status
    # это НЕ разовое завершение, а ежедневно повторяющийся чекбокс.
    done_today: Mapped[bool] = mapped_column(default=False)

    # "Скрыто НА СЕГОДНЯ" — временно не показывать в главном экране
    # чек-листа (кнопка "👁 Скрыть на сегодня" в режиме настройки, см.
    # handlers/checklist.py), не трогая саму привычку — она никуда не
    # делась и снова появится с завтрашнего дня. Сбрасывается на False
    # той же полуночной задачей, что и done_today. Отличается от полного
    # удаления привычки (кнопка "🗑 Удалить привычку" — та стирает запись
    # из базы насовсем).
    hidden_today: Mapped[bool] = mapped_column(default=False)


# --- Инициализация БД ---------------------------------------------------------

async def _migrate_missing_columns(conn) -> None:
    """
    Лёгкая ручная миграция для тех, кто запускал бота ДО того, как
    появились текущие поля.

    create_all() создаёт только ОТСУТСТВУЮЩИЕ таблицы целиком — если
    таблица уже существует (в уже работающем focus_bot.db), новые
    колонки в неё не добавляются, и бот упадёт с ошибкой "no such column".
    Поэтому здесь проверяем, каких колонок не хватает, и досоздаём их
    вручную через ALTER TABLE. Новую таблицу reminders трогать не нужно —
    её create_all() создаст сама, так как раньше её не существовало вообще.
    """
    result = await conn.exec_driver_sql("PRAGMA table_info(users)")
    existing_user_columns = {row[1] for row in result.fetchall()}

    if "last_active_date" not in existing_user_columns:
        await conn.exec_driver_sql("ALTER TABLE users ADD COLUMN last_active_date DATE")
    if "streak_days" not in existing_user_columns:
        await conn.exec_driver_sql("ALTER TABLE users ADD COLUMN streak_days INTEGER NOT NULL DEFAULT 0")
    if "last_freeze_used_date" not in existing_user_columns:
        await conn.exec_driver_sql("ALTER TABLE users ADD COLUMN last_freeze_used_date DATE")
    if "reminders_enabled" not in existing_user_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE users ADD COLUMN reminders_enabled BOOLEAN NOT NULL DEFAULT 1"
        )
    if "quiet_hours_enabled" not in existing_user_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE users ADD COLUMN quiet_hours_enabled BOOLEAN NOT NULL DEFAULT 1"
        )
    if "morning_checklist_enabled" not in existing_user_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE users ADD COLUMN morning_checklist_enabled BOOLEAN NOT NULL DEFAULT 1"
        )
    if "checklist_morning_push_enabled" not in existing_user_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE users ADD COLUMN checklist_morning_push_enabled BOOLEAN NOT NULL DEFAULT 1"
        )
    if "checklist_evening_push_enabled" not in existing_user_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE users ADD COLUMN checklist_evening_push_enabled BOOLEAN NOT NULL DEFAULT 1"
        )

    result = await conn.exec_driver_sql("PRAGMA table_info(tasks)")
    existing_task_columns = {row[1] for row in result.fetchall()}

    if "deadline" not in existing_task_columns:
        await conn.exec_driver_sql("ALTER TABLE tasks ADD COLUMN deadline DATETIME")
    if "deadline_all_day" not in existing_task_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE tasks ADD COLUMN deadline_all_day BOOLEAN NOT NULL DEFAULT 0"
        )
    if "completed_at" not in existing_task_columns:
        await conn.exec_driver_sql("ALTER TABLE tasks ADD COLUMN completed_at DATETIME")

    # reminders — таблица появилась позже tasks/users; если она уже
    # существует (бот какое-то время работал ДО этого шага), а колонки
    # second_chance_sent в ней ещё нет — досоздаём её так же вручную. Если
    # таблицы вообще ещё нет, PRAGMA вернёт пустой результат — в этом
    # случае ничего делать не нужно, create_all() создаст её сразу с этой
    # колонкой.
    result = await conn.exec_driver_sql("PRAGMA table_info(reminders)")
    existing_reminder_columns = {row[1] for row in result.fetchall()}

    if existing_reminder_columns and "second_chance_sent" not in existing_reminder_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE reminders ADD COLUMN second_chance_sent BOOLEAN NOT NULL DEFAULT 0"
        )


async def init_db() -> None:
    """
    Создаёт все таблицы в базе данных, если их ещё нет, и досоздаёт
    отдельные колонки для уже существующих таблиц (см. _migrate_missing_columns).
    Вызывается один раз при старте бота (см. main.py).
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate_missing_columns(conn)
