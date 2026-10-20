import os
from app.config.db import ConfigKeysEnum

# A setting whose name ends like this is a secret: it is read from the environment only (get_secret), never
# stored in app_config and never returned by /configurations.
SECRET_KEY_SUFFIXES = ("_KEY", "_SECRET", "_PASSWORD", "_TOKEN", "_USERNAME")
# Secrets whose name does not end like one.
SECRET_KEYS = frozenset({"RESELLER_ID"})
# Names that end like a secret but are not: cache version keys.
NON_SECRET_KEYS = frozenset({"APP_CACHE_KEY", "CACHE_KEY"})


class MissingSecretError(RuntimeError):
    """A secret is not set in the environment. The message names the key, never a value."""


def _key_name(key: ConfigKeysEnum | str) -> str:
    return key.value if isinstance(key, ConfigKeysEnum) else str(key)


def is_secret_key(key: ConfigKeysEnum | str) -> bool:
    name = _key_name(key).upper()
    if name in NON_SECRET_KEYS:
        return False
    return name in SECRET_KEYS or name.endswith(SECRET_KEY_SUFFIXES)


def get_secret(key: str) -> str:
    """A secret, from the environment only. Never written to app_config, never logged."""
    value = os.getenv(key)
    if not value:
        raise MissingSecretError(f"{key} is not set in the environment")
    return value


def get_setting(key: ConfigKeysEnum | str, default_value: str | int | float | None = None) -> str | None:
    """
    A non-secret setting from app_config. A setting missing from app_config is read from the environment (or
    default_value) and stored in app_config; concurrent first reads store one row.
    """
    from app.models.app import AppConfigModel
    from app.repo.config_repo import ConfigRepo
    key = _key_name(key)
    if is_secret_key(key):
        raise ValueError(f"{key} is a secret: read it with get_secret")
    config_repo = ConfigRepo()
    val: AppConfigModel = config_repo.get_first_by(where={"key": key})
    if val is None:
        os_val = os.getenv(str(key), default_value)
        if os_val:
            # app_config.key is unique: a concurrent first read already stored it, keep that row
            config_repo.insert_if_absent(key=key, value=os_val)
        return os_val
    return val.value


get_config = get_setting
