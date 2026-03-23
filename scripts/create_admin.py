from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.db import session_scope
from shared.user_admin import create_user, get_user_by_email


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the initial admin user.")
    parser.add_argument("--email", required=True)
    parser.add_argument("--display-name", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument(
        "--if-missing",
        action="store_true",
        help="Succeed without changes when the admin user already exists.",
    )
    args = parser.parse_args()
    normalized_email = args.email.lower()

    with session_scope() as db:
        existing_user = get_user_by_email(db, args.email)
        if existing_user is not None:
            if not args.if_missing:
                raise SystemExit(f"Admin user already exists: {normalized_email}")
            if existing_user.role != "admin":
                raise SystemExit(f"Existing user is not an admin: {normalized_email}")
            print(f"Admin user already exists: {normalized_email}")
            return
        create_user(
            db,
            email=args.email,
            display_name=args.display_name,
            password=args.password,
            role="admin",
        )
    print(f"Created admin user {normalized_email}")


if __name__ == "__main__":
    main()
