import os
from pathlib import Path
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

BACKEND_ROOT = Path(__file__).resolve().parents[1]
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///" + str(BACKEND_ROOT / "credit.db"))
SQLITE = DATABASE_URL.startswith("sqlite")
engine = create_engine(
    DATABASE_URL, connect_args={"check_same_thread": False, "timeout": 30} if SQLITE else {}, pool_pre_ping=True, hide_parameters=True
)
if SQLITE:

    @event.listens_for(engine, "connect")
    def configure_sqlite(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass
