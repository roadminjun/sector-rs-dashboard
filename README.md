# 미국 섹터 상대강도 신호 탐지기 (MVP)

QQQ를 기준으로 미국 섹터/테마 ETF의 일봉 상대강도, 기술적 추세, 거래량을 계산하고
근거가 보이는 상태 신호를 표시하는 Python + Streamlit 프로젝트입니다. 선택적으로
[1SIGMA 대시보드](https://sigma-dashboard-five.vercel.app/)의 장 마감 스냅샷을 같은 날짜의
단기 예상 범위 맥락으로 덧붙입니다.

이 프로그램은 시장 관찰 도구입니다. 매수·매도 지시, 자동주문, 매매전략, 백테스트,
자금 배분 기능은 포함하지 않습니다.

## MVP 설계 검토

- **데이터 계층 분리:** 분석 코드는 `MarketDataProvider` 인터페이스만 사용합니다. 첫 공급자는
  API 키가 필요 없는 `yfinance`이며, 나중에 다른 공급자로 교체할 수 있습니다. yfinance의
  내부 SQLite 캐시도 기본적으로 프로젝트의 `data/yfinance_cache`에 두어 Windows 프로필
  권한 문제를 피합니다.
- **단순하고 결정적인 순위:** 복합 점수 없이 QQQ 대비 20일 상대수익률을 우선하고,
  동률은 60일 → 5일 → ETF 코드 순으로 정렬합니다.
- **재현 가능한 신호:** 양수 전환은 전일 값과 비교하고, 약화 여부에 필요한 과거 강세는
  가격 시계열에서 계산합니다. CSV 유무가 당일 신호 자체를 바꾸지 않습니다.
- **안전한 이력:** `(날짜, 기준지수, ETF)`를 키로 upsert하고 임시 파일에서 완성한 뒤
  원본 CSV를 교체합니다. 같은 날 다시 실행해도 중복 행이 생기지 않습니다.
- **부분 장애 격리:** QQQ 실패는 분석을 중단하지만 개별 ETF 실패는 해당 종목만 제외하고
  화면과 CLI에 원인을 표시합니다. QQQ 기준일의 ETF 데이터가 없으면 오래된 값을 섞지 않습니다.
- **완결 일봉 강제:** 뉴욕 정규장 종료 후 15분 전에는 Yahoo가 반환한 당일 행을 자동 제외해
  장중 값이 일별 CSV에 섞이지 않게 합니다. 이 동작과 유예 시간은 `config.yaml`에서 조정할 수 있습니다.
- **외부 보조 데이터 격리:** 1SIGMA가 `CLOSED`이고 기준일이 Yahoo 분석일과 정확히 같을 때만
  동일 티커를 병합합니다. 다운로드 실패, 날짜 불일치, 미지원 ETF가 있어도 기존 분석과 저장은
  계속되며 1SIGMA 값은 순위와 여섯 신호를 바꾸지 않습니다.

## 파일 구성

```text
config.yaml       기준지수, ETF 목록, 지표 기간, 데이터/저장 설정
market_data.py    공급자 인터페이스, yfinance 다운로드, 정규화/검증/재시도
indicators.py     수익률, RS, 이동평균, 거래량, RSI, 변동성 계산
signals.py        여섯 상태 판정, 이유 문장, 순위 생성
sigma_data.py     1SIGMA 스냅샷 다운로드·검증·선택적 병합
storage.py        UTF-8 CSV 원자적 upsert, 과거 스냅샷과 신호 비교
pipeline.py       다운로드부터 저장 전 단계까지 공통 실행 흐름
run_analysis.py   매일 실행 가능한 CLI
dashboard.py      Streamlit 순위표, 요약, 차트, 전일 변화
tests/            지표·신호·저장·파이프라인 테스트
.github/workflows/daily-analysis.yml  매일 실행하는 GitHub Actions
```

## Windows 설치와 실행

Python 3.10 이상을 권장합니다. PowerShell에서 프로젝트 폴더로 이동한 뒤 실행합니다.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

PowerShell이 가상환경 활성화 스크립트를 막는 경우, 현재 창에만 적용되는 다음 설정 후 다시
활성화할 수 있습니다.

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

CLI로 다운로드·계산·CSV 저장을 한 번 실행합니다.

```powershell
python run_analysis.py
```

저장하지 않고 계산만 확인하려면 다음을 사용합니다.

```powershell
python run_analysis.py --no-save
```

대시보드를 실행합니다.

```powershell
streamlit run dashboard.py
```

브라우저에서 표시되는 로컬 주소(보통 `http://localhost:8501`)를 열면 됩니다. 첫 실행은
Yahoo Finance 데이터 다운로드 때문에 시간이 조금 걸릴 수 있습니다.

## ETF 목록 수정

[`config.yaml`](config.yaml)의 `etfs`에서 한 줄을 추가하거나 삭제합니다.

```yaml
etfs:
  SMH: 반도체
  IGV: 소프트웨어
  # 예: 새 티커 추가
  KRE: 지역은행
```

기준지수, 이동평균 기간, RSI 기간, 다운로드 길이와 CSV 경로도 같은 파일에서 바꿀 수
있습니다. 상대 저장 경로는 현재 터미널 위치가 아니라 `config.yaml` 위치를 기준으로 해석됩니다.

## 계산 정의

가격은 분할·배당을 반영한 조정종가를 사용합니다. 표시값은 반올림하지만 모든 판정은 원값으로
수행합니다.

- N일 ETF 수익률: `ETF 가격 / N거래일 전 ETF 가격 - 1`
- N일 상대수익률: `ETF N일 수익률 - QQQ N일 수익률`
- RS 비율: `ETF 가격 / QQQ 가격`
- RS N일 변화율: `현재 RS / N거래일 전 RS - 1`
- RS 신고가: 현재를 포함한 최근 20일 또는 60일의 최고 RS와 같거나 더 높음
- 이동평균: 조정종가 단순이동평균(SMA) 20·50·200일
- 정배열: `MA20 > MA50 > MA200`
- 거래량 비율: `현재 거래량 / 현재를 포함한 20일 평균 거래량`
- 아웃퍼폼 일수: 최근 5개 일간 수익률 중 ETF가 QQQ보다 높은 날의 수
- RSI 14: 최초 14일 단순평균으로 시작하는 Wilder 방식
- 20일 변동성: 일간 수익률의 20일 표본표준편차 × `sqrt(252)` (연율화)

상대수익률과 RS 변화율은 비슷하지만 같은 값은 아닙니다. 전자는 두 수익률의 퍼센트포인트
차이이고, 후자는 가격 비율 자체의 변화율입니다.

## 1SIGMA 결합 방식

`config.yaml`의 `sigma.enabled`가 `true`이면 프로그램이 한 번의 실행 안에서 Yahoo 분석 후
`/api/snapshot`도 호출합니다. 다음 조건을 전부 만족하는 값만 사용합니다.

1. 스냅샷 장 상태가 `CLOSED`
2. `snapshot.sessionDate`가 주 분석 기준 거래일과 일치
3. 감시 ETF와 1SIGMA의 심볼이 정확히 일치

`SIGMA 위치`는 `(현재가 - 기준가) / (기준가 × 예상 변동률)`로 계산해 `+1.00σ`처럼 표시합니다.
이 값은 중기 상대강도와 다른 축의 보조 설명일 뿐 점수, 신호, 매매 추천에 사용하지 않습니다.
`SMH`를 1SIGMA의 `SOXX`로 대체하는 식의 임의 매핑도 하지 않습니다. 첫 버전에는 GEX를
포함하지 않았습니다.

상태 표시는 `|위치| < 1` 정상 범위, `±1` 이상 상·하단 이탈, `±1.5` 이상 큰 이탈이라는
단순 설명용 구간입니다. 금요일 종가에는 새 주간 밴드 기준가가 그날 종가로 재설정되므로
위치가 `0.00σ`로 보일 수 있으며, 이후 거래일에 기준가 대비 위치가 움직입니다.

이 연결은 사이트 내부 JSON 구조에 의존하므로 엔드포인트나 필드가 바뀔 수 있습니다. 그런 경우
화면에 경고를 남기고 yfinance 기반 결과만 정상 출력하는 구조입니다. 사용 전 해당 사이트의
데이터 사용 조건도 직접 확인하세요.

## 신호 정의와 우선순위

신호가 겹치면 **약화 → 강한 주도 → 약세 → 주도 후보 → 개선 중 → 중립** 순으로 하나만
선택합니다.

- **강한 주도:** 20일·60일 상대수익률이 모두 양수, RS 20일 신고가, 가격이 MA20·MA50 위
- **주도 후보:** 전일 20일 상대수익률이 0 이하이고 당일 양수로 전환, RS 5일 변화율 양수
- **개선 중:** 20일 상대수익률은 음수지만 5일 상대수익률이 20일 값보다 큼
- **약화:** 최근 20거래일 안에 `20·60일 상대수익률 양수 + RS 20일 신고가 + 가격 MA20·MA50 위`였고,
  현재 RS가 RS MA20 아래이거나 최근 5거래일 중 4일 이상 QQQ 언더퍼폼
- **약세:** 20일·60일 상대수익률이 모두 음수, 가격이 MA20·MA50 아래
- **중립:** 위 조건 중 어느 것도 충족하지 않음

“20일 상대수익률은 음수지만 단기 상대강도가 좋아지는 경우”는 규칙 본문의 의미에 맞춰
`주도 후보`가 아니라 `개선 중`으로 표시합니다.

## 핵심 요약의 리더십 폭

다음 세 조건을 모두 만족하면 해당 ETF를 리더십 참여 종목으로 봅니다.

1. 20일 상대수익률 > 0
2. RS 비율 > RS 20일 이동평균
3. 가격 > MA50

현재와 이전 저장일에 공통으로 존재하는 ETF만 비교합니다. 참여 수 변화가 공통 종목 수의
15% 이상(올림, 최소 1개)이면 `확대` 또는 `축소`, 그보다 작으면 `유지`입니다. 화면에 전일 수,
현재 수, 변화량과 판정 기준을 함께 표시합니다. 현재 또는 이전 스냅샷이 일부 ETF만 포함한
불완전 실행이면 잘못된 폭 신호를 피하기 위해 `비교 불가`로 표시합니다. 복합 점수로 사용하지 않습니다.

## CSV와 전일 비교

기본 경로는 `data/daily_sector_signals.csv`입니다. 수익률은 `4.2%` 문자열이 아닌 `0.042`
숫자로 저장하며, 한글이 Windows Excel에서 잘 열리도록 UTF-8 BOM을 사용합니다.

“전일”은 단순 달력 전날이 아니라 현재 분석일보다 앞선 **가장 최근 저장 거래일**입니다.
주말·휴장일 또는 실행을 건너뛴 날에도 마지막 스냅샷과 비교합니다. 같은 날짜를 다시 실행하면
해당 날짜·ETF 행을 새 결과로 교체합니다.

## 매일 자동 실행: 로컬 컴퓨터

Windows 작업 스케줄러에서 미국장 마감 후 충분한 여유를 둔 시간에 다음을 실행하도록 등록할
수 있습니다.

- 프로그램/스크립트: 프로젝트의 `.venv\Scripts\python.exe`
- 인수 추가: `run_analysis.py`
- 시작 위치: 이 프로젝트의 절대 경로

대시보드는 저장된 결과만 보는 정적 앱이 아니라 시작/새로고침 시 최신 데이터를 직접 분석합니다.
대시보드의 선택 상자 조작은 CSV를 다시 쓰지 않으며, 새로고침 버튼은 당일 스냅샷을 upsert합니다.

## 컴퓨터를 끄고 자동 실행: GitHub + Streamlit Cloud

프로젝트에 포함된 `.github/workflows/daily-analysis.yml`은 한국시간 화~토 오전 8시 30분
(미국 월~금 장 마감 뒤)에 GitHub 서버에서 다음 작업을 수행합니다.

1. 의존성 설치
2. yfinance와 1SIGMA 데이터 다운로드
3. 상대강도 분석과 신호 생성
4. `data/daily_sector_signals.csv` upsert
5. 변경된 CSV를 저장소에 자동 커밋

사용자의 PC는 꺼져 있어도 됩니다. 단, GitHub 스케줄 실행은 정확히 초 단위로 보장되는 작업이
아니므로 GitHub 사정에 따라 시작이 조금 늦을 수 있습니다. 오전 6시 30분 대신 8시 30분으로
둔 이유는 미국 서머타임/표준시간 모두 정규장 마감과 1SIGMA 생성에 충분한 여유를 주기 위해서입니다.

### 1단계: GitHub 저장소 만들기

GitHub 웹사이트에서 새 빈 저장소를 만듭니다. README, `.gitignore`, 라이선스 자동 생성은
선택하지 않으면 첫 push가 단순합니다. 이 폴더는 아직 원격 저장소 정보가 없으므로 아래 명령의
주소만 본인의 저장소 주소로 바꿔 PowerShell에서 실행합니다.

```powershell
cd "C:\Users\jack6\Documents\New project"
git init
git add .
git commit -m "Initial sector relative-strength dashboard"
git branch -M main
git remote add origin https://github.com/GITHUB_ID/REPOSITORY.git
git push -u origin main
```

처음 커밋에서 이름/이메일을 요구하면 본인 값으로 한 번 설정합니다.

```powershell
git config user.name "YOUR_NAME"
git config user.email "YOUR_EMAIL"
```

### 2단계: GitHub Actions 허용 및 시험 실행

저장소의 **Settings → Actions → General → Workflow permissions**에서
**Read and write permissions**를 허용합니다. 그다음 **Actions → Daily sector
relative-strength analysis → Run workflow**로 한 번 수동 실행합니다. 실행이 성공하고
`data/daily_sector_signals.csv`에 자동 커밋이 생기면 예약 저장까지 준비된 것입니다.

보호된 브랜치가 Actions의 직접 push를 막는 설정이라면 해당 규칙에서 이 워크플로의 쓰기를
허용하거나, 별도 브랜치/PR 방식으로 워크플로를 변경해야 합니다.

### 3단계: Streamlit Community Cloud에 화면 배포

[Streamlit Community Cloud](https://share.streamlit.io/)에 GitHub 계정으로 로그인해 저장소를
연결하고 앱 진입 파일을 `dashboard.py`로 지정합니다. 앱의 **Secrets**에 다음 한 줄을 넣습니다.

```toml
SECTOR_RS_DASHBOARD_READ_ONLY = true
```

이 설정은 클라우드 대시보드가 임시 파일시스템에 CSV를 잘못 저장하지 않게 합니다. CSV의 영구
갱신은 GitHub Actions만 담당하고, 대시보드는 저장소에 누적된 이력과 새 분석값을 읽어 표시합니다.
로컬에서는 이 설정이 없으므로 기존처럼 새로고침 시 CSV를 직접 저장합니다.

### 담당 범위

- 코드로 이미 자동화됨: 데이터 다운로드, 날짜/장 상태 검증, 계산, CSV upsert, 예약 실행,
  자동 커밋, 클라우드 읽기 전용 화면
- 사용자가 한 번 직접 해야 함: GitHub 저장소 생성, 로그인/최초 push, Actions 쓰기 권한,
  Streamlit Cloud 연결과 Secret 입력
- API 키: 현재 yfinance와 1SIGMA 연결에는 필요 없음. 향후 키 기반 공급자는 환경변수 또는
  Streamlit Secrets를 사용해야 함

## 테스트

```powershell
python -m pytest -q
```

테스트는 네트워크를 사용하지 않는 합성 일봉으로 지표, 여섯 신호, 결정적 순위, CSV upsert,
이전 스냅샷 비교, 1SIGMA 파싱/검증과 전체 파이프라인을 확인합니다. 실제 Yahoo/1SIGMA 연결은
아래 CLI 명령으로 별도 확인합니다.

```powershell
python run_analysis.py --no-save
```

## 데이터와 운영상 주의

- `yfinance`는 접근하기 쉬운 무료 소스이며 공식 거래소 피드나 주문용 실시간 피드가 아닙니다.
- 장중에는 Yahoo에 미완성 일봉이 보일 수 있습니다. 기본 설정은 뉴욕 정규장 종료+15분 전까지
  당일 행을 제외하며, 운영 스케줄도 미국장 마감 후로 두는 것을 권장합니다.
- 가격/거래량을 임의로 forward-fill하지 않습니다. QQQ 최신 거래일 데이터가 없는 ETF는 제외합니다.
- 네트워크 제한, Yahoo 응답 변경, 잘못된 티커는 오류 목록에 표시됩니다.
- 1SIGMA 스냅샷의 기준일이나 장 상태가 맞지 않으면 병합하지 않고 경고만 표시합니다.
- CSV가 Excel에서 열려 있으면 Windows가 파일 교체를 막을 수 있습니다. Excel에서 닫고 다시
  실행하세요.

향후 단계로 남긴 항목은 점수 모델, 매매전략, 백테스트, 자동주문, 자금 배분, 옵션 GEX,
ETF 구성종목 기반 시장 폭 분석입니다.
