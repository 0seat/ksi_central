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

## 사용 예 (계획)
```python
a = CentralClient(url, api_key="서브계좌 A 키")
await a.buy("KR:005930", qty=10, price="61000")
await a.balance()          # A의 잔고만 보입니다

b = CentralClient(url, api_key="서브계좌 B 키")
await b.buy("US:AAPL", qty=3, price="187.25")
```

## 진행 상황
| 단계 | 내용 | 상태 |
|---|---|---|
| M0 | 프로젝트 셋업 | 완료 |
| M1 | KIS 연결 (인증, REST, WebSocket, 점검 도구) | 진행 중 |
| M2 | 서브계좌 코어 (원장, 주문 귀속, 상태머신) | 완료 (가상 브로커로 검증) |
| M3 | KIS 주문 연동, 대사 | 예정 |
| M4 | API 서버 (REST + WebSocket) | 예정 |
| M5~ | 데이터 서비스, 운영, 전략 러너, 웹 대시보드 | 예정 |

설계 상세: [docs/PLAN.md](docs/PLAN.md)

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
