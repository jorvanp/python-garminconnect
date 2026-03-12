import os
import logging
import google.generativeai as genai

logger = logging.getLogger(__name__)

def generate_daily_recommendation(dashboard_data):
    """
    Takes the processed stats from helpers.py and asks Gemini to generate
    an HTML-formatted daily recommendation block.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.warning("No GEMINI_API_KEY found. Skipping AI recommendation generation.")
        return None
        
    try:
        logger.info("Connecting to Gemini API to generate daily insights...")
        genai.configure(api_key=api_key)
        # We use a fast text model
        model = genai.GenerativeModel('gemini-2.5-flash')
        
        # Extract variables from dashboard_data dictionary securely
        vo2_max = dashboard_data.get('vo2_max', '—')
        rhr = dashboard_data.get('rhr', '—')
        body_battery = dashboard_data.get('body_battery', '—')
        sleep_score = dashboard_data.get('sleep_score', '—')
        sleep_hours = dashboard_data.get('sleep_hours', '—')
        stress_score = dashboard_data.get('stress_score', '—')
        total_run_distance = dashboard_data.get('total_run_distance', '—')
        weekly_remaining = dashboard_data.get('weekly_remaining', '—')
        runs_count = dashboard_data.get('runs_count', '—')

        # Build a concise recent activities summary (last 5)
        recent_activities = dashboard_data.get('recent_activities', [])
        activity_lines = []
        for act in recent_activities[:5]:
            name = act.get('activityName', act.get('activityType', {}).get('typeKey', 'Actividad'))
            dist = act.get('_dist_km', '—')
            pace = act.get('_pace', '—')
            date_str = act.get('_date', '')
            activity_lines.append(f"  • {date_str} — {name}: {dist} km @ {pace}")
        activities_text = "\n".join(activity_lines) if activity_lines else "  (sin actividades recientes)"

        prompt = f"""
Eres un coach de atletismo experto (especialidad: Media Maratón,  ritmo objetivo 4:30 minutos el km). 
A continuación te comparto las métricas de salud de tu atleta (Jorvan) tras despertar el día de hoy:
- VO2 Máx: {vo2_max} ml/kg/min
- Frecuencia cardíaca en reposo: {rhr} bpm
- Body Battery actual: {body_battery}%
- Score de sueño de anoche: {sleep_score}/100 
- Horas dormidas: {sleep_hours}h
- Estrés promedio de ayer: {stress_score}/100
- Kilómetros acumulados esta semana: {total_run_distance} km (faltan ~{weekly_remaining} km para 80 km pico)
- Carreras esta semana: {runs_count}

Actividades recientes (últimas sesiones):
{activities_text}

Genera la "Prescripción del Día" para el atleta. Si su Body Battery y sueño están bajos, o su RHR es muy alta, recomiéndale recuperar (Día de recuperación recomendado) con trote suave/descanso. Si sus métricas son óptimas, recomiéndale un entrenamiento fuerte (Día de intensidad / calidad).

Debes responder ÚNICAMENTE con un código HTML usando esta estructura exacta (no agregues ```html):

<div style="font-size:22px;font-weight:800;color:COLOR_HEX;margin-bottom:6px;">EMOJI TÍTULO DEL DÍA</div>
<div style="font-size:14px;color:#444;line-height:1.6;">ANÁLISIS_EN_UN_PÁRRAFO_SOBRE_SU_FATIGA_Y_RECOMENDACIÓN_DEL_ENTRENAMIENTO</div>
<div style='margin-top:10px;'><span style='font-size:12px;font-weight:700;color:#ef4444;'>⚠️ Señales de atención:</span><ul style='margin:4px 0 0 16px;font-size:13px;color:#555;line-height:1.8;'><li>[Métrica alarmante 1]</li><li>[Métrica alarmante 2]</li>...</ul></div>
<div style='margin-top:8px;'><span style='font-size:12px;font-weight:700;color:#22c55e;'>✅ Señales positivas:</span><ul style='margin:4px 0 0 16px;font-size:13px;color:#555;line-height:1.8;'><li>[Métrica positiva 1]</li><li>[Métrica positiva 2]</li>...</ul></div>

Instrucciones:
- Reemplaza COLOR_HEX y EMOJI por: 🔴 #ef4444 (si requiere recuperación urgente), 🟡 #eab308 (si requiere precaución/fondo suave), o 🟢 #22c55e (si está listo para intervalos/máximo esfuerzo).
- Completa las viñetas clasificando estrictamente los datos de salud reales de hoy que te proveí.
- Si no hay señales alarmantes, puedes quitar la lista de advertencias (o viceversa).
- Mantén el texto directo, profesional y conciso de Coach de Running.

"""
        response = model.generate_content(prompt)
        # Clean the response just in case the AI wraps it in markdown blocks
        html_fragment = response.text.replace('```html', '').replace('```', '').strip()
        logger.info("AI recommendation successfully generated.")
        return html_fragment
    except Exception as e:
        logger.error(f"Error calling Gemini API: {e}")
        return None
