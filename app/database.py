import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# Defaults to a local SQLite file for fast local development / testing.
# In production (Render/Railway), set DATABASE_URL to a Postgres connection
# string, e.g. postgresql://user:pass@host:5432/dbname
# This 12-factor-style config swap is intentional: same code, different DB
# depending on environment.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./transactions.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
