"""Case 16: no get_config / get_setting call in app/ reads a secret-shaped key (those go through get_secret)."""

import ast
from pathlib import Path

from app.config.db import ConfigKeysEnum
from app.config.helper import is_secret_key

APP_DIR = Path(__file__).resolve().parents[2] / "app"
DB_BACKED_READERS = {"get_config", "get_setting"}


def key_of(node):
    """The key a call reads: a string literal or a ConfigKeysEnum member; None when computed at runtime."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "ConfigKeysEnum":
        return ConfigKeysEnum[node.attr].value
    return None


def db_backed_reads():
    for path in sorted(APP_DIR.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in DB_BACKED_READERS and node.args:
                yield f"{path.relative_to(APP_DIR.parent)}:{node.lineno}", key_of(node.args[0])


def test_no_db_backed_read_of_a_secret_key():
    reads = list(db_backed_reads())
    assert len(reads) > 50  # the scan finds the call sites
    offenders = [f"{where} {key}" for where, key in reads if key and is_secret_key(key)]
    assert offenders == []


def test_every_db_backed_read_names_its_key():
    assert [where for where, key in db_backed_reads() if key is None] == []
