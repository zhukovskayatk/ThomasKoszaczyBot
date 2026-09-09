"""
Генерация текста сообщений.

Весь HTML-текст, который в итоге видит пользователь, собран в одном
месте — так тон общения (ToV) и форматирование можно менять, не залезая
в логику хэндлеров (handlers/*) или построение клавиатур (keyboards.py).

Важно про экранирование: у бота включён parse_mode=HTML (см. main.py).
Это значит, что Telegram воспринимает <, >, & в тексте сообщения как
разметку. Название задачи — это ВВОД ПОЛЬЗОВАТЕЛЯ, и если человек
напишет что-то вроде "почитать про <html>", символы "<" и ">" сломают
разбор HTML и сообщение вообще не отправится (именно так раньше упало
приветствие на /start). Поэтому весь пользовательский текст перед
вставкой в HTML-сообщение обязательно пропускаем через escape().
"""

import random
from datetime import date, datetime, timedelta
from html import escape as _escape

from database.models import Priority, ReminderOffset
from services.leveling import LevelInfo, XP_BY_PRIORITY, XP_PER_TASK, build_progress_bar

# Подписи приоритета в формате "Слово + эмодзи" — используются везде,
# где приоритет показывается человеку текстом (карточка задачи, список).
PRIORITY_LABELS = {
    Priority.low: "Низкий 🟢",
    Priority.medium: "Средний 🟡",
    Priority.high: "Высокий 🔴",
    # Показывается в карточке задачи и подтверждениях как "Приоритет:
    # Обычный ⚪️" — сама КНОПКА выбора при этом подписана иначе ("⚪️ Без
    # приоритета", см. keyboards.priority_keyboard) — это два разных по
    # смыслу текста для одного и того же значения (действие на кнопке vs.
    # уже свершившееся состояние в карточке), поэтому не переиспользуем
    # один и тот же текст для обоих мест.
    Priority.none: "Обычный ⚪️",
}

# Подписи вариантов напоминания — используются в меню мультивыбора
# (keyboards.reminders_keyboard) и в карточке задачи.
REMINDER_OFFSET_LABELS = {
    ReminderOffset.days_7: "За 7 дней",
    ReminderOffset.days_5: "За 5 дней",
    ReminderOffset.days_3: "За 3 дня",
    ReminderOffset.days_1: "За 1 день",
    ReminderOffset.hour_1: "За 1 час",
    ReminderOffset.minutes_15: "За 15 минут",
    ReminderOffset.exact: "В точное время дедлайна",
}

# Порядок напоминаний в карточке задачи — тот же, что объявление Enum'а
# (от самого дальнего смещения к самому близкому).
_REMINDER_ORDER = {offset: index for index, offset in enumerate(ReminderOffset)}

_MONTHS_GENITIVE_RU = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}

# Короткая форма месяца для кнопок списка задач (см. short_deadline_label) —
# "18 авг", а не "18 августа", чтобы уместиться на кнопке рядом с названием.
_MONTHS_SHORT_RU = {
    1: "янв", 2: "фев", 3: "мар", 4: "апр",
    5: "май", 6: "июн", 7: "июл", 8: "авг",
    9: "сен", 10: "окт", 11: "ноя", 12: "дек",
}

# Иконки СРОЧНОСТИ (не приоритета!) для группировки списка задач и кнопок —
# см. task_urgency_category. 0 — просрочено/горит сегодня, 1 — ближайшие
# дни, 2 — без дедлайна (бэклог). Раньше здесь были цветные кружки — те же,
# что и у приоритета — и это визуально путало два разных понятия (что
# делу пора, и насколько дело вообще важное). Теперь у срочности своя,
# независимая от приоритета иконка-тема (часы), а кружки 🔴🟡🟢 остаются
# строго за приоритетом (см. PRIORITY_MARKERS/PRIORITY_LABELS ниже).
URGENCY_MARKERS = {0: "🔥", 1: "⏳", 2: "🌱"}

# Заголовки блоков-групп списка задач (см. tasks_list_text) — та же
# срочность, что и URGENCY_MARKERS, просто с подписью для дашборда.
_GROUP_TITLES = {0: "Горят сегодня", 1: "Ближайшие", 2: "В бэклоге"}

# Только цветной кружок приоритета, без слова — используется в строке-
# буллете списка задач (см. _list_bullet_line), где рядом уже есть текст
# и точное слово приоритета было бы лишним. Полная подпись со словом —
# в PRIORITY_LABELS выше (карточка задачи и т.п.).
PRIORITY_MARKERS = {
    Priority.low: "🟢",
    Priority.medium: "🟡",
    Priority.high: "🔴",
    Priority.none: "⚪️",
}

# Тонкий HTML-разделитель для карточек (задача/профиль/список) — <code>
# держит моноширинность, так что линия ровно совпадает по ширине в любом
# клиенте, в отличие от голого текста, который может "гулять" из-за
# пропорционального шрифта Telegram.
_DIVIDER = "<code>──────────────────</code>"


def escape(text: str) -> str:
    """Экранирует пользовательский текст перед вставкой в HTML-сообщение."""
    return _escape(text, quote=False)


def format_deadline(deadline, all_day: bool = False) -> str:
    """
    Человеко-читаемый формат дедлайна.
    all_day=True (кнопка "☀️ В течение дня") — без часа: "17 августа
    (в течение дня)". Иначе — с точным временем: "25 августа в 14:00".
    """
    if all_day:
        return f"{deadline.day} {_MONTHS_GENITIVE_RU[deadline.month]} (в течение дня)"
    return f"{deadline.day} {_MONTHS_GENITIVE_RU[deadline.month]} в {deadline.strftime('%H:%M')}"


def _date_ru(moment) -> str:
    """Дата без времени в родительном падеже, например «28 сентября 2026»."""
    return f"{moment.day} {_MONTHS_GENITIVE_RU[moment.month]} {moment.year}"


def premium_status_text(is_active: bool, premium_until, price_stars: int, duration_days: int) -> str:
    """Экран "💎 Premium" — статус подписки + приглашение оформить/продлить."""
    if is_active:
        return (
            "💎 <b>Premium активен</b>\n"
            f"Действует до {_date_ru(premium_until)}.\n\n"
            "Открыт партнёрский режим — общие задачи для двоих 👥\n\n"
            f"Продлить ещё на {duration_days} дней можно в любой момент — "
            f"дни добавятся к уже оплаченному сроку, а не сгорят."
        )
    return (
        "💎 <b>Thomas Koszaczy Premium</b>\n\n"
        "Что открывает Premium:\n"
        "👥 Партнёрский режим — общее пространство задач для двоих\n"
        "✨ Все будущие Premium-фичи автоматически\n\n"
        f"Стоимость: {price_stars} ⭐ Stars / {duration_days} дней.\n"
        "Оплата — прямо здесь, в Telegram, звёздами. Карты и банковские "
        "данные боту не нужны и не видны."
    )


def premium_purchased_text(premium_until) -> str:
    """Подтверждение после успешной оплаты (message.successful_payment)."""
    return (
        "✨ <b>Спасибо! Premium активирован.</b>\n"
        f"Действует до {_date_ru(premium_until)}.\n\n"
        "Партнёрский режим теперь доступен — загляни в 👥 Партнёр, там "
        "появится кнопка приглашения."
    )


# --- Партнёрский режим (экран "👥 Партнёр", Premium-фича) -----------------------

def _partner_display(partner) -> str:
    """Короткое упоминание партнёра в тексте — по username, если он есть у
    человека в Telegram, иначе просто нейтрально "партнёром"."""
    if partner is not None and partner.username:
        return f"@{partner.username}"
    return "партнёром"


def partner_screen_text(is_premium: bool, partner) -> str:
    """
    Экран "👥 Партнёр". partner — объект User партнёра, если пара уже
    образована (см. database.requests.get_partner), иначе None.
    """
    if not is_premium:
        return (
            "👥 <b>Партнёрский режим</b>\n\n"
            "Общее пространство задач для двоих — что один пометит "
            "«общей задачей», увидит и сможет закрыть другой.\n\n"
            "Доступно с 💎 Premium."
        )
    if partner is not None:
        return (
            "👥 <b>Партнёрский режим активен</b>\n"
            f"Вы в паре с {_partner_display(partner)}.\n\n"
            "В карточке любой своей задачи можно нажать «Сделать общей с "
            "партнёром» — партнёр увидит её у себя в списке и сможет "
            "закрыть или отредактировать."
        )
    return (
        "👥 <b>Партнёрский режим</b>\n\n"
        "Пары пока нет. Получи ссылку-приглашение и отправь её человеку, "
        "с которым хочешь делиться задачами — как только он перейдёт по "
        "ней в Telegram, вы окажетесь в паре.\n\n"
        "После этого в карточке любой своей задачи появится кнопка "
        "«Сделать общей с партнёром»."
    )


def partner_invite_link_text(link: str) -> str:
    """Показывается после нажатия "🔗 Получить ссылку-приглашение"."""
    return (
        "🔗 <b>Ссылка готова!</b>\n"
        f"{link}\n\n"
        "Отправь её партнёру — как только он перейдёт по ней, вы окажетесь "
        "в паре. Ссылка одноразовая: если получить новую, старая перестанет "
        "работать."
    )


_PARTNER_INVITE_ERROR_TEXTS = {
    "invalid_code": "🤔 Это приглашение уже не действует — попроси прислать новую ссылку.",
    "self_invite": "😄 По собственной ссылке-приглашению в пару не встать — отправь её партнёру.",
    "already_paired": "У тебя уже есть партнёр — сначала отвяжи его в 👥 Партнёр, если хочешь пригласить другого.",
    "inviter_already_paired": "🤔 У этого приглашения уже появился партнёр — попроси прислать новую ссылку.",
}


def partner_invite_error_text(reason: str) -> str:
    """Текст ошибки при переходе по невалидной/устаревшей ссылке-приглашению
    (см. database.requests.PairResult.reason)."""
    return _PARTNER_INVITE_ERROR_TEXTS.get(reason, "🤔 Не получилось создать пару — попробуй ещё раз.")


def partner_paired_text(other) -> str:
    """
    Подтверждение образования пары — показывается ОБЕИМ сторонам (принявшему
    приглашение — сразу, пригласившему — отдельным сообщением, см.
    handlers/start.py). other — объект User второй стороны пары.
    """
    return (
        f"✨ <b>Готово! Вы в паре с {_partner_display(other)}</b> 👥\n\n"
        "Теперь в карточке любой своей задачи можно сделать её «общей» — "
        "партнёр увидит её у себя и сможет помочь закрыть."
    )


def partner_unlink_confirm_text(partner) -> str:
    """Подтверждение перед разрывом пары (кнопка "🔓 Отвязать партнёра")."""
    return (
        f"🔓 Точно отвязать {_partner_display(partner)}? Общие задачи "
        "перестанут быть видны друг другу, но никуда не денутся — каждая "
        "останется у своего владельца."
    )


def partner_unlinked_text() -> str:
    """Подтверждение после успешного разрыва пары."""
    return "🔓 Пара разорвана. Вернуться к партнёрскому режиму можно в любой момент — из 👥 Партнёр."


def partner_unlinked_by_other_text(other) -> str:
    """Уведомление ВТОРОЙ стороне, когда пару разорвал не он сам (см.
    handlers/partner.py::partner_unlink_yes)."""
    return f"🔓 {_partner_display(other)} отвязал(а) партнёрский доступ — общие задачи больше не видны друг другу."


def welcome_text() -> str:
    """
    Компактное приветствие /start от маскота — котика Thomas Koszaczy.
    Раньше здесь был длинный список "как этим пользоваться" сплошным
    текстом — заменён на короткую тёплую подводку + список инструментов
    по пунктам, без давления и суеты в самом тоне.
    """
    return (
        "Привет! Я <b>Thomas Koszaczy</b> 🐾\n\n"
        "Помогу навести порядок в мыслях, вовремя напомню о делах и поддержу "
        "на каждом шаге — без давления и суеты.\n\n"
        "<b>Твои инструменты:</b>\n"
        "• <b>➕ Добавить</b> — быстро записать задачу и настроить срок\n"
        "• <b>📋 Задачи</b> — список дел и карточки для редактирования\n"
        "• <b>✅ Я сделал!</b> — закрыть дело в один клик и забрать опыт\n"
        "• <b>☀️ Чек-лист</b> — фокус на сегодняшний день\n\n"
        "<i>Просто напиши мне, что нужно сделать, или воспользуйся меню внизу 👇</i>"
    )


def task_added_text(title: str, priority: Priority) -> str:
    """Лаконичное подтверждение создания задачи (без кнопки "Выполнено")."""
    label = PRIORITY_LABELS[priority]
    return (
        f"✨ <b>Записал!</b>\n"
        f"📌 «{escape(title)}»\n"
        f"<i>Приоритет: {label}</i>"
    )


def task_urgency_category(task) -> int:
    """
    Категория "срочности" задачи (0 — просрочено/горит сегодня, 1 —
    ближайшие дни, 2 — без дедлайна). Используется ТОЛЬКО для цветовых
    маркеров на кнопках списка задач (см. keyboards.tasks_page_keyboard) —
    сама сортировка списка считается отдельно, тем же способом, в
    database.requests._deadline_sort_key (продублировано здесь, чтобы
    UI-слою не нужно было тянуть значение категории из БД-слоя туда и
    обратно, это чистая функция без запросов).
    """
    if task.deadline is None:
        return 2
    today_end = datetime.combine(date.today(), datetime.max.time())
    if task.deadline <= today_end:
        return 0
    return 1


def task_in_checklist_today(task) -> bool:
    """
    Задача считается частью сегодняшнего "☀️ Чек-лист дня", если у неё есть
    дедлайн и он приходится ровно на сегодня — тот же критерий, что и
    database.requests.get_checklist_tasks_for_today, только как чистая
    функция без похода в БД (нужна, чтобы решить, какую подпись показать
    на кнопке-тумблере в карточке задачи, см.
    keyboards.task_card_keyboard).
    """
    return task.deadline is not None and task.deadline.date() == date.today()


def button_deadline_suffix(task) -> str:
    """
    Короткая подпись дедлайна для инлайн-кнопки в списке задач — умещается
    в одну строку рядом с названием. Иконка срочности (🔥/⏳/🌱) перед
    названием уже говорит "горит"/"скоро"/"без срока", поэтому здесь не
    дублируем это словами — только конкретика: точное время для горящих
    ("18:15"), короткая дата для ближайших ("19 авг"), и пустая строка для
    бэклога (там иконке 🌱 больше нечего добавить). Возвращает "" — тогда
    кнопка обходится без " · суффикс" вообще.
    """
    if task.deadline is None:
        return ""

    deadline_date = task.deadline.date()
    today = date.today()

    if deadline_date == today:
        return "" if task.deadline_all_day else task.deadline.strftime("%H:%M")

    if deadline_date == today + timedelta(days=1):
        return "завтра" if task.deadline_all_day else f"завтра, {task.deadline.strftime('%H:%M')}"

    short_date = f"{task.deadline.day} {_MONTHS_SHORT_RU[task.deadline.month]}"
    if task.deadline_all_day:
        return short_date
    return f"{short_date} {task.deadline.strftime('%H:%M')}"


def _list_bullet_detail(task) -> str:
    """
    Скобочная деталь дедлайна для строки-буллета в дашборде списка задач
    (см. tasks_list_text) — например " <i>(до 18:15)</i>". Пустая строка —
    для задач без дедлайна (бэклог, иконка 🌱 уже всё сказала). Возвращает
    строку С ведущим пробелом, чтобы просто конкатенировать после названия.
    """
    if task.deadline is None:
        return ""

    today = date.today()
    deadline_date = task.deadline.date()
    short_date = f"{task.deadline.day} {_MONTHS_SHORT_RU[task.deadline.month]}"

    if deadline_date < today:
        if task.deadline_all_day:
            return f" <i>(просрочено, {short_date})</i>"
        return f" <i>(просрочено, {short_date} {task.deadline.strftime('%H:%M')})</i>"

    if deadline_date == today:
        if task.deadline_all_day:
            return " <i>(сегодня)</i>"
        return f" <i>(до {task.deadline.strftime('%H:%M')})</i>"

    if deadline_date == today + timedelta(days=1):
        if task.deadline_all_day:
            return " <i>(завтра, день)</i>"
        return f" <i>(завтра, {task.deadline.strftime('%H:%M')})</i>"

    if task.deadline_all_day:
        return f" <i>({short_date}, день)</i>"
    return f" <i>({short_date} {task.deadline.strftime('%H:%M')})</i>"


def _list_bullet_line(task, viewer_user_id: int | None = None) -> str:
    """
    Одна строка-буллет в дашборде списка задач: название + скобочная
    деталь срока (если есть) + кружок приоритета в конце.

    viewer_user_id — если задача не принадлежит ему (общая задача
    партнёра, партнёрский режим, Premium — см.
    database.requests.get_active_tasks), перед названием добавляется
    значок 👥 (та же логика, что и у keyboards._task_button_label).
    """
    partner_marker = "👥 " if (viewer_user_id is not None and task.user_id != viewer_user_id) else ""
    return f"• {partner_marker}<b>{escape(task.title)}</b>{_list_bullet_detail(task)} {PRIORITY_MARKERS[task.priority]}"


def tasks_list_text(tasks: list, viewer_user_id: int | None = None) -> str:
    """
    Список задач как компактный дашборд с группировкой по срочности (см.
    task_urgency_category): 🔥 горят сегодня → ⏳ ближайшие → 🌱 бэклог,
    внутри каждой группы — строка-буллет на задачу. tasks — ПОЛНЫЙ список
    активных задач (не только текущая страница) — в отличие от инлайн-
    кнопок под сообщением (см. keyboards.tasks_page_keyboard), которые
    показывают только одну страницу, текст всегда даёт целостную картину.

    viewer_user_id — см. _list_bullet_line (маркер 👥 у общих задач партнёра).
    """
    if not tasks:
        return no_active_tasks_text()

    grouped: dict[int, list] = {0: [], 1: [], 2: []}
    for task in tasks:
        grouped[task_urgency_category(task)].append(task)

    blocks = ["📋 <b>ТВОИ ЗАДАЧИ</b>", _DIVIDER]
    for category in (0, 1, 2):
        group_tasks = grouped[category]
        if not group_tasks:
            continue
        blocks.append(f"{URGENCY_MARKERS[category]} <b>{_GROUP_TITLES[category]}:</b>")
        blocks.extend(_list_bullet_line(task, viewer_user_id) for task in group_tasks)
        blocks.append("")

    if blocks[-1] == "":
        blocks.pop()

    blocks.append(_DIVIDER)
    blocks.append("<i>Нажми на задачу, чтобы открыть карточку 🐾</i>")
    return "\n".join(blocks)


def no_active_tasks_text() -> str:
    return "Активных задач нет 🎉 Можно отдохнуть или добавить новую!"


def quick_close_nothing_text() -> str:
    return "Активных задач нет 🎉 Нечего закрывать!"


# --- Накопительный чек-ин "✅ Я сделал!" ---------------------------------------
#
# Всё в рамках ОДНОГО и того же сообщения (см. handlers/tasks.py::qc_close/
# qc_finish) — вместо отдельного сообщения-награды на каждую закрытую
# задачу, прогресс накапливается прямо перед глазами: вычеркнутые задачи
# не исчезают, а копятся одна под другой, XP суммируется.

# Тёплые реплики Томаса на каждое закрытие задачи внутри сессии — без
# повторов заезженной шутки про чай (та уже есть в другом пуле, см.
# services.meme_manager.SUCCESS_PHRASES, который используется для награды
# под пуш-уведомлением о напоминании, не здесь).
QUICK_CLOSE_PHRASES = [
    "Минус один хвост! Горжусь тобой 🐾",
    "Красиво закрыто. Плюс к спокойствию и опыту ✨",
    "Одной заботой меньше. Двигаемся в твоём ритме 🌿",
    "Отличный фокус! Твоя продуктивность сегодня сияет 💫",
    "Дело сделано — выдохни и похвали себя 🐾",
    "Я бы замурчал от такой эффективности 🐾",
    "Ты справляешься лучше, чем тебе кажется ✨",
    "Лапки на месте, фокус в порядке. Идём дальше ⚡️",
    "Твой внутренний баланс говорит тебе спасибо 🍵",
    "Маленький шаг для котика, огромный шаг для твоих целей 🐾",
    "Опыт в копилку! Твой уровень мастерства растёт 📈",
    "Гештальт закрыт, пространство чисто ✨",
    "Чётко, быстро и без лишней суеты 🎯",
    "Ещё один пункт вычеркнут. Ты большая умница 🐾",
    "Плюс к внутренней силе и спокойствию ⚡️",
    "Отличный результат! Не забудь сделать мягкую паузу ☕️",
    "Каждая галочка приближает к отдыху без чувства вины ✨",
    "День становится проще с каждым закрытым пунктом 🌿",
    "Зафиксировали! Переходим к следующему или пора на перерыв? 🐾",
    "Великолепная работа. Томас одобряет на все 100% 🏆",
]


def random_quick_close_phrase() -> str:
    """Случайная тёплая реплика Томаса для отчёта сессии (см. QUICK_CLOSE_PHRASES)."""
    return random.choice(QUICK_CLOSE_PHRASES)


def quick_close_start_text() -> str:
    """Приглашение в самом начале сессии — ещё ничего не закрыто."""
    return "Что удалось завершить? 🐾\nВыбирай пункт ниже — зафиксируем твой успех! ✨"


def quick_close_abort_text() -> str:
    """"❌ Закрыть меню" — сессия закрыта без единой закрытой задачи."""
    return "Хорошо, продолжим позже 🐾"


def _quick_close_closed_lines(closed: list[tuple[str, int]]) -> list[str]:
    """Строки уже закрытых в этой сессии задач — зачёркнутое название + XP."""
    return [f"✅ <s>{escape(title)}</s> <i>(+{xp} XP)</i>" for title, xp in closed]


def quick_close_progress_text(closed: list[tuple[str, int]], phrase: str) -> str:
    """
    Промежуточное состояние сессии — есть уже хотя бы одна закрытая задача,
    но остались ещё не закрытые (иначе сразу показывается финальный отчёт,
    см. quick_close_final_text). closed — список (название, XP) в порядке
    закрытия, накопленный в handlers/tasks.py::_QuickCloseSession.
    """
    total_xp = sum(xp for _, xp in closed)
    lines = [
        "🎉 <b>Твой прогресс за сессию:</b>",
        *_quick_close_closed_lines(closed),
        _DIVIDER,
        f"✨ <i>«{phrase}»</i>",
        f"⚡️ <b>Заработано сейчас:</b> +{total_xp} XP",
        "",
        "<i>Что ещё сделано?</i>",
    ]
    return "\n".join(lines)


def quick_close_final_text(closed: list[tuple[str, int]], phrase: str, streak_days: int) -> str:
    """
    Финальный отчёт сессии — по кнопке "🏁 Всё на сегодня!" или когда
    закрыты вообще все активные задачи. Инлайн-кнопки к этому моменту уже
    убраны вызывающим кодом (см. handlers/tasks.py::_qc_finalize) — это
    просто зафиксированная витрина побед, дальше с этим сообщением
    ничего не происходит.
    """
    total_xp = sum(xp for _, xp in closed)
    days_word = _pluralize_days(streak_days)
    lines = [
        "🏆 <b>Отличная работа! Отчёт за сессию:</b>",
        *_quick_close_closed_lines(closed),
        _DIVIDER,
        f"✨ <i>«{phrase}»</i>",
        f"⚡️ <b>Всего начислено:</b> +{total_xp} XP",
        f"🔥 <b>Серия активности:</b> {streak_days} {days_word} подряд",
    ]
    return "\n".join(lines)


# --- Модуль "☀️ Чек-лист дня" ---------------------------------------------------
#
# Автономный интерактивный экран (в отличие от morning_checklist_text
# ниже — тот пассивный текстовый дайджест утреннего пуша). Главный экран
# показывает ДВЕ группы пунктов: постоянные привычки/рутины (Habit) и
# обычные задачи с дедлайном сегодня — обе группы заполняются сами, без
# ручного дублирования (см. database.requests.get_visible_habits_today /
# get_checklist_tasks_for_today).

CHECKLIST_MASCOT_PHRASES = [
    "День только начинается. Выбирай комфортный темп ✨",
    "Не обязательно закрыть всё и сразу — маленькими шагами тоже в счёт 🐾",
    "Смотрю на список вместе с тобой. Начнём с чего-то одного? 🌿",
    "Порядок в делах — это не про идеально, а про спокойно 🐾",
    "Здесь только то, что действительно на сегодня. Остальное подождёт ✨",
]


def random_checklist_phrase() -> str:
    """Случайная реплика Томаса под заголовком чек-листа (см. CHECKLIST_MASCOT_PHRASES)."""
    return random.choice(CHECKLIST_MASCOT_PHRASES)


def checklist_dashboard_text(habits: list, tasks: list) -> str:
    """
    Главный экран "☀️ Чек-лист дня" (см. keyboards.checklist_dashboard_keyboard
    для самих кликабельных пунктов — они живут КНОПКАМИ под этим текстом,
    не строками в сообщении, ровно как в твоём мокапе). Текст — это только
    шапка с датой, реплика Томаса и короткий прогресс "сделано/всего".
    """
    today = date.today()
    header = f"☀️ <b>ТВОЙ ДЕНЬ</b> · {today.day} {_MONTHS_GENITIVE_RU[today.month]}"
    # tasks сюда попадают только ЕЩЁ не закрытые (закрытая задача сразу
    # пропадает и из списка, и из знаменателя — см.
    # database.requests.get_checklist_tasks_for_today), поэтому в "сделано"
    # считаем только привычки: они единственные, кто может быть виден на
    # экране одновременно и в статусе "сделано", и в статусе "ещё нет".
    total = len(habits) + len(tasks)
    done = sum(1 for h in habits if h.done_today)
    phrase = random_checklist_phrase()

    lines = [header, _DIVIDER, f"🐾 <i>Томас: «{phrase}»</i>"]
    if total:
        lines.append("")
        lines.append(f"📊 <b>Прогресс:</b> {done} из {total}")
    else:
        lines.append("")
        lines.append("<i>Пока пусто — добавь рутину или дело на сегодня кнопкой ниже 👇</i>")
    return "\n".join(lines)


def checklist_edit_screen_text() -> str:
    """Заголовок режима "⚙️ Настроить фокус дня" — полное ручное управление
    составом чек-листа (скрыть/удалить привычку, убрать задачу из дня)."""
    return (
        "⚙️ <b>НАСТРОЙКА ЧЕК-ЛИСТА НА СЕГОДНЯ</b>\n"
        f"{_DIVIDER}\n"
        "<i>Нажимай на значки, чтобы скрыть дело из фокуса дня или удалить пункт 🐾</i>"
    )


def checklist_add_habit_prompt_text() -> str:
    """Приглашение прислать текст новой рутины (кнопка "➕ Добавить рутину")."""
    return "🐾 Напиши название новой рутины следующим сообщением (например: «Витамины»)"


def checklist_add_task_prompt_text() -> str:
    """Приглашение прислать текст быстрого дела на сегодня (кнопка "➕ Дело
    на сегодня") — дедлайн проставится автоматически."""
    return "☀️ Напиши, что нужно сделать сегодня, следующим сообщением — дедлайн проставлю сам"


def habit_delete_confirm_text(title: str) -> str:
    """Подтверждение перед необратимым удалением привычки (в отличие от
    "убрать из дня" у задач — привычку целиком стереть нельзя отменить)."""
    return f"🗑 Точно удалить рутину «{escape(title)}» насовсем? Это нельзя отменить."


def checklist_morning_push_text(task_count: int) -> str:
    """
    Утренний пуш-приглашение в интерактивный чек-лист (~09:00) — короткий
    тизер с кнопкой, ведущей прямо в экран (см. handlers/checklist.py).
    Отдельная настройка от morning_checklist_text (тот — текстовый
    дайджест, см. User.morning_checklist_enabled).
    """
    if task_count <= 0:
        return "Доброе утро! ☀️ На сегодня горящих дел нет — можно выбрать спокойный темп. Заглянем в чек-лист? 🐾"
    word = _pluralize_tasks_word(task_count)
    return f"Доброе утро! ☀️ Чек-лист на сегодня готов. В фокусе {task_count} {word}. Заглянем?"


def _pluralize_tasks_word(n: int) -> str:
    """Русское склонение "дело/дела/дел" под число (та же схема, что и
    _pluralize_days)."""
    if 11 <= n % 100 <= 14:
        return "дел"
    last_digit = n % 10
    if last_digit == 1:
        return "дело"
    if 2 <= last_digit <= 4:
        return "дела"
    return "дел"


def checklist_evening_push_text(done_count: int, total_count: int, xp_earned: int) -> str:
    """
    Вечерняя мягкая сводка (~21:00) — намеренно БЕЗ чувства вины и упрёков
    даже если закрыто мало пунктов: просто честная цифра и приглашение
    отдохнуть, без "ты не успел" интонации.
    """
    if total_count <= 0:
        return "День подходит к концу 🐾 На сегодня в чек-листе ничего не было запланировано. Время отдыхать ✨"
    return (
        f"День подходит к концу 🐾 Сегодня закрыто {done_count} из {total_count} пунктов "
        f"(+{xp_earned} XP в копилку). Время отдыхать ✨"
    )


def reward_text(title: str, xp_amount: int, phrase: str) -> str:
    """Сообщение-награда за выполненную задачу (отдельным сообщением)."""
    return (
        f"✅ <b>«{escape(title)}»</b> <code>+{xp_amount} XP</code>\n"
        f"<i>{phrase}</i>"
    )


def level_up_text(level_info: LevelInfo) -> str:
    return (
        f"🎉 <b>Новый уровень!</b>\n"
        f"Теперь ты — {level_info.emoji} <b>{level_info.title}</b> "
        f"(Уровень {level_info.level})!"
    )


def _pluralize_days(n: int) -> str:
    """Русское склонение слова "день" под число: 1 день, 2/3/4 дня, 5 дней."""
    if 11 <= n % 100 <= 14:
        return "дней"
    last_digit = n % 10
    if last_digit == 1:
        return "день"
    if 2 <= last_digit <= 4:
        return "дня"
    return "дней"


def profile_text(
    level_info: LevelInfo, xp: int, completed_count: int, streak_days: int, frozen_today: bool = False
) -> str:
    """
    Карточка профиля с чёткой иерархией шрифтов. streak_days сюда уже
    приходит "живым" (см. database.requests.get_streak_status).
    frozen_today=True — прямо сейчас сработала автоматическая недельная
    заморозка серии ("Выходной для кота") — добавляем короткую плашку об
    этом, чтобы человек понимал, почему серия не оборвалась, хотя вчера он
    точно ничего не закрывал.
    """
    bar = build_progress_bar(xp, level_info.next_threshold)
    days_word = _pluralize_days(streak_days)
    lines = [
        "👤 <b>ПРОФИЛЬ ГЕРОЯ</b>",
        _DIVIDER,
        f"🎖 <b>Звание:</b> <i>{level_info.title} {level_info.emoji}</i>",
        f"⚡️ <b>Уровень:</b> {level_info.level}",
        f"📊 <b>Опыт:</b> <code>{bar}</code>",
        _DIVIDER,
        f"✅ <b>Закрыто задач:</b> {completed_count}",
        f"🔥 <b>Серия активности:</b> {streak_days} {days_word} подряд",
    ]
    if frozen_today:
        lines.append("🧊 <i>Пропущенный день не в счёт — сработал «Выходной для кота» 🐾</i>")
    return "\n".join(lines)


def streak_rescue_prompt_text(at_risk_days: int, cost: int) -> str:
    """
    Приглашение спасти серию за XP — показывается отдельным сообщением под
    карточкой профиля, когда авто-заморозка уже потрачена на этой неделе,
    но пропущен всего один день (см. database.requests.get_streak_status).
    """
    days_word = _pluralize_days(at_risk_days)
    return (
        f"💔 <b>Серия из {at_risk_days} {days_word} вот-вот прервётся!</b>\n"
        "«Выходной для кота» на этой неделе уже использован, но серию ещё можно спасти:\n"
        f"<code>{cost} XP</code> — и всё останется как есть 🐾"
    )


def streak_rescued_text(streak_days: int) -> str:
    """Подтверждение после успешного нажатия "💎 Спасти серию за N XP"."""
    days_word = _pluralize_days(streak_days)
    return f"💎 <b>Серия спасена!</b> {streak_days} {days_word} подряд — продолжаем 🔥"


# Мягкие реплики при по-настоящему сброшенной серии (пропуск 2+ дней —
# заморозка и ручное спасение здесь уже не действуют, см.
# database.requests.get_streak_status/StreakStatus.just_reset). Никакого
# чувства вины и упрёков — только нормализация паузы.
STREAK_RESET_PHRASES = [
    "Серия обнулилась, но весь твой накопленный опыт и закрытые дела "
    "остались с тобой ✨ Отдых — это часть пути. Начнём новую серию прямо сейчас 🐾",
    "Иногда сделать паузу важнее, чем держать счётчик 🌿 Главное — ты здесь. "
    "Погнали дальше в твоём комфортном темпе!",
    "Счётчик дней обновился, но мастерство никуда не делось ✨ Сделаем сегодня "
    "одно маленькое дело, чтобы зажечь новый огонёк? 🔥",
    "Никакого самобичевания 🐾 Жизнь идёт своим чередом, а мы просто "
    "продолжаем с того места, где остановились.",
]


def streak_reset_text() -> str:
    """Случайная мягкая реплика при обнаружении сброшенной серии (см. STREAK_RESET_PHRASES)."""
    return random.choice(STREAK_RESET_PHRASES)


# --- Модуль дедлайнов и напоминаний (Time Management Module) ------------------

def deadline_presets_prompt_text(title: str) -> str:
    """
    Приглашение на экране быстрых пресетов срока (см.
    keyboards.deadline_presets_keyboard) — первое, что видит человек после
    выбора приоритета, ВМЕСТО сразу открытого календаря. Полный календарь
    доступен отдельной кнопкой ("🗓 Выбрать в календаре..." → deadline_prompt_text).
    """
    return (
        f"⏳ <b>Когда нужно сделать «{escape(title)}»?</b>\n"
        "Выбери один из вариантов, либо открой календарь для точной даты 👇"
    )


def deadline_prompt_text(title: str) -> str:
    """Приглашение выбрать дедлайн в самом календаре (открывается кнопкой
    "🗓 Выбрать в календаре..." с экрана быстрых пресетов)."""
    return (
        f"📅 <b>Когда нужно закончить «{escape(title)}»?</b>\n"
        "Выбери дату в календаре ниже, либо оставь задачу без срока 👇"
    )


def time_prompt_text(title: str, day: int, month: int, year: int) -> str:
    """Приглашение выбрать время (шаг 2 — барабан времени, после выбора даты)."""
    return (
        f"🕐 <b>Во сколько напомнить про «{escape(title)}»?</b>\n"
        f"Дата: {day:02d}.{month:02d}.{year}"
    )


def reminders_prompt_text(title: str, deadline, all_day: bool = False) -> str:
    """Приглашение выбрать напоминания (шаг 3, мультивыбор чекбоксов)."""
    return (
        f"🔔 <b>Настрой напоминания для «{escape(title)}»</b>\n"
        f"<i>Дедлайн: {format_deadline(deadline, all_day)}</i>\n\n"
        "Можно выбрать сразу несколько — просто нажимай на нужные 👇"
    )


def deadline_saved_text(title: str, priority: Priority, deadline, all_day: bool = False) -> str:
    """Финальная карточка задачи после завершения всего мастера дедлайна
    (только для сценария создания новой задачи — CTX_NEW)."""
    base = (
        f"✨ <b>Записал!</b>\n"
        f"📌 «{escape(title)}»\n"
        f"<i>Приоритет: {PRIORITY_LABELS[priority]}</i>"
    )
    if deadline is None:
        return base + "\n⏳ <i>Срок: Без дедлайна (сделаем, когда будет настрой ✨)</i>"
    return base + f"\n⏳ <i>Срок: {format_deadline(deadline, all_day)}</i>"


# Тёплые реплики Томаса под пуш-уведомлением о напоминании — короткий
# толчок взяться за дело ПРЯМО СЕЙЧАС (в отличие от QUICK_CLOSE_PHRASES
# выше, которые звучат уже ПОСЛЕ закрытия задачи). Тот же принцип "без
# упрёков и давления" — это приглашение, а не будильник с претензией.
REMINDER_PUSH_PHRASES = [
    "Лапки в готовности — самое время взяться за дело ✨",
    "Маленький шаг сейчас — и с плеч долой 🐾",
    "Ты справишься быстрее, чем кажется. Погнали? ⚡️",
    "Всего пара минут — и это уже позади 🌿",
    "Хороший момент, чтобы закрыть один хвостик 🐾",
    "Фокус включён? Тогда вперёд, я рядом ✨",
    "Небольшое дело — а порядка в мыслях сразу больше 🎯",
    "Самое время — не будем откладывать в долгий ящик 🐾",
]


def random_reminder_push_phrase() -> str:
    """Случайная реплика Томаса для пуш-уведомления (см. REMINDER_PUSH_PHRASES)."""
    return random.choice(REMINDER_PUSH_PHRASES)


def reminder_second_chance_text(task) -> str:
    """
    Мягкий разовый повтор через 2 часа после обычного напоминания (см.
    services.scheduler._maybe_send_second_chance) — если задача так и
    осталась не закрыта. Специально БЕЗ тревожного "⏰"/"🚨" и без
    повторения слов "Пора заняться делом!" — это не второй будильник,
    а тихая заметка на полях, что дело просто не потерялось из виду.
    """
    return (
        "🐾 <b>Небольшое напоминание</b>\n"
        f"У тебя остался один хвостик с дневного напоминания:\n"
        f"📌 «{escape(task.title)}»\n\n"
        "<i>Без спешки — просто чтобы не потерялось из виду ✨</i>"
    )


def morning_checklist_text(tasks: list) -> str:
    """
    Утренний чек-лист — ежедневный пуш в 09:00 тем, у кого включён
    переключатель "☑️ Утренний чек-лист (09:00)" в 🔔 Уведомления (см.
    services.scheduler._send_morning_checklists). tasks — уже
    отфильтрованный список задач с дедлайном сегодня или раньше (см.
    database.requests.get_tasks_due_today_or_overdue) — переиспользует ту
    же строку-буллет, что и дашборд "📋 Мои задачи" (см. _list_bullet_line).
    """
    if not tasks:
        return "☀️ <b>Доброе утро!</b>\nНа сегодня горящих дел нет — можно выдохнуть 🐾"

    lines = ["☀️ <b>Доброе утро! Вот что горит сегодня:</b>", _DIVIDER]
    lines.extend(_list_bullet_line(task) for task in tasks)
    lines.append(_DIVIDER)
    lines.append("<i>Небольшими шагами — и всё получится 🐾</i>")
    return "\n".join(lines)


def notifications_entry_text() -> str:
    """Короткая подсказка под карточкой профиля, ведущая на экран 🔔 Уведомления."""
    return "🔔 Хочешь настроить уведомления — тихие часы, напоминания по задачам, утренний чек-лист?"


def notifications_settings_text() -> str:
    """
    Заголовок экрана "🔔 Уведомления" (Профиль и Настройки → 🔔 Уведомления,
    см. handlers/profile.py). Состояние каждого переключателя видно прямо
    на кнопках (☑️/◻️, см. keyboards.notification_settings_keyboard) —
    отдельно дублировать его в тексте не нужно.
    """
    return (
        "🔔 <b>Уведомления</b>\n"
        "Нажимай на пункт, чтобы включить или выключить 👇"
    )


def reminder_notification_text(task, offset: ReminderOffset) -> str:
    """
    Текст пуш-уведомления, когда наступает время напоминания — два разных
    по тону сообщения в зависимости от того, КАКОЕ именно из выбранных
    напоминаний сработало (offset самого сработавшего Reminder, см.
    services.scheduler._fire_reminder):
    - ReminderOffset.exact (сам момент дедлайна) — "🚨 Время пришло!",
      динамичный призыв к действию, с приоритетом задачи вместо дедлайна
      (дедлайн — это буквально "сейчас", повторять его незачем).
    - Любое заблаговременное смещение ("за N дней/часов/минут") —
      "⏳ Напоминание о плане", более мягкий, предупреждающий тон, с самим
      дедлайном (чтобы было понятно, сколько времени ещё есть в запасе).

    Кнопки-пульт под сообщением одни и те же для обоих типов (см.
    keyboards.reminder_notification_keyboard) — разница только в самом
    тексте, тыкать нужно ровно то же самое.
    """
    xp_amount = XP_BY_PRIORITY.get(task.priority, XP_PER_TASK)
    phrase = random_reminder_push_phrase()

    if offset == ReminderOffset.exact:
        lines = [
            "🚨 <b>Время пришло!</b>",
            f"📌 <b>{escape(task.title)}</b>",
            _DIVIDER,
            f"⚡️ <b>Приоритет:</b> {PRIORITY_LABELS[task.priority]}",
            f"⚡️ <b>Награда:</b> +{xp_amount} XP",
            "",
            f"🐾 <i>«{phrase}»</i>",
        ]
    else:
        urgency_marker = URGENCY_MARKERS[task_urgency_category(task)]
        lines = [
            "⏳ <b>Напоминание о плане</b>",
            f"📌 <b>{escape(task.title)}</b>",
            _DIVIDER,
            f"⏰ <b>Дедлайн:</b> {format_deadline(task.deadline, task.deadline_all_day)} {urgency_marker}",
            f"⚡️ <b>Награда:</b> +{xp_amount} XP",
            "",
            f"🐾 <i>«{phrase}»</i>",
        ]
    return "\n".join(lines)


def snooze_confirmed_text(new_time) -> str:
    """
    Подтверждение переноса напоминания (см. handlers/tasks.py::
    snz_quick/snz_tmr/td_confirm при CTX_SNOOZE) — твоя формулировка
    "Договорились, напомню в ..." вместо сухого "Напоминание перенесено на".
    """
    return f"💤 Договорились, напомню {format_deadline(new_time)} 🐾"


def card_deadline_text(task) -> str:
    """
    Текст дедлайна для карточки задачи. Для "Сегодня"/"Завтра" — короткая
    относительная формулировка ("Сегодня в 18:15"), она читается быстрее
    полной даты и именно так показана в примере оформления карточки. Для
    остальных дат (включая просроченные из прошлых дней) — обычный
    format_deadline с полной датой: то, что дедлайн уже прошёл, и так
    видно по маркеру срочности в конце строки (см. task_card_text), не
    нужно повторять это ещё и словом "просрочено" в самой дате.
    """
    deadline = task.deadline
    today = date.today()
    deadline_date = deadline.date()

    if deadline_date == today:
        day_part = "Сегодня"
    elif deadline_date == today + timedelta(days=1):
        day_part = "Завтра"
    else:
        return format_deadline(deadline, task.deadline_all_day)

    if task.deadline_all_day:
        return f"{day_part} (в течение дня)"
    return f"{day_part} в {deadline.strftime('%H:%M')}"


def task_card_text(task, reminders, shared_by_partner: bool = False) -> str:
    """
    Детальная карточка задачи (открывается кликом по задаче в списке
    "📋 Мои задачи") — оформлена как "посадочный талон": рамки-разделители
    сверху и снизу, плашки-поля с фиксированными иконками. reminders —
    список ещё не сработавших Reminder этой задачи (см.
    database.requests.get_task_reminders).

    shared_by_partner=True — это ОБЩАЯ задача партнёра, открытая через
    партнёрский режим (Premium, см. database.requests._authorized_task), а
    не своя — добавляем короткую плашку об этом, чтобы не путать её со
    своими задачами (полные права редактирования при этом остаются те же).
    """
    urgency_marker = URGENCY_MARKERS[task_urgency_category(task)]

    lines = [f"📌 <b>{escape(task.title)}</b>"]
    if shared_by_partner:
        lines.append("👥 <i>Общая задача партнёра</i>")
    lines += [
        _DIVIDER,
        f"⚡️ <b>Приоритет:</b> {PRIORITY_LABELS[task.priority]}",
    ]

    if task.deadline is None:
        lines.append(f"⏳ <b>Дедлайн:</b> без срока {urgency_marker}")
    else:
        lines.append(f"⏳ <b>Дедлайн:</b> {card_deadline_text(task)} {urgency_marker}")

    if reminders:
        sorted_reminders = sorted(reminders, key=lambda r: _REMINDER_ORDER[r.offset])
        labels = ", ".join(REMINDER_OFFSET_LABELS[r.offset] for r in sorted_reminders)
        lines.append(f"🔔 <b>Напоминания:</b> {labels}")
    else:
        lines.append("🔔 <b>Напоминания:</b> нет")

    lines.append(_DIVIDER)
    return "\n".join(lines)
