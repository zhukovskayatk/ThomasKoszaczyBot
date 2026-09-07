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
            reply_markup=task_card_keyboard(pending_task_id, offset=0, in_checklist_today=texts.task_in_checklist_today(task)).as_markup