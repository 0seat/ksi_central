# 중앙증권 (Central Securities)

한국투자증권(KIS) Open API 게이트웨이입니다. 하나의 실계좌를 여러 **가상 서브계좌**로 나눠서, 여러 전략과 프로그램이 서로 독립적으로 매매할 수 있게 해 줍니다.

- 설계 문서: [docs/PLAN.md](docs/PLAN.md)
- 대상 시장: 국내주식(KRX/NXT, SOR 주문), 미국주식(NASDAQ/NYSE/AMEX)

> ⚠️ 이 저장소에는 **어떤 비밀정보도 포함되지 않습니다.** 앱키, 계좌번호, 토큰, DB, 로그는 모두 저장소 밖 `CENTRAL_HOME`에 저장됩니다.

## 요구사항
- Windows 10/11 (Linux도 동작은 하도록 작성)
- Python 3.12+
- Git

## 설치

```powershell
git clone <repo-url>
cd <repo>
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pre-commit install          # 커밋 전 비밀정보 검사 훅
```

## 설정 (`CENTRAL_HOME`)

설정과 비밀정보는 **저장소 밖**에만 둡니다. 기본 경로는 `C:\ProgramData\CentralSecurities`입니다.

```powershell
python -m tools.init_home            # 디렉토리 생성, 예시 설정 복사, secrets/state 권한 제한
# 다른 경로를 쓰려면:
#   python -m tools.init_home --home D:\central
#   setx CENTRAL_HOME D:\central
```

```
%CENTRAL_HOME%\
  config\central.toml     일반 설정 (서버, 실계좌 profile)
  secrets\secrets.toml    앱키, 시크릿, HTS ID, 계좌번호   ← 여기만 직접 채우기
  state\tokens\           접근토큰 캐시
  data\  logs\  backups\
```

설정을 확인할 때는 아래 명령을 씁니다. 비밀정보는 출력되지 않습니다.
```powershell
python -m central config
```

## 비밀정보 보호 장치
| 장치 | 내용 |
|---|---|
| 경로 분리 | `CENTRAL_HOME`이 저장소 안이면 실행을 거부합니다. 저장소 안의 `.env`는 읽지 않습니다 |
| `.gitignore` | 설정, 키, DB, 토큰 캐시, 로그 패턴을 제외합니다 |
| pre-commit | `tools/check_secrets.py`(금지 파일명, 키 리터럴), gitleaks, private key 검사 |
| CI | GitHub Actions에서 같은 검사와 전체 히스토리 gitleaks를 실행합니다 |
| 로그 마스킹 | 앱키, 시크릿, 토큰, 계좌번호를 `****`로 치환합니다 (예외 traceback 포함) |

공개 전에 GitHub 저장소 설정에서 **Secret scanning**과 **Push protection**을 켜세요.

## 테스트
```powershell
python -m pytest
```
