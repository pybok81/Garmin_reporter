"""주간 리포트(reports/weekly/YYYY-Www.md) 만들기. 숫자는 이력 DB에서 가져온다.

- 주는 월요일 시작, 이름은 ISO 주 번호(2026-W47).
- 아래 '주간 메모' 칸(<!-- notes:week -->)은 다시 만들어도 보존한다.
- 진행 중인 주에는 경고 줄을 붙인다. 주가 끝난 뒤 다시 만들면 사라진다.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import history_view as hv
from garmin_report import NOTE_RE

WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")
NOTE_TEMPLATE = """- 한 주 컨디션·피로 흐름:
- 계획 대비 (한 것 / 못 한 것):
- 통증·이상 (부위·날짜):
- 다음 주 조정할 것:
"""


def valid_week(value: object) -> bool:
    if not isinstance(value, str) or not WEEK_RE.match(value):
        return False
    try:
        week_range(value)
    except ValueError:
        return False
    return True


def week_key(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def week_range(key: str) -> tuple[date, date]:
    m = WEEK_RE.match(key)
    if not m:
        raise ValueError(key)
    start = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
    return start, start + timedelta(days=6)


def weeks_with_activity(db: sqlite3.Connection, only_days: list[str] | None = None) -> list[str]:
    """운동 기록이 하나라도 있는 주 이름들. only_days 가 있으면 그 날짜가 속한 주만."""
    if only_days is not None:
        want = {week_key(date.fromisoformat(d)) for d in only_days}
    else:
        want = None
    found = {week_key(date.fromisoformat(r[0])) for r in db.execute("SELECT DISTINCT day FROM activities")}
    return sorted(found if want is None else found & want)


def _row(label, cur, prev, unit="", dg=1):
    def f(v):
        return "-" if v is None else f"{v:g}{unit}"
    d = "" if cur is None or prev is None else (lambda x: "동일" if not x else f"{'+' if x > 0 else ''}{x:g}{unit}")(round(cur - prev, dg))
    return f"| {label} | {f(cur)} | {f(prev)} | {d} |"


def build(db_path: Path, key: str, existing: str | None, today: date) -> str | None:
    """주간 리포트 마크다운. DB가 없으면 None. existing 은 기존 파일 내용(메모 보존용)."""
    start, end = week_range(key)
    db = hv.connect_ro(db_path)
    if db is None:
        return None
    try:
        a, b = start.isoformat(), end.isoformat()
        cur = hv.summarize(db, a, b)
        ps, pe = hv.period("week", start, end)
        prev = hv.summarize(db, ps.isoformat(), pe.isoformat())
        days = hv.day_rows(db, a, b)
    finally:
        db.close()
    saved = {m.group(1): m.group(2) for m in NOTE_RE.finditer(existing or "")}
    body = saved.get("week", NOTE_TEMPLATE)
    L = [f"# {key} ({start:%m-%d} ~ {end:%m-%d}) 주간 리포트", ""]
    if end >= today:
        L += [f"> ⚠️ 진행 중인 주입니다. {today.isoformat()} 기준으로 만든 내용이고, 주가 끝난 뒤 다시 만들면 갱신됩니다.", ""]
    L += [f"기간: {a} ~ {b} · 비교: {ps.isoformat()} ~ {pe.isoformat()} (전주) · 심박은 손목 센서 기준", "",
          "## 주간 요약", "", "| 항목 | 이번 주 | 전주 | 차이 |", "|---|---|---|---|",
          f"| 운동 | {cur['sessions']}회 / {cur['activeDays']}일 | {prev['sessions']}회 / {prev['activeDays']}일 | "
          f"{cur['sessions'] - prev['sessions']:+d}회 |",
          _row("총 시간", cur["hours"], prev["hours"], "h", 2),
          _row("러닝 거리", cur["runKm"], prev["runKm"], "km"),
          _row("웨이트 세트", cur["sets"], prev["sets"], "", 0),
          _row("훈련 부하 합", cur["load"], prev["load"], "", 0),
          _row("수면 평균", cur["sleepH"], prev["sleepH"], "h"),
          _row("HRV 평균", cur["hrv"], prev["hrv"]),
          _row("안정시 심박 평균", cur["restHr"], prev["restHr"], "bpm")]
    if cur["acwr"]:
        ac = cur["acwr"]
        L.append(f"| 급성/만성 부하 | {hv._fmt(ac['acute'])} / {hv._fmt(ac['chronic'])} (ACWR {hv._fmt(ac['ratio'])}, {ac['status'] or '-'}) | | {ac['day']} 기준 |")
    if cur["types"]:
        L += ["", "## 종류별", "", "| 종류 | 횟수 | 시간 | 거리 | 평균 심박 | 세트 | 부하 |", "|---|---|---|---|---|---|---|"]
        for t in cur["types"]:
            L.append(f"| {t['name']} | {t['count']} | {t['hours']:g}h | {hv._fmt(t['km'], 'km')} | {hv._fmt(t['avgHr'])} | "
                     f"{hv._fmt(t['sets'])} | {t['load']} |")
    z = cur["zonesMin"]
    L += ["", "## 심박존 시간 (분)", "",
          f"Z1 미만 {z[0]} · Z1 {z[1]} · Z2 {z[2]} · Z3 {z[3]} · Z4 {z[4]} · Z5 {z[5]}", "", "## 날짜별", ""]
    by_day = {d["date"]: d for d in days}
    for i in range(7):
        d = start + timedelta(days=i)
        if d > today:
            break
        r = by_day.get(d.isoformat())
        acts = ", ".join(
            f"{x['type']} {x['min']}분" + (f" {x['km']:g}km" if x["km"] else "") + (f" 심박 {x['hr']}" if x["hr"] else "")
            + (f" {x['sets']}세트" if x["sets"] and x["type"] == "웨이트" else "") for x in (r["acts"] if r else [])) or "휴식"
        extra = []
        if r and r["sleepH"]:
            extra.append(f"수면 {r['sleepH']:g}h")
        if r and r["hrv"]:
            extra.append(f"HRV {r['hrv']:g}")
        L.append(f"- {d:%m-%d}({hv.WEEKDAYS[d.weekday()]}) {acts}" + (f" · {' · '.join(extra)}" if extra else ""))
    L += ["", "## 주간 메모", f"<!-- notes:week -->\n{body}<!-- /notes -->", ""]
    return "\n".join(L).rstrip() + "\n"
