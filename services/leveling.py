"""
Логика уровней и опыта (XP) — геймификация.

Важное архитектурное решение: уровень пользователя НЕ хранится в базе
отдельным полем, а всегда вычисляется на лету из xp_points. Так не может
возникнуть рассинхрон (например, если позже поменяем пороги уровней —
не придётся писать миграцию данных, все уровни пересчитаются сами).
"""

from dataclasses import dataclass

from database.models import Priority

# XP по умолчанию (используется как подстраховка, если почему-то не
# удалось определить приоритет задачи)
XP_PER_TASK = 10

# Сколько XP начисляется за выполненную задачу — зависит от приоритета.
# Логика простая: выше приоритет — больше награда, иначе какой смысл
# вообще выставлять приоритет.
XP_BY_PRIORITY = {
    Priority.low: 5,
    Priority.medium: 10,
    Priority.high: 15,
    # "Без приоритета" — та же награда, что и раньше была подстраховкой по
    # умолчанию (XP_PER_TASK) для любого приоритета, которого нет в этом
    # словаре. Прописано явно, а не оставлено на волю .get(..., XP_PER_TASK)
    # по всему проекту — так сразу видно, что это осознанное решение, а не
    # забытый случай.
    Priority.none: XP_PER_TASK,
}

# Пороги уровней: (минимальный XP для уровня, номер уровня, звание, эмодзи).
# Специально отсортировано по УБЫВАНИЮ порога — так удобнее искать
# подходящий уровень (см. get_level_info).
LEVELS = [
    (300, 4, "Гигачад продуктивности", "🗿"),
    (150, 3, "Магистр закрытых гештальтов", "🧙‍♂️"),
    (50, 2, "Укротитель одного носка", "🧦"),
    (0, 1, "Адепт диванного царства", "🛋"),
]

# Длина прогресс-бара в символах (сколько блоков "█"/"░" всего)
BAR_LENGTH = 10


@dataclass
class LevelInfo:
    level: int
    title: str
    emoji: str
    next_threshold: int | None  # None — уже достигнут максимальный уровень


def get_level_info(xp: int) -> LevelInfo:
    """
    Возвращает уровень, звание, эмодзи и порог XP следующего уровня
    для указанного количества очков опыта.

    Пороги в LEVELS идут по убыванию, поэтому просто ищем первый порог,
    которому текущий xp уже соответствует ("сверху вниз").
    """
    for i, (threshold, level, title, emoji) in enumerate(LEVELS):
        if xp >= threshold:
            # LEVELS отсортирован по убыванию, поэтому следующий уровень
            # "вверх" — это предыдущий элемент списка (с индексом i - 1).
            next_threshold = LEVELS[i - 1][0] if i > 0 else None
            return LevelInfo(level=level, title=title, emoji=emoji, next_threshold=next_threshold)

    # Сюда мы никогда не должны попасть, так как самый нижний порог в
    # LEVELS равен 0, а xp не бывает отрицательным — но на всякий случай
    # подстрахуемся и вернём первый уровень.
    lowest_threshold, lowest_level, lowest_title, lowest_emoji = LEVELS[-1]
    next_threshold = LEVELS[-2][0]
    return LevelInfo(level=lowest_level, title=lowest_title, emoji=lowest_emoji, next_threshold=next_threshold)


def build_progress_bar(xp: int, next_threshold: int | None) -> str:
    """
    Графическая шкала прогресса вида "▰▰▰▰▰▱▱▱▱▱ 345 / 500 XP" —
    моноширинные квадраты-сегменты вместо текстового "[████░░░░░░]"
    (см. texts.profile_text, там бар оборачивается в <code>...</code>
    для ровного выравнивания). Если next_threshold is None — пользователь
    уже на максимальном уровне, показываем полностью заполненную шкалу
    без "цели".
    """
    if next_threshold is None:
        return f"{'▰' * BAR_LENGTH} {xp} XP (максимум)"

    filled = min(BAR_LENGTH, int(BAR_LENGTH * xp / next_threshold))
    empty = BAR_LENGTH - filled
    return f"{'▰' * filled}{'▱' * empty} {xp} / {next_threshold} XP"
