from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
SessionLocal = sessionmaker(autoflush=False, expire_on_commit=False)


def init_engine(db_path: Path | str) -> Engine:
    """Crea (o ricrea) l'engine SQLite e le tabelle."""
    global _engine
    url = "sqlite://" if str(db_path) == ":memory:" else f"sqlite:///{db_path}"
    kwargs = {"connect_args": {"check_same_thread": False}}
    if str(db_path) == ":memory:":
        from sqlalchemy.pool import StaticPool

        kwargs["poolclass"] = StaticPool
    _engine = create_engine(url, **kwargs)

    @event.listens_for(_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    SessionLocal.configure(bind=_engine)
    from app import models  # noqa: F401  (registra i modelli)

    Base.metadata.create_all(_engine)
    return _engine


def get_engine() -> Engine:
    assert _engine is not None, "init_engine() non chiamato"
    return _engine


def get_session() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session
