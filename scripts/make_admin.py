"""
Promote an existing account to admin, so it can manage inventory
(POST /inventory). There is deliberately no API endpoint or UI for this --
letting any authenticated user grant themselves admin rights would defeat
the point of the restriction. Granting admin is an operator action, done
directly against the database, the same way a real ops/support team would
flip an internal flag rather than exposing it over the public API.

Usage (run from the project root, with the same DATABASE_URL / venv the
app uses):

    python -m scripts.make_admin you@example.com

The account must already exist (sign up through the app first).
"""

import sys

# Ensure `app` is importable when this is run as a script rather than
# `python -m scripts.make_admin`.
sys.path.insert(0, ".")

from app.database import SessionLocal  # noqa: E402
from app import models  # noqa: E402


def make_admin(email: str) -> None:
    db = SessionLocal()
    try:
        customer = db.query(models.Customer).filter(models.Customer.email == email).first()
        if not customer:
            print(f"No account found for {email!r}. Sign up first, then re-run this.")
            return
        if customer.is_admin:
            print(f"{email} is already an admin.")
            return
        customer.is_admin = True
        db.commit()
        print(f"{email} is now an admin and can manage inventory.")
    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python -m scripts.make_admin <email>")
        sys.exit(1)
    make_admin(sys.argv[1])
