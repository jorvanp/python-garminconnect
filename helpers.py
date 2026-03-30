from datetime import date, timedelta
import logging

from tz_utils import today_tz

logger = logging.getLogger(__name__)

DEFAULT_GOAL = {
    "race_type": "Carrera",
    "target_pace_str": "5:00",
    "target_pace_min": 5,
    "target_pace_sec": 0,
    "weekly_peak_km": 40,
    "easy_hr_max": 155,
    "tempo_hr_min": 155,
    "tempo_hr_max": 170,
    "interval_hr_min": 170,
    "description": "",
}


def _merge_goal(user_goal: dict | None) -> dict:
    """Returns goal dict filled with defaults for any missing keys."""
    base = dict(DEFAULT_GOAL)
    if user_goal:
        base.update({k: v for k, v in user_goal.items() if v not in (None, "")})
    return base


def process_dashboard_data(raw_data, training_goal: dict | None = None, user_tz: str | None = None):
    """Processes raw garmin json data into flattened variables for Jinja."""
    if not raw_data or "months" not in raw_data:
        return None

    goal_configured = bool(training_goal and training_goal.get('target_pace_str'))
    goal = _merge_goal(training_goal)

    today_str = today_tz(user_tz).isoformat()
    yesterday_str = (today_tz(user_tz) - timedelta(days=1)).isoformat()

    last_month_key = sorted(raw_data["months"].keys())[-1]
    daily_stats = raw_data["months"][last_month_key].get("daily_stats", {})

    def extract_metric(key, extractor_fn, default='—'):
        """Busca hacia atrás (hasta 7 días) hasta encontrar una métrica válida."""
        current_dt = today_tz(user_tz)
        for _ in range(7):
            d_str = current_dt.isoformat()
            month_key = d_str[:7]
            if month_key in raw_data["months"]:
                day_dict = raw_data["months"][month_key].get("daily_stats", {}).get(d_str, {})
                val = day_dict.get(key)
                if val:
                    try:
                        res = extractor_fn(val)
                        if res not in (None, '', '—', 0, 0.0):
                            return res
                    except Exception as e:
                        logger.warning(f"Error parsing metric {key}: {e}")
            current_dt -= timedelta(days=1)
        return default

    # Extract VO2 Max from the most recent activity that has it
    vo2_max = "—"
    for m in sorted(raw_data["months"].keys(), reverse=True):
        for act in raw_data["months"][m].get("activities", []):
            v = act.get("vO2MaxValue")
            if v and v > 0:
                vo2_max = round(v, 1)
                break
        if vo2_max != "—":
            break

    def parse_rhr(x):
        try:
            if "allMetrics" in x:
                metrics_map = x.get("allMetrics", {}).get("metricsMap", {})
                rhr_list = metrics_map.get("WELLNESS_RESTING_HEART_RATE", [])
                if rhr_list and rhr_list[0].get("value"):
                    return int(rhr_list[0]["value"])
            return int(x.get("restingHeartRate", 0)) or None
        except Exception:
            return None

    rhr = extract_metric('resting_heart_rate', parse_rhr)

    def parse_bb(x):
        values_array = x.get('bodyBatteryValuesArray', [])
        for item in reversed(values_array):
            if isinstance(item, list) and len(item) >= 3:
                bb_val = item[2]
                if bb_val is not None and bb_val > 0:
                    return bb_val
        return None

    current_bb = extract_metric('stress', parse_bb)
    if not current_bb:
        current_bb = '—'

    sleep_score = extract_metric('sleep', lambda x: x.get('dailySleepDTO', {}).get('sleepScores', {}).get('overall', {}).get('value'))
    sleep_hours = extract_metric('sleep', lambda x: round((x.get('dailySleepDTO', {}).get('sleepTimeSeconds') or 0) / 3600, 1), default=0.0)
    stress_score = extract_metric('stress', lambda x: x.get('avgStressLevel'))

    # Historical chart data (RHR by month — kept for when section is re-enabled)
    chart_months = []
    chart_rhr = []
    for m in sorted(raw_data["months"].keys()):
        chart_months.append(m[-2:])
        m_rhrs = []
        for d in raw_data["months"][m].get('daily_stats', {}).values():
            if not isinstance(d, dict):
                continue
            val = parse_rhr(d.get('resting_heart_rate', {}))
            if val and val > 0:
                m_rhrs.append(val)
        avg_rhr = int(sum(m_rhrs) / len(m_rhrs)) if m_rhrs else 0
        chart_rhr.append(avg_rhr)

    # All activities flat list
    all_activities = []
    for m in sorted(raw_data["months"].keys()):
        all_activities.extend(raw_data["months"][m].get("activities", []))
    all_activities.sort(key=lambda x: x.get("startTimeLocal", ""), reverse=True)

    # Weekly totals (last 7 days)
    seven_days_ago = (today_tz(user_tz) - timedelta(days=7)).isoformat()
    runs_count = 0
    strength_count = 0
    total_run_distance = 0.0

    for act in all_activities:
        if act.get("startTimeLocal", "") >= seven_days_ago:
            act_type = act.get("activityType", {}).get("typeKey", "")
            if "run" in act_type:
                runs_count += 1
                total_run_distance += act.get("distance", 0) / 1000.0
            elif "strength" in act_type:
                strength_count += 1

    # Enrich display fields for all activities shown in the table (not limited to 7 days)
    def _enrich(act):
        act["_date"] = act.get("startTimeLocal", "").split(" ")[0]
        act["_dist_km"] = round(act.get("distance", 0) / 1000.0, 1) if act.get("distance") else "—"
        dur_sec = act.get("duration", 0)
        act["_dur_min"] = int(dur_sec / 60) if dur_sec else "—"
        speed_ms = act.get("averageSpeed", 0)
        if speed_ms and speed_ms > 0:
            pace_sec = 1000 / speed_ms
            act["_pace"] = f"{int(pace_sec // 60)}:{int(pace_sec % 60):02d} /km"
        else:
            act["_pace"] = "—"
        load = act.get("activityTrainingLoad")
        act["_load"] = round(load) if load else "—"
        aero = act.get("aerobicTrainingEffect")
        anaero = act.get("anaerobicTrainingEffect")
        if aero is not None and anaero is not None:
            act["_effect"] = f"{round(aero, 1)} / {round(anaero, 1)}"
        else:
            act["_effect"] = "—"
        # Stride length (realistic running range: 60–230 cm)
        stride_cm = act.get("avgStrideLength")
        if stride_cm and 60 < stride_cm < 230:
            act["_stride"] = f"{round(stride_cm)} cm"
        else:
            act["_stride"] = "—"
        # Ground contact balance (left %) — realistic range 40–60
        balance = act.get("avgGroundContactBalance")
        if balance and 40 < balance < 60:
            act["_balance"] = f"{round(balance, 1)}% izq / {round(100 - balance, 1)}% der"
        else:
            act["_balance"] = "—"
        return act

    recent_activities = [_enrich(a) for a in all_activities[:15] if a.get("startTimeLocal")]

    # Total training load for the last 7 days
    total_load_7d = 0
    for act in all_activities:
        if act.get("startTimeLocal", "") >= seven_days_ago:
            load_val = act.get("activityTrainingLoad")
            if load_val:
                total_load_7d += load_val
    total_load_7d = round(total_load_7d) if total_load_7d else "—"

    # Weekly km chart — last 7 weeks (Mon–Sun) including current
    today = today_tz(user_tz)
    current_week_monday = today - timedelta(days=today.weekday())
    weekly_km_chart_labels = []
    weekly_km_chart_data = []
    for w in range(6, -1, -1):
        week_start = current_week_monday - timedelta(weeks=w)
        week_end = week_start + timedelta(days=6)
        label = "Esta sem." if w == 0 else week_start.strftime("%d/%m")
        km = 0.0
        for act in all_activities:
            act_date_str = act.get("startTimeLocal", "")[:10]
            if not act_date_str:
                continue
            try:
                act_date = date.fromisoformat(act_date_str)
            except ValueError:
                continue
            if week_start <= act_date <= week_end:
                if "run" in act.get("activityType", {}).get("typeKey", ""):
                    km += act.get("distance", 0) / 1000.0
        weekly_km_chart_labels.append(label)
        weekly_km_chart_data.append(round(km, 1))

    # Progress bar calculations using user goal
    weekly_peak = float(goal["weekly_peak_km"])
    total_run_distance = round(total_run_distance, 1)
    weekly_remaining = round(max(0, weekly_peak - total_run_distance), 1)
    weekly_progress_pct = round(min(100, (total_run_distance / weekly_peak) * 100), 1)

    # Progress bar reference markers (75%, 100%, 112.5% of peak)
    marker_base_pct = round(75 / weekly_peak * 100, 1)   # "base" line
    marker_peak_pct = 100.0                                # peak line
    marker_max_pct = round(min(112.5 / weekly_peak * 100, 100), 1)

    # Count actual days with loaded data
    all_dates_with_data = set()
    for m in raw_data["months"].values():
        for d_str, d_val in m.get("daily_stats", {}).items():
            if d_val:  # only count days that have at least some data
                all_dates_with_data.add(d_str)
    days_loaded = len(all_dates_with_data)
    earliest_date = min(all_dates_with_data) if all_dates_with_data else None

    return {
        "user_profile": raw_data.get("user_profile", {}),
        "date_str": today_tz(user_tz).strftime("%d de %B de %Y"),
        "vo2_max": vo2_max,
        "rhr": rhr,
        "body_battery": current_bb,
        "sleep_hours": sleep_hours,
        "sleep_score": sleep_score,
        "stress_score": stress_score,
        "chart_months": chart_months,
        "chart_rhr": chart_rhr,
        "total_load_7d": total_load_7d,
        "runs_count": runs_count,
        "strength_count": strength_count,
        "total_run_distance": total_run_distance,
        "weekly_remaining": weekly_remaining,
        "weekly_progress_pct": weekly_progress_pct,
        "weekly_peak": weekly_peak,
        "marker_base_pct": marker_base_pct,
        "marker_peak_pct": marker_peak_pct,
        "marker_max_pct": marker_max_pct,
        "weekly_km_chart_labels": weekly_km_chart_labels,
        "weekly_km_chart_data": weekly_km_chart_data,
        "recent_activities": recent_activities,
        "training_goal": goal,
        "goal_configured": goal_configured,
        "days_loaded": days_loaded,
        "earliest_date": earliest_date,
        "ai_recommendation": raw_data.get("metadata", {}).get("ai_recommendation"),
        "raw_data": raw_data,
    }
