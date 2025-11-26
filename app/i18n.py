import contextvars
import json
import os
from functools import lru_cache
from typing import Dict, Optional

# Context variable holding current request language (two-letter code)
_current_lang: contextvars.ContextVar[str] = contextvars.ContextVar("current_lang", default="en")


@lru_cache(maxsize=16)
def _load_locale_file(lang: str) -> Dict[str, str]:
    """Load a locale JSON file from the project's locales/ directory and cache it.

    Falls back to English if the file doesn't exist or can't be parsed.
    """
    root = os.path.abspath(os.curdir)
    root_path = os.getenv("LOCALES_DIR", None)
    if root_path:
        path = os.path.join(root_path, f"{lang}.json")
    else:
        path = os.path.join(root, "locales", f"{lang}.json")
    if not os.path.exists(path):
        path = os.path.join(root, "locales", "en.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # return empty mapping to avoid crashing callers
        return {}


def set_locale(lang: Optional[str]):
    """Set the current locale for the running context (request).

    Accepts language codes like 'en' or 'ar' or 'en-US' (only first part used).
    """
    if not lang:
        lang = "en"
    lang = lang.split("-")[0].lower()
    _current_lang.set(lang)


def get_locale() -> str:
    """Get current locale from context (default 'en')."""
    return _current_lang.get()


def translate(key: str, lang: Optional[str] = None, default: Optional[str] = None) -> str:
    """Return translated message for key in the specified or current language.

    Usage:
      from app.i18n import translate
      msg = translate('BUNDLE_ACTIVITY_POLICY')

    This function never raises; it returns `default` or the key when no translation found.
    """
    if lang:
        lang = lang.split("-")[0].lower()
    else:
        lang = get_locale()

    messages = _load_locale_file(lang)
    if not messages:
        messages = _load_locale_file("en")
    return messages.get(key, default if default is not None else key)
