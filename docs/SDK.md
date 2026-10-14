# 중앙증권 SDK / API 안내 (서브계좌 프로그램 개발용)

중앙증권 위에서 돌아가는 서브계좌 프로그램(전략, 파일럿, 외부 앱)을 만들 때 보는 문서입니다.
프로그램은 중앙증권 서버와 **별도 프로세스**이며, 발급받은 **서브계좌 API 키 하나**로 자기 서브계좌만 다룹니다.
완성된 예제는 `apps/pilot/` 입니다.

- 서버 주소: `http://127.0.0.1:8700` (같은 PC) 또는 Tailscale IP. 모든 경로 앞에 `/api/v1`
- 대화형 문서: `http://127.0.0.1:8700/api/v1/docs` (서버 실행 중)
- 서버 코드 위치: `central/api/me.py`, `central/api/market.py`, `central/api/ws.py`, SDK 는 `central/client.py`

---

## 1. 지켜야 할 규칙

| 규칙 | 이유 |
|---|---|
| API 키를 코드나 저장소에 쓰지 않는다. `%CENTRAL_HOME%\secrets\apps\<이름>.toml` 이나 환경변수에서 읽는다 | 저장소가 public. `cs_` 키는 커밋 훅이 막는다 |
| 새 프로그램은 **모의 계좌(paper)** 서브계좌로 만든다 | 실전 주문은 사용자가 매번 명시적으로 승인해야 한다 |
| 금액과 가격은 **문자열 또는 Decimal**. float 금지 | 서버가 JSON 숫자 금액을 거부한다 |
| 주문마다 고유한 `client_order_id` (1~48자) | 같은 값으로 다시 보내면 새 주문 대신 기존 주문을 돌려준다 (멱등) |
| WebSocket 이 끊기면 다시 붙고, 붙은 직후 REST 로 보유와 진행 중 주문을 다시 읽는다 | 끊긴 동안의 이벤트는 다시 보내 주지 않는다 |
| REST 시세/캔들 호출을 줄이고 실시간 채널을 쓴다 | KIS 호출 한도(모의 계좌 초당 1건)를 모든 프로그램이 나눠 쓴다 |

현재 운영 환경 (2026-10)
- 모의 계좌는 **국내(KR)만** 지원합니다. 미국 주문은 실전 계좌에서만 가능합니다(승인 필요).
- 실전 계좌의 실시간 등록 한도가 3건뿐이라 새 미국 실시간 채널은 대부분 `SUBSCRIPTION_LIMIT` 로 거부됩니다.
  `US:AAPL` 체결은 상시 수집 중이라 함께 받을 수 있습니다.
- 국내 시세는 모의 계좌 연결로 받으므로 KRX 시세만 있습니다(NXT/통합 시세 없음).

---

## 2. 빠른 시작

```python
import asyncio, os
from central.client import CentralClient, CentralError

async def main():
    async with CentralClient(os.environ["CENTRAL_URL"], os.environ["CENTRAL_API_KEY"]) as cs:
        me = await cs.me()                       # 서브계좌 정보, 거래 가능 여부, 한도
        q = await cs.quote("KR:005930")          # 현재가
        o = await cs.buy("KR:005930", 1, q["lower_limit"], client_order_id="demo-001")   # 지정가
        await cs.cancel(o["id"])
        async for msg in cs.stream(["tick:KR:005930", "candle.1m:KR:005930"]):
            print(msg["ch"], msg["data"])

asyncio.run(main())
```

SDK 오류는 모두 `CentralError(code, message, status)` 로 올라옵니다.

---

## 3. 공통 형식

- 응답: `{"ok": true, "data": ...}` 또는 `{"ok": false, "error": {"code": "...", "message": "..."}}`. SDK 는 `data` 만 돌려준다
- 인증 헤더: `X-API-Key: cs_...` (`/me`, `/market`, `/ws`). 쿼리스트링 키는 받지 않는다
- 종목 ID: `시장:코드` 형식. `KR:005930`, `US:AAPL`, `US:BRK/B` (대소문자 무관, 서버가 대문자로 정규화)
- 시각: ISO8601, KST(+09:00). 기간 조회의 날짜만 준 값은 그날 00:00~23:59:59 KST
- 금액: 문자열 Decimal (`"237000"`, `"187.25"`, 틱 가격은 `"61500.0000"` 처럼 소수 4자리일 수 있음)

---

## 4. SDK 메서드

### 계좌
| 메서드 | 경로 | 설명 |
|---|---|---|
| `me()` | `GET /me` | id, name, status, broker_env(paper/real), markets, trading_enabled, kr_routes, kr_default_route, halted{시장: bool}, risk_limits |
| `balance()` | `GET /me/balance` | 통화별 `cash`, `held`(주문 홀드), `available`, `position_cost`, `realized_pnl`. 실시간 시세가 있는 보유는 `market_value`, `unrealized_pnl` 추가 |
| `positions(market=None)` | `GET /me/positions` | `[{symbol, currency, qty, sellable, avg_price, realized_pnl}]` (수량 0 제외) |
| `ledger(currency=, type=, start=, end=, limit=200)` | `GET /me/ledger` | 현금 원장 (최신순) |
| `pnl(market=, start=, end=)` | `GET /me/pnl` | 통화별 `{realized_pnl, fee, tax, buy_amount, sell_amount, fills}` (수수료/세금 차감 후) |

### 주문
| 메서드 | 경로 | 설명 |
|---|---|---|
| `buy(symbol, qty, price=None, client_order_id=, route=)` | `POST /me/orders` | price 생략 = 시장가 (국내만) |
| `sell(...)` | 같음 | |
| `place(symbol, side, qty, price, order_type=, route=, client_order_id=)` | 같음 | 일반형 |
| `cancel(order_id)` | `POST /me/orders/{id}/cancel` | 상태가 `CANCEL_PENDING` 으로 바뀌고, 확정은 이벤트로 온다 |
| `amend(order_id, price, qty=None)` | `POST /me/orders/{id}/amend` | 지정가만. 기존 주문은 `REPLACED`, **새 주문 객체(새 id)** 를 돌려준다. `orig_order_id` 로 연결 |
| `cancel_all(market=None)` | `POST /me/orders/cancel-all` | `{requested: [id...], failed: [{order_id, error}]}` |
| `order(order_id)` | `GET /me/orders/{id}` | 주문 + `fills` 목록 |
| `orders(status=, symbol=, market=, start=, end=, limit=200)` | `GET /me/orders` | 최신순. status 는 `"ACCEPTED,PARTIALLY_FILLED"` 처럼 쉼표로 여러 개 |
| `fills(symbol=, order_id=, start=, end=, limit=200)` | `GET /me/fills` | `[{id, order_id, symbol, side, qty, price, fee, tax, realized_pnl, currency, venue, filled_at}]` |

주문 객체: `id, sub_account_id, client_order_id, symbol, side, order_type, route, price, qty, filled_qty,
avg_fill_price, status, reject_reason, orig_order_id, broker_order_no, created_at`

### 시세
| 메서드 | 경로 | 설명 |
|---|---|---|
| `quote(symbol)` | `GET /market/quote/{symbol}` | `last, upper_limit, lower_limit, source`. 실시간 수신 중(최근 10초 체결)이면 `source="realtime"` 에 `bid, ask, cum_volume, venue` 추가 |
| `orderbook(symbol)` | `GET /market/orderbook/{symbol}` | `asks`, `bids`: `[[가격, 수량], ...]` 가까운 호가부터 10단계 (미국은 1단계) |
| `candles(symbol, interval="1d", start=, end=, limit=500)` | `GET /market/candles/{symbol}` | interval: `1m 5m 15m 30m 60m 1d 1w 1M`. `candles: [{ts, open, high, low, close, volume}]` 오래된 것부터, ts 는 봉 시작 시각. 분봉은 정규장만. start 생략 시 분봉은 하루 전부터, `truncated` 면 기간을 줄일 것 |
| `history(symbol, interval="1d", adjust="none", start=, end=, limit=5000)` | `GET /market/history/{symbol}` | 보관소 조회 (KIS 에서 만료된 과거 분봉 포함, 보관 중인 종목만). interval: `1m 1d`. adjust: `none` 원주가, `split` 분할/병합만, `total` 배당까지. symbol 에 지수 가능 (`INDEX:KR:0001` 코스피, `INDEX:US:COMP` 나스닥 종합). `events: [{date, ratio, kind}]` 은 기업행위 (date 이전 가격 x ratio) |
| `ticks(symbol, day=None, limit=10000)` | `GET /market/ticks/{symbol}` | 저장된 틱 (상시 수집 종목이나 누군가 구독 중일 때만 쌓임) |
| `calendar(market="KR", start=, end=)` | `GET /market/calendar` | `days: [{date, open, source, early_close_et?}]` |
| `market_status(market=None)` | `GET /market/status` | 시장별 `{accepting_orders, reason}` |
| `symbols(q="", market=None, limit=50)` | `GET /market/symbols` | 이름/코드 검색. `[{id, market, code, exchange, name, name_en, kind, nxt_tradable}]` |
| `fx()` | `GET /market/fx` | USDKRW 환율 |

---

## 5. 주문 규칙

- **시간**: 정규장만. 국내 09:00~15:30 (15:20~15:30 동시호가 허용, 장전 동시호가 거부), 미국 09:30~16:00 ET(조기폐장일 13:00). 휴장일 거부. 위반은 `MARKET_CLOSED`
- **주문유형**: 국내 `limit`, `market` / 미국 `limit` 만
- **호가단위**: 국내 주식은 2023년 호가단위표로 검사(`INVALID_TICK_SIZE`). ETF/ETN 은 원 단위만 검사하고 판단은 KIS 에 맡김. 정정 가격도 같다. 안전한 가격은 `orderbook()` 의 호가나 `upper_limit`/`lower_limit`
- **경로(route)**: 국내만. 생략하면 실계좌 기본값(모의 KRX, 실전 SOR). NXT 는 관리자가 지정한 종목만
- **홀드**: 매수는 `가격 x 수량 x (1 + 수수료율)` 만큼 현금을 잡는다. **시장가 매수는 상한가(+30%) 기준**이라 실제보다 많이 잡히고, 리스크 한도(1회 주문금액)도 상한가 기준으로 본다. 매도는 수량을 잡는다
- **리스크 한도**(관리자가 설정, `me()["risk_limits"]`): 1회 주문금액, 종목 보유금액, 일일 손실(넘으면 매수만 차단), 미체결 수, 허용 종목
- **미체결 소멸**: 국내는 20:10, 미국은 마감 후 서버가 남은 주문을 `EXPIRED` 로 정리하고 홀드를 푼다

### 주문 상태
```
SUBMITTING → ACCEPTED → PARTIALLY_FILLED → FILLED
                 │              └─────────→ CANCEL_PENDING → CANCELED
                 ├→ REPLACED (정정, 새 주문이 이어받음)
                 └→ EXPIRED (장 종료)
SUBMITTING → REJECTED (KIS 거부) / UNKNOWN (접수 응답 유실, 서버가 동기화로 확정) → SUBMIT_FAILED
```
끝난 상태: `FILLED CANCELED REJECTED SUBMIT_FAILED EXPIRED REPLACED`.
`UNKNOWN` 은 접수 여부를 모르는 상태입니다. 같은 주문을 새 client_order_id 로 다시 내지 마세요(이중 주문 위험).
서버가 KIS 당일 주문내역과 맞춰 확정하는 시점은 서버 시작, 실시간 연결 재접속, 장 마감 작업(국내 15:45, 20:10)이며
그 사이에는 확정되지 않을 수 있습니다. 홀드도 그때까지 유지됩니다.

---

## 6. 실시간 WebSocket

`cs.stream(channels)` 가 `ws://.../api/v1/ws` 에 붙어 메시지 dict 를 하나씩 돌려줍니다. 끊기면 반복이 끝나거나 예외가 나므로 **호출 측에서 재접속**합니다.

### 자동으로 받는 채널 (자기 서브계좌 것만)
| ch | data |
|---|---|
| `system` | 접속 직후 `{"type": "WELCOME", sub_account_id, channels}`. 구독 응답 `SUBSCRIBED`, `PONG`, 오류 `{"type": "ERROR", code, message}`, 운영 이벤트 `HALT`/`RESUME`/`TRADING_ENABLED` |
| `orders` | `{"type": "ORDER" 또는 "ORDER_UNKNOWN", id, client_order_id, symbol, side, order_type, route, price, qty, filled_qty, status, reject_reason, ts}` 상태가 바뀔 때마다 |
| `fills` | `{"type": "FILL", order_id, qty, price, venue, ts}` 체결 1건 (이번 체결 수량) |
| `account` | 자금 이체, 보정, 상태 변경 등 |

### 시세 채널 (구독 필요)
| 채널 | data |
|---|---|
| `tick:KR:005930` | `{symbol, ts, price, qty, cum_volume, bid, ask, side(buy/sell/null), venue}` |
| `orderbook:KR:005930` | `{symbol, ts, asks: [[가격, 수량]...], bids: [...], venue}` |
| `candle.1m:KR:005930` | `{symbol, ts(봉 시작), open, high, low, close, volume, closed}` 체결마다 진행 중 봉(`closed=false`), 분이 바뀌면 직전 봉을 `closed=true` 로 한 번 |

- 같은 종목의 `tick` 과 `candle.1m` 은 KIS 등록 하나를 같이 씁니다. 실시간 1분봉은 체결이 있어야 만들어집니다
- 등록 한도를 넘으면 `ERROR` 의 code `SUBSCRIPTION_LIMIT`. 재연결 뒤 KIS 가 거부한 채널은 `SUBSCRIPTION_DROPPED` 로 알리고 빠집니다
- 연결을 끊으면 그 연결의 시세 구독은 자동 해제됩니다
- 메시지를 늦게 읽어 대기열(1000건)이 차면 서버가 `4429 SLOW_CONSUMER` 로 끊습니다. 수신 루프에서 오래 걸리는 일(주문 등)은 `asyncio.create_task` 로 넘기세요
- 직접 보내는 명령: `{"op": "subscribe" | "unsubscribe", "channels": [...]}`, `{"op": "ping"}`. 인증 실패는 `4401`

### 주의: 이벤트와 REST 응답의 순서
`buy()` 의 응답보다 `orders` 이벤트(체결 완료)가 먼저 올 수 있습니다. 보유를 이벤트로 누적한다면
**주문별로 이미 반영한 `filled_qty` 를 기억해 늘어난 만큼만 더하고, 끝난 주문에 늦게 온 이전 상태는 버리세요**
(`apps/pilot/runner.py` 의 `on_order`).

---

## 7. 오류 코드

| code | HTTP | 뜻 / 대응 |
|---|---|---|
| `UNAUTHORIZED` | 401 | 키 없음/틀림/폐기됨 |
| `INVALID_REQUEST` | 400 | 입력 오류 (메시지 참고) |
| `INVALID_SYMBOL` | 400 | 없는 종목 |
| `INVALID_TICK_SIZE` | 400 | 호가단위 위반 |
| `UNSUPPORTED_ORDER_TYPE` / `UNSUPPORTED_ROUTE` | 400 | 미국 시장가, 미지원 경로 |
| `MARKET_NOT_SUPPORTED` | 400 | 실계좌 또는 서브계좌가 그 시장 미지원 |
| `MARKET_CLOSED` | 400 | 정규장 아님, 휴장일 |
| `ACCOUNT_TRADING_DISABLED` | 400 | 실계좌 거래 스위치 꺼짐 (관리자) |
| `SUB_ACCOUNT_SUSPENDED` | 400 | 서브계좌 정지 |
| `HALTED` | 400 | 긴급 정지 중 (`me()["halted"]`) |
| `RISK_LIMIT_EXCEEDED` | 400 | 리스크 한도 |
| `INSUFFICIENT_CASH` / `INSUFFICIENT_POSITION` | 400 | 가용 현금/매도 가능 수량 부족 |
| `ORDER_NOT_FOUND` | 404 | 내 주문이 아님 또는 없음 |
| `ORDER_NOT_CANCELABLE` | 400 | 이미 끝났거나 취소 중, 정정은 지정가만 |
| `BROKER_REJECTED` | 400 | KIS 가 정정 거부 |
| `BROKER_UNAVAILABLE` | 503 | KIS 연결/응답 문제. 잠시 뒤 재시도 |
| `SUBSCRIPTION_LIMIT` | - | 실시간 등록 한도 (WS ERROR) |

KIS 가 접수를 거부한 주문은 예외가 아니라 `status="REJECTED"`, `reject_reason` 으로 돌아옵니다.

---

## 8. 새 파일럿/프로그램 만들기

### 8.1 서브계좌와 키 준비 (관리자 작업)
`apps/pilot/__main__.py` 의 `setup` 을 본뜨면 됩니다. 관리자 API(`X-Admin-Key`, 키는 secrets.toml 에서 코드로 읽고 출력하지 않음):
1. `POST /admin/sub-accounts` `{"broker_account_id": "paper", "name": "<이름>", "type": "app", "markets": ["KR"]}`
2. `POST /admin/transfers` `{"from": <미배정 id>, "to": <id>, "currency": "KRW", "amount": "5000000"}`
3. `PUT /admin/sub-accounts/{id}/risk-limits` `{"market": "KR", "max_order_amount": "...", "max_open_orders": 4, "allowed_symbols": [...]}`
4. `POST /admin/sub-accounts/{id}/api-keys` → 응답의 `api_key` 는 **이때 한 번만** 보인다. `%CENTRAL_HOME%\secrets\apps\<이름>.toml` 에 저장

같은 실계좌의 서브계좌끼리 같은 종목을 반대로 주문하면 자기 주문끼리 체결될 수 있습니다(현재 막지 않음). 기존 PILOT 과 종목을 나누는 것이 안전합니다.

### 8.2 감시자에 등록
`%CENTRAL_HOME%\config\central.toml`:
```toml
[[ops.programs]]
name = "pilot2"                       # 소문자, 숫자, -, _
args = ["-m", "apps.pilot2", "run"]   # 저장소 루트에서 같은 파이썬으로 실행
```
감시자를 재시작하면(`python -m central service stop` 후 `start`) 함께 뜨고, 죽으면 다시 뜨고 Discord 로 알립니다.

### 8.3 감시자 아래에서 지킬 것
| 항목 | 방법 |
|---|---|
| 정상 종료 | 환경변수 `CENTRAL_STOP_FILE` 경로에 파일이 생기면 정리하고 끝낸다 (Windows 에서 Ctrl+C 대신) |
| 로그 | `central.core.logging.setup_logging(settings, name="<이름>", console=not os.environ.get("CENTRAL_SUPERVISED"))` → `logs\<이름>.log`, 키는 자동 마스킹. 콘솔 출력은 `logs\<이름>.console.log` 로 간다 |
| 서버 대기 | 서버가 아직 안 떴을 수 있으니 시작 시 `me()` 를 재시도 |
| 재접속 | 스트림이 끊기면 백오프(1초→30초)로 다시 붙고 `WELCOME` 을 받으면 REST 로 상태 재동기화 |
| 상태/보고서 | `%CENTRAL_HOME%\apps\<이름>\` (`settings.apps_dir`) |
| 알림 | `central.ops.notify.Notifier(webhook, source="<이름>")`. 웹훅은 자기 설정 파일에서 읽는다 |

### 8.4 파일럿 예제 구조 (`apps/pilot/`)
- `strategy.py`: 네트워크 없는 순수 로직 (이동평균 신호, 실시간/REST 1분봉 비교). 테스트는 여기에
- `runner.py`: `Pilot` 클래스. `stream_loop` (수신, 재접속), `clock_loop` (5초마다 시간 기반 작업), `on_order` (주문 상태 반영), `probe` (접수→정정→취소 시험), `check` (보유/주문 자체 점검), `report` (일일 보고)
- `__main__.py`: `setup` / `run` / `report` 명령

테스트는 `tests/test_ops.py` 의 파일럿 부분처럼 네트워크 없이 로직만 검증합니다. 서버 전체를 띄우는 테스트가 필요하면
`tests/test_reference.py` 처럼 `CentralApp(... brokers={"paper": SimBroker("paper"), ...})` 와 `httpx.ASGITransport` 를 씁니다
(`CentralClient(base_url, key, transport=...)` 로 SDK 를 그대로 붙일 수 있음).
