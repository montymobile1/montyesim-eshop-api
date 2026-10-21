"""One-off: delete secret rows from app_config.

The app reads secrets from the environment only (app.config.helper.get_secret), so a secret row in app_config is
no longer used, and app_config is readable from more places than the server. A row is a secret by the same rule
as get_secret (app.config.helper.is_secret_key).

Run AFTER deploying the version that reads secrets from the environment. For each secret row the script checks
the environment it runs in (point it at the same values as the deployed app) and warns loudly when the variable
is missing or holds a different value than the row: the app now uses the environment value. Only key names are
printed, never values.

Usage (dry run is the default and changes nothing):
    venv/bin/python scripts/remove_secrets_from_app_config.py
    venv/bin/python scripts/remove_secrets_from_app_config.py --apply

Reads SUPABASE_URL and SUPABASE_KEY (service role) from the environment or .env.
"""

import argparse
import hmac
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, List, Mapping, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config.helper import is_secret_key  # noqa: E402

TABLE = "app_config"
ENV_MATCHES, ENV_DIFFERS, ENV_MISSING = "env matches", "ENV DIFFERS", "ENV MISSING"


@dataclass
class Result:
    removed: List[str] = field(default_factory=list)
    kept: int = 0
    failed: List[str] = field(default_factory=list)
    env_problems: List[str] = field(default_factory=list)


def env_status(key: str, value: Any, environ: Mapping[str, str]) -> str:
    env_value = environ.get(key)
    if not env_value:
        return ENV_MISSING
    return ENV_MATCHES if hmac.compare_digest(str(env_value), str(value or "")) else ENV_DIFFERS


def remove_secret_rows(table: Any, rows: Iterable[Mapping[str, Any]], apply: bool,
                       environ: Mapping[str, str] = os.environ,
                       log: Callable[[str], None] = print) -> Result:
    result = Result()
    for row in rows:
        key = row["key"]
        if not is_secret_key(key):
            result.kept += 1
            continue
        status = env_status(key, row.get("value"), environ)
        if status != ENV_MATCHES:
            result.env_problems.append(key)
            log(f"!!! WARNING: {key}: {status} in this environment. The app reads {key} from the environment "
                f"only: set it on the deployed app before relying on it.")
        if not apply:
            log(f"[dry-run] would remove {key} ({status})")
            result.removed.append(key)
            continue
        try:
            table.delete().eq("key", key).execute()
            log(f"removed {key} ({status})")
            result.removed.append(key)
        except Exception as e:
            result.failed.append(key)
            log(f"failed to remove {key}: {type(e).__name__}")
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Delete secret rows from app_config.")
    parser.add_argument("--apply", action="store_true", help="delete the rows (default is a dry run)")
    args = parser.parse_args(argv)

    from dotenv import load_dotenv
    from supabase import create_client
    from supabase.lib.client_options import SyncClientOptions

    load_dotenv()
    supabase_url, supabase_key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
    if not supabase_url or not supabase_key:
        print("SUPABASE_URL and SUPABASE_KEY (service role) must be set")
        return 2

    client = create_client(supabase_url, supabase_key, options=SyncClientOptions(auto_refresh_token=False))
    rows = client.table(TABLE).select("key, value").execute().data or []
    print(f"{'APPLY' if args.apply else 'DRY RUN'}: {len(rows)} app_config rows")
    result = remove_secret_rows(client.table(TABLE), rows, apply=args.apply)
    prefix = "" if args.apply else "would_"
    print(f"{prefix}removed={len(result.removed)} kept={result.kept} failed={len(result.failed)} "
          f"env_problems={len(result.env_problems)}")
    if result.removed:
        print(f"{prefix}removed keys: {', '.join(result.removed)}")
    if result.env_problems:
        print(f"!!! check these on the deployed app's environment: {', '.join(result.env_problems)}")
    if result.failed:
        print("Re-run the same command to retry; removed rows are gone and not counted again.")
    return 1 if result.failed else 0


if __name__ == "__main__":
    sys.exit(main())
