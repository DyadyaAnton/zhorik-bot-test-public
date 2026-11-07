"""Загрузка конфигурации из переменных окружения Amvera."""
import os
from dataclasses import dataclass
from pathlib import Path


def _int_or_none(value: str):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    sandbox_owner_id_raw: str = os.getenv("SANDBOX_OWNER_ID", "").strip()
    yandex_api_key: str = os.getenv("YANDEX_API_KEY", "").strip()
    yandex_folder_id: str = os.getenv("YANDEX_FOLDER_ID", "").strip()
    data_dir: Path = Path(os.getenv("DATA_DIR", "/data"))
    db_path: Path = Path(os.getenv("DB_PATH", os.getenv("DATA_DIR", "/data") + "/zhorik.sqlite3"))
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    @property
    def sandbox_owner_id(self):
        return _int_or_none(self.sandbox_owner_id_raw)

    def validate(self) -> None:
        if not self.telegram_bot_token:
            raise RuntimeError("Не задан обязательный секрет TELEGRAM_BOT_TOKEN.")
        if self.sandbox_owner_id is None or self.sandbox_owner_id <= 0:
            raise RuntimeError("Не задан корректный SANDBOX_OWNER_ID. Закрытая песочница не запущена.")


settings = Settings()
