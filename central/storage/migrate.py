"""DB 스키마 마이그레이션 (Alembic).

    python -m central db upgrade            운영 DB 를 최신 스키마로
    python -m central db revision -m "..."  모델 변경 후 새 리비전 생성 (개발용)

M2/M3 에서 create_all 로 만든 DB(alembic_version 없음)는 초기 리비전으로 표시한 뒤 이어서 올린다.
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
BASELINE = "0001_initial"
logging.getLogger("alembic").setLevel(logging.WARNING)  # 시작할 때마다 나오는 플러그인 INFO 로그 억제


def _config(engine: Engine) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


def upgrade(engine: Engine, revision: str = "head") -> None:
    cfg = _config(engine)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        tables = set(inspect(conn).get_table_names())
        if "alembic_version" not in tables and "sub_accounts" in tables:
            command.stamp(cfg, BASELINE)
        command.upgrade(cfg, revision)


def head() -> str:
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return ScriptDirectory.from_config(cfg).get_current_head()  # type: ignore[return-value]


def current(engine: Engine) -> str | None:
    with engine.connect() as conn:
        if "alembic_version" not in inspect(conn).get_table_names():
            return None
        return conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()


def revision(engine: Engine, message: str, rev_id: str | None = None) -> None:
    cfg = _config(engine)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.revision(cfg, message=message, autogenerate=True, rev_id=rev_id)
