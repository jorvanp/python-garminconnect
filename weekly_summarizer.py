"""
Computes weekly training summaries from raw Garmin data.
These summaries are stored in Firestore and used as AI context
instead of iterating raw activity lists.
"""
import logging
from datetime import date, timedelta

logger = logging.getLogger(__name__)


def compute_weekly_summaries(raw_data: dict) -> dict:
    """
    Computes weekly training summaries from raw Garmin data.
    Returns a dict keyed by ISO week (e.g. "2025-W10") with summary stats.
    """
    if not raw_data or "months" not in raw_data:
        return {}

    # Collect all activities
    all_activities = []
    for m_data in raw_data["months"].values():
        all_activities.extend(m_data.get("activities", []))
    all_activities.sort(key=lambda x: x.get("startTimeLocal", ""))

    weeks: dict = {}

    for act in all_activities:
        date_str = act.get("startTimeLocal", "")[:10]
        if not date_str:
            continue
        try:
            act_date = date.fromisoformat(date_str)
        except ValueError:
            continue

        iso_year, iso_week, _ = act_date.isocalendar()
        week_key = f"{iso_year}-W{iso_week:02d}"
        week_monday = act_date - timedelta(days=act_date.weekday())
        week_sunday = week_monday + timedelta(days=6)

        if week_key not in weeks:
            weeks[week_key] = {
                "week_start": week_monday.isoformat(),
                "week_end": week_sunday.isoformat(),
                "run_count": 0,
                "strength_count": 0,
                "total_km": 0.0,
                "fastest_pace_sec": None,
                "total_pace_sec": 0.0,
                "run_with_pace_count": 0,
                "vo2_values": [],
                "stride_values": [],
                "balance_values": [],
                "hr_values": [],
                "elevation_total": 0.0,
                "bb_values": [],
                "sleep_values": [],
            }

        week = weeks[week_key]
        act_type = act.get("activityType", {}).get("typeKey", "")

        if "run" in act_type:
            week["run_count"] += 1
            week["total_km"] += act.get("distance", 0) / 1000.0

            speed = act.get("averageSpeed", 0)
            if speed and speed > 0:
                pace_sec = 1000.0 / speed
                week["total_pace_sec"] += pace_sec
                week["run_with_pace_count"] += 1
                if week["fastest_pace_sec"] is None or pace_sec < week["fastest_pace_sec"]:
                    week["fastest_pace_sec"] = pace_sec

            v = act.get("vO2MaxValue")
            if v and v > 0:
                week["vo2_values"].append(v)

            stride = act.get("avgStrideLength")
            if stride and 60 < stride < 230:
                week["stride_values"].append(stride)

            balance = act.get("avgGroundContactBalance")
            if balance and 40 < balance < 60:
                week["balance_values"].append(balance)

            hr = act.get("averageHR")
            if hr and hr > 0:
                week["hr_values"].append(hr)

            elev = act.get("elevationGain") or 0
            week["elevation_total"] += elev

        elif "strength" in act_type:
            week["strength_count"] += 1

    # Aggregate daily sleep and body battery data into weeks
    for m_data in raw_data["months"].values():
        for d_str, d_val in m_data.get("daily_stats", {}).items():
            if not isinstance(d_val, dict):
                continue
            try:
                d = date.fromisoformat(d_str)
            except ValueError:
                continue
            iso_year, iso_week, _ = d.isocalendar()
            week_key = f"{iso_year}-W{iso_week:02d}"
            if week_key not in weeks:
                continue

            # Body battery: max value of the day ≈ wakeup reading
            stress = d_val.get("stress", {})
            if stress:
                bb_max = None
                for item in stress.get("bodyBatteryValuesArray", []):
                    if isinstance(item, list) and len(item) >= 3:
                        bb_val = item[2]
                        if bb_val is not None and bb_val > 0:
                            if bb_max is None or bb_val > bb_max:
                                bb_max = bb_val
                if bb_max:
                    weeks[week_key]["bb_values"].append(bb_max)

            # Sleep hours
            sleep = d_val.get("sleep", {})
            if sleep:
                sleep_secs = sleep.get("dailySleepDTO", {}).get("sleepTimeSeconds", 0)
                if sleep_secs and sleep_secs > 0:
                    weeks[week_key]["sleep_values"].append(sleep_secs / 3600.0)

    # Build final clean summaries
    def _avg(lst):
        return round(sum(lst) / len(lst), 1) if lst else None

    def _fmt_pace(sec):
        if sec is None:
            return None
        return f"{int(sec // 60)}:{int(sec % 60):02d}"

    result = {}
    for week_key, week in weeks.items():
        avg_pace_sec = (
            week["total_pace_sec"] / week["run_with_pace_count"]
            if week["run_with_pace_count"] > 0 else None
        )
        avg_vo2 = _avg(week["vo2_values"])
        avg_stride = _avg(week["stride_values"])
        avg_balance = _avg(week["balance_values"])
        avg_hr = _avg(week["hr_values"])
        avg_bb = _avg(week["bb_values"])
        avg_sleep = _avg(week["sleep_values"])

        result[week_key] = {
            "week_key": week_key,
            "week_start": week["week_start"],
            "week_end": week["week_end"],
            "run_count": week["run_count"],
            "strength_count": week["strength_count"],
            "total_km": round(week["total_km"], 1),
            "fastest_pace": _fmt_pace(week["fastest_pace_sec"]),
            "avg_pace": _fmt_pace(avg_pace_sec),
            "avg_vo2": avg_vo2,
            "avg_stride_cm": round(avg_stride) if avg_stride else None,
            "total_elevation_m": round(week["elevation_total"]),
            "avg_balance_left_pct": avg_balance,
            "avg_hr": round(avg_hr) if avg_hr else None,
            "avg_bb": round(avg_bb) if avg_bb else None,
            "avg_sleep_h": avg_sleep,
        }

    return result


def format_weekly_summaries_for_ai(weekly_summaries: dict, num_weeks: int = 26) -> str:
    """
    Formats weekly summaries as a human-readable text block for AI prompts.
    Only includes weeks with at least one run or strength session.
    """
    if not weekly_summaries:
        return "  (sin resúmenes semanales disponibles)"

    sorted_keys = sorted(weekly_summaries.keys(), reverse=True)[:num_weeks]
    sorted_keys.reverse()  # chronological order

    lines = []
    for wk in sorted_keys:
        w = weekly_summaries[wk]
        if w["run_count"] == 0 and w["strength_count"] == 0:
            continue

        row = [f"  {w['week_start']} → {w['week_end']}: {w['run_count']} carreras · {w['total_km']} km"]

        details = []
        if w.get("fastest_pace"):
            avg_p = w.get("avg_pace", "—")
            details.append(f"ritmo más rápido {w['fastest_pace']} /km · ritmo prom {avg_p} /km")
        if w.get("avg_vo2") is not None:
            details.append(f"VO2 prom {w['avg_vo2']}")
        if w.get("avg_stride_cm"):
            details.append(f"zancada {w['avg_stride_cm']} cm")
        if w.get("total_elevation_m"):
            details.append(f"desnivel {w['total_elevation_m']} m")
        if w.get("avg_balance_left_pct") is not None:
            left = w["avg_balance_left_pct"]
            right = round(100 - left, 1)
            details.append(f"pisada {left}% izq/{right}% der")
        if w.get("avg_hr"):
            details.append(f"FC prom {w['avg_hr']} bpm")
        if w.get("avg_bb") is not None:
            details.append(f"Body Battery al despertar {w['avg_bb']}%")
        if w.get("avg_sleep_h"):
            details.append(f"sueño {w['avg_sleep_h']}h")
        if w.get("strength_count"):
            details.append(f"fuerza {w['strength_count']}x")

        if details:
            row.append("    " + " · ".join(details))

        lines.append("\n".join(row))

    return "\n".join(lines) if lines else "  (sin datos semanales)"
