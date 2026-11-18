"""이력 DB와 가민 원본에서 폰으로 볼 CSV 네 개를 보관 폴더의 export/ 에 쓴다 (R18).

days.csv(하루 한 줄) / activities.csv(활동 한 건) / weeks.csv(주 하나) / months.csv(달 하나).
DB 전체 기간을 담고 최신이 위로 온다. 내용이 같으면 다시 쓰지 않는다(드라이브 동기화를 줄이려고).
주·월 CSV에는 웨이트 구분별(푸시/풀/하체/코어)·부위별·세부(어깨 프레스/레터럴/후면 등) 세트 수 열이 붙는다.
"""
from __future__ import annotations

import csv
import io
import tomllib
from datetime import date, timedelta
from pathlib import Path

import history_view as hv
import profile_data
import weekly_report as wk

FILES = ("days.csv", "activities.csv", "weeks.csv", "months.csv")


def _csv(rows: list[list]) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return "﻿" + buf.getvalue()           # 엑셀에서 한글이 깨지지 않게 BOM


def _sub_names(names_path: Path) -> list[str]:
    cfg = tomllib.loads(names_path.read_text(encoding="utf-8")) if names_path.is_file() else {}
    out: list[str] = []
    for v in (cfg.get("subparts") or {}).values():
        if str(v) not in out:
            out.append(str(v))
    return out


def _weight_cols(subs: list[str]) -> list[str]:
    return (["웨이트세트"] + [f"구분_{g}" for g in wk.GROUPS] + ["구분_미분류", "버티기세트", "버티기초"] + [f"부위_{p}" for p in wk.PARTS]
            + [s.replace("/", "_") for s in subs])


def _weight_vals(w: dict, subs: list[str]) -> list:
    if not w.get("rawDays"):
        return [""] * (len(wk.GROUPS) + 4 + len(wk.PARTS) + len(subs))
    parts = {b["part"]: b for b in w["body"]}
    sub = {f"{b['part']}/{x['name']}": x["sets"] for b in w["body"] for x in b["subs"]}
    return ([w["sets"]] + [w["groups"][g]["sets"] for g in wk.GROUPS] + [sum(x["sets"] for x in w["unclassified"]), w["holds"]["sets"], w["holds"]["sec"]] + [parts[p]["sets"] if p in parts else 0 for p in wk.PARTS]
            + [sub.get(s, 0) for s in subs])


def _period_row(db, a: date, b: date) -> list:
    s = hv.summarize(db, a.isoformat(), b.isoformat())
    return [s["activeDays"], s["sessions"], s["hours"], s["runKm"], s["sets"], s["load"],
            s["sleepH"], s["hrv"], s["restHr"]]


PERIOD_COLS = ["운동한날", "운동수", "시간(h)", "러닝(km)", "세트", "부하", "평균수면(h)", "평균HRV", "평균안정시심박"]


def build(db_path: Path, data_root: Path, corr_root: Path, names_path: Path, today: date) -> dict[str, str] | None:
    db = hv.connect_ro(db_path)
    if db is None:
        return None
    try:
        lo = db.execute("SELECT MIN(day) FROM (SELECT day FROM activities UNION SELECT day FROM daily UNION SELECT day FROM sleep)").fetchone()[0]
        if not lo:
            return None
        first, last = date.fromisoformat(lo), today
        subs = _sub_names(names_path)
        prof = profile_data.load(corr_root) if corr_root else []
        wday = {r["date"]: r["weight"] for r in prof if r["weight"]}

        def recent(a: date, b: date):
            cur, _ = profile_data.latest(prof, b.isoformat(), a.isoformat())
            return cur["weight"] if cur else ""
        out: dict[str, str] = {}

        rows = [["날짜", "요일", "운동", "운동수", "운동시간(분)", "러닝(km)", "세트", "부하",
                 "수면(h)", "수면점수", "HRV", "안정시심박", "체중(kg)"]]
        drows = hv.day_rows(db, first.isoformat(), last.isoformat())
        have = {d["date"] for d in drows}
        for k in wday:                                       # 체중만 잰 날도 한 줄
            if k not in have and first.isoformat() <= k <= last.isoformat():
                drows.append({"date": k, "weekday": hv.WEEKDAYS[date.fromisoformat(k).weekday()], "acts": [],
                              "sleepH": None, "sleepScore": None, "hrv": None, "restHr": None})
        drows.sort(key=lambda d: d["date"])
        for d in reversed(drows):
            acts = d["acts"]
            rows.append([d["date"], d["weekday"], " / ".join(f"{a['type']} {a['min']}분" for a in acts) or "휴식",
                         len(acts), sum(a["min"] for a in acts),
                         round(sum(a["km"] or 0 for a in acts if a["type"] == "러닝"), 1),
                         sum(a["sets"] or 0 for a in acts), sum(a["load"] or 0 for a in acts),
                         d["sleepH"], d["sleepScore"], d["hrv"], d["restHr"], wday.get(d["date"], "")])
        out["days.csv"] = _csv(rows)

        cols = [r[1] for r in db.execute("PRAGMA table_info(activities)")]
        out["activities.csv"] = _csv([cols] + [list(r) for r in db.execute(
            "SELECT * FROM activities ORDER BY start_local DESC")])

        wcols = _weight_cols(subs)
        rows = [["주", "시작", "끝"] + PERIOD_COLS + ["체중(최근)"] + wcols]
        d = hv.week_start(first)
        while d <= last:
            e = d + timedelta(days=6)
            w = wk.weight_summary(data_root, corr_root, d, e, names_path)
            rows.insert(1, [wk.month_week(wk.week_key(d)), d.isoformat(), e.isoformat()] + _period_row(db, d, e) + [recent(d, e)] + _weight_vals(w, subs))
            d = e + timedelta(days=1)
        out["weeks.csv"] = _csv(rows)

        rows = [["월", "시작", "끝"] + PERIOD_COLS + ["체중(최근)"] + wcols]
        d = first.replace(day=1)
        while d <= last:
            nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
            e = nxt - timedelta(days=1)
            w = wk.weight_summary(data_root, corr_root, d, e, names_path)
            rows.insert(1, [d.strftime("%Y-%m"), d.isoformat(), e.isoformat()] + _period_row(db, d, e) + [recent(d, e)] + _weight_vals(w, subs))
            d = nxt
        out["months.csv"] = _csv(rows)
        return out
    finally:
        db.close()


def write_all(out_dir: Path, files: dict[str, str]) -> int:
    """바뀐 파일만 쓴다. 쓴 개수를 돌려준다."""
    out_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for name, text in files.items():
        p = out_dir / name
        data = text.encode("utf-8")
        if p.is_file() and p.read_bytes() == data:
            continue
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)
        n += 1
    return n
