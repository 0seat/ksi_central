"""Alembic 환경. central.storage.migrate 가 연결을 넘겨 실행한다."""

from __future__ import annotations

from alembic import context

from central.storage import models  # noqa: F401  (테이블 등록)
from central.storage.db import Base

config = context.config
target_metadata = Base.metadata


def _run(connection) -> None:
    # SQLite 는 ALTER 가 제한적이라 batch 모드로 생성한다
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


connection = config.attributes.get("connection")
if connection is None:
    raise RuntimeError("central.storage.migrate 를 통해 실행할 것 (python -m central db upgrade)")
_run(connection)
