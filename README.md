# 중앙증권 (Central Securities)

**하나의 증권 계좌를 여러 개의 독립된 서브계좌로 나눠 쓰는 서버**입니다.

한국투자증권(KIS) Open API는 계좌 하나에 앱키 하나만 붙일 수 있습니다. 그래서 같은 계좌에서 여러 전략이나 프로그램을 동시에 돌리면 문제가 생깁니다.
- 누가 무엇을 샀는지 구분할 수 없습니다.
- 토큰과 호출 한도가 서로 충돌합니다.
- 한 전략이 다른 전략의 주식을 팔아버릴 수 있습니다.

중앙증권은 KIS 연결을 혼자 맡고, 그 위에 **가상 서브계좌**를 만들어 각 전략에 하나씩 나눠 줍니다.

```
                  ┌──────────────────── 중앙증권 ────────────────────┐
 KIS 실계좌  ◀──▶ │  KIS 연결 1개 (토큰 · 호출 한도 · 실시간 시세)     │
                  │                                                  │
                  │   서브계좌 A        서브계좌 B        서브계좌 C   │
                  │   예수금 300만      예수금 500만      USD 2,000   │
                  │   삼성전자 10주     SK하이닉스 3주    AAPL 5주    │
                  └──────┬──────────────────┬────────────────┬───────┘
                         │                  │                │
                      전략 A             전략 B          외부 프로그램
```

## 핵심 기능
- **서브계좌 분리**: 서브계좌마다 예수금(KRW/USD), 보유종목, 주문, 체결, 손익을 따로 관리합니다. 전략 A는 A의 잔고만 보고 A의 주식만 팔 수 있습니다.
- **주문 귀속**: 모든 주문과 체결을 해당 서브계좌로 정확히 나눕니다. 서브계좌 장부의 합계는 실계좌 잔고와 주기적으로 대사(검증)합니다.
- **서브계좌별 리스크 한도**: 주문금액, 보유금액, 일일 손실 한도를 따로 걸고, 한 서브계좌만 정지할 수 있습니다.
- **데이터 제공**: 실시간 시세와 과거 캔들을 한 번만 받아서 여러 서브계좌에 나눠 줍니다.
- **여러 실계좌**: 실계좌마다 서브계좌를 만들 수 있습니다. 모의투자 계좌와 실전 계좌는 같은 코드로 동작합니다.

## 대상 시장
- 국내주식: KRX, NXT. 주문은 SOR(최선집행)이 기본입니다.
- 미국주식: NASDAQ, NYSE, AMEX
- 정규장만 지원합니다.

## 사용 예
```python
import os
from central.client import CentralClient

# 키는 코드에 적지 말고 환경변수 등 코드 밖에서 읽는다
async with CentralClient(os.environ["CENTRAL_URL"], os.environ["CENTRAL_API_KEY"]) as a:
    await a.buy("KR:005930", qty=10, price="61000")   # 지정가
    await a.buy("KR:005930", qty=1)                   # 가격 생략 시 시장가 (국내만)
    await a.balance()                                 # A의 잔고만 보입니다
    async for msg in a.stream(["tick:KR:005930", "candle.1m:US:AAPL"]):  # 주문/체결/잔고 이벤트 + 시세
        print(msg["ch"], msg["data"])
```

시세 데이터: `/market/quote`, `/market/orderbook`, `/market/candles?interval=1m|5m|15m|30m|60m|1d|1w|1M`,
`/market/ticks`, `/market/calendar`, `/market/symbols` (SDK: `quote`, `orderbook`, `candles`, `ticks`, `calendar`, `symbols`).
실시간 채널은 `tick:`, `orderbook:`, `candle.1m:`.

### 과거 시세 보관소
KIS 는 1분봉을 국내 약 1년, 미국 약 1개월만 보관하고 지수 1분봉은 최근 약 100개만 줍니다. 보관소는 이를 매일 받아
`CENTRAL_HOME\data\history` 에 Parquet 로 쌓아, KIS 에서 사라진 뒤에도 쓸 수 있게 합니다.
- **원주가만 저장**하고 분할, 병합, 무상증자, 배당은 **기업행위 이벤트**(기준일, 비율)로 따로 저장합니다.
  이벤트는 KIS 원주가 일봉과 수정주가 일봉의 비율이 바뀌는 날에서 찾습니다. 조회할 때 `adjust` 로 고릅니다.
  - `none` 원주가, `split` 분할/병합 등 자본변동만, `total` 배당까지 (국내 수정주가는 배당을 반영하지 않아 split 과 같음)
- 조회: `/market/history/{symbol}?interval=1m|1d&adjust=...` (SDK: `history`). symbol 예: `KR:005930`, `US:AAPL`,
  `INDEX:KR:0001`(코스피), `INDEX:KR:1001`(코스닥), `INDEX:US:COMP`(나스닥 종합), `INDEX:US:SPX`(S&P500)
- 대상: `central.toml` 의 `[history]` 목록, 나스닥100 구성종목(기본 켬, 구성은 nasdaq.com 에서 매일 갱신),
  앱이 API 로 등록한 종목/지수. 처음 보는 대상은 서버가 백그라운드로 끝까지 받고(그 시장 장중이면 마감 후),
  이후 장 마감 후 매일 이어 받습니다. 국내 1년치 1분봉은 종목당 약 15분, 미국 1개월치는 약 4분.
  - 등록 `POST /market/history-subscriptions {"symbol": "KR:005930"}`, 취소 `DELETE /market/history-subscriptions/KR:005930`,
    목록과 상태 `GET /market/history-subscriptions?mine=true` (SDK: `history_subscribe`, `history_unsubscribe`,
    `history_subscriptions`). 취소해도 보관본은 남고, 다른 앱이나 설정이 등록했으면 수집은 계속됩니다.
- 수동 받기: `python -m central history fetch KR:005930 US:AAPL INDEX:KR:0001` 또는 `--top-kr 10 --top-us 10`
- 확인: `python -m central history list`, `history events US:AAPL`, `history show US:AAPL --adjust split`

## 서버 실행
```powershell
python -m central gen-admin-key   # 관리자 키 생성 (secrets.toml 에 기록, 화면에 출력하지 않음)
python -m central serve           # API 서버 (처음 실행 시 실계좌 잔고를 미배정 서브계좌로 가져옴)
python -m central supervise       # 서버 + 추가 프로그램을 감시하며 실행 (상시 운영은 아래 '운영')
```
- API 문서: `http://127.0.0.1:8700/api/v1/docs`
- 관리자 API(`/api/v1/admin/...`, 헤더 `X-Admin-Key`)로 서브계좌 생성, 자금 배정, API 키 발급, 거래 스위치, 긴급 정지를 합니다.
- 실계좌에 돈을 넣거나 뺄 때는 `POST /admin/deposits`, `POST /admin/withdrawals` 로 장부에 반영합니다.
  입금은 그날 대사(국내 15:45, 미국 16:20 ET) 전에, 출금은 실제로 빼기 전에 반영해야 대사 불일치로 주문이 정지되지 않습니다.
- 서브계좌 프로그램은 발급받은 키(헤더 `X-API-Key`)로 `/api/v1/me/...`, `/api/v1/market/...`, `/api/v1/ws` 를 씁니다.
- 바인딩은 `127.0.0.1` 과 Tailscale 주소(100.64.0.0/10)만 허용합니다.

## 운영 (상시 실행)
```powershell
python -m central service install   # 작업 스케줄러 등록 (관리자 권한 PowerShell, 한 번만)
python -m central service start     # 시작
python -m central service status    # 감시자, 서버, 추가 프로그램 상태
python -m central service stop      # 정상 종료 (자동 재실행도 멈춤)
python -m central notify-test       # Discord 알림 시험
```
- 감시자(`python -m central supervise`)가 서버와 `[ops] programs` 의 프로그램을 띄우고, 끝나면 다시 띄우고 Discord 로 알립니다.
  작업 스케줄러는 부팅 때와 5분마다 감시자가 떠 있는지 확인합니다.
- 알림: `secrets.toml` 의 `[discord] webhook_url`. 대사 불일치, 실시간 세션 끊김, 장중 시세 멈춤, 긴급 정지,
  매핑 없는 체결, 오류 로그, 프로세스 종료, 일일 보고를 보냅니다. 같은 알림은 15분에 한 번만 보냅니다.
- 일정 작업: 07:00 DB 백업과 틱 파일 합치기, 07:20 휴장일/종목 마스터 갱신, 08:40 장 시작 전 점검,
  15:45 국내 동기화/대사/일일 보고, 20:10 국내 미체결 소멸, 미국 16:20 ET 동기화/미체결 소멸/대사.
  상태와 수동 실행은 `GET /admin/ops`, `POST /admin/ops/jobs/{name}/run`.
- 긴급 정지 상태는 DB 에 남아 재시작해도 유지됩니다. 해제는 `POST /admin/kill-switch` 로만 합니다.

## 파일럿 (예제 서브계좌 프로그램)
`apps/pilot` 은 SDK 와 서브계좌 키만 쓰는 별도 프로그램입니다. 모의 계좌에서 국내 2종목을 소량 매매하고,
30분마다 접수/정정/취소를 시험하고, 장 마감 후 실시간 1분봉과 REST 1분봉을 비교한 보고서를 남깁니다.
```powershell
python -m apps.pilot setup          # 서브계좌, 예산, 한도, 키 준비 (서버 실행 중)
# central.toml 에 [[ops.programs]] name = "pilot", args = ["-m", "apps.pilot", "run"] 를 넣으면 감시자가 함께 띄운다
```

## 진행 상황
| 단계 | 내용 | 상태 |
|---|---|---|
| M0 | 프로젝트 셋업 | 완료 |
| M1 | KIS 연결 (인증, REST, WebSocket, 점검 도구) | 완료 |
| M2 | 서브계좌 코어 (원장, 주문 귀속, 상태머신) | 완료 |
| M3 | KIS 주문 연동, 대사 | 완료 (국내 모의/실전 실거래 검증, 미국 실거래 검증 남음) |
| M4 | API 서버 (REST + WebSocket, API 키, SDK) | 완료 (모의 계좌 SDK 실주문 검증) |
| M5 | 데이터 서비스 (실시간 시세 허브, 틱 저장, 캔들, 호가, 영업일, 종목 마스터) | 구현 완료 (1주 무중단 수집 검증은 M6 서비스 등록 후) |
| M6 | 운영 (일정 작업, 감시자와 상시 실행, Discord 알림, 백업, 정지 영속화, 파일럿) | 구현 완료 (1주 시험 운영 중) |
| M7~ | 전략 러너, 실전 확대, 웹 대시보드 | 예정 |

설계 상세: [docs/PLAN.md](docs/PLAN.md) · 서브계좌 프로그램 개발(SDK/API): [docs/SDK.md](docs/SDK.md)

## 설치
Windows와 Python 3.12 이상이 필요합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pre-commit install
```

## 설정
설정과 키는 저장소 밖 `CENTRAL_HOME`에 둡니다. 기본 경로는 `C:\ProgramData\CentralSecurities`입니다.

```powershell
python -m tools.init_home      # 설정 폴더 생성
# %CENTRAL_HOME%\secrets\secrets.toml 에 앱키와 계좌번호 입력
python -m central config       # 설정 확인
python -m tools.kis_check      # KIS 연결 점검
```

## 테스트
```powershell
python -m pytest
```
