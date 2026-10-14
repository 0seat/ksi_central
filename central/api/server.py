"""FastAPI 앱 조립과 실행 (docs/PLAN.md 8장).

    python -m central serve

uvicorn 은 설정의 server.hosts (127.0.0.1, Tailscale IP) 각각에 소켓을 열고 하나의 앱으로 서비스한다.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError

from central.api import admin, market, me, ws
from central.api.common import error_response, ok
from central.app import CentralApp
from central.oms.errors import OmsError

log = logging.getLogger(__name__)
API_PREFIX = "/api/v1"


def create_app(central: CentralApp, *, manage_lifecycle: bool = True, bootstrap: bool = True) -> FastAPI:
    """manage_lifecycle: 앱 시작/종료 때 브로커 연결을 열고 닫는다 (테스트에서 직접 관리하면 False)."""

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if manage_lifecycle:
            await central.start()
            if bootstrap:
                for alias in central.profiles:
                    try:
                        summary = await central.bootstrap_if_empty(alias)
                        if summary is not None:
                            log.info("[%s] 초기 동기화 완료: %s", alias, summary)
                    except Exception:
                        log.exception("[%s] 초기 동기화 실패 (관리자 API 로 다시 실행 가능)", alias)
        try:
            yield
        finally:
            if manage_lifecycle:
                await central.stop()

    api = FastAPI(
        title="중앙증권", version="0.4.0", lifespan=lifespan,
        docs_url=f"{API_PREFIX}/docs", redoc_url=None, openapi_url=f"{API_PREFIX}/openapi.json",
    )
    api.state.central = central
    api.state.hub = ws.EventHub()
    central.bus.subscribe(api.state.hub.on_event)
    central.market.listeners.append(api.state.hub.on_market)

    @api.exception_handler(OmsError)
    async def _oms_error(_: Request, e: OmsError):
        return error_response(e.code, e.message)

    @api.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, e: RequestValidationError):
        # 입력값을 그대로 돌려주지 않는다 (키 등이 섞여 들어올 수 있음)
        detail = "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors())
        return error_response("INVALID_REQUEST", detail, 400)

    @api.exception_handler(Exception)
    async def _unexpected(_: Request, e: Exception):
        log.exception("API 처리 중 예외")
        return error_response("INTERNAL", type(e).__name__, 500)

    @api.get(f"{API_PREFIX}/health", tags=["system"])
    def health():
        return ok({"status": "up", "ws_connections": api.state.hub.connections,
                   "market_channels": len(central.market.stats()["channels"])})

    for router in (admin.router, me.router, market.router, ws.router):
        api.include_router(router, prefix=API_PREFIX)
    return api


def _sockets(hosts: list[str], port: int) -> list[socket.socket]:
    socks = []
    for host in hosts:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        s = socket.socket(family, socket.SOCK_STREAM)
        if sys.platform == "win32":
            # Windows 의 SO_REUSEADDR 는 다른 프로세스가 같은 포트를 가로챌 수 있게 하므로 배타 바인딩
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        socks.append(s)
    return socks


def serve(central: CentralApp, stop_file: Path | None = None) -> None:
    """stop_file: 이 파일이 생기면 정상 종료한다 (감시자가 보내는 종료 요청. Windows 에서 콘솔 없는
    프로세스에 Ctrl+C 를 보낼 수 없어서 파일로 알린다)."""
    import uvicorn

    cfg = central.settings.server
    api = create_app(central)
    config = uvicorn.Config(api, log_config=None, ws="websockets-sansio", lifespan="on")
    server = uvicorn.Server(config)
    socks = _sockets(cfg.hosts, cfg.port)
    log.info("중앙증권 API 시작: %s 포트 %d", cfg.hosts, cfg.port)

    async def main() -> None:
        watcher = asyncio.create_task(_watch_stop_file(server, stop_file)) if stop_file else None
        try:
            await server.serve(sockets=socks)
        finally:
            if watcher:
                watcher.cancel()

    if stop_file:
        stop_file.unlink(missing_ok=True)
    asyncio.run(main())


async def _watch_stop_file(server, path: Path) -> None:
    while not server.should_exit:
        if path.exists():
            log.info("종료 요청 파일 감지: 정상 종료")
            path.unlink(missing_ok=True)
            server.should_exit = True
            return
        await asyncio.sleep(1)
