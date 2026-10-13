# 중앙증권 (Central Securities) 구축 계획서

- 작성일: 2026-10-13
- 버전: v0.3
- 운영 환경: Windows 24/7 서버 (Tailscale 망 내부)
- 공개 범위: **GitHub public 저장소 예정**. 비밀정보는 저장소에 절대 포함하지 않는다 (11장)

### 변경 이력
| 버전 | 내용 |
|---|---|
| v0.1 | 초안 |
| v0.2 | 미국주식 포함, 실계좌 구성, 시간외 제외, SOR 기본, Tailscale 전용, 웹 대시보드, 다통화 원장 |
| v0.3 | KIS 공식 GitHub 샘플 기준으로 TR ID와 제약 확정, 미국 실시간 확인, 모의/실전 구분 없는 단일 설계, **통합증거금 사용** 반영, 장전 동시호가 제외, **public 저장소 비밀정보 설계** 추가 |

### 참고 자료
- KIS 공식 샘플: <https://github.com/koreainvestment/open-trading-api> (`examples_llm/`, `examples_user/`)
- KIS Developers 포털: <https://apiportal.koreainvestment.com>
- 공식 샘플은 **참고용**으로만 쓴다. 예를 들어 해외 분봉 샘플의 연속조회 로직에는 커서를 진행시키지 않는 버그가 있다. 클라이언트는 직접 구현한다.

---

## 1. 개요

### 1.1 목적
KIS 계좌는 **하나의 앱키로만 접근**할 수 있다. 하나의 실계좌에서 여러 전략과 프로그램을 **서로 독립적으로** 운용하기 위해,
KIS 연결을 독점하고 그 위에 **가상 서브계좌**를 제공하는 중앙 서버 **"중앙증권"**을 구축한다.

### 1.2 중앙증권의 3대 역할
| # | 역할 | 설명 |
|---|---|---|
| 1 | **KIS 게이트웨이** | 실계좌별로 토큰, REST, WebSocket 연결을 하나만 유지하고 호출 한도를 관리한다 |
| 2 | **서브계좌 시스템 (OMS)** | 서브계좌별 예수금(KRW/USD), 보유종목, 주문, 체결, 손익을 독립된 장부로 관리하고 실계좌와 대사한다 |
| 3 | **데이터 서비스** | 국내와 미국의 실시간 시세 및 과거 데이터를 KIS에서 받아 서브계좌에 제공한다 |

### 1.3 1차 범위
| 항목 | 범위 |
|---|---|
| 시장 | **국내주식** (KRX + NXT, SOR 주문), **미국주식** (NASDAQ / NYSE / AMEX) |
| 실계좌 | 2개 (KIS 모의투자 1, 실전 1). **설계상 구분하지 않는다** (1.4) |
| 거래 시간 | **정규장만**. 장전 동시호가, 시간외(장전/장후/단일가), NXT 프리/애프터마켓, 미국 프리/애프터/주간거래는 모두 제외 |
| 국내 주문 경로 | **SOR(최선집행) 기본** |
| 증거금 | 실전 계좌는 **통합증거금 사용 중** (6.2) |
| 접속 | **Tailscale 망 내부에서만** |
| 관리 UI | 1차는 API와 Swagger. 웹 대시보드는 M9 |
| 수수료·세율 | 설정 자리만 마련하고 값은 사용자가 추후 기입·보정 |

### 1.4 모의/실전 단일 설계 원칙
- 코드에서 모의와 실전을 **분기하지 않는다.** 둘 다 같은 `KisBroker`로 처리한다.
- 차이는 **실계좌 설정(profile)**에만 둔다.
  - `env`: `real` | `paper` → 도메인과 TR ID 접두어(`T`↔`V`)만 바뀐다
  - `capabilities`: 지원 시장(`KR`, `US`), 지원 경로(`KRX`, `NXT`, `SOR`), 지원 주문유형, 초당 호출 한도
- KIS 모의투자가 현재 국내만 지원하더라도, 설계는 **양쪽 모두 국내와 미국을 지원**하는 것으로 한다. 지원하지 않는 기능은 capability에서 꺼 두기만 하면 된다. KIS가 모의 지원을 넓히면 설정만 바꾼다.
- 모든 실계좌에 `trading_enabled` 안전 스위치를 둔다.

### 1.5 비목표 (1차 제외)
- 시간외 거래 전체, 장전 동시호가
- 선물옵션, 기타 해외시장 (구조는 확장 가능하게)
- 서브계좌 간 내부 상계 체결
- 미수, 신용 (서브계좌는 현금 100%)
- 소수점 거래, 미국 LOO/LOC/MOO/MOC 주문 (2차 검토)

---

## 2. 용어

| 용어 | 의미 |
|---|---|
| **실계좌 (Broker Account)** | KIS에 실제로 존재하는 계좌. 앱키 1개와 1:1 |
| **서브계좌 (Sub-Account)** | 중앙증권 내부의 가상 계좌 |
| **미배정 (Unallocated)** | 어느 서브계좌에도 배정되지 않은 자산. 실계좌마다 자동 생성되는 시스템 서브계좌 |
| **클라이언트** | 서브계좌를 사용하는 주체 (내부 전략, 외부 프로그램) |
| **대사** | 서브계좌 합계와 미배정분을 더한 값이 KIS 실잔고와 같은지 검증 |
| **홀드 (Hold)** | 미체결 주문용으로 선점한 예수금 또는 보유수량 |
| **SOR** | KRX와 NXT 중 최선 가격으로 자동 라우팅 |
| **통합증거금** | KRW와 외화를 합쳐 증거금으로 쓰는 KIS 계좌 기능. 원화만 있어도 미국주식 매수가 가능하다 |
| **종목 식별자** | `KR:005930`, `US:AAPL` |

---

## 3. 시스템 아키텍처

```
                       ┌──────────────────────── 중앙증권 (단일 프로세스) ────────────────────────┐
                       │                                                                         │
 KIS REST  ◀────────▶  │  [Broker Gateway]   계좌별: 토큰 관리 · Rate Limiter · REST 클라이언트   │
 KIS WebSocket ◀────▶  │                     계좌별: WS 연결 · 재연결 · 체결통보 복호화            │
                       │                     시장 어댑터: KR(KRX/NXT/SOR) · US(NASD/NYSE/AMEX)    │
                       │          │                                                              │
                       │  [Market Data Service]   구독 통합(ref-count) · 스냅샷 캐시 · 캔들 캐시   │
                       │  [OMS / Sub-Account]     주문 라우팅 · 귀속 · 상태머신 · 다통화 원장      │
                       │  [Risk Engine]           서브계좌별/실계좌별 한도 · Kill Switch          │
                       │  [Reconciler]            주기적 대사 · 불일치 시 거래 정지               │
                       │  [Scheduler]             국내/미국 장 운영 스케줄 · 토큰 갱신 · 정산      │
                       │  [Event Bus]             내부 이벤트 발행/구독                            │
                       │  [Strategy Runner]       내부 전략 플러그인 실행                          │
                       │  [API Server]            REST + WebSocket (FastAPI)                     │
                       │          │                                                              │
                       │  [Storage]  SQLite(WAL): 주문·체결·원장·설정 / Parquet: 틱·캔들          │
                       └──────────┼──────────────────────────────────────────────────────────────┘
                                  │  Tailscale 전용 · HTTP / WebSocket · 서브계좌 API 키 인증
              ┌───────────────────┼────────────────────┬──────────────────┐
         내부 전략 A          외부 프로그램 B       웹 대시보드(M9)     스크립트/Jupyter
```

### 설계 원칙
1. **KIS와 통신하는 것은 중앙증권뿐이다.**
2. **서브계좌 장부가 원본이다.** KIS 잔고는 검증용이다.
3. **모든 주문은 같은 경로를 거친다.** `리스크 → OMS → Broker`
4. **Broker는 인터페이스로 추상화한다.** `KisBroker`(모의와 실전 공용), `SimBroker`(테스트·백테스트)
5. **시장 차이는 시장 어댑터 안에 가둔다.**
6. **모든 상태 변화는 DB에 기록하고, 재시작 시 복구한다.**
7. **비밀정보는 코드 저장소 밖에 둔다.** (11장)

---

## 4. 기술 스택

| 구분 | 선택 | 비고 |
|---|---|---|
| 언어 | Python 3.12 | 서버에 3.12.10 설치 완료 (`%LOCALAPPDATA%\Programs\Python\Python312`) |
| 가상환경 | `venv` (`.venv/`, git 제외) | |
| API 서버 | FastAPI + uvicorn | |
| HTTP/WS 클라이언트 | httpx, websockets | |
| DB | SQLite (WAL) + SQLAlchemy 2.0 + Alembic | |
| 금액 | `decimal.Decimal` | float 금지 |
| 시계열 | Parquet (pyarrow), DuckDB | |
| 시간대 | `zoneinfo` (+ `tzdata` 패키지, Windows 필요) | KST, America/New_York |
| 암호화 | pycryptodome | 체결통보 AES-256-CBC 복호화 |
| 설정 | pydantic + tomllib (`central/settings.py`) | `CENTRAL_HOME`의 TOML만 로드. 저장소 내부 경로 거부 |
| 테스트 | pytest, pytest-asyncio | |
| 비밀 유출 방지 | gitleaks + pre-commit | 11장 |
| 서비스화 | NSSM 또는 WinSW | |
| 알림 | Telegram Bot | |

---

## 5. 단계 1: KIS 연결 확인

### 5.1 접속 정보 (공식 샘플 `kis_devlp.yaml` 기준)
| env | REST | WebSocket |
|---|---|---|
| real | `https://openapi.koreainvestment.com:9443` | `ws://ops.koreainvestment.com:21000` |
| paper | `https://openapivts.koreainvestment.com:29443` | `ws://ops.koreainvestment.com:31000` |

### 5.2 확인된 KIS 제약 (공식 샘플과 문서 기준)
| 항목 | 내용 | 대응 |
|---|---|---|
| 접근토큰 | `POST /oauth2/tokenP`. **1일 1회 발급이 원칙**이고 유효기간은 약 24시간이다. 6시간 내 재요청하면 기존 토큰이 반환된다. 재발급은 1분에 1회로 제한되고, 잦은 발급은 이용 제한 사유가 된다 | 토큰을 디스크에 캐시(저장소 밖)하고, 만료 임박 시에만 갱신 |
| WS 접속키 | `POST /oauth2/Approval` (body: `appkey`, `secretkey`) | 연결 시마다 발급 |
| 초당 호출 | 공식 샘플 기준 실전 약 **20건/초**(50ms 간격), 모의 약 **2건/초**(500ms 간격). 초과 시 `EGW00201` | 계좌별 토큰버킷. 한도는 profile 설정값으로 두고 포털 공지로 보정 |
| WS 구독 | 세션당 약 **40건** 등록 (공식 샘플 상한). 국내와 미국 구독이 이 한도를 함께 쓴다 | 구독 허브가 슬롯을 관리 |
| WS 유지 | `PINGPONG` 메시지를 받으면 그대로 pong으로 응답 | |
| 체결통보 | AES-256-CBC + base64. 구독 응답에 key/iv 포함 | |
| 휴장일 조회 | `CTCA0903R`. **1일 1회 호출 권장** (원장 시스템 부하) | 하루 한 번 캐시 |
| 연속조회 | 응답 헤더 `tr_cont`가 `M`/`F`이면 다음 페이지가 있다. 요청 헤더 `tr_cont=N`과 `CTX_AREA_*` 키를 넘긴다 | 공통 페이지네이터 |
| 잔고 페이지 크기 | 실전 50건, 모의 20건 | |

#### M1 실측 결과 (2026-10-13)
| 항목 | 결과 | 반영 |
|---|---|---|
| 실전 호출 한도 | 18건/초 설정 시 `EGW00201` 연속 발생. 실제로는 **초당 약 2~3건** | profile `rate_limit_per_sec = 2` |
| 실전 WS 구독 | **3건**에서 `OPSP0008 MAX SUBSCRIBE OVER` | profile `ws_max_subscriptions = 3` |
| 모의 호출 한도 | 0.5초 간격에서도 `EGW00201` 빈발 | profile `rate_limit_per_sec = 1` |
| 모의 휴장일 조회 | `EGW02006 모의투자 TR 이 아닙니다` | `KR_HOLIDAY` 모의 미지원 처리 |
| 미국 실시간 체결 `HDFSCNT0` | 실시간 수신 확인. **필드 26개** (공식 샘플 컬럼 목록 25개 + 맨 앞 `RSYM`) | M5 파서에 반영 |
| 미국 호가 `HDFSASP0` | **필드 71개** (공식 샘플은 1호가 16개만 정의) | M5에서 필드 정의 확인 |

> 실전 제한(초당 2~3건, 구독 3건)은 신규 앱키에 걸리는 임시 제한으로 추정한다. 공식 문서에서는 근거를 찾지 못했다. 며칠 뒤 `kis_check`로 다시 측정하고 설정값을 올린다. 구독 3건으로는 실시간 운영이 불가능하므로 **M5 전에 반드시 해소**해야 한다.

### 5.3 TR ID 확정표: 국내주식
| 기능 | 경로 | TR ID (real / paper) | 비고 |
|---|---|---|---|
| 현재가 | `/uapi/domestic-stock/v1/quotations/inquire-price` | `FHKST01010100` | `FID_COND_MRKT_DIV_CODE`: `J`(KRX) / `NX`(NXT) / `UN`(통합) |
| 일/주/월봉 | `.../quotations/inquire-daily-itemchartprice` | `FHKST03010100` | 1회 최대 100건 |
| 당일 분봉 | `.../quotations/inquire-time-itemchartprice` | `FHKST03010200` | 당일만, 1회 30건 |
| 일별 분봉 (과거) | `.../quotations/inquire-time-dailychartprice` | `FHKST03010230` | **최대 1년 보관**, 1회 120건 (실전) |
| 잔고 | `/uapi/domestic-stock/v1/trading/inquire-balance` | `TTTC8434R` / `VTTC8434R` | `AFHR_FLPR_YN`: `N` 기본, `X` NXT |
| 매수가능 | `.../trading/inquire-psbl-order` | `TTTC8908R` / `VTTC8908R` | |
| 통합증거금 현황 | `.../trading/intgr-margin` | `TTTC0869R` | 국내·해외 주문가능금액 |
| 현금 주문 | `.../trading/order-cash` | 매수 `TTTC0012U` / `VTTC0012U`, 매도 `TTTC0011U` / `VTTC0011U` | `EXCG_ID_DVSN_CD`: `KRX` / `NXT` / `SOR` |
| 정정·취소 | `.../trading/order-rvsecncl` | `TTTC0013U` / `VTTC0013U` | `RVSE_CNCL_DVSN_CD` 01 정정, 02 취소 |
| 휴장일 | `.../quotations/chk-holiday` | `CTCA0903R` | `opnd_yn`(개장일 여부) 사용 |
| WS 체결가 | (WS) | `H0STCNT0`(KRX), `H0NXCNT0`(NXT), **`H0UNCNT0`(통합)** | |
| WS 호가 | (WS) | `H0STASP0`(KRX), `H0NXASP0`(NXT), **`H0UNASP0`(통합)** | |
| WS 체결통보 | (WS) | `H0STCNI0` / `H0STCNI9` | `CNTG_YN` 2=체결, 1=접수·정정·취소·거부. `ORD_EXG_GB`에 거래소 구분 |

### 5.4 TR ID 확정표: 미국주식
| 기능 | 경로 | TR ID (real / paper) | 비고 |
|---|---|---|---|
| 현재가 | `/uapi/overseas-price/v1/quotations/price` | `HHDFS00000300` | `EXCD`: `NAS` / `NYS` / `AMS` |
| 일/주/월봉 | `.../quotations/dailyprice` | `HHDFS76240000` | `GUBN` 0일/1주/2월, `MODP` 수정주가 |
| 분봉 | `.../quotations/inquire-time-itemchartprice` | `HHDFS76950200` | 1회 최대 120건, `KEYB`로 과거 방향 연속조회 |
| 주문 | `/uapi/overseas-stock/v1/trading/order` | 매수 `TTTT1002U` / `VTTT1002U`, 매도 `TTTT1006U` / `VTTT1006U` | `OVRS_EXCG_CD`: `NASD` / `NYSE` / `AMEX` |
| 정정·취소 | `.../trading/order-rvsecncl` | `TTTT1004U` / `VTTT1004U` | |
| 매수가능 | `.../trading/inquire-psamount` | `TTTS3007R` / `VTTS3007R` | |
| 체결기준 현재잔고 | `.../trading/inquire-present-balance` | `CTRP6504R` / `VTRP6504R` | `WCRC_FRCR_DVSN_CD` 01 원화 / 02 외화. 대사용 |
| 잔고 | `.../trading/inquire-balance` | `TTTS3012R` / `VTTS3012R` | |
| 통화별 증거금 | `.../trading/foreign-margin` | `TTTC2101R` | 통합증거금 확인용 |
| WS 체결가 | (WS) | `HDFSCNT0` | tr_key `DNASAAPL` (D + 거래소 + 티커) |
| WS 호가 | (WS) | `HDFSASP0` | 미국은 **1호가 무료 실시간** |
| WS 체결통보 | (WS) | `H0GSCNI0` / `H0GSCNI9` | tr_key는 HTS ID |

> **미국 실시간 확인 결과**: 공식 샘플(`overseas_stock/delayed_ccnl`)에 "미국의 경우 0분지연시세로 제공"이라고 명시되어 있다. **미국 체결가는 지연 없이 실시간**이다. 호가는 1호가(매수/매도 최우선)만 무료 실시간으로 제공된다. 사용자 확인 내용과 일치한다.

> **미국 주문유형**: 정규장 기준 `ORD_DVSN` `00`(지정가)만 범용으로 쓸 수 있고 **시장가는 없다**. 실전 전용으로 LOO(32)/LOC(34)(매수·매도), MOO(31)/MOC(33)(매도만)가 있다. 모의는 지정가만 된다. 1차는 지정가만 지원한다.

### 5.5 점검 도구 `kis_check`
`python -m tools.kis_check --account <alias> [--market kr|us] [--order-test]`
- 실계좌 profile의 capability에 따라 가능한 항목만 실행하고, 나머지는 `SKIP`으로 표시한다
- 항목: 토큰(발급, 캐시 재사용), WS 접속키, 현재가, 일봉, 분봉, 잔고, 매수가능, 통합증거금, WS 체결가와 호가, WS 체결통보, rate limit
- `--order-test`: 체결되지 않을 가격으로 지정가 1주 주문 → 접수 통보 확인 → 취소. 실전은 확인 프롬프트를 거친다
- 결과는 콘솔 표와 JSON으로 출력한다. **로그에 키, 토큰, 계좌번호는 마스킹**한다

### 5.6 산출물
- `central/broker/kis/auth.py`, `rest.py`, `ws.py`, `tr.py`(TR ID 테이블 단일 관리), `crypto.py`
- `tools/kis_check.py`
- 시장 어댑터(`kr.py`, `us.py`)는 M2~M3에서, WS 자동 재연결과 재구독은 M5 구독 허브에서 구현한다.

---

## 6. 단계 2: 서브계좌 시스템

### 6.1 계층 구조
```
중앙증권
 ├─ 실계좌 "paper"  (env=paper, capabilities: KR/KRX)
 │   ├─ 미배정
 │   └─ 서브계좌 …
 └─ 실계좌 "real"   (env=real,  capabilities: KR/KRX·NXT·SOR, US, 통합증거금)
     ├─ 미배정
     ├─ 서브계좌 "kr-scalp-A"
     ├─ 서브계좌 "us-trend-B"
     └─ 서브계좌 "app-X"
```
서브계좌 하나는 KRW 지갑과 USD 지갑을 함께 가진다.

### 6.2 통합증거금 반영 설계
통합증거금을 쓰면 **KRW만으로 미국주식을 매수**할 수 있고, KIS가 결제 시점에 자동으로 환전한다. 그래서 KIS의 USD 잔고는 서브계좌 장부와 그대로 맞지 않는다. 다음 규칙으로 처리한다.

| 항목 | 규칙 |
|---|---|
| 서브계좌 지갑 | KRW와 USD를 따로 둔다 (원장도 통화별) |
| 미국 매수 hold | ① USD 지갑에 잔액이 있으면 USD로 hold한다. ② 부족분은 **KRW 지갑에서 `부족 USD × 기준환율 × (1 + fx_buffer)`만큼 hold**한다 (`fx_buffer` 기본 2%, 설정값) |
| 미국 매수 체결 | USD 원장에 `BUY`를 기록한다. USD가 부족하면 **`FX_AUTO` 원장 항목**(KRW 차감, USD 증가)을 함께 기록한다. 환율은 1차에서 체결 시점 기준환율로 **잠정** 반영한다 |
| 환율 확정 | KIS 결제와 자동환전이 끝난 뒤(결제일), 실제 적용 환율로 `FX_AUTO`를 **정산(ADJUST)**한다. 원천 데이터는 체결기준잔고와 증거금 조회, 수동 입력 순으로 쓴다 |
| 미국 매도 체결 | USD 지갑에 입금한다. 원화로 전환하지 않는다 |
| 수동 환전 | `POST /admin/fx`로 기록한다 |
| 대사 | 종목 **수량은 엄격하게** 비교한다. 현금은 **KRW 환산 총액 기준**으로 허용오차 내에서 비교하고, 통화별 차이는 참고 정보로 남긴다 |
| 국내 매수 시 | KIS 통합증거금은 USD로 국내주식을 사는 것도 허용할 수 있다. 하지만 **서브계좌는 국내 주문을 KRW 지갑으로만 결제**하도록 제한한다. 장부를 단순하게 유지하기 위해서다 |

> 통합증거금 자동환전의 정확한 시점과 환율 산정, 그리고 이를 조회할 API는 M3에서 실계좌 데이터로 검증하고 확정한다.

### 6.3 데이터 모델
| 테이블 | 주요 컬럼 |
|---|---|
| `broker_accounts` | id, alias, broker, env, account_ref(**비밀 참조키**, 계좌번호 자체는 저장하지 않음), capabilities_json, trading_enabled, status |
| `sub_accounts` | id, broker_account_id, name, type(system/strategy/app), status(active/suspended/closed) |
| `api_keys` | id, sub_account_id, prefix, key_hash(argon2/sha256+salt), scopes, expires_at, revoked |
| `risk_limits` | sub_account_id, market, currency, max_order_amount, max_position_amount, max_daily_loss, max_open_orders, allowed_symbols |
| `instruments` | id(`KR:005930`), market, code, exchange(KRX/NASD/NYSE/AMEX), name, currency, tick_rule, lot_size, nxt_tradable |
| `cash_ledger` | id, sub_account_id, currency, type, amount, fx_rate, provisional, ref_type, ref_id, created_at |
| `holds` | id, sub_account_id, order_id, kind(cash/qty), currency, instrument_id, amount, status |
| `positions` | sub_account_id, instrument_id, qty, avg_price, realized_pnl, currency |
| `orders` | id, sub_account_id, client_order_id, broker_order_no, broker_org_no, orig_order_id, instrument_id, side, order_type, route, price, qty, filled_qty, avg_fill_price, status, reject_reason, created_at, updated_at |
| `fills` | id, order_id, sub_account_id, broker_fill_key, instrument_id, venue, side, price, qty, fee, tax, currency, filled_at |
| `fee_schedules` | broker_account_id, market, venue, side, fee_rate, min_fee, tax_rate, other_fees_json, effective_from |
| `reconciliations` | id, broker_account_id, market, run_at, result, diff_json |
| `events` | id, ts, type, payload_json |

**원장 type**: `DEPOSIT`, `WITHDRAW`, `TRANSFER_IN/OUT`, `FX`, `FX_AUTO`, `BUY`, `SELL`, `FEE`, `TAX`, `DIVIDEND`, `ADJUST`

### 6.4 주문 처리 흐름
```
클라이언트 ─ POST /orders ─▶ ① 검증 (서브계좌 상태, 종목, 호가단위, 정규장 여부, 계좌 capability)
                             ② 리스크 체크
                             ③ Hold 생성 (통합증거금 규칙 포함)             ── DB 트랜잭션
                             ④ orders 저장 (SUBMITTING, route 결정)
                             ⑤ KIS 주문 → 주문번호 + 주문조직번호 저장 (ACCEPTED)
KIS 체결통보 ─────────────▶ ⑥ 주문번호로 주문과 서브계좌 조회
                             ⑦ fills(venue 포함), 원장, positions 갱신, hold 차감
                             ⑧ 해당 서브계좌 클라이언트에만 이벤트 전달
```
- 정정과 취소에는 `KRX_FWDG_ORD_ORGNO`(주문조직번호)가 필요하므로 주문할 때 함께 저장한다.

### 6.5 시장별 주문 규칙
| 항목 | 국내 | 미국 |
|---|---|---|
| 기본 경로 | **SOR**. capability에 SOR이 없으면 KRX | `NASD`/`NYSE`/`AMEX`는 종목 마스터로 자동 결정 |
| 주문유형 (1차) | 지정가(`00`), 시장가(`01`) | **지정가(`00`)만** |
| 주문 허용 시간 (KST) | **09:00–15:20** (연속매매). 15:20–15:30 장마감 동시호가 허용 여부는 설정값(기본 허용) | 정규장 **23:30–06:00** (서머타임 22:30–05:00) |
| 장전 동시호가 08:30–09:00 | **거부** | 해당 없음 |
| 호가단위 | 가격대별 호가단위 | $1 이상 $0.01, $1 미만 $0.0001 |
| 결제 | T+2 | T+1 |
| 거래비용 | 수수료 + 거래세(매도) | 수수료 + SEC fee 등(매도) |

### 6.6 주문 상태머신 (M2 구현 기준)
```
(검증/리스크/홀드 실패 → 주문 생성 안 됨, OmsError 반환)

SUBMITTING ─▶ REJECTED        브로커 거부 (홀드 해제)
 │         └▶ UNKNOWN         타임아웃/연결 오류. 홀드 유지, M3 복구 루틴이 KIS 주문내역으로 확정
 ▼
ACCEPTED ─▶ PARTIALLY_FILLED ─▶ FILLED
 └──────┬─────────┘
        ├▶ CANCEL_PENDING ─▶ CANCELED   (취소 확인 통보 시 잔여 홀드 해제)
        ├▶ REPLACED                     정정: 기존 주문 종료, 새 주문(orig_order_id 연결, client_order_id "#A1")
        └▶ EXPIRED                      장 종료 후 미체결 잔량 소멸
```
- 정정은 KIS 처럼 새 주문번호를 받는 방식이라 별도 AMEND_PENDING 상태를 두지 않고, 락 안에서 기존 주문 REPLACED 와 새 주문 ACCEPTED 를 한 번에 기록한다.

### 6.6.1 금액 처리 규칙 (M2 구현)
| 항목 | 규칙 |
|---|---|
| 저장 | Decimal 을 문자열로 저장 (SQLite 실수 오차 방지) |
| 체결대금 | KRW 원 단위, USD 센트 단위 반올림 |
| 수수료/세금 | KRW 원 미만 절사, USD 센트 반올림 |
| 홀드 | 올림 (보수적). 매수 홀드 = 가격 x 수량 x (1 + 최대 수수료율 + 세율) |
| 국내 시장가 홀드 | 상한가 x 수량 기준 |
| 평균단가 | 0.0001 단위. 매수 수수료는 평균단가에 넣지 않고 원장 FEE 로 기록 |
| 실현손익 | (매도가 - 평균단가) x 수량 - 매도 수수료 - 세금 |
| 동시성 | 실계좌별 asyncio.Lock 으로 주문 접수와 체결통보 처리를 직렬화 (체결통보가 접수 응답보다 먼저 와도 안전) |

### 6.7 정합성 규칙
| 상황 | 처리 |
|---|---|
| 서브계좌 보유 이상 매도 | 거부 |
| 실계좌 주문가능금액 부족 | hold로 원칙적으로 방지. 발생하면 KIS 거부로 처리하고 알림 |
| 매핑 없는 체결 (HTS/MTS 수동매매) | 미배정으로 귀속하고 알림 |
| 재시작 | DB 미완료 주문 + KIS 당일 주문·체결 조회(국내, 미국)로 복구 |
| 대사 불일치 | 해당 실계좌×시장 신규 주문 정지, 알림, 관리자 보정 |
| 자기 주문끼리 체결 위험 | 경고 (1차) |
| 배당, 기업행위 | 대사에서 감지하면 관리자가 수동 반영 |

### 6.8 대사
- 주기: 각 시장 장중 5분, 각 시장 마감 후 상세 대사 1회
- 수량: `Σ서브계좌 + 미배정 = KIS 보유수량` (엄격)
- 현금: KRW 환산 총액 기준 허용오차 (통합증거금 때문, 6.2)

---

## 7. 단계 3: 데이터 서비스

### 7.1 실시간 시세
- 구독 허브가 ref-count로 KIS WS 구독을 통합한다.
- 국내는 **통합 시세(`H0UNCNT0`/`H0UNASP0`) 기본**이다. capability가 없는 계좌(모의)는 KRX 시세를 쓴다.
- 미국은 `HDFSCNT0`(실시간 체결)과 `HDFSASP0`(1호가)를 쓴다.
- **시세 수신 계좌 지정**: 시세는 계좌와 무관하므로, 시세 전용으로 쓸 실계좌(기본: real)를 하나 정한다. 이렇게 하면 다른 계좌의 WS 슬롯과 호출 한도를 아낄 수 있다. 체결통보는 각 계좌가 자기 연결로 받는다.
- 세션당 구독 약 40건을 넘으면 `SUBSCRIPTION_LIMIT`를 반환한다.
- 모든 틱은 Parquet로 저장한다.

### 7.2 과거 데이터
| 데이터 | 국내 | 미국 |
|---|---|---|
| 일/주/월봉 | `FHKST03010100` (1회 100건, 연속조회로 확장) | `HHDFS76240000` |
| 분봉 | `FHKST03010230` **최대 1년** + 자체 수집 | `HHDFS76950200` (1회 120건) + 자체 수집 |
| 틱 | 자체 수집만 | 자체 수집만 |
| 종목 마스터 | KIS 종목정보 파일 (KOSPI/KOSDAQ, NXT 거래가능) | KIS 해외 종목정보 파일 |
| 휴장일 | `CTCA0903R` (1일 1회) | 내장 캘린더 (조기폐장 포함) |
| 환율 | 기준환율 조회 | |

- 로컬 캐시를 우선 쓰고, 없는 구간만 KIS에 조회한다.

### 7.3 호출 예산
- 계좌별 우선순위 큐: `주문/취소 > 체결·잔고 > 시세 > 백필`
- 대량 백필은 해당 시장이 닫힌 시간대에 실행한다.
- 서브계좌별 데이터 요청 쿼터를 둔다.

---

## 8. API 설계 (v1 초안)

### 8.1 공통
- Base URL: `http://{tailscale-host}:{port}/api/v1`
- 인증: 서브계좌 `X-API-Key`, 관리자 `X-Admin-Key`
- 응답: `{ "ok": true, "data": … }` / `{ "ok": false, "error": { "code", "message" } }`
- 금액은 문자열 Decimal에 `currency` 필드를 함께 둔다. 시각은 ISO8601 KST
- 종목은 `KR:005930`, `US:AAPL` 형식
- 주문 멱등성은 `client_order_id`로 보장한다

### 8.2 관리자 API (`/admin`)
| Method | Path | 설명 |
|---|---|---|
| GET | `/admin/broker-accounts` | 실계좌 목록, capability, 연결 상태 |
| GET | `/admin/broker-accounts/{id}/health` | 토큰, WS, rate limit 상태 |
| PATCH | `/admin/broker-accounts/{id}` | `trading_enabled` 전환 |
| POST/GET | `/admin/sub-accounts` | 서브계좌 생성, 목록 |
| PATCH | `/admin/sub-accounts/{id}` | 상태와 이름 변경 |
| PUT | `/admin/sub-accounts/{id}/risk-limits` | 시장별 한도 |
| POST | `/admin/sub-accounts/{id}/api-keys` | API 키 발급 (평문은 1회만 반환) |
| DELETE | `/admin/api-keys/{key_id}` | 폐기 |
| POST | `/admin/transfers` | `{from, to, currency, amount}` |
| POST | `/admin/fx` | 수동 환전 반영 |
| POST | `/admin/positions/transfer` | 보유종목 이동 |
| POST | `/admin/adjustments` | 수동 보정 |
| GET/PUT | `/admin/fee-schedules` | 수수료·세율 |
| POST | `/admin/reconcile` | 즉시 대사 |
| GET | `/admin/reconciliations` | 대사 이력 |
| POST | `/admin/kill-switch` | `{scope, id, action: halt/resume, cancel_open_orders}` |

### 8.3 서브계좌 API
| Method | Path | 설명 |
|---|---|---|
| GET | `/me` | 정보, 상태, 한도, 사용 가능한 시장 |
| GET | `/me/balance` | 통화별 예수금, hold, 주문가능금액(통합증거금 반영), 평가, 손익, KRW 환산 |
| GET | `/me/positions?market=` | 보유종목 |
| GET | `/me/ledger?currency&from&to&type` | 원장 |
| GET | `/me/pnl?from&to&market` | 손익 |
| POST | `/me/orders` | 주문 |
| GET | `/me/orders?status&symbol&market&from&to` | 주문 목록 |
| GET | `/me/orders/{id}` | 상세 |
| POST | `/me/orders/{id}/cancel` | 취소 |
| POST | `/me/orders/{id}/amend` | 정정 |
| POST | `/me/orders/cancel-all?market=` | 전체 취소 |
| GET | `/me/fills?…` | 체결 |

주문 예:
```json
{ "client_order_id": "krA-0001", "symbol": "KR:005930", "side": "buy",
  "order_type": "limit", "price": "61000", "qty": 10, "route": "SOR" }
{ "client_order_id": "usB-0001", "symbol": "US:AAPL", "side": "buy",
  "order_type": "limit", "price": "187.25", "qty": 3 }
```

### 8.4 데이터 API (`/market`)
| Method | Path | 설명 |
|---|---|---|
| GET | `/market/symbols?q=&market=` | 종목 검색 |
| GET | `/market/quote/{symbol}?source=unified\|krx\|nxt` | 현재가 |
| GET | `/market/orderbook/{symbol}` | 호가 (미국은 1호가) |
| GET | `/market/candles/{symbol}?interval=1m\|5m\|1d\|1w&from&to` | 캔들 |
| GET | `/market/ticks/{symbol}?date=` | 저장 틱 |
| GET | `/market/calendar?market=&from&to` | 영업일 |
| GET | `/market/status?market=` | 장 상태 |
| GET | `/market/fx?pair=USDKRW` | 환율 |

### 8.5 실시간 WebSocket (`/ws`)
```json
→ { "op": "subscribe", "channels": ["tick:KR:005930", "orderbook:US:AAPL", "candle.1m:KR:005930"] }
← { "ch": "tick:KR:005930", "data": { "ts": "...", "price": "61000", "qty": 12, "venue": "KRX" } }
← { "ch": "orders" | "fills" | "account" | "system", "data": { ... } }
```
`orders`, `fills`, `account`, `system`은 자동 구독되며 자기 서브계좌 이벤트만 받는다.

### 8.6 에러 코드
`UNAUTHORIZED`, `SUB_ACCOUNT_SUSPENDED`, `ACCOUNT_TRADING_DISABLED`, `MARKET_NOT_SUPPORTED`, `INVALID_SYMBOL`, `INVALID_TICK_SIZE`, `UNSUPPORTED_ORDER_TYPE`, `UNSUPPORTED_ROUTE`, `MARKET_CLOSED`, `INSUFFICIENT_CASH`, `INSUFFICIENT_POSITION`, `RISK_LIMIT_EXCEEDED`, `DUPLICATE_CLIENT_ORDER_ID`, `ORDER_NOT_FOUND`, `ORDER_NOT_CANCELABLE`, `BROKER_REJECTED`, `BROKER_UNAVAILABLE`, `RATE_LIMITED`, `HALTED`, `SUBSCRIPTION_LIMIT`

### 8.7 클라이언트 SDK
```python
cs = CentralClient(url, api_key)
await cs.buy("KR:005930", qty=10, price="61000")
await cs.buy("US:AAPL", qty=3, price="187.25")
async for tick in cs.stream("tick:US:AAPL"): ...
```

### 8.8 네트워크 보안 (Tailscale)
- uvicorn은 Tailscale IP(100.x)와 `127.0.0.1`에만 바인딩한다. `0.0.0.0` 금지
- Windows 방화벽은 API 포트를 Tailscale 인터페이스에서만 허용한다
- Tailscale ACL로 접근 기기를 제한한다
- VPN 내부라도 API 키 인증은 유지한다

---

## 9. 리스크 관리
| 레벨 | 항목 |
|---|---|
| 서브계좌×시장 | 1회 주문금액, 종목당 보유금액, 일일 최대 손실(도달 시 자동 정지), 최대 미체결 수, 허용 종목, 초당 주문 수 |
| 실계좌 | `trading_enabled`, 통화별 일일 손실 한도, 주문 속도 |
| 전역 | Kill switch, 대사 불일치 시 자동 정지, 연결 끊김 시 해당 시장 신규 주문 차단 |

---

## 10. 운영 (Windows 24/7)

### 10.1 일일 스케줄 (KST)
| 시각 | 시장 | 작업 |
|---|---|---|
| 07:30 | KR | 휴장일(1일 1회), 토큰 확인, 종목 마스터 |
| 08:40 | KR | WS 구독, 대사, 주문 대기 (장전 동시호가 주문은 거부) |
| 09:00–15:30 | KR | 정규장 (주문 허용 09:00–15:20, 15:20–15:30은 설정) |
| 15:40 | KR | 미체결 만료 처리, 상세 대사, 일일 손익 |
| 16:00–20:00 | KR | 틱→분봉, 백필, DB 백업 |
| 21:30 | US | 휴장일, 종목 마스터, 토큰 유효성 |
| 개장 30분 전 | US | WS 구독, 대사 |
| 22:30/23:30–05:00/06:00 | US | 정규장 |
| 마감 +10분 | US | 미체결 만료, 상세 대사, 손익 |
| 07:00 | US | 백필, 백업 |

- 토큰은 1일 1회 발급이 원칙이므로, 국내장과 미국장 세션을 모두 덮을 수 있는 시점(예: 07:30)에 발급한다. 남은 유효시간이 부족할 때만 재발급한다.
- 점검과 재부팅은 주말에만 한다.

### 10.2 Windows 서버 체크리스트
- [x] Python 3.12.10 설치
- [x] Git 2.55 설치
- [x] `CENTRAL_HOME` 초기화 (`C:\ProgramData\CentralSecurities`, secrets/state 권한 제한)
- [ ] NSSM/WinSW 서비스 등록
- [ ] Windows Update 자동 재부팅 차단 (주말 수동 업데이트)
- [ ] 절전 해제, KST와 시간 동기화
- [ ] Tailscale 자동 시작, 방화벽 규칙
- [ ] 로그 로테이션, DB 백업, 텔레그램 알림

---

## 11. 비밀정보 관리 (public 저장소 대비)

### 11.1 원칙
**저장소에는 코드와 예시 설정만 둔다. 비밀정보와 운영 데이터는 저장소 밖 경로에만 둔다.**

### 11.2 비밀정보 분류
| 분류 | 예 | 위치 |
|---|---|---|
| KIS 자격증명 | 앱키, 앱시크릿, HTS ID | 저장소 밖 `secrets.toml` |
| 계좌 식별정보 | 계좌번호, 상품코드 | 같은 파일 (DB에는 alias만 저장) |
| 런타임 토큰 | 접근토큰, WS 접속키 | 저장소 밖 `state/` (WS 키는 메모리만) |
| 중앙증권 키 | 관리자 키, 서브계좌 API 키 | 관리자 키는 secrets, API 키는 DB에 **해시만** 저장 |
| 알림 | 텔레그램 봇 토큰, chat id | secrets |
| 운영 데이터 | DB, 틱 데이터, 로그 | 저장소 밖 데이터 디렉토리 |

### 11.3 디렉토리 분리
```
E:\01_project\26_tick_trading\        ← git 저장소 (public 가능)
  config\central.example.toml        ← 예시만 커밋
  config\secrets.example.toml        ← 키 이름만 있고 값은 비어 있음

%CENTRAL_HOME%  (기본: C:\ProgramData\CentralSecurities\)   ← 저장소 밖
  config\central.toml                ← 실제 설정
  secrets\secrets.toml               ← 실제 키 (NTFS 권한: 서비스 계정과 관리자만)
  state\tokens\                      ← 토큰 캐시
  data\central.db, data\ticks\      ← DB, 시세
  logs\
  backups\
```
- 실행 시 `CENTRAL_HOME` 환경변수로 위치를 지정한다. 저장소 안의 `.env`나 `secrets` 파일은 **읽지 않는다.** 실수로 만들어 커밋되는 상황 자체를 막기 위해서다.
- `secrets.toml` 예시:
  ```toml
  [accounts.real]
  env = "real"
  app_key = ""
  app_secret = ""
  hts_id = ""
  account_no = ""      # 8자리
  product_code = "01"

  [accounts.paper]
  env = "paper"
  ...
  ```

### 11.4 유출 방지 장치 (다층)
1. **`.gitignore`**: `.env*`, `*.toml`(단 `*.example.toml`은 허용), `secrets/`, `state/`, `data/`, `logs/`, `*.db`, `*.parquet`, `.venv/`, `KIS*` 토큰 파일 패턴
2. **pre-commit + gitleaks**: 커밋 전에 비밀 패턴을 검사한다. KIS 앱키 형식 규칙과 계좌번호 형태 규칙을 커스텀으로 추가한다
3. **GitHub 설정**: Secret scanning과 Push protection을 켠다
4. **CI (GitHub Actions)**: PR과 push마다 gitleaks를 실행한다
5. **로그 마스킹**: 로거 필터로 appkey, appsecret, token, 계좌번호, approval_key를 `****`로 치환한다. 예외 메시지와 HTTP 덤프에도 적용한다
6. **테스트 데이터**: 실제 KIS 응답을 테스트 픽스처로 저장할 때는 익명화 스크립트를 반드시 거친다 (계좌번호, 주문번호, 이름 치환)
7. **공개 전 점검**: public 전환 전에 전체 히스토리를 `gitleaks detect --log-opts="--all"`로 검사한다

### 11.5 유출 시 대응
- KIS 포털에서 앱키를 즉시 재발급(폐기)한다
- 중앙증권 관리자 키와 서브계좌 API 키를 회전한다
- 히스토리에 남은 경우 `git filter-repo`로 제거하고 강제 푸시한다. 키 재발급이 먼저다

---

## 12. 디렉토리 구조 (저장소)
```
26_tick_trading/
├── docs/
├── config/                  # *.example.toml만
├── central/
│   ├── main.py, settings.py
│   ├── core/                # 이벤트 버스, 도메인 모델, Money/Decimal, 캘린더, 로그 마스킹
│   ├── broker/
│   │   ├── base.py
│   │   ├── kis/ (auth, rest, ws, tr, kr, us, crypto)
│   │   └── sim.py
│   ├── oms/  risk/  reconcile/  marketdata/  scheduler/  strategies/
│   ├── api/  (admin, me, market, ws)
│   ├── storage/
│   └── notify/
├── sdk/central_sdk/
├── dashboard/               # M9
├── tools/ (kis_check.py, anonymize_fixture.py)
├── tests/
├── .gitignore  .gitleaks.toml  .pre-commit-config.yaml
├── .github/workflows/ci.yml
├── LICENSE  README.md  pyproject.toml
```

---

## 13. 로드맵
| 마일스톤 | 내용 | 완료 기준 |
|---|---|---|
| **M0** 셋업 ✅ | 저장소, venv, 설정 로더(CENTRAL_HOME), 로그 마스킹, gitleaks/pre-commit, CI | 비밀 커밋 시도가 차단됨, `pytest` 통과 (2026-10-13 완료) |
| **M1** KIS 연결 | auth/rest/ws/tr + `kis_check` | paper: 국내 항목 통과 / real: 국내·미국 조회와 WS 통과 (2026-10-13 조회·실시간 통과. 남은 것: 국내 장중 시세 수신, 모의 주문/취소 `--order-test`) |
| **M2** 서브계좌 코어 | 데이터 모델, 다통화 원장, 통합증거금 hold, 상태머신, SimBroker | A(국내)/B(미국)/C(혼합) 동시 매매 시나리오 통과 (2026-10-13 완료. 테이블 생성은 create_all, Alembic 마이그레이션은 M4 에서 도입) |
| **M3** KIS 주문 연동 | 체결통보 귀속, 재시작 복구, 대사, 통합증거금 자동환전 검증 | paper 국내 A/B 동시 매매 후 대사 일치. real 소액 국내 SOR과 미국 1주 검증 |
| **M4** API 서버 | Admin, /me, /market, WS, API 키, Tailscale 바인딩 | SDK로 외부 주문 성공 |
| **M5** 데이터 서비스 | 구독 허브, 틱 저장, 캔들 캐시, 백필 | 1주 무중단 수집 |
| **M6** 운영화 | 스케줄러, Windows 서비스, 알림, 백업, Kill switch | 2주 무인 운영 |
| **M7** 전략 러너 | 플러그인, 샘플 전략 2개 | 두 전략 독립 운영 |
| **M8** 실전 확대 | 수수료·세율 반영, 한도 조정 | 실전 1주 대사 일치 |
| **M9** 웹 대시보드 | 현황, 주문/체결, 손익, 대사, Kill switch UI | Tailscale 내부 접속 |

---

## 14. 결정 사항과 미결 사항

### 결정됨
| 항목 | 결정 |
|---|---|
| 시장 | 국내 + 미국 |
| 실계좌 | paper 1, real 1. 코드상 구분 없음 |
| 시간외, 장전 동시호가 | 제외 |
| 국내 경로 | SOR 기본 |
| 통합증거금 | 사용 중 → 6.2 설계 |
| 미국 시세 | 실시간 (0분 지연), 호가는 1호가 |
| 접속 | Tailscale 전용 |
| 저장소 | public 예정 → 11장 |
| 웹 대시보드 | M9 |

### 미결
1. 수수료·세율 값 (사용자 기입 예정)
2. 국내 장마감 동시호가(15:20–15:30) 주문 허용 여부 (기본: 허용)
3. 통합증거금 자동환전의 실제 환율과 시점 확인 방법 (M3에서 실데이터로 확정)
4. 라이선스 선택 (public 공개 시)
