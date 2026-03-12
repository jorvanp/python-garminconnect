from datetime import date, timedelta
import logging

logger = logging.getLogger(__name__)

def process_dashboard_data(raw_data):
    """Processes raw garmin json data into flattened variables for Jinja."""
    if not raw_data or "months" not in raw_data:
        return None
        
    today_str = date.today().isoformat()
    yesterday_str = (date.today() - timedelta(days=1)).isoformat()
    
    # 1. Snapshot de hoy (o del último día disponible)
    last_month_key = sorted(raw_data["months"].keys())[-1]
    daily_stats = raw_data["months"][last_month_key].get("daily_stats", {})
    
    current_day = daily_stats.get(today_str, {})
    def extract_metric(key, extractor_fn, default='—'):
        """Busca hacia atrás (hasta 7 días) hasta encontrar una métrica válida."""
        current_dt = date.today()
        for _ in range(7):
            d_str = current_dt.isoformat()
            
            # Buscar el mes correspondiente a este date_str
            month_key = d_str[:7] # e.g '2026-03'
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
    
    # VO2 Max 
    vo2_max = 49 
    
    # RHR
    def parse_rhr(x):
        try:
            # Format varies. Sometimes it might be x.get("restingHeartRate") for another endpoint, 
            # but for daily statistics it's nested deep inside metricsMap
            if "allMetrics" in x:
                metrics_map = x.get("allMetrics", {}).get("metricsMap", {})
                rhr_list = metrics_map.get("WELLNESS_RESTING_HEART_RATE", [])
                if rhr_list and rhr_list[0].get("value"):
                    return int(rhr_list[0]["value"])
            # Fallback if it comes as a simpler dict in older Garmin formats
            return int(x.get("restingHeartRate", 0)) or None
        except Exception:
            return None

    rhr = extract_metric('resting_heart_rate', parse_rhr)
    
    # Body Battery
    def parse_bb(x):
        # We need to extract the last valid body battery value
        values_array = x.get('bodyBatteryValuesArray', [])
        for item in reversed(values_array):
            if isinstance(item, list) and len(item) >= 3:
                bb_val = item[2]
                if bb_val is not None and bb_val > 0:
                    return bb_val
        return None
            
    current_bb = extract_metric('stress', parse_bb) # body battery is inside the 'stress' endpoint dict
    if not current_bb: current_bb = '—'
    
    # Sleep
    sleep_score = extract_metric('sleep', lambda x: x.get('dailySleepDTO', {}).get('sleepScores', {}).get('overall', {}).get('value'))
    
    sleep_hours = extract_metric('sleep', lambda x: round(x.get('dailySleepDTO', {}).get('sleepTimeSeconds', 0) / 3600, 1), default=0.0)
    
    # Stress
    stress_score = extract_metric('stress', lambda x: x.get('avgStressLevel'))
    
    # Process Historical Data for Charts
    # We need last 6 months + current
    chart_months = []
    chart_rhr = []
    
    for m in sorted(raw_data["months"].keys()):
        chart_months.append(m[-2:]) # Just the MM part
        # average RHR for the month
        m_rhrs = []
        for d in raw_data["months"][m].get('daily_stats', {}).values():
            if not isinstance(d, dict): continue
            
            # Use the robust parse_rhr
            # The data for historical might just be the direct response of get_rhr_day
            val = parse_rhr(d.get('resting_heart_rate', {}))
            if val and val > 0:
                m_rhrs.append(val)
                
        avg_rhr = int(sum(m_rhrs)/len(m_rhrs)) if m_rhrs else 0
        chart_rhr.append(avg_rhr)

    # Process Recent Activities (last 7 days approx, just flatten all activities and take last 10)
    all_activities = []
    for m in sorted(raw_data["months"].keys()):
        all_activities.extend(raw_data["months"][m].get("activities", []))
    
    # Sort by start time descending
    all_activities.sort(key=lambda x: x.get("startTimeLocal", ""), reverse=True)
    recent_activities = all_activities[:15]
    
    # Calculate totals for the last 7 days (by looking at recent items within 7 days)
    seven_days_ago = (date.today() - timedelta(days=7)).isoformat()
    runs_count = 0
    strength_count = 0
    total_run_distance = 0.0
    total_load = 0
    
    for act in all_activities:
        if act.get("startTimeLocal", "") >= seven_days_ago:
            act_type = act.get("activityType", {}).get("typeKey", "")
            if "run" in act_type:
                runs_count += 1
                total_run_distance += (act.get("distance", 0) / 1000.0)
            elif "strength" in act_type:
                strength_count += 1
            
            # Sum up training load if available
            # Garmin stores it sometimes as vO2MaxValue, or trainingEffect, etc., 
            # but let's approximate by looking for vO2 or just simple count if missing
            # A real app would pull 'trainingLoad' metric but Garmin Connect varies by watch
            pass
            
            # Format some fields for the template nicely inside the act dictionary itself
            act["_date"] = act.get("startTimeLocal", "").split(" ")[0]
            act["_dist_km"] = round(act.get("distance", 0) / 1000.0, 1) if act.get("distance") else "—"
            dur_sec = act.get("duration", 0)
            act["_dur_min"] = int(dur_sec / 60) if dur_sec else "—"
            # Pace (min/km)
            speed_ms = act.get("averageSpeed", 0)
            if speed_ms and speed_ms > 0:
                pace_sec = 1000 / speed_ms
                act["_pace"] = f"{int(pace_sec // 60)}:{int(pace_sec % 60):02d} /km"
            else:
                act["_pace"] = "—"
                

    # Calculate final stats for weekly progress bar
    weekly_peak = 80.0
    total_run_distance = round(total_run_distance, 1)
    weekly_remaining = round(max(0, weekly_peak - total_run_distance), 1)
    weekly_progress_pct = round(min(100, (total_run_distance / weekly_peak) * 100), 1)

    # Compile the final dictionary that the Jinja template will consume
    return {
        "user_profile": raw_data.get("user_profile", {}),
        "date_str": date.today().strftime("%d de %B de %Y"),
        "vo2_max": vo2_max,
        "rhr": rhr,
        "body_battery": current_bb,
        "sleep_hours": sleep_hours,
        "sleep_score": sleep_score,
        "stress_score": stress_score,
        "chart_months": chart_months,
        "chart_rhr": chart_rhr,
        "runs_count": runs_count,
        "strength_count": strength_count,
        "total_run_distance": total_run_distance,
        "weekly_remaining": weekly_remaining,
        "weekly_progress_pct": weekly_progress_pct,
        "recent_activities": recent_activities,
        "ai_recommendation": raw_data.get("metadata", {}).get("ai_recommendation"),
        "raw_data": raw_data  # Pass through in case the template needs deep dive
    }
