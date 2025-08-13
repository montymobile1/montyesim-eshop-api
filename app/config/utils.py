import os

from app.config.db import ConfigKeysEnum
from app.models.app import AppConfigModel
from app.repo.config_repo import ConfigRepo


def get_config(key: ConfigKeysEnum | str, default_value: str = None) -> str | None:
    config_repo = ConfigRepo()
    val: AppConfigModel = config_repo.get_first_by(where={"key": key.value})
    if val is None:
        os_val = os.getenv(str(key.value), default_value)
        if os_val:
            config_repo.create({"key": key.value, "value": os_val})
        return os_val
    return val.value
