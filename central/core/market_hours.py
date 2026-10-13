"""정규장 주문 허용 시간 판단 (시간외, 장전 동시호가 제외).

국내: 09:00–15:20 연속매매, 15:20–15:30 장마감 동시호가(설정으로 허용 여부 결정). 08:30–09:00 장전 동시호가는 거부.
미국: 09:30–16:00 America/New_York (서머타임은 zoneinfo 가 처리).
휴장일은 외부(M5 캘린더)에서 주입한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
NEW_YORK = ZoneInfo("America/New_York")

_KR_OPEN, _KR_CONTINUOUS_END, _KR_CLOSE = time(9, 0), time(15, 20), time(15, 30)
_US_OPEN, _US_CLOSE = time(9, 30), time(16, 0)


def now_kst() -> datetime:
    return datetime.now(KST)


@dataclass
class MarketHours:
    kr_allow_closing_auction: bool = True
    holidays: dict[str, set[date]] = field(default_factory=lambda: {"KR": set(), "US": set()})

    def can_place_order(self, market: str, at: datetime) -> tuple[bool, str]:
        if market == "KR":
            local = at.astimezone(KST)
            if local.weekday() >= 5 or local.date() in self.holidays.get("KR", set()):
                return False, "국내 휴장일"
            t = local.time()
            if _KR_OPEN <= t < _KR_CONTINUOUS_END:
                return True, ""
            if _KR_CONTINUOUS_END <= t < _KR_CLOSE:
                return (True, "") if self.kr_allow_closing_auction else (False, "장마감 동시호가 주문 비허용")
            return False, "국내 정규장 시간 아님 (09:00–15:30, 장전 동시호가 제외)"
        if market == "US":
            local = at.astimezone(NEW_YORK)
            if local.weekday() >= 5 or local.date() in self.holidays.get("US", set()):
                return False, "미국 휴장일"
            if _US_OPEN <= local.time() < _US_CLOSE:
                return True, ""
            return False, "미국 정규장 시간 아님 (09:30–16:00 ET)"
        return False, f"알 수 없는 시장 {market}"
