"""웹 대시보드 (/ui). 화면은 위젯 단위이고, 위젯 배치(레이아웃)는 로그인한 사용자별로 저장한다.

- 로그인: 관리자 키 또는 서브계좌 API 키 → 세션 쿠키 (키는 URL 이나 브라우저 저장소에 남기지 않는다)
- 관리자는 모든 위젯과 모든 서브계좌, 서브계좌 키는 허용된 위젯에서 자기 데이터만 본다
- 조회 전용. 상태를 바꾸는 기능은 없다
- 계좌번호와 키는 내보내지 않는다 (실계좌는 별칭 paper/real 로만 표시)
"""

from __future__ import annotations

import asyncio
import hmac
import inspect
import json
import secrets
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from central.api.common import ApiError, ok
from central.app import CentralApp
from central.broker.base import BrokerUnavailable
from central.core import logbuffer
from central.core.market_hours import KST, NEW_YORK
from central.core.money import ZERO, q_cash
from central.marketdata.candles import MINUTE_INTERVALS
from central.oms import queries
from central.oms.accounts import SubAccount
from central.oms.errors import OmsError
from central.ops.supervisor import status_path
from central.storage.models import EventRow, FillRow, JobRunRow, OrderRow, UiLayoutRow

STATIC_DIR = Path(__file__).resolve().parent / "ui_static"
COOKIE = "cs_ui"
SESSION_TTL_S = 12 * 3600
MAX_SESSIONS = 200

router = APIRouter(prefix="/ui", include_in_schema=False)


# ───────────── 세션 ─────────────
@dataclass(frozen=True)
class Viewer:
    role: Literal["admin", "sub"]
    sub_id: int | None
    name: str

    @property
    def owner(self) -> str:
        return "admin" if self.role == "admin" else f"sub:{self.sub_id}"


class Sessions:
    def __init__(self) -> None:
        self._items: dict[str, tuple[Viewer, float]] = {}

    def create(self, viewer: Viewer) -> str:
        now = time.time()
        self._items = {t: v for t, v in self._items.items() if v[1] > now}
        while len(self._items) >= MAX_SESSIONS:
            self._items.pop(next(iter(self._items)))
        token = secrets.token_urlsafe(32)
        self._items[token] = (viewer, now + SESSION_TTL_S)
        return token

    def get(self, token: str | None) -> Viewer | None:
        if not token or token not in self._items:
            return None
        viewer, expires = self._items[token]
        if expires < time.time():
            self._items.pop(token, None)
            return None
        return viewer

    def drop(self, token: str | None) -> None:
        if token:
            self._items.pop(token, None)

    def drop_sub(self, sub_id: int) -> None:
        self._items = {t: v for t, v in self._items.items() if v[0].sub_id != sub_id}


def _viewer(request: Request) -> Viewer:
    viewer = request.app.state.ui_sessions.get(request.cookies.get(COOKIE))
    if viewer is None:
        raise ApiError("UNAUTHORIZED", "로그인이 필요함")
    if viewer.role == "sub":   # 키 폐기나 서브계좌 정지 뒤에도 남은 세션으로 보지 않게
        app: CentralApp = request.app.state.central
        sub = app.accounts.get(viewer.sub_id)  # type: ignore[arg-type]
        if sub.status == "closed":
            request.app.state.ui_sessions.drop_sub(viewer.sub_id)
            raise ApiError("UNAUTHORIZED", "서브계좌가 닫힘")
    return viewer


def mount(api: FastAPI) -> None:
    api.state.ui_sessions = Sessions()
    logbuffer.install()
    api.include_router(router)
    api.mount("/ui/static", StaticFiles(directory=STATIC_DIR), name="ui-static")


# ───────────── 페이지와 로그인 ─────────────
@router.get("")
def ui_root():
    return RedirectResponse("/ui/")


@router.get("/")
def ui_index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


class LoginBody(BaseModel):
    key: str = Field(min_length=1, max_length=200)


@router.post("/api/login")
async def login(request: Request, response: Response, body: LoginBody):
    app: CentralApp = request.app.state.central
    viewer: Viewer | None = None
    admin_key = app.settings.admin_key
    if admin_key is not None and hmac.compare_digest(body.key.encode(), admin_key.get_secret_value().encode()):
        viewer = Viewer("admin", None, "관리자")
    else:
        sub = app.apikeys.authenticate(body.key)
        if sub is not None:
            viewer = Viewer("sub", sub.id, sub.name)
    if viewer is None:
        await asyncio.sleep(1.0)   # 키 대입 속도를 늦춘다
        raise ApiError("UNAUTHORIZED", "키가 올바르지 않음")
    token = request.app.state.ui_sessions.create(viewer)
    response.set_cookie(COOKIE, token, max_age=SESSION_TTL_S, httponly=True, samesite="strict", path="/ui")
    return ok({"role": viewer.role, "name": viewer.name})


@router.post("/api/logout")
def logout(request: Request, response: Response):
    request.app.state.ui_sessions.drop(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path="/ui")
    return ok()


@router.get("/api/me")
def whoami(request: Request):
    viewer = _viewer(request)
    app: CentralApp = request.app.state.central
    subs = [{"id": s.id, "name": s.name, "broker": s.broker_account_id, "type": s.type, "status": s.status}
            for s in _subs(app, viewer, include_system=True)]
    return ok({"role": viewer.role, "name": viewer.name, "subs": subs,
               "widgets": [{"type": t, **{k: v for k, v in w.items() if k != "fn"}}
                           for t, w in WIDGETS.items() if viewer.role in w["roles"]]})


# ───────────── 레이아웃 ─────────────
class WidgetSpec(BaseModel):
    model_config = {"extra": "forbid"}
    type: str
    w: int = Field(default=12, ge=1, le=12)
    title: str | None = Field(default=None, max_length=60)
    params: dict[str, str] = Field(default_factory=dict)


class PageSpec(BaseModel):
    model_config = {"extra": "forbid"}
    id: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=40)
    # 페이지 상단 선택 칸. 고른 값은 이 페이지 모든 위젯의 params 로 들어간다 (위젯 params 보다 우선)
    vars: list[Literal["sub", "date", "symbol"]] = Field(default_factory=list)
    widgets: list[WidgetSpec] = Field(max_length=30)


class LayoutBody(BaseModel):
    model_config = {"extra": "forbid"}
    pages: list[PageSpec] = Field(min_length=1, max_length=20)


def _default_layout(viewer: Viewer) -> dict:
    pages = [
        {"id": "overview", "title": "개요", "widgets": [
            {"type": "status", "w": 12}, {"type": "accounts", "w": 12},
            {"type": "pnl_curve", "w": 7}, {"type": "positions", "w": 5}]},
        {"id": "blotter", "title": "블로터", "widgets": [{"type": "blotter", "w": 12, "params": {"limit": "300"}}]},
        {"id": "pnl", "title": "손익", "widgets": [
            {"type": "pnl_daily", "w": 12, "params": {"days": "30"}},
            {"type": "symbols", "w": 12, "params": {"days": "30"}}, {"type": "positions", "w": 12}]},
        {"id": "app", "title": "앱", "vars": ["sub", "date", "symbol"] if viewer.role == "admin" else ["date", "symbol"],
         "widgets": [
             {"type": "accounts", "w": 12}, {"type": "chart", "w": 8}, {"type": "symbols", "w": 4},
             {"type": "pnl_curve", "w": 6}, {"type": "positions", "w": 6}, {"type": "blotter", "w": 12}]},
    ]
    if viewer.role == "admin":
        pages.append({"id": "system", "title": "시스템", "widgets": [
            {"type": "calls", "w": 12}, {"type": "programs", "w": 5}, {"type": "jobs", "w": 7},
            {"type": "errors", "w": 12}]})
    return {"pages": pages}


@router.get("/api/layout")
def layout_get(request: Request):
    viewer = _viewer(request)
    app: CentralApp = request.app.state.central
    with app.db.session() as s:
        row = s.get(UiLayoutRow, viewer.owner)
        if row is not None:
            return ok({"layout": row.layout, "custom": True})
    return ok({"layout": _default_layout(viewer), "custom": False})


@router.put("/api/layout")
def layout_put(request: Request, body: LayoutBody):
    viewer = _viewer(request)
    app: CentralApp = request.app.state.central
    for page in body.pages:
        for w in page.widgets:
            if w.type not in WIDGETS or viewer.role not in WIDGETS[w.type]["roles"]:
                raise OmsError("INVALID_REQUEST", f"쓸 수 없는 위젯: {w.type}")
    if len({p.id for p in body.pages}) != len(body.pages):
        raise OmsError("INVALID_REQUEST", "페이지 id 가 겹침")
    layout = body.model_dump(exclude_none=True)
    with app.db.session() as s, s.begin():
        row = s.get(UiLayoutRow, viewer.owner)
        if row is None:
            s.add(UiLayoutRow(owner=viewer.owner, layout=layout, updated_at=app.clock()))
        else:
            row.layout, row.updated_at = layout, app.clock()
    return ok({"layout": layout, "custom": True})


@router.delete("/api/layout")
def layout_reset(request: Request):
    viewer = _viewer(request)
    app: CentralApp = request.app.state.central
    with app.db.session() as s, s.begin():
        row = s.get(UiLayoutRow, viewer.owner)
        if row is not None:
            s.delete(row)
    return ok({"layout": _default_layout(viewer), "custom": False})


# ───────────── 위젯 데이터 ─────────────
@router.get("/api/w/{wtype}")
async def widget_data(request: Request, wtype: str):
    viewer = _viewer(request)
    spec = WIDGETS.get(wtype)
    if spec is None or viewer.role not in spec["roles"]:
        raise OmsError("NOT_FOUND", f"위젯 {wtype} 없음")
    params = {k: v for k, v in request.query_params.items()}
    fn, app = spec["fn"], request.app.state.central
    if inspect.iscoroutinefunction(fn):
        return ok(await fn(app, viewer, params))
    return ok(await run_in_threadpool(fn, app, viewer, params))   # DB 조회가 이벤트 루프를 막지 않게


@router.get("/api/orders/{order_id}")
def order_detail(request: Request, order_id: int):
    viewer = _viewer(request)
    app: CentralApp = request.app.state.central
    with app.db.session() as s:
        o = s.get(OrderRow, order_id)
        if o is None or (viewer.role == "sub" and o.sub_account_id != viewer.sub_id):
            raise OmsError("ORDER_NOT_FOUND", f"주문 {order_id} 없음")
        start = queries.kst(o.created_at) - timedelta(minutes=1)  # type: ignore[operator]
        events = []
        for e in s.scalars(select(EventRow).where(EventRow.sub_account_id == o.sub_account_id,
                                                  EventRow.ts >= start).order_by(EventRow.id)):
            p = e.payload or {}
            if p.get("id") == order_id or p.get("order_id") == order_id:
                events.append({"ts": queries.kst(e.ts), "type": e.type, "status": p.get("status"),
                               "qty": p.get("qty") if e.type == "FILL" else None,
                               "price": p.get("price") if e.type == "FILL" else None,
                               "reason": p.get("reject_reason")})
        acc = next((e["ts"] for e in events if e["type"] == "ORDER" and e["status"] == "ACCEPTED"), None)
        row = _order_row(app, o, _sub_names(app), {order_id: acc} if acc else {})
    fills = queries.fills(app.db, o.sub_account_id, order_id=order_id)
    return ok({"order": row, "events": events, "fills": fills})


# ── 공통 도우미 ──
def _subs(app: CentralApp, viewer: Viewer, include_system: bool = False) -> list[SubAccount]:
    if viewer.role == "sub":
        return [app.accounts.get(viewer.sub_id)]  # type: ignore[arg-type]
    return [s for s in app.accounts.list() if include_system or s.type != "system"]


def _scope(app: CentralApp, viewer: Viewer, params: dict, include_system: bool = False) -> list[SubAccount]:
    subs = _subs(app, viewer, include_system)
    if params.get("sub") and viewer.role == "admin":
        subs = [s for s in subs if str(s.id) == params["sub"]]
    if params.get("broker"):
        subs = [s for s in subs if s.broker_account_id == params["broker"]]
    return subs


def _sub_names(app: CentralApp) -> dict[int, str]:
    return {s.id: (s.name if s.type != "system" else f"미배정({s.broker_account_id})") for s in app.accounts.list()}


def _day(app: CentralApp, params: dict) -> date:
    raw = params.get("date")
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            raise OmsError("INVALID_REQUEST", f"date 형식 오류: {raw}") from None
    return app.clock().date()


def _int(params: dict, name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(params.get(name, default))))
    except ValueError:
        raise OmsError("INVALID_REQUEST", f"{name} 는 정수") from None


def _name(app: CentralApp, symbol: str) -> str:
    inst = app.instruments.get(symbol)
    return inst.name if inst and inst.name else ""


def _order_row(app: CentralApp, o: OrderRow, names: dict[int, str], accepted: dict[int, datetime]) -> dict:
    created = queries.kst(o.created_at)
    acc = accepted.get(o.id)
    return {"id": o.id, "ts": created, "sub_id": o.sub_account_id, "sub": names.get(o.sub_account_id, "?"),
            "broker": o.broker_account_id, "symbol": o.instrument_id, "name": _name(app, o.instrument_id),
            "side": o.side, "type": o.order_type, "route": o.route, "price": o.price, "qty": o.qty,
            "filled": o.filled_qty, "avg_price": o.avg_fill_price, "status": o.status, "reason": o.reject_reason,
            "client_id": o.client_order_id, "orig_id": o.orig_order_id,
            "accept_ms": round((acc - created).total_seconds() * 1000) if acc and created else None}


def _accepted_times(app: CentralApp, start: datetime, end: datetime) -> dict[int, datetime]:
    out: dict[int, datetime] = {}
    with app.db.session() as s:
        q = select(EventRow.ts, EventRow.payload).where(EventRow.type == "ORDER", EventRow.ts >= start,
                                                         EventRow.ts <= end).order_by(EventRow.id)
        for ts, p in s.execute(q):
            if p and p.get("status") == "ACCEPTED" and p.get("id") not in out:
                out[p["id"]] = queries.kst(ts)  # type: ignore[assignment]
    return out


def _status_counts(app: CentralApp, sub_ids: list[int], start: datetime, end: datetime) -> dict[int, dict[str, int]]:
    out: dict[int, dict[str, int]] = {}
    with app.db.session() as s:
        q = (select(OrderRow.sub_account_id, OrderRow.status, func.count())
             .where(OrderRow.sub_account_id.in_(sub_ids), OrderRow.created_at >= start, OrderRow.created_at <= end)
             .group_by(OrderRow.sub_account_id, OrderRow.status))
        for sub_id, status, n in s.execute(q):
            out.setdefault(sub_id, {})[status] = n
    return out


def _day_stats(counts: dict[str, int]) -> dict:
    total = sum(n for st, n in counts.items() if st != "REPLACED")   # 정정은 새 주문으로 다시 센다
    rejected = counts.get("REJECTED", 0) + counts.get("SUBMIT_FAILED", 0)
    return {"orders": total, "filled": counts.get("FILLED", 0) + counts.get("PARTIALLY_FILLED", 0),
            "rejected": rejected, "unknown": counts.get("UNKNOWN", 0),
            "working": counts.get("ACCEPTED", 0) + counts.get("PARTIALLY_FILLED", 0),
            "reject_rate": round(rejected / total, 3) if total else None}


def _programs(app: CentralApp) -> dict | None:
    path = status_path(app.settings)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {"running": data.get("running"), "updated_at": data.get("updated_at"),
            "children": [{"name": n, **{k: c.get(k) for k in ("running", "started_at", "restarts", "last_exit")}}
                         for n, c in (data.get("children") or {}).items()]}


# ── 위젯 ──
def w_status(app: CentralApp, viewer: Viewer, params: dict) -> dict:
    subs = _subs(app, viewer)
    brokers_in_scope = sorted({s.broker_account_id for s in subs}) if viewer.role == "sub" else list(app.profiles)
    brokers = []
    alerts: list[dict] = []
    for alias in brokers_in_scope:
        p = app.profiles[alias]
        h = app.brokers[alias].health()
        rt = h.get("realtime") or {}
        b = {"alias": alias, "env": p.env, "trading_enabled": p.trading_enabled,
             "connected": bool(h.get("connected")), "realtime_connected": rt.get("connected"),
             "slots_used": rt.get("slots_used"), "slots_limit": rt.get("slots_limit"), "connects": rt.get("connects")}
        brokers.append(b)
        if not b["connected"]:
            alerts.append({"level": "error", "text": f"{alias} 체결통보 연결 끊김"})

    halted = app.halted()
    if viewer.role == "sub":
        sub = subs[0]
        halted = [h for h in halted if h["scope"] in ("all", "market") or h["id"] in (
            sub.broker_account_id, f"{sub.broker_account_id}:KR", f"{sub.broker_account_id}:US", str(sub.id))]
    for h in halted:
        alerts.append({"level": "error", "text": f"정지: {h['scope']} {h['id']}".strip()})

    sub_ids = [s.id for s in subs]
    with app.db.session() as s:
        unknown = s.scalar(select(func.count()).select_from(OrderRow).where(
            OrderRow.status == "UNKNOWN", OrderRow.sub_account_id.in_(sub_ids))) or 0
    if unknown:
        alerts.append({"level": "error", "text": f"접수 여부 미확인(UNKNOWN) 주문 {unknown}건"})

    day = app.clock().date()
    counts = _status_counts(app, sub_ids, queries.day_start(day), queries.day_end(day))
    names = {s.id: s.name for s in subs}
    for sid, c in counts.items():
        st = _day_stats(c)
        if st["orders"] >= 10 and (st["reject_rate"] or 0) >= 0.3:
            alerts.append({"level": "warn", "text": f"{names.get(sid, sid)} 거부율 {st['reject_rate']:.0%} "
                                                     f"({st['rejected']}/{st['orders']})"})

    recon = []
    seen = set()
    for r in queries.reconciliations(app.db, limit=20):
        key = (r["broker_account_id"], r["market"])
        if key in seen or r["broker_account_id"] not in brokers_in_scope:
            continue
        seen.add(key)
        recon.append({"broker": key[0], "market": key[1], "run_at": r["run_at"], "ok": r["ok"]})
        if not r["ok"]:
            alerts.append({"level": "error", "text": f"대사 불일치 {key[0]}/{key[1]}"})

    data: dict[str, Any] = {"brokers": brokers, "halted": halted, "unknown_orders": unknown,
                            "reconciliations": recon, "alerts": alerts}
    if viewer.role == "admin":
        buf = logbuffer.install()
        since = queries.day_start(day).isoformat(timespec="seconds")
        today = [i for i in buf.items if i["ts"] >= since]
        data["log"] = {"errors": sum(i["level"] in ("ERROR", "CRITICAL") for i in today),
                       "warnings": sum(i["level"] == "WARNING" for i in today), "since": buf.started_at}
        if data["log"]["errors"]:
            alerts.append({"level": "warn", "text": f"오늘 ERROR 로그 {data['log']['errors']}건"})
        progs = _programs(app)
        data["programs"] = progs
        for c in (progs or {}).get("children", []):
            if not c.get("running"):
                alerts.append({"level": "error", "text": f"프로그램 {c['name']} 멈춤"})
    return data


def w_accounts(app: CentralApp, viewer: Viewer, params: dict) -> dict:
    day = _day(app, params)
    subs = _scope(app, viewer, params, include_system=viewer.role == "admin")
    start, end = queries.day_start(day), queries.day_end(day)
    counts = _status_counts(app, [s.id for s in subs], start, end)
    names = _sub_names(app)
    rows = []
    for sub in subs:
        balances = app.accounts.balances(sub.id)
        cur: dict[str, dict] = {}
        for c, b in balances.items():
            cur[c] = {"cash": b.cash, "held": b.held, "available": b.available, "position_cost": ZERO,
                      "market_value": ZERO, "unpriced": 0}
        for p in app.accounts.positions(sub.id):
            acc = cur.setdefault(p.currency, {"cash": ZERO, "held": ZERO, "available": ZERO, "position_cost": ZERO,
                                              "market_value": ZERO, "unpriced": 0})
            acc["position_cost"] += q_cash(p.avg_price * p.qty, p.currency)
            last = app.market.last_price(p.instrument_id)
            if last is None:
                acc["unpriced"] += 1
                acc["market_value"] += q_cash(p.avg_price * p.qty, p.currency)
            else:
                acc["market_value"] += q_cash(last * p.qty, p.currency)
        pnl = queries.pnl(app.db, sub.id, start=start, end=end)
        cur = {c: v for c, v in cur.items() if any(v[k] for k in ("cash", "held", "position_cost")) or c in pnl}
        rows.append({"id": sub.id, "name": names[sub.id], "broker": sub.broker_account_id,
                     "env": app.profiles[sub.broker_account_id].env if sub.broker_account_id in app.profiles else None,
                     "type": sub.type, "status": sub.status, "currencies": cur, "pnl": pnl,
                     "day": _day_stats(counts.get(sub.id, {}))})
    return {"date": day, "rows": rows}


def w_pnl_curve(app: CentralApp, viewer: Viewer, params: dict) -> dict:
    day = _day(app, params)
    subs = _scope(app, viewer, params)
    names = _sub_names(app)
    series: dict[tuple[int, str], list] = {}
    with app.db.session() as s:
        q = (select(FillRow).where(FillRow.sub_account_id.in_([x.id for x in subs]),
                                   FillRow.filled_at >= queries.day_start(day), FillRow.filled_at <= queries.day_end(day))
             .order_by(FillRow.filled_at, FillRow.id))
        for f in s.scalars(q):
            pts = series.setdefault((f.sub_account_id, f.currency), [])
            prev = pts[-1][1] if pts else ZERO
            pts.append([queries.kst(f.filled_at), prev + f.realized_pnl])
    return {"date": day, "series": [{"sub_id": sid, "name": names.get(sid, "?"), "currency": cur, "points": pts}
                                    for (sid, cur), pts in sorted(series.items())]}


def w_pnl_daily(app: CentralApp, viewer: Viewer, params: dict) -> dict:
    days = _int(params, "days", 30, 1, 365)
    end_day = _day(app, params)
    start_day = end_day - timedelta(days=days - 1)
    subs = _scope(app, viewer, params)
    names = _sub_names(app)
    by: dict[tuple[int, str], dict[date, dict]] = {}
    with app.db.session() as s:
        q = select(FillRow).where(FillRow.sub_account_id.in_([x.id for x in subs]),
                                  FillRow.filled_at >= queries.day_start(start_day),
                                  FillRow.filled_at <= queries.day_end(end_day))
        for f in s.scalars(q):
            d = queries.kst(f.filled_at).date()  # type: ignore[union-attr]
            a = by.setdefault((f.sub_account_id, f.currency), {}).setdefault(
                d, {"pnl": ZERO, "fee": ZERO, "tax": ZERO, "fills": 0, "wins": 0, "losses": 0,
                    "buy_amount": ZERO, "sell_amount": ZERO})
            a["pnl"] += f.realized_pnl
            a["fee"] += f.fee
            a["tax"] += f.tax
            a["fills"] += 1
            a["buy_amount" if f.side == "buy" else "sell_amount"] += f.price * f.qty
            if f.side == "sell":
                a["wins" if f.realized_pnl > 0 else "losses"] += 1
    out = []
    for (sid, cur), daily in sorted(by.items()):
        tot: dict[str, Any] = {}
        for v in daily.values():
            for k, x in v.items():
                tot[k] = tot.get(k, 0) + x
        sells = tot["wins"] + tot["losses"]
        tot["win_rate"] = round(tot["wins"] / sells, 3) if sells else None
        out.append({"sub_id": sid, "name": names.get(sid, "?"), "currency": cur, "total": tot,
                    "days": [{"date": d, **v} for d, v in sorted(daily.items())]})
    return {"from": start_day, "to": end_day, "series": out}


def w_blotter(app: CentralApp, viewer: Viewer, params: dict) -> dict:
    day = _day(app, params)
    limit = _int(params, "limit", 300, 1, 1000)
    subs = _scope(app, viewer, params, include_system=viewer.role == "admin")
    start, end = queries.day_start(day), queries.day_end(day)
    names = _sub_names(app)
    with app.db.session() as s:
        q = select(OrderRow).where(OrderRow.sub_account_id.in_([x.id for x in subs]),
                                   OrderRow.created_at >= start, OrderRow.created_at <= end)
        if params.get("status"):
            q = q.where(OrderRow.status.in_(params["status"].upper().split(",")))
        if params.get("symbol"):
            q = q.where(OrderRow.instrument_id == params["symbol"].upper())
        if params.get("market"):
            q = q.where(OrderRow.market == params["market"].upper())
        orders = list(s.scalars(q.order_by(OrderRow.id.desc()).limit(limit)))
        accepted = _accepted_times(app, start, end + timedelta(hours=1)) if orders else {}
        rows = [_order_row(app, o, names, accepted) for o in orders]
    counts: dict[str, int] = {}
    for c in _status_counts(app, [x.id for x in subs], start, end).values():
        for st, n in c.items():
            counts[st] = counts.get(st, 0) + n
    return {"date": day, "rows": rows, "counts": counts}


def w_positions(app: CentralApp, viewer: Viewer, params: dict) -> dict:
    subs = _scope(app, viewer, params, include_system=viewer.role == "admin")
    names = _sub_names(app)
    rows = []
    for sub in subs:
        for p in app.accounts.positions(sub.id):
            last = app.market.last_price(p.instrument_id)
            cost = q_cash(p.avg_price * p.qty, p.currency)
            value = q_cash(last * p.qty, p.currency) if last is not None else None
            rows.append({"sub_id": sub.id, "sub": names[sub.id], "symbol": p.instrument_id,
                         "name": _name(app, p.instrument_id), "currency": p.currency, "qty": p.qty,
                         "sellable": p.sellable, "avg_price": p.avg_price, "last": last, "cost": cost,
                         "value": value, "unrealized": value - cost if value is not None else None,
                         "unrealized_rate": round((value - cost) / cost, 4) if value is not None and cost else None})
    return {"rows": rows}


def w_calls(app: CentralApp, viewer: Viewer, params: dict) -> dict:
    out = []
    for alias, broker in app.brokers.items():
        rt = (broker.health().get("realtime") or {})
        out.append({"alias": alias, "env": app.profiles[alias].env if alias in app.profiles else None,
                    "rate_limit_per_sec": app.profiles[alias].rate_limit_per_sec if alias in app.profiles else None,
                    "slots_used": rt.get("slots_used"), "slots_limit": rt.get("slots_limit"),
                    "connects": rt.get("connects"), "stats": broker.call_stats()})
    return {"brokers": out}


def w_errors(app: CentralApp, viewer: Viewer, params: dict) -> dict:
    buf = logbuffer.install()
    since = None
    if params.get("since", "today") == "today":
        since = queries.day_start(app.clock().date()).isoformat(timespec="seconds")
    return {"since": since or buf.started_at, "started_at": buf.started_at, "groups": buf.groups(since)[:50],
            "recent": buf.recent(_int(params, "limit", 50, 1, 500))}


def w_jobs(app: CentralApp, viewer: Viewer, params: dict) -> dict:
    sched = {j["name"]: j for j in app.ops.scheduler.status()}
    with app.db.session() as s:
        runs = {r.name: r for r in s.scalars(select(JobRunRow))}
    jobs = []
    for name in sorted(set(sched) | set(runs)):
        j, r = sched.get(name, {}), runs.get(name)
        jobs.append({"name": name, "at": j.get("at"), "tz": j.get("tz"), "description": j.get("description"),
                     "running": j.get("running", False), "run_date": r.run_date if r else None,
                     "finished_at": queries.kst(r.finished_at) if r else None, "ok": r.ok if r else None,
                     "detail": r.detail if r else None})
    jobs.sort(key=lambda j: (j["at"] or "99:99"))
    return {"jobs": jobs, "reconciliations": queries.reconciliations(app.db, limit=10)}


def w_programs(app: CentralApp, viewer: Viewer, params: dict) -> dict:
    return {"supervisor": _programs(app)}


def _session_window(market: str, day: date) -> tuple[datetime, datetime]:
    """그 거래일 정규장 구간 (미국은 뉴욕 날짜 기준)."""
    if market == "US":
        return (datetime.combine(day, dtime(9, 30), NEW_YORK).astimezone(KST),
                datetime.combine(day, dtime(16, 0), NEW_YORK).astimezone(KST))
    return datetime.combine(day, dtime(9, 0), KST), datetime.combine(day, dtime(15, 30, 59), KST)


CHART_INTERVALS = ("1m", "5m", "15m", "30m", "60m", "1d")
_CHART_TTL_LIVE_S, _CHART_TTL_PAST_S, _CHART_TTL_ERROR_S = 20.0, 600.0, 10.0


class ChartCache:
    """화면 갱신이 KIS 호출 한도(주문과 같은 한도)를 먹지 않게 봉 조회를 짧게 캐시한다."""

    def __init__(self, max_items: int = 200) -> None:
        self.items: dict[tuple, tuple[float, list, str | None]] = {}
        self.max_items = max_items

    async def get(self, app: CentralApp, inst, interval: str, start: datetime, end: datetime,
                  live: bool) -> tuple[list, str | None]:
        key = (inst.id, interval, start, end)
        now = time.monotonic()
        hit = self.items.get(key)
        if hit and hit[0] > now:
            return hit[1], hit[2]
        try:
            bars, err = (await app.candles.get(inst, interval, start, end)).candles, None
        except (BrokerUnavailable, OmsError) as e:
            bars, err = [], str(e)
        if len(self.items) >= self.max_items:
            self.items = {k: v for k, v in self.items.items() if v[0] > now}
            while len(self.items) >= self.max_items:
                self.items.pop(next(iter(self.items)))
        ttl = _CHART_TTL_ERROR_S if err else _CHART_TTL_LIVE_S if live else _CHART_TTL_PAST_S
        self.items[key] = (now + ttl, bars, err)
        return bars, err


def _traded_symbols(app: CentralApp, sub_ids: list[int], start: datetime, end: datetime) -> list[dict]:
    with app.db.session() as s:
        q = (select(FillRow.instrument_id, func.max(FillRow.filled_at), func.count())
             .where(FillRow.sub_account_id.in_(sub_ids), FillRow.filled_at >= start, FillRow.filled_at <= end)
             .group_by(FillRow.instrument_id))
        rows = sorted(s.execute(q), key=lambda r: r[1], reverse=True)
    return [{"symbol": sym, "name": _name(app, sym), "fills": n, "last": queries.kst(last)} for sym, last, n in rows]


async def w_chart(app: CentralApp, viewer: Viewer, params: dict) -> dict:
    day = _day(app, params)
    interval = params.get("interval") or "1m"
    if interval not in CHART_INTERVALS:
        raise OmsError("INVALID_REQUEST", f"interval 은 {CHART_INTERVALS} 중 하나")
    subs = _scope(app, viewer, params, include_system=viewer.role == "admin")
    sub_ids = [x.id for x in subs]
    # 미국장은 KST 날짜가 하루 걸치므로 전날부터 본다
    traded = await run_in_threadpool(_traded_symbols, app, sub_ids, queries.day_start(day - timedelta(days=1)),
                                     queries.day_end(day))
    symbol = (params.get("symbol") or "").strip().upper()
    if not symbol and traded:
        symbol = traded[0]["symbol"]
    out: dict[str, Any] = {"date": day, "interval": interval, "symbols": traded, "symbol": symbol or None,
                           "candles": [], "markers": [], "positions": [], "error": None}
    if not symbol:
        return out
    try:
        inst = app.catalog.resolve(symbol)
    except OmsError as e:
        return {**out, "error": e.message}

    if interval in MINUTE_INTERVALS:
        start, end = _session_window(inst.market, day)
    else:
        start = queries.day_start(day - timedelta(days=_int(params, "days", 120, 5, 400)))
        end = queries.day_end(day)
    live = end >= app.clock() - timedelta(minutes=2)
    cache: ChartCache = app.__dict__.setdefault("_ui_chart_cache", ChartCache())
    bars, err = await cache.get(app, inst, interval, start, end, live)

    def fills_and_positions():
        names = _sub_names(app)
        with app.db.session() as s:
            q = (select(FillRow).where(FillRow.sub_account_id.in_(sub_ids), FillRow.instrument_id == inst.id,
                                       FillRow.filled_at >= start, FillRow.filled_at <= end)
                 .order_by(FillRow.filled_at, FillRow.id))
            marks = [{"ts": queries.kst(f.filled_at), "side": f.side, "price": f.price, "qty": f.qty,
                      "order_id": f.order_id, "sub": names.get(f.sub_account_id, "?"), "pnl": f.realized_pnl}
                     for f in s.scalars(q)]
        pos = [{"sub": names[x.id], "qty": p.qty, "avg_price": p.avg_price}
               for x in subs for p in app.accounts.positions(x.id) if p.instrument_id == inst.id and p.qty]
        return marks, pos

    marks, pos = await run_in_threadpool(fills_and_positions)
    return {**out, "symbol": inst.id, "name": inst.name or "", "currency": inst.currency, "error": err,
            "last": app.market.last_price(inst.id),
            "candles": [[b.ts, b.open, b.high, b.low, b.close, b.volume] for b in bars],
            "markers": marks, "positions": pos}


def w_symbols(app: CentralApp, viewer: Viewer, params: dict) -> dict:
    """종목별 매매 요약. 실현손익은 매도 체결에 붙은 값(수수료, 세금 차감)."""
    days = _int(params, "days", 1, 1, 365)
    end_day = _day(app, params)
    start_day = end_day - timedelta(days=days - 1)
    subs = _scope(app, viewer, params, include_system=viewer.role == "admin")
    names = _sub_names(app)
    by: dict[tuple[int, str], dict] = {}
    with app.db.session() as s:
        q = (select(FillRow).where(FillRow.sub_account_id.in_([x.id for x in subs]),
                                   FillRow.filled_at >= queries.day_start(start_day),
                                   FillRow.filled_at <= queries.day_end(end_day))
             .order_by(FillRow.filled_at, FillRow.id))
        for f in s.scalars(q):
            a = by.setdefault((f.sub_account_id, f.instrument_id), {
                "sub_id": f.sub_account_id, "sub": names.get(f.sub_account_id, "?"), "symbol": f.instrument_id,
                "name": _name(app, f.instrument_id), "currency": f.currency, "buy_qty": 0, "sell_qty": 0,
                "buy_amount": ZERO, "sell_amount": ZERO, "pnl": ZERO, "fee": ZERO, "wins": 0, "losses": 0,
                "last": None})
            a["buy_qty" if f.side == "buy" else "sell_qty"] += f.qty
            a["buy_amount" if f.side == "buy" else "sell_amount"] += f.price * f.qty
            a["pnl"] += f.realized_pnl
            a["fee"] += f.fee + f.tax
            if f.side == "sell":
                a["wins" if f.realized_pnl > 0 else "losses"] += 1
            a["last"] = queries.kst(f.filled_at)
    held = {(x.id, p.instrument_id): p.qty for x in subs for p in app.accounts.positions(x.id)}
    rows = []
    for (sid, sym), a in by.items():
        sells = a["wins"] + a["losses"]
        rows.append({**a, "held": held.get((sid, sym), 0), "win_rate": round(a["wins"] / sells, 3) if sells else None})
    rows.sort(key=lambda r: r["last"], reverse=True)
    return {"from": start_day, "to": end_day, "rows": rows}


WIDGETS: dict[str, dict[str, Any]] = {
    "status": {"fn": w_status, "roles": ("admin", "sub"), "title": "상태", "refresh": 5,
               "params": {}},
    "accounts": {"fn": w_accounts, "roles": ("admin", "sub"), "title": "계좌", "refresh": 10,
                 "params": {"date": "YYYY-MM-DD (기본 오늘)", "broker": "paper/real", "sub": "서브계좌 id"}},
    "pnl_curve": {"fn": w_pnl_curve, "roles": ("admin", "sub"), "title": "당일 손익 곡선", "refresh": 10,
                  "params": {"date": "YYYY-MM-DD", "broker": "paper/real", "sub": "서브계좌 id"}},
    "pnl_daily": {"fn": w_pnl_daily, "roles": ("admin", "sub"), "title": "일별 손익", "refresh": 60,
                  "params": {"days": "기간 일수 (기본 30)", "date": "마지막 날", "sub": "서브계좌 id"}},
    "blotter": {"fn": w_blotter, "roles": ("admin", "sub"), "title": "주문 블로터", "refresh": 3,
                "params": {"date": "YYYY-MM-DD", "sub": "서브계좌 id", "status": "FILLED,REJECTED,...",
                           "symbol": "KR:005930", "market": "KR/US", "limit": "최대 행 (기본 300)"}},
    "positions": {"fn": w_positions, "roles": ("admin", "sub"), "title": "보유 종목", "refresh": 10,
                  "params": {"broker": "paper/real", "sub": "서브계좌 id"}},
    "chart": {"fn": w_chart, "roles": ("admin", "sub"), "title": "차트", "refresh": 15,
              "params": {"symbol": "KR:005930 (비우면 최근 체결 종목)", "interval": "1m/5m/15m/30m/60m/1d",
                         "date": "YYYY-MM-DD", "days": "일봉 기간 (기본 120)", "sub": "서브계좌 id"}},
    "symbols": {"fn": w_symbols, "roles": ("admin", "sub"), "title": "종목별 매매", "refresh": 15,
                "params": {"days": "기간 일수 (기본 1)", "date": "마지막 날", "sub": "서브계좌 id"}},
    "calls": {"fn": w_calls, "roles": ("admin",), "title": "KIS 호출", "refresh": 10, "params": {}},
    "errors": {"fn": w_errors, "roles": ("admin",), "title": "오류와 경고", "refresh": 15,
               "params": {"since": "today/start", "limit": "최근 목록 수 (기본 50)"}},
    "jobs": {"fn": w_jobs, "roles": ("admin",), "title": "일정 작업과 대사", "refresh": 30, "params": {}},
    "programs": {"fn": w_programs, "roles": ("admin",), "title": "프로그램", "refresh": 10, "params": {}},
}
