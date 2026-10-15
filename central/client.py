"""SDK 는 sdk/central_client.py (서버 없이 따로 설치: pip install E:/.../sdk). 기존 import 경로 호환용."""

from __future__ import annotations

try:
    from central_client import *  # noqa: F403
    from central_client import API_PREFIX, CentralClient, CentralError  # noqa: F401
except ImportError:   # sdk 를 설치하지 않은 저장소 실행
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sdk"))
    from central_client import *  # noqa: F403
    from central_client import API_PREFIX, CentralClient, CentralError  # noqa: F401
