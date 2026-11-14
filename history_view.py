"""이력 DB(data/history.db)에서 기간별(주·월·직접 지정) 요약을 만든다. DB는 읽기만 한다.

화면(JSON), AI에 붙여 넣을 텍스트(마크다운), CSV 세 가지를 같은 집계에서 만든다.
주는 월요일 시작. 시간은 초, 거리는 미터로 저장돼 있고 여기서 시간/km로 바꾼다.
"""
from __future__ import annotations

import csv
import io
import sqlite3
from datetime import date, timedelta
from pathlib import Path

WEEKDAYS = "월화수목금토일"
GROUPS = {   # 활동 종류(type_key) -> (묶음 이름, 표시 순서)
    "running": ("러닝", 0), "treadmill_running": ("러닝", 0), "track_running": ("러닝", 0),
    "strength_training": ("웨이트", 1), "tennis_v2": ("테니스", 2), "tennis": ("테니스", 2),
    "indoor_cycling": ("사이클", 3), "cycling": ("사이클", 3), "walking": ("걷기", 4),
}
ORDER = ["러닝", "웨이트", "테니스", "사이클", "걷기", "기타"]


def group_of(type_key: str | None) -> str:
    return GROUPS.get(type_key or "", ("기타", 9))[0]


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def period(mode: str, start: date, end: date) -> tuple[date, date]:
    """비교할 바로 앞 기간. week=앞의 7일, month=앞의 달, 그 밖에는 같은 길이."""
    if mode == "month":
        pe = start - timedelta(days=1)
        return pe.replace(day=1), pe
    n = (end - start).days + 1
    return start - timedelta(days=n), start - timedelta(days=1)


def connect_ro(db_path: Path) -> sqlite3.Connection | None:
    p = Path(db_path)
    if not p.is_file():
        return None
    db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def _avg(vals) -> float | None:
    v = [x for x in vals if x is not None]
    return round(sum(v) / len(v), 1) if v else None


def summarize(db: sqlite3.Connection, start: str, end: str) -> dict:
    acts = [dict(r) for r in db.execute(
        "SELECT * FROM activities WHERE day BETWEEN ? AND ? ORDER BY start_local", (start, end))]
    by_type: dict[str, dict] = {}
    for a in acts:
        g = by_type.setdefault(group_of(a["type_key"]), {"n": 0, "sec": 0.0, "m": 0.0, "load": 0.0, "hrw": 0.0, "hrs": 0.0,
                                                          "sets": 0, "reps": 0})
        g["n"] += 1
        g["sec"] += a["duration_s"] or 0
        g["m"] += a["distance_m"] or 0
        g["load"] += a["training_load"] or 0
        if a["avg_hr"] and a["duration_s"]:
            g["hrw"] += a["avg_hr"] * a["duration_s"]
            g["hrs"] += a["duration_s"]
        g["sets"] += a["total_sets"] or 0
        g["reps"] += a["total_reps"] or 0
    types = []
    for name in ORDER:
        g = by_type.get(name)
        if g:
            types.append({"name": name, "count": g["n"], "hours": round(g["sec"] / 3600, 2),
                          "km": round(g["m"] / 1000, 1) if name in ("러닝", "걷기", "사이클") else None,
                          "avgHr": round(g["hrw"] / g["hrs"]) if g["hrs"] else None,
                          "load": round(g["load"]), "sets": g["sets"] or None, "reps": g["reps"] or None})
    zones = [round(sum(a[f"z{i}"] or 0 for a in acts) / 60) for i in range(0, 6)]
    wellness = [dict(r) for r in db.execute(
        """SELECT d.day, d.resting_hr, d.steps, s.total_s, s.score, h.hrv
           FROM (SELECT day FROM daily UNION SELECT day FROM sleep) k
           LEFT JOIN daily d ON d.day=k.day LEFT JOIN sleep s ON s.day=k.day LEFT JOIN health_status h ON h.day=k.day
           WHERE k.day BETWEEN ? AND ? ORDER BY k.day""", (start, end))]
    load = db.execute("SELECT * FROM acute_load WHERE day<=? ORDER BY day DESC LIMIT 1", (end,)).fetchone()
    tot_sec = sum(g["sec"] for g in by_type.values())
    run = by_type.get("러닝")
    return {
        "start": start, "end": end, "days": (date.fromisoformat(end) - date.fromisoformat(start)).days + 1,
        "activeDays": len({a["day"] for a in acts}),
        "sessions": len(acts), "hours": round(tot_sec / 3600, 2),
        "runKm": round(run["m"] / 1000, 1) if run else 0.0,
        "sets": sum(g["sets"] for g in by_type.values()) or 0,
        "load": round(sum(g["load"] for g in by_type.values())),
        "types": types, "zonesMin": zones,
        "sleepH": _avg([w["total_s"] / 3600 for w in wellness if w["total_s"]]),
        "sleepScore": _avg([w["score"] for w in wellness]),
        "hrv": _avg([w["hrv"] for w in wellness]),
        "restHr": _avg([w["resting_hr"] for w in wellness]),
        "acwr": ({"day": load["day"], "acute": load["acute"], "chronic": load["chronic"],
                  "ratio": load["acwr"], "status": load["status"]} if load else None),
    }


def day_rows(db: sqlite3.Connection, start: str, end: str) -> list[dict]:
    acts: dict[str, list] = {}
    for r in db.execute("SELECT * FROM activities WHERE day BETWEEN ? AND ? ORDER BY start_local", (start, end)):
        acts.setdefault(r["day"], []).append(dict(r))
    well = {r["day"]: dict(r) for r in db.execute(
        """SELECT k.day, d.resting_hr, s.total_s, s.score, h.hrv
           FROM (SELECT day FROM daily UNION SELECT day FROM sleep) k
           LEFT JOIN daily d ON d.day=k.day LEFT JOIN sleep s ON s.day=k.day LEFT JOIN health_status h ON h.day=k.day
           WHERE k.day BETWEEN ? AND ?""", (start, end))}
    out, d = [], date.fromisoformat(start)
    while d <= date.fromisoformat(end):
        k = d.isoformat()
        items = []
        for a in acts.get(k, []):
            items.append({"type": group_of(a["type_key"]), "name": a["name"], "min": round((a["duration_s"] or 0) / 60),
                          "km": round((a["distance_m"] or 0) / 1000, 1) if group_of(a["type_key"]) in ("러닝", "걷기", "사이클") else None,
                          "hr": round(a["avg_hr"]) if a["avg_hr"] else None, "sets": a["total_sets"] or None,
                          "load": round(a["training_load"]) if a["training_load"] else None})
        w = well.get(k, {})
        if items or w:
            out.append({"date": k, "weekday": WEEKDAYS[d.weekday()], "acts": items,
                        "sleepH": round(w["total_s"] / 3600, 1) if w.get("total_s") else None,
                        "sleepScore": w.get("score"), "hrv": w.get("hrv"), "restHr": w.get("resting_hr")})
        d += timedelta(days=1)
    return out


def weekly(db: sqlite3.Connection, start: str, end: str) -> list[dict]:
    """기간 안의 주(월요일 시작)별 요약. 기간 밖 날짜는 잘라서 센다."""
    out, d = [], week_start(date.fromisoformat(start))
    last = date.fromisoformat(end)
    while d <= last:
        a, b = max(d, date.fromisoformat(start)), min(d + timedelta(days=6), last)
        s = summarize(db, a.isoformat(), b.isoformat())
        out.append({"start": a.isoformat(), "end": b.isoformat(), "sessions": s["sessions"], "hours": s["hours"],
                    "runKm": s["runKm"], "sets": s["sets"], "load": s["load"], "sleepH": s["sleepH"], "hrv": s["hrv"]})
        d += timedelta(days=7)
    return out


def build(db_path: Path, mode: str, start: str, end: str) -> dict:
    db = connect_ro(db_path)
    if db is None:
        return {"exists": False}
    try:
        cur = summarize(db, start, end)
        ps, pe = period(mode, date.fromisoformat(start), date.fromisoformat(end))
        prev = summarize(db, ps.isoformat(), pe.isoformat())
        rng = (db.execute("SELECT MIN(day), MAX(day) FROM activities").fetchone())
        res = {"exists": True, "mode": mode, "cur": cur, "prev": prev, "days": day_rows(db, start, end),
               "weeks": weekly(db, start, end) if cur["days"] > 10 else [], "dbFrom": rng[0], "dbTo": rng[1]}
        res["text"] = to_text(res)
        return res
    finally:
        db.close()


def _fmt(v, unit="", dash="-"):
    return dash if v is None else f"{v:g}{unit}"


def _delta(cur, prev, unit="", digits=1):
    if cur is None or prev is None:
        return ""
    d = round(cur - prev, digits)
    return f" ({'+' if d > 0 else ''}{d:g}{unit})" if d else " (동일)"


def to_text(res: dict) -> str:
    c, p = res["cur"], res["prev"]
    label = {"week": "주간", "month": "월간"}.get(res["mode"], "기간")
    L = [f"## {c['start']} ~ {c['end']} {label} 요약 (Garmin 기록, 손목 심박 기준)", "",
         f"비교 기간: {p['start']} ~ {p['end']}", ""]
    L += [f"- 운동 {c['sessions']}회 / {c['activeDays']}일, 총 {c['hours']:g}시간{_delta(c['hours'], p['hours'], '시간', 2)}",
          f"- 러닝 거리 {c['runKm']:g}km{_delta(c['runKm'], p['runKm'], 'km')}",
          f"- 웨이트 {c['sets']}세트{_delta(c['sets'], p['sets'], '세트', 0)}",
          f"- 훈련 부하 합 {c['load']}{_delta(c['load'], p['load'], '', 0)}",
          f"- 수면 평균 {_fmt(c['sleepH'], '시간')}{_delta(c['sleepH'], p['sleepH'], '시간')}, 점수 {_fmt(c['sleepScore'])}",
          f"- HRV 평균 {_fmt(c['hrv'])}{_delta(c['hrv'], p['hrv'])}, 안정시 심박 {_fmt(c['restHr'], 'bpm')}{_delta(c['restHr'], p['restHr'], 'bpm')}"]
    if c["acwr"]:
        a = c["acwr"]
        L.append(f"- 급성/만성 부하 {a['acute']:g}/{a['chronic']:g} (ACWR {a['ratio']:g}, {a['status']}, {a['day']} 기준)")
    z = c["zonesMin"]
    L.append(f"- 심박존 시간(분): Z1 미만 {z[0]}, Z1 {z[1]}, Z2 {z[2]}, Z3 {z[3]}, Z4 {z[4]}, Z5 {z[5]}")
    if c["types"]:
        L += ["", "종류별:", "", "| 종류 | 횟수 | 시간 | 거리 | 평균심박 | 세트 | 부하 |", "|---|---|---|---|---|---|---|"]
        for t in c["types"]:
            L.append(f"| {t['name']} | {t['count']} | {t['hours']:g}h | {_fmt(t['km'], 'km')} | {_fmt(t['avgHr'])} | {_fmt(t['sets'])} | {t['load']} |")
    if res["weeks"]:
        L += ["", "주별:", "", "| 주 | 운동 | 시간 | 러닝 | 세트 | 부하 | 수면 | HRV |", "|---|---|---|---|---|---|---|---|"]
        for w in res["weeks"]:
            L.append(f"| {w['start'][5:]}~{w['end'][5:]} | {w['sessions']} | {w['hours']:g}h | {w['runKm']:g}km | {w['sets']} | {w['load']} | {_fmt(w['sleepH'], 'h')} | {_fmt(w['hrv'])} |")
    L += ["", "날짜별:", ""]
    for d in res["days"]:
        acts = ", ".join(
            f"{a['type']} {a['min']}분" + (f" {a['km']:g}km" if a["km"] else "") + (f" 심박{a['hr']}" if a["hr"] else "")
            + (f" {a['sets']}세트" if a["sets"] and a["type"] == "웨이트" else "") for a in d["acts"]) or "휴식"
        extra = []
        if d["sleepH"]:
            extra.append(f"수면 {d['sleepH']:g}h")
        if d["hrv"]:
            extra.append(f"HRV {d['hrv']:g}")
        L.append(f"- {d['date'][5:]}({d['weekday']}) {acts}" + (f" · {' · '.join(extra)}" if extra else ""))
    return "\n".join(L) + "\n"


def to_csv(db_path: Path, kind: str, start: str, end: str) -> str:
    db = connect_ro(db_path)
    if db is None:
        return ""
    try:
        buf = io.StringIO()
        w = csv.writer(buf)
        if kind == "days":
            w.writerow(["날짜", "요일", "운동", "수면(시간)", "수면점수", "HRV", "안정시심박"])
            for d in day_rows(db, start, end):
                w.writerow([d["date"], d["weekday"], " / ".join(f"{a['type']} {a['min']}분" for a in d["acts"]),
                            d["sleepH"], d["sleepScore"], d["hrv"], d["restHr"]])
        else:
            cols = [r[1] for r in db.execute("PRAGMA table_info(activities)")]
            w.writerow(cols)
            w.writerows(tuple(r) for r in db.execute(
                "SELECT * FROM activities WHERE day BETWEEN ? AND ? ORDER BY start_local", (start, end)))
        return buf.getvalue()
    finally:
        db.close()
