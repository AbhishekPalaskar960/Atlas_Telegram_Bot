from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base
from config import DATABASE_URL

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def get_session():
    """Call this wherever you need a DB session. Caller is responsible for closing it."""
    return SessionLocal()


def _add_column_if_missing(table: str, column_def: str) -> None:
    """Idempotent migration for older SQLite DBs (e.g. pre-existing assistant.db).

    SQLAlchemy 2.0 removed Engine.execute() — connections must be obtained
    explicitly via engine.connect(), and DDL/DML needs an explicit commit()
    since 2.0 connections are non-autocommit by default.
    """
    try:
        with engine.connect() as conn:
            existing = {
                row[1] for row in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
            }
    except Exception:
        return
    name = column_def.split()[0]
    if name not in existing:
        with engine.connect() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column_def}"))
            conn.commit()


def init_db():
    import models  # noqa: F401 (ensures models are registered before create_all)
    Base.metadata.create_all(bind=engine)
    _add_column_if_missing("users", "last_briefing_date VARCHAR(10)")
    _add_column_if_missing("users", "google_access_token TEXT")
    _add_column_if_missing("users", "google_refresh_token TEXT")
    _add_column_if_missing("users", "google_token_expiry VARCHAR")
    _add_column_if_missing("users", "oauth_state VARCHAR")
    _add_column_if_missing("alerts", "last_seen_id VARCHAR")
