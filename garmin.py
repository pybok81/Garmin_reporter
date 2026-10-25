"""상위 진입점: 하나의 명령으로 수집·리포트를 옵션처럼 실행한다.

    python garmin.py sync                 # 오늘 수집 + 리포트
    python garmin.py sync --date 2026-10-20
    python garmin.py sync --days 7        # 백필
    python garmin.py export               # 수집만 (원본 JSON)
    python garmin.py report               # 리포트만 (로그인 없이 재생성)
    python garmin.py open                 # 오늘 리포트(.md)를 기본 앱으로 열기
    python garmin.py view                 # 보기 좋은 HTML로 변환해 브라우저로 열기 (읽기 전용)

내부적으로는 garmin_export.py / garmin_report.py를 그대로 호출하므로
문제가 생기면 각 스크립트를 따로 실행해 원인을 좁힐 수 있다.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import webbrowser
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent


def run(script: str, args: argparse.Namespace) -> int:
    cmd = [sys.executable, str(HERE / script), "--date", args.date, "--days", str(args.days)]
    print(f"$ {' '.join(cmd[1:])}")
    return subprocess.call(cmd)


def open_report(args: argparse.Namespace) -> int:
    path = Path(args.reports) / f"{args.date}.md"
    if not path.exists():
        print(f"{path} 없음 — 먼저 `garmin.py sync --date {args.date}` 를 실행하세요.", file=sys.stderr)
        return 1
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.call(["open", str(path)])
    else:
        subprocess.call(["xdg-open", str(path)])
    print(path.resolve())
    return 0


CSS = """
:root{--bg:#fafaf8;--fg:#1c1c1a;--muted:#6b6b66;--line:#dcdcd5;--head:#efeee9;--accent:#2a6f5c;--warn-bg:#fff3d6;--warn:#7a5200}
@media (prefers-color-scheme:dark){:root{--bg:#161715;--fg:#e8e8e3;--muted:#9a9a93;--line:#3a3b37;--head:#232420;--accent:#6fc2a8;--warn-bg:#3a3014;--warn:#f0c866}}
body{background:var(--bg);color:var(--fg);font:16px/1.65 -apple-system,"Segoe UI","Malgun Gothic",sans-serif;margin:0}
main{max-width:860px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:1.6rem;margin:.2em 0 .6em}
h2{font-size:1.2rem;margin:1.8em 0 .5em;padding-bottom:.25em;border-bottom:2px solid var(--accent)}
h3{font-size:1.05rem;margin:1.6em 0 .5em;color:var(--accent)}
table{border-collapse:collapse;width:100%;margin:.6em 0;display:block;overflow-x:auto;font-size:.93rem}
th,td{border:1px solid var(--line);padding:6px 10px;text-align:left;white-space:nowrap}
td:last-child{white-space:normal}
th{background:var(--head)}
blockquote{margin:.8em 0;padding:.4em 1em;border-left:4px solid var(--warn);background:var(--warn-bg);color:var(--warn)}
ul{padding-left:1.3em}li{margin:.15em 0}
strong{font-weight:650}
.foot{color:var(--muted);font-size:.85rem;margin-top:2.5em}
"""


def view_report(args: argparse.Namespace) -> int:
    try:
        import markdown
    except ImportError:
        print("`pip install -r requirements.txt` 로 markdown 패키지를 먼저 설치하세요.", file=sys.stderr)
        return 1
    src = Path(args.reports) / f"{args.date}.md"
    if not src.exists():
        print(f"{src} 없음 — 먼저 `garmin.py sync --date {args.date}` 를 실행하세요.", file=sys.stderr)
        return 1
    text = src.read_text(encoding="utf-8")
    # 메모 마커는 HTML 주석이라 바로 뒤의 목록이 렌더링되지 않는다 → 빈 줄로 바꿔서 보이게 한다.
    text = re.sub(r"<!-- /?notes[^>]*-->", "\n", text)
    body = markdown.markdown(text, extensions=["tables", "sane_lists"])
    html = (f'<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{args.date} 훈련 리포트</title><style>{CSS}</style></head><body><main>{body}'
            f'<p class="foot">읽기 전용 화면입니다. 메모는 .md 파일의 메모 칸에 쓰세요 ({src.name})</p></main></body></html>')
    out_dir = Path(args.reports) / "_view"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{args.date}.html"
    out.write_text(html, encoding="utf-8")
    webbrowser.open(out.resolve().as_uri())
    print(out.resolve())
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Garmin 수집/리포트 통합 명령")
    p.add_argument("command", choices=["sync", "export", "report", "open", "view"],
                   help="sync=수집+리포트, export=수집만, report=리포트만, open=md 열기, view=HTML로 보기")
    p.add_argument("--date", default=date.today().isoformat(), help="기준 날짜 (기본: 오늘)")
    p.add_argument("--days", type=int, default=1, help="--date 포함 과거 N일 (백필용)")
    p.add_argument("--reports", default="reports", help="open이 찾을 리포트 폴더")
    args = p.parse_args()

    if args.command == "export":
        return run("garmin_export.py", args)
    if args.command == "report":
        return run("garmin_report.py", args)
    if args.command == "open":
        return open_report(args)
    if args.command == "view":
        return view_report(args)

    # sync: 수집이 실패하면 오래된 원본으로 리포트를 만들지 않고 멈춘다.
    code = run("garmin_export.py", args)
    if code != 0:
        print("수집 실패 — 리포트를 만들지 않았습니다. `garmin.py export`만 따로 실행해 확인하세요.", file=sys.stderr)
        return code
    return run("garmin_report.py", args)


if __name__ == "__main__":
    raise SystemExit(main())
