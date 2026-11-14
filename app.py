"""Garmin 리포트 웹앱 서버. 표준 라이브러리만 쓴다 (가민 수집만 garminconnect 필요).

    python app.py                  # http://127.0.0.1:8765 를 열고 브라우저로 사용
    python app.py --port 9000 --no-browser

- 리포트(.md)가 원본이다. 메모는 .md의 `<!-- notes:ID -->` 마커 안쪽만 고친다.
- 가져오기는 garmin_export.py 로 수집한 뒤 garmin_report.py 를 (동기화 폴더 경로를 넘겨) 호출한다.
- 127.0.0.1에만 열고, Host/Content-Type을 검사해서 다른 웹페이지가 이 서버를 건드리지 못하게 한다.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import webbrowser
import zipfile
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import corrections as corr
import history_db
from garmin_report import NOTE_RE, load_names, weight_kg

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "webapp"
WEEKDAYS = "월화수목금토일"
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ID_RE = re.compile(r"^[\w-]+$")
ACT_HEAD_RE = re.compile(r"^### \d+\. (.+?) — .*\(ID (\d+)\)\s*$", re.M)
FIELD_RE = re.compile(r"^- [^:\n]+:\s*\S", re.M)
MARKER_RE = re.compile(r"<!--\s*/?notes")
MAX_BODY = 4 * 1024 * 1024
MAX_RANGE_DAYS = 31

FILE_LOCK = threading.RLock()  # 리포트 파일 쓰기
SYNC_LOCK = threading.Lock()   # 가민 수집은 한 번에 하나만

CFG = {
    "root": ROOT,   # 동기화(구글 드라이브) 대상 폴더: reports/ feedback/ plan/ reference/ inbox/ archive/ 가 이 아래에 놓인다
    "reports": ROOT / "reports",
    "data": ROOT / "data",
    "feedback": ROOT / "feedback",
    "outbox": ROOT / "outbox",
    "inbox": None,
    "hosts": {"127.0.0.1", "localhost", "[::1]", "::1"},
}


# ------------------------------------------------------------------ helpers
def valid_day(value: object) -> bool:
    if not isinstance(value, str) or not DAY_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def report_path(day: str) -> Path:
    return CFG["reports"] / f"{day}.md"


AI_DIR_RE = re.compile(r"^[0-9A-Za-z가-힣][0-9A-Za-z가-힣 ._\-]{0,39}$")


def valid_ai(name) -> bool:
    """AI 이름은 feedback/ 아래 폴더 이름이 된다. 경로 문자, 숨김(.)·예약(_) 시작, 끝의 점/공백(Windows)을 막는다."""
    return isinstance(name, str) and bool(AI_DIR_RE.fullmatch(name)) and name == name.rstrip(" .")


def feedback_path(day: str, ai: str) -> Path:
    return CFG["feedback"] / ai / f"{day}.md"


def feedback_items(day: str) -> list[tuple[str, Path]]:
    root = CFG["feedback"]
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir(), key=lambda x: x.name.lower()):
        if d.is_dir() and valid_ai(d.name):
            f = d / f"{day}.md"
            if f.is_file():
                out.append((d.name, f))
    return out


DOC_KINDS = {"plan": "훈련 계획", "reference": "사전"}
DOC_NAME_RE = re.compile(r"^[0-9A-Za-z가-힣][0-9A-Za-z가-힣 ._\-()]{0,80}\.md$")


def doc_dir(kind: str) -> Path:
    if kind not in DOC_KINDS:
        raise ApiError(400, "문서 종류가 올바르지 않습니다")
    return CFG["reports"].parent / kind


def doc_file(kind: str, name) -> Path:
    if not isinstance(name, str) or not DOC_NAME_RE.fullmatch(name) or name != name.rstrip(" ."):
        raise ApiError(400, "파일 이름이 올바르지 않습니다 (한글·영문·숫자·공백·._-() 만, .md로 끝나야 함)")
    return doc_dir(kind) / name


def doc_names(kind: str) -> list[str]:
    d = doc_dir(kind)
    if not d.is_dir():
        return []
    return sorted((f.name for f in d.glob("*.md") if f.is_file() and DOC_NAME_RE.fullmatch(f.name)), key=str.lower)


def reveal_in_file_manager(path: Path) -> bool:
    """AI 창에 끌어다 놓을 수 있게 탐색기에서 파일을 선택한 채 연다. 실패하면 False."""
    try:
        if sys.platform == "win32":
            # 리스트로 넘기면 /select,"경로" 전체가 따옴표로 묶여 탐색기가 무시한다. 명령줄 문자열로 직접 만든다.
            try:
                subprocess.Popen(f'explorer /select,"{path}"')  # noqa: S603
            except OSError:
                os.startfile(path.parent)  # type: ignore[attr-defined]  # 선택은 못 해도 폴더는 연다
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])  # noqa: S603,S607
        else:
            subprocess.Popen(["xdg-open", str(path.parent)], stderr=subprocess.DEVNULL)  # noqa: S603,S607
        return True
    except OSError:
        return False


INBOX_NAME_RE = re.compile(r"^(\d{4})[-_.]?(\d{2})[-_.]?(\d{2})(?:[\s_\-.]+(.+?))?\.md$", re.IGNORECASE)   # 2026-11-14_gpt.md / 20261114_gpt.md / 2026.11.14 gemini.md
AI_ALIASES = {"gpt": "ChatGPT", "chatgpt": "ChatGPT", "openai": "ChatGPT",
              "gemini": "gemini", "claude": "claude", "claud": "claude", "cladue": "claude", "기타": "claude",
              "perplexity": "Perplexity", "grok": "Grok", "copilot": "Copilot"}


def canon_ai(name: str) -> str:
    """AI 이름 표기를 하나로 통일한다. gpt / chatgpt / Chat GPT / chat-gpt / openai → ChatGPT. 그 외는 입력 그대로."""
    key = re.sub(r"[\s_.\-]+", "", name.strip().lower())
    return AI_ALIASES.get(key, name.strip())


def inbox_dir() -> Path:
    """피드백 파일을 모아 두는 폴더. .env의 FEEDBACK_INBOX(절대 경로 또는 프로젝트 기준 상대 경로), 기본은 inbox/."""
    raw = os.getenv("FEEDBACK_INBOX") or load_env_file().get("FEEDBACK_INBOX")
    if raw:
        p = Path(raw).expanduser()
        return p if p.is_absolute() else ROOT / p
    return CFG["inbox"] or ROOT / "inbox"


def inbox_match(name: str) -> tuple[str, str | None] | None:
    """파일 이름에서 (YYYY-MM-DD, AI 이름 또는 None). 날짜로 시작하지 않으면 None."""
    m = INBOX_NAME_RE.match(name)
    return (f"{m.group(1)}-{m.group(2)}-{m.group(3)}", (m.group(4) or "").strip() or None) if m else None


def inbox_sources() -> tuple[list[tuple[Path, str, str, Path]], list[dict]]:
    """반영할 파일 목록 [(파일, 날짜, AI 이름, 그 파일이 있는 폴더)]과, 건드리지 못한 .md 파일 [{name, dir, why}].

    찾는 곳: ① 피드백 폴더(설정) ② 예전 기본 위치 앱 폴더의 inbox/ ③ feedback/ 바로 아래, 또는 feedback/<AI>/ 폴더 안에 날짜_이름.md 로 넣은 파일(폴더 이름이 AI).
    """
    found, skipped, seen = [], [], set()
    dirs = [inbox_dir()]
    legacy = ROOT / "inbox"
    if legacy.is_dir() and legacy.resolve() != inbox_dir().resolve():
        dirs.append(legacy)
    for d in dirs:
        if not d.is_dir() or d.resolve() in seen:
            continue
        seen.add(d.resolve())
        for f in sorted(d.glob("*.md")):
            if not f.is_file():
                continue
            mm = inbox_match(f.name)
            if not mm:
                skipped.append({"name": f.name, "dir": str(d), "why": "이름이 날짜로 시작하지 않음 (예: 2026-11-14_gpt.md)"})
            elif not mm[1]:
                skipped.append({"name": f.name, "dir": str(d), "why": "AI 이름이 없음 (예: 2026-11-14_gpt.md)"})
            else:
                found.append((f, mm[0], mm[1], d))
    fb = CFG["feedback"]
    if fb.is_dir():
        for f in sorted(fb.glob("*.md")):       # feedback/ 바로 아래에 날짜_gpt.md 로 넣은 파일
            if not f.is_file() or valid_day(f.stem):   # 날짜.md 는 예전 형식(시작할 때 자동 정리)
                continue
            mm = inbox_match(f.name)
            if mm and mm[1]:
                found.append((f, mm[0], mm[1], fb))
            else:
                skipped.append({"name": f.name, "dir": str(fb), "why": "이름이 날짜_AI이름.md 형식이 아님 (예: 2026-11-14_gpt.md)"})
        for ad in sorted(p for p in fb.iterdir() if p.is_dir() and not p.name.startswith("_")):
            for f in sorted(ad.glob("*.md")):
                if valid_day(f.stem):      # 이미 정리된 파일(날짜.md)
                    continue
                mm = inbox_match(f.name)
                if mm:
                    found.append((f, mm[0], ad.name, ad))
                else:
                    skipped.append({"name": f.name, "dir": str(ad), "why": "날짜.md 형식이 아님 (예: 2026-11-14.md)"})
    return found, skipped


def inbox_files() -> list[Path]:
    return [f for f, *_ in inbox_sources()[0]]


def uses_crlf(data: bytes) -> bool:
    return b"\r\n" in data


def atomic_write(path: Path, text: str, crlf: bool = False) -> None:
    """text는 \\n 기준. 파일의 원래 줄바꿈(CRLF/LF)을 유지해서 쓴다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\r\n" if crlf else "\n")
    os.replace(tmp, path)


def note_filled(body: str) -> bool:
    return bool(FIELD_RE.search(body))


def day_info(path: Path) -> dict:
    day = path.stem
    text = path.read_text(encoding="utf-8")
    heads = ACT_HEAD_RE.findall(text)
    notes = {m.group(1): m.group(2) for m in NOTE_RE.finditer(text)}
    ids = [h[1] for h in heads]
    return {
        "date": day,
        "weekday": WEEKDAYS[date.fromisoformat(day).weekday()],
        "workouts": len(heads),
        "types": [h[0] for h in heads],
        "notesTotal": len(ids),
        "notesFilled": sum(1 for i in ids if note_filled(notes.get(i, ""))),
        "snapshot": "오늘 스냅샷" in text,
        "hasRaw": (CFG["data"] / "raw" / day).is_dir(),
        "hasFeedback": bool(feedback_items(day)),
    }


def list_days() -> list[dict]:
    out = []
    if CFG["reports"].is_dir():
        for p in CFG["reports"].glob("*.md"):
            if valid_day(p.stem):
                try:
                    out.append(day_info(p))
                except (OSError, UnicodeDecodeError):
                    continue
    return sorted(out, key=lambda d: d["date"], reverse=True)


def load_env_file() -> dict[str, str]:
    path = ROOT / ".env"
    values: dict[str, str] = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def token_file() -> Path:
    env = load_env_file()
    token_dir = os.getenv("GARMIN_TOKEN_DIR") or env.get("GARMIN_TOKEN_DIR") or ".garminconnect"
    p = Path(token_dir)
    return (p if p.is_absolute() else ROOT / p) / "garmin_tokens.json"


def set_env_key(key: str, value: str) -> None:
    path = ROOT / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for i, raw in enumerate(lines):
        if raw.strip().startswith(key + "="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_script(args: list[str], timeout: int = 900) -> tuple[int, str]:
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    try:
        proc = subprocess.run(
            [sys.executable, *args], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, "시간 초과 (15분). 가민 응답이 없거나 로그인 입력을 기다리고 있을 수 있습니다."
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out.strip()[-4000:]


def contiguous_runs(days: list[str]) -> list[tuple[str, int]]:
    """정렬된 날짜 목록을 연속 구간으로 묶어 (마지막 날, 일수) 목록으로."""
    runs: list[tuple[str, int]] = []
    prev: date | None = None
    start_len = 0
    for d in days:
        cur = date.fromisoformat(d)
        if prev is not None and cur - prev == timedelta(days=1):
            start_len += 1
        else:
            if prev is not None:
                runs.append((prev.isoformat(), start_len))
            start_len = 1
        prev = cur
    if prev is not None:
        runs.append((prev.isoformat(), start_len))
    return runs


# ---------------------------------------------------------------- API logic
# ------------------------------------------------- 동기화 폴더(구글 드라이브) / 스냅샷
SYNC_SUBDIRS = ("reports", "feedback", "plan", "reference", "corrections")   # 동기화 폴더로 옮겨 가는 폴더 (원본 가민 JSON과 DB는 로컬에 둔다)


def apply_root(root: Path) -> None:
    CFG["root"] = root
    CFG["reports"] = root / "reports"
    CFG["feedback"] = root / "feedback"
    CFG["inbox"] = root / "inbox"
    CFG["outbox"] = ROOT / "outbox"   # AI용 임시 파일은 동기화할 필요가 없다


def configured_root() -> Path:
    raw = os.getenv("DATA_ROOT") or load_env_file().get("DATA_ROOT")
    return Path(raw).expanduser() if raw else ROOT


def change_root(raw: str) -> dict:
    """동기화 폴더를 바꾼다. 새 폴더 안에 없는 하위 폴더는 현재 것을 복사해 가고(원본은 지우지 않음), 바로 적용한다."""
    raw = raw.strip()
    new = ROOT if not raw else Path(raw).expanduser()
    if not new.is_absolute():
        raise ApiError(400, "동기화 폴더는 전체 경로(예: G:\\내 드라이브\\Garmin)로 적어 주세요")
    new = new.resolve()
    old = CFG["root"]
    with FILE_LOCK:
        try:
            new.mkdir(parents=True, exist_ok=True)
            probe = new / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as err:
            raise ApiError(400, f"이 폴더에 쓸 수 없습니다: {err}") from None
        copied = []
        if new != old.resolve():
            for sub in SYNC_SUBDIRS:
                src, dst = (CFG["reports"] if sub == "reports" else old / sub), new / sub
                if src.is_dir() and any(src.iterdir()) and (not dst.exists() or not any(dst.iterdir())):
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                    copied.append(sub)
        set_env_key("DATA_ROOT", "" if new == ROOT else str(new))
        apply_root(new)
    return {"copied": copied}


def snapshot_files() -> list[tuple[Path, str]]:
    out = []
    for sub, base in (("reports", CFG["reports"]), ("feedback", CFG["feedback"]),
                      ("plan", doc_dir("plan")), ("reference", doc_dir("reference"))):
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("*.md")):
            rel = f.relative_to(base)
            if any(part.startswith("_") for part in rel.parts):   # _backup, _삭제, _이전, _반영됨 은 제외
                continue
            out.append((f, f"{sub}/{rel.as_posix()}"))
    return out


def make_snapshot() -> dict:
    """리포트/피드백/계획/사전(.md)을 zip 하나로 묶어 동기화 폴더의 archive/ 에 둔다."""
    files = snapshot_files()
    if not files:
        raise ApiError(400, "백업할 파일이 없습니다")
    out_dir = CFG["root"] / "archive"
    out = out_dir / f"스냅샷_{date.today().isoformat()}.zip"
    with FILE_LOCK:
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".zip.tmp")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for f, arc in files:
                z.write(f, arc)
        os.replace(tmp, out)
    return {"ok": True, "path": str(out), "files": len(files), "bytes": out.stat().st_size}


def auto_snapshot() -> str | None:
    """이번 달 스냅샷이 없으면 하나 만든다 (시작할 때 한 번)."""
    d = CFG["root"] / "archive"
    month = date.today().strftime("%Y-%m")
    if d.is_dir() and any(d.glob(f"스냅샷_{month}-*.zip")):
        return None
    try:
        return make_snapshot()["path"]
    except ApiError:
        return None


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra: object):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


def api_days() -> dict:
    return {"today": date.today().isoformat(), "days": list_days(), "reportsDir": str(CFG["reports"]),
            "hasPlan": bool(doc_names("plan")), "hasRef": bool(doc_names("reference")),
            "rpeDoc": next((n for n in doc_names("reference") if "rpe" in n.lower()), None)}


def api_report(day: str) -> dict:
    if not valid_day(day):
        raise ApiError(400, "날짜 형식이 올바르지 않습니다")
    path = report_path(day)
    if not path.exists():
        raise ApiError(404, "리포트가 없습니다")
    return {"date": day, "markdown": path.read_text(encoding="utf-8")}


def api_feedback_get(day: str) -> dict:
    if not valid_day(day):
        raise ApiError(400, "날짜 형식이 올바르지 않습니다")
    return {"date": day, "items": [{"ai": ai, "text": f.read_text(encoding="utf-8")} for ai, f in feedback_items(day)]}


def api_feedback_save(body: dict) -> dict:
    """feedback/<AI>/<날짜>.md 한 개를 쓴다. mode=set: 통째로 저장(빈 문자열이면 파일 삭제) / append: 구분선과 함께 뒤에 추가."""
    day, ai, text, mode = body.get("date"), body.get("ai"), body.get("text"), body.get("mode", "set")
    if not valid_day(day) or not isinstance(text, str) or mode not in ("set", "append"):
        raise ApiError(400, "요청 형식이 올바르지 않습니다")
    if isinstance(ai, str):
        ai = canon_ai(ai)
    if not valid_ai(ai):
        raise ApiError(400, "AI 이름이 올바르지 않습니다 (한글·영문·숫자·공백·._- 만, 40자 이내)")
    if len(text) > 2 * 1024 * 1024:
        raise ApiError(413, "내용이 너무 큽니다 (2MB 초과)")
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    duplicate = False
    with FILE_LOCK:
        path = feedback_path(day, ai)
        old = path.read_text(encoding="utf-8").strip("\n") if path.is_file() else ""
        if mode == "append":
            if not text.strip():
                raise ApiError(400, "추가할 내용이 없습니다")
            sections = [x.strip() for x in re.split(r"(?m)^\s*-{3,}\s*$", old)]
            if text.strip() in sections:  # 구분선으로 나뉜 항목과 통째로 같을 때만 중복 (일부 포함은 중복 아님)
                duplicate = True
            else:
                text = f"{old}\n\n---\n\n{text}" if old else text
        else:
            if not text.strip():
                if path.is_file():
                    path.unlink()
                    try:
                        path.parent.rmdir()  # 비었으면 폴더도 정리
                    except OSError:
                        pass
        if not duplicate and text.strip():
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, text + "\n")
    out = api_feedback_get(day)
    out["ok"], out["duplicate"] = True, duplicate
    return out


def migrate_feedback() -> int:
    """예전 feedback/날짜.md(여러 AI가 `## [이름]` 머리글로 섞인 형식)를 feedback/<AI>/날짜.md로 나눈다. 원본은 _이전/에 보관."""
    root = CFG["feedback"]
    if not root.is_dir():
        return 0
    moved = 0
    for f in sorted(root.glob("*.md")):
        if not valid_day(f.stem):
            continue
        try:
            text = f.read_text(encoding="utf-8-sig")
            for chunk in re.split(r"(?m)^\s*-{3,}\s*$", text):
                chunk = chunk.strip()
                if not chunk:
                    continue
                m = re.match(r"##\s*\[([^\]\n]+)\]\s*\n?", chunk)
                ai, body = (m.group(1).strip(), chunk[m.end():].strip()) if m else ("claude", chunk)  # 머리글 없던 예전 피드백은 Claude 답변
                ai = canon_ai(ai)
                if not valid_ai(ai):
                    ai, body = "claude", chunk
                if body:
                    api_feedback_save({"date": f.stem, "ai": ai, "text": body, "mode": "append"})
            old = root / "_이전"
            old.mkdir(exist_ok=True)
            dest = old / f.name
            if dest.exists():
                dest = old / f"{f.stem}.{datetime.now().strftime('%Y%m%d-%H%M%S')}{f.suffix}"
            os.replace(f, dest)
            moved += 1
        except (OSError, UnicodeDecodeError, ApiError) as err:
            print(f"피드백 이전 실패: {f.name} ({err})", file=sys.stderr)
    return moved


def merge_ai_folders() -> int:
    """feedback/ 아래 폴더 이름이 gpt, chatgpt, Chat GPT처럼 제각각이면 하나(ChatGPT)로 합친다. 같은 날짜 파일은 이어 붙인다."""
    root = CFG["feedback"]
    if not root.is_dir():
        return 0
    merged = 0
    for d in sorted(root.iterdir()):
        if not (d.is_dir() and valid_ai(d.name)):
            continue
        canon = canon_ai(d.name)
        if canon == d.name:
            continue
        tmp = root / f"_합치는중_{d.name}"
        try:
            os.replace(d, tmp)  # 대소문자만 다른 경우(Windows)도 안전하게 이름을 바꾸기 위해 항상 임시 이름을 거친다
            target = root / canon
            if not target.exists():
                os.replace(tmp, target)
            else:
                for f in sorted(tmp.glob("*.md")):
                    if valid_day(f.stem):
                        api_feedback_save({"date": f.stem, "ai": canon, "text": f.read_text(encoding="utf-8-sig"), "mode": "append"})
                        f.unlink()
                try:
                    tmp.rmdir()
                except OSError:
                    print(f"합친 뒤 남은 파일이 있어 {tmp.name} 폴더를 남겨 둡니다", file=sys.stderr)
            merged += 1
        except (OSError, UnicodeDecodeError, ApiError) as err:
            print(f"피드백 폴더 합치기 실패: {d.name} ({err})", file=sys.stderr)
    return merged


def api_docs() -> dict:
    out = []
    for kind, label in DOC_KINDS.items():
        items = []
        for n in doc_names(kind):
            st = (doc_dir(kind) / n).stat()
            items.append({"name": n, "size": st.st_size, "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
        out.append({"kind": kind, "label": label, "dir": str(doc_dir(kind)), "items": items})
    return {"kinds": out}


def api_doc_get(kind: str, name: str) -> dict:
    f = doc_file(kind, name)
    if not f.is_file():
        raise ApiError(404, "문서가 없습니다")
    return {"kind": kind, "name": name, "text": f.read_text(encoding="utf-8-sig")}


def api_doc_save(body: dict) -> dict:
    kind, name, text = body.get("kind"), body.get("name"), body.get("text")
    f = doc_file(kind, name)
    if not isinstance(text, str) or not text.strip():
        raise ApiError(400, "내용이 비어 있습니다 (삭제는 삭제 버튼을 쓰세요)")
    if len(text) > 1024 * 1024:
        raise ApiError(413, "문서가 너무 큽니다 (1MB 초과)")
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n") + "\n"
    with FILE_LOCK:
        created = not f.exists()
        crlf = False
        if not created:
            raw = f.read_bytes()
            crlf = uses_crlf(raw)
            if raw.decode("utf-8-sig").replace("\r\n", "\n") == text:
                return {"ok": True, "created": False, "unchanged": True, "name": name}
            bak = f.parent / "_backup"
            bak.mkdir(exist_ok=True)
            os.replace(f, bak / f"{f.stem}.{datetime.now().strftime('%Y%m%d-%H%M%S')}{f.suffix}")
        atomic_write(f, text, crlf)
    return {"ok": True, "created": created, "name": name}


def api_doc_delete(body: dict) -> dict:
    f = doc_file(body.get("kind"), body.get("name"))
    if not f.is_file():
        raise ApiError(404, "문서가 없습니다")
    with FILE_LOCK:
        trash = f.parent / "_삭제"
        trash.mkdir(exist_ok=True)
        os.replace(f, trash / f"{f.stem}.{datetime.now().strftime('%Y%m%d-%H%M%S')}{f.suffix}")
    return {"ok": True}


# ---- 심박/페이스 시계열 -------------------------------------------------
HR_JUMP_BPM, HR_JUMP_SEC = 15, 30   # 30초 안에 15bpm 이상 변하면 '급변'으로 표시


def parse_series(details: dict) -> dict | None:
    """get_activity_details JSON -> {t,hr,pace,cad} (초/ bpm / 초·km / spm). 구조가 달라도 죽지 않게 방어적으로 읽는다."""
    try:
        keys = {}
        for d in details.get("metricDescriptors") or []:
            keys[d.get("key")] = d.get("metricsIndex")
        rows = [r.get("metrics") or [] for r in details.get("activityDetailMetrics") or []]
        if not keys or not rows:
            return None

        def col(*names):
            for n in names:
                i = keys.get(n)
                if i is not None:
                    return i
            return None
        it, ihr, isp = col("sumDuration", "sumElapsedDuration"), col("directHeartRate"), col("directSpeed")
        its = col("directTimestamp")
        icd = col("directRunCadence", "directDoubleCadence")
        if ihr is None or (it is None and its is None):
            return None

        def at(r, i):
            return r[i] if i is not None and i < len(r) and isinstance(r[i], (int, float)) else None
        out = {"t": [], "hr": [], "pace": [], "cad": []}
        t0 = None
        for r in rows:
            if it is not None:
                t = at(r, it)
            else:
                ts = at(r, its)
                t0 = ts if t0 is None and ts is not None else t0
                t = None if ts is None else (ts - t0) / 1000
            if t is None:
                continue
            hr, sp, cd = at(r, ihr), at(r, isp), at(r, icd)
            out["t"].append(round(t, 1))
            out["hr"].append(round(hr) if hr else None)
            out["pace"].append(round(1000 / sp) if sp and sp > 0.5 else None)   # 초/km
            out["cad"].append(round(cd) if cd else None)
        return out if len(out["t"]) > 5 else None
    except (AttributeError, TypeError, ValueError, IndexError):
        return None


def hr_jumps(s: dict) -> list[dict]:
    """HR_JUMP_SEC 안에 HR_JUMP_BPM 이상 오른/내린 지점. 그 순간의 페이스·케이던스도 함께 돌려준다(센서 오류 vs 실제 판단용)."""
    t, hr = s["t"], s["hr"]
    res, last_end = [], -999
    for i in range(len(t)):
        if hr[i] is None:
            continue
        j = i
        while j > 0 and t[i] - t[j - 1] <= HR_JUMP_SEC:
            j -= 1
        base = [h for h in hr[j:i] if h is not None]
        if not base:
            continue
        lo, hi = min(base), max(base)
        d = hr[i] - lo if hr[i] - lo >= HR_JUMP_BPM else (hr[i] - hi if hi - hr[i] >= HR_JUMP_BPM else 0)
        if d and t[i] - last_end > 60:
            res.append({"t": t[i], "from": lo if d > 0 else hi, "to": hr[i], "pace": s["pace"][i], "cad": s["cad"][i], "lock": cadence_lock(s, t[i], d > 0)})
            last_end = t[i]
    return res


def cadence_lock(s: dict, t0: float, up: bool) -> bool:
    """점프 전후 90초 동안 심박이 케이던스와 거의 같은 값(±6)으로 붙어 있으면 '케이던스 락 의심'."""
    lo, hi = (t0, t0 + 90) if up else (t0 - 90, t0)
    pairs = [(h, c) for t, h, c in zip(s["t"], s["hr"], s["cad"]) if lo <= t <= hi and h and c]
    if len(pairs) < 10:
        return False
    return sum(abs(h - c) <= 6 for h, c in pairs) / len(pairs) >= 0.6


def day_series(day: str) -> list[dict]:
    root = CFG["data"] / "raw" / day
    out = []
    if not root.is_dir():
        return out
    for d in sorted(root.glob("activity_*")):
        f = d / "details.json"
        if not f.is_file():
            continue
        try:
            s = parse_series(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        if not s:
            continue
        name = d.name[len("activity_"):]
        try:
            summ = json.loads((d / "summary.json").read_text(encoding="utf-8"))
            name = f"{summ.get('activityName') or name}"
            kind = ((summ.get("activityTypeDTO") or {}).get("typeKey")) or ""
        except (OSError, ValueError):
            kind = ""
        # 급변 탐지는 달리기·걷기처럼 속도가 있는 운동에만 한다. 웨이트·사이클 등은 세트/휴식마다 심박이 원래 크게 오르내린다.
        moving = sum(p is not None for p in s["pace"]) >= len(s["pace"]) * 0.5 and kind not in ("indoor_cycling", "cycling")
        out.append({"id": d.name[len("activity_"):], "name": name, "type": kind, "series": s,
                    "detect": moving, "jumps": hr_jumps(s) if moving else []})
    return out


def api_series(day: str) -> dict:
    if not valid_day(day):
        raise ApiError(400, "날짜 형식이 올바르지 않습니다")
    return {"date": day, "activities": day_series(day)}


def _mmss(sec: float) -> str:
    sec = int(round(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def series_text(day: str, mode: str) -> str:
    """AI에 붙일 시계열 텍스트. mode: 'jumps'(급변 요약만) | 'full'(30초 간격 표 + 급변)."""
    acts = day_series(day)
    if not acts:
        return ""
    parts = [f"## {day} 심박·페이스 시계열 (Garmin 손목 광학센서 기준, 센서 오류 가능성 있음)", ""]
    for a in acts:
        s = a["series"]
        hrs = [h for h in s["hr"] if h]
        parts.append(f"### {a['name']} ({a['type']}) — 총 {_mmss(s['t'][-1])}, 심박 {min(hrs)}~{max(hrs)} bpm" if hrs else f"### {a['name']}")
        if not a["detect"]:
            parts.append("심박 급변 탐지: 하지 않음 (속도가 있는 운동이 아니라 심박이 원래 오르내림)")
        elif a["jumps"]:
            parts.append("심박 급변 구간 (30초 내 15bpm 이상, 당시 페이스/케이던스 함께 표기):")
            for j in a["jumps"]:
                pc = f"{_mmss(j['pace'])}/km" if j["pace"] else "-"
                parts.append(f"- {_mmss(j['t'])}: {j['from']}→{j['to']} bpm, 페이스 {pc}, 케이던스 {j['cad'] or '-'}" + (" ※ 점프 전후 심박이 케이던스와 거의 같은 값 → 케이던스 락 의심" if j["lock"] else ""))
        else:
            parts.append("심박 급변 구간: 없음")
        if mode == "full":
            parts += ["", "시간 | 심박 | 페이스(/km) | 케이던스"]
            nxt = 0
            for i, t in enumerate(s["t"]):
                if t < nxt:
                    continue
                nxt = t + 30
                pc = _mmss(s["pace"][i]) if s["pace"][i] else "-"
                parts.append(f"{_mmss(t)} | {s['hr'][i] or '-'} | {pc} | {s['cad'][i] or '-'}")
        parts.append("")
    return "\n".join(parts)


def compose_ai_text(day: str, question: str, with_plan: bool, with_ref: bool, graph: str = "") -> str:
    rp = report_path(day)
    if not rp.is_file():
        raise ApiError(404, "리포트가 없습니다")
    parts = [question.strip() or "이 날 훈련을 평가해 줘", ""]
    for kind, flag, title in (("plan", with_plan, "## 현재 훈련 방향·계획"), ("reference", with_ref, "## 참고 사전")):
        names = doc_names(kind) if flag else []
        if names:
            parts.append(title)
            for n in names:
                parts += [f"### {n[:-3]}", (doc_dir(kind) / n).read_text(encoding="utf-8-sig").replace("\r\n", "\n").strip(), ""]
    parts += [f"## {day} 훈련 리포트 (메모 포함)", rp.read_text(encoding="utf-8").replace("\r\n", "\n").strip(), ""]
    if graph in ("jumps", "full"):
        parts.append(series_text(day, graph))
    return "\n".join(parts)


def api_bundle(body: dict) -> dict:
    """AI에 보낼 내용(질문 + 선택한 계획/사전 + 리포트)을 만든다. textOnly면 텍스트만 돌려주고, 아니면 outbox에 파일로 만들고 탐색기에서 연다."""
    day, question = body.get("date"), body.get("question", "")
    if not valid_day(day) or not isinstance(question, str) or len(question) > 20000:
        raise ApiError(400, "요청 형식이 올바르지 않습니다")
    text = compose_ai_text(day, question, bool(body.get("plan")), bool(body.get("ref")),
                           body.get("graph") if body.get("graph") in ("jumps", "full") else "")
    if body.get("textOnly"):
        return {"ok": True, "text": text}
    out = CFG["outbox"] / f"AI용_{day}.md"
    with FILE_LOCK:
        atomic_write(out, text)
    return {"ok": True, "path": str(out), "revealed": reveal_in_file_manager(out), "chars": len(text)}


def api_inbox_status() -> dict:
    found, skipped = inbox_sources()
    return {"path": str(inbox_dir()), "exists": inbox_dir().is_dir(), "count": len(found), "skipped": len(skipped)}


def api_inbox_apply(body: dict) -> dict:
    """피드백 파일(`날짜_AI이름.md`)을 feedback/<AI>/날짜.md 에 반영하고, 처리한 파일은 그 폴더의 `_반영됨/`으로 옮긴다."""
    found, skipped = inbox_sources()
    results, dates = [], set()
    for f, day, ai, d in found:
        label = canon_ai(ai)
        r = {"name": f.name, "date": day, "ai": label, "dir": str(d)}
        try:
            if not valid_day(day):
                raise ApiError(400, "날짜가 올바르지 않습니다")
            if not report_path(day).is_file():
                raise ApiError(404, f"{day} 리포트가 없어 건너뜀 (먼저 가져오기)")
            if f.stat().st_size > 2 * 1024 * 1024:
                raise ApiError(413, "파일이 너무 큽니다 (2MB 초과)")
            text = f.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n").strip()
            if not text:
                raise ApiError(400, "빈 파일입니다")
            out = api_feedback_save({"date": day, "ai": label, "text": text, "mode": "append"})
            r["state"] = "dup" if out.get("duplicate") else "ok"
            done = d / "_반영됨"
            done.mkdir(exist_ok=True)
            dest = done / f.name
            if dest.exists():
                dest = done / f"{f.stem}.{datetime.now().strftime('%Y%m%d-%H%M%S')}{f.suffix}"
            os.replace(f, dest)
            dates.add(day)
        except ApiError as err:
            r["state"], r["error"] = "skip", err.message
        except (OSError, UnicodeDecodeError) as err:
            r["state"], r["error"] = "skip", f"읽지 못했습니다 ({type(err).__name__})"
        results.append(r)
    return {"ok": True, "results": results, "dates": sorted(dates), "skipped": skipped,
            "ignored": len(skipped), "path": str(inbox_dir())}


def api_save_note(body: dict) -> dict:
    day, note_id, text = body.get("date"), body.get("id"), body.get("text")
    if not valid_day(day) or not isinstance(note_id, str) or not ID_RE.match(note_id) or not isinstance(text, str):
        raise ApiError(400, "요청 형식이 올바르지 않습니다")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if MARKER_RE.search(text):
        raise ApiError(400, "메모에 메모 마커(<!-- notes ... -->)를 쓸 수 없습니다")
    if text and not text.endswith("\n"):
        text += "\n"
    if SYNC_LOCK.locked():
        raise ApiError(409, "가민 가져오기가 진행 중입니다. 끝난 뒤 다시 저장하세요", code="busy")
    with FILE_LOCK:
        path = report_path(day)
        if not path.exists():
            raise ApiError(404, "리포트가 없습니다")
        crlf = uses_crlf(path.read_bytes())
        original = path.read_text(encoding="utf-8")
        pattern = re.compile(rf"<!-- notes:{re.escape(note_id)} -->\n?.*?<!-- /notes -->", re.DOTALL)
        if not pattern.search(original):
            raise ApiError(404, "해당 메모 칸을 찾지 못했습니다")
        updated = pattern.sub(lambda m: f"<!-- notes:{note_id} -->\n{text}<!-- /notes -->", original, count=1)
        atomic_write(path, updated, crlf)
    return {"ok": True, "info": day_info(path)}


def api_upload(body: dict) -> dict:
    name, content, overwrite = body.get("name"), body.get("content"), bool(body.get("overwrite"))
    if not isinstance(name, str) or not isinstance(content, str):
        raise ApiError(400, "요청 형식이 올바르지 않습니다")
    if len(content) > 2 * 1024 * 1024:
        raise ApiError(413, "파일이 너무 큽니다 (2MB 초과)")
    crlf = "\r\n" in content
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    from_name = re.search(r"(\d{4}-\d{2}-\d{2})\.md$", name)
    from_head = re.match(r"# (\d{4}-\d{2}-\d{2})", content)
    day = from_name.group(1) if from_name else (from_head.group(1) if from_head else None)
    if not valid_day(day):
        raise ApiError(400, "날짜를 알 수 없습니다. 파일 이름을 2026-11-05.md 형식으로 바꿔 주세요")
    if from_head and from_head.group(1) != day:
        raise ApiError(400, f"파일 이름의 날짜({day})와 본문 제목의 날짜({from_head.group(1)})가 다릅니다")
    if not from_head or "훈련 리포트" not in content.splitlines()[0]:
        raise ApiError(400, "일별 훈련 리포트 형식이 아닙니다 (첫 줄이 `# 날짜 (요일) 훈련 리포트`여야 합니다)")
    with FILE_LOCK:
        path = report_path(day)
        if path.exists() and not overwrite:
            return {"ok": False, "code": "exists", "date": day,
                    "mtime": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")}
        backup = None
        if path.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = CFG["reports"] / "_backup" / f"{day}.{stamp}.md"
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(path.read_bytes())
        atomic_write(path, content, crlf)
    return {"ok": True, "date": day, "backup": backup.name if backup else None, "info": day_info(path)}


def api_rebuild(body: dict) -> dict:
    day = body.get("date")
    if not valid_day(day):
        raise ApiError(400, "날짜 형식이 올바르지 않습니다")
    if not (CFG["data"] / "raw" / day).is_dir():
        raise ApiError(404, "이 날짜의 가민 원본이 없어서 다시 만들 수 없습니다 (업로드한 파일일 수 있습니다)")
    if SYNC_LOCK.locked():
        raise ApiError(409, "가민 가져오기가 진행 중입니다", code="busy")
    with FILE_LOCK:
        code, out = run_script([str(ROOT / "garmin_report.py"), "--date", day, "--days", "1",
                                "--data", str(CFG["data"]), "--reports", str(CFG["reports"]),
                                "--corrections", str(CFG["root"])], timeout=120)
    if code != 0:
        raise ApiError(500, "리포트를 다시 만들지 못했습니다", output=out)
    return {"ok": True, "output": out, "info": day_info(report_path(day))}


def api_sync(body: dict) -> dict:
    start, end, redo = body.get("start"), body.get("end"), bool(body.get("redo"))
    if not valid_day(start) or not valid_day(end) or start > end:
        raise ApiError(400, "기간이 올바르지 않습니다")
    span = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    if span > MAX_RANGE_DAYS:
        raise ApiError(400, f"한 번에 {MAX_RANGE_DAYS}일까지만 가져올 수 있습니다")
    if not token_file().exists():
        raise ApiError(400, "가민 로그인이 필요합니다", code="need_login",
                       command="python garmin.py export")
    today = date.today().isoformat()
    days = [(date.fromisoformat(start) + timedelta(days=i)).isoformat() for i in range(span)]
    if not redo:
        days = [d for d in days if d == today or not (CFG["data"] / "raw" / d).is_dir()]
    if not days:
        return {"ok": True, "skipped": span, "results": [], "message": "이미 가져온 날짜입니다. 다시 받으려면 '다시 받기'를 체크하세요"}
    if not SYNC_LOCK.acquire(blocking=False):
        raise ApiError(409, "이미 가져오는 중입니다", code="busy")
    try:
        results = []
        for last, count in contiguous_runs(days):
            first = (date.fromisoformat(last) - timedelta(days=count - 1)).isoformat()
            # 수집 -> 리포트 순서로 직접 호출한다. (garmin.py sync 는 리포트를 앱 폴더의 reports/ 에 만들어서,
            # 동기화 폴더를 바꾼 뒤에는 새 리포트가 앱에 보이지 않았다.)
            code, out = run_script([str(ROOT / "garmin_export.py"), "--date", last, "--days", str(count),
                                    "--output", str(CFG["data"])])
            if code != 0:
                out = "수집 실패 — 리포트를 만들지 않았습니다.\n" + out
            else:
                with FILE_LOCK:
                    code, out2 = run_script([str(ROOT / "garmin_report.py"), "--date", last, "--days", str(count),
                                             "--data", str(CFG["data"]), "--reports", str(CFG["reports"]),
                                             "--corrections", str(CFG["root"])], timeout=300)
                out = (out + "\n" + out2).strip()[-4000:]
            results.append({"range": first if count == 1 else f"{first} ~ {last}", "ok": code == 0, "output": out})
            if code != 0:
                break
    finally:
        SYNC_LOCK.release()
    db = db_sync(days) if any(r["ok"] for r in results) else None
    return {"ok": all(r["ok"] for r in results), "results": results, "db": db}


# ---------------------------------------------------------------- 가민 데이터 직접 수정 (웨이트 세트)
def activity_dir(day: str, act_id: str) -> Path:
    return CFG["data"] / "raw" / day / f"activity_{act_id}"


def raw_sets(day: str, act_id: str) -> list:
    try:
        data = json.loads((activity_dir(day, act_id) / "exercise_sets.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    sets = data.get("exerciseSets") if isinstance(data, dict) else None
    return [s for s in sets if isinstance(s, dict)] if isinstance(sets, list) else []


def api_sets_get(day: str, act_id: str) -> dict:
    if not valid_day(day) or not act_id.isdigit():
        raise ApiError(400, "날짜 또는 운동 번호가 올바르지 않습니다")
    sets = raw_sets(day, act_id)
    if not sets:
        raise ApiError(404, "이 운동에는 가민 세트 기록이 없습니다")
    names, unit_cfg = load_names(ROOT / "names.toml")
    ws = [s.get("weight") for s in sets if isinstance(s.get("weight"), (int, float)) and s["weight"] > 0]
    grams = bool(ws) and max(ws) >= 1000
    fixes = {r["i"]: r for r in corr.for_activity(corr.load(CFG["root"], day), act_id)}
    rows, n = [], 0
    for s in sets:
        if s.get("setType") == "REST":
            continue
        n += 1
        code = corr.set_code(s)
        rows.append({"i": n, "code": code, "label": names.get(code) or code.replace("_", " ").title(),
                     "reps": s.get("repetitionCount") or 0,
                     "weightKg": weight_kg(s.get("weight"), unit_cfg, grams) or 0,
                     "duration": round(s.get("duration") or 0), "fix": fixes.get(n)})
    return {"date": day, "id": act_id, "sets": rows,
            "names": sorted(({"code": c, "label": l} for c, l in names.items()), key=lambda x: x["label"])}


def api_sets_save(body: dict) -> dict:
    day, act_id = str(body.get("date") or ""), str(body.get("id") or "")
    if not valid_day(day) or not act_id.isdigit():
        raise ApiError(400, "날짜 또는 운동 번호가 올바르지 않습니다")
    sets = raw_sets(day, act_id)
    if not sets:
        raise ApiError(404, "이 운동에는 가민 세트 기록이 없습니다")
    names, unit_cfg = load_names(ROOT / "names.toml")
    ws = [s.get("weight") for s in sets if isinstance(s.get("weight"), (int, float)) and s["weight"] > 0]
    grams = bool(ws) and max(ws) >= 1000
    active = [s for s in sets if s.get("setType") != "REST"]

    rows = []
    for item in body.get("sets") or []:
        if not isinstance(item, dict) or not isinstance(item.get("i"), int):
            continue
        i = item["i"]
        if not 1 <= i <= len(active):
            continue
        src = active[i - 1]
        was = corr.set_code(src)
        row = {"i": i, "was": was}
        if item.get("drop"):
            row["drop"] = True
        else:
            code = str(item.get("exercise") or "").strip().upper().replace(" ", "_")[:60]
            label = str(item.get("label") or "").strip()[:60]
            if code and code != was:
                row["exercise"] = code
            if label and label != (names.get(code or was) or ""):
                row["label"] = label
            reps = item.get("reps")
            if isinstance(reps, int) and 0 <= reps <= 999 and reps != (src.get("repetitionCount") or 0):
                row["reps"] = reps
            w = item.get("weightKg")
            if isinstance(w, (int, float)) and 0 <= w <= 999 and round(float(w), 2) != round(weight_kg(src.get("weight"), unit_cfg, grams) or 0, 2):
                row["weight_kg"] = round(float(w), 2)
        if len(row) > 2 or row.get("drop"):
            rows.append(row)

    with FILE_LOCK:
        data = corr.load(CFG["root"], day)
        acts = {k: v for k, v in (data.get("activities") or {}).items() if isinstance(v, dict)}
        if rows:
            acts[act_id] = {"sets": rows}
        else:
            acts.pop(act_id, None)
        corr.save(CFG["root"], day, acts)
    res = api_rebuild({"date": day})
    db_sync([day])
    return {"ok": True, "fixes": len(rows), "info": res.get("info")}


# ---------------------------------------------------------------- 이력 DB
def db_path() -> Path:
    return CFG["data"] / "history.db"


def db_sync(days: list[str] | None = None, create: bool = False) -> dict | None:
    """data/raw 의 하루 원본을 이력 DB에 반영한다. DB가 없으면(create가 아닐 때) 아무것도 하지 않는다. 실패해도 예외를 내지 않는다."""
    if not create and not db_path().is_file():
        return None
    try:
        with FILE_LOCK:
            return history_db.sync_all(db_path(), CFG["data"] / "raw", days, CFG["root"])
    except Exception as err:  # noqa: BLE001 - DB는 부가 기능이라 가져오기 자체를 막지 않는다
        sys.stderr.write(f"이력 DB 반영 실패: {type(err).__name__}: {err}\n")
        return {"error": f"{type(err).__name__}: {err}"}


def api_db_status() -> dict:
    st = history_db.status(db_path())
    st["path"] = str(db_path())
    st["rawDays"] = sum(1 for p in (CFG["data"] / "raw").glob("????-??-??") if p.is_dir()) if (CFG["data"] / "raw").is_dir() else 0
    return st


def api_db_sync(body: dict) -> dict:
    res = db_sync(create=True)
    if res is None or "error" in res:
        raise ApiError(500, "이력 DB에 반영하지 못했습니다" + (f": {res['error']}" if res else ""))
    return {"ok": True, "result": res, "status": api_db_status()}


def api_settings_get() -> dict:
    env = load_env_file()
    return {
        "email": env.get("GARMIN_EMAIL", ""),
        "tokenExists": token_file().exists(),
        "reportsDir": str(CFG["reports"]),
        "dataDir": str(CFG["data"]),
        "root": str(CFG["root"]),
        "rootIsDefault": CFG["root"] == ROOT,
        "loginCommand": "python garmin.py export",
        "inbox": str(inbox_dir()),
    }


def api_settings_set(body: dict) -> dict:
    email, inbox, root = body.get("email"), body.get("inbox"), body.get("root")
    if root is not None and (not isinstance(root, str) or len(root) > 500 or any(c in root for c in "\"'#\r\n\0")):
        raise ApiError(400, "동기화 폴더 경로가 올바르지 않습니다")
    if email is not None:
        if not isinstance(email, str) or len(email) > 200 or not re.fullmatch(r"[^\s\"'#=]+@[^\s\"'#=]+", email.strip()):
            raise ApiError(400, "이메일 형식이 올바르지 않습니다")
    if inbox is not None:
        if not isinstance(inbox, str) or len(inbox) > 500 or any(c in inbox for c in "\"'#\r\n\0"):
            raise ApiError(400, "피드백 폴더 경로가 올바르지 않습니다")
        inbox = inbox.strip()
        if inbox and not Path(inbox).expanduser().is_absolute():
            raise ApiError(400, "피드백 폴더는 전체 경로(예: C:\\Users\\내이름\\Downloads)로 적어 주세요")
    if email is not None:
        set_env_key("GARMIN_EMAIL", email.strip())
    if inbox is not None:
        set_env_key("FEEDBACK_INBOX", inbox)
    copied = []
    if root is not None:
        target = ROOT if not root.strip() else Path(root.strip()).expanduser()
        if not target.is_absolute() or target.resolve() != CFG["root"].resolve():   # 바뀐 경우에만 처리
            copied = change_root(root)["copied"]
    return {**api_settings_get(), "copied": copied}


# ------------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    server_version = "GarminReporter/0.1"

    def log_message(self, fmt: str, *args: object) -> None:  # 조용히
        if args and str(args[1]).startswith(("4", "5")):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- plumbing
    def _send(self, status: int, payload: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
                         "img-src 'self' data:; font-src 'self' data:; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status: int, obj: object) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        host = host.split("]")[0] + "]" if host.startswith("[") else host.split(":")[0]
        return host in CFG["hosts"]

    def _read_json(self) -> dict:
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            raise ApiError(415, "application/json 요청만 받습니다")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "Content-Length 오류") from None
        if length > MAX_BODY:
            raise ApiError(413, "요청이 너무 큽니다")
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            raise ApiError(400, "JSON 형식 오류") from None
        if not isinstance(data, dict):
            raise ApiError(400, "JSON 객체가 필요합니다")
        return data

    def _dispatch(self, method: str) -> None:
        try:
            if not self._host_ok():
                raise ApiError(403, "허용되지 않은 호스트입니다")
            url = urlparse(self.path)
            q = parse_qs(url.query)
            if method == "GET":
                if url.path in ("/", "/index.html"):
                    self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
                elif url.path == "/api/days":
                    self._json(200, api_days())
                elif url.path == "/api/report":
                    self._json(200, api_report((q.get("date") or [""])[0]))
                elif url.path == "/api/feedback":
                    self._json(200, api_feedback_get((q.get("date") or [""])[0]))
                elif url.path == "/api/sets":
                    self._json(200, api_sets_get((q.get("date") or [""])[0], (q.get("id") or [""])[0]))
                elif url.path == "/api/db":
                    self._json(200, api_db_status())
                elif url.path == "/api/series":
                    self._json(200, api_series((q.get("date") or [""])[0]))
                elif url.path == "/api/docs":
                    self._json(200, api_docs())
                elif url.path == "/api/doc":
                    self._json(200, api_doc_get((q.get("kind") or [""])[0], (q.get("name") or [""])[0]))
                elif url.path == "/api/inbox":
                    self._json(200, api_inbox_status())
                elif url.path == "/api/settings":
                    self._json(200, api_settings_get())
                else:
                    raise ApiError(404, "없는 주소입니다")
            else:
                body = self._read_json()
                routes = {
                    "/api/notes": api_save_note,
                    "/api/feedback": api_feedback_save,
                    "/api/bundle": api_bundle,
                    "/api/inbox/apply": api_inbox_apply,
                    "/api/doc": api_doc_save,
                    "/api/doc/delete": api_doc_delete,
                    "/api/upload": api_upload,
                    "/api/report/rebuild": api_rebuild,
                    "/api/garmin/sync": api_sync,
                    "/api/settings": api_settings_set,
                    "/api/snapshot": lambda body: make_snapshot(),
                    "/api/db/sync": api_db_sync,
                    "/api/sets": api_sets_save,
                }
                fn = routes.get(url.path)
                if fn is None:
                    raise ApiError(404, "없는 주소입니다")
                self._json(200, fn(body))
        except ApiError as err:
            self._json(err.status, {"error": err.message, **err.extra})
        except Exception as err:  # noqa: BLE001
            sys.stderr.write(f"오류: {type(err).__name__}: {err}\n")
            self._json(500, {"error": f"서버 오류: {type(err).__name__}"})

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")


def main() -> int:
    p = argparse.ArgumentParser(description="Garmin 리포트 웹앱")
    p.add_argument("--host", default="127.0.0.1", help="기본 127.0.0.1 (본인 PC에서만 접속)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--reports", type=Path, default=None, help="리포트 폴더 (생략하면 동기화 폴더 설정/reports)")
    p.add_argument("--data", type=Path, default=ROOT / "data")
    p.add_argument("--no-browser", action="store_true", help="브라우저를 자동으로 열지 않음")
    args = p.parse_args()
    CFG["data"] = args.data.resolve()
    if args.reports:   # 예전 방식: 리포트 폴더의 부모를 동기화 폴더로 본다
        apply_root(args.reports.resolve().parent)
        CFG["reports"] = args.reports.resolve()
    else:
        apply_root(configured_root().resolve())
    n = migrate_feedback()
    m = merge_ai_folders()
    snap = auto_snapshot()
    if snap:
        print(f"이번 달 백업 스냅샷을 만들었습니다: {snap}")
    if m:
        print(f"이름이 다른 AI 피드백 폴더 {m}개를 하나로 합쳤습니다")
    if n:
        print(f"예전 피드백 파일 {n}개를 AI별 폴더로 옮겼습니다 (원본: feedback/_이전)")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        CFG["hosts"].add(args.host.lower())
        print("주의: 로컬 주소가 아닌 곳에 열었습니다. 이 앱에는 로그인 보호가 없습니다.", file=sys.stderr)
    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as err:
        print(f"{args.host}:{args.port} 를 열 수 없습니다 ({err}). --port 로 다른 번호를 지정하세요.", file=sys.stderr)
        return 1
    url = f"http://{'127.0.0.1' if args.host in ('localhost', '::1') else args.host}:{args.port}/"
    print(f"동기화 폴더: {CFG['root']}\n리포트 폴더: {CFG['reports']}\n실행 중: {url}  (종료: Ctrl+C)")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
