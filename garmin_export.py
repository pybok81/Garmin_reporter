"""Garmin Connect 원본 데이터를 하루 단위 JSON으로 저장한다 (읽기 전용).

저장 구조 (원본은 항상 먼저 저장하고, 가공은 garmin_report.py가 한다):

    data/raw/YYYY-MM-DD/
        daily.json                 # 수면·HRV·바디배터리·스트레스 등 하루 지표
        activities.json            # 그날 운동 요약 목록
        activity_<ID>/
            summary.json           # 운동 상세 요약
            exercise_sets.json     # 웨이트 세트 (Garmin에서 편집한 값)
            hr_zones.json          # 심박 존별 시간
            splits.json            # 랩/스플릿

기본은 '오늘 하루'. 여러 날은 --days 로 지난 기록을 채울 때만 쓴다.
garminconnect는 커뮤니티 라이브러리라 Garmin 쪽 변경으로 깨질 수 있다.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from garminconnect import Garmin

DAILY_ENDPOINTS: dict[str, Callable[[Garmin, str], Any]] = {
    "stats": lambda c, d: c.get_stats(d),
    "heart_rate": lambda c, d: c.get_heart_rates(d),
    "sleep": lambda c, d: c.get_sleep_data(d),
    "stress": lambda c, d: c.get_stress_data(d),
    "body_battery": lambda c, d: c.get_body_battery(d),
    "respiration": lambda c, d: c.get_respiration_data(d),
    "spo2": lambda c, d: c.get_spo2_data(d),
    "intensity_minutes": lambda c, d: c.get_intensity_minutes_data(d),
    "hrv": lambda c, d: c.get_hrv_data(d),
    "training_readiness": lambda c, d: c.get_training_readiness(d),
}

ACTIVITY_ENDPOINTS: dict[str, Callable[[Garmin, str], Any]] = {
    "summary": lambda c, a: c.get_activity(a),
    "exercise_sets": lambda c, a: c.get_activity_exercise_sets(a),
    "hr_zones": lambda c, a: c.get_activity_hr_in_timezones(a),
    "splits": lambda c, a: c.get_activity_splits(a),
}


def load_project_env(path: Path = Path(".env")) -> None:
    """.env의 GARMIN_EMAIL / GARMIN_TOKEN_DIR만 읽는다 (기존 환경변수 우선)."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in {"GARMIN_EMAIL", "GARMIN_TOKEN_DIR"}:
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Garmin Connect 하루 단위 원본 수집")
    p.add_argument("--date", default=date.today().isoformat(), help="기준 날짜 YYYY-MM-DD (기본: 오늘)")
    p.add_argument("--days", type=int, default=1, help="--date 포함 과거 N일 (기본 1, 백필용)")
    p.add_argument("--output", type=Path, default=Path("data"), help="저장 폴더 (raw/ 하위에 날짜별 저장)")
    p.add_argument("--token-dir", type=Path, default=Path(os.getenv("GARMIN_TOKEN_DIR", ".garminconnect")))
    p.add_argument("--email", default=os.getenv("GARMIN_EMAIL"), help="Garmin 로그인 이메일")
    return p.parse_args()


def json_default(value: Any) -> str:
    return value.isoformat() if isinstance(value, (datetime, date)) else str(value)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")


def days_ending_on(end: str, count: int) -> list[str]:
    if count < 1:
        raise ValueError("--days는 1 이상이어야 합니다")
    last = date.fromisoformat(end)
    return [(last - timedelta(days=i)).isoformat() for i in range(count - 1, -1, -1)]


def safe_call(fetch: Callable[[Garmin, str], Any], client: Garmin, arg: str) -> Any:
    """지표는 기기/계정마다 없을 수 있으므로 실패해도 나머지는 계속 진행한다."""
    try:
        return fetch(client, arg)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def login(args: argparse.Namespace) -> Garmin | None:
    token_file = args.token_dir / "garmin_tokens.json"
    if not args.email and not token_file.exists():
        print("GARMIN_EMAIL 환경 변수 또는 --email 이 필요합니다.", file=sys.stderr)
        return None

    def attempt(password: str | None) -> Garmin:
        client = Garmin(args.email, password, prompt_mfa=lambda: input("MFA code: "))
        args.token_dir.mkdir(parents=True, exist_ok=True)
        client.login(str(args.token_dir))
        return client

    # 비밀번호는 저장하지 않는다. 유효한 토큰이 있으면 묻지 않는다.
    had_token = token_file.exists()
    try:
        return attempt(None if had_token else getpass.getpass("Garmin password: "))
    except Exception as first_error:  # noqa: BLE001
        if had_token and args.email:
            print("저장된 토큰이 거부되었습니다. 다시 로그인합니다.")
            try:
                return attempt(getpass.getpass("Garmin password: "))
            except Exception as second_error:  # noqa: BLE001
                print(f"Garmin 로그인 실패: {second_error}", file=sys.stderr)
                return None
        print(f"Garmin 로그인 실패: {first_error}", file=sys.stderr)
        return None


def export_day(client: Garmin, day: str, raw_root: Path) -> tuple[int, int]:
    day_dir = raw_root / day
    fetched_at = datetime.now().astimezone().isoformat()

    daily = {name: safe_call(fn, client, day) for name, fn in DAILY_ENDPOINTS.items()}
    write_json(day_dir / "daily.json", {"date": day, "fetched_at": fetched_at, **daily})

    try:
        activities = client.get_activities_by_date(day, day) or []
    except Exception as exc:  # noqa: BLE001
        print(f"  [{day}] 운동 목록 실패: {exc}", file=sys.stderr)
        activities = []
    write_json(day_dir / "activities.json", {"date": day, "fetched_at": fetched_at, "activities": activities})

    errors = sum(1 for v in daily.values() if isinstance(v, dict) and "error" in v)
    for act in activities:
        act_id = str(act.get("activityId"))
        for name, fn in ACTIVITY_ENDPOINTS.items():
            data = safe_call(fn, client, act_id)
            if isinstance(data, dict) and "error" in data:
                errors += 1
            write_json(day_dir / f"activity_{act_id}" / f"{name}.json", data)
    return len(activities), errors


def main() -> int:
    load_project_env()
    args = parse_args()
    try:
        days = days_ending_on(args.date, args.days)
    except ValueError as exc:
        print(f"날짜/기간 오류: {exc}", file=sys.stderr)
        return 2

    client = login(args)
    if client is None:
        return 1

    raw_root = args.output / "raw"
    for day in days:
        n_act, n_err = export_day(client, day, raw_root)
        note = f", 실패한 항목 {n_err}개(JSON 안 error 참고)" if n_err else ""
        print(f"{day}: 운동 {n_act}건 저장{note}")
    print(f"완료: {raw_root.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
