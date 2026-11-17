"""주간 리포트(reports/weekly/YYYY-Www.md) 만들기. 숫자는 이력 DB에서 가져온다.

- 주는 월요일 시작, 이름은 ISO 주 번호(2026-W47).
- 아래 '주간 메모' 칸(<!-- notes:week -->)은 다시 만들어도 보존한다.
- 진행 중인 주에는 경고 줄을 붙인다. 주가 끝난 뒤 다시 만들면 사라진다.
"""
from __future__ import annotations

import re
import sqlite3
import tomllib
from datetime import date, timedelta
from pathlib import Path

import corrections as corr
import garmin_report as gr
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


GROUPS = ("푸시", "풀", "하체", "코어")


def weight_summary(data_root: Path, corr_root: Path, start: date, end: date, names_path: Path) -> dict:
    """주(또는 기간)의 웨이트 세트를 푸시/풀/하체/코어로 센다. 가민 원본(data/raw)과 직접 수정(corrections)에서 계산한다.

    일일 리포트와 같은 분류 함수(garmin_report.classify_sets)를 쓰므로 수정한 세트는 수정값으로 센다.
    세트·반복 수만 센다(무게 볼륨은 쓰지 않음). 워밍업도 함께 센다.
    """
    cfg = tomllib.loads(names_path.read_text(encoding="utf-8")) if names_path.is_file() else {}
    names, unit_cfg = gr.load_names(names_path)          # PARTS·BODYWEIGHT 도 같이 채운다
    groups = {str(k).upper(): str(v) for k, v in (cfg.get("groups") or {}).items()}
    targets = {str(k).upper(): v for k, v in (cfg.get("targets") or {}).items() if isinstance(v, (int, float))}
    home_max = float((cfg.get("settings") or {}).get("home_hinge_max_kg", 20))

    tot = {g: [0, 0] for g in GROUPS}
    sub = {"PULL_UP": [0, 0], "PUSH_UP": [0, 0]}
    hinge = {"gym": 0, "home": 0, "noweight": 0}
    unclassified: dict[str, int] = {}
    by_code: dict[str, int] = {}
    n_days, n_sets = 0, 0
    d = start
    while d <= end:
        day = d.isoformat()
        d += timedelta(days=1)
        raw = Path(data_root) / "raw" / day
        if not raw.is_dir():
            continue
        n_days += 1
        fixes = corr.load(corr_root, day) if corr_root else {}
        for adir in sorted(raw.glob("activity_*")):
            c = gr.classify_sets(gr.load_json(adir / "exercise_sets.json"), names, unit_cfg,
                                 corr.for_activity(fixes, adir.name[len("activity_"):]))
            for r in (c["rows"] if c else []):
                n_sets += 1
                codes = [r["name"], r["category"]]
                grp = next((groups[x] for x in codes if x in groups), None) or groups.get(r["part"].upper())
                if grp not in tot:
                    unclassified[r["base_label"]] = unclassified.get(r["base_label"], 0) + 1
                    continue
                tot[grp][0] += 1
                tot[grp][1] += r["reps"]
                for k in sub:
                    if k in codes:
                        sub[k][0] += 1
                        sub[k][1] += r["reps"]
                for x in codes:
                    if x:
                        by_code[x] = by_code.get(x, 0) + 1
                if any(x.endswith("DEADLIFT") for x in codes):      # 힌지류만 홈/웨이트를 나눈다
                    kg = r["kg"]
                    if any("KETTLEBELL" in x for x in codes) or (kg and kg <= home_max):
                        hinge["home"] += 1
                    else:
                        hinge["gym"] += 1
                        if not kg:
                            hinge["noweight"] += 1
    done = []
    for code, goal in targets.items():
        done.append({"label": names.get(code) or gr.prettify(code), "done": by_code.get(code, 0), "goal": goal})
    return {"rawDays": n_days, "sets": n_sets, "groups": {g: {"sets": v[0], "reps": v[1]} for g, v in tot.items()},
            "pullUp": {"sets": sub["PULL_UP"][0], "reps": sub["PULL_UP"][1]},
            "pushUp": {"sets": sub["PUSH_UP"][0], "reps": sub["PUSH_UP"][1]},
            "hinge": hinge, "homeMaxKg": home_max,
            "unclassified": [{"name": k, "sets": v} for k, v in sorted(unclassified.items(), key=lambda kv: -kv[1])],
            "targets": done}


def weight_markdown(w: dict) -> list[str]:
    """주간 리포트에 붙일 '웨이트 구분별 세트' 절 (줄 목록). 웨이트 세트가 없으면 빈 목록."""
    if not w["rawDays"]:
        return ["## 웨이트 구분별 세트", "", "_가민 원본(data/raw)이 이 PC에 없어 계산하지 못했습니다._", ""]
    if not w["sets"]:
        return []
    g = w["groups"]
    L = ["## 웨이트 구분별 세트", "", "| 구분 | 세트 | 반복 |", "|---|---|---|"]
    for name in GROUPS:
        L.append(f"| {name} | {g[name]['sets']} | {g[name]['reps']} |")
        if name == "푸시":
            L.append(f"| ↳ 그중 푸시업 | {w['pushUp']['sets']} | {w['pushUp']['reps']} |")
        if name == "풀":
            L.append(f"| ↳ 그중 풀업 | {w['pullUp']['sets']} | {w['pullUp']['reps']} |")
    un = sum(x["sets"] for x in w["unclassified"])
    if un:
        L.append(f"| 미분류 | {un} | |")
    L += [f"| **합계** | **{w['sets']}** | |", ""]
    push, pull = g["푸시"]["sets"], g["풀"]["sets"]
    L.append(f"- 푸시:풀 세트 비율 {push} : {pull}" + (f" (풀 1 기준 푸시 {push / pull:.2f})" if pull else ""))
    h = w["hinge"]
    if h["gym"] + h["home"]:
        L.append(f"- 힌지류(데드리프트 계열) 웨이트:홈 세트 비율 {h['gym']} : {h['home']} "
                 f"(홈 = 가민 이름에 케틀벨, 또는 무게 {w['homeMaxKg']:g}kg 이하)")
        if h["noweight"]:
            L.append(f"  - 무게 미입력 {h['noweight']}세트는 웨이트로 셈")
    if un:
        L.append("- 미분류(`names.toml [groups]`에 추가하면 구분됨): " + ", ".join(f"{x['name']} {x['sets']}세트" for x in w["unclassified"]))
    for t in w["targets"]:
        if t["goal"]:
            L.append(f"- 주간 목표 {t['label']}: {t['done']}/{t['goal']:g}세트 ({t['done'] / t['goal'] * 100:.0f}%)")
    L += ["- 세트·반복만 셈 (워밍업 포함, 직접 수정한 세트는 수정값 기준)", ""]
    return L


def _row(label, cur, prev, unit="", dg=1):
    def f(v):
        return "-" if v is None else f"{v:g}{unit}"
    d = "" if cur is None or prev is None else (lambda x: "동일" if not x else f"{'+' if x > 0 else ''}{x:g}{unit}")(round(cur - prev, dg))
    return f"| {label} | {f(cur)} | {f(prev)} | {d} |"


def build(db_path: Path, key: str, existing: str | None, today: date, *, data_root: Path | None = None,
          corr_root: Path | None = None, names_path: Path | None = None) -> str | None:
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
    if data_root is not None and names_path is not None and cur["sets"]:
        L += [""] + weight_markdown(weight_summary(data_root, corr_root, start, end, names_path))[:-1]
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
