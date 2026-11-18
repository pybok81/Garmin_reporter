"""프로필(체중·키) 기록 (R19). 보관 폴더의 profile/weight.csv 가 원본이고 DB에는 넣지 않는다.

잴 때만 한 줄씩 쌓는다. 같은 날짜를 다시 저장하면 그 줄을 바꾼다.
나중에 체지방률 같은 항목을 늘릴 때는 COLS 에 열을 추가하면 된다(예전 파일은 빈 칸으로 읽힌다).
"""
from __future__ import annotations

import csv
import io
from datetime import date
from pathlib import Path

COLS = ["날짜", "체중(kg)", "키(cm)", "메모"]


def path(root: Path) -> Path:
    return Path(root) / "profile" / "weight.csv"


def _num(v) -> float | None:
    try:
        x = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    return x if x > 0 else None


def load(root: Path) -> list[dict]:
    """날짜 오름차순. 각 줄: {date, weight, height, memo}."""
    p = path(root)
    if not p.is_file():
        return []
    out = []
    for r in csv.DictReader(io.StringIO(p.read_text(encoding="utf-8-sig"))):
        d = (r.get("날짜") or "").strip()
        try:
            date.fromisoformat(d)
        except ValueError:
            continue
        out.append({"date": d, "weight": _num(r.get("체중(kg)")), "height": _num(r.get("키(cm)")),
                    "memo": (r.get("메모") or "").strip()})
    return sorted(out, key=lambda x: x["date"])


def save(root: Path, rows: list[dict]) -> None:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(COLS)
    for r in sorted(rows, key=lambda x: x["date"]):
        w.writerow([r["date"], f"{r['weight']:g}" if r["weight"] else "", f"{r['height']:g}" if r["height"] else "",
                    r["memo"]])
    p = path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text("﻿" + buf.getvalue(), encoding="utf-8")
    tmp.replace(p)


def upsert(root: Path, day: str, weight: float | None, height: float | None, memo: str) -> list[dict]:
    rows = [r for r in load(root) if r["date"] != day]
    rows.append({"date": day, "weight": weight, "height": height, "memo": memo})
    save(root, rows)
    return load(root)


def remove(root: Path, day: str) -> list[dict]:
    rows = [r for r in load(root) if r["date"] != day]
    save(root, rows)
    return rows


def latest(rows: list[dict], upto: str, since: str | None = None) -> tuple[dict | None, dict | None]:
    """upto 이하(그리고 since 이상)에서 가장 최근 체중 기록과, 그 직전 체중 기록."""
    ws = [r for r in rows if r["weight"] and r["date"] <= upto]
    if not ws or (since and ws[-1]["date"] < since):
        return None, None
    return ws[-1], (ws[-2] if len(ws) > 1 else None)


def height_at(rows: list[dict], upto: str) -> float | None:
    hs = [r["height"] for r in rows if r["height"] and r["date"] <= upto]
    return hs[-1] if hs else None


def line(rows: list[dict], upto: str, since: str | None = None) -> str:
    """'체중 72.4kg (11-19 측정, 직전 11-12 대비 -0.6kg) · 키 175cm'. since 가 있으면 그 기간 안 측정만 본다."""
    cur, prev = latest(rows, upto, since)
    h = height_at(rows, upto)
    if cur is None:
        last, _ = latest(rows, upto)
        if since and last:
            s = f"이 기간 체중 측정 없음 (최근 {last['weight']:g}kg, {last['date'][5:]} 측정)"
        else:
            return ""
    else:
        s = f"체중 {cur['weight']:g}kg ({cur['date'][5:]} 측정"
        s += f", 직전 {prev['date'][5:]} 대비 {cur['weight'] - prev['weight']:+.1f}kg)" if prev else ")"
    if h:
        s += f" · 키 {h:g}cm"
    return s
