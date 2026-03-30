import os
import logging
from datetime import timedelta
from google import genai

from tz_utils import today_tz

logger = logging.getLogger(__name__)

def _get_client():
    """Returns a configured Gemini client or None if no API key is set."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    return genai.Client(api_key=api_key)


def extract_training_plan_from_image(image_bytes: bytes, mime_type: str, user_name: str = "Atleta") -> dict:
    """
    Uses Gemini vision to extract a structured training plan from an image.
    Returns {"text": str, "summary": str} or {"error": str}
    """
    client = _get_client()
    if not client:
        return {"error": "No GEMINI_API_KEY configurada."}
    try:
        from google.genai import types as genai_types
        prompt = f"""Analiza esta imagen de un plan de entrenamiento deportivo y extrae toda la información visible.

Devuelve la información organizada así:
1. **Tipo de plan y duración total** (ej: Plan Maratón 16 semanas)
2. **Estructura semanal** — describe cada día de la semana con el entrenamiento asignado
3. **Semana actual** — si hay fechas, indica en qué semana estaría el atleta hoy ({today_tz(user_tz).isoformat()})
4. **Intensidades y ritmos** — zonas de FC, ritmos objetivo, tipo de carrera (fácil, tempo, long run, intervalos)
5. **Notas o progresiones** — cualquier instrucción especial visible

Si ves texto en la imagen, transcríbelo fielmente. Si la imagen no es un plan de entrenamiento deportivo, indícalo claramente."""

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=genai_types.Content(parts=[
                genai_types.Part(inline_data=genai_types.Blob(mime_type=mime_type, data=image_bytes)),
                genai_types.Part(text=prompt),
            ])
        )
        plan_text = response.text.strip()

        # Short summary for sidebar display
        summary_resp = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=f"Resume en máximo 1 línea (20 palabras) este plan de entrenamiento:\n{plan_text[:800]}"
        )
        summary = summary_resp.text.strip()[:200]

        return {"text": plan_text, "summary": summary}
    except Exception as e:
        logger.error(f"Error extracting training plan from image: {e}")
        return {"error": str(e)}


_FITNESS_ONLY_RULE = """

=== RESTRICCIÓN DE DOMINIO (OBLIGATORIA) ===
Sento SOLO puede responder preguntas relacionadas con: entrenamiento deportivo, fitness, nutrición deportiva, recuperación/descanso, fisiología del ejercicio y competencias deportivas.
Si el usuario pregunta sobre cualquier otro tema (programación, idiomas, historia, política, recetas no deportivas, entretenimiento, etc.), debes declinar educadamente con una respuesta breve como: "Solo puedo ayudarte con temas de fitness y entrenamiento deportivo. ¿Tienes alguna pregunta sobre tu entrenamiento?"
"""


def _format_rules(coaching_rules: list | None) -> str:
    """Formats active coaching rules as a prompt section."""
    if not coaching_rules:
        return ""
    active = [r for r in coaching_rules if r.get('active', True)]
    if not active:
        return ""
    lines = "\n".join(f"  • {r['text']}" for r in active)
    return f"\n\n=== INSTRUCCIONES DE OVERRIDE (OBLIGATORIAS — aplican sobre cualquier dato) ===\n{lines}\nEstas instrucciones tienen prioridad absoluta sobre cualquier interpretación automática de los datos.\n"


def _format_training_plan(training_plan: dict | None) -> str:
    """Formats stored training plan as a prompt section."""
    if not training_plan or not training_plan.get('text'):
        return ""
    plan_text = training_plan['text'][:2500]
    return f"\n=== PLAN DE ENTRENAMIENTO PROGRAMADO DEL ATLETA ===\n{plan_text}\nHoy es {today_tz(user_tz).isoformat()}. Cuando sea relevante, identifica qué entrenamiento corresponde hoy según el plan.\n"


def generate_daily_recommendation(dashboard_data, user_name="Atleta", training_goal=None, coaching_rules=None, training_plan=None, user_tz=None, raw_data=None, weekly_summaries=None):
    """
    Takes the processed stats from helpers.py and asks Gemini to generate
    an HTML-formatted daily recommendation block.
    raw_data: full GCS JSON (used to build 6-week activity detail).
    weekly_summaries: dict from Firestore (used for 4-month context).
    """
    client = _get_client()
    if not client:
        logger.warning("No GEMINI_API_KEY found. Skipping AI recommendation generation.")
        return None

    try:
        logger.info("Connecting to Gemini API to generate daily insights...")

        vo2_max = dashboard_data.get('vo2_max', '—')
        rhr = dashboard_data.get('rhr', '—')
        stress_score = dashboard_data.get('stress_score', '—')
        total_run_distance = dashboard_data.get('total_run_distance', '—')
        weekly_remaining = dashboard_data.get('weekly_remaining', '—')
        runs_count = dashboard_data.get('runs_count', '—')

        # Build 6-week detailed activity list from raw_data when available
        six_weeks_ago = (today_tz(user_tz) - timedelta(days=42)).isoformat()[:10]
        acts_6w = []
        if raw_data:
            for m_data in raw_data.get("months", {}).values():
                for a in m_data.get("activities", []):
                    if a.get("startTimeLocal", "")[:10] >= six_weeks_ago:
                        acts_6w.append(a)
            acts_6w.sort(key=lambda x: x.get("startTimeLocal", ""), reverse=True)

        activity_lines = []
        if acts_6w:
            for act in acts_6w:
                name = act.get('activityName', act.get('activityType', {}).get('typeKey', 'Actividad'))
                dist = round(act.get('distance', 0) / 1000, 1) if act.get('distance') else '—'
                speed = act.get('averageSpeed', 0)
                pace_s = f"{int(1000/speed//60)}:{int(1000/speed%60):02d} /km" if speed and speed > 0 else '—'
                hr = act.get('averageHR', '')
                hr_str = f", FC {hr} bpm" if hr else ''
                load = act.get('activityTrainingLoad')
                load_str = f", carga {round(load)}" if load else ''
                stride_cm = act.get('avgStrideLength')
                stride_str = f", zancada {round(stride_cm)} cm" if stride_cm and 60 < stride_cm < 230 else ''
                balance = act.get('avgGroundContactBalance')
                bal_str = (f", pisada {round(balance,1)}%izq/{round(100-balance,1)}%der"
                           if balance and 40 < balance < 60 else '')
                d_s = act.get('startTimeLocal', '')[:10]
                activity_lines.append(f"  • {d_s} — {name}: {dist} km @ {pace_s}{hr_str}{load_str}{stride_str}{bal_str}")
        else:
            # Fallback: use pre-enriched recent_activities from dashboard_data
            for act in dashboard_data.get('recent_activities', [])[:7]:
                name = act.get('activityName', act.get('activityType', {}).get('typeKey', 'Actividad'))
                dist = act.get('_dist_km', '—')
                pace = act.get('_pace', '—')
                date_str = act.get('_date', '')
                load = act.get('_load', '—')
                stride = act.get('_stride', '—')
                balance = act.get('_balance', '—')
                extras = ""
                if load != "—":
                    extras += f", carga {load}"
                if stride != "—":
                    extras += f", zancada {stride}"
                if balance != "—":
                    extras += f", pisada {balance}"
                activity_lines.append(f"  • {date_str} — {name}: {dist} km @ {pace}{extras}")
        activities_text = "\n".join(activity_lines) if activity_lines else "  (sin actividades recientes)"

        # 4-month weekly summary context
        from weekly_summarizer import format_weekly_summaries_for_ai
        weekly_text = format_weekly_summaries_for_ai(weekly_summaries or {}, num_weeks=17)
        weekly_section = f"\nResúmenes semanales (últimos 4 meses):\n{weekly_text}\n" if weekly_summaries else ""

        goal = training_goal or dashboard_data.get('training_goal') or {}
        goal_configured = bool(dashboard_data.get('goal_configured')) or bool(
            training_goal and training_goal.get('target_pace_str'))
        race_type = goal.get('race_type', '')
        target_pace = goal.get('target_pace_str', '')
        weekly_peak_km = goal.get('weekly_peak_km')
        goal_desc = goal.get('description', '')
        injuries = goal.get('injuries', '')
        avail_days = goal.get('availability_days', '')
        avail_hours = goal.get('availability_hours_week', '')

        if goal_configured:
            goal_context = f"Objetivo: {race_type} · Ritmo meta: {target_pace} min/km · Pico semanal: {weekly_peak_km} km"
            if goal_desc:
                goal_context += f" · {goal_desc}"
            specialist_intro = f"especialidad: {race_type}, ritmo objetivo {target_pace} minutos el km"
        else:
            goal_context = ("Sin objetivo de carrera configurado. "
                            "Basa la recomendación en las métricas fisiológicas y el rendimiento real de las actividades recientes.")
            specialist_intro = "análisis de métricas fisiológicas y rendimiento deportivo"

        # Athlete profile (injuries + availability)
        profile_lines = []
        if injuries:
            profile_lines.append(f"- Lesiones/condiciones: {injuries}")
        if avail_days:
            profile_lines.append(f"- Disponibilidad: {avail_days} días/semana")
        if avail_hours:
            profile_lines.append(f"- Horas disponibles/semana: {avail_hours}h")
        athlete_profile = ("\n=== PERFIL DEL ATLETA ===\n" + "\n".join(profile_lines) + "\n") if profile_lines else ""

        rules_section = _format_rules(coaching_rules)

        plan_section = ""
        if training_plan and training_plan.get('text'):
            plan_text = training_plan['text'][:2500]
            today_label = today_tz(user_tz).isoformat()
            weekday_label = today_tz(user_tz).strftime('%A')
            plan_section = f"""
=== PLAN DE ENTRENAMIENTO PROGRAMADO ===
{plan_text}

Hoy es {today_label} ({weekday_label}). Con base en el plan y las actividades recientes del atleta:

1. **Entrenamiento de hoy**: Identifica qué sesión corresponde hoy según el plan e inclúyela explícitamente como "📅 Entrenamiento del plan de hoy:".
2. **Sesiones saltadas**: Compara las actividades recientes con el plan. Si detectas que el atleta omitió una sesión programada en los últimos 7 días, menciónala.
3. **Recomendación de recuperación de sesión**: Si existe una sesión saltada, evalúa si recuperarla hoy o mañana causaría dos sesiones de alta intensidad en días consecutivos. Si no hay riesgo de acumulación de carga excesiva, recomienda retomar esa sesión indicando cuándo y cómo. Si sí habría dos días duros seguidos, recomienda dejarla ir y mantener el plan desde hoy.
"""

        data_context = (
            f"CONTEXTO DE DATOS: Para esta prescripción tengo acceso a las actividades detalladas de las últimas 6 semanas "
            f"({six_weeks_ago} a hoy) y resúmenes semanales de los últimos 4 meses. "
            f"Si detectas que falta información clave para dar una recomendación precisa, menciónalo brevemente al final."
        )

        prompt = f"""
Eres Sento, un sistema experto en {specialist_intro}.
A continuación te comparto las métricas fisiológicas de {user_name} para el día de hoy:
{goal_context}
- VO2 Máx: {vo2_max} ml/kg/min
- Frecuencia cardíaca en reposo: {rhr} bpm
- Estrés promedio de ayer: {stress_score}/100
- Kilómetros acumulados esta semana: {total_run_distance} km (faltan ~{weekly_remaining} km para el pico de {weekly_peak_km} km)
- Carreras esta semana: {runs_count}
{athlete_profile}
Actividades de las últimas 6 semanas (detalle completo):
{activities_text}
{weekly_section}
{data_context}

Genera la "Prescripción del Día" para el atleta. Basa el nivel de intensidad recomendado en la RHR, el estrés acumulado y la carga de entrenamientos recientes. Si la RHR está elevada o la carga reciente es alta, recomienda recuperación. Si las métricas son favorables, recomienda intensidad.

Debes responder ÚNICAMENTE con un código HTML usando esta estructura exacta (no agregues ```html):

<div style="font-size:22px;font-weight:800;color:COLOR_HEX;margin-bottom:6px;">EMOJI TÍTULO DEL DÍA</div>
<div style="font-size:14px;color:#444;line-height:1.6;">ANÁLISIS_EN_UN_PÁRRAFO_SOBRE_CARGA_ACUMULADA_Y_RECOMENDACIÓN_DEL_ENTRENAMIENTO</div>
<div style='margin-top:10px;'><span style='font-size:12px;font-weight:700;color:#ef4444;'>⚠️ Señales de atención:</span><ul style='margin:4px 0 0 16px;font-size:13px;color:#555;line-height:1.8;'><li>[Señal 1]</li>...</ul></div>
<div style='margin-top:8px;'><span style='font-size:12px;font-weight:700;color:#22c55e;'>✅ Señales positivas:</span><ul style='margin:4px 0 0 16px;font-size:13px;color:#555;line-height:1.8;'><li>[Señal 1]</li>...</ul></div>

Instrucciones:
- Reemplaza COLOR_HEX y EMOJI por: 🔴 #ef4444 (recuperación urgente), 🟡 #eab308 (precaución/fondo suave), o 🟢 #22c55e (listo para intensidad).
- Clasifica en las viñetas solo los datos reales que te proveí.
- Si no hay señales alarmantes, omite esa lista (y viceversa).
- Mantén el texto directo, profesional y conciso.
{plan_section}{rules_section}
"""
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt
        )
        html_fragment = response.text.replace('```html', '').replace('```', '').strip()
        logger.info("AI recommendation successfully generated.")
        return html_fragment
    except Exception as e:
        logger.error(f"Error calling Gemini API: {e}")
        return None


def goal_setup_chat(message: str, history: list, raw_data: dict, user_name: str = "Atleta", coaching_rules=None, weekly_summaries=None, user_tz=None) -> dict:
    """
    Conversational goal-setting coach. Analyzes 90 days of athlete data and guides
    a natural conversation to produce a structured training goal.
    Returns: {"reply": str, "goal_draft": dict | None}
    """
    import re as _re
    import json as _json

    client = _get_client()
    if not client:
        return {"reply": "No se encontró la GEMINI_API_KEY.", "goal_draft": None}

    try:
        from google.genai import types as genai_types

        cutoff = (today_tz(user_tz) - timedelta(days=90)).isoformat()
        cutoff_month = cutoff[:7]

        # ---- Build 90-day run summary ----
        all_acts_90 = []
        for m_key, m_data in raw_data.get("months", {}).items():
            if m_key >= cutoff_month:
                for a in m_data.get("activities", []):
                    if a.get("startTimeLocal", "")[:10] >= cutoff:
                        all_acts_90.append(a)

        run_acts = [a for a in all_acts_90
                    if "run" in a.get("activityType", {}).get("typeKey", "")]
        run_acts.sort(key=lambda x: x.get("startTimeLocal", ""), reverse=True)

        other_types: dict = {}
        for a in all_acts_90:
            tk = a.get("activityType", {}).get("typeKey", "")
            if tk and "run" not in tk:
                other_types[tk] = other_types.get(tk, 0) + 1

        total_run_km = sum(a.get("distance", 0) / 1000.0 for a in run_acts)
        avg_weekly_km = round(total_run_km / 13, 1)

        # Last 10 runs with pace + HR + stride + balance
        pace_lines = []
        for a in run_acts[:10]:
            speed = a.get("averageSpeed", 0)
            dist = round(a.get("distance", 0) / 1000.0, 1)
            d_str = a.get("startTimeLocal", "")[:10]
            pace_str = "—"
            if speed and speed > 0:
                ps = 1000 / speed
                pace_str = f"{int(ps // 60)}:{int(ps % 60):02d} /km"
            hr = a.get("averageHR", "")
            hr_str = f" · FC {hr} bpm" if hr else ""
            stride_cm = a.get("avgStrideLength")
            stride_str = f" · zancada {round(stride_cm)} cm" if stride_cm and 60 < stride_cm < 230 else ""
            balance = a.get("avgGroundContactBalance")
            balance_str = f" · pisada {round(balance, 1)}%izq/{round(100 - balance, 1)}%der" if balance and 40 < balance < 60 else ""
            pace_lines.append(f"  {d_str}: {dist} km @ {pace_str}{hr_str}{stride_str}{balance_str}")

        # Weekly km breakdown last 13 weeks
        today = today_tz(user_tz)
        current_week_monday = today - timedelta(days=today.weekday())
        weekly_km_lines = []
        for w in range(12, -1, -1):
            ws = current_week_monday - timedelta(weeks=w)
            we = ws + timedelta(days=6)
            km = sum(
                a.get("distance", 0) / 1000
                for a in run_acts
                if ws.isoformat() <= a.get("startTimeLocal", "")[:10] <= we.isoformat()
            )
            label = "Esta sem." if w == 0 else ws.strftime("%d/%m")
            weekly_km_lines.append(f"  {label}: {round(km, 1)} km")

        # Extract RHR (latest available)
        rhr = "—"
        for m_key in sorted(raw_data.get("months", {}).keys(), reverse=True):
            for d_str in sorted(raw_data["months"][m_key].get("daily_stats", {}).keys(), reverse=True):
                rhr_data = raw_data["months"][m_key]["daily_stats"][d_str].get("resting_heart_rate", {})
                if rhr_data:
                    try:
                        metrics_map = rhr_data.get("allMetrics", {}).get("metricsMap", {})
                        v = metrics_map.get("WELLNESS_RESTING_HEART_RATE", [{}])[0].get("value")
                        if v:
                            rhr = int(v)
                    except Exception:
                        pass
                if rhr != "—":
                    break
            if rhr != "—":
                break

        other_str = (
            ", ".join(f"{t}: {c}x" for t, c in sorted(other_types.items(), key=lambda x: -x[1])[:5])
            or "ninguna registrada"
        )

        from weekly_summarizer import format_weekly_summaries_for_ai
        weekly_text = format_weekly_summaries_for_ai(weekly_summaries or {}, num_weeks=13)

        system_ctx = f"""Eres Sento, un sistema experto en análisis de entrenamiento. Tu misión es ayudar a {user_name} a definir un objetivo de entrenamiento realista y estructurado, usando sus datos reales de Garmin de los últimos 90 días.

=== DATOS DEL ATLETA — ÚLTIMOS 90 DÍAS ===
- FC en reposo (RHR): {rhr} bpm
- Km totales corridos: {round(total_run_km, 1)} km
- Promedio semanal: {avg_weekly_km} km/semana
- Total carreras: {len(run_acts)}
- Otras actividades: {other_str}

Resúmenes semanales (últimas 13 semanas):
{weekly_text if weekly_text.strip() != "(sin resúmenes semanales disponibles)" else chr(10).join(weekly_km_lines)}

Últimas 10 carreras (detalle):
{chr(10).join(pace_lines) if pace_lines else "  (sin carreras recientes)"}

=== TU MISIÓN ===
Conversa de forma natural para recopilar la información necesaria. OBLIGATORIO — antes de proponer ningún objetivo ni plan, haz estas 3 preguntas (puedes hacerlas gradualmente, no todas de golpe):

1. **¿Tienes o has tenido alguna lesión reciente** o condición física que debas tomar en cuenta? (Ej: rodilla, tendón de Aquiles, espalda, etc.)
2. **¿Cuántos días a la semana puedes entrenar** y cuántas horas en total tienes disponibles por semana?
3. **¿Cuál es tu objetivo de distancia?** (5K, 10K, media maratón, maratón, trail, etc.)

Una vez que tengas esas 3 respuestas, también pregunta o infiere:
- Tiempo o ritmo objetivo (min/km o tiempo total HH:MM:SS), si lo tiene
- Fecha del evento, si la tiene
- **¿Cuántos días a la semana puedes dedicar a ejercicios de fuerza o gimnasio** (tren inferior, core)? (puede ser 0 si no quiere fuerza)
- **¿Tienes preferencias sobre cuándo entrenar?** Por ejemplo: ¿qué día prefieres hacer tu carrera larga (¿sábado o domingo?)? ¿Qué día(s) prefieres descansar? ¿Hay algún día fijo en que no puedas entrenar?

Evalúa si el objetivo es realista en función de los datos del atleta. Si es demasiado ambicioso, sugiere uno más alcanzable con argumentos basados en los datos. Si es conservador, menciónalo positivamente.

=== REGLAS DE DURACIÓN MÍNIMA DE PLAN ===
Al proponer un plan de entrenamiento, usa estos rangos según la distancia objetivo:
- **10K**: mínimo 6 semanas · óptimo 8–12 semanas
- **Media maratón**: mínimo 8 semanas · óptimo 12–16 semanas
- **Maratón**: mínimo 12 semanas · óptimo 16–20 semanas

Si la fecha del evento no deja suficiente tiempo para el mínimo recomendado, adviértelo claramente y sugiere alternativas (cambiar evento, ajustar objetivo de tiempo, etc.).

Cuando tengas suficiente información, propón también las **zonas de FC**:
- Fácil/Trote: hasta FC máx estimada × 0.75
- Tempo: FC máx estimada × 0.85–0.90
- Intervalos: > FC máx estimada × 0.90
(Estima FC máx ≈ 220 - edad estimada, o usa 182 bpm si no sabes la edad. Ajusta si el atleta te da su edad.)

Cuando el atleta confirme el objetivo y tengas los datos mínimos (tipo de carrera + ritmo/tiempo + fecha aproximada + lesiones + disponibilidad), incluye AL FINAL de tu mensaje el bloque JSON:

```goal_json
{{
  "race_type": "tipo de carrera",
  "target_pace_str": "M:SS",
  "target_pace_min": N,
  "target_pace_sec": NN,
  "weekly_peak_km": N,
  "easy_hr_max": N,
  "tempo_hr_min": N,
  "tempo_hr_max": N,
  "interval_hr_min": N,
  "description": "descripción breve del evento y fecha",
  "event_date": "YYYY-MM-DD",
  "injuries": "descripción de lesiones o condiciones, o 'ninguna'",
  "availability_days": N,
  "availability_hours_week": N,
  "strength_days": N,
  "schedule_preferences": "ej: largo el domingo, descanso el lunes y viernes, no puedo entrenar los miércoles",
  "plan_duration_weeks": N,
  "plan_start_date": "YYYY-MM-DD"
}}
```

Solo incluye ese bloque cuando el atleta confirme o pida proceder. Puedes actualizar el bloque si ajusta algo después.
Responde en español. Sé directo, motivador y profesional. Usa negritas y listas cuando ayude a la claridad.

COMUNICACIÓN CON EL USUARIO (OBLIGATORIO): Nunca menciones términos técnicos como "JSON", "bloque de código", "datos estructurados", "estructura", "formato", "parámetros" ni ningún término informático. Cuando el objetivo esté listo para guardar, simplemente di: "¡Perfecto, {user_name}! Tu objetivo de entrenamiento está configurado. Revisa los detalles en la tarjeta de abajo y confirma cuando estés listo."{_format_rules(coaching_rules)}{_FITNESS_ONLY_RULE}"""

        contents = [
            genai_types.Content(role="user", parts=[genai_types.Part(text=system_ctx)]),
            genai_types.Content(role="model", parts=[genai_types.Part(
                text=f"¡Hola {user_name}! Soy Sento. Ya tengo acceso a tus datos de los últimos 90 días y estoy listo para ayudarte a definir tu próximo objetivo. Para empezar: **¿tienes o has tenido alguna lesión reciente** o condición física que debamos tomar en cuenta al planificar tu entrenamiento?"
            )]),
        ]
        for turn in (history or []):
            role = turn.get("role", "user")
            text = turn.get("content", "")
            if text:
                contents.append(genai_types.Content(role=role, parts=[genai_types.Part(text=text)]))
        contents.append(genai_types.Content(role="user", parts=[genai_types.Part(text=message)]))

        response = client.models.generate_content(model="gemini-2.5-flash", contents=contents)
        full_text = response.text.strip()

        # Extract structured goal if present
        goal_draft = None
        match = _re.search(r'```goal_json\s*([\s\S]*?)```', full_text)
        if match:
            try:
                goal_draft = _json.loads(match.group(1).strip())
                if not all(k in goal_draft for k in ("race_type", "target_pace_str", "weekly_peak_km")):
                    goal_draft = None
            except Exception:
                goal_draft = None

        reply = _re.sub(r'```goal_json[\s\S]*?```', '', full_text).strip()
        return {"reply": reply, "goal_draft": goal_draft}

    except Exception as e:
        logger.error(f"Error en goal_setup_chat: {e}")
        return {"reply": f"Error al consultar la IA: {str(e)}", "goal_draft": None}


def ask_ai_with_context(question, dashboard_data, raw_data, history=None, user_name="Atleta", training_goal=None, coaching_rules=None, training_plan=None, weekly_summaries=None, user_tz=None):
    """
    Toma una pregunta del usuario y responde usando Gemini con contexto
    completo de los últimos 120 días de métricas de Garmin.
    """
    client = _get_client()
    if not client:
        return "No se encontró la GEMINI_API_KEY. Configura la variable de entorno para usar el chat de IA."

    try:
        from weekly_summarizer import format_weekly_summaries_for_ai
        weekly_text = format_weekly_summaries_for_ai(weekly_summaries or {})

        monthly_lines = []
        for m_key in sorted(raw_data.get("months", {}).keys()):
            m_data = raw_data["months"][m_key]
            acts = m_data.get("activities", [])
            run_km = sum(a.get("distance", 0) / 1000.0 for a in acts
                         if "run" in a.get("activityType", {}).get("typeKey", ""))
            runs = sum(1 for a in acts if "run" in a.get("activityType", {}).get("typeKey", ""))
            strength = sum(1 for a in acts if "strength" in a.get("activityType", {}).get("typeKey", ""))
            monthly_lines.append(
                f"  {m_key}: {runs} carreras / {round(run_km,1)} km / {strength} fuerza"
            )
        monthly_summary = "\n".join(monthly_lines) if monthly_lines else "  (sin datos mensuales)"

        vo2_max = dashboard_data.get('vo2_max', '—')
        rhr = dashboard_data.get('rhr', '—')
        stress_score = dashboard_data.get('stress_score', '—')
        total_run_distance = dashboard_data.get('total_run_distance', '—')
        runs_count = dashboard_data.get('runs_count', '—')

        goal = training_goal or dashboard_data.get('training_goal') or {}
        goal_configured = bool(dashboard_data.get('goal_configured')) or bool(
            training_goal and training_goal.get('target_pace_str'))
        race_type = goal.get('race_type', '')
        target_pace = goal.get('target_pace_str', '')
        weekly_peak_km = goal.get('weekly_peak_km', '')
        goal_desc = goal.get('description', '')

        if goal_configured:
            goal_line = (f"Especializado en {race_type} (objetivo: ritmo {target_pace} min/km)."
                         + (f" {goal_desc}" if goal_desc else ""))
        else:
            goal_line = "Sin objetivo de carrera configurado aún. Responde basándote en métricas y rendimiento real."

        # 6-week detailed activities
        six_weeks_ago = (today_tz(user_tz) - timedelta(days=42)).isoformat()[:10]
        acts_6w = []
        for m_data in raw_data.get("months", {}).values():
            for a in m_data.get("activities", []):
                if a.get("startTimeLocal", "")[:10] >= six_weeks_ago:
                    acts_6w.append(a)
        acts_6w.sort(key=lambda x: x.get("startTimeLocal", ""), reverse=True)

        acts_6w_lines = []
        for act in acts_6w:
            name = act.get('activityName', act.get('activityType', {}).get('typeKey', 'Actividad'))
            dist = round(act.get('distance', 0) / 1000, 1) if act.get('distance') else '—'
            speed = act.get('averageSpeed', 0)
            pace_s = f"{int(1000/speed//60)}:{int(1000/speed%60):02d} /km" if speed and speed > 0 else '—'
            hr = act.get('averageHR', '')
            hr_str = f" · FC {hr} bpm" if hr else ''
            load = act.get('activityTrainingLoad')
            load_str = f" · carga {round(load)}" if load else ''
            d_s = act.get('startTimeLocal', '')[:10]
            acts_6w_lines.append(f"  • {d_s} — {name}: {dist} km @ {pace_s}{hr_str}{load_str}")
        acts_6w_text = "\n".join(acts_6w_lines) if acts_6w_lines else "  (sin actividades en las últimas 6 semanas)"

        # Athlete profile
        injuries = goal.get('injuries', '') if goal else ''
        avail_days = goal.get('availability_days', '') if goal else ''
        avail_hours = goal.get('availability_hours_week', '') if goal else ''
        profile_lines = []
        if injuries:
            profile_lines.append(f"- Lesiones/condiciones: {injuries}")
        if avail_days:
            profile_lines.append(f"- Disponibilidad: {avail_days} días/semana")
        if avail_hours:
            profile_lines.append(f"- Horas disponibles/semana: {avail_hours}h")
        athlete_profile = ("\n=== PERFIL DEL ATLETA ===\n" + "\n".join(profile_lines) + "\n") if profile_lines else ""

        system_ctx = f"""Eres Sento, un sistema experto en análisis de entrenamiento para {user_name}. {goal_line}

=== QUÉ DATOS TENGO DE TI ===
- Actividades detalladas de las últimas 6 semanas ({six_weeks_ago} a hoy)
- Resúmenes semanales de las últimas 26 semanas
- Resumen mensual de los últimos 6 meses
- Métricas fisiológicas de hoy
Si necesitas información que no está aquí, pregúntame directamente en el chat.
{athlete_profile}
=== MÉTRICAS DE HOY ({today_tz(user_tz).isoformat()}) ===
- VO2 Máx: {vo2_max} ml/kg/min
- FC en reposo: {rhr} bpm
- Estrés promedio: {stress_score}/100
- Km corridos esta semana: {total_run_distance} km ({runs_count} carreras)

=== ACTIVIDADES — ÚLTIMAS 6 SEMANAS (detalle) ===
{acts_6w_text}

=== RESUMEN MENSUAL (6 meses) ===
{monthly_summary}

=== RESÚMENES SEMANALES (hasta 26 semanas — cada fila es una semana) ===
{weekly_text}

Responde de forma clara, directa y profesional. Usa los datos reales de arriba. Puedes usar markdown básico (negritas, listas). Responde en español. Si el usuario pregunta algo que requiere información que no tienes, pídela directamente en el chat.{_format_rules(coaching_rules)}{_format_training_plan(training_plan)}{_FITNESS_ONLY_RULE}"""

        from google.genai import types as genai_types

        contents = []
        contents.append(genai_types.Content(role="user", parts=[genai_types.Part(text=system_ctx)]))
        contents.append(genai_types.Content(role="model", parts=[genai_types.Part(text="Entendido. Tengo acceso a todos tus datos de Garmin. ¿En qué puedo ayudarte?")]))

        for turn in (history or []):
            role = turn.get("role", "user")
            text = turn.get("content", "")
            if text:
                contents.append(genai_types.Content(role=role, parts=[genai_types.Part(text=text)]))

        contents.append(genai_types.Content(role="user", parts=[genai_types.Part(text=question)]))

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=contents
        )
        return response.text.strip()

    except Exception as e:
        logger.error(f"Error en ask_ai_with_context: {e}")
        return f"Error al consultar la IA: {str(e)}"


def generate_training_plan_schedule(goal: dict, weekly_summaries: dict | None = None, user_name: str = "Atleta", user_tz: str | None = None) -> dict | None:
    """
    Generates a structured week-by-week training plan from goal parameters.
    Returns a structured dict or None on error.
    """
    import json as _json
    import re as _re

    client = _get_client()
    if not client:
        return None

    try:
        race_type = goal.get('race_type', 'Carrera')
        target_pace = goal.get('target_pace_str', '5:00')
        weekly_peak_km = goal.get('weekly_peak_km', 40)
        event_date = goal.get('event_date', '')
        plan_duration_weeks = int(goal.get('plan_duration_weeks') or 12)
        plan_start_date = goal.get('plan_start_date') or today_tz(user_tz).isoformat()
        injuries = goal.get('injuries') or 'ninguna'
        availability_days = goal.get('availability_days') or 4
        availability_hours = goal.get('availability_hours_week') or 5
        strength_days = int(goal.get('strength_days') or 0)
        schedule_preferences = goal.get('schedule_preferences') or 'sin preferencias específicas'
        today_str = today_tz(user_tz).isoformat()

        from weekly_summarizer import format_weekly_summaries_for_ai
        weekly_text = format_weekly_summaries_for_ai(weekly_summaries or {}, num_weeks=8)

        prompt = f"""Actúa como un entrenador de atletismo de élite certificado. Genera un plan de entrenamiento profesional y exhaustivo para {user_name}.

=== OBJETIVO ===
- Distancia: {race_type}
- Ritmo meta: {target_pace} min/km
- Fecha del evento: {event_date or 'no definida'}
- Pico semanal acordado: {weekly_peak_km} km
- Duración del plan: {plan_duration_weeks} semanas
- Inicio del plan: {plan_start_date}
- Lesiones/condiciones: {injuries}
- Disponibilidad: {availability_days} días/semana, {availability_hours}h/semana totales
- Días de fuerza: {strength_days} días/semana
- Preferencias de horario: {schedule_preferences}

=== NIVEL ACTUAL (últimas 8 semanas) ===
{weekly_text}

=== PRINCIPIOS OBLIGATORIOS ===

1. PRECISIÓN DE VOLUMEN Y KILOMETRAJE
   - Ancla el volumen a la base actual del atleta (ver historial arriba), su disponibilidad y la distancia objetivo.
   - NO subentrenar por aplicar reglas genéricas de progresión. Si el atleta ya corre más km de los que una progresión del 10% permitiría, arranca desde su nivel real.
   - REGLA DEL 10%: El kilometraje semanal total NUNCA debe aumentar más del 10–15% respecto a la semana de carga anterior (excluyendo semanas de descarga).
   - La carrera larga (long run) debe escalar correctamente según la distancia objetivo:
     * 10K: long run de 12–18 km en pico
     * Medio maratón: long run de 18–26 km en pico
     * Maratón: long run de 28–34 km en pico (máximo 1–2 salidas en el rango 32–34 km)
   - LÍMITE DE FONDO (maratón): ESTRICTAMENTE prohibido programar fondos mayores a 34 km para corredores amateurs. El pico de long run es 32–34 km.
   - La carrera larga NO debe exceder el 30–35% del volumen semanal total.

2. GESTIÓN DE CARGA (regla 80/20)
   - 80% de los km en zonas aeróbicas bajas (fácil / zona 2).
   - 20% en intensidad moderada/alta (tempo, intervalos, ritmo de carrera).
   - No agregar "km basura" solo para alcanzar porcentajes. Cada sesión tiene propósito.

3. RITMOS PRECISOS
   - Define ritmos exactos para cada tipo de sesión, calculados desde el estado de forma actual y el ritmo meta:
     * Recuperación: ritmo meta + 90–120 s/km
     * Suave/Zona 2: ritmo meta + 60–90 s/km
     * Tempo/Umbral de lactato: ritmo meta + 10–20 s/km
     * Intervalos VO2 Max: ritmo meta − 10–20 s/km
     * Ritmo objetivo de carrera (race pace): exactamente el ritmo meta

4. PERIODIZACIÓN Y TAPERING
   - Estructura el plan en fases claras: Base → Construcción → Pico → Tapering.
   - Tapering bien calculado según distancia:
     * 10K: 1 semana de tapering
     * Medio maratón: 2 semanas de tapering
     * Maratón: 2–3 semanas de tapering
   - Durante el tapering reducir volumen 20–30% por semana pero mantener intensidad (no bajar a km ridículos).
   - La semana previa al evento debe sentirse ligera pero activa (mínimo 40% del pico para maratón).

5. DESCARGAS
   - Cada 3–4 semanas incorporar una semana de descarga reduciendo volumen 15–20% respecto a la semana anterior.
   - Las descargas NO eliminan la intensidad; solo bajan el volumen total.

6. DISPONIBILIDAD Y DISTRIBUCIÓN SEMANAL
   - Exactamente {availability_days} días de entrenamiento por semana. El resto son descanso o recuperación activa.
   - PREFERENCIAS DEL ATLETA (OBLIGATORIO respetarlas): {schedule_preferences}
     * Si indica un día preferido para el fondo largo (long), asignarlo SIEMPRE en ese día en todas las semanas.
     * Si indica días de descanso preferidos o días en que no puede entrenar, respetarlos en cada microciclo.
     * Si no hay preferencias, colocar el largo al final del microciclo (sábado o domingo) por defecto.
   - REGLA DE RECUPERACIÓN: NUNCA programar dos sesiones de calidad (tempo, intervals, race pace) en días consecutivos. SIEMPRE debe haber al menos un día de descanso o 'easy' entre sesiones intensas.
   - El día posterior al fondo largo debe ser obligatoriamente descanso o recuperación muy suave ('easy' corto ≤ 6 km).
   - Si hay lesiones, evitar impacto alto y mencionar precauciones específicas en la descripción de cada workout afectado.

7. FUERZA Y PREVENCIÓN
   - Asignar exactamente {strength_days} días a la semana de tipo 'cross' enfocados en fuerza de tren inferior y core (glúteos, isquiotibiales, cadera, estabilización).
   - Si {strength_days} es 0, no incluir sesiones de fuerza.
   - Si {injuries} menciona asimetrías, desequilibrios musculares o lesiones (ej. isquiotibiales, cadera, rodilla), los días de fuerza son OBLIGATORIOS (mínimo 1–2 días) aunque {strength_days} sea 0, y deben incluir trabajo correctivo específico.
   - Los días de fuerza NO deben coincidir con sesiones de carrera intensa (tempo, intervals) en el mismo día. Preferir combinarlos con días 'easy' o de descanso activo.

Genera EXACTAMENTE {plan_duration_weeks} semanas. Devuelve ÚNICAMENTE JSON válido sin ningún texto adicional ni marcadores de código markdown:

{{
  "title": "Plan {race_type} — {plan_duration_weeks} semanas",
  "total_weeks": {plan_duration_weeks},
  "race_type": "{race_type}",
  "event_date": "{event_date}",
  "goal_pace": "{target_pace} /km",
  "weekly_peak_km": {weekly_peak_km},
  "plan_start_date": "{plan_start_date}",
  "generated_at": "{today_str}",
  "phases": [
    {{"name": "nombre de la fase", "start_week": 1, "end_week": N}}
  ],
  "weeks": [
    {{
      "week_number": 1,
      "phase": "nombre de la fase",
      "total_km": N,
      "focus": "foco de la semana en 1 frase corta",
      "workouts": [
        {{
          "day": 1,
          "day_name": "Lunes",
          "type": "rest",
          "label": "Descanso",
          "km": 0,
          "zone": "",
          "description": "descripción breve en 1-2 oraciones"
        }}
      ]
    }}
  ],
  "disclaimer": "Este plan es una recomendación generada por inteligencia artificial basada en tus datos reales de entrenamiento. Siempre escucha a tu cuerpo y ajusta la carga según cómo te sientas. Ante cualquier molestia o dolor, descansa y consulta con un profesional de la salud o entrenador certificado."
}}

Tipos válidos de workout: rest, easy, tempo, intervals, long, cross, race"""

        response = client.models.generate_content(
            model='gemini-2.5-pro',
            contents=prompt
        )
        text = response.text.strip()

        # Strip any markdown code fences Gemini might add
        text = _re.sub(r'^```(?:json)?\s*\n?', '', text, flags=_re.MULTILINE)
        text = _re.sub(r'\n?```\s*$', '', text, flags=_re.MULTILINE)
        text = text.strip()

        plan = _json.loads(text)
        if not isinstance(plan.get('weeks'), list) or len(plan['weeks']) == 0:
            raise ValueError("Plan JSON missing valid 'weeks' array")

        logger.info(f"Training plan generated: {plan.get('total_weeks')} weeks for {race_type}")
        return plan

    except Exception as e:
        logger.error(f"Error generating training plan schedule: {e}")
        return None
