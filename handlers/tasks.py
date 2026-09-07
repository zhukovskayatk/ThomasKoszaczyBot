"""
Обработчики, связанные с задачами:
- /add <текст> и обычное текстовое сообщение — создать задачу (либо, если
  ждём новый текст переименовываемой задачи — переименовать её, см.
  _pending_rename).
- /tasks (или кнопка "📋 Мои задачи") — показать список активных задач,
  отсортированный по срочности; каждая задача — кликабельная кнопка,
  открывающая её карточку.
- "🎉 Я сделал!" — единственное место, где задачи закрываются "быстро".
- Выбор приоритета (🟢/🟡/🔴) под карточкой созданной задачи.
- Time Management Module: после выбора приоритета — инлайн-календарь
  дедлайна → барабан времени (iOS-style) → мультивыбор напоминаний.
  Никакого ручного ввода дат/времени текстом, всё строится на инлайн-
  кнопках.
- Детальная карточка задачи: изменить текст / дату-время / напоминания,
  удалить, вернуться к списку.
- Кнопки-"пульт" под пуш-уведомлением о напоминании: "✅ Сделано! (+XP)",
  меню "💤 Отложить..." (быстрый перенос, на завтра, полная перенастройка)
  и "✏️ Открыть карточку" (полное редактирование прямо из уведомления).

Вся цепочка шагов (пресеты срока → календарь → время → напоминания)
сделана БЕЗ FSM-состояний aiogram: task_id/reminder_id и уже выбранные
дата/время просто "едут" дальше в callback_data каждой следующей кнопки —
ровно тот же подход, что уже используется для приоритета
(prio:task_id:priority). Накопительный чек-ин "✅ Я сделал!" — исключение:
там нужно копить историю закрытых за сессию задач, а не просто "везти"
одно значение дальше, поэтому состояние сессии живёт в простом словаре в
памяти процесса (см. _quick_close_sessions), тем же способом, что и
_pending_rename ниже. Единственное место, где реального free-text ввода
не избежать — переименование задачи
("✏️ Изменить текст") — под это заведён простой словарь _pending_rename
в памяти процесса (без полноценного FSM: если бот перезапустится ровно
между нажатием кнопки и отправкой нового текста, следующее сообщение
просто станет новой задачей — редкий и не критичный случай).

Логика построения текста сообщений — в texts.py, построение клавиатур —
в keyboards.py. Здесь только "что делать по каждому действию".
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, FSInputFile, Message

import services.scheduler as scheduler_service
import texts
from database.models import Priority, ReminderOffset
from database.requests import (
    REMINDER_OFFSET_DELTAS,
    add_reminder,
    add_task,
    available_reminder_offsets,
    clear_task_reminders,
    delete_task,
    get_active_tasks,
    get_active_tasks_by_deadline,
    get_reminder_with_task,
    get_streak_status,
    get_task,
    get_task_reminders,
    recompute_reminders_for_new_deadline,
    remove_reminder,
    set_task_deadline,
    set_task_priority,
    update_task_title,
)
from keyboards import (
    ADD_BUTTON_TEXT,
    CTX_EDIT,
    CTX_NEW,
    CTX_SNOOZE,
    LEGACY_ADD_BUTTON_TEXTS,
    LEGACY_QUICK_CLOSE_BUTTON_TEXTS,
    LEGACY_TASKS_BUTTON_TEXTS,
    QUICK_CLOSE_BUTTON_TEXT,
    TASKS_BUTTON_TEXT,
    calendar_keyboard,
    deadline_presets_keyboard,
    delete_confirm_keyboard,
    main_menu_keyboard,
    priority_keyboard,
    quick_close_session_keyboard,
    reminder_notification_keyboard,
    reminders_keyboard,
    shift_month,
    snooze_menu_keyboard,
    task_card_keyboard,
    tasks_page_keyboard,
    time_drum_keyboard,
)
from handlers import checklist
from services.meme_manager import get_success_reward
from services.task_actions import complete_task_core

router = Router(name="tasks")

# user_id -> task_id, чью задачу переименовываем следующим текстовым
# сообщением (см. card_edittext / add_task_from_text).
_pending_rename: dict[int, int] = {}


@dataclass
class _QuickCloseSession:
    """
    Состояние ОДНОЙ сессии накопительного чек-ина "✅ Я сделал!" — живёт в
    памяти процесса, пока сообщение с прогрессом открыто (без полноценного
    FSM, тем же способом, что и _pending_rename выше). closed — список
    (название, XP) уже закрытых в этой сессии задач, в порядке закрытия —
    именно он превращается в зачёркнутые строки отчёта (см.
    texts.quick_close_progress_text/quick_close_final_text).
    """
    closed: list[tuple[str, int]] = field(default_factory=list)


# (chat_id, message_id) -> сессия чек-ина этого конкретного сообщения.
# Ключ — само сообщение, а не user_id: так, если человек каким-то образом
# откроет чек-ин дважды, у каждого открытого сообщения будет свой честный
# прогресс, без путаницы между ними.
_quick_close_sessions: dict[tuple[int, int], _QuickCloseSession] = {}


async def confirm_task_added(message: Message, task) -> None:
    """
    Лаконичное подтверждение после создания задачи + выбор приоритета.
    Кнопки "Выполнено" тут больше нет — закрывать задачу сразу же после
    создания незачем, для этого есть отдельный сценарий "🎉 Я сделал!".
    """
    keyboard = priority_keyboard(task.task_id)
    await message.answer(
        texts.task_added_text(task.title, task.priority),
        reply_markup=keyboard.as_markup(),
    )


async def show_tasks(message: Message) -> None:
    """
    Общая логика показа списка задач — вызывается и /tasks, и кнопкой.
    Список отсортирован по срочности (get_active_tasks_by_deadline).
    Каждая задача на текущей странице — кликабельная кнопка, открывающая
    её карточку (см. tasks_page_keyboard / card_open).
    """
    tasks = await get_active_tasks_by_deadline(user_id=message.from_user.id)

    if not tasks:
        await message.answer(texts.no_active_tasks_text(), reply_markup=main_menu_keyboard)
        return

    keyboard, offset = tasks_page_keyboard(tasks, offset=0)
    await message.answer(texts.tasks_list_text(tasks), reply_markup=keyboard.as_markup())


def _default_time_for_date(year: int, month: int, day: int) -> tuple[int, int]:
    """
    Разумное время по умолчанию при переходе на шаг выбора времени:
    - если выбран сегодняшний день — берём текущее время, округлённое
      вверх до ближайших 15 минут (шаг минутной кнопки "➕/➖ 15 м" —
      тоже 15, чтобы значение по умолчанию сразу попадало в его сетку);
    - иначе — полдень (12:00), нейтральный вариант.
    """
    if date(year, month, day) != date.today():
        return 12, 0

    now = datetime.now()
    total_minutes = now.hour * 60 + now.minute
    rounded = min(((total_minutes // 15) + 1) * 15, 23 * 60 + 45)
    return divmod(rounded, 60)


async def _finish_wizard(callback: CallbackQuery, text: str, reply_markup=None) -> None:
    """
    Завершает шаг мастера дедлайна/напоминаний (календарь, барабан времени,
    мультивыбор напоминаний, custom-перенос в меню "💤 Отложить"): вместо
    очередного edit_text ПОВЕРХ служебного сообщения — удаляет его целиком
    и присылает НОВОЕ, уже с финальным результатом (карточка задачи,
    подтверждение создания, подтверждение переноса напоминания). Так лента
    чата не копит следы промежуточных шагов мастера — виден только чистый
    финал, а не история "как мы к нему пришли".

    Подстраховка: если сообщение почему-то нельзя удалить (например, чат
    успел измениться, или сообщение уже удалено вручную) — просто
    редактируем его на месте, как раньше, лишь бы результат точно дошёл
    до человека, а не потерялся из-за одной неудавшейся операции.
    """
    try:
        await callback.message.delete()
        await callback.message.answer(text, reply_markup=reply_markup)
    except TelegramBadRequest:
        await callback.message.edit_text(text, reply_markup=reply_markup)


# --- Создание задачи через команду /add --------------------------------------

@router.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject) -> None:
    task_text = command.args

    if not task_text:
        await message.answer(
            "Напиши, что нужно сделать, после команды.\n"
            "Например: /add Помыть посуду"
        )
        return

    task = await add_task(user_id=message.from_user.id, title=task_text)
    await confirm_task_added(message, task)


# --- Список активных задач ----------------------------------------------------

@router.message(Command("tasks"))
async def cmd_tasks(message: Message) -> None:
    await show_tasks(message)


@router.message(F.text.in_({TASKS_BUTTON_TEXT, *LEGACY_TASKS_BUTTON_TEXTS}))
async def tasks_button(message: Message) -> None:
    await show_tasks(message)


@router.callback_query(F.data.startswith("tasks_page:"))
async def tasks_page(callback: CallbackQuery) -> None:
    """Листание страниц списка "📋 Мои задачи" и кнопка "◀️ Назад к списку" в
    карточке задачи (она использует этот же callback_data)."""
    offset = int(callback.data.split(":", maxsplit=1)[1])
    tasks = await get_active_tasks_by_deadline(user_id=callback.from_user.id)

    if not tasks:
        await callback.message.edit_text(texts.no_active_tasks_text())
        await callback.answer()
        return

    keyboard, offset = tasks_page_keyboard(tasks, offset)
    await callback.message.edit_text(texts.tasks_list_text(tasks), reply_markup=keyboard.as_markup())
    await callback.answer()


# --- Кнопка "➕ Добавить" ------------------------------------------------------

# Ловим и текущую подпись кнопки, и её прежние варианты (LEGACY_ADD_BUTTON_TEXTS) —
# если у человека на экране ещё старая кнопка, она должна сработать как
# обычно, а НЕ провалиться в создание задачи из собственного текста кнопки.
@router.message(F.text.in_({ADD_BUTTON_TEXT, *LEGACY_ADD_BUTTON_TEXTS}))
async def add_button(message: Message) -> None:
    # reply_markup=main_menu_keyboard здесь же "чинит" видимую клавиатуру —
    # после этого ответа на экране будет актуальная подпись кнопки.
    await message.answer("Напиши текст задачи следующим сообщением 👇", reply_markup=main_menu_keyboard)


# --- Кнопка "✅ Я сделал!" — накопительный чек-ин за сессию -------------------

@router.message(F.text.in_({QUICK_CLOSE_BUTTON_TEXT, *LEGACY_QUICK_CLOSE_BUTTON_TEXTS}))
async def quick_close_button(message: Message) -> None:
    """
    Открывает накопительный чек-ин: одно и то же сообщение по ходу сессии
    редактируется на месте (edit_message_text/edit_message_reply_markup) —
    без спама новым сообщением на каждую закрытую задачу, прогресс
    накапливается прямо перед глазами (см. qc_close/qc_finish и
    texts.quick_close_progress_text).
    """
    tasks = list(reversed(await get_active_tasks(user_id=message.from_user.id)))

    if not tasks:
        await message.answer(texts.quick_close_nothing_text(), reply_markup=main_menu_keyboard)
        return

    keyboard = quick_close_session_keyboard(tasks, has_progress=False)
    await message.answer(texts.quick_close_start_text(), reply_markup=keyboard.as_markup())


# --- Кнопка "☀️ Чек-лист" — теперь полноценный модуль, см. handlers/checklist.py ---

# --- Общая логика закрытия задачи (XP, уровень, "дофаминовое" сообщение) ------

async def _send_reward(message: Message, task_title: str, xp_amount: int) -> None:
    """
    Отправляет "дофаминовую" награду за закрытую задачу.

    get_success_reward() решает, ЧТО войдёт в награду (цитата + максимум
    одно медиа — стикер или картинка-мем из assets/memes/), а здесь мы
    решаем, КАК это отправить в Telegram:
    - если это стикер — стикеры не поддерживают подпись, поэтому текст
      с XP и цитатой шлём отдельным сообщением следом;
    - если это картинка — у фото есть caption, лишнее сообщение не нужно;
    - если медиа нет вообще — просто текст, как раньше.

    На случай "битого" file_id стикера или удалённого файла картинки —
    подстраховываемся try/except, чтобы отметка задачи не падала целиком
    из-за проблемы с одним конкретным медиафайлом.
    """
    reward = get_success_reward()
    reward_message = texts.reward_text(task_title, xp_amount, reward.phrase)

    try:
        if reward.sticker_id:
            await message.answer_sticker(reward.sticker_id)
            await message.answer(reward_message)
        elif reward.meme_path:
            await message.answer_photo(FSInputFile(reward.meme_path), caption=reward_message)
        else:
            await message.answer(reward_message)
    except TelegramBadRequest:
        await message.answer(reward_message)


async def complete_task(
    callback: CallbackQuery, task_id: int, *, send_reward: bool = True
) -> tuple[str, int] | None:
    """
    Отмечает задачу выполненной в БД, начисляет XP и обновляет серию
    активности. Используется в двух местах с разным финальным поведением:
    - кнопка "✅ Я сделал!" под пуш-уведомлением о напоминании
      (send_reward=True, по умолчанию) — отдельная "дофаминовая" награда
      (стикер/мем + цитата) уместна, рядом нет другого открытого чек-ина;
    - накопительный чек-ин "✅ Я сделал!" (send_reward=False, см.
      qc_close) — награда НЕ уходит отдельным сообщением, сам результат
      вписывается в уже накопленный отчёт сессии вызывающим кодом.

    Возвращает (название_задачи, начисленный_XP) при успехе, иначе None —
    вызывающий код должен на этом остановиться (задача не найдена или уже
    была закрыта раньше).

    Сама логика закрытия (БД, XP, уровень, серия, снятие напоминаний) живёт
    в services.task_actions.complete_task_core — она общая сразу для
    нескольких экранов (см. handlers/checklist.py, где тап по задаче в
    "☀️ Чек-лист дня" переиспользует то же самое ядро). Здесь — только то,
    что специфично именно для этого сценария (Telegram-реакция).
    """
    user_id = callback.from_user.id
    result = await complete_task_core(user_id=user_id, task_id=task_id)

    if result is None:
        await callback.answer("Эта задача уже закрыта или не найдена 🤔", show_alert=True)
        return None

    if send_reward:
        await callback.answer("Задача отмечена как выполненная! 🎉")
        await _send_reward(callback.message, result.task_title, result.xp_amount)
    else:
        # Короткий тост вместо отдельного сообщения — результат и так
        # появится в накопленном отчёте сессии (см. qc_close).
        await callback.answer(f"+{result.xp_amount} XP 🐾")

    if result.leveled_up:
        await callback.message.answer(texts.level_up_text(result.new_level))

    return result.task_title, result.xp_amount


# --- Меню "✅ Я сделал!": закрытие задачи внутри сессии ------------------------

@router.callback_query(F.data.startswith("qc_close:"))
async def qc_close(callback: CallbackQuery) -> None:
    """
    Закрывает одну задачу внутри сессии чек-ина — само сообщение
    редактируется на месте: к уже вычеркнутым задачам добавляется новая
    строка, XP суммируется, реплика Томаса берётся случайной (см.
    texts.random_quick_close_phrase). Если после этого активных задач
    больше не осталось — сессия сразу завершается финальным отчётом (см.
    _qc_finalize), как и по кнопке "🏁 Всё на сегодня!".
    """
    task_id = int(callback.data.split(":", maxsplit=1)[1])

    result = await complete_task(callback, task_id, send_reward=False)
    if result is None:
        return
    title, xp_amount = result

    key = (callback.message.chat.id, callback.message.message_id)
    session = _quick_close_sessions.setdefault(key, _QuickCloseSession())
    session.closed.append((title, xp_amount))

    remaining_tasks = list(reversed(await get_active_tasks(user_id=callback.from_user.id)))

    if not remaining_tasks:
        await _qc_finalize(callback, session, key)
        return

    keyboard = quick_close_session_keyboard(remaining_tasks, has_progress=True)
    await callback.message.edit_text(
        texts.quick_close_progress_text(session.closed, texts.random_quick_close_phrase()),
        reply_markup=keyboard.as_markup(),
    )


@router.callback_query(F.data == "qc_finish")
async def qc_finish(callback: CallbackQuery) -> None:
    """Кнопка "🏁 Всё на сегодня!" — осознанно завершает сессию чек-ина
    финальным отчётом, даже если остались ещё не закрытые задачи."""
    key = (callback.message.chat.id, callback.message.message_id)
    session = _quick_close_sessions.get(key)

    if session is None or not session.closed:
        # На случай, если сессия в памяти как-то потерялась (например,
        # бот перезапустился прямо посреди чек-ина) — не показываем
        # пустой "отчёт ни о чём", просто закрываем меню как есть.
        await callback.answer()
        await callback.message.edit_text(texts.quick_close_abort_text())
        await callback.message.edit_reply_markup(reply_markup=None)
        return

    await _qc_finalize(callback, session, key)


@router.callback_query(F.data == "qc_abort")
async def qc_abort(callback: CallbackQuery) -> None:
    """"❌ Закрыть меню" — доступна, только пока в сессии ещё ничего не
    закрыто (как только закрыта хотя бы одна задача, эта кнопка сменяется
    на "🏁 Всё на сегодня!", см. keyboards.quick_close_session_keyboard)."""
    await callback.answer()
    await callback.message.edit_text(texts.quick_close_abort_text())
    await callback.message.edit_reply_markup(reply_markup=None)


async def _qc_finalize(callback: CallbackQuery, session: "_QuickCloseSession", key: tuple[int, int]) -> None:
    """
    Фиксирует накопительный чек-ин финальным отчётом побед и убирает
    инлайн-кнопки целиком — сессия на этом закончена, дальше это сообщение
    уже никто не редактирует.
    """
    _quick_close_sessions.pop(key, None)
    status = await get_streak_status(user_id=callback.from_user.id)
    await callback.answer()
    await callback.message.edit_text(
        texts.quick_close_final_text(session.closed, texts.random_quick_close_phrase(), status.streak_days)
    )
    await callback.message.edit_reply_markup(reply_markup=None)


# --- Создание задачи (или переименование) из обычного текстового сообщения ----

# Важно: этот хендлер регистрируется последним и ловит любой текст,
# который не подошёл под команды/кнопки выше.
@router.message(F.text, ~F.text.startswith("/"))
async def add_task_from_text(message: Message) -> None:
    user_id = message.from_user.id

    if await checklist.try_handle_pending_text(message):
        # Ждали текст нового пункта чек-листа (рутина или быстрое дело на
        # сегодня, см. handlers/checklist.py::chk_add_routine/chk_add_task) —
        # он уже создан и экран перерисован, обычной задачей это не считаем.
        return

    pending_task_id = _pending_rename.pop(user_id, None)

    if pending_task_id is not None:
        # Мы ждали новый текст для конкретной задачи (кнопка "✏️ Изменить
        # текст" в её карточке) — это переименование, а не новая задача.
        success = await update_task_title(task_id=pending_task_id, user_id=user_id, title=message.text)
        if not success:
            await message.answer("Не удалось найти эту задачу 🤔", reply_markup=main_menu_keyboard)
            return

        task = await get_task(task_id=pending_task_id, user_id=user_id)
        reminders = await get_task_reminders(pending_task_id)
        await message.answer(
            "✅ Текст обновлён!\n\n" + texts.task_card_text(task, reminders),
            reply_markup=task_card_keyboard(pending_task_id, offset=0, in_checklist_today=texts.task_in_checklist_today(task)).as_markup(),
        )
        return

    task = await add_task(user_id=user_id, title=message.text)
    await confirm_task_added(message, task)


# --- Обработка нажатия на кнопку приоритета (🟢/🟡/🔴) -------------------------

@router.callback_query(F.data.startswith("prio:"))
async def process_priority_choice(callback: CallbackQuery) -> None:
    _, task_id_str, priority_value = callback.data.split(":", maxsplit=2)
    task_id = int(task_id_str)
    priority = Priority(priority_value)

    success = await set_task_priority(
        task_id=task_id, user_id=callback.from_user.id, priority=priority
    )

    if not success:
        await callback.answer("Не удалось найти эту задачу 🤔", show_alert=True)
        return

    await callback.answer("Приоритет сохранён ✅")

    task = await get_task(task_id=task_id, user_id=callback.from_user.id)
    if task is None:
        return

    # Приоритет выбран — переходим к следующему шагу мастера: экран быстрых
    # пресетов срока (Сегодня/Завтра/на выходных/на неделе), а не сразу
    # календарь — для большинства задач точная дата не нужна (см. модуль
    # дедлайнов и напоминаний ниже). Кнопки 🟢/🟡/🔴 своё дело сделали, не
    # передаём reply_markup от них — deadline_presets_keyboard заменит их
    # в этом же сообщении.
    await callback.message.edit_text(
        texts.deadline_presets_prompt_text(task.title),
        reply_markup=deadline_presets_keyboard(CTX_NEW, task_id).as_markup(),
    )


@router.callback_query(F.data.startswith("prio_cancel:"))
async def prio_cancel(callback: CallbackQuery) -> None:
    """
    "❌ Отмена" сразу после создания задачи (пока не выбран даже приоритет).
    На этом шаге в БД есть только название задачи и ничего больше — отмена
    здесь просто удаляет её целиком, как будто её и не создавали.
    """
    task_id = int(callback.data.split(":", maxsplit=1)[1])
    await delete_task(task_id=task_id, user_id=callback.from_user.id)
    await callback.answer("Отменено, ничего не сохранил")
    await callback.message.edit_text("❌ Создание задачи отменено.")


# --- Time Management Module: декоративные/неактивные кнопки -------------------

@router.callback_query(F.data == "noop")
async def noop_callback(callback: CallbackQuery) -> None:
    # Заголовки, шапка дней недели, пустые клетки сетки, прошедшие дни,
    # заблокированная "◀️", текущие значения часов/минут по центру
    # барабана времени — все они используют этот же callback_data, просто
    # чтобы Telegram не показывал бесконечный "часики" при нажатии на
    # декоративную кнопку.
    await callback.answer()


@router.callback_query(F.data.startswith("wiz_cancel:"))
async def wiz_cancel(callback: CallbackQuery) -> None:
    """
    "❌ Отмена" на экранах календаря и селектора времени (общая для обоих —
    в этот момент ни один из них ещё ничего не сохранил, дедлайн/новое
    время фиксируются только по явному подтверждению). Поведение зависит
    от контекста:
    - CTX_NEW: дедлайн ещё не сохранён — отмена равносильна "⚪️ Без срока",
      задача остаётся как есть (её всегда можно доредактировать из карточки).
    - CTX_EDIT: просто возвращаемся на карточку задачи, ничего не меняя.
    - CTX_SNOOZE: возвращаемся к исходному уведомлению — как и "◀️ Отмена"
      в меню "💤 Отложить".
    """
    _, context, entity_id_str = callback.data.split(":", maxsplit=2)
    entity_id = int(entity_id_str)

    if context == CTX_SNOOZE:
        reminder_id = entity_id
        found = await get_reminder_with_task(reminder_id)
        if found is None:
            await callback.answer("Не удалось найти это напоминание 🤔", show_alert=True)
            return
        reminder, task = found
        await callback.answer("Отменено")
        await _finish_wizard(
            callback,
            texts.reminder_notification_text(task, reminder.offset),
            reminder_notification_keyboard(reminder_id, task).as_markup(),
        )
        return

    task_id, user_id = entity_id, callback.from_user.id
    task = await get_task(task_id=task_id, user_id=user_id)
    if task is None:
        await callback.answer("Не удалось найти эту задачу 🤔", show_alert=True)
        return

    if context == CTX_NEW:
        await callback.answer("Ок, без дедлайна")
        await _finish_wizard(callback, texts.deadline_saved_text(task.title, task.priority, None))
        return

    # CTX_EDIT — возвращаемся на карточку задачи без изменений.
    await callback.answer("Отменено")
    reminders = await get_task_reminders(task_id)
    await _finish_wizard(
        callback,
        texts.task_card_text(task, reminders),
        task_card_keyboard(task_id, offset=0, in_checklist_today=texts.task_in_checklist_today(task)).as_markup(),
    )


# --- Time Management Module: экран быстрых пресетов срока ---------------------

def _resolve_preset_date(code: str) -> date:
    """
    Переводит код быстрого пресета (см. keyboards.deadline_presets_keyboard)
    в конкретную дату:
    - today/tomorrow — буквально сегодня/завтра;
    - weekend — ближайшая суббота; если сегодня уже суббота или
      воскресенье — само сегодня (иначе "на выходных" улетало бы на
      следующую неделю, если открыть бота уже в выходной день);
    - week — конец текущей недели (воскресенье), включая сегодня, если
      сегодня уже воскресенье.
    """
    today = date.today()
    weekday = today.weekday()  # Пн=0 ... Вс=6

    if code == "tomorrow":
        return today + timedelta(days=1)
    if code == "weekend":
        if weekday >= 5:
            return today
        return today + timedelta(days=5 - weekday)
    if code == "week":
        return today + timedelta(days=6 - weekday)
    return today  # code == "today" (и любой неизвестный код — безопасный дефолт)


@router.callback_query(F.data.startswith("cal_open:"))
async def cal_open(callback: CallbackQuery) -> None:
    """Кнопка "🗓 Выбрать в календаре..." на экране быстрых пресетов —
    открывает обычный интерактивный календарь (см. deadline_presets_keyboard)."""
    _, context, entity_id_str = callback.data.split(":", maxsplit=2)
    task_id = int(entity_id_str)

    task = await get_task(task_id=task_id, user_id=callback.from_user.id)
    if task is None:
        await callback.answer("Не удалось найти эту задачу 🤔", show_alert=True)
        return

    await callback.answer()
    today = date.today()
    await callback.message.edit_text(
        texts.deadline_prompt_text(task.title),
        reply_markup=calendar_keyboard(context, task_id, today.year, today.month).as_markup(),
    )


@router.callback_query(F.data.startswith("preset_day:"))
async def preset_day(callback: CallbackQuery) -> None:
    """
    Клик по одному из быстрых пресетов ("📍 Сегодня", "🌅 Завтра" и т.п.) —
    сразу переходит к шагу выбора времени (тот же экран, что и после клика
    по конкретному дню в обычном календаре, см. cal_day), без промежуточного
    открытия самого календаря.
    """
    _, context, entity_id_str, code = callback.data.split(":", maxsplit=3)
    task_id = int(entity_id_str)

    task = await get_task(task_id=task_id, user_id=callback.from_user.id)
    if task is None:
        await callback.answer("Не удалось найти эту задачу 🤔", show_alert=True)
        return

    target_date = _resolve_preset_date(code)
    await callback.answer()
    default_hour, default_minute = _default_time_for_date(target_date.year, target_date.month, target_date.day)
    await callback.message.edit_text(
        texts.time_prompt_text(task.title, target_date.day, target_date.month, target_date.year),
        reply_markup=time_drum_keyboard(
            context, task_id, target_date.year, target_date.month, target_date.day, default_hour, default_minute
        ).as_markup(),
    )


# --- Time Management Module: шаг 1 — инлайн-календарь --------------------------

@router.callback_query(F.data.startswith("cal_nav:"))
async def cal_nav(callback: CallbackQuery) -> None:
    _, context, entity_id_str, year_str, month_str, direction = callback.data.split(":", maxsplit=5)
    year, month = shift_month(int(year_str), int(month_str), direction)

    keyboard = calendar_keyboard(context, int(entity_id_str), year, month)
    await callback.message.edit_reply_markup(reply_markup=keyboard.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("cal_day:"))
async def cal_day(callback: CallbackQuery) -> None:
    _, context, entity_id_str, year_str, month_str, day_str = callback.data.split(":", maxsplit=5)
    entity_id, year, month, day = int(entity_id_str), int(year_str), int(month_str), int(day_str)

    if context == CTX_SNOOZE:
        # entity_id здесь — reminder_id, не task_id.
        found = await get_reminder_with_task(entity_id)
        if found is None:
            await callback.answer("Не удалось найти это напоминание 🤔", show_alert=True)
            return
        title = found[1].title
    else:
        task = await get_task(task_id=entity_id, user_id=callback.from_user.id)
        if task is None:
            await callback.answer("Не удалось найти эту задачу 🤔", show_alert=True)
            return
        title = task.title

    await callback.answer()
    default_hour, default_minute = _default_time_for_date(year, month, day)
    await callback.message.edit_text(
        texts.time_prompt_text(title, day, month, year),
        reply_markup=time_drum_keyboard(
            context, entity_id, year, month, day, default_hour, default_minute
        ).as_markup(),
    )


@router.callback_query(F.data.startswith("cal_none:"))
async def cal_none(callback: CallbackQuery) -> None:
    """Кнопка "⚪️ Без срока" — задача сразу сохраняется без срока и без
    напоминаний, шаги времени/напоминаний пропускаются целиком."""
    _, context, entity_id_str = callback.data.split(":", maxsplit=2)

    if context == CTX_SNOOZE:
        # Календарь в контексте переноса напоминания эту кнопку вообще не
        # показывает — сюда почти невозможно попасть, но на всякий случай.
        await callback.answer("Недоступно для переноса напоминания", show_alert=True)
        return

    task_id, user_id = int(entity_id_str), callback.from_user.id

    await set_task_deadline(task_id=task_id, user_id=user_id, deadline=None, all_day=False)
    await clear_task_reminders(task_id)
    scheduler_service.unschedule_all_for_task(task_id)

    task = await get_task(task_id=task_id, user_id=user_id)
    if task is None:
        await callback.answer("Не удалось найти эту задачу 🤔", show_alert=True)
        return

    await callback.answer("Сохранено без срока")

    if context == CTX_NEW:
        await _finish_wizard(callback, texts.deadline_saved_text(task.title, task.priority, None))
    else:  # CTX_EDIT — возвращаемся на карточку задачи
        reminders = await get_task_reminders(task_id)
        await _finish_wizard(
            callback,
            texts.task_card_text(task, reminders),
            task_card_keyboard(task_id, offset=0, in_checklist_today=texts.task_in_checklist_today(task)).as_markup(),
        )


async def _save_all_day_deadline(
    callback: CallbackQuery, context: str, task_id: int, target_date: date
) -> None:
    """
    Общая логика для обеих кнопок "☀️ В течение дня" — календарной (всегда
    сегодня, cal_allday) и той, что появляется уже на экране барабана
    времени для ЛЮБОЙ выбранной даты (td_allday).

    Раньше этот шаг сразу сохранял задачу и ПОЛНОСТЬЮ пропускал шаг
    напоминаний (раз точного часа нет) — из-за этого дедлайн "сегодня, без
    часа" сохранялся молча, без единого шанса поставить напоминание, хотя
    дедлайн всё равно наступает именно в этот день и напомнить о нём не
    менее важно, чем о задаче с точным временем (это и была жалоба —
    бот "забывал" спросить про уведомления для дел без точного часа).
    Теперь шаг напоминаний показывается точно так же, как и после выбора
    точного времени (см. td_confirm) — дедлайн просто хранится как конец
    этого дня (23:59), поэтому смещения вроде "За 1 день"/"За 3 дня"
    отсчитываются от конца дня, а не от конкретного часа.
    """
    user_id = callback.from_user.id
    deadline = datetime.combine(target_date, datetime.max.time().replace(microsecond=0))

    await set_task_deadline(task_id=task_id, user_id=user_id, deadline=deadline, all_day=True)

    task = await get_task(task_id=task_id, user_id=user_id)
    if task is None:
        await callback.answer("Не удалось найти эту задачу 🤔", show_alert=True)
        return

    await callback.answer("Сохранено без точного часа")

    if context == CTX_NEW:
        # Новая задача — напоминаний по ней ещё физически не может быть,
        # просто показываем шаг их выбора (см. td_confirm/CTX_NEW).
        available_offsets = available_reminder_offsets(deadline)
        await callback.message.edit_text(
            texts.reminders_prompt_text(task.title, deadline, all_day=True),
            reply_markup=reminders_keyboard(CTX_NEW, task_id, available_offsets, selected_offsets=set()).as_markup(),
        )
        return

    # CTX_EDIT — дедлайн уже существующей задачи поменялся на "без часа":
    # пересчитываем уже выбранные напоминания под новый дедлайн (23:59
    # этого дня), а не сбрасываем их молча — так же, как и при правке
    # точного времени (см. td_confirm/CTX_EDIT).
    kept, removed_offsets = await recompute_reminders_for_new_deadline(task_id, deadline)
    for offset in removed_offsets:
        scheduler_service.unschedule_reminder(task_id, offset)
    for reminder in kept:
        await scheduler_service.schedule_reminder(task_id, reminder.offset, reminder.reminder_id, reminder.remind_at)

    reminders = await get_task_reminders(task_id)
    await _finish_wizard(
        callback,
        texts.task_card_text(task, reminders),
        task_card_keyboard(task_id, offset=0, in_checklist_today=texts.task_in_checklist_today(task)).as_markup(),
    )


@router.callback_query(F.data.startswith("cal_allday:"))
async def cal_allday(callback: CallbackQuery) -> None:
    """Кнопка "☀️ В течение дня" в календаре — быстрый срок "сегодня, без
    конкретного часа" (независимо от того, какой месяц сейчас пролистан)."""
    _, context, entity_id_str = callback.data.split(":", maxsplit=2)

    if context == CTX_SNOOZE:
        await callback.answer("Недоступно для переноса напоминания", show_alert=True)
        return

    await _save_all_day_deadline(callback, context, int(entity_id_str), date.today())


@router.callback_query(F.data.startswith("td_allday:"))
async def td_allday(callback: CallbackQuery) -> None:
    """Кнопка "☀️ В течение дня" на экране барабана времени — то же самое,
    но для ДАТЫ, уже выбранной на предыдущем шаге (любой, не только сегодня)."""
    _, context, entity_id_str, year_str, month_str, day_str = callback.data.split(":", maxsplit=5)

    if context == CTX_SNOOZE:
        await callback.answer("Недоступно для переноса напоминания", show_alert=True)
        return

    target_date = date(int(year_str), int(month_str), int(day_str))
    await _save_all_day_deadline(callback, context, int(entity_id_str), target_date)


# --- Time Management Module: шаг 2 — барабан времени ----------------------------

@router.callback_query(F.data.startswith("td_pick:"))
async def td_pick(callback: CallbackQuery) -> None:
    """Клик по стрелке ▲/▼ или по соседнему значению на барабане — сразу
    переносит барабан на него, само подтверждение — отдельным явным шагом."""
    _, context, entity_id_str, year_str, month_str, day_str, hour_str, minute_str = (
        callback.data.split(":", maxsplit=7)
    )
    entity_id, year, month, day = int(entity_id_str), int(year_str), int(month_str), int(day_str)
    hour, minute = int(hour_str), int(minute_str)

    keyboard = time_drum_keyboard(context, entity_id, year, month, day, hour, minute)
    await callback.message.edit_reply_markup(reply_markup=keyboard.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("td_confirm:"))
async def td_confirm(callback: CallbackQuery) -> None:
    """
    Явное подтверждение времени. Поведение зависит от контекста:
    - CTX_SNOOZE: entity_id — reminder_id, просто переносим ЭТО напоминание
      на выбранные дату/время и возвращаем короткое подтверждение.
    - CTX_NEW: сохраняет дедлайн новой задачи и переходит к следующему шагу
      мастера — мультивыбору напоминаний.
    - CTX_EDIT: сохраняет новый дедлайн уже существующей задачи, пересчитывает
      под него уже выбранные напоминания и возвращает карточку задачи.
    """
    _, context, entity_id_str, year_str, month_str, day_str, hour_str, minute_str = (
        callback.data.split(":", maxsplit=7)
    )
    entity_id, year, month, day = int(entity_id_str), int(year_str), int(month_str), int(day_str)
    hour, minute = int(hour_str), int(minute_str)
    chosen = datetime(year, month, day, hour, minute)

    if chosen <= datetime.now():
        await callback.answer("Это время уже прошло — выбери время в будущем 🙂", show_alert=True)
        return

    if context == CTX_SNOOZE:
        reminder_id = entity_id
        ok = await scheduler_service.reschedule(reminder_id, chosen)
        if not ok:
            await callback.answer("Не удалось найти это напоминание 🤔", show_alert=True)
            return
        await callback.answer("Перенесено ✅")
        await _finish_wizard(callback, texts.snooze_confirmed_text(chosen))
        return

    task_id, user_id = entity_id, callback.from_user.id

    if context == CTX_NEW:
        await set_task_deadline(task_id=task_id, user_id=user_id, deadline=chosen, all_day=False)
        task = await get_task(task_id=task_id, user_id=user_id)
        if task is None:
            await callback.answer("Не удалось найти эту задачу 🤔", show_alert=True)
            return

        await callback.answer("Дедлайн сохранён ✅")
        available_offsets = available_reminder_offsets(chosen)
        await callback.message.edit_text(
            texts.reminders_prompt_text(task.title, chosen),
            reply_markup=reminders_keyboard(CTX_NEW, task_id, available_offsets, selected_offsets=set()).as_markup(),
        )
        return

    # CTX_EDIT — правка даты/времени уже существующей задачи из карточки.
    await set_task_deadline(task_id=task_id, user_id=user_id, deadline=chosen, all_day=False)
    kept, removed_offsets = await recompute_reminders_for_new_deadline(task_id, chosen)

    for offset in removed_offsets:
        scheduler_service.unschedule_reminder(task_id, offset)
    for reminder in kept:
        await scheduler_service.schedule_reminder(task_id, reminder.offset, reminder.reminder_id, reminder.remind_at)

    task = await get_task(task_id=task_id, user_id=user_id)
    if task is None:
        await callback.answer("Не удалось найти эту задачу 🤔", show_alert=True)
        return

    await callback.answer("Дедлайн обновлён ✅")
    reminders = await get_task_reminders(task_id)
    await _finish_wizard(
        callback,
        texts.task_card_text(task, reminders),
        task_card_keyboard(task_id, offset=0, in_checklist_today=texts.task_in_checklist_today(task)).as_markup(),
    )


# --- Time Management Module: шаг 3 — мультивыбор напоминаний -------------------

@router.callback_query(F.data.startswith("rmd_toggle:"))
async def rmd_toggle(callback: CallbackQuery) -> None:
    """
    Переключает один чекбокс напоминания (◻️ ↔ ☑️) ПРЯМО в этом же
    сообщении через edit_message_reply_markup — без спама новыми
    сообщениями. Каждая отмеченная галочка сразу же создаёт запись в БД
    и таймер в планировщике (а не откладывается до кнопки "Сохранить") —
    так надёжнее: если человек просто закроет чат посередине настройки,
    уже выбранные напоминания не потеряются.
    """
    _, context, task_id_str, offset_value = callback.data.split(":", maxsplit=3)
    task_id = int(task_id_str)
    offset = ReminderOffset(offset_value)
    user_id = callback.from_user.id

    task = await get_task(task_id=task_id, user_id=user_id)
    if task is None or task.deadline is None:
        await callback.answer("Сначала нужен дедлайн 🤔", show_alert=True)
        return

    current_offsets = {reminder.offset for reminder in await get_task_reminders(task_id)}

    if offset in current_offsets:
        removed = await remove_reminder(task_id, offset)
        if removed:
            scheduler_service.unschedule_reminder(task_id, offset)
        await callback.answer("Убрано")
    else:
        remind_at = task.deadline - REMINDER_OFFSET_DELTAS[offset]
        if remind_at <= datetime.now():
            await callback.answer(
                "Это напоминание сработало бы уже в прошлом — выбери другое 🙂",
                show_alert=True,
            )
            return

        reminder = await add_reminder(task_id, offset, remind_at)
        await scheduler_service.schedule_reminder(task_id, offset, reminder.reminder_id, remind_at)
        await callback.answer("Добавлено ✅")

    updated_offsets = {reminder.offset for reminder in await get_task_reminders(task_id)}
    # Пересчитываем доступные варианты заново — время идёт, и пока
    # человек тыкает чекбоксы, какой-то из ранее доступных вариантов мог
    # физически "протухнуть" (см. database.requests.available_reminder_offsets).
    offsets_available_now = available_reminder_offsets(task.deadline)
    keyboard = reminders_keyboard(context, task_id, offsets_available_now, updated_offsets)
    await callback.message.edit_reply_markup(reply_markup=keyboard.as_markup())


@router.callback_query(F.data.startswith("rmd_clear:"))
async def rmd_clear(callback: CallbackQuery) -> None:
    """Кнопка "🔕 Без напоминаний" — снимает разом все уже выбранные галочки."""
    _, context, task_id_str = callback.data.split(":", maxsplit=2)
    task_id = int(task_id_str)

    task = await get_task(task_id=task_id, user_id=callback.from_user.id)

    await clear_task_reminders(task_id)
    scheduler_service.unschedule_all_for_task(task_id)

    await callback.answer("Напоминания отключены 🔕")
    offsets_available_now = available_reminder_offsets(task.deadline) if task and task.deadline else []
    keyboard = reminders_keyboard(context, task_id, offsets_available_now, set())
    await callback.message.edit_reply_markup(reply_markup=keyboard.as_markup())


@router.callback_query(F.data.startswith("rmd_done:"))
async def rmd_done(callback: CallbackQuery) -> None:
    """Кнопка "💾 Готово к сохранению" — завершает шаг напоминаний. Для CTX_NEW
    (только что созданная задача) показывает финальную карточку, для
    CTX_EDIT (правка напоминаний уже существующей задачи из её карточки)
    возвращает саму карточку."""
    _, context, task_id_str = callback.data.split(":", maxsplit=2)
    task_id = int(task_id_str)

    task = await get_task(task_id=task_id, user_id=callback.from_user.id)
    if task is None:
        await callback.answer("Не удалось найти эту задачу 🤔", show_alert=True)
        return

    await callback.answer("Готово! 🎉")

    if context == CTX_NEW:
        await _finish_wizard(
            callback,
            texts.deadline_saved_text(task.title, task.priority, task.deadline, task.deadline_all_day),
        )
        return

    reminders = await get_task_reminders(task_id)
    await _finish_wizard(
        callback,
        texts.task_card_text(task, reminders),
        task_card_keyboard(task_id, offset=0, in_checklist_today=texts.task_in_checklist_today(task)).as_markup(),
    )


# --- Детальная карточка задачи (список → клик по задаче) ----------------------

@router.callback_query(F.data.startswith("card_open:"))
async def card_open(callback: CallbackQuery) -> None:
    _, task_id_str, offset_str = callback.data.split(":", maxsplit=2)
    task_id, offset = int(task_id_str), int(offset_str)

    task = await get_task(task_id=task_id, user_id=callback.from_user.id)
    if task is None:
        await callback.answer("Не удалось найти эту задачу 🤔", show_alert=True)
        return

    reminders = await get_task_reminders(task_id)
    await callback.answer()
    await callback.message.edit_text(
        texts.task_card_text(task, reminders),
        reply_markup=task_card_keyboard(task_id, offset, in_checklist_today=texts.task_in_checklist_today(task)).as_markup(),
    )


@router.callback_query(F.data.startswith("card_edittext:"))
async def card_edittext(callback: CallbackQuery) -> None:
    task_id = int(callback.data.split(":", maxsplit=1)[1])
    task = await get_task(task_id=task_id, user_id=callback.from_user.id)
    if task is None:
        await callback.answer("Не удалось найти эту задачу 🤔", show_alert=True)
        return

    _pending_rename[callback.from_user.id] = task_id
    await callback.answer()
    await callback.message.edit_text(
        f"✏️ Пришли новый текст для задачи «{texts.escape(task.title)}» следующим сообщением 👇"
    )


@router.callback_query(F.data.startswith("card_editdl:"))
async def card_editdl(callback: CallbackQuery) -> None:
    task_id = int(callback.data.split(":", maxsplit=1)[1])
    task = await get_task(task_id=task_id, user_id=callback.from_user.id)
    if task is None:
        await callback.answer("Не удалось найти эту задачу 🤔", show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_text(
        texts.deadline_presets_prompt_text(task.title),
        reply_markup=deadline_presets_keyboard(CTX_EDIT, task_id).as_markup(),
    )


@router.callback_query(F.data.startswith("card_remind:"))
async def card_remind(callback: CallbackQuery) -> None:
    task_id = int(callback.data.split(":", maxsplit=1)[1])
    task = await get_task(task_id=task_id, user_id=callback.from_user.id)
    if task is None:
        await callback.answer("Не удалось найти эту задачу 🤔", show_alert=True)
        return
    if task.deadline is None:
        await callback.answer("Сначала задай дату/время — кнопка «📅 Изменить дату / время» 🤔", show_alert=True)
        return

    await callback.answer()
    current_offsets = {reminder.offset for reminder in await get_task_reminders(task_id)}
    offsets_available_now = available_reminder_offsets(task.deadline)
    await callback.message.edit_text(
        texts.reminders_prompt_text(task.title, task.deadline, task.deadline_all_day),
        reply_markup=reminders_keyboard(CTX_EDIT, task_id, offsets_available_now, current_offsets).as_markup(),
    )


@router.callback_query(F.data.startswith("card_chk_in:"))
async def card_chk_in(callback: CallbackQuery) -> None:
    """
    Тумблер "☀️ Включить в Чек-лист дня" в карточке задачи — быстро
    ставит дедлайн "сегодня, в течение дня" (без похода в мастер срока
    целиком), задача сразу появляется на главном экране чек-листа. Если у
    задачи уже был дедлайн (на другой день или с точным часом) — он
    заменяется: тумблер именно про "сегодня", а не про сохранение старого
    значения.
    """
    task_id = int(callback.data.split(":", maxsplit=1)[1])
    user_id = callback.from_user.id
    task = await get_task(task_id=task_id, user_id=user_id)
    if task is None:
        await callback.answer("Не удалось найти эту задачу 🤔", show_alert=True)
        return

    today_end = datetime.combine(date.today(), datetime.max.time().replace(microsecond=0))
    await set_task_deadline(task_id=task_id, user_id=user_id, deadline=today_end, all_day=True)

    # Пересчитываем уже выбранные напоминания под новый дедлайн — та же
    # логика, что и при обычной правке даты/времени из карточки (td_confirm).
    kept, removed_offsets = await recompute_reminders_for_new_deadline(task_id, today_end)
    for offset in removed_offsets:
        scheduler_service.unschedule_reminder(task_id, offset)
    for reminder in kept:
        await scheduler_service.schedule_reminder(task_id, reminder.offset, reminder.reminder_id, reminder.remind_at)

    task = await get_task(task_id=task_id, user_id=user_id)
    if task is None:
        await callback.answer("Не удалось найти эту задачу 🤔", show_alert=True)
        return

    await callback.answer("Добавлено в Чек-лист дня ☀️")
    reminders = await get_task_reminders(task_id)
    await callback.message.edit_text(
        texts.task_card_text(task, reminders),
        reply_markup=task_card_keyboard(task_id, offset=0, in_checklist_today=True).as_markup(),
    )


@router.callback_query(F.data.startswith("card_chk_out:"))
async def card_chk_out(callback: CallbackQuery) -> None:
    """
    Тумблер "🌙 Убрать из Чек-листа дня" в карточке задачи — снимает
    сегодняшний дедлайн целиком (та же операция, что и кнопка "⚪️ Без
    срока" в календаре, и кнопка "🚫 Убрать из дня" в режиме настройки
    чек-листа, см. handlers/checklist.py::chked_trm): задача НЕ удаляется,
    просто возвращается в бэклог без срока.
    """
    task_id = int(callback.data.split(":", maxsplit=1)[1])
    user_id = callback.from_user.id

    await set_task_deadline(task_id=task_id, user_id=user_id, deadline=None, all_day=False)
    await clear_task_reminders(task_id)
    scheduler_service.unschedule_all_for_task(task_id)

    task = await get_task(task_id=task_id, user_id=user_id)
    if task is None:
        await callback.answer("Не удалось найти эту задачу 🤔", show_alert=True)
        return

    await callback.answer("Убрано из Чек-листа дня 🌙")
    reminders = await get_task_reminders(task_id)
    await callback.message.edit_text(
        texts.task_card_text(task, reminders),
        reply_markup=task_card_keyboard(task_id, offset=0, in_checklist_today=False).as_markup(),
    )


@router.callback_query(F.data.startswith("card_delete:"))
async def card_delete(callback: CallbackQuery) -> None:
    task_id = int(callback.data.split(":", maxsplit=1)[1])
    task = await get_task(task_id=task_id, user_id=callback.from_user.id)
    if task is None:
        await callback.answer("Не удалось найти эту задачу 🤔", show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_text(
        f"🗑 Точно удалить задачу «{texts.escape(task.title)}»? Это нельзя отменить.",
        reply_markup=delete_confirm_keyboard(task_id).as_markup(),
    )


@router.callback_query(F.data.startswith("card_delyes:"))
async def card_delyes(callback: CallbackQuery) -> None:
    task_id = int(callback.data.split(":", maxsplit=1)[1])
    user_id = callback.from_user.id

    scheduler_service.unschedule_all_for_task(task_id)
    success = await delete_task(task_id=task_id, user_id=user_id)

    if not success:
        await callback.answer("Не удалось найти эту задачу 🤔", show_alert=True)
        return

    await callback.answer("Удалено 🗑")
    await callback.message.edit_text("🗑 Задача удалена.")


@router.callback_query(F.data.startswith("card_delno:"))
async def card_delno(callback: CallbackQuery) -> None:
    task_id = int(callback.data.split(":", maxsplit=1)[1])
    task = await get_task(task_id=task_id, user_id=callback.from_user.id)
    if task is None:
        await callback.answer("Не удалось найти эту задачу 🤔", show_alert=True)
        return

    await callback.answer("Отменено")
    reminders = await get_task_reminders(task_id)
    await callback.message.edit_text(
        texts.task_card_text(task, reminders),
        reply_markup=task_card_keyboard(task_id, offset=0, in_checklist_today=texts.task_in_checklist_today(task)).as_markup(),
    )


# --- Time Management Module: кнопки под пуш-уведомлением о напоминании --------

@router.callback_query(F.data.startswith("remind_task_done:"))
async def remind_task_done(callback: CallbackQuery) -> None:
    """Кнопка "✅ Сделано! (+XP)" прямо под уведомлением-напоминанием —
    переиспользует ту же логику закрытия задачи, что и накопительный
    чек-ин "✅ Я сделал!"."""
    task_id = int(callback.data.split(":", maxsplit=1)[1])
    if await complete_task(callback, task_id) is None:
        return
    # Реакция (награда) уже ушла отдельным сообщением внутри complete_task —
    # здесь просто убираем кнопки с самого уведомления, чтобы нельзя было
    # нажать повторно.
    await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith("snz_menu:"))
async def snz_menu(callback: CallbackQuery) -> None:
    """Кнопка "💤 Отложить..." под уведомлением — открывает меню вариантов
    переноса, подменяя клавиатуру ПРЯМО в этом же сообщении."""
    _, reminder_id_str, task_id_str = callback.data.split(":", maxsplit=2)
    reminder_id, task_id = int(reminder_id_str), int(task_id_str)

    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=snooze_menu_keyboard(reminder_id, task_id).as_markup())


@router.callback_query(F.data.startswith("snz_quick:"))
async def snz_quick(callback: CallbackQuery) -> None:
    """Быстрый перенос: +15 минут / +30 минут / +1 час / +3 часа от текущего момента."""
    _, reminder_id_str, task_id_str, minutes_str = callback.data.split(":", maxsplit=3)
    reminder_id, minutes = int(reminder_id_str), int(minutes_str)

    new_time = datetime.now() + timedelta(minutes=minutes)
    ok = await scheduler_service.reschedule(reminder_id, new_time)
    if not ok:
        await callback.answer("Не удалось найти это напоминание 🤔", show_alert=True)
        return

    await callback.answer("Перенесено ⏰")
    await callback.message.edit_text(texts.snooze_confirmed_text(new_time))


@router.callback_query(F.data.startswith("snz_tmr:"))
async def snz_tmr(callback: CallbackQuery) -> None:
    """Перенос "на завтра утро (09:00)" / "на завтра вечер (18:00)"."""
    _, reminder_id_str, task_id_str, hour_str, minute_str = callback.data.split(":", maxsplit=4)
    reminder_id, hour, minute = int(reminder_id_str), int(hour_str), int(minute_str)

    tomorrow = date.today() + timedelta(days=1)
    new_time = datetime.combine(tomorrow, datetime.min.time()).replace(hour=hour, minute=minute)

    ok = await scheduler_service.reschedule(reminder_id, new_time)
    if not ok:
        await callback.answer("Не удалось найти это напоминание 🤔", show_alert=True)
        return

    await callback.answer("Перенесено ⏰")
    await callback.message.edit_text(texts.snooze_confirmed_text(new_time))


@router.callback_query(F.data.startswith("snz_custom:"))
async def snz_custom(callback: CallbackQuery) -> None:
    """"🗓 Выбрать новую дату и время" — открывает тот же календарь, что и
    мастер создания задачи, но в контексте CTX_SNOOZE (переносит именно
    это напоминание, а не дедлайн всей задачи)."""
    _, reminder_id_str, task_id_str = callback.data.split(":", maxsplit=2)
    reminder_id = int(reminder_id_str)

    found = await get_reminder_with_task(reminder_id)
    if found is None:
        await callback.answer("Не удалось найти это напоминание 🤔", show_alert=True)
        return
    _, task = found

    await callback.answer()
    today = date.today()
    await callback.message.edit_text(
        f"🗓 Выбери новую дату для напоминания про «{texts.escape(task.title)}»:",
        reply_markup=calendar_keyboard(CTX_SNOOZE, reminder_id, today.year, today.month).as_markup(),
    )


@router.callback_query(F.data.startswith("snz_cancel:"))
async def snz_cancel(callback: CallbackQuery) -> None:
    """"◀️ Отмена" в меню "💤 Отложить" — возвращает исходное уведомление как было."""
    _, reminder_id_str, task_id_str = callback.data.split(":", maxsplit=2)
    reminder_id, task_id = int(reminder_id_str), int(task_id_str)

    found = await get_reminder_with_task(reminder_id)
    if found is None:
        await callback.answer("Не удалось найти это напоминание 🤔", show_alert=True)
        return
    reminder, task = found

    await callback.answer()
    await callback.message.edit_text(
        texts.reminder_notification_text(task, reminder.offset),
        reply_markup=reminder_notification_keyboard(reminder_id, task).as_markup(),
    )


@router.callback_query(F.data.startswith("remind_open_card:"))
async def remind_open_card(callback: CallbackQuery) -> None:
    """"✏️ Открыть карточку" под пуш-уведомлением — превращает само
    уведомление в полную карточку задачи (изменить текст/дату/напоминания,
    удалить), чтобы не нужно было отдельно идти в общее меню "📋 Задачи" и
    искать дело в списке."""
    _, reminder_id_str, task_id_str = callback.data.split(":", maxsplit=2)
    task_id = int(task_id_str)

    task = await get_task(task_id=task_id, user_id=callback.from_user.id)
    if task is None:
        await callback.answer("Не удалось найти эту задачу 🤔", show_alert=True)
        return

    reminders = await get_task_reminders(task_id)
    await callback.answer()
    await callback.message.edit_text(
        texts.task_card_text(task, reminders),
        reply_markup=task_card_keyboard(task_id, offset=0, in_checklist_today=texts.task_in_checklist_today(task)).as_markup(),
    )
