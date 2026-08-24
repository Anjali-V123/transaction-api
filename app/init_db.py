"""
One-shot schema initialization, meant to run once before any API replica
starts -- not from inside the app on every boot.

Why this exists: SQLAlchemy's Base.metadata.create_all() is not safe to
call from multiple processes racing against a fresh Postgres database. On
Postgres, creating a native ENUM type is a genuine race: two processes can
both check "does this type exist yet?", both see no, and both issue
CREATE TYPE -- one wins, the other crashes with a UniqueViolation on
pg_type. This is exactly what happened when api1 and api2 were both
started at once against an empty database while building this project.

The fix is the standard one: separate "create the schema" from "run the
app" so schema setup happens exactly once, before any replica boots.
docker-compose.yml runs this as a one-shot `migrate` service that api1 and
api2 both depend on (service_completed_successfully) before they start.
"""

from . import models  # noqa: F401  (import so metadata is populated)
from .database import Base, engine

if __name__ == "__main__":
    Base.metadata.create_all(bind=engine)
    print("Database schema initialized.")
