import os
import json
from functools import lru_cache

class I18n:
    @staticmethod
    @lru_cache(maxsize=2)  # Cache for 2 languages (en, ar)
    def load_messages(lang: str = 'en') -> dict:
        """Load messages from i18n json files with caching."""
        ROOT_PATH = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
        path = f"{ROOT_PATH}/locales/{lang}.json"
        if not os.path.exists(path):
            path = f"{ROOT_PATH}/locales/en.json"  # Fallback to English
            
        with open(path, "r", encoding='utf-8') as f:
            return json.load(f)

    @staticmethod
    def get_message(key: str, lang: str = 'en') -> str:
        """Get a specific message by key from the language file."""
        messages = I18n.load_messages(lang)
        return messages.get(key, key)  # Return the key itself if message not found
