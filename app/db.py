from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from app.config import DATABASE_URL

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


def init_db():
    # Import models so they're registered on Base before create_all
    from app import models  # noqa: F401

    # Safety net for a genuinely fresh install / the test suite (which
    # calls this directly against a throwaway DB) — creates any missing
    # table from scratch. It does NOT alter existing tables, though, which
    # is exactly why this alone caused several "no such column" incidents
    # in production before Alembic was introduced. For the real deployed
    # database, schema changes now go through Alembic migrations
    # (migrations/versions/, applied automatically on container start via
    # the Dockerfile's CMD) — see README.md.
    Base.metadata.create_all(bind=engine)
