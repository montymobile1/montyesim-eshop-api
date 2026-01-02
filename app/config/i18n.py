import json
import os
from functools import lru_cache


class I18n:
    @staticmethod
    @lru_cache(maxsize=2)  # Cache for 2 languages (en, ar)
    def load_messages(lang: str = 'en') -> dict:
        """Load messages from i18n json files with caching."""
        root = os.path.abspath(os.curdir)
        root_path = os.getenv("LOCALES_DIR", None)
        if root_path:
            path = os.path.join(root_path, f"{lang}.json")
        else:
            path = os.path.join(root, "locales", f"{lang}.json")
        if not os.path.exists(path):
            path = os.path.join(root, "locales", "en.json")

        with open(path, "r", encoding='utf-8') as f:
            return json.load(f)

    @staticmethod
    def get_message(key: str, lang: str = 'en') -> str:
        """Get a specific message by key from the language file."""
        messages = I18n.load_messages(lang)
        return messages.get(key, key)  # Return the key itself if message not found
