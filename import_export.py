#!/usr/bin/env python3
"""Garmin 'Export Your Data' ZIP 압축 해제 폴더 -> 분석용 SQLite (data/history.db).

사용: python import_export.py <DI_CONNECT 또는 4개 폴더가 있는 경로> [--db data/history.db]
- 원본 JSON은 건드리지 않는다. DB는 언제든 다시 만들 수 있다(매번 새로 생성).
- 시각은 한국 시간(KST, UTC+9) 기준 문자열. 건강 데이터이므로 DB/원본은 저장소에 올리지 않는다.
- 출처 구분: source = 'garmin'(기기 기록) / 'imported'(deviceId 0, 삼성 등에서 가져온 기록)
"""
import argparse, glob, json, os, sqlite3, datetime as dt

KST = dt.timezone(dt.timedelta(hours=9))


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def kst(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).astimezone(KST) if ms else None


def gmt_str(s):
    """'2026-11-11T15:26:35.0' (GMT) -> KST ISO"""
    if not s:
        return None
    d = dt.datetime.fromisoformat(s.rstrip("0").rstrip(".")).replace(tzinfo=dt.UTC)
    return d.astimezone(KST).isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE activities(
  activity_id INTEGER PRIMARY KEY, source TEXT, day TEXT, start_local TEXT,
  type_key TEXT, name TEXT, duration_s REAL, distance_m REAL, speed_ms REAL,
  avg_hr REAL, max_hr REAL, min_hr REAL, calories REAL,
  te_aerobic REAL, te_anaerobic REAL, training_load REAL, avg_cadence REAL,
  total_sets INTEGER, total_reps INTEGER,
  z0 REAL, z1 REAL, z2 REAL, z3 REAL, z4 REAL, z5 REAL, z6 REAL);
CREATE INDEX ix_act_day ON activities(day);
CREATE INDEX ix_act_type ON activities(type_key, day);
CREATE TABLE daily(
  day TEXT PRIMARY KEY, steps INTEGER, resting_hr INTEGER, min_hr INTEGER, max_hr INTEGER,
  stress_avg INTEGER, bb_high INTEGER, bb_low INTEGER, active_kcal REAL, total_kcal REAL,
  moderate_min INTEGER, vigorous_min INTEGER, resp_avg REAL);
CREATE TABLE sleep(
  day TEXT PRIMARY KEY, start_local TEXT, end_local TEXT, total_s INTEGER,
  deep_s INTEGER, light_s INTEGER, rem_s INTEGER, awake_s INTEGER,
  score INTEGER, awake_count INTEGER, avg_stress REAL, resp_avg REAL);
CREATE TABLE health_status(
  day TEXT PRIMARY KEY, hrv REAL, hrv_low REAL, hrv_high REAL, hrv_status TEXT,
  hr REAL, hr_low REAL, hr_high REAL, hr_status TEXT);
CREATE TABLE readiness(
  day TEXT, timestamp TEXT, score INTEGER, level TEXT, sleep_score INTEGER,
  recovery_time INTEGER, acwr_pct INTEGER, PRIMARY KEY(day, timestamp));
CREATE TABLE acute_load(
  day TEXT PRIMARY KEY, acute REAL, chronic REAL, acwr REAL, status TEXT);
CREATE TABLE vo2max(day TEXT PRIMARY KEY, sport TEXT, vo2max REAL);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--db", default="data/history.db")
    a = ap.parse_args()
    root = a.root
    if os.path.isdir(os.path.join(root, "DI_CONNECT")):
        root = os.path.join(root, "DI_CONNECT")
    os.makedirs(os.path.dirname(a.db) or ".", exist_ok=True)
    if os.path.exists(a.db):
        os.remove(a.db)
    db = sqlite3.connect(a.db)
    db.executescript(SCHEMA)

    # 활동
    n = 0
    for f in sorted(glob.glob(f"{root}/DI-Connect-Fitness/*summarizedActivities.json")):
        for w in load(f):
            for x in w["summarizedActivitiesExport"]:
                t = kst(x.get("startTimeGmt"))
                if not t:
                    continue
                dur = (x.get("duration") or 0) / 1000
                dist = (x.get("distance") or 0) / 100
                z = [(x.get(f"hrTimeInZone_{i}") or 0) / 1000 for i in range(7)]
                db.execute(
                    "INSERT OR REPLACE INTO activities VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (x["activityId"], "imported" if x.get("deviceId") == 0 else "garmin",
                     t.strftime("%Y-%m-%d"), t.isoformat(timespec="seconds"),
                     x.get("activityType"), x.get("name"), dur, dist,
                     dist / dur if dur and dist else None,
                     x.get("avgHr"), x.get("maxHr"), x.get("minHr"), x.get("calories"),
                     x.get("aerobicTrainingEffect"), x.get("anaerobicTrainingEffect"),
                     x.get("activityTrainingLoad"), x.get("avgDoubleCadence") or x.get("avgFractionalCadence"),
                     x.get("totalSets"), x.get("totalReps"), *z))
                n += 1
    print("activities", n)

    # 일별 요약
    n = 0
    for f in sorted(glob.glob(f"{root}/DI-Connect-Aggregator/UDSFile_*.json")):
        for u in load(f):
            bb = {s["bodyBatteryStatType"]: s.get("statsValue")
                  for s in (u.get("bodyBattery") or {}).get("bodyBatteryStatList", [])}
            stress = next((g.get("averageStressLevel") for g in
                           (u.get("allDayStress") or {}).get("aggregatorList", []) if g.get("type") == "TOTAL"), None)
            db.execute("INSERT OR REPLACE INTO daily VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (u["calendarDate"], u.get("totalSteps"), u.get("restingHeartRate"),
                        u.get("minHeartRate"), u.get("maxHeartRate"), stress,
                        bb.get("HIGHEST"), bb.get("LOWEST"), u.get("activeKilocalories"),
                        u.get("totalKilocalories"), u.get("moderateIntensityMinutes"),
                        u.get("vigorousIntensityMinutes"),
                        (u.get("respiration") or {}).get("avgWakingRespirationValue")))
            n += 1
    print("daily", n)

    # 수면
    n = 0
    for f in sorted(glob.glob(f"{root}/DI-Connect-Wellness/*_sleepData.json")):
        for s in load(f):
            if "calendarDate" not in s:
                continue
            tot = sum(s.get(k) or 0 for k in ("deepSleepSeconds", "lightSleepSeconds", "remSleepSeconds"))
            db.execute("INSERT OR REPLACE INTO sleep VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (s["calendarDate"], gmt_str(s.get("sleepStartTimestampGMT")),
                        gmt_str(s.get("sleepEndTimestampGMT")), tot,
                        s.get("deepSleepSeconds"), s.get("lightSleepSeconds"), s.get("remSleepSeconds"),
                        s.get("awakeSleepSeconds"), (s.get("sleepScores") or {}).get("overallScore"),
                        s.get("awakeCount"), s.get("avgSleepStress"), s.get("averageRespiration")))
            n += 1
    print("sleep", n)

    # HRV / 안정시 심박 기준선
    n = 0
    for f in sorted(glob.glob(f"{root}/DI-Connect-Wellness/*_healthStatusData.json")):
        for h in load(f):
            if "calendarDate" not in h:
                continue
            m = {x["type"]: x for x in h.get("metrics", [])}
            hv, hr = m.get("HRV", {}), m.get("HR", {})
            db.execute("INSERT OR REPLACE INTO health_status VALUES(?,?,?,?,?,?,?,?,?)",
                       (h["calendarDate"], hv.get("value"), hv.get("baselineLowerLimit"),
                        hv.get("baselineUpperLimit"), hv.get("status"), hr.get("value"),
                        hr.get("baselineLowerLimit"), hr.get("baselineUpperLimit"), hr.get("status")))
            n += 1
    print("health_status", n)

    # 훈련 준비도, 급성/만성 부하, VO2max
    n = 0
    for f in sorted(glob.glob(f"{root}/DI-Connect-Metrics/TrainingReadinessDTO_*.json")):
        for r in load(f):
            db.execute("INSERT OR REPLACE INTO readiness VALUES(?,?,?,?,?,?,?)",
                       (r["calendarDate"], r.get("timestamp"), r.get("score"), r.get("level"),
                        r.get("sleepScore"), r.get("recoveryTime"), r.get("acwrFactorPercent")))
            n += 1
    print("readiness", n)
    n = 0
    for f in sorted(glob.glob(f"{root}/DI-Connect-Metrics/MetricsAcuteTrainingLoad_*.json")):
        for r in load(f):
            d = r["calendarDate"]
            d = kst(d).strftime("%Y-%m-%d") if isinstance(d, (int, float)) else d
            db.execute("INSERT OR REPLACE INTO acute_load VALUES(?,?,?,?,?)",
                       (d, r.get("dailyTrainingLoadAcute"), r.get("dailyTrainingLoadChronic"),
                        r.get("dailyAcuteChronicWorkloadRatio"), r.get("acwrStatus")))
            n += 1
    print("acute_load", n)
    n = 0
    for f in sorted(glob.glob(f"{root}/DI-Connect-Metrics/MetricsMaxMetData_*.json")):
        for r in load(f):
            db.execute("INSERT OR REPLACE INTO vo2max VALUES(?,?,?)",
                       (r["calendarDate"], r.get("sport"), r.get("vo2MaxValue")))
            n += 1
    print("vo2max", n)

    db.execute("INSERT INTO meta VALUES('built_at',?)", (dt.datetime.now(KST).isoformat(timespec="seconds"),))
    db.commit()
    db.close()


if __name__ == "__main__":
    main()
