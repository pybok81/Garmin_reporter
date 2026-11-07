# Garmin 하루 리포트 도구

Garmin Connect 데이터를 **하루 단위 원본 JSON**으로 저장하고, 그걸 **Markdown 리포트**로 만듭니다.
리포트의 운동별 메모 칸은 다시 생성해도 보존됩니다. Claude에게는 DB/JSON이 아니라 `reports/날짜.md`를 넘기세요.

> 커뮤니티 `garminconnect` 라이브러리를 쓰므로 Garmin 변경으로 깨질 수 있습니다. 토큰과 `data/`는 민감 정보입니다(.gitignore 포함).

## 설치 (Python 3.12+)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env   # GARMIN_EMAIL 수정
```

비밀번호는 저장하지 않고 실행 시 입력합니다(MFA 포함). 이후엔 토큰을 써서 묻지 않습니다.

## 매일 쓰는 법

```powershell
python .\garmin.py sync              # 오늘 수집 + 리포트
python .\garmin.py sync --date 2026-10-20
python .\garmin.py sync --days 7     # 과거 채우기(백필)
python .\garmin.py report            # 리포트만 재생성 (로그인 불필요)
python .\garmin.py export            # 수집만
python .\garmin.py open              # 오늘 리포트(.md)를 기본 앱으로 열기
python .\garmin.py view              # 보기 좋은 HTML로 변환해 브라우저로 열기 (읽기 전용)
```

- `garmin.py`는 `garmin_export.py`와 `garmin_report.py`를 순서대로 호출하는 얇은 진입점입니다. 문제가 생기면 두 스크립트를 따로 실행해 원인을 좁힐 수 있습니다.
- `sync`에서 수집이 실패하면 오래된 원본으로 리포트를 만들지 않고 멈춥니다.
- 오늘 리포트는 `⚠️ 오늘 스냅샷` 표시가 붙습니다. 하루가 끝난 뒤 다시 돌리면 갱신됩니다.

## 리포트 보기/편집

- **보기:** `garmin.py view`는 표와 제목이 정리된 HTML을 브라우저로 엽니다(`reports/_view/`). 읽기 전용이고, 메모를 채운 내용도 그대로 보입니다.
- **편집:** 메모는 `.md` 파일의 마커 안쪽에 씁니다. 노트패드보다 VS Code 같은 편집기를 권합니다(`Ctrl+Shift+V` 미리보기, `Ctrl+K V` 나란히 보기).
- `view`를 쓰려면 `pip install -r requirements.txt`로 `markdown` 패키지가 필요합니다.

## 메모 규칙

- 운동마다 `<!-- notes:ID --> ... <!-- /notes -->` 칸이 있습니다. **마커 안쪽에만** 쓰세요.
  항목: 목적 / 컨디션 / RPE / 통증(부위·좌우) / 계획 대비 / 비고
- 마커 바깥에 쓴 글은 재생성 시 사라집니다.
- 같은 날 운동이 2개여도, 과거 날짜를 재생성해도 ID별로 유지됩니다.
- 원본에서 사라진 운동의 메모는 `연결된 운동 없음` 구역에 남습니다.
- 기존 `notes.md`는 더 이상 읽지 않습니다. 필요하면 내용을 새 리포트의 메모 칸으로 옮기세요.

## 웨이트 세트

Garmin 앱에서 수정한 운동 세트를 그대로 가져옵니다(휴식 세트 제외, 같은 운동 연속 세트는 묶음).
처음 한 번은 리포트의 무게가 실제와 맞는지 확인하세요. 단위가 틀리면 `names.toml`의 `weight_unit`을 `"g"` 또는 `"kg"`로 고정합니다.
운동 이름이 `Bench Press`처럼 영어 코드로 나오면 `names.toml`의 `[exercises]`에 한글 이름을 추가하세요.

## 저장 구조

```
data/raw/YYYY-MM-DD/daily.json, activities.json, activity_<ID>/{summary,exercise_sets,hr_zones,splits}.json
reports/YYYY-MM-DD.md
```

## 웹앱 (브라우저로 보기·메모 편집·가져오기)

```powershell
python .\app.py            # http://127.0.0.1:8765 이 브라우저에서 열립니다
python .\app.py --port 9000 --no-browser
```

- 표준 라이브러리만 씁니다. 가민 수집(`가민에서 가져오기`)에만 `garminconnect`가 필요합니다.
- **왼쪽** 주 단위로 접고 펴는 날짜별 파일 목록(메모 입력 현황) / **가운데** 리포트 보기 / **오른쪽** AI에게 보내기.
- 상단 탭: 리포트 · AI · 리포트+AI. 리포트+AI에서는 가운데 경계선을 끌거나 ←/→ 키로 AI 패널 폭을 조절합니다(저장됨).
- **메모 편집**은 리포트 화면 안에서 바로 합니다. `reports/날짜.md`의 메모 마커 안쪽만 고치며(줄바꿈 형식 유지), `가민에서 가져오기`로 다시 받아도 메모는 유지됩니다. 리포트 내용이 틀렸을 때도 자동 생성 부분은 그대로 두고 `비고`에 적습니다. 웨이트 메모의 `슈퍼세트 표시`는 `SS: A 횟수 + B 횟수 ×N` 한 줄을 비고에 추가합니다(저장 버튼을 눌러야 반영).
- **.md 파일 올리기**: 화면 어디에든 `2026-11-05.md`를 끌어다 놓으면 날짜별로 저장합니다. 같은 날짜가 있으면 물어보고, 덮어쓰면 기존 파일은 `reports/_backup/`에 남습니다.
- **가민에서 가져오기**: 기간을 고르면 `garmin.py sync`를 실행합니다. 처음 한 번은 터미널에서 `python garmin.py export`로 로그인해 토큰을 만들어야 합니다(비밀번호·MFA는 앱에 입력하지 않음).
- **AI 연동**은 API 없이 질문+리포트(메모 포함)를 복사하고 Claude/ChatGPT/Gemini 등을 별도 창으로 여는 방식입니다. 목록은 `AI 목록 편집`에서 바꿉니다.
- 서버는 `127.0.0.1`에만 열립니다. 로그인 보호가 없으므로 다른 주소로 열 때는 주의하세요.
