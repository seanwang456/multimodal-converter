"""SQLite 引擎：WAL + busy_timeout（双进程 api/worker 安全）。"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import fcntl

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings

engine = create_engine(
    f"sqlite:///{settings.sqlite_path}",
    connect_args={"check_same_thread": False},
    echo=False,
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _record):  # noqa: ANN001
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


@contextmanager
def _sqlite_init_lock() -> Iterator[None]:
    """Serialize schema creation across API/worker processes."""
    lock_path = settings.sqlite_path.with_name(f"{settings.sqlite_path.name}.init.lock")
    with lock_path.open("a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def init_db() -> None:
    settings.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    # 确保模型已注册
    from app import models  # noqa: F401

    with _sqlite_init_lock():
        SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
