"""파일럿의 매매 규칙과 데이터 점검 (네트워크 없는 순수 로직, 테스트 대상).

매매 규칙 (수익이 아니라 주문 경로를 고르게 쓰는 것이 목적)
- 1분봉 종가가 20봉 이동평균보다 entry_bp 이상 낮으면 1주 시장가 매수
- 보유 중이면 종가가 이동평균 이상이거나 max_hold 분이 지나면 전량 시장가 매도
- 종목마다 하루 최대 max_entries 번 진입
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal


@dataclass
class SignalState:
    window: int = 20
    entry_bp: Decimal = Decimal("5")        # 0.05%
    max_hold: timedelta = timedelta(minutes=20)
    max_entries: int = 8
    closes: deque = field(default_factory=deque)
    entries_today: int = 0
    entered_at: datetime | None = None

    def push(self, close: Decimal) -> Decimal | None:
        """종가를 넣고 이동평균을 돌려준다 (봉이 모자라면 None)."""
        self.closes.append(close)
        while len(self.closes) > self.window:
            self.closes.popleft()
        if len(self.closes) < self.window:
            return None
        return sum(self.closes, Decimal(0)) / self.window

    def decide(self, close: Decimal, sma: Decimal | None, position: int, now: datetime) -> str | None:
        """'buy' / 'sell' / None."""
        if sma is None:
            return None
        if position > 0:
            held_long = self.entered_at is not None and now - self.entered_at >= self.max_hold
            if close >= sma or held_long:
                return "sell"
            return None
        if self.entries_today >= self.max_entries:
            return None
        if close <= sma * (1 - self.entry_bp / Decimal(10000)):
            return "buy"
        return None


@dataclass
class BarCheck:
    """실시간 1분봉(체결로 만든 것)과 REST 1분봉을 비교한다."""
    received: dict[datetime, dict] = field(default_factory=dict)   # 봉 시작 시각 → 실시간 완성봉

    def add(self, bar: dict, ts: datetime) -> None:
        self.received[ts] = bar

    def compare(self, rest: list[dict], start: datetime, end: datetime) -> dict:
        """start~end 의 REST 봉과 비교. 실시간 쪽이 없는 분, 종가가 다른 분, 거래량 차이를 센다."""
        rest_by_ts = {datetime.fromisoformat(b["ts"]): b for b in rest}
        window = [t for t in rest_by_ts if start <= t < end]
        missing = [t for t in window if t not in self.received]
        close_diff, vol_diff = [], 0
        for t in window:
            got = self.received.get(t)
            if got is None:
                continue
            if Decimal(got["close"]) != Decimal(rest_by_ts[t]["close"]):
                close_diff.append(t)
            vol_diff += abs(int(got["volume"]) - int(rest_by_ts[t]["volume"]))
        rest_vol = sum(int(rest_by_ts[t]["volume"]) for t in window) or 1
        return {"rest_bars": len(window), "realtime_bars": len(window) - len(missing),
                "missing": [f"{t:%H:%M}" for t in sorted(missing)][:30], "missing_count": len(missing),
                "close_mismatch": [f"{t:%H:%M}" for t in sorted(close_diff)][:30],
                "close_mismatch_count": len(close_diff), "volume_diff_pct": round(vol_diff * 100 / rest_vol, 3)}
