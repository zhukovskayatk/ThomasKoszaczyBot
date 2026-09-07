"""
Конфигурация проекта.

Настройки (токен бота, ключ ИИ) читаются из файла .env, чтобы секретные
данные не попадали в код и в git-репозиторий.

Как это работает:
1. pydantic-settings при импорте автоматически ищет файл ".env"
   в текущей рабочей директории.
2. Поле bot_token заполняется из переменной окружения BOT_TOKEN.
3. Если .env отсутствует или BOT_TOKEN не задан — приложение
   упадёт с понятной ошибкой валидации при старте, а не где-то
   в середине работы бота.
4. Поле deepseek_api_key — необязательное (по умолчанию None): без него
   бот просто не пытается распознавать дату/приоритет из свободного текста
   (см. services/ai_parser.py) и работает как раньше, без ИИ.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Токен Telegram-бота, выданный @BotFather
    bot_token: str

    # Ключ DeepSeek API для распознавания задач из свободного текста
    # (см. services/ai_parser.py). Необязателен — если не задан, эта
    # функция просто тихо выключена.
    deepseek_api_key: str | None = None

    # Настройки поиска .env файла (регистр переменных не важен:
    # BOT_TOKEN, bot_token — будет найдено одинаково)
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )


# Единый экземпляр настроек, который импортируется в других модулях:
# from config import settings
settings = Settings()
