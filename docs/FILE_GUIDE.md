# OJT 작성 도우미 파일 가이드

이 문서는 기능을 수정할 때 어느 파일을 확인해야 하는지 빠르게 찾기 위한 개발자용 안내서입니다.
사용 방법과 개인정보 보호 원칙은 루트의 `README.md`를 먼저 확인합니다.

## 전체 실행 흐름

```text
start.bat / launch-mini.vbs
        │
        ├─ server.py ── 활동 수집 ── Windows UI Automation / pynput
        │      │
        │      ├─ 저장 ───────────── data/activity.db (SQLite + DPAPI)
        │      ├─ 요약 ───────────── 로컬 규칙 → Ollama → 선택적 OpenAI 폴백
        │      └─ HTTP API ───────── http://127.0.0.1:8765
        │
        ├─ index.html + styles.css + app.js ── 전체 브라우저 화면
        └─ mini_launcher.py + mini_app.py ──── Windows 미니 창
```

브라우저 화면과 미니 창은 별도 애플리케이션처럼 보이지만 같은 `server.py` API와 SQLite 데이터를
사용합니다. 따라서 저장 형식이나 API 응답을 바꾸면 두 화면을 함께 확인해야 합니다.

## 파일별 책임

| 파일 | 입력 | 책임 | 출력·영향 |
|---|---|---|---|
| `server.py` | Windows 활성 창, UI 클릭 이벤트, HTTP 요청 | 로컬 서버, 활동 수집, DPAPI 암복호화, SQLite 저장, 활동 묶기, AI 호출 및 응답 정제 | `/api/*` JSON, `data/activity.db` |
| `app.js` | DOM 이벤트, 서버 API 응답, `localStorage`의 이전 설정 | 전체 화면 상태 관리, 후보 선택, OJT 생성·수정·저장·복사, 설정 동기화 | 화면 DOM, 서버 저장 요청, 백업 파일 |
| `index.html` | 없음 | 전체 화면의 의미 구조와 접근성 레이블 정의 | `app.js`가 참조하는 고정 DOM ID |
| `styles.css` | HTML 클래스 및 상태 클래스 | 전체 화면·반응형·컴팩트 모드의 시각 규칙 | 브라우저 UI |
| `mini_app.py` | `server.py`의 JSON API, 키보드·마우스 이벤트 | CustomTkinter 미니 UI, 후보 편집, OJT 생성, 투명도·항상 위 상태 관리 | Windows 네이티브 창 |
| `mini_launcher.py` | 실행 중인 프로세스와 모니터 좌표 | 서버 준비 확인, 중복 창 방지 보조, 현재 모니터 기준 창 위치 계산 | 미니 앱 실행 환경 |
| `start.bat` | 사용자 더블클릭 | UTF-8 콘솔에서 `server.py` 실행, `py` 실패 시 `python` 폴백 | 로컬 서버 프로세스 |
| `launch-mini.vbs` | 사용자 더블클릭 또는 바로가기 | 콘솔 창 없이 `mini_app.py` 실행 | 백그라운드 `pythonw` 프로세스 |
| `install-mini-shortcut.vbs` | 사용자 실행 | 바탕화면에 미니 앱 바로가기 생성 | `.lnk` 파일 |
| `.env.example` | 사용자가 복사한 `.env` | Ollama 모델과 선택적 OpenAI 폴백 설정 예시 | `server.py` 실행 설정 |
| `requirements.txt` | `pip` | Windows 활동 수집 및 네이티브 UI의 고정 의존성 | Python 실행 환경 |
| `.gitignore` | Git | 활동 DB, 비밀 설정, 캐시가 저장소에 포함되지 않도록 차단 | 커밋 대상 필터 |

## `server.py` 내부 영역

`server.py`는 한 파일에 런타임 핵심이 모여 있으므로 아래 경계를 유지합니다.

1. **설정과 환경 로드** — 경로, 포트, 보존 기간, AI 공급자 설정을 확정합니다.
2. **보호 저장소** — DPAPI로 창 제목과 클릭 레이블을 암호화하고 SQLite 스키마를 관리합니다.
3. **활동 수집** — 활성 창을 주기적으로 샘플링하고 유효한 클릭 이벤트만 큐에 넣습니다.
4. **후보 생성** — 연속 활동을 업무 단위로 묶고 개인 활동·잡음 UI 레이블을 분류합니다.
5. **초안 생성** — 로컬 규칙을 기본 안전망으로 두고 Ollama 또는 선택적 OpenAI 응답을 정제합니다.
6. **HTTP 경계** — 허용된 정적 파일만 제공하고 `/api/*` 요청 크기와 경로를 검증합니다.

API나 데이터 스키마를 변경할 때는 이전 `data/activity.db`와의 호환성을 유지해야 합니다. 새 열은
기본값을 갖도록 추가하고, 기존 행을 읽는 코드가 누락 값을 처리하도록 작성합니다.

## 프런트엔드 상태 경계

- 서버가 원본인 값: 활동 후보, 저장된 OJT, 모니터 상태, AI 연결 상태, 공통 설정.
- 브라우저가 임시로 보유하는 값: 현재 선택, 편집 중인 폼, 토스트와 로딩 상태.
- `localStorage`는 이전 버전 데이터 마이그레이션과 화면 편의 설정에만 사용합니다.
- `index.html`의 ID를 바꾸면 `app.js`의 `document.getElementById` 참조도 반드시 함께 바꿉니다.

## 개인정보 보호 경계

- `data/`, `.env`, 백업 파일을 커밋하지 않습니다.
- 키 입력 내용, 클립보드, 화면 캡처를 수집하는 기능은 추가하지 않습니다.
- 외부 AI 폴백을 수정할 때는 전송되는 필드를 명시하고 로컬 규칙 모드를 계속 사용할 수 있어야 합니다.
- 정적 파일 제공 허용 목록을 넓힐 때 `.env`, Python 소스, 데이터베이스가 노출되지 않는지 확인합니다.

## 변경 후 확인

```powershell
python -m py_compile server.py mini_app.py mini_launcher.py
python server.py
```

서버 실행 후 전체 화면과 미니 창에서 같은 날짜의 후보·저장 기록이 일치하는지 확인합니다. 활동 수집
변경은 자리 비움, 시크릿 창, 메신저 제목, 작업표시줄 클릭 제외 규칙도 함께 회귀 확인합니다.
