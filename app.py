"""Garmin 리포트 웹앱 서버. 표준 라이브러리만 쓴다 (가민 수집만 garminconnect 필요).

    python app.py                  # http://127.0.0.1:8765 를 열고 브라우저로 사용
    python app.py --port 9000 --no-browser

- 리포트(.md)가 원본이다. 메모는 .md의 `<!-- notes:ID -->` 마커 안쪽만 고친다.
- 가져오기는 기존 garmin.py sync / garmin_report.py 를 그대로 호출한다.
- 127.0.0.1에만 열고, Host/Content-Type을 검사해서 다른 웹페이지가 이 서버를 건드리지 못하게 한다.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import webbrowser
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from garmin_report import NOTE_RE

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
    "reports": ROOT / "reports",
    "data": ROOT / "data",
    "feedback": ROOT / "feedback",
    "outbox": ROOT / "outbox",
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


def feedback_path(day: str) -> Path:
    return CFG["feedback"] / f"{day}.md"


def plan_path() -> Path:
    return CFG["reports"].parent / "plan" / "방향.md"


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
        "hasFeedback": feedback_path(day).is_file(),
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
class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra: object):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


def api_days() -> dict:
    return {"today": date.today().isoformat(), "days": list_days(), "reportsDir": str(CFG["reports"]),
            "hasPlan": plan_path().is_file()}


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
    path = feedback_path(day)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    return {"date": day, "text": text}


def api_feedback_save(body: dict) -> dict:
    """mode=set: 통째로 저장(빈 문자열이면 파일 삭제) / mode=append: 기존 내용 뒤에 구분선과 함께 추가."""
    day, text, mode = body.get("date"), body.get("text"), body.get("mode", "set")
    if not valid_day(day) or not isinstance(text, str) or mode not in ("set", "append"):
        raise ApiError(400, "요청 형식이 올바르지 않습니다")
    if len(text) > 2 * 1024 * 1024:
        raise ApiError(413, "내용이 너무 큽니다 (2MB 초과)")
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    with FILE_LOCK:
        path = feedback_path(day)
        old = path.read_text(encoding="utf-8").strip("\n") if path.is_file() else ""
        if mode == "append":
            if not text:
                raise ApiError(400, "추가할 내용이 없습니다")
            if text in old:
                return {"ok": True, "text": old + "\n", "duplicate": True}
            text = f"{old}\n\n---\n\n{text}" if old else text
        if not text.strip():
            if path.is_file():
                path.unlink()
            return {"ok": True, "text": ""}
        atomic_write(path, text + "\n")
    return {"ok": True, "text": text + "\n"}


def api_bundle(body: dict) -> dict:
    """AI에 첨부할 파일 하나(질문 + 훈련 방향 + 리포트)를 outbox에 만들고, 탐색기에서 선택해 연다."""
    day, question, with_plan = body.get("date"), body.get("question", ""), bool(body.get("plan"))
    if not valid_day(day) or not isinstance(question, str) or len(question) > 20000:
        raise ApiError(400, "요청 형식이 올바르지 않습니다")
    rp = report_path(day)
    if not rp.is_file():
        raise ApiError(404, "리포트가 없습니다")
    parts = [question.strip() or "이 날 훈련을 평가해 줘", ""]
    if with_plan and plan_path().is_file():
        parts += ["## 현재 훈련 방향", plan_path().read_text(encoding="utf-8").strip(), ""]
    parts += [f"## {day} 훈련 리포트 (메모 포함)", rp.read_text(encoding="utf-8").replace("\r\n", "\n").strip(), ""]
    out = CFG["outbox"] / f"AI용_{day}.md"
    with FILE_LOCK:
        atomic_write(out, "\n".join(parts))
    return {"ok": True, "path": str(out), "revealed": reveal_in_file_manager(out), "chars": len("\n".join(parts))}


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
                                "--data", str(CFG["data"]), "--reports", str(CFG["reports"])], timeout=120)
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
            code, out = run_script([str(ROOT / "garmin.py"), "sync", "--date", last, "--days", str(count)])
            results.append({"range": first if count == 1 else f"{first} ~ {last}", "ok": code == 0, "output": out})
            if code != 0:
                break
    finally:
        SYNC_LOCK.release()
    return {"ok": all(r["ok"] for r in results), "results": results}


def api_settings_get() -> dict:
    env = load_env_file()
    return {
        "email": env.get("GARMIN_EMAIL", ""),
        "tokenExists": token_file().exists(),
        "reportsDir": str(CFG["reports"]),
        "dataDir": str(CFG["data"]),
        "loginCommand": "python garmin.py export",
    }


def api_settings_set(body: dict) -> dict:
    email = body.get("email")
    if not isinstance(email, str) or len(email) > 200 or not re.fullmatch(r"[^\s\"'#=]+@[^\s\"'#=]+", email.strip()):
        raise ApiError(400, "이메일 형식이 올바르지 않습니다")
    set_env_key("GARMIN_EMAIL", email.strip())
    return api_settings_get()


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
                    "/api/upload": api_upload,
                    "/api/report/rebuild": api_rebuild,
                    "/api/garmin/sync": api_sync,
                    "/api/settings": api_settings_set,
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
    p.add_argument("--reports", type=Path, default=ROOT / "reports")
    p.add_argument("--data", type=Path, default=ROOT / "data")
    p.add_argument("--no-browser", action="store_true", help="브라우저를 자동으로 열지 않음")
    args = p.parse_args()
    CFG["reports"], CFG["data"] = args.reports.resolve(), args.data.resolve()
    CFG["feedback"] = CFG["reports"].parent / "feedback"
    CFG["outbox"] = CFG["reports"].parent / "outbox"
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        CFG["hosts"].add(args.host.lower())
        print("주의: 로컬 주소가 아닌 곳에 열었습니다. 이 앱에는 로그인 보호가 없습니다.", file=sys.stderr)
    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as err:
        print(f"{args.host}:{args.port} 를 열 수 없습니다 ({err}). --port 로 다른 번호를 지정하세요.", file=sys.stderr)
        return 1
    url = f"http://{'127.0.0.1' if args.host in ('localhost', '::1') else args.host}:{args.port}/"
    print(f"리포트 폴더: {CFG['reports']}\n실행 중: {url}  (종료: Ctrl+C)")
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
