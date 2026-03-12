#!/usr/bin/env python3
"""
Garmin Training Analyzer — Análisis de Condición Física
========================================================
Lee training_data.json (últimos 7 días) y training_data_monthly.json (6 meses)
y genera un reporte HTML con análisis de condición física y recomendaciones.

Uso:
    python3 analyze_training.py
"""

import json
import os
import sys
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
WEEKLY_FILE = SCRIPT_DIR / "training_data.json"
MONTHLY_FILE = SCRIPT_DIR / "training_data_monthly.json"
OUTPUT_FILE = SCRIPT_DIR / "fitness_report.html"

ACTIVITY_LABELS = {
    "running": "Carrera exterior",
    "treadmill_running": "Cinta de correr",
    "strength_training": "Fuerza",
    "walking": "Caminata",
    "cycling": "Ciclismo",
    "swimming": "Natación",
    "yoga": "Yoga",
    "elliptical": "Elíptica",
    "cardio": "Cardio",
    "hiit": "HIIT",
}

SLEEP_SCORE_LABELS = {
    "EXCELLENT": "Excelente",
    "GOOD": "Bueno",
    "FAIR": "Regular",
    "POOR": "Malo",
}

VO2MAX_CATEGORIES = [
    (30, "Muy bajo"),
    (38, "Bajo"),
    (42, "Regular"),
    (47, "Bueno"),
    (52, "Muy bueno"),
    (57, "Excelente"),
    (float("inf"), "Superior"),
]

# ─────────────────────────────────────────────────────────────────────────────
# PLAN DE ENTRENAMIENTO — OBJETIVOS PERSONALIZADOS (Jorge)
# ─────────────────────────────────────────────────────────────────────────────
PLAN = {
    "target_pace_mps": 1000 / (4 * 60 + 40),   # 4:40 min/km → m/s
    "target_pace_str": "4:40 /km",
    "marathon_target": "sub 3:20",

    # Umbrales de FC por tipo de sesión (bpm absolutos)
    "hr_easy_max": 160,          # Trote ligero: mantenerse < 160
    "hr_long_min": 160,          # Fondo largo:  160–170
    "hr_long_max": 170,
    "hr_interval_min": 170,      # Repeticiones: llegar a 170–180

    # Volumen semanal (km)
    "weekly_km_base": 60,        # Semana normal mínima
    "weekly_km_peak": 80,        # Semana pico objetivo
    "weekly_km_max": 90,         # Techo máximo

    # Body Battery mínimo para sesión de calidad
    "bb_quality_threshold": 40,
    # RHR: cuántos bpm sobre el promedio = señal de fatiga
    "rhr_fatigue_delta": 4,
    # Sueño mínimo aceptable para sesión dura
    "sleep_score_quality": 70,
}

# Palabras clave en el nombre de la actividad para clasificar tipo de sesión
SESSION_KEYWORDS = {
    "interval": ["interval", "repeticion", "repeat", "serie", "x 1", "x 2", "x 3", "x 4", "x 5", "fartlek", "vo2"],
    "tempo":    ["tempo", "umbral", "threshold", "lactato", "ritmo maratón", "marathon pace", "mp"],
    "long":     ["long", "fondo", "largo", "lsd"],
    "easy":     ["easy", "recovery", "recuper", "aerob", "suave", "caminar", "walk", "jog"],
    "strength": ["fuerza", "strength", "gym", "weights", "functional"],
}


# ─────────────────────────────────────────────────────────────────────────────
# UTILIDADES
# ─────────────────────────────────────────────────────────────────────────────
def load_json(path: Path) -> dict | None:
    if not path.exists():
        print(f"⚠️  Archivo no encontrado: {path}")
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def fmt_duration(seconds: float) -> str:
    if seconds is None:
        return "—"
    m = int(seconds // 60)
    h = m // 60
    m = m % 60
    return f"{h}h {m}min" if h else f"{m} min"


def fmt_pace(speed_mps: float) -> str:
    """speed in m/s → pace as min/km string"""
    if not speed_mps or speed_mps <= 0:
        return "—"
    pace_s = 1000 / speed_mps
    minutes = int(pace_s // 60)
    seconds = int(pace_s % 60)
    return f"{minutes}:{seconds:02d} /km"


def vo2max_category(val: float) -> str:
    for threshold, label in VO2MAX_CATEGORIES:
        if val < threshold:
            return label
    return "Superior"


def sleep_qualifier(key: str) -> str:
    return SLEEP_SCORE_LABELS.get(key, key)


def activity_label(type_key: str) -> str:
    return ACTIVITY_LABELS.get(type_key, type_key.replace("_", " ").title())


def trend_arrow(values: list[float | None]) -> str:
    clean = [v for v in values if v is not None]
    if len(clean) < 2:
        return "→"
    delta = clean[-1] - clean[0]
    if delta > 1:
        return "↑"
    elif delta < -1:
        return "↓"
    return "→"


def color_for_score(val: float, good_high=True) -> str:
    """Returns a CSS color based on a 0-100 score."""
    if val is None:
        return "#aaa"
    if good_high:
        if val >= 80:
            return "#22c55e"
        elif val >= 65:
            return "#eab308"
        else:
            return "#ef4444"
    else:
        if val <= 30:
            return "#22c55e"
        elif val <= 55:
            return "#eab308"
        else:
            return "#ef4444"


# ─────────────────────────────────────────────────────────────────────────────
# LÓGICA DE PLAN — CLASIFICACIÓN Y CUMPLIMIENTO
# ─────────────────────────────────────────────────────────────────────────────

def classify_session(activity: dict) -> str:
    """Clasifica el tipo de sesión: interval / tempo / long / easy / strength / other."""
    name = (activity.get("name") or "").lower()
    type_key = activity.get("type") or ""
    aerobic_eff = activity.get("aerobic_effect") or 0
    anaerobic_eff = activity.get("anaerobic_effect") or 0

    if "strength" in type_key:
        return "strength"
    if "walk" in type_key:
        return "walk"

    for session_type, keywords in SESSION_KEYWORDS.items():
        if any(kw in name for kw in keywords):
            return session_type

    # Fallback: usar training effect si no hay keyword
    if "running" in type_key:
        if anaerobic_eff >= 2.5:
            return "interval"
        elif aerobic_eff >= 3.5:
            return "tempo"
        elif activity.get("distance_km", 0) >= 18:
            return "long"
        else:
            return "easy"
    return "other"


SESSION_TYPE_LABELS = {
    "interval":  ("🔴 Intervalos",   "Llegar 170–180 bpm"),
    "tempo":     ("🟠 Tempo",        "Mantener 160–170 bpm"),
    "long":      ("🟡 Fondo largo",  "Mantener 160–170 bpm"),
    "easy":      ("🟢 Trote suave",  "Mantenerse < 160 bpm"),
    "strength":  ("💪 Fuerza",       "—"),
    "walk":      ("🚶 Caminata",     "—"),
    "other":     ("⚪ Otro",         "—"),
}


def hr_compliance(activity: dict, session_type: str) -> dict:
    """
    Evalúa si la FC de la sesión cumplió con el objetivo del plan.
    Retorna dict con: ok (bool), color, label, detail.
    """
    avg_hr = activity.get("avg_hr") or 0
    max_hr = activity.get("max_hr") or 0
    z1 = activity.get("hr_z1") or 0
    z2 = activity.get("hr_z2") or 0
    z3 = activity.get("hr_z3") or 0
    z4 = activity.get("hr_z4") or 0
    z5 = activity.get("hr_z5") or 0
    total_hr_time = z1 + z2 + z3 + z4 + z5
    if total_hr_time == 0:
        return {"ok": None, "color": "#aaa", "label": "Sin datos FC", "detail": ""}

    pct_low = (z1 + z2 + z3) / total_hr_time * 100   # % debajo de ~160
    pct_high = (z4 + z5) / total_hr_time * 100         # % encima de ~170

    if session_type == "easy":
        ok = avg_hr < PLAN["hr_easy_max"]
        if ok:
            return {"ok": True,  "color": "#22c55e", "label": "✅ En zona", "detail": f"FC media {avg_hr} bpm (objetivo <{PLAN['hr_easy_max']})"}
        else:
            return {"ok": False, "color": "#ef4444", "label": "⚠️ Demasiado rápido", "detail": f"FC media {avg_hr} bpm — debía mantenerse <{PLAN['hr_easy_max']} bpm. Reduce el ritmo en trotes suaves."}

    elif session_type in ("long", "tempo"):
        in_zone = PLAN["hr_long_min"] <= avg_hr <= PLAN["hr_long_max"]
        if in_zone:
            return {"ok": True,  "color": "#22c55e", "label": "✅ En zona", "detail": f"FC media {avg_hr} bpm (objetivo {PLAN['hr_long_min']}–{PLAN['hr_long_max']})"}
        elif avg_hr < PLAN["hr_long_min"]:
            return {"ok": False, "color": "#eab308", "label": "🔽 Muy suave", "detail": f"FC media {avg_hr} bpm — estuvo por debajo de los {PLAN['hr_long_min']} bpm objetivo."}
        else:
            return {"ok": False, "color": "#ef4444", "label": "⚠️ Demasiado duro", "detail": f"FC media {avg_hr} bpm — superó el límite de {PLAN['hr_long_max']} bpm para esta sesión."}

    elif session_type == "interval":
        reached = max_hr >= PLAN["hr_interval_min"]
        high_pct = pct_high >= 20   # al menos 20% del tiempo en Z4/Z5
        if reached and high_pct:
            return {"ok": True,  "color": "#22c55e", "label": "✅ Intensidad lograda", "detail": f"FC máx {max_hr} bpm, {pct_high:.0f}% en Z4/Z5 (objetivo ≥170 bpm)"}
        elif reached:
            return {"ok": None,  "color": "#eab308", "label": "🟡 Parcial", "detail": f"FC máx {max_hr} bpm pero solo {pct_high:.0f}% del tiempo en zonas altas."}
        else:
            return {"ok": False, "color": "#ef4444", "label": "🔽 Faltó intensidad", "detail": f"FC máx {max_hr} bpm — no llegó a los {PLAN['hr_interval_min']} bpm objetivo de intervalos."}

    return {"ok": None, "color": "#aaa", "label": "—", "detail": ""}


def pace_compliance(activity: dict, session_type: str) -> dict:
    """Evalúa si el ritmo estuvo en línea con el objetivo de maratón."""
    speed = activity.get("averageSpeed") if isinstance(activity, dict) and "averageSpeed" in activity else None
    # activity aquí es el dict procesado de analyze_weekly, tiene "pace" como str
    # Necesitamos recalcular desde avg_speed — lo añadimos en analyze_weekly
    pace_str = activity.get("pace") or "—"
    avg_speed = activity.get("avg_speed_mps") or 0
    if not avg_speed or session_type in ("strength", "walk", "other"):
        return {"label": "—", "color": "#aaa", "detail": ""}

    target = PLAN["target_pace_mps"]
    diff_s = (1000 / avg_speed) - (1000 / target)   # positivo = más lento que objetivo

    if session_type == "interval":
        # En intervalos debe ir más rápido que 4:40
        if avg_speed >= target:
            return {"label": f"✅ {pace_str}", "color": "#22c55e", "detail": f"Más rápido que {PLAN['target_pace_str']} objetivo"}
        else:
            return {"label": f"🔽 {pace_str}", "color": "#eab308", "detail": f"{abs(diff_s):.0f}s/km por debajo del ritmo objetivo"}

    elif session_type in ("long", "tempo"):
        # Fondo largo y tempo: se permite hasta 30s más lento
        if diff_s <= 30:
            return {"label": f"✅ {pace_str}", "color": "#22c55e", "detail": f"Dentro del rango de {PLAN['target_pace_str']}"}
        else:
            return {"label": f"🔽 {pace_str}", "color": "#eab308", "detail": f"{diff_s:.0f}s/km por debajo del ritmo objetivo maratón"}

    else:  # easy
        # En suave no importa el pace, pero mostramos
        return {"label": pace_str, "color": "#888", "detail": "Trote suave, ritmo libre"}


def daily_prescription(weekly: dict, monthly: dict) -> dict:
    """
    Genera la prescripción del día: semáforo + mensaje basado en métricas actuales.
    """
    snap = weekly["today"]
    wa = weekly["weekly_avgs"]
    ws = weekly["week_summary"]
    months_data = monthly["months"]
    month_keys = sorted(months_data.keys())

    bb = snap.get("body_battery_current") or 0
    bb_start = snap.get("body_battery_start") or 0
    rhr = snap.get("resting_hr") or 0
    avg_rhr = wa.get("avg_rhr") or rhr
    sleep_score = snap.get("sleep_score") or 0
    sleep_h = snap.get("sleep_total_h") or 0

    rhr_delta = rhr - avg_rhr

    # Puntuación de fatiga (0 = descansado, 10 = agotado)
    fatigue_score = 0
    fatigue_reasons = []
    recovery_reasons = []

    if bb_start < PLAN["bb_quality_threshold"]:
        fatigue_score += 3
        fatigue_reasons.append(f"Body Battery al despertar bajo ({bb_start}%)")
    elif bb_start >= 70:
        recovery_reasons.append(f"Body Battery al despertar alto ({bb_start}%)")

    if rhr_delta >= PLAN["rhr_fatigue_delta"]:
        fatigue_score += 3
        fatigue_reasons.append(f"FC reposo {rhr} bpm (+{rhr_delta} sobre tu promedio)")
    elif rhr_delta <= -2:
        recovery_reasons.append(f"FC reposo muy baja ({rhr} bpm, -{abs(rhr_delta)} bajo tu promedio)")

    if sleep_score < PLAN["sleep_score_quality"]:
        fatigue_score += 2
        fatigue_reasons.append(f"Sueño de calidad regular ({sleep_score} pts, {sleep_h:.1f}h)")
    elif sleep_score >= 80:
        recovery_reasons.append(f"Sueño excelente ({sleep_score} pts)")

    if sleep_h < 6.5:
        fatigue_score += 2
        fatigue_reasons.append(f"Pocas horas de sueño ({sleep_h:.1f}h)")

    # Volumen semanal acumulado
    current_week_km = ws.get("total_run_km", 0)
    remaining_km = PLAN["weekly_km_peak"] - current_week_km
    days_left = 7 - date.today().weekday()  # días restantes en la semana

    # Semáforo
    if fatigue_score >= 6:
        status = "red"
        headline = "🔴 Día de recuperación recomendado"
        message = "Tus indicadores muestran fatiga acumulada significativa. Reemplaza cualquier sesión de calidad por trote muy suave (30 min <145 bpm), movilidad o descanso completo."
    elif fatigue_score >= 3:
        status = "yellow"
        headline = "🟡 Sesión con intensidad reducida"
        message = "Condición aceptable pero no óptima. Si hoy toca sesión de calidad, baja el volumen un 30% o cambia a trote aeróbico. Prioriza la recuperación esta noche."
    else:
        status = "green"
        headline = "🟢 Adelante con la sesión planificada"
        message = "Indicadores favorables para entrenamiento de calidad. Puedes ejecutar la sesión del plan tal como está."

    # Mensaje de volumen
    vol_message = ""
    if current_week_km >= PLAN["weekly_km_max"]:
        vol_message = f"Ya alcanzaste {current_week_km:.1f} km — estás en el techo semanal ({PLAN['weekly_km_max']} km). No agregues más volumen esta semana."
    elif current_week_km >= PLAN["weekly_km_peak"]:
        vol_message = f"Llevas {current_week_km:.1f} km — meta de semana pico ({PLAN['weekly_km_peak']} km) cumplida. Cualquier km extra es bonus."
    elif remaining_km > 0:
        vol_message = f"Llevas {current_week_km:.1f} km. Faltan ~{remaining_km:.0f} km para una semana pico de {PLAN['weekly_km_peak']} km ({days_left} días restantes)."

    return {
        "status": status,
        "headline": headline,
        "message": message,
        "fatigue_score": fatigue_score,
        "fatigue_reasons": fatigue_reasons,
        "recovery_reasons": recovery_reasons,
        "vol_message": vol_message,
        "current_week_km": current_week_km,
    }


# ─────────────────────────────────────────────────────────────────────────────
# ANÁLISIS DE DATOS
# ─────────────────────────────────────────────────────────────────────────────
def analyze_weekly(data: dict) -> dict:
    """Extract key metrics from the 7-day JSON."""
    daily = data.get("daily_stats", {})
    activities = data.get("activities", [])
    today_str = date.today().isoformat()

    # ── Today's snapshot ──────────────────────────────────────────────────
    today_data = daily.get(today_str, {})
    summary = today_data.get("summary", {}) or {}
    sleep_dto = (today_data.get("sleep") or {}).get("dailySleepDTO") or {}
    sleep_scores = sleep_dto.get("sleepScores") or {}

    today_snapshot = {
        "resting_hr": summary.get("restingHeartRate"),
        "body_battery_start": summary.get("bodyBatteryAtWakeTime"),
        "body_battery_current": summary.get("bodyBatteryMostRecentValue"),
        "body_battery_high": summary.get("bodyBatteryHighestValue"),
        "stress_avg": summary.get("averageStressLevel"),
        "steps": summary.get("totalSteps"),
        "active_kcal": summary.get("activeKilocalories"),
        "vigorous_min": summary.get("vigorousIntensityMinutes"),
        "moderate_min": summary.get("moderateIntensityMinutes"),
        "altitude_m": summary.get("averageMonitoringEnvironmentAltitude"),
        "sleep_score": sleep_scores.get("overall", {}).get("value"),
        "sleep_score_key": sleep_scores.get("overall", {}).get("qualifierKey"),
        "sleep_total_h": sleep_dto.get("sleepTimeSeconds", 0) / 3600 if sleep_dto else 0,
        "deep_pct": sleep_scores.get("deepPercentage", {}).get("value"),
        "rem_pct": sleep_scores.get("remPercentage", {}).get("value"),
        "light_pct": sleep_scores.get("lightPercentage", {}).get("value"),
        "awake_min": (sleep_dto.get("awakeSleepSeconds") or 0) / 60,
        "sleep_stress": sleep_dto.get("avgSleepStress"),
    }

    # ── 7-day averages ─────────────────────────────────────────────────────
    rhr_vals, bb_vals, stress_vals, sleep_s = [], [], [], []
    for d, dd in daily.items():
        s = (dd.get("summary") or {})
        if s.get("restingHeartRate"):
            rhr_vals.append(s["restingHeartRate"])
        if s.get("bodyBatteryHighestValue"):
            bb_vals.append(s["bodyBatteryHighestValue"])
        if s.get("averageStressLevel"):
            stress_vals.append(s["averageStressLevel"])
        sdto = (dd.get("sleep") or {}).get("dailySleepDTO") or {}
        sc = sdto.get("sleepScores", {}).get("overall", {}).get("value")
        if sc:
            sleep_s.append(sc)

    weekly_avgs = {
        "avg_rhr": round(sum(rhr_vals) / len(rhr_vals)) if rhr_vals else None,
        "avg_bb": round(sum(bb_vals) / len(bb_vals)) if bb_vals else None,
        "avg_stress": round(sum(stress_vals) / len(stress_vals)) if stress_vals else None,
        "avg_sleep_score": round(sum(sleep_s) / len(sleep_s)) if sleep_s else None,
    }

    # ── Activities this week ───────────────────────────────────────────────
    week_activities = []
    for a in activities:
        type_key = a.get("activityType", {}).get("typeKey", "")
        avg_speed = a.get("averageSpeed") or 0
        act_dict = {
            "date": a.get("startTimeLocal", "")[:10],
            "name": a.get("activityName", ""),
            "type": type_key,
            "type_label": activity_label(type_key),
            "distance_km": (a.get("distance") or 0) / 1000,
            "duration_min": (a.get("duration") or 0) / 60,
            "avg_hr": a.get("averageHR"),
            "max_hr": a.get("maxHR"),
            "vo2max": a.get("vO2MaxValue"),
            "training_load": a.get("activityTrainingLoad") or 0,
            "aerobic_effect": a.get("aerobicTrainingEffect"),
            "anaerobic_effect": a.get("anaerobicTrainingEffect"),
            "pace": fmt_pace(avg_speed),
            "avg_speed_mps": avg_speed,
            "cadence": a.get("averageRunningCadenceInStepsPerMinute"),
            "effect_label": a.get("trainingEffectLabel", ""),
            "aerobic_msg": a.get("aerobicTrainingEffectMessage", ""),
            "anaerobic_msg": a.get("anaerobicTrainingEffectMessage", ""),
            "hr_z1": a.get("hrTimeInZone_1") or 0,
            "hr_z2": a.get("hrTimeInZone_2") or 0,
            "hr_z3": a.get("hrTimeInZone_3") or 0,
            "hr_z4": a.get("hrTimeInZone_4") or 0,
            "hr_z5": a.get("hrTimeInZone_5") or 0,
            "calories": a.get("calories") or 0,
            "elevation_gain": a.get("elevationGain") or 0,
        }
        # Clasificar tipo de sesión y calcular cumplimiento del plan
        session_type = classify_session(act_dict)
        act_dict["session_type"] = session_type
        act_dict["session_label"] = SESSION_TYPE_LABELS.get(session_type, ("⚪ Otro", "—"))
        act_dict["hr_compliance"] = hr_compliance(act_dict, session_type)
        act_dict["pace_compliance"] = pace_compliance(act_dict, session_type)
        week_activities.append(act_dict)

    vo2_vals = [a["vo2max"] for a in week_activities if a["vo2max"]]
    latest_vo2 = vo2_vals[0] if vo2_vals else None

    week_summary = {
        "total_activities": len(week_activities),
        "run_count": sum(1 for a in week_activities if "running" in a["type"]),
        "strength_count": sum(1 for a in week_activities if "strength" in a["type"]),
        "total_run_km": sum(a["distance_km"] for a in week_activities if "running" in a["type"]),
        "total_training_load": sum(a["training_load"] for a in week_activities),
        "total_active_min": sum(a["duration_min"] for a in week_activities),
        "latest_vo2max": latest_vo2,
        "vo2max_category": vo2max_category(latest_vo2) if latest_vo2 else "—",
    }

    return {
        "today": today_snapshot,
        "weekly_avgs": weekly_avgs,
        "activities": week_activities,
        "week_summary": week_summary,
    }


def analyze_monthly(data: dict) -> dict:
    """Extract trends from the 6-month JSON."""
    months_raw = data.get("months", {})
    months = {}

    for month_key in sorted(months_raw.keys()):
        m = months_raw[month_key]
        acts = m.get("activities", [])
        running_acts = [a for a in acts if a.get("activityType", {}).get("typeKey") in ("running", "treadmill_running")]
        strength_acts = [a for a in acts if "strength" in a.get("activityType", {}).get("typeKey", "")]

        vo2_vals = [a.get("vO2MaxValue") for a in acts if a.get("vO2MaxValue")]
        rhr_vals, sleep_scores, bb_vals, stress_vals = [], [], [], []
        total_sleep_h = []

        for d_str, dd in m.get("daily_stats", {}).items():
            s = dd.get("summary") or {}
            if s.get("restingHeartRate"):
                rhr_vals.append(s["restingHeartRate"])
            if s.get("bodyBatteryHighestValue"):
                bb_vals.append(s["bodyBatteryHighestValue"])
            if s.get("averageStressLevel"):
                stress_vals.append(s["averageStressLevel"])
            sdto = (dd.get("sleep") or {}).get("dailySleepDTO") or {}
            sc = sdto.get("sleepScores", {}).get("overall", {}).get("value")
            if sc:
                sleep_scores.append(sc)
            sleep_secs = sdto.get("sleepTimeSeconds")
            if sleep_secs:
                total_sleep_h.append(sleep_secs / 3600)

        months[month_key] = {
            "run_count": len(running_acts),
            "strength_count": len(strength_acts),
            "run_km": round(sum(a.get("distance", 0) for a in running_acts) / 1000, 1),
            "total_training_load": round(sum((a.get("activityTrainingLoad") or 0) for a in acts)),
            "avg_vo2max": round(sum(vo2_vals) / len(vo2_vals), 1) if vo2_vals else None,
            "avg_rhr": round(sum(rhr_vals) / len(rhr_vals)) if rhr_vals else None,
            "avg_sleep_score": round(sum(sleep_scores) / len(sleep_scores)) if sleep_scores else None,
            "avg_bb": round(sum(bb_vals) / len(bb_vals)) if bb_vals else None,
            "avg_stress": round(sum(stress_vals) / len(stress_vals)) if stress_vals else None,
            "avg_sleep_h": round(sum(total_sleep_h) / len(total_sleep_h), 1) if total_sleep_h else None,
        }

    # Build trend series for charts
    month_keys = sorted(months.keys())
    trends = {
        "months": month_keys,
        "vo2max": [months[k]["avg_vo2max"] for k in month_keys],
        "rhr": [months[k]["avg_rhr"] for k in month_keys],
        "sleep_score": [months[k]["avg_sleep_score"] for k in month_keys],
        "run_km": [months[k]["run_km"] for k in month_keys],
        "training_load": [months[k]["total_training_load"] for k in month_keys],
        "strength_count": [months[k]["strength_count"] for k in month_keys],
    }

    return {"months": months, "trends": trends}


def generate_recommendations(weekly: dict, monthly: dict) -> list[dict]:
    """Generate personalized training recommendations."""
    recs = []
    w = weekly
    snap = w["today"]
    ws = w["week_summary"]
    m = monthly
    months = m["months"]
    month_keys = sorted(months.keys())

    latest_vo2 = ws.get("latest_vo2max")
    if latest_vo2:
        # VO2max trend
        vo2_vals = [months[k]["avg_vo2max"] for k in month_keys if months[k]["avg_vo2max"]]
        if len(vo2_vals) >= 2 and vo2_vals[-1] > vo2_vals[-2]:
            recs.append({
                "icon": "📈",
                "category": "VO2 Máx",
                "priority": "positive",
                "title": "VO2 Máx en ascenso",
                "detail": f"Tu VO2 máx subió a {latest_vo2:.0f} ml/kg/min ({vo2max_category(latest_vo2)}). El entrenamiento por intervalos está funcionando. Continúa incorporando sesiones de alta intensidad 1-2 veces/semana.",
            })
        elif latest_vo2 < 47:
            recs.append({
                "icon": "🎯",
                "category": "VO2 Máx",
                "priority": "warning",
                "title": "Potencial de mejora en capacidad aeróbica",
                "detail": "Incluye más carreras de tempo (20-40 min al 85-90% FCmáx) y sesiones de intervalos cortos para elevar tu VO2 máx.",
            })

    # Resting HR
    rhr = snap.get("resting_hr")
    if rhr:
        if rhr <= 48:
            recs.append({
                "icon": "💚",
                "category": "Recuperación",
                "priority": "positive",
                "title": f"FC en reposo excelente ({rhr} bpm)",
                "detail": "Una FC de reposo ≤50 bpm indica muy buena condición cardiovascular. Mantén el volumen aeróbico de base.",
            })
        elif rhr >= 60:
            recs.append({
                "icon": "⚠️",
                "category": "Recuperación",
                "priority": "warning",
                "title": f"FC en reposo elevada ({rhr} bpm)",
                "detail": "FC en reposo alta puede indicar fatiga acumulada, estrés o inicio de enfermedad. Considera descanso activo o día de recuperación.",
            })

    # Sleep
    sleep_score = snap.get("sleep_score")
    sleep_h = snap.get("sleep_total_h", 0)
    if sleep_score and sleep_score < 70:
        recs.append({
            "icon": "😴",
            "category": "Sueño",
            "priority": "warning",
            "title": f"Calidad de sueño mejorable (puntaje {sleep_score})",
            "detail": f"Dormiste {sleep_h:.1f}h con puntaje FAIR/POOR. El sueño es cuando ocurre la adaptación al entrenamiento. Intenta dormir 7.5-8.5h y mantén horarios consistentes.",
        })
    elif sleep_h < 7:
        recs.append({
            "icon": "😴",
            "category": "Sueño",
            "priority": "warning",
            "title": f"Horas de sueño insuficientes ({sleep_h:.1f}h)",
            "detail": "Menos de 7h de sueño impacta negativamente en la recuperación muscular, la hormona de crecimiento y el rendimiento. Prioriza acostarte más temprano.",
        })

    # Body battery
    bb = snap.get("body_battery_current")
    if bb is not None and bb < 25:
        recs.append({
            "icon": "🔋",
            "category": "Energía",
            "priority": "warning",
            "title": f"Body Battery muy bajo ({bb}%)",
            "detail": "Con Body Battery <25, tu cuerpo no ha recuperado completamente. Si hoy toca entrenamiento fuerte, considera bajarlo a ritmo fácil o hacer trabajo de movilidad.",
        })

    # Strength training trend
    strength_counts = [months[k]["strength_count"] for k in month_keys[-3:] if months[k]["strength_count"] is not None]
    if strength_counts and max(strength_counts) <= 3:
        recs.append({
            "icon": "🏋️",
            "category": "Fuerza",
            "priority": "warning",
            "title": "Volumen de fuerza bajo",
            "detail": "En los últimos meses el entrenamiento de fuerza ha disminuido. Para corredores, 2-3 sesiones/semana de fuerza reducen riesgo de lesión y mejoran economía de carrera.",
        })

    # Training load
    tl = ws.get("total_training_load", 0)
    if tl > 400:
        recs.append({
            "icon": "🔥",
            "category": "Carga",
            "priority": "warning",
            "title": "Carga de entrenamiento alta esta semana",
            "detail": f"Carga acumulada de {round(tl)} esta semana. Asegúrate de incluir al menos 1-2 días de recuperación activa (caminata, movilidad, foam rolling).",
        })
    elif tl < 100 and ws["total_activities"] > 2:
        recs.append({
            "icon": "💪",
            "category": "Carga",
            "priority": "positive",
            "title": "Semana de carga moderada — buen balance",
            "detail": f"Carga de {round(tl)} esta semana. Tienes margen para agregar una sesión de calidad (intervalos o tempo) sin sobrecargarte.",
        })

    # Altitude
    alt = snap.get("altitude_m")
    if alt and alt > 2000:
        recs.append({
            "icon": "⛰️",
            "category": "Altitud",
            "priority": "info",
            "title": f"Entrenando a {round(alt):,}m de altitud",
            "detail": "A más de 2,000m el VO2 máx aparente puede ser 3-5% menor que a nivel del mar. Ajusta tus ritmos de referencia y asegura hidratación adecuada.",
        })

    # Positive overall
    run_km = ws.get("total_run_km", 0)
    if run_km >= 20:
        recs.append({
            "icon": "🏃",
            "category": "Volumen",
            "priority": "positive",
            "title": f"Buen volumen semanal de carrera ({run_km:.1f} km)",
            "detail": "Mantén consistencia. Si tu objetivo es aumentar volumen, no superes el 10% semanal para evitar lesiones por sobreuso.",
        })

    return recs


# ─────────────────────────────────────────────────────────────────────────────
# HTML GENERATOR
# ─────────────────────────────────────────────────────────────────────────────
def build_html(weekly: dict, monthly: dict, recs: list, prescription: dict = None) -> str:
    snap = weekly["today"]
    ws = weekly["week_summary"]
    wa = weekly["weekly_avgs"]
    acts = weekly["activities"]
    trends = monthly["trends"]
    months_data = monthly["months"]
    month_keys = sorted(months_data.keys())

    today = date.today().strftime("%d de %B de %Y").replace(
        "January", "enero").replace("February", "febrero").replace("March", "marzo").replace(
        "April", "abril").replace("May", "mayo").replace("June", "junio").replace(
        "July", "julio").replace("August", "agosto").replace("September", "septiembre").replace(
        "October", "octubre").replace("November", "noviembre").replace("December", "diciembre")

    latest_vo2 = ws.get("latest_vo2max")
    vo2_cat = ws.get("vo2max_category", "—")
    rhr = snap.get("resting_hr")
    bb = snap.get("body_battery_current")
    bb_start = snap.get("body_battery_start")
    sleep_score = snap.get("sleep_score")
    sleep_h = snap.get("sleep_total_h", 0)
    stress = snap.get("stress_avg")

    # ── Chart data (JSON for inline JS) ───────────────────────────────────
    def null_or(v):
        return v if v is not None else "null"

    chart_months_js = json.dumps([m[-2:] for m in month_keys])  # "09","10",...
    chart_vo2_js = "[" + ",".join(str(null_or(v)) for v in trends["vo2max"]) + "]"
    chart_rhr_js = "[" + ",".join(str(null_or(v)) for v in trends["rhr"]) + "]"
    chart_km_js = "[" + ",".join(str(null_or(v)) for v in trends["run_km"]) + "]"
    chart_load_js = "[" + ",".join(str(null_or(v)) for v in trends["training_load"]) + "]"
    chart_sleep_js = "[" + ",".join(str(null_or(v)) for v in trends["sleep_score"]) + "]"
    chart_strength_js = "[" + ",".join(str(null_or(v)) for v in trends["strength_count"]) + "]"

    # HR zones for activities (running only)
    running_acts = [a for a in acts if "running" in a["type"]]

    def rec_card(r):
        colors = {"positive": "#22c55e", "warning": "#f97316", "info": "#3b82f6"}
        bg = {"positive": "#f0fdf4", "warning": "#fff7ed", "info": "#eff6ff"}
        color = colors.get(r["priority"], "#888")
        bgcol = bg.get(r["priority"], "#f9f9f9")
        return f"""
        <div style="background:{bgcol};border-left:4px solid {color};border-radius:8px;padding:14px 18px;margin-bottom:12px;">
            <div style="font-size:18px;font-weight:700;color:{color};">{r['icon']} {r['title']}</div>
            <div style="font-size:12px;color:#888;margin-bottom:4px;">{r['category']}</div>
            <div style="font-size:14px;color:#444;line-height:1.5;">{r['detail']}</div>
        </div>"""

    def activity_row(a):
        ld_color = "#22c55e" if a["training_load"] < 80 else "#eab308" if a["training_load"] < 150 else "#ef4444"
        dist_str = f"{a['distance_km']:.1f} km" if a["distance_km"] > 0.1 else "—"
        hrc = a.get("hr_compliance", {})
        pc = a.get("pace_compliance", {})
        sl = a.get("session_label", ("⚪ Otro", "—"))
        return f"""
        <tr>
            <td style="padding:10px 8px;border-bottom:1px solid #f0f0f0;">{a['date']}</td>
            <td style="padding:10px 8px;border-bottom:1px solid #f0f0f0;">
                <div style="font-weight:600;">{sl[0]}</div>
                <div style="font-size:11px;color:#999;">{sl[1]}</div>
            </td>
            <td style="padding:10px 8px;border-bottom:1px solid #f0f0f0;">{dist_str}</td>
            <td style="padding:10px 8px;border-bottom:1px solid #f0f0f0;">{round(a['duration_min'])} min</td>
            <td style="padding:10px 8px;border-bottom:1px solid #f0f0f0;">
                <span style="font-weight:600;">{a['avg_hr'] or '—'}</span> / {a['max_hr'] or '—'} bpm<br>
                <span style="font-size:12px;color:{hrc.get('color','#aaa')};font-weight:600;">{hrc.get('label','—')}</span>
            </td>
            <td style="padding:10px 8px;border-bottom:1px solid #f0f0f0;">
                <span style="color:{pc.get('color','#888')};font-weight:600;">{pc.get('label','—')}</span>
            </td>
            <td style="padding:10px 8px;border-bottom:1px solid #f0f0f0;font-weight:700;color:{ld_color};">{round(a['training_load'])}</td>
            <td style="padding:10px 8px;border-bottom:1px solid #f0f0f0;">{a['aerobic_effect'] or '—'} / {a['anaerobic_effect'] or '—'}</td>
        </tr>"""

    def month_row(k):
        m = months_data[k]
        vo2_color = "#22c55e" if (m["avg_vo2max"] or 0) >= 50 else "#eab308" if (m["avg_vo2max"] or 0) >= 45 else "#ef4444"
        rhr_color = "#22c55e" if (m["avg_rhr"] or 99) <= 50 else "#eab308" if (m["avg_rhr"] or 99) <= 60 else "#ef4444"
        sleep_color = color_for_score(m["avg_sleep_score"] or 0)
        return f"""
        <tr>
            <td style="padding:9px 8px;border-bottom:1px solid #f0f0f0;font-weight:600;">{k}</td>
            <td style="padding:9px 8px;border-bottom:1px solid #f0f0f0;">{m['run_count']}</td>
            <td style="padding:9px 8px;border-bottom:1px solid #f0f0f0;">{m['run_km']} km</td>
            <td style="padding:9px 8px;border-bottom:1px solid #f0f0f0;">{m['strength_count']}</td>
            <td style="padding:9px 8px;border-bottom:1px solid #f0f0f0;">{m['total_training_load']}</td>
            <td style="padding:9px 8px;border-bottom:1px solid #f0f0f0;color:{vo2_color};font-weight:700;">{m['avg_vo2max'] or '—'}</td>
            <td style="padding:9px 8px;border-bottom:1px solid #f0f0f0;color:{rhr_color};font-weight:700;">{m['avg_rhr'] or '—'}</td>
            <td style="padding:9px 8px;border-bottom:1px solid #f0f0f0;color:{sleep_color};font-weight:700;">{m['avg_sleep_score'] or '—'}</td>
        </tr>"""

    recs_html = "".join(rec_card(r) for r in recs) if recs else "<p style='color:#888'>Sin recomendaciones activas.</p>"
    activities_html = "".join(activity_row(a) for a in acts)
    months_html = "".join(month_row(k) for k in month_keys)

    # ── Prescripción del día ───────────────────────────────────────────────
    if prescription:
        p = prescription
        status_colors = {"green": "#22c55e", "yellow": "#eab308", "red": "#ef4444"}
        status_bgs    = {"green": "#f0fdf4", "yellow": "#fefce8", "red": "#fff1f2"}
        pcol = status_colors.get(p["status"], "#888")
        pbg  = status_bgs.get(p["status"], "#f9f9f9")

        reasons_html = ""
        if p["fatigue_reasons"]:
            reasons_html += "<div style='margin-top:10px;'><span style='font-size:12px;font-weight:700;color:#ef4444;'>⚠️ Señales de fatiga:</span><ul style='margin:4px 0 0 16px;font-size:13px;color:#555;line-height:1.8;'>" + \
                "".join(f"<li>{r}</li>" for r in p["fatigue_reasons"]) + "</ul></div>"
        if p["recovery_reasons"]:
            reasons_html += "<div style='margin-top:8px;'><span style='font-size:12px;font-weight:700;color:#22c55e;'>✅ Señales positivas:</span><ul style='margin:4px 0 0 16px;font-size:13px;color:#555;line-height:1.8;'>" + \
                "".join(f"<li>{r}</li>" for r in p["recovery_reasons"]) + "</ul></div>"

        # Barra de volumen semanal
        km_done = p["current_week_km"]
        km_base = PLAN["weekly_km_base"]
        km_peak = PLAN["weekly_km_peak"]
        km_max  = PLAN["weekly_km_max"]
        bar_pct_done = min(100, km_done / km_max * 100)
        bar_pct_base = km_base / km_max * 100
        bar_pct_peak = km_peak / km_max * 100
        bar_color = "#22c55e" if km_done >= km_peak else "#3b82f6" if km_done >= km_base else "#eab308"

        vol_bar_html = f"""
        <div style="margin-top:14px;">
          <div style="display:flex;justify-content:space-between;font-size:12px;color:#888;margin-bottom:4px;">
            <span>0 km</span>
            <span>{km_base} km (base)</span>
            <span>{km_peak} km (pico)</span>
            <span>{km_max} km (máx)</span>
          </div>
          <div style="position:relative;height:18px;background:#e5e7eb;border-radius:9px;overflow:hidden;">
            <div style="position:absolute;left:0;top:0;height:100%;width:{bar_pct_done:.1f}%;background:{bar_color};border-radius:9px;transition:width 0.3s;"></div>
            <div style="position:absolute;left:{bar_pct_base:.1f}%;top:0;height:100%;width:2px;background:#6b7280;opacity:0.5;"></div>
            <div style="position:absolute;left:{bar_pct_peak:.1f}%;top:0;height:100%;width:2px;background:#6b7280;opacity:0.5;"></div>
          </div>
          <div style="font-size:13px;color:#555;margin-top:6px;"><strong>{km_done:.1f} km</strong> acumulados esta semana. {p['vol_message']}</div>
        </div>"""

        prescription_html = f"""
  <div class="section" style="border-left:5px solid {pcol};background:{pbg};">
    <h2>📋 Prescripción del Día · Plan Maratón (objetivo {PLAN['target_pace_str']})</h2>
    <div style="display:flex;gap:20px;flex-wrap:wrap;align-items:flex-start;">
      <div style="flex:2;min-width:260px;">
        <div style="font-size:22px;font-weight:800;color:{pcol};margin-bottom:6px;">{p['headline']}</div>
        <div style="font-size:14px;color:#444;line-height:1.6;">{p['message']}</div>
        {reasons_html}
      </div>
      <div style="flex:1;min-width:200px;background:#fff;border-radius:10px;padding:14px 16px;box-shadow:0 1px 4px #0001;">
        <div style="font-size:12px;font-weight:700;color:#888;margin-bottom:8px;">OBJETIVOS DEL PLAN</div>
        <div style="font-size:13px;color:#444;line-height:2;">
          🎯 Ritmo maratón: <strong>{PLAN['target_pace_str']}</strong><br>
          🟢 Trote suave:    <strong>&lt;{PLAN['hr_easy_max']} bpm</strong><br>
          🟡 Fondo largo:    <strong>{PLAN['hr_long_min']}–{PLAN['hr_long_max']} bpm</strong><br>
          🔴 Intervalos:     <strong>≥{PLAN['hr_interval_min']} bpm</strong><br>
          📏 Pico semanal:   <strong>{PLAN['weekly_km_peak']}–{PLAN['weekly_km_max']} km</strong>
        </div>
      </div>
    </div>
    {vol_bar_html}
  </div>"""
    else:
        prescription_html = ""

    bb_color = color_for_score(bb or 0)
    sleep_color = color_for_score(sleep_score or 0)
    stress_color = color_for_score(stress or 0, good_high=False)
    vo2_color = "#22c55e" if (latest_vo2 or 0) >= 50 else "#eab308" if (latest_vo2 or 0) >= 45 else "#ef4444"
    rhr_color = "#22c55e" if (rhr or 99) <= 50 else "#eab308" if (rhr or 99) <= 60 else "#ef4444"

    def stat_card(icon, label, value, unit, color, sub=""):
        return f"""
        <div style="background:#fff;border-radius:12px;padding:18px 20px;box-shadow:0 2px 8px #0001;min-width:140px;flex:1;">
            <div style="font-size:22px;">{icon}</div>
            <div style="font-size:28px;font-weight:800;color:{color};">{value}<span style="font-size:14px;font-weight:400;color:#999;"> {unit}</span></div>
            <div style="font-size:13px;color:#666;margin-top:2px;">{label}</div>
            {f'<div style="font-size:12px;color:#aaa;margin-top:2px;">{sub}</div>' if sub else ''}
        </div>"""

    vo2_card = stat_card("🫀", "VO2 Máx", f"{latest_vo2:.0f}" if latest_vo2 else "—", "ml/kg/min", vo2_color, vo2_cat)
    rhr_card = stat_card("❤️", "FC en reposo", rhr or "—", "bpm", rhr_color, "7d avg: " + str(wa.get("avg_rhr", "—")))
    bb_card = stat_card("🔋", "Body Battery", bb or "—", "%", bb_color, f"Amaneció en {bb_start or '—'}%")
    sleep_card = stat_card("😴", "Sueño anoche", f"{sleep_h:.1f}", "h", sleep_color, f"Score: {sleep_score or '—'}")
    stress_card = stat_card("🧠", "Estrés promedio", stress or "—", "", stress_color, "0=bajo 100=alto")

    # Sleep distribution bar
    deep_pct = snap.get("deep_pct") or 0
    rem_pct = snap.get("rem_pct") or 0
    light_pct = snap.get("light_pct") or 0
    awake_pct = 100 - deep_pct - rem_pct - light_pct
    awake_pct = max(0, awake_pct)

    sleep_bar = f"""
    <div style="display:flex;border-radius:8px;overflow:hidden;height:20px;width:100%;margin:10px 0;">
        <div style="width:{deep_pct}%;background:#6366f1;title='Sueño profundo'"></div>
        <div style="width:{rem_pct}%;background:#8b5cf6;"></div>
        <div style="width:{light_pct}%;background:#a78bfa;"></div>
        <div style="width:{awake_pct:.0f}%;background:#e5e7eb;"></div>
    </div>
    <div style="display:flex;gap:16px;font-size:12px;color:#666;flex-wrap:wrap;">
        <span>🟦 Profundo {deep_pct}%</span>
        <span>🟪 REM {rem_pct}%</span>
        <span>💜 Ligero {light_pct}%</span>
        <span>⬜ Despierto {awake_pct:.0f}%</span>
    </div>"""

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Análisis Físico — {today}</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f4f6f9; color: #222; }}
  .container {{ max-width: 1100px; margin: 0 auto; padding: 24px 16px; }}
  h2 {{ font-size: 20px; font-weight: 700; color: #1a1a2e; margin-bottom: 16px; border-bottom: 2px solid #e0e0e0; padding-bottom: 8px; }}
  h3 {{ font-size: 16px; font-weight: 600; color: #333; margin-bottom: 10px; }}
  .section {{ background: #fff; border-radius: 14px; padding: 24px; margin-bottom: 24px; box-shadow: 0 2px 10px #0001; }}
  .stat-row {{ display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 0; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
  th {{ background: #f8f9fa; font-weight: 600; text-align: left; padding: 10px 8px; color: #555; font-size: 13px; border-bottom: 2px solid #e0e0e0; }}
  .header {{ background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%); color: white; padding: 28px 24px; border-radius: 14px; margin-bottom: 24px; }}
  .header h1 {{ font-size: 26px; font-weight: 800; }}
  .header p {{ opacity: 0.75; margin-top: 4px; font-size: 14px; }}
  .chart-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
  @media (max-width: 700px) {{ .chart-grid {{ grid-template-columns: 1fr; }} }}
  .chart-box {{ background: #fff; border-radius: 12px; padding: 16px 18px; box-shadow: 0 2px 8px #0001; }}
  .chart-wrap {{ position: relative; height: 160px; width: 100%; }}
  .tag {{ display: inline-block; border-radius: 20px; padding: 2px 10px; font-size: 12px; font-weight: 600; }}
</style>
</head>
<body>
<div class="container">

  <!-- HEADER -->
  <div class="header">
    <h1>📊 Análisis de Condición Física</h1>
    <p>Jorvan · {today} · Altitud de entrenamiento: ~{round(snap.get("altitude_m") or 0):,}m</p>
  </div>

  <!-- SNAPSHOT DE HOY -->
  <div class="section">
    <h2>🌅 Snapshot de Hoy</h2>
    <div class="stat-row">
      {vo2_card}
      {rhr_card}
      {bb_card}
      {sleep_card}
      {stress_card}
    </div>
  </div>

  <!-- PRESCRIPCIÓN DEL DÍA -->
  {prescription_html}

  <!-- SUEÑO DETALLE -->
  <div class="section">
    <h2>😴 Calidad de Sueño — Anoche</h2>
    <div style="display:flex;gap:24px;flex-wrap:wrap;align-items:flex-start;">
      <div style="flex:2;min-width:260px;">
        <p style="font-size:14px;color:#555;margin-bottom:12px;">
          Dormiste <strong>{sleep_h:.1f}h</strong> con un puntaje de <strong style="color:{sleep_color};">{sleep_score or '—'} ({sleep_qualifier(snap.get('sleep_score_key',''))})</strong>.
          Tiempo despierto: <strong>{round(snap.get('awake_min',0))} min</strong>. Estrés durante el sueño: <strong>{snap.get('sleep_stress','—')}</strong>.
        </p>
        {sleep_bar}
      </div>
      <div style="flex:1;min-width:200px;background:#f8f9fa;border-radius:10px;padding:16px;">
        <h3>Óptimo adulto activo</h3>
        <ul style="font-size:13px;color:#555;padding-left:16px;line-height:2;">
          <li>Profundo: 16–33%</li>
          <li>REM: 21–31%</li>
          <li>Total: 7.5–8.5h</li>
        </ul>
      </div>
    </div>
  </div>

  <!-- ACTIVIDADES ESTA SEMANA -->
  <div class="section">
    <h2>🏃 Actividades — Últimos 7 Días</h2>
    <div class="stat-row" style="margin-bottom:20px;">
      <div style="background:#f0fdf4;border-radius:10px;padding:14px 20px;flex:1;min-width:130px;">
        <div style="font-size:24px;font-weight:800;color:#22c55e;">{ws['run_count']}</div>
        <div style="font-size:13px;color:#666;">Carreras</div>
      </div>
      <div style="background:#fff7ed;border-radius:10px;padding:14px 20px;flex:1;min-width:130px;">
        <div style="font-size:24px;font-weight:800;color:#f97316;">{ws['strength_count']}</div>
        <div style="font-size:13px;color:#666;">Sesiones de fuerza</div>
      </div>
      <div style="background:#eff6ff;border-radius:10px;padding:14px 20px;flex:1;min-width:130px;">
        <div style="font-size:24px;font-weight:800;color:#3b82f6;">{ws['total_run_km']:.1f}</div>
        <div style="font-size:13px;color:#666;">km totales carrera</div>
      </div>
      <div style="background:#fdf4ff;border-radius:10px;padding:14px 20px;flex:1;min-width:130px;">
        <div style="font-size:24px;font-weight:800;color:#a855f7;">{round(ws['total_training_load'])}</div>
        <div style="font-size:13px;color:#666;">Carga total</div>
      </div>
    </div>
    <div style="overflow-x:auto;">
    <table>
      <thead><tr>
        <th>Fecha</th><th>Tipo</th><th>Distancia</th><th>Duración</th>
        <th>FC media/máx</th><th>Ritmo</th><th>Carga</th><th>Efecto A/An</th>
      </tr></thead>
      <tbody>{activities_html}</tbody>
    </table>
    </div>
  </div>

  <!-- RECOMENDACIONES -->
  <div class="section">
    <h2>🎯 Recomendaciones Personalizadas</h2>
    {recs_html}
  </div>

  <!-- TENDENCIAS HISTÓRICAS (charts) -->
  <div class="section">
    <h2>📈 Tendencias Históricas (6 meses)</h2>
    <div class="chart-grid">
      <div class="chart-box">
        <h3>VO2 Máx</h3>
        <div class="chart-wrap"><canvas id="chartVo2"></canvas></div>
      </div>
      <div class="chart-box">
        <h3>FC en Reposo</h3>
        <div class="chart-wrap"><canvas id="chartRhr"></canvas></div>
      </div>
      <div class="chart-box">
        <h3>Kilómetros de Carrera / Mes</h3>
        <div class="chart-wrap"><canvas id="chartKm"></canvas></div>
      </div>
      <div class="chart-box">
        <h3>Carga de Entrenamiento / Mes</h3>
        <div class="chart-wrap"><canvas id="chartLoad"></canvas></div>
      </div>
      <div class="chart-box">
        <h3>Puntaje de Sueño Promedio</h3>
        <div class="chart-wrap"><canvas id="chartSleep"></canvas></div>
      </div>
      <div class="chart-box">
        <h3>Sesiones de Fuerza / Mes</h3>
        <div class="chart-wrap"><canvas id="chartStrength"></canvas></div>
      </div>
    </div>
  </div>

  <!-- TABLA MENSUAL -->
  <div class="section">
    <h2>📅 Resumen Mensual</h2>
    <div style="overflow-x:auto;">
    <table>
      <thead><tr>
        <th>Mes</th><th>Carreras</th><th>Km</th><th>Fuerza</th>
        <th>Carga total</th><th>VO2 Máx</th><th>FC reposo</th><th>Sueño</th>
      </tr></thead>
      <tbody>{months_html}</tbody>
    </table>
    </div>
  </div>

  <p style="text-align:center;color:#bbb;font-size:12px;padding:12px 0;">
    Generado automáticamente · Datos de Garmin Connect · {date.today().isoformat()}
  </p>
</div>

<script>
const months = {chart_months_js};
const baseOpts = {{
  responsive:true,
  maintainAspectRatio:false,
  plugins:{{legend:{{display:false}}}},
  scales:{{
    x:{{grid:{{display:false}}, ticks:{{font:{{size:11}}}}}},
    y:{{grid:{{color:'#f0f0f0'}}, ticks:{{font:{{size:11}}}}}}
  }}
}};

new Chart(document.getElementById('chartVo2'), {{
  type:'line', data:{{
    labels:months,
    datasets:[{{data:{chart_vo2_js}, borderColor:'#6366f1', backgroundColor:'#6366f120', fill:true, tension:0.4, pointRadius:5}}]
  }}, options:{{...baseOpts, scales:{{...baseOpts.scales, y:{{...baseOpts.scales.y, min:44, max:55}}}}}}
}});

new Chart(document.getElementById('chartRhr'), {{
  type:'line', data:{{
    labels:months,
    datasets:[{{data:{chart_rhr_js}, borderColor:'#ef4444', backgroundColor:'#ef444420', fill:true, tension:0.4, pointRadius:5}}]
  }}, options:baseOpts
}});

new Chart(document.getElementById('chartKm'), {{
  type:'bar', data:{{
    labels:months,
    datasets:[{{data:{chart_km_js}, backgroundColor:'#3b82f680', borderColor:'#3b82f6', borderRadius:6}}]
  }}, options:baseOpts
}});

new Chart(document.getElementById('chartLoad'), {{
  type:'bar', data:{{
    labels:months,
    datasets:[{{data:{chart_load_js}, backgroundColor:'#f9731680', borderColor:'#f97316', borderRadius:6}}]
  }}, options:baseOpts
}});

new Chart(document.getElementById('chartSleep'), {{
  type:'line', data:{{
    labels:months,
    datasets:[{{data:{chart_sleep_js}, borderColor:'#8b5cf6', backgroundColor:'#8b5cf620', fill:true, tension:0.4, pointRadius:5}}]
  }}, options:{{...baseOpts, scales:{{...baseOpts.scales, y:{{...baseOpts.scales.y, min:60, max:90}}}}}}
}});

new Chart(document.getElementById('chartStrength'), {{
  type:'bar', data:{{
    labels:months,
    datasets:[{{data:{chart_strength_js}, backgroundColor:'#22c55e80', borderColor:'#22c55e', borderRadius:6}}]
  }}, options:baseOpts
}});
</script>
</body>
</html>"""

    return html


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    print("🔄 Cargando datos de entrenamiento...")

    weekly_data = load_json(WEEKLY_FILE)
    monthly_data = load_json(MONTHLY_FILE)

    if not weekly_data or not monthly_data:
        print("❌ No se encontraron los archivos de datos. Asegúrate de haber ejecutado export_data.py primero.")
        sys.exit(1)

    print("📊 Analizando datos semanales...")
    weekly = analyze_weekly(weekly_data)

    print("📈 Analizando tendencias mensuales...")
    monthly = analyze_monthly(monthly_data)

    print("🎯 Generando recomendaciones...")
    recs = generate_recommendations(weekly, monthly)

    print("📋 Calculando prescripción del día...")
    prescription = daily_prescription(weekly, monthly)

    print("🖥️  Construyendo reporte HTML...")
    html = build_html(weekly, monthly, recs, prescription)

    OUTPUT_FILE.write_text(html, encoding="utf-8")
    print(f"✅ Reporte guardado en: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
