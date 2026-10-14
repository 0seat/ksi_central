"""SQLite 엔진/세션. 금액은 정밀도 보존을 위해 Decimal 을 문자열로 저장한다."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from sqlalchemy import String, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator


class DecimalText(TypeDecorator):
    impl = String(48)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, float):
            raise TypeError("DecimalText 에 float 저장 금지")
        return str(Decimal(value))

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)


class Base(DeclarativeBase):
    pass


class Database:
    def __init__(self, url: str) -> None:
        kwargs: dict = {}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
        self.engine: Engine = create_engine(url, **kwargs)
        event.listen(self.engine, "connect", _sqlite_pragmas)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    @classmethod
    def file(cls, path: Path) -> Database:
        path.parent.mkdir(parents=True, exist_ok=True)
        return cls(f"sqlite:///{path.as_posix()}")

    @classmethod
    def memory(cls) -> Database:
        return cls("sqlite://")

    def create_all(self) -> None:
        from central.storage import models  # noqa: F401  (테이블 등록)

        Base.metadata.create_all(self.engine)

    def migrate(self) -> None:
        """Alembic 으로 최신 스키마까지 올린다 (운영 경로). create_all 은 테스트 편의용."""
        from central.storage.migrate import upgrade

        upgrade(self.engine)

    def session(self) -> Session:
        return self._sessions()


def _sqlite_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()
