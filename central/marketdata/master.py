"""KIS 종목 마스터 파일 (docs/PLAN.md 7.2).

출처: 공식 저장소 stocks_info/ (kis_kospi_code_mst.py, kis_kosdaq_code_mst.py, overseas_stock_code.py)
- 국내: {BASE}kospi_code.mst.zip, kosdaq_code.mst.zip (cp949 고정폭). 줄 = 단축코드(9) + 표준코드(12) + 한글명 + 꼬리.
  꼬리 길이(개행 제외)는 코스피 227, 코스닥 221자 (공식 샘플은 개행 포함 228/222). 꼬리 첫 2자가 증권그룹구분코드.
  NXT 거래 가능 여부 필드는 없다.
- 미국: {BASE}nasmst.cod.zip, nysmst.cod.zip, amsmst.cod.zip (cp949 탭 구분 24열).
  4: Symbol, 5: 실시간 심볼, 6: 한글명, 7: 영문명, 8: 증권유형(1 지수, 2 주식, 3 ETP, 4 워런트)
공식 샘플은 SSL 검증을 끄고 받지만 여기서는 켠다. 파일은 디스크에 풀지 않고 메모리에서만 처리한다.
"""

from __future__ import annotations

import io
import logging
import zipfile
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

BASE = "https://new.real.download.dws.co.kr/common/master/"
KR_FILES = {"kospi_code.mst": 227, "kosdaq_code.mst": 221}
US_FILES = {"nas": "NASD", "nys": "NYSE", "ams": "AMEX"}
_KR_KIND = {"ST": "stock", "EF": "etf", "EN": "etn"}
_US_KIND = {"2": "stock", "3": "etf"}
MAX_ZIP_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class MasterItem:
    market: str
    code: str
    exchange: str
    name: str
    name_en: str
    kind: str


def parse_kr(text: str, tail: int) -> list[MasterItem]:
    items = []
    for line in text.splitlines():
        if len(line) <= tail + 21:
            continue
        code = line[:9].strip()
        if len(code) != 6:
            continue
        name = line[21:len(line) - tail].strip()
        group = line[len(line) - tail:][:2]
        items.append(MasterItem("KR", code, "KRX", name, "", _KR_KIND.get(group, "other")))
    return items


def parse_us(text: str, exchange: str) -> list[MasterItem]:
    items = []
    for line in text.splitlines():
        f = line.split("\t")
        if len(f) < 9 or not f[4].strip():
            continue
        if f[8] not in ("2", "3"):      # 지수, 워런트 제외
            continue
        items.append(MasterItem("US", f[4].strip().upper(), exchange, f[6].strip(), f[7].strip(), _US_KIND[f[8]]))
    return items


def _unzip_text(content: bytes) -> str:
    if len(content) > MAX_ZIP_BYTES:
        raise ValueError(f"마스터 파일이 너무 큼 ({len(content)} bytes)")
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        names = z.namelist()
        if len(names) != 1 or z.getinfo(names[0]).file_size > 5 * MAX_ZIP_BYTES:
            raise ValueError(f"예상과 다른 압축 내용: {names}")
        return z.read(names[0]).decode("cp949", errors="replace")


async def fetch_master(http: httpx.AsyncClient, markets: tuple[str, ...] = ("KR", "US")) -> list[MasterItem]:
    items: list[MasterItem] = []
    if "KR" in markets:
        for name, tail in KR_FILES.items():
            res = await http.get(BASE + name + ".zip", timeout=60)
            res.raise_for_status()
            part = parse_kr(_unzip_text(res.content), tail)
            log.info("종목 마스터 %s: %d건", name, len(part))
            items += part
    if "US" in markets:
        for prefix, exchange in US_FILES.items():
            res = await http.get(BASE + f"{prefix}mst.cod.zip", timeout=60)
            res.raise_for_status()
            part = parse_us(_unzip_text(res.content), exchange)
            log.info("종목 마스터 %s: %d건", prefix, len(part))
            items += part
    return items
