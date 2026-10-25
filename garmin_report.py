"""data/raw/<날짜>/ 원본 JSON → reports/<날짜>.md 하루 리포트.

- 운동 블록은 activity ID 기준. 각 블록 끝에 메모 칸이 붙고,
  `<!-- notes:ID --> ... <!-- /notes -->` 안쪽 내용은 다시 생성해도 보존된다.
  (과거 날짜를 --date로 재생성해도, 같은 날 운동이 2개여도 ID별로 유지)
- 마커 바깥에 직접 쓴 글은 재생성 시 사라진다. 메모는 반드시 마커 안쪽에.
- 원본에 더 이상 없는 ID의 메모는 맨 아래 '연결된 운동 없음' 구역에 보존된다.
- 웨이트 세트는 Garmin 운동 세트(사용자가 앱에서 수정한 값)를 그대로 쓴다.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

WEEKDAYS = "월화수목금토일"
NOTE_RE = re.compile(r"<!-- notes:([\w-]+) -->\n?(.*?)<!-- /notes -->", re.DOTALL)

ACTIVITY_NOTE_TEMPLATE = """- 목적:
- 컨디션:
- RPE(1~10):
- 통증(부위·좌우):
- 계획 대비:
- 비고:
"""
DAILY_NOTE_TEMPLATE = """- 아침 컨디션/수면 체감:
- 식사·카페인·약/보충제:
- 하루 통증·피로:
- 비고:
"""

TYPE_KO = {
    "running": "러닝", "treadmill_running": "트레드밀 러닝", "trail_running": "트레일 러닝",
    "strength_training": "웨이트", "cycling": "사이클", "indoor_cycling": "실내 사이클",
    "hiit": "HIIT", "walking": "걷기", "tennis_v2": "테니스", "tennis": "테니스",
    "pilates": "필라테스", "yoga": "요가", "lap_swimming": "수영", "e_bike_fitness": "E-bike",
}
RUN_TYPES = {"running", "treadmill_running", "trail_running"}
BIKE_TYPES = {"cycling", "indoor_cycling", "e_bike_fitness"}


# ----------------------------------------------------------------- helpers
def load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def ok(value: Any) -> Any:
    """error 응답은 없는 것으로 취급."""
    if isinstance(value, dict) and "error" in value and len(value) == 1:
        return None
    return value


def dig(obj: Any, *keys: str) -> Any:
    for k in keys:
        if isinstance(obj, dict):
            obj = obj.get(k)
        else:
            return None
    return obj


def pick(sources: list[dict], *names: str) -> Any:
    for src in sources:
        for n in names:
            if isinstance(src, dict) and src.get(n) is not None:
                return src[n]
    return None


def fmt_hms(sec: float | None) -> str:
    if sec is None:
        return "-"
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_hm(sec: float | None) -> str:
    if not sec:
        return "-"
    h, m = divmod(int(round(sec / 60)), 60)
    return f"{h}시간 {m}분"


def fmt_pace(speed_ms: float | None) -> str:
    if not speed_ms or speed_ms <= 0:
        return "-"
    sec_per_km = 1000 / speed_ms
    return f"{int(sec_per_km // 60)}:{int(round(sec_per_km % 60)):02d}/km".replace(":60/", ":59/")


def fmt_num(v: Any, unit: str = "", digits: int = 0) -> str:
    if v is None:
        return "-"
    return f"{v:.{digits}f}{unit}" if digits else f"{int(round(v))}{unit}"


def clock_from_ms(ms: Any) -> str:
    """Garmin '...Local' 타임스탬프는 현지시각을 epoch처럼 담고 있다 → UTC로 해석하면 현지 시계값."""
    if not isinstance(ms, (int, float)):
        return "-"
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%H:%M")


BODYWEIGHT: set[str] = {"PULL_UP", "CHIN_UP", "PUSH_UP", "PLANK", "SIT_UP", "CRUNCH"}
PARTS: dict[str, str] = {}  # 운동 코드 -> 부위 태그 (names.toml [parts])


def load_names(path: Path) -> tuple[dict[str, str], str]:
    if not path.exists():
        return {}, "auto"
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    BODYWEIGHT.clear()
    BODYWEIGHT.update(str(c).upper() for c in data.get("settings", {}).get(
        "bodyweight", ["PULL_UP", "CHIN_UP", "PUSH_UP", "PLANK", "SIT_UP", "CRUNCH"]))
    PARTS.clear()
    PARTS.update({str(k).upper(): str(v) for k, v in data.get("parts", {}).items()})
    return data.get("exercises", {}), str(data.get("settings", {}).get("weight_unit", "auto")).lower()


def prettify(code: str) -> str:
    return code.replace("_", " ").title()


# ------------------------------------------------------------ daily summary
def find_naps(obj: Any, out: list | None = None) -> list[dict]:
    """Body Battery 데이터 어디에 있든 eventType == NAP 인 이벤트를 모은다 (같은 시작시각은 한 번만)."""
    if out is None:
        out = []
    if isinstance(obj, dict):
        if obj.get("eventType") == "NAP":
            if all(o.get("eventStartTimeGmt") != obj.get("eventStartTimeGmt") for o in out):
                out.append(obj)
        for v in obj.values():
            find_naps(v, out)
    elif isinstance(obj, list):
        for v in obj:
            find_naps(v, out)
    return out


def nap_text(nap: dict) -> str:
    try:
        start = datetime.fromisoformat(str(nap["eventStartTimeGmt"]).replace("Z", ""))
        start += timedelta(milliseconds=nap.get("timezoneOffset") or 0)
        clock = start.strftime("%H:%M")
    except (KeyError, ValueError):
        clock = "-"
    minutes = round((nap.get("durationInMilliseconds") or 0) / 60000)
    h, m = divmod(minutes, 60)
    dur = f"{h}시간 {m}분" if h else f"{m}분"
    impact = nap.get("bodyBatteryImpact")
    return f"{clock} 시작, {dur}" + ((" (Body Battery 변화 없음)" if impact == 0 else f" (Body Battery 변화 {impact:+d})") if isinstance(impact, int) else "")


def daily_section(day: str, raw: Path) -> list[str]:
    d = load_json(raw / "daily.json") or {}
    stats = ok(d.get("stats")) or {}
    sleep = ok(d.get("sleep")) or {}
    sdto = sleep.get("dailySleepDTO") or {}
    hrv = (ok(d.get("hrv")) or {}).get("hrvSummary") or {}
    stress = ok(d.get("stress")) or {}
    ready = ok(d.get("training_readiness"))
    ready0 = ready[0] if isinstance(ready, list) and ready else (ready if isinstance(ready, dict) else {})

    rows: list[tuple[str, str]] = []
    if sdto:
        score = dig(sdto, "sleepScores", "overall", "value")
        rows.append(("수면", f"{clock_from_ms(sdto.get('sleepStartTimestampLocal'))} 취침 → "
                             f"{clock_from_ms(sdto.get('sleepEndTimestampLocal'))} 기상, "
                             f"총 {fmt_hm(sdto.get('sleepTimeSeconds'))}"
                             + (f", 점수 {score}" if score is not None else "")))
        rows.append(("수면 단계", " / ".join(
            f"{label} {fmt_num((sdto.get(k) or 0) / 60, '분')}"
            for label, k in (("깊은", "deepSleepSeconds"), ("얕은", "lightSleepSeconds"),
                             ("REM", "remSleepSeconds"), ("깸", "awakeSleepSeconds")))))
    if hrv:
        rows.append(("HRV", f"지난밤 평균 {fmt_num(hrv.get('lastNightAvg'), 'ms')}, "
                            f"7일 평균 {fmt_num(hrv.get('weeklyAvg'), 'ms')}, 상태 {hrv.get('status', '-')}"))
    naps = find_naps(d.get("body_battery"))
    if naps:
        rows.append(("낮잠", " / ".join(nap_text(n) for n in sorted(naps, key=lambda n: str(n.get("eventStartTimeGmt"))))))
    wake = stats.get("bodyBatteryAtWakeTime")
    rows.append(("Body Battery", f"기상 {fmt_num(wake)} / 최저 {fmt_num(stats.get('bodyBatteryLowestValue'))} / "
                                 f"최고 {fmt_num(stats.get('bodyBatteryHighestValue'))}"))
    rows.append(("안정시 심박", f"{fmt_num(stats.get('restingHeartRate'), 'bpm')} "
                               f"(7일 평균 {fmt_num(stats.get('lastSevenDaysAvgRestingHeartRate'), 'bpm')})"))
    rows.append(("스트레스", f"평균 {fmt_num(stress.get('avgStressLevel'))} / 최고 {fmt_num(stress.get('maxStressLevel'))}"))
    if ready0:
        rows.append(("훈련 준비도", f"{ready0.get('score', '-')} ({ready0.get('level', '-')})"))
    rows.append(("걸음/활동 칼로리", f"{fmt_num(stats.get('totalSteps'))}걸음 / {fmt_num(stats.get('activeKilocalories'), 'kcal')}"))

    out = ["## 하루 요약", "", "| 항목 | 값 |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in rows]
    return out + [""]


# --------------------------------------------------------------- activities
def hr_zone_line(zones: Any) -> str | None:
    zones = ok(zones)
    if not isinstance(zones, list) or not zones:
        return None
    zs = sorted(zones, key=lambda z: z.get("zoneNumber", 0))
    return " / ".join(f"Z{z.get('zoneNumber')} {fmt_hms(z.get('secsInZone'))}" for z in zs)


def lap_table(splits: Any, is_run: bool) -> list[str]:
    splits = ok(splits)
    laps = (splits or {}).get("lapDTOs") if isinstance(splits, dict) else None
    # 마지막에 찍히는 0.00km 같은 기록 아티팩트(50m 미만 & 15초 미만)는 숨긴다.
    laps = [l for l in (laps or []) if not ((l.get("distance") or 0) < 50 and (l.get("duration") or 0) < 15)]
    if not laps or len(laps) < 2:
        return []
    out = ["", "**랩**", "", "| # | 거리 | 시간 | 페이스 | 평균HR | 최대HR | 케이던스 |", "|---|---|---|---|---|---|---|"]
    for i, lap in enumerate(laps, 1):
        dist = lap.get("distance")
        cad = lap.get("averageRunCadence")
        out.append(f"| {i} | {fmt_num((dist or 0) / 1000, 'km', 2) if dist else '-'} | {fmt_hms(lap.get('duration'))} | "
                   f"{fmt_pace(lap.get('averageSpeed')) if is_run else '-'} | {fmt_num(lap.get('averageHR'))} | "
                   f"{fmt_num(lap.get('maxHR'))} | {fmt_num(cad)} |")
    return out


def weight_kg(raw_w: Any, unit: str, grams_likely: bool) -> float | None:
    if not isinstance(raw_w, (int, float)) or raw_w <= 0:
        return None
    if unit == "g" or (unit == "auto" and grams_likely):
        return raw_w / 1000
    return float(raw_w)


def strength_section(sets_json: Any, names: dict[str, str], unit_cfg: str) -> list[str]:
    sets_json = ok(sets_json)
    sets = (sets_json or {}).get("exerciseSets") if isinstance(sets_json, dict) else None
    if not sets:
        return ["", "_세트 기록 없음 (Garmin 운동 세트가 비어 있음)_"]

    weights = [s.get("weight") for s in sets if isinstance(s.get("weight"), (int, float)) and s["weight"] > 0]
    grams_likely = bool(weights) and max(weights) >= 1000  # 1000 이상이면 g으로 추정
    unit_note = {"g": "g 단위로 설정됨", "kg": "kg 단위로 설정됨"}.get(
        unit_cfg, "무게 단위 자동 판정: " + ("g→kg 변환" if grams_likely else "kg 그대로") + " (맞지 않으면 names.toml의 weight_unit 지정)")

    groups: list[dict] = []
    rest_total = 0.0
    for s in sets:
        if s.get("setType") == "REST":
            rest_total += s.get("duration") or 0
            continue
        ex = (s.get("exercises") or [{}])[0] or {}
        code = ex.get("name") or ex.get("category") or "UNKNOWN"
        label = names.get(ex.get("name") or "") or names.get(ex.get("category") or "") or prettify(code)
        reps = s.get("repetitionCount") or 0
        w = weight_kg(s.get("weight"), unit_cfg, grams_likely)
        codes = [(ex.get("name") or "").upper(), (ex.get("category") or "").upper()]
        part = next((PARTS[c] for c in codes if c in PARTS), "미분류")
        if groups and groups[-1]["label"] == label:
            g = groups[-1]
        else:
            g = {"label": label, "part": part, "sets": []}
            groups.append(g)
        is_bw = any(c in BODYWEIGHT for c in codes)
        g["sets"].append((reps, w, is_bw, s.get("duration") or 0))

    out = ["", "**웨이트 세트**", "", "| 운동 | 세트 | 반복×중량 | 총 반복 | 볼륨 |", "|---|---|---|---|---|"]
    total_sets = total_reps = 0
    total_vol = 0.0
    unlogged = 0
    part_tot: dict[str, list] = {}
    for g in groups:
        parts, reps_sum, vol = [], 0, 0.0
        for reps, w, is_bw, dur in g["sets"]:
            if reps == 0:
                parts.append(f"{int(round(dur))}초" if dur else "0회")  # 플랭크처럼 시간 운동이거나 반복 미기록
            elif w:
                parts.append(f"{reps}×{w:g}kg")
            elif is_bw:
                parts.append(f"{reps}회")
            else:
                parts.append(f"{reps}×무게?")
                unlogged += 1
            reps_sum += reps
            vol += reps * w if w else 0
        pt = part_tot.setdefault(g["part"], [0, 0, 0.0])
        pt[0] += len(g["sets"]); pt[1] += reps_sum; pt[2] += vol
        total_sets += len(g["sets"])
        total_reps += reps_sum
        total_vol += vol
        out.append(f"| {g['label']} | {len(g['sets'])} | {', '.join(parts)} | {reps_sum} | "
                   f"{f'{vol:,.0f}kg' if vol else '-'} |")
    out.append(f"| **합계** | **{total_sets}** | | **{total_reps}** | **{total_vol:,.0f}kg** |")
    order = sorted(part_tot.items(), key=lambda kv: -kv[1][0])
    out += ["", "부위별: " + " / ".join(
        f"{k} {v[0]}세트·{v[1]}회" + (f"·{v[2]:,.0f}kg" if v[2] else "") for k, v in order)]
    if unlogged:
        out += ["", f"⚠️ 무게 미입력 세트 {unlogged}개(`무게?`) — 볼륨이 실제보다 적게 계산됨. 분석 시 신뢰도 낮음."]
    out += ["", f"휴식 합계 {fmt_hms(rest_total)} · 볼륨은 무게가 있는 세트만 합산 (맨몸 운동은 반복 수만 표시) · {unit_note}"]
    return out


def activity_block(idx: int, entry: dict, adir: Path, names: dict[str, str], unit_cfg: str,
                   saved_notes: dict[str, str]) -> list[str]:
    act_id = str(entry.get("activityId"))
    summary = ok(load_json(adir / "summary.json")) or {}
    sdto = summary.get("summaryDTO") or {}
    srcs = [entry, sdto]

    type_key = dig(entry, "activityType", "typeKey") or dig(summary, "activityTypeDTO", "typeKey") or "unknown"
    type_ko = TYPE_KO.get(type_key, prettify(type_key))
    start = str(pick(srcs, "startTimeLocal") or "")
    start_clock = start[11:16] if len(start) >= 16 else "-"
    name = entry.get("activityName") or summary.get("activityName") or type_ko

    duration = pick(srcs, "duration")
    dist = pick(srcs, "distance")
    avg_speed = pick(srcs, "averageSpeed")
    rows = [("시작/시간", f"{start_clock} / {fmt_hms(duration)}")]
    if dist and dist > 0:
        rows.append(("거리", f"{dist / 1000:.2f}km"))
        if type_key in RUN_TYPES:
            rows.append(("평균 페이스", fmt_pace(avg_speed)))
        elif type_key in BIKE_TYPES and avg_speed:
            rows.append(("평균 속도", f"{avg_speed * 3.6:.1f}km/h"))
    rows.append(("심박 평균/최대", f"{fmt_num(pick(srcs, 'averageHR'))} / {fmt_num(pick(srcs, 'maxHR'))} bpm"))
    cad = pick(srcs, "averageRunningCadenceInStepsPerMinute", "averageRunCadence")
    if type_key in RUN_TYPES and cad:
        rows.append(("케이던스", f"{fmt_num(cad)}spm"))
    rows.append(("칼로리", fmt_num(pick(srcs, "calories"), "kcal")))
    te_a, te_n = pick(srcs, "aerobicTrainingEffect"), pick(srcs, "anaerobicTrainingEffect")
    if te_a is not None or te_n is not None:
        rows.append(("훈련효과 유산소/무산소", f"{fmt_num(te_a, '', 1)} / {fmt_num(te_n, '', 1)}"))
    load = pick(srcs, "activityTrainingLoad")
    if load is not None:
        rows.append(("훈련 부하", fmt_num(load)))
    zline = hr_zone_line(load_json(adir / "hr_zones.json"))
    if zline:
        rows.append(("심박 존 시간", zline))

    out = [f"### {idx}. {type_ko} — {name}  (ID {act_id})", "", "| 항목 | 값 |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in rows]
    if type_key in RUN_TYPES | BIKE_TYPES or "run" in type_key:
        out += lap_table(load_json(adir / "splits.json"), type_key in RUN_TYPES)
    if type_key == "strength_training" or ok(load_json(adir / "exercise_sets.json")) and \
            (ok(load_json(adir / "exercise_sets.json")) or {}).get("exerciseSets"):
        out += strength_section(load_json(adir / "exercise_sets.json"), names, unit_cfg)

    body = saved_notes.get(act_id, ACTIVITY_NOTE_TEMPLATE)
    out += ["", "**메모** (마커 안쪽에 쓰세요)", f"<!-- notes:{act_id} -->\n{body}<!-- /notes -->", ""]
    return out


# ------------------------------------------------------------------- report
def read_saved_notes(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    return {m.group(1): m.group(2) if m.group(2).endswith("\n") or not m.group(2) else m.group(2) + "\n"
            for m in NOTE_RE.finditer(text)}


def build_report(day: str, data_root: Path, report_path: Path, names: dict[str, str], unit_cfg: str) -> str | None:
    raw = data_root / "raw" / day
    if not raw.exists():
        return None
    saved = read_saved_notes(report_path)
    wd = WEEKDAYS[date.fromisoformat(day).weekday()]
    fetched = (load_json(raw / "daily.json") or {}).get("fetched_at", "-")

    lines = [f"# {day} ({wd}) 훈련 리포트", ""]
    if day == date.today().isoformat():
        lines += ["> ⚠️ **오늘 스냅샷** — 하루가 끝나기 전 데이터입니다 (수면·Body Battery·스트레스 합계는 아직 변할 수 있음).", ""]
    lines += [f"> 수집 시각: {fetched}", ""]
    lines += daily_section(day, raw)

    acts = (load_json(raw / "activities.json") or {}).get("activities") or []
    acts = sorted(acts, key=lambda a: str(a.get("startTimeLocal", "")))
    lines += [f"## 운동 {len(acts)}건", ""]
    if not acts:
        lines += ["_기록된 운동 없음_", ""]
    used: set[str] = set()
    for i, entry in enumerate(acts, 1):
        act_id = str(entry.get("activityId"))
        used.add(act_id)
        lines += activity_block(i, entry, raw / f"activity_{act_id}", names, unit_cfg, saved)

    daily_body = saved.get("daily", DAILY_NOTE_TEMPLATE)
    lines += ["## 하루 메모", f"<!-- notes:daily -->\n{daily_body}<!-- /notes -->", ""]

    orphans = {k: v for k, v in saved.items() if k not in used and k != "daily"}
    if orphans:
        lines += ["## 연결된 운동 없음 (보존된 메모)", "",
                  "원본에서 해당 운동을 찾지 못해 메모만 남겼습니다. 삭제된 운동이면 지워도 됩니다.", ""]
        for k, v in orphans.items():
            lines += [f"<!-- notes:{k} -->\n{v}<!-- /notes -->", ""]
    return "\n".join(lines).rstrip() + "\n"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="하루 리포트(Markdown) 생성")
    p.add_argument("--date", default=date.today().isoformat(), help="기준 날짜 (기본: 오늘)")
    p.add_argument("--days", type=int, default=1, help="--date 포함 과거 N일 (백필용)")
    p.add_argument("--data", type=Path, default=Path("data"))
    p.add_argument("--reports", type=Path, default=Path("reports"))
    p.add_argument("--names", type=Path, default=Path("names.toml"), help="운동명/무게 단위 설정")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.days < 1:
        print("--days는 1 이상이어야 합니다", file=sys.stderr)
        return 2
    last = date.fromisoformat(args.date)
    names, unit_cfg = load_names(args.names)
    code = 0
    for i in range(args.days - 1, -1, -1):
        day = (last - timedelta(days=i)).isoformat()
        path = args.reports / f"{day}.md"
        text = build_report(day, args.data, path, names, unit_cfg)
        if text is None:
            print(f"{day}: 원본 없음 — 먼저 garmin_export.py --date {day} 를 실행하세요", file=sys.stderr)
            code = 1
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"{day}: {path}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
