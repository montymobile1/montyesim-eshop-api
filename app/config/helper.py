import os

from app.config.db import ConfigKeysEnum


def get_config(key: ConfigKeysEnum | str, default_value: str | int | float | None = None) -> str | None:
    from app.models.app_config import AppConfigModel
    from app.repo.config_repo import ConfigRepo
    config_repo = ConfigRepo()
    key = key.value if isinstance(key, ConfigKeysEnum) else key
    val: AppConfigModel = config_repo.sget_first_by(where={"key": key})
    if val is None:
        os_val = os.getenv(str(key), default_value)
        if os_val:
            config_repo.screate({"key": key, "value": os_val})
        return os_val
    return val.value
