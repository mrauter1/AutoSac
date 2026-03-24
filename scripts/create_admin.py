from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.db import session_scope
from shared.contracts import WORKSPACE_BOOTSTRAP_VERSION
from shared.ticketing import ensure_system_state_defaults
from shared.user_admin import ensure_admin_user


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the initial admin user.")
    parser.add_argument("--email", required=True)
    parser.add_argument("--display-name", required=True)
    parser.add_argument("--password", required=True)
    args = parser.parse_args()

    with session_scope() as db:
        ensure_system_state_defaults(db, WORKSPACE_BOOTSTRAP_VERSION)
        user, outcome = ensure_admin_user(
            db,
            email=args.email,
            display_name=args.display_name,
            password=args.password,
        )
    if outcome == "created":
        print(f"Created admin user {user.email}")
        return
    print(f"Admin user {user.email} already matched the requested bootstrap state")


if __name__ == "__main__":
    main()
