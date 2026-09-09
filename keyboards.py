"""
Построение клавиатур (постоянной и inline).

Тексты кнопок и логика их расположения собраны здесь же, отдельно от
handlers/*, чтобы хэндлеры отвечали только за "что делать", а не за
"как это нарисовать".
"""

import calendar as _calendar
from datetime import date

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database.models import Priority, ReminderOffset
from services.leveling import XP_BY_PRIORITY, XP_PER_TASK
from texts import (
    PRIORITY_LABELS,
    PRIORITY_MARKERS,
    REMINDER_OFFSET_LABELS,
    URGENCY_MARKERS,
    button_deadline_suffix,
    task_urgency_category,
)

# --- Тексты постоянных кнопок внизу экрана ------------------------------------

TASKS_BUTTON_TEXT = "📋 Задачи"
ADD_BUTTON_TEXT = "➕ Добавить"
QUICK_CLOSE_BUTTON_TEXT = "✅ Я сделал!"
CHECKLIST_BUTTON_TEXT = "☀️ Чек-лист"
PROFILE_BUTTON_TEXT = "👤 Профиль и Настройки"
PREMIUM_BUTTON_TEXT = "💎 Premium"
PARTNER_BUTTON_TEXT = "👥 Партнёр"

# --- "Устаревшие" подписи кнопок --------------------------------------------
#
# ВАЖНО: постоянная клавиатура (ReplyKeyboardMarkup) в Telegram-клиенте
# обновляется ТОЛЬКО когда бот присылает новое сообщение с этой клавиатурой.
# Она не обновляется сама по себе, просто потому что мы поменяли код.
#
# Подписи кнопок несколько раз менялись — если у кого-то на экране всё ещё
# старая подпись (бот ещё не успел прислать свежую клавиатуру), нажатие на
# неё отправит боту старый текст. Без этих списков бот не узнал бы кнопку и
# создал бы из её текста "задачу" (именно так когда-то и случилось). Здесь
# мы явно перечисляем все прежние подписи, чтобы старая кнопка продолжала
# работать как ожидается, а не превращалась в мусорную задачу.
LEGACY_TASKS_BUTTON_TEXTS = frozenset({"📋 Мои задачи"})
LEGACY_ADD_BUTTON_TEXTS = frozenset({"➕ Добавить задачу"})
LEGACY_QUICK_CLOSE_BUTTON_TEXTS = frozenset({"🎉 Я сделал!"})
LEGACY_PROFILE_BUTTON_TEXTS = frozenset({"🏆 Профиль"})

# Сколько задач показывать за раз в списке "📋 Мои задачи" (карточки задач)
TASKS_PAGE_SIZE = 6

# Сколько символов из названия задачи оставлять на кнопке списка (см.
# tasks_page_keyboard) — вместе с маркером срочности и короткой датой
# кнопка должна умещаться в одну строку.
_TASK_BUTTON_TITLE_LIMIT = 22

# Общий callback_data для всех декоративных/неактивных кнопок (заголовок
# месяца, шапка дней недели, пустые клетки сетки, уже прошедшие дни,
# заблокированная кнопка "◀️" на текущем месяце, текущее значение времени
# посередине компактного селектора). Единый обработчик — просто
# callback.answer().
NOOP_CALLBACK = "noop"

# --- Контексты мастера дедлайна/времени/напоминаний ----------------------------
#
# Календарь и барабан времени переиспользуются в трёх разных сценариях —
# контекст "путешествует" вместе с entity_id в callback_data и решает,
# что делать после выбора даты/времени:
# - CTX_NEW  — мастер создания задачи (после выбора приоритета), entity_id
#              это task_id; после подтверждения времени идём дальше, к
#              мультивыбору напоминаний.
# - CTX_EDIT — правка даты/времени УЖЕ существующей задачи из её карточки
#              ("📅 Изменить дату / время"), entity_id это task_id; после
#              подтверждения — пересчитываем уже выбранные напоминания под
#              новый дедлайн и возвращаемся на карточку (см. handlers/tasks.py).
# - CTX_SNOOZE — полная перенастройка ОДНОГО напоминания из меню "💤
#              Отложить" ("🗓 Выбрать новую дату и время"), entity_id это
#              reminder_id; "Без срока"/"В течение дня" для этого сценария
#              смысла не имеют и в календаре не показываются.
CTX_NEW = "new"
CTX_EDIT = "edit"
CTX_SNOOZE = "snz"

# --- Постоянная клавиатура (под полем ввода) ----------------------------------

main_menu_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=TASKS_BUTTON_TEXT), KeyboardButton(text=ADD_BUTTON_TEXT)],
        [KeyboardButton(text=QUICK_CLOSE_BUTTON_TEXT), KeyboardButton(text=CHECKLIST_BUTTON_TEXT)],
        [KeyboardButton(text=PROFILE_BUTTON_TEXT), KeyboardButton(text=PREMIUM_BUTTON_TEXT)],
        [KeyboardButton(text=PARTNER_BUTTON_TEXT)],
    ],
    resize_keyboard=True,
)


def premium_buy_keyboard(is_active: bool) -> InlineKeyboardBuilder:
    """
    Кнопка на экране "💎 Premium". Текст меняется в зависимости от того,
    активна ли подписка уже сейчас (см. handlers/subscription.py) — купить
    первый раз или продлить действующую формулируются по-разному, хотя
    технически это один и тот же callback ("premium_buy").
    """
    builder = InlineKeyboardBuilder()
    label = "🔄 Продлить Premium" if is_active else "💎 Оформить Premium"
    builder.button(text=label, callback_data="premium_buy")
    return builder


def partner_screen_keyboard(is_premium: bool, is_paired: bool) -> InlineKeyboardBuilder:
    """
    Кнопки на экране "👥 Партнёр" (см. handlers/partner.py):
    - без Premium — единственная кнопка ведёт на экран оформления подписки
      (партнёрский режим целиком заблокирован без неё);
    - с Premium и без пары — предложение получить ссылку-приглашение;
    - с Premium и уже в паре — только отвязка (сам процесс приглашения
      партнёру не нужен, пока пара уже есть).
    """
    builder = InlineKeyboardBuilder()
    if not is_premium:
        builder.button(text="💎 Оформить Premium", callback_data="partner_go_premium")
    elif is_paired:
        builder.button(text="🔓 Отвязать партнёра", callback_data="partner_unlink")
    else:
        builder.button(text="🔗 Получить ссылку-приглашение", callback_data="partner_invite")
    builder.adjust(1)
    return builder


def partner_unlink_confirm_keyboard() -> InlineKeyboardBuilder:
    """Подтверждение перед разрывом пары (кнопка "🔓 Отвязать партнёра")."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🔓 Да, отвязать", callback_data="partner_unlink_yes")
    builder.button(text="◀️ Отмена", callback_data="partner_unlink_no")
    builder.adjust(2)
    return builder


# Подпись кнопки "Без приоритета" на ЭКРАНЕ ВЫБОРА (см. priority_keyboard) —
# сознательно отличается от PRIORITY_LABELS[Priority.none] ("Обычный ⚪️"),
# которая используется для ОТОБРАЖЕНИЯ уже выбранного состояния в карточке
# задачи и подтверждениях. Кнопка описывает ДЕЙСТВИЕ ("не выбирать
# приоритет"), карточка — уже свершившийся ФАКТ ("приоритет обычный").
_PRIORITY_NONE_BUTTON_TEXT = "⚪️ Без приоритета"


def priority_keyboard(task_id: int) -> InlineKeyboardBuilder:
    """
    Четыре кнопки выбора приоритета (🟢/🟡/🔴 + "⚪️ Без приоритета" —
    сеткой 2×2) под карточкой задачи, плюс "❌ Отмена" отдельной строкой —
    на этом шаге задача уже создана в БД (у неё есть только название), но
    ничего больше не выбрано, поэтому отмена здесь просто удаляет её
    целиком (см. handlers/tasks.py::prio_cancel) — как будто её и не
    создавали.

    Кнопки "Выполнено" здесь больше нет — закрывать задачи сразу после
    создания не нужно, для этого есть отдельный сценарий "🎉 Я сделал!".
    """
    builder = InlineKeyboardBuilder()
    for priority in Priority:
        label = _PRIORITY_NONE_BUTTON_TEXT if priority == Priority.none else PRIORITY_LABELS[priority]
        builder.button(
            text=label,
            callback_data=f"prio:{task_id}:{priority.value}",
        )
    builder.button(text="❌ Отмена", callback_data=f"prio_cancel:{task_id}")
    builder.adjust(2, 2, 1)
    return builder


def sharing_choice_keyboard(task_id: int) -> InlineKeyboardBuilder:
    """
    Шаг "Личное/Партнёр" мастера создания задачи (см. handlers/tasks.py::
    _offer_sharing_or_finish/share_choice) — показывается ТОЛЬКО когда у
    создателя активен Premium И уже есть привязанный партнёр (иначе шаг
    целиком пропускается, задача остаётся личной по умолчанию, как и
    раньше). Две кнопки, без промежуточных состояний — ровно как просила
    пользовательница: либо "🔒 Личное", либо "👥 Партнёру" (общий список на
    двоих), никаких дополнительных настроек видимости.

    Позже категорию всё ещё можно сменить из уже открытой карточки задачи
    (см. task_card_keyboard/card_toggle_shared) — этот шаг только задаёт
    исходное значение при создании.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="🔒 Личное", callback_data=f"share_choice:{task_id}:personal")
    builder.button(text="👥 Партнёру", callback_data=f"share_choice:{task_id}:shared")
    builder.adjust(2)
    return builder


def quick_close_session_keyboard(
    remaining_tasks: list, has_progress: bool, viewer_user_id: int | None = None
) -> InlineKeyboardBuilder:
    """
    Клавиатура накопительного чек-ина "✅ Я сделал!" (см.
    handlers/tasks.py::qc_open/qc_close/qc_finish) — одна кнопка на каждую
    ещё НЕ закрытую в этой сессии задачу ("▫️ Название (+N XP)"), клик по
    ней закрывает задачу и сама кнопка исчезает из клавиатуры (остальные
    остаются). Внизу — кнопка выхода, её текст меняется по смыслу:
    - пока в сессии ещё ничего не закрыто — "❌ Закрыть меню" (выходить
      пока не жалко, отчитываться не о чем);
    - как только закрыта хотя бы одна задача — "🏁 Всё на сегодня!"
      (это уже осознанное завершение сессии с зафиксированным отчётом).

    viewer_user_id — id того, кто сейчас смотрит на меню; remaining_tasks
    (см. database.requests.get_active_tasks) может включать и ОБЩИЕ задачи
    партнёра (партнёрский режим, Premium) — такие помечаются значком 👥
    перед названием, чтобы не путать со своими.
    """
    builder = InlineKeyboardBuilder()
    for task in remaining_tasks:
        xp_amount = XP_BY_PRIORITY.get(task.priority, XP_PER_TASK)
        short_title = task.title if len(task.title) <= 28 else task.title[:27] + "…"
        marker = "👥 " if (viewer_user_id is not None and task.user_id != viewer_user_id) else ""
        builder.button(text=f"▫️ {marker}{short_title} (+{xp_amount} XP)", callback_data=f"qc_close:{task.task_id}")

    if has_progress:
        builder.button(text="🏁 Всё на сегодня!", callback_data="qc_finish")
    else:
        builder.button(text="❌ Закрыть меню", callback_data="qc_abort")

    builder.adjust(*([1] * len(remaining_tasks)), 1)
    return builder


def _task_button_label(task, viewer_user_id: int | None = None) -> str:
    """
    Текст кнопки одной задачи в списке "📋 Мои задачи": иконка СРОЧНОСТИ
    (🔥 просрочено/горит сегодня, ⏳ ближайшие дни, 🌱 без дедлайна — см.
    texts.task_urgency_category, это НЕ маркер приоритета — те остались
    только кружками 🔴🟡🟢 в карточке и в тексте самого списка) + название
    + короткий суффикс дедлайна, например "🔥 Чесать котов · 18:15". Для
    задач без дедлайна суффикса нет вообще (см. texts.button_deadline_suffix).

    Если задача принадлежит не viewer_user_id, а его партнёру (общая
    задача, партнёрский режим, Premium — см. database.requests.get_active_tasks),
    перед названием добавляется значок 👥, чтобы не путать "моё" и "общее".
    """
    marker = URGENCY_MARKERS.get(task_urgency_category(task), "")
    partner_marker = "👥 " if (viewer_user_id is not None and task.user_id != viewer_user_id) else ""
    short_title = (
        task.title if len(task.title) <= _TASK_BUTTON_TITLE_LIMIT
        else task.title[:_TASK_BUTTON_TITLE_LIMIT - 1] + "…"
    )
    suffix = button_deadline_suffix(task)
    if suffix:
        return f"{marker} {partner_marker}{short_title} · {suffix}"
    return f"{marker} {partner_marker}{short_title}"


def tasks_page_keyboard(
    tasks_sorted: list, offset: int, viewer_user_id: int | None = None
) -> tuple[InlineKeyboardBuilder, int]:
    """
    Строит одну "страницу" списка "📋 Мои задачи": каждая задача — кликабельная
    кнопка, открывающая её карточку (см. task_card_keyboard). tasks_sorted —
    уже отсортированный по срочности список (get_active_tasks_by_deadline:
    сначала просроченные/горящие сегодня, потом ближайшие дни по возрастанию
    даты, потом без дедлайна — та же сортировка, что и маркеры на кнопках).

    viewer_user_id — см. _task_button_label (маркер 👥 у общих задач партнёра).
    """
    offset = max(0, offset)
    if tasks_sorted and offset >= len(tasks_sorted):
        last_page_start = (len(tasks_sorted) - 1) // TASKS_PAGE_SIZE * TASKS_PAGE_SIZE
        offset = last_page_start

    page = tasks_sorted[offset:offset + TASKS_PAGE_SIZE]

    builder = InlineKeyboardBuilder()
    for task in page:
        builder.button(
            text=_task_button_label(task, viewer_user_id), callback_data=f"card_open:{task.task_id}:{offset}"
        )

    row_sizes = [1] * len(page)

    has_prev = offset > 0
    has_next = offset + TASKS_PAGE_SIZE < len(tasks_sorted)

    if has_prev:
        builder.button(text="⬅️ Назад", callback_data=f"tasks_page:{max(0, offset - TASKS_PAGE_SIZE)}")
    if has_next:
        builder.button(text="➡️ Показать другие", callback_data=f"tasks_page:{offset + TASKS_PAGE_SIZE}")

    nav_count = int(has_prev) + int(has_next)
    if nav_count:
        row_sizes.append(nav_count)

    if row_sizes:
        builder.adjust(*row_sizes)

    return builder, offset


# --- Модуль дедлайнов и напоминаний (Time Management Module) ------------------
#
# Главное правило (по требованию): никакого ручного ввода дат/времени
# текстом — весь выбор строится на инлайн-кнопках. Три шага: календарь →
# компактный селектор времени → напоминания.

_MONTH_NAMES_RU = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}

_WEEKDAY_HEADERS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def shift_month(year: int, month: int, direction: str) -> tuple[int, int]:
    """Сдвигает (год, месяц) на один месяц вперёд/назад с переносом года."""
    if direction == "next":
        month += 1
        if month > 12:
            month = 1
            year += 1
    else:
        month -= 1
        if month < 1:
            month = 12
            year -= 1
    return year, month


def deadline_presets_keyboard(context: str, entity_id: int) -> InlineKeyboardBuilder:
    """
    Экран быстрых пресетов срока (UX как в Todoist/Things 3) — показывается
    ВМЕСТО сразу открытого календаря (см. handlers/tasks.py:
    process_priority_choice/card_editdl). Для большинства задач точная дата
    не нужна — "Сегодня/Завтра/на выходных/на неделе" покрывает почти все
    случаи, полный интерактивный календарь остаётся доступен отдельной
    кнопкой "🗓 Выбрать в календаре...".

    "🌱 Без срока" и "❌ Отмена" переиспользуют ТЕ ЖЕ callback_data, что и
    одноимённые кнопки внутри самого календаря (cal_none:/wiz_cancel:) —
    их хэндлеры уже не зависят от того, с какого именно экрана пришёл клик.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="📍 Сегодня", callback_data=f"preset_day:{context}:{entity_id}:today")
    builder.button(text="🌅 Завтра", callback_data=f"preset_day:{context}:{entity_id}:tomorrow")
    builder.button(text="🗓 В эти выходные", callback_data=f"preset_day:{context}:{entity_id}:weekend")
    builder.button(text="☀️ В течение недели", callback_data=f"preset_day:{context}:{entity_id}:week")
    builder.button(text="🗓 Выбрать в календаре...", callback_data=f"cal_open:{context}:{entity_id}")
    builder.button(text="🌱 Без срока (когда будет настрой ✨)", callback_data=f"cal_none:{context}:{entity_id}")
    builder.button(text="❌ Отмена", callback_data=f"wiz_cancel:{context}:{entity_id}")
    builder.adjust(2, 2, 1, 1, 1)
    return builder


def calendar_keyboard(context: str, entity_id: int, year: int, month: int) -> InlineKeyboardBuilder:
    """
    Инлайн-календарь на месяц: заголовок, шапка дней недели, сетка чисел,
    переключение месяцев.

    - Уже прошедшие дни — просто маленькая некликабельная точка "·" (без
      номера дня), чтобы не сливаться визуально с активными датами.
    - Сегодняшний день выделен маркером: "📍17".
    - Будущие дни (и сегодня) — обычные кликабельные кнопки.
    - Перелистнуть раньше текущего месяца нельзя — "◀️" на нём заблокирована.
    - Для CTX_SNOOZE (перенос ОДНОГО напоминания) кнопки "⚪️ Без срока" и
      "☀️ В течение дня" не показываются — для переноса конкретного
      напоминания они не имеют смысла, нужна именно дата и время.
    - "❌ Отмена" внизу есть всегда — чтобы не оставлять человека зависшим
      посреди мастера, если он передумал (см. handlers/tasks.py::wiz_cancel).
    """
    builder = InlineKeyboardBuilder()
    today = date.today()

    builder.button(text=f"{_MONTH_NAMES_RU[month]} {year}", callback_data=NOOP_CALLBACK)

    for day_label in _WEEKDAY_HEADERS_RU:
        builder.button(text=day_label, callback_data=NOOP_CALLBACK)

    cal = _calendar.Calendar(firstweekday=0)  # неделя начинается с понедельника
    month_days = list(cal.itermonthdays(year, month))

    for day in month_days:
        if day == 0:
            # Клетка вне текущего месяца — просто выравнивание сетки.
            builder.button(text="·", callback_data=NOOP_CALLBACK)
            continue

        cell_date = date(year, month, day)
        if cell_date < today:
            # Прошедший день — просто точка, без номера: активные (кликабельные)
            # даты за счёт этого сразу бросаются в глаза на фоне прошедших.
            builder.button(text="·", callback_data=NOOP_CALLBACK)
        else:
            label = f"📍{day}" if cell_date == today else str(day)
            builder.button(
                text=label,
                callback_data=f"cal_day:{context}:{entity_id}:{year}:{month}:{day}",
            )

    is_current_month = (year, month) == (today.year, today.month)
    if is_current_month:
        builder.button(text="·", callback_data=NOOP_CALLBACK)
    else:
        builder.button(text="◀️", callback_data=f"cal_nav:{context}:{entity_id}:{year}:{month}:prev")

    weeks_count = len(month_days) // 7
    row_sizes = [1, 7, *([7] * weeks_count)]

    if context == CTX_SNOOZE:
        builder.button(text="▶️", callback_data=f"cal_nav:{context}:{entity_id}:{year}:{month}:next")
        row_sizes.append(2)
    else:
        builder.button(text="⚪️ Без срока", callback_data=f"cal_none:{context}:{entity_id}")
        builder.button(text="▶️", callback_data=f"cal_nav:{context}:{entity_id}:{year}:{month}:next")
        builder.button(text="☀️ В течение дня", callback_data=f"cal_allday:{context}:{entity_id}")
        row_sizes += [3, 1]

    builder.button(text="❌ Отмена", callback_data=f"wiz_cancel:{context}:{entity_id}")
    row_sizes.append(1)

    builder.adjust(*row_sizes)
    return builder


def time_drum_keyboard(
    context: str, entity_id: int, year: int, month: int, day: int, hour: int, minute: int
) -> InlineKeyboardBuilder:
    """
    Компактный кнопочный селектор времени (заменил громоздкий 7-рядный
    "барабан" в стиле iOS — тот занимал слишком много места на экране и
    неудобно нажимался). Два ряда ➖/значение/➕: часы шагом 1, минуты
    шагом 15. Клик по ➖/➕ сразу пересчитывает значение и обновляет
    клавиатуру на месте (edit_message_reply_markup), без новых сообщений.
    Дата (год/месяц/день), выбранная на предыдущем шаге, просто "едет"
    вместе со временем в callback_data — отдельного состояния (FSM) для
    этого не заводим, как и везде в этом мастере.
    """
    builder = InlineKeyboardBuilder()

    def pick(h: int, m: int) -> str:
        return f"td_pick:{context}:{entity_id}:{year}:{month}:{day}:{h}:{m}"

    hour_plus = (hour + 1) % 24
    hour_minus = (hour - 1) % 24
    minute_plus = (minute + 15) % 60
    minute_minus = (minute - 15) % 60

    current_label = f"{hour:02d}:{minute:02d}"

    builder.button(text="➖ 1 ч", callback_data=pick(hour_minus, minute))
    builder.button(text=current_label, callback_data=NOOP_CALLBACK)
    builder.button(text="➕ 1 ч", callback_data=pick(hour_plus, minute))

    builder.button(text="➖ 15 м", callback_data=pick(hour, minute_minus))
    builder.button(text=current_label, callback_data=NOOP_CALLBACK)
    builder.button(text="➕ 15 м", callback_data=pick(hour, minute_plus))

    row_sizes = [3, 3]

    # "☀️ В течение дня" здесь работает для ЛЮБОЙ уже выбранной на
    # предыдущем шаге даты (год/месяц/день едут в callback_data), а не
    # только для сегодня — в отличие от одноимённой кнопки в календаре
    # (та — быстрый шорткат именно на сегодня). Для CTX_SNOOZE (перенос
    # ОДНОГО напоминания) кнопка не имеет смысла — там нужен точный момент.
    if context != CTX_SNOOZE:
        builder.button(
            text="☀️ В течение дня (без часа)",
            callback_data=f"td_allday:{context}:{entity_id}:{year}:{month}:{day}",
        )
        row_sizes.append(1)

    # "❌ Отмена" и "✅ Подтвердить" — рядом, в одну строку: это последний
    # шаг перед сохранением, обе кнопки одинаково важны и одинаково часто
    # нужны, отдельная строка под каждую только удлиняла бы экран.
    builder.button(text="❌ Отмена", callback_data=f"wiz_cancel:{context}:{entity_id}")
    builder.button(
        text=f"✅ Подтвердить {hour:02d}:{minute:02d}",
        callback_data=f"td_confirm:{context}:{entity_id}:{year}:{month}:{day}:{hour}:{minute}",
    )
    row_sizes.append(2)

    builder.adjust(*row_sizes)
    return builder


# Порядок кнопок в сетке напоминаний — ОТ БЛИЖНЕГО смещения к дальнему
# ("За 15 минут" сначала, "За 7 дней" почти в конце), с "В точное время
# дедлайна" всегда последней, отдельной широкой строкой. Специально не
# полагаемся на порядок объявления самого ReminderOffset (там для другой
# цели — от дальнего к ближнему, см. database/models.py) — здесь порядок
# именно под сетку 2 колонки, которую проще всего читать по возрастанию.
_REMINDER_DISPLAY_ORDER = (
    ReminderOffset.minutes_15,
    ReminderOffset.hour_1,
    ReminderOffset.days_1,
    ReminderOffset.days_3,
    ReminderOffset.days_5,
    ReminderOffset.days_7,
    ReminderOffset.exact,
)


def reminders_keyboard(
    context: str, task_id: int, available_offsets, selected_offsets: set
) -> InlineKeyboardBuilder:
    """
    Мультивыбор напоминаний (чекбоксы ◻️/☑️) компактной сеткой в 2 колонки
    (см. _REMINDER_DISPLAY_ORDER), "В точное время дедлайна" — отдельной
    широкой строкой в конце.

    available_offsets — варианты, которые ещё физически МОГУТ сработать
    для текущего дедлайна (см. database.requests.available_reminder_offsets:
    "дедлайн минус смещение" ещё в будущем) — нельзя предлагать напоминание
    "за 7 дней", если до дедлайна остался один день, оно бы сработало уже
    в прошлом. Недоступные варианты просто не попадают в сетку — никакого
    отдельного экрана "все варианты" с серыми кнопками. selected_offsets —
    набор ReminderOffset, для которых уже есть запись в БД (см.
    database.requests.get_task_reminders) — по нему решаем, какая галочка
    сейчас стоит. Показываем ОБЪЕДИНЕНИЕ available_offsets и
    selected_offsets: если пользователь уже выбрал какой-то вариант раньше,
    а время с тех пор почти истекло, галочку всё равно нужно показать —
    иначе её будет невозможно снять.

    context решает, куда вернуться по кнопке "💾 Готово к сохранению" — см.
    keyboards.CTX_NEW / CTX_EDIT.
    """
    builder = InlineKeyboardBuilder()
    displayed_offsets = [
        offset for offset in _REMINDER_DISPLAY_ORDER
        if offset in available_offsets or offset in selected_offsets
    ]
    for offset in displayed_offsets:
        checkbox = "☑️" if offset in selected_offsets else "◻️"
        builder.button(
            text=f"{checkbox} {REMINDER_OFFSET_LABELS[offset]}",
            callback_data=f"rmd_toggle:{context}:{task_id}:{offset.value}",
        )
    builder.button(text="🔕 Без напоминаний", callback_data=f"rmd_clear:{context}:{task_id}")
    builder.button(text="💾 Готово к сохранению", callback_data=f"rmd_done:{context}:{task_id}")

    # "В точное время дедлайна" (если доступно — оно всегда последним в
    # _REMINDER_DISPLAY_ORDER) идёт отдельной широкой строкой, остальные —
    # парами по 2 в порядке возрастания смещения.
    has_exact = ReminderOffset.exact in displayed_offsets
    pair_count = len(displayed_offsets) - (1 if has_exact else 0)

    row_sizes = []
    remaining = pair_count
    while remaining > 0:
        take = min(2, remaining)
        row_sizes.append(take)
        remaining -= take
    if has_exact:
        row_sizes.append(1)
    row_sizes += [1, 1]

    builder.adjust(*row_sizes)
    return builder


def reminder_notification_keyboard(reminder_id: int, task) -> InlineKeyboardBuilder:
    """
    Кнопки-"пульт" под пуш-уведомлением о напоминании — три чётких
    действия, без раздумий: закрыть прямо отсюда (с суммой XP прямо на
    кнопке — понятно, ради чего тап), отложить, либо открыть карточку
    задачи, если планы поменялись сильнее, чем просто "попозже".
    """
    xp_amount = XP_BY_PRIORITY.get(task.priority, XP_PER_TASK)
    builder = InlineKeyboardBuilder()
    builder.button(text=f"✅ Сделано! (+{xp_amount} XP)", callback_data=f"remind_task_done:{task.task_id}")
    builder.button(text="💤 Отложить...", callback_data=f"snz_menu:{reminder_id}:{task.task_id}")
    builder.button(text="✏️ Открыть карточку", callback_data=f"remind_open_card:{reminder_id}:{task.task_id}")
    builder.adjust(1, 1, 1)
    return builder


def snooze_menu_keyboard(reminder_id: int, task_id: int) -> InlineKeyboardBuilder:
    """
    Меню кнопки "💤 Отложить..." под пуш-уведомлением: компактная сетка в
    2 ряда самых частых интервалов, полная перенастройка через
    календарь/селектор времени, либо возврат к исходному уведомлению.
    Намеренно только 2 быстрых интервала (а не 4) — под пальцы, без
    лишнего выбора, который всё равно почти никогда не используют; более
    тонкая настройка всегда доступна через "🗓 Другое время...".
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="⏳ +15 минут", callback_data=f"snz_quick:{reminder_id}:{task_id}:15")
    builder.button(text="⏳ +1 час", callback_data=f"snz_quick:{reminder_id}:{task_id}:60")
    builder.button(text="🌅 Завтра 09:00", callback_data=f"snz_tmr:{reminder_id}:{task_id}:9:0")
    builder.button(text="🌇 Завтра 18:00", callback_data=f"snz_tmr:{reminder_id}:{task_id}:18:0")
    builder.button(text="🗓 Другое время...", callback_data=f"snz_custom:{reminder_id}:{task_id}")
    builder.button(text="◀️ Назад", callback_data=f"snz_cancel:{reminder_id}:{task_id}")
    builder.adjust(2, 2, 1, 1)
    return builder


def task_card_keyboard(
    task_id: int,
    offset: int,
    in_checklist_today: bool,
    is_shared: bool = False,
    can_toggle_shared: bool = False,
) -> InlineKeyboardBuilder:
    """
    Кнопки управления карточкой задачи (открывается кликом по задаче в
    списке "📋 Мои задачи"). offset — с какой страницы списка сюда попали,
    чтобы "◀️ Назад к списку" вернул именно на неё.

    in_checklist_today — тумблер "☀️ В чек-лист дня / Убрать из чек-листа"
    (см. texts.task_in_checklist_today и handlers/tasks.py::card_chk_in/
    card_chk_out): True — задача уже видна в сегодняшнем "☀️ Чек-лист дня"
    (её дедлайн — сегодня), кнопка предлагает убрать её оттуда (снимает
    дедлайн, задача уходит в бэклог); False — задачи там ещё нет, кнопка
    предлагает быстро добавить (дедлайн станет "сегодня, в течение дня").

    can_toggle_shared — партнёрский режим (Premium): показывать ли кнопку
    "личная/общая" вообще — её видит только ВЛАДЕЛЕЦ задачи, и только с
    активным Premium (см. handlers/tasks.py::_task_card_extras); партнёр,
    открывший общую задачу, эту кнопку не видит — решать, каким задачам
    "быть общими", может только сам владелец (см.
    database.requests.toggle_task_shared). is_shared — текущее состояние
    этого конкретного переключателя (Task.shared), решает подпись кнопки.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Изменить текст", callback_data=f"card_edittext:{task_id}")
    builder.button(text="📅 Изменить дату / время", callback_data=f"card_editdl:{task_id}")
    builder.button(text="🔔 Настроить напоминания", callback_data=f"card_remind:{task_id}")
    if in_checklist_today:
        builder.button(text="🌙 Убрать из Чек-листа дня", callback_data=f"card_chk_out:{task_id}")
    else:
        builder.button(text="☀️ Включить в Чек-лист дня", callback_data=f"card_chk_in:{task_id}")

    row_sizes = [1, 1, 1, 1]
    if can_toggle_shared:
        toggle_label = "🔒 Сделать личной" if is_shared else "👥 Сделать общей с партнёром"
        builder.button(text=toggle_label, callback_data=f"card_toggle_shared:{task_id}")
        row_sizes.append(1)

    builder.button(text="🗑 Удалить", callback_data=f"card_delete:{task_id}")
    builder.button(text="◀️ Назад к списку", callback_data=f"tasks_page:{offset}")
    row_sizes += [1, 1]
    builder.adjust(*row_sizes)
    return builder


def delete_confirm_keyboard(task_id: int) -> InlineKeyboardBuilder:
    """Подтверждение удаления задачи (карточка → "🗑 Удалить")."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🗑 Да, удалить", callback_data=f"card_delyes:{task_id}")
    builder.button(text="◀️ Отмена", callback_data=f"card_delno:{task_id}")
    builder.adjust(2)
    return builder


def notifications_entry_keyboard() -> InlineKeyboardBuilder:
    """Короткая подсказка под карточкой профиля — ведёт на экран 🔔 Уведомления
    (см. handlers/profile.py::notif_open)."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🔔 Уведомления", callback_data="notif_open")
    builder.adjust(1)
    return builder


def notification_settings_keyboard(
    reminders_enabled: bool,
    quiet_hours_enabled: bool,
    morning_checklist_enabled: bool,
    checklist_morning_push_enabled: bool,
    checklist_evening_push_enabled: bool,
) -> InlineKeyboardBuilder:
    """
    Экран "🔔 Уведомления" (Профиль и Настройки → 🔔 Уведомления) — пять
    переключателей (см. User.reminders_enabled/quiet_hours_enabled/
    morning_checklist_enabled/checklist_morning_push_enabled/
    checklist_evening_push_enabled). Каждый клик сразу меняет состояние в
    БД и перерисовывает галочку на месте (edit_message_reply_markup) — без
    отдельной кнопки "Сохранить", изменения применяются мгновенно.

    Два новых пункта (утренний/вечерний пуш чек-листа дня) — НЕ то же
    самое, что "Утренний чек-лист (09:00)" выше: тот — пассивный текстовый
    дайджест, эти два — приглашения в интерактивный экран "☀️ Чек-лист дня"
    (см. services.scheduler._send_checklist_morning_briefs/
    _send_checklist_evening_summaries).
    """
    builder = InlineKeyboardBuilder()
    morning_mark = "☑️" if morning_checklist_enabled else "◻️"
    reminders_mark = "☑️" if reminders_enabled else "◻️"
    quiet_mark = "☑️" if quiet_hours_enabled else "◻️"
    checklist_morning_mark = "☑️" if checklist_morning_push_enabled else "◻️"
    checklist_evening_mark = "☑️" if checklist_evening_push_enabled else "◻️"
    builder.button(text=f"{morning_mark} Утренний чек-лист (09:00)", callback_data="notif_toggle:morning")
    builder.button(text=f"{reminders_mark} Напоминания по задачам", callback_data="notif_toggle:reminders")
    builder.button(text=f"{quiet_mark} 🌙 Тихие часы (22:00–08:00)", callback_data="notif_toggle:quiet")
    builder.button(
        text=f"{checklist_morning_mark} ☀️ Приглашение в чек-лист (09:00)",
        callback_data="notif_toggle:checklist_morning",
    )
    builder.button(
        text=f"{checklist_evening_mark} 🌙 Вечерняя сводка чек-листа (21:00)",
        callback_data="notif_toggle:checklist_evening",
    )
    builder.button(text="◀️ Назад к профилю", callback_data="notif_back")
    builder.adjust(1, 1, 1, 1, 1, 1)
    return builder


# --- Модуль "☀️ Чек-лист дня" ---------------------------------------------------

_CHECKLIST_TITLE_LIMIT = 26


def _checklist_short_title(title: str, limit: int = _CHECKLIST_TITLE_LIMIT) -> str:
    """Обрезка длинного названия под ширину кнопки — тот же приём, что и
    _task_button_label выше, отдельная копия под свой лимит длины."""
    return title if len(title) <= limit else title[:limit - 1] + "…"


def checklist_dashboard_keyboard(habits: list, tasks: list) -> InlineKeyboardBuilder:
    """
    Главный экран "☀️ Чек-лист дня": каждая привычка и каждая задача на
    сегодня — своя кнопка-чекбокс (▫️ ↔ ✅), тап переключает её ПРЯМО на
    месте (см. handlers/checklist.py::chk_htoggle/chk_ttoggle,
    edit_message_text — текст тоже меняется, т.к. в нём прогресс "N из M").
    Внизу — быстрое добавление рутины/дела на сегодня и вход в режим
    ручной настройки состава списка (см. checklist_edit_keyboard).
    """
    builder = InlineKeyboardBuilder()
    row_sizes: list[int] = []

    for habit in habits:
        checkbox = "✅" if habit.done_today else "▫️"
        label = f"{checkbox} {_checklist_short_title(habit.title)} (+{habit.xp_reward} XP)"
        builder.button(text=label, callback_data=f"chk_htoggle:{habit.habit_id}")
        row_sizes.append(1)

    for task in tasks:
        xp_amount = XP_BY_PRIORITY.get(task.priority, XP_PER_TASK)
        marker = PRIORITY_MARKERS.get(task.priority, "")
        suffix = button_deadline_suffix(task)
        label = f"▫️ {_checklist_short_title(task.title)}"
        if suffix:
            label += f" · {suffix}"
        if marker:
            label += f" {marker}"
        label += f" (+{xp_amount} XP)"
        builder.button(text=label, callback_data=f"chk_ttoggle:{task.task_id}")
        row_sizes.append(1)

    builder.button(text="➕ Добавить рутину", callback_data="chk_add_routine")
    builder.button(text="➕ Дело на сегодня", callback_data="chk_add_task")
    row_sizes.append(2)

    builder.button(text="⚙️ Настроить фокус дня", callback_data="chk_edit")
    row_sizes.append(1)

    builder.adjust(*row_sizes)
    return builder


def checklist_edit_keyboard(habits: list, tasks: list) -> InlineKeyboardBuilder:
    """
    Режим "⚙️ Настроить фокус дня" — полное ручное управление составом
    чек-листа: у привычек — переключатель "скрыть на сегодня" (без
    удаления самой привычки) и отдельная кнопка необратимого удаления; у
    задач — только "🚫 Убрать из дня" (снимает сегодняшний дедлайн, задача
    остаётся в бэклоге целой и невредимой). Название задачи слева — просто
    подпись (NOOP_CALLBACK), не кнопка: у задач тут нет действия "скрыть",
    только "убрать из дня" целиком.
    """
    builder = InlineKeyboardBuilder()
    row_sizes: list[int] = []

    for habit in habits:
        eye_icon = "🙈" if habit.hidden_today else "👁"
        builder.button(
            text=f"{eye_icon} {_checklist_short_title(habit.title)}",
            callback_data=f"chked_hhide:{habit.habit_id}",
        )
        builder.button(text="🗑", callback_data=f"chked_hdel:{habit.habit_id}")
        row_sizes.append(2)

    for task in tasks:
        suffix = button_deadline_suffix(task)
        label = f"👁 {_checklist_short_title(task.title)}"
        if suffix:
            label += f" · {suffix}"
        builder.button(text=label, callback_data=NOOP_CALLBACK)
        builder.button(text="🚫 Убрать из дня", callback_data=f"chked_trm:{task.task_id}")
        row_sizes.append(2)

    builder.button(text="➕ Добавить быстрый пункт", callback_data="chked_addtask")
    row_sizes.append(1)
    builder.button(text="💾 Сохранить и вернуться в Чек-лист", callback_data="chked_save")
    row_sizes.append(1)

    builder.adjust(*row_sizes)
    return builder


def checklist_push_open_keyboard() -> InlineKeyboardBuilder:
    """Кнопка "☀️ Открыть Чек-лист дня" под утренним пуш-приглашением (см.
    services.scheduler._send_checklist_morning_briefs, handlers/checklist.py::chk_open)."""
    builder = InlineKeyboardBuilder()
    builder.button(text="☀️ Открыть Чек-лист дня", callback_data="chk_open")
    builder.adjust(1)
    return builder


def habit_delete_confirm_keyboard(habit_id: int) -> InlineKeyboardBuilder:
    """Подтверждение перед необратимым удалением привычки (кнопка "🗑" в
    режиме настройки чек-листа)."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🗑 Да, удалить", callback_data=f"chked_hdel_yes:{habit_id}")
    builder.button(text="◀️ Отмена", callback_data=f"chked_hdel_no:{habit_id}")
    builder.adjust(2)
    return builder


def streak_rescue_keyboard(cost: int) -> InlineKeyboardBuilder:
    """
    Кнопка ручного спасения серии активности за XP (см.
    handlers/profile.py::streak_rescue, database.requests.rescue_streak_with_xp).
    Цена не "едет" в callback_data — сервер всегда пересчитывает её сам
    (STREAK_RESCUE_XP_COST), чтобы нельзя было подменить сумму списания.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text=f"💎 Спасти серию за {cost} XP", callback_data="streak_rescue")
    builder.adjust(1)
    return builder
