"""이력 DB(data/history.db) 관리: 앱이 받은 하루 원본(data/raw/날짜/)을 DB에 반영한다.

DB는 재생성 가능한 로컬 색인이다 (git/구글 드라이브에 올리지 않는다).
- 처음 만들기/전체 재생성: python import_export.py <가민 내보내기 폴더>   (기존 DB를 지우고 새로 만든다)
- 앱에서 받은 날 반영:      python history_db.py [--db data/history.db] [--raw data/raw]   (여러 번 실행해도 안전)
표 구조는 import_export.py 의 SCHEMA 를 그대로 쓴다. 단위: 시간=초, 거리=미터, 속도=m/s, 시각=한국 시간(+09:00).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
from pathlib import Path

import corrections as corr
from import_export import SCHEMA

KST = dt.timezone(dt.timedelta(hours=9))


def _load(path: Path):
    try:
        v = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return None if isinstance(v, dict) and set(v) == {"error"} else v   # 수집 실패 표시({"error":..})는 없는 것으로


def _num(*vals):
    for v in vals:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v
    return None


def _local_iso(s) -> str | None:
    """'2026-11-04 07:43:25' (기기 현지 시각) -> '2026-11-04T07:43:25+09:00'"""
    return f"{str(s)[:10]}T{str(s)[11:19]}+09:00" if s and len(str(s)) >= 19 else None


def _ms_local_iso(ms) -> str | None:
    """가민의 '...TimestampLocal'(현지 시각을 UTC처럼 담은 ms) -> KST ISO"""
    if not isinstance(ms, (int, float)):
        return None
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime("%Y-%m-%dT%H:%M:%S") + "+09:00"


def connect(db_path: Path) -> sqlite3.Connection:
    """DB를 연다. 없으면 빈 표를 만든다."""
    db_path = Path(db_path)
    new = not db_path.exists()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    if new:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO meta VALUES('built_at',?)", (dt.datetime.now(KST).isoformat(timespec="seconds"),))
        db.commit()
    return db


def _activity_row(day: str, entry: dict, adir: Path, fixes: list[dict] | None = None) -> tuple | None:
    aid = entry.get("activityId")
    if aid is None:
        return None
    summ = _load(adir / "summary.json") or {}
    sd = summ.get("summaryDTO") or {}

    def pick(*names):
        return _num(*[s.get(n) for s in (entry, sd) for n in names])
    typ = (entry.get("activityType") or {}).get("typeKey") or (summ.get("activityTypeDTO") or {}).get("typeKey")
    start = entry.get("startTimeLocal") or sd.get("startTimeLocal")
    dur, dist = pick("duration"), pick("distance")
    sets = (_load(adir / "exercise_sets.json") or {}).get("exerciseSets") or []
    if fixes:
        sets = corr.apply_sets(sets, fixes)[0]   # 직접 수정한 세트 반영 (삭제·반복 수 변경)
    real = [s for s in sets if s.get("setType") != "REST"]
    n_sets = len(real) if real else pick("totalSets")
    n_reps = sum(s.get("repetitionCount") or 0 for s in real) if real else pick("totalReps")
    z = [None] * 7
    zones = _load(adir / "hr_zones.json")
    if isinstance(zones, list):
        for zz in zones:
            n, secs = zz.get("zoneNumber"), zz.get("secsInZone")
            if isinstance(n, int) and 1 <= n <= 6 and isinstance(secs, (int, float)):
                z[n] = float(secs)
        if dur:   # z0 = Z1 미만 시간. API에는 없어서 총 시간에서 뺀 값(가민 내보내기와 수 초 차이 날 수 있음)
            z[0] = max(0.0, dur - sum(v or 0 for v in z[1:]))
        z = [0.0 if v is None and i else v for i, v in enumerate(z)]
    return (int(aid), "garmin", str(start)[:10] if start else day, _local_iso(start), typ,
            entry.get("activityName") or summ.get("activityName"), dur, dist,
            dist / dur if dur and dist else None,
            pick("averageHR"), pick("maxHR"), pick("minHR"), pick("calories"),
            pick("aerobicTrainingEffect"), pick("anaerobicTrainingEffect"), pick("activityTrainingLoad"),
            pick("averageRunningCadenceInStepsPerMinute", "averageRunCadence"),
            n_sets, n_reps, *z)


def sync_day(db: sqlite3.Connection, day_dir: Path, corr_root: Path | None = None) -> dict:
    """하루 원본 폴더 하나를 DB에 반영(같은 키는 덮어씀). 반영한 건수를 돌려준다."""
    day = day_dir.name
    fixes = corr.load(corr_root, day) if corr_root else {}
    n = {"activities": 0, "daily": 0, "sleep": 0, "health_status": 0, "readiness": 0}

    acts = (_load(day_dir / "activities.json") or {}).get("activities") or []
    for e in acts:
        row = _activity_row(day, e, day_dir / f"activity_{e.get('activityId')}",
                            corr.for_activity(fixes, e.get("activityId")))
        if row:
            db.execute("INSERT OR REPLACE INTO activities VALUES(" + ",".join("?" * 26) + ")", row)
            n["activities"] += 1

    d = _load(day_dir / "daily.json") or {}
    stats = d.get("stats") if isinstance(d.get("stats"), dict) and "error" not in d["stats"] else {}
    stress = d.get("stress") if isinstance(d.get("stress"), dict) and "error" not in d["stress"] else {}
    if stats:
        db.execute("INSERT OR REPLACE INTO daily VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            day, stats.get("totalSteps"), stats.get("restingHeartRate"), stats.get("minHeartRate"),
            stats.get("maxHeartRate"), _num(stress.get("avgStressLevel"), stats.get("averageStressLevel")),
            stats.get("bodyBatteryHighestValue"), stats.get("bodyBatteryLowestValue"),
            stats.get("activeKilocalories"), stats.get("totalKilocalories"),
            stats.get("moderateIntensityMinutes"), stats.get("vigorousIntensityMinutes"),
            stats.get("avgWakingRespirationValue")))
        n["daily"] = 1

    sl = d.get("sleep") if isinstance(d.get("sleep"), dict) and "error" not in d["sleep"] else {}
    sd = sl.get("dailySleepDTO") or {}
    if sd.get("sleepTimeSeconds"):
        score = ((sd.get("sleepScores") or {}).get("overall") or {}).get("value")
        db.execute("INSERT OR REPLACE INTO sleep VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
            sd.get("calendarDate") or day, _ms_local_iso(sd.get("sleepStartTimestampLocal")),
            _ms_local_iso(sd.get("sleepEndTimestampLocal")), sd.get("sleepTimeSeconds"),
            sd.get("deepSleepSeconds"), sd.get("lightSleepSeconds"), sd.get("remSleepSeconds"),
            sd.get("awakeSleepSeconds"), score, sd.get("awakeCount"), sd.get("avgSleepStress"),
            sd.get("averageRespirationValue")))
        n["sleep"] = 1

    hv = (d.get("hrv") or {}).get("hrvSummary") if isinstance(d.get("hrv"), dict) else None
    if hv and hv.get("lastNightAvg") is not None:
        b = hv.get("baseline") or {}
        # health_status 의 hr_* 칸(안정시 심박 기준선)은 이 경로로는 알 수 없어 비워 둔다. 기존 값이 있으면 보존.
        old = db.execute("SELECT hr,hr_low,hr_high,hr_status FROM health_status WHERE day=?", (day,)).fetchone() or (None,) * 4
        db.execute("INSERT OR REPLACE INTO health_status VALUES(?,?,?,?,?,?,?,?,?)", (
            day, hv.get("lastNightAvg"), b.get("balancedLow"), b.get("balancedUpper"), hv.get("status"), *old))
        n["health_status"] = 1

    rd = d.get("training_readiness")
    for r in rd if isinstance(rd, list) else []:
        if isinstance(r, dict) and r.get("score") is not None:
            db.execute("INSERT OR REPLACE INTO readiness VALUES(?,?,?,?,?,?,?)", (
                r.get("calendarDate") or day, r.get("timestamp"), r.get("score"), r.get("level"),
                r.get("sleepScore"), r.get("recoveryTime"), r.get("acwrFactorPercent")))
            n["readiness"] += 1
    return n


def sync_all(db_path: Path, raw_root: Path, days: list[str] | None = None, corr_root: Path | None = None) -> dict:
    """raw_root 아래 날짜 폴더를 모두(또는 days만) DB에 반영한다."""
    db = connect(db_path)
    total: dict[str, int] = {}
    try:
        for d in sorted(p for p in Path(raw_root).glob("????-??-??") if p.is_dir() and (not days or p.name in days)):
            for k, v in sync_day(db, d, corr_root).items():
                total[k] = total.get(k, 0) + v
            total["days"] = total.get("days", 0) + 1
        db.execute("INSERT OR REPLACE INTO meta VALUES('synced_at',?)", (dt.datetime.now(KST).isoformat(timespec="seconds"),))
        db.commit()
    finally:
        db.close()
    return total


def status(db_path: Path) -> dict:
    p = Path(db_path)
    if not p.is_file():
        return {"exists": False}
    db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        out = {"exists": True, "tables": {}, "meta": dict(db.execute("SELECT key,value FROM meta"))}
        for t, col in (("activities", "day"), ("daily", "day"), ("sleep", "day")):
            n, lo, hi = db.execute(f"SELECT COUNT(*),MIN({col}),MAX({col}) FROM {t}").fetchone()
            out["tables"][t] = {"rows": n, "from": lo, "to": hi}
        return out
    finally:
        db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="앱이 받은 하루 원본을 이력 DB에 반영")
    ap.add_argument("--db", default="data/history.db")
    ap.add_argument("--raw", default="data/raw")
    ap.add_argument("--corrections", default=".", help="corrections/ 폴더의 부모")
    a = ap.parse_args()
    print(sync_all(Path(a.db), Path(a.raw), None, Path(a.corrections)))
