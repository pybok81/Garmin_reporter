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


def month_week(key: str) -> str:
    """'11월 2주'. 주의 목요일이 속한 달 기준(달에 걸친 주는 목요일 쪽 달)."""
    th = week_range(key)[0] + timedelta(days=3)
    return f"{th.month}월 {(th.day - 1) // 7 + 1}주"


def week_title(key: str) -> str:
    """'2026-11-09 ~ 11-15 (11월 2주)'. 화면·리포트 제목용 (파일 이름은 2026-W46 그대로)."""
    a, b = week_range(key)
    return f"{a.isoformat()} ~ {b:%m-%d} ({month_week(key)})"


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
PARTS = ("가슴", "등", "어깨", "팔", "하체", "코어", "전신", "미분류")


def goal_text(t: dict) -> str:
    """목표 범위 문장: 8~11세트 / 18세트 + 달성 상태."""
    lo, hi, n = t["lo"], t["hi"], t["done"]
    goal = f"{lo:g}세트" if lo == hi else f"{lo:g}~{hi:g}세트"
    state = "부족" if n < lo else ("초과" if n > hi else "범위 안")
    return f"{n}/{goal} ({state})"


def weight_summary(data_root: Path, corr_root: Path, start: date, end: date, names_path: Path,
                   reports_root: Path | None = None) -> dict:
    """주(또는 기간)의 웨이트 세트를 푸시/풀/하체/코어로 센다. 가민 원본(data/raw)과 직접 수정(corrections)에서 계산한다.

    일일 리포트와 같은 분류 함수(garmin_report.classify_sets)를 쓰므로 수정한 세트는 수정값으로 센다.
    세트·반복 수만 센다(무게 볼륨은 쓰지 않음). 워밍업도 함께 센다.
    """
    cfg = tomllib.loads(names_path.read_text(encoding="utf-8")) if names_path.is_file() else {}
    names, unit_cfg = gr.load_names(names_path)          # PARTS·BODYWEIGHT 도 같이 채운다
    groups = {str(k).upper(): str(v) for k, v in (cfg.get("groups") or {}).items()}
    targets = {}
    for k, v in (cfg.get("targets") or {}).items():          # 숫자 하나 또는 [최소, 최대] 범위
        if isinstance(v, (int, float)):
            targets[str(k)] = (float(v), float(v))
        elif isinstance(v, list) and len(v) == 2 and all(isinstance(x, (int, float)) for x in v):
            targets[str(k)] = (float(v[0]), float(v[1]))
    subparts = {str(k).upper(): str(v) for k, v in (cfg.get("subparts") or {}).items()}
    home_max = float((cfg.get("settings") or {}).get("home_hinge_max_kg", 20))

    tot = {g: [0, 0] for g in GROUPS}
    sub = {"PULL_UP": [0, 0], "PUSH_UP": [0, 0]}
    hinge = {"gym": 0, "home": 0, "unknown": 0}
    holds = {"sets": 0, "sec": 0.0, "parts": {}}            # 버티기(R26): 부위 → 초
    unclassified: dict[str, int] = {}
    by_code: dict[str, int] = {}
    parts: dict[str, list[int]] = {}                       # 부위 → [세트, 반복]
    subs: dict[str, list[int]] = {}                        # "어깨/레터럴" → [세트, 반복]
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
        rroot = Path(reports_root) if reports_root else (Path(corr_root) / "reports" if corr_root else None)
        memos = gr.read_saved_notes(rroot / f"{day}.md") if rroot else {}     # 메모 `보정:` 줄 (R22)
        for adir in sorted(raw.glob("activity_*")):
            aid = adir.name[len("activity_"):]
            c = gr.classify_sets(gr.load_json(adir / "exercise_sets.json"), names, unit_cfg,
                                 corr.for_activity(fixes, aid), memos.get(aid))
            for r in (c["rows"] if c else []):
                n_sets += 1
                codes = [r["name"], r["category"]]
                if r.get("hold"):                          # 시간만 있는 세트는 따로 (구분·부위·목표에서 뺌)
                    holds["sets"] += 1
                    holds["sec"] += r["dur"]
                    holds["parts"][r["part"] or "미분류"] = holds["parts"].get(r["part"] or "미분류", 0) + r["dur"]
                    continue
                pt = r["part"] or "미분류"
                parts.setdefault(pt, [0, 0])
                parts[pt][0] += 1
                parts[pt][1] += r["reps"]
                sp = next((subparts[x] for x in codes if x in subparts), None)
                if sp:
                    subs.setdefault(sp, [0, 0])
                    subs[sp][0] += 1
                    subs[sp][1] += r["reps"]
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
                    if any("KETTLEBELL" in x for x in codes) or (kg and kg <= home_max) or (not kg and r["bw"]):
                        hinge["home"] += 1
                    elif not kg:
                        hinge["unknown"] += 1                # 무게 미입력은 불명 (R27)
                    else:
                        hinge["gym"] += 1
    done = []
    for key, (lo, hi) in targets.items():
        if key.upper() in by_code:
            label, n = names.get(key.upper()) or gr.prettify(key.upper()), by_code[key.upper()]
        elif key in subs or key in parts:
            label, n = key, (subs.get(key) or parts.get(key))[0]
        else:
            label, n = (names.get(key.upper()) or key), 0
        done.append({"label": label, "done": n, "lo": lo, "hi": hi})
    order = [p for p in PARTS if p in parts] + sorted(p for p in parts if p not in PARTS)
    body = [{"part": p, "sets": parts[p][0], "reps": parts[p][1],
             "subs": [{"name": k.split("/", 1)[1], "sets": v[0], "reps": v[1]}
                      for k, v in subs.items() if k.split("/", 1)[0] == p]} for p in order]
    return {"rawDays": n_days, "sets": n_sets, "groups": {g: {"sets": v[0], "reps": v[1]} for g, v in tot.items()},
            "pullUp": {"sets": sub["PULL_UP"][0], "reps": sub["PULL_UP"][1]},
            "pushUp": {"sets": sub["PUSH_UP"][0], "reps": sub["PUSH_UP"][1]},
            "hinge": hinge, "homeMaxKg": home_max,
            "unclassified": [{"name": k, "sets": v} for k, v in sorted(unclassified.items(), key=lambda kv: -kv[1])],
            "targets": done, "body": body,
            "holds": {"sets": holds["sets"], "sec": round(holds["sec"]),
                      "parts": [{"part": k, "sec": round(v)} for k, v in sorted(holds["parts"].items(), key=lambda kv: -kv[1])]}}


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
    ho = w.get("holds") or {"sets": 0}
    if ho["sets"]:
        L.append(f"| 버티기 (시간 세트) | {ho['sets']} | {ho['sec']}초 |")
    L += [f"| **합계** | **{w['sets']}** | |", ""]
    push, pull = g["푸시"]["sets"], g["풀"]["sets"]
    L.append(f"- 푸시:풀 세트 비율 {push} : {pull}" + (f" (풀 1 기준 푸시 {push / pull:.2f})" if pull else ""))
    h = w["hinge"]
    if h["gym"] + h["home"] + h["unknown"]:
        L.append(f"- 힌지류(데드리프트 계열) 웨이트:홈:불명 세트 비율 {h['gym']} : {h['home']} : {h['unknown']} "
                 f"(홈 = 가민 이름에 케틀벨, 또는 무게 {w['homeMaxKg']:g}kg 이하 · 불명 = 무게 미입력)")
    if un:
        L.append("- 미분류(`names.toml [groups]`에 추가하면 구분됨): " + ", ".join(f"{x['name']} {x['sets']}세트" for x in w["unclassified"]))
    if ho["sets"]:
        L.append("- 버티기 = 반복 없이 시간만 있는 세트(플랭크 포함). 구분·부위 합계와 푸시:풀 비율에서 뺌")
    L += ["- 세트·반복만 셈 (워밍업 포함, 직접 수정한 세트는 수정값 기준)", ""]
    if w.get("body"):
        L += ["## 웨이트 부위별 세트", "", "| 부위 | 세트 | 반복 |", "|---|---|---|"]
        for b in w["body"]:
            L.append(f"| {b['part']} | {b['sets']} | {b['reps']} |")
            for x in b["subs"]:
                L.append(f"| ↳ {x['name']} | {x['sets']} | {x['reps']} |")
        if ho["sets"]:
            L.append(f"| 버티기 (시간 세트) | {ho['sets']} | {ho['sec']}초 |")
            for x in ho["parts"]:
                L.append(f"| ↳ {x['part']} | | {x['sec']}초 |")
        L += [f"| **합계** | **{w['sets']}** | |", ""]
        for t in w["targets"]:
            L.append(f"- 주간 목표 {t['label']}: {goal_text(t)}")
        if w["targets"]:
            L.append("")
    return L


RUN_KEYS = ("running", "treadmill_running", "track_running", "trail_running")
BIKE_KEYS = ("cycling", "indoor_cycling", "e_bike_fitness", "virtual_ride")


def felt_summary(db, start: str, end: str, reports_root: Path | None) -> dict:
    """체감 부하(RPE × 분, R23): DB 활동의 시간·종류 + 그날 리포트 메모의 RPE."""
    acts = [dict(r) for r in db.execute(
        "SELECT activity_id, day, type_key, duration_s FROM activities WHERE day BETWEEN ? AND ? ORDER BY start_local",
        (start, end))]
    memo_cache: dict[str, dict] = {}
    by_type: dict[str, int] = {}
    by_day: dict[str, int] = {}
    total, miss = 0, 0
    for a in acts:
        if a["day"] not in memo_cache:
            memo_cache[a["day"]] = gr.read_saved_notes(Path(reports_root) / f"{a['day']}.md") if reports_root else {}
        v = gr.felt_load(gr.parse_rpe(memo_cache[a["day"]].get(str(a["activity_id"]))), a["duration_s"])
        if v is None:
            miss += 1
            continue
        t = hv.group_of(a["type_key"])
        by_type[t] = by_type.get(t, 0) + v
        by_day[a["day"]] = by_day.get(a["day"], 0) + v
        total += v
    return {"total": total if len(acts) > miss else None, "missing": miss, "count": len(acts),
            "types": [(t, by_type[t]) for t in hv.ORDER if t in by_type], "days": by_day}


def run_stats(db, start: date, end: date, bike_min_per_km: float) -> dict:
    """러닝 보조 지표(R24)."""
    def km_min(a: date, b: date) -> tuple[float, float, dict]:
        run_m, bike_s, per_day = 0.0, 0.0, {}
        for r in db.execute("SELECT day, type_key, distance_m, duration_s FROM activities WHERE day BETWEEN ? AND ?",
                            (a.isoformat(), b.isoformat())):
            if r["type_key"] in RUN_KEYS:
                run_m += r["distance_m"] or 0
                per_day[r["day"]] = per_day.get(r["day"], 0) + 1
            elif r["type_key"] in BIKE_KEYS:
                bike_s += r["duration_s"] or 0
        return run_m / 1000, bike_s / 60, per_day
    km, bmin, per_day = km_min(start, end)
    prev = [km_min(start - timedelta(days=7 * k), end - timedelta(days=7 * k))[0] for k in (1, 2, 3)]
    had = [x for x in prev if x > 0]
    return {"km": round(km, 1), "bikeMin": round(bmin), "bikeKm": round(bmin / bike_min_per_km, 1) if bike_min_per_km else 0.0,
            "prevKm": round(prev[0], 1), "avg3": round(sum(had) / len(had), 1) if had else None,
            "doubles": sum(1 for n in per_day.values() if n >= 2), "ratio": bike_min_per_km}


def recovery_stats(days: list[dict], start: date, today: date) -> dict:
    """회복 보조 지표(R25): 수면 5.5h 미만 밤, 완전 휴식일(걷기 말고 운동 없음), HRV 평균·중앙값·최저."""
    by = {d["date"]: d for d in days}
    short = sum(1 for d in days if d["sleepH"] and d["sleepH"] < 5.5)
    nights = sum(1 for d in days if d["sleepH"])
    rest, counted = 0, 0
    for i in range(7):
        k = (start + timedelta(days=i))
        if k > today:
            break
        counted += 1
        acts = (by.get(k.isoformat()) or {}).get("acts") or []
        if not [a for a in acts if a["type"] != "걷기"]:
            rest += 1
    hv_ = [(d["hrv"], d["date"]) for d in days if d["hrv"]]
    vals = sorted(v for v, _ in hv_)
    med = None
    if vals:
        n = len(vals)
        med = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
    low = min(hv_) if hv_ else None
    return {"short": short, "nights": nights, "rest": rest, "counted": counted,
            "hrvAvg": round(sum(vals) / len(vals), 1) if vals else None, "hrvMed": round(med, 1) if med is not None else None,
            "hrvMin": (low[0], low[1]) if low else None}


def _row(label, cur, prev, unit="", dg=1):
    def f(v):
        return "-" if v is None else f"{v:g}{unit}"
    d = "" if cur is None or prev is None else (lambda x: "동일" if not x else f"{'+' if x > 0 else ''}{x:g}{unit}")(round(cur - prev, dg))
    return f"| {label} | {f(cur)} | {f(prev)} | {d} |"


def build(db_path: Path, key: str, existing: str | None, today: date, *, data_root: Path | None = None,
          corr_root: Path | None = None, names_path: Path | None = None, profile_line: str = "",
          reports_root: Path | None = None) -> str | None:
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
        rroot = Path(reports_root) if reports_root else (Path(corr_root) / "reports" if corr_root else None)
        felt = felt_summary(db, a, b, rroot)
        felt_prev = felt_summary(db, ps.isoformat(), pe.isoformat(), rroot)
        cfg = tomllib.loads(names_path.read_text(encoding="utf-8")) if names_path and Path(names_path).is_file() else {}
        runs = run_stats(db, start, end, float((cfg.get("settings") or {}).get("bike_min_per_km", 15)))
    finally:
        db.close()
    saved = {m.group(1): m.group(2) for m in NOTE_RE.finditer(existing or "")}
    body = saved.get("week", NOTE_TEMPLATE)
    L = [f"# {week_title(key)} 주간 리포트", ""]
    if end >= today:
        L += [f"> ⚠️ 진행 중인 주입니다. {today.isoformat()} 기준으로 만든 내용이고, 주가 끝난 뒤 다시 만들면 갱신됩니다.", ""]
    L += [f"기간: {a} ~ {b} · 비교: {ps.isoformat()} ~ {pe.isoformat()} (전주) · 심박은 손목 센서 기준", ""]
    if profile_line:
        L += [f"프로필: {profile_line}", ""]
    L += [
          "## 주간 요약", "", "| 항목 | 이번 주 | 전주 | 차이 |", "|---|---|---|---|",
          f"| 운동 | {cur['sessions']}회 / {cur['activeDays']}일 | {prev['sessions']}회 / {prev['activeDays']}일 | "
          f"{cur['sessions'] - prev['sessions']:+d}회 |",
          _row("총 시간", cur["hours"], prev["hours"], "h", 2),
          _row("러닝 거리", cur["runKm"], prev["runKm"], "km"),
          _row("웨이트 세트", cur["sets"], prev["sets"], "", 0),
          _row("체감 부하 합 (RPE×분)", felt["total"], felt_prev["total"], "", 0),
          _row("훈련 부하 합 (참고·가민)", cur["load"], prev["load"], "", 0),
          _row("수면 평균", cur["sleepH"], prev["sleepH"], "h"),
          _row("HRV 평균", cur["hrv"], prev["hrv"]),
          _row("안정시 심박 평균", cur["restHr"], prev["restHr"], "bpm")]
    if cur["acwr"]:
        ac = cur["acwr"]
        L.append(f"| 급성/만성 부하 (참고·가민) | {hv._fmt(ac['acute'])} / {hv._fmt(ac['chronic'])} (ACWR {hv._fmt(ac['ratio'])}, {ac['status'] or '-'}) | | {ac['day']} 기준 |")
    if cur["types"]:
        L += ["", "## 종류별", "", "| 종류 | 횟수 | 시간 | 거리 | 평균 심박 | 세트 | 부하 |", "|---|---|---|---|---|---|---|"]
        for t in cur["types"]:
            L.append(f"| {t['name']} | {t['count']} | {t['hours']:g}h | {hv._fmt(t['km'], 'km')} | {hv._fmt(t['avgHr'])} | "
                     f"{hv._fmt(t['sets'])} | {t['load']} |")
    if felt["count"]:
        L += ["", "## 체감 부하 (RPE × 분)", ""]
        if felt["total"] is None:
            L.append(f"_이번 주 운동 {felt['count']}건 모두 RPE 미입력. 일일 리포트 메모의 RPE 칸을 채우면 계산됩니다._")
        else:
            L.append("- 종류별: " + " · ".join(f"{t} {v}" for t, v in felt["types"]))
            L.append("- 날짜별: " + " · ".join(f"{date.fromisoformat(k):%m-%d}({hv.WEEKDAYS[date.fromisoformat(k).weekday()]}) {v}"
                                               for k, v in sorted(felt["days"].items())))
            if felt["missing"]:
                L.append(f"- RPE 미입력 {felt['missing']}건 (합계에서 뺌)")
        L.append("- 판단 기준은 체감 부하. 가민 훈련 부하는 심박 기반이라 웨이트·느린 러닝이 낮게 잡혀 참고로만 본다")
    r = runs
    L += ["", "## 러닝 보조 지표", "",
          f"- 실제 러닝 {r['km']:g}km · 사이클 환산 {r['bikeKm']:g}km (사이클 {r['bikeMin']}분, {r['ratio']:g}분=1km) · 합계(환산 포함) {r['km'] + r['bikeKm']:.1f}km"]
    if r["avg3"]:
        L.append(f"- 직전 3주 평균 {r['avg3']:g}km 대비 {r['km'] / r['avg3'] * 100:.0f}% (실제 러닝 기준)")
    if r["prevKm"]:
        inc = (r["km"] - r["prevKm"]) / r["prevKm"] * 100
        L.append(f"- 전주 대비 {inc:+.0f}% (전주 {r['prevKm']:g}km)" + (" ⚠️ +30% 초과" if inc > 30 else ""))
    L.append(f"- 하루 2회 러닝 {r['doubles']}일")
    rc = recovery_stats(days, start, today)
    L += ["", "## 회복 보조 지표", "",
          f"- 수면 5.5시간 미만 {rc['short']}밤 (수면 기록 {rc['nights']}밤 중)",
          f"- 완전 휴식일 {rc['rest']}일 (걷기 말고 운동이 없는 날, {rc['counted']}일 중)"]
    if rc["hrvAvg"] is not None:
        mn = rc["hrvMin"]
        L.append(f"- HRV 평균 {rc['hrvAvg']:g} · 중앙값 {rc['hrvMed']:g} · 최저 {mn[0]:g} ({mn[1][5:]})")
    if data_root is not None and names_path is not None and cur["sets"]:
        L += [""] + weight_markdown(weight_summary(data_root, corr_root, start, end, names_path, reports_root))[:-1]
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
