"""가민 데이터 직접 수정(보정) 저장·적용.

가민 원본 JSON은 건드리지 않는다. 수정 내용은 동기화 폴더의 corrections/<날짜>.json 에 따로 두고,
리포트를 만들 때와 DB에 넣을 때 덮어씌운다. 그래서 가민에서 다시 받아도 수정이 살아 있다.

파일 형태:
{
  "date": "2026-11-11",
  "updated": "2026-11-14T01:00:00+09:00",
  "activities": {
    "<활동ID>": {
      "sets": [
        {"i": 3, "was": "DEAD_BUG", "exercise": "BIRD_DOG", "label": "버드독",
         "reps": 12, "weight_kg": 0, "drop": false}
      ]
    }
  }
}
- i: 쉬는 세트를 뺀 운동 세트의 번호(1부터). 화면에 보이는 번호와 같다.
- was: 수정할 때의 원래 운동 코드. 가민에서 다시 받아 순서가 달라지면 맞지 않는 것으로 보고 적용하지 않는다.
- 없는 칸은 원래 값을 그대로 쓴다.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

KST = dt.timezone(dt.timedelta(hours=9))
SET_KEYS = ("exercise", "label", "reps", "weight_kg", "drop")


def file_path(root: Path, day: str) -> Path:
    return Path(root) / "corrections" / f"{day}.json"


def load(root: Path, day: str) -> dict:
    try:
        data = json.loads(file_path(root, day).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(root: Path, day: str, activities: dict) -> dict:
    """activities 가 비면 파일을 지운다."""
    p = file_path(root, day)
    activities = {k: v for k, v in activities.items() if v.get("sets")}
    if not activities:
        p.unlink(missing_ok=True)
        return {}
    data = {"date": day, "updated": dt.datetime.now(KST).isoformat(timespec="seconds"), "activities": activities}
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)
    return data


def set_code(s: dict) -> str:
    ex = (s.get("exercises") or [{}])[0] or {}
    return ex.get("name") or ex.get("category") or "UNKNOWN"


def for_activity(day_corr: dict, activity_id: Any) -> list[dict]:
    act = (day_corr.get("activities") or {}).get(str(activity_id)) or {}
    rows = act.get("sets")
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("i"), int)] if isinstance(rows, list) else []


def apply_sets(sets: list, rows: list[dict]) -> tuple[list, list[str], set[int]]:
    """운동 세트 목록에 수정을 덮어씌운 새 목록, 적용하지 못한 수정의 설명, 실제로 적용한 세트 번호를 돌려준다.

    수정된 세트에는 _corrected(원래 코드)와, 무게를 고친 경우 _wkg(kg)가 붙는다.
    """
    if not rows:
        return sets, [], set()
    by_i = {r["i"]: r for r in rows}
    out, warn, done, n = [], [], set(), 0
    for s in sets:
        if not isinstance(s, dict) or s.get("setType") == "REST":
            out.append(s)
            continue
        n += 1
        r = by_i.pop(n, None)
        if not r:
            out.append(s)
            continue
        was = set_code(s)
        if r.get("was") and r["was"] != was:
            warn.append(f"{n}번째 세트: 원본이 {was}로 바뀌어 수정({r['was']}→{r.get('exercise') or r.get('label')})을 적용하지 않았습니다")
            out.append(s)
            continue
        if r.get("drop"):   # 삭제는 describe() 에서 따로 적는다
            done.add(n)
            continue
        s = dict(s)
        done.add(n)
        s["_corrected"] = was
        if r.get("exercise"):
            s["exercises"] = [{"name": r["exercise"], "category": r["exercise"]}]
        if r.get("label"):
            s["_label"] = r["label"]
        if isinstance(r.get("reps"), int):
            s["repetitionCount"] = r["reps"]
        if isinstance(r.get("weight_kg"), (int, float)):
            s["_wkg"] = float(r["weight_kg"]) or None
        out.append(s)
    for i in sorted(by_i):
        warn.append(f"{i}번째 세트 수정: 해당 세트가 원본에 없어 적용하지 않았습니다")
    return out, warn, done


def describe(sets_before: list, rows: list[dict], names: dict[str, str],
             only: set[int] | None = None) -> list[str]:
    """리포트에 적을 '무엇을 고쳤는지' 줄들."""
    active = [s for s in sets_before if isinstance(s, dict) and s.get("setType") != "REST"]
    out = []
    for r in sorted(rows, key=lambda r: r["i"]):
        i = r["i"]
        if only is not None and i not in only:
            continue
        was = set_code(active[i - 1]) if 0 < i <= len(active) else (r.get("was") or "?")
        was_ko = names.get(was) or was
        if r.get("drop"):
            out.append(f"{i}번째 세트 {was_ko}: 삭제")
            continue
        bits = []
        if r.get("exercise") or r.get("label"):
            bits.append(f"{was_ko} → {r.get('label') or names.get(r['exercise']) or r['exercise']}")
        if isinstance(r.get("reps"), int):
            bits.append(f"{r['reps']}회")
        if isinstance(r.get("weight_kg"), (int, float)):
            bits.append(f"{r['weight_kg']:g}kg")
        if bits:
            out.append(f"{i}번째 세트: " + ", ".join(bits))
    return out
