import os

from app.config.db import ConfigKeysEnum
from app.models.app import AppConfigModel
from app.repo.config_repo import ConfigRepo


def get_config(key: ConfigKeysEnum) -> str | None:
    config_repo = ConfigRepo()
    val: AppConfigModel = config_repo.get_first_by(where={"key": key.value})
    if val is None:
        return os.getenv(str(key.value), None)
    return val.value
