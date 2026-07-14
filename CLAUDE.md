# CLAUDE.md — Sento Run

Contexto del proyecto para Claude y nuevos colaboradores.

---

## Qué hace la app

App web de coaching de fitness personalizado con login por cuenta de Google. El usuario sube su historial de actividades exportado desde Garmin Connect (CSV), y el asistente IA **Sento** (basado en Gemini) genera prescripciones de entrenamiento diarias, responde preguntas de fitness, define objetivos y genera planes de entrenamiento semana a semana. Es **multideporte**: soporta objetivos de carrera a pie y de ciclismo (ruta y rodillo/indoor), más recomendaciones de fuerza y movilidad/estiramientos.

**Usuarios:** ~5 usuarios activos. App en producción en GCP.

---

## Stack

| Capa | Tecnología |
|---|---|
| Backend | Flask (Python), blueprints por módulo |
| Auth usuarios | Google OAuth 2.0 |
| Ingesta de datos | Import manual de CSV exportado desde Garmin Connect (sin conexión API en vivo) |
| IA | Google Gemini 2.5 Flash (asistente "Sento") |
| Base de datos | Firestore (usuarios, metadata, summaries) |
| Almacenamiento | GCS bucket `{PROJECT_ID}-garmin-data` |
| Deploy | Cloud Run — `bash deploy.sh` |

---

## Archivos clave

```
app.py                      # Entrada Flask, registra blueprints, filtros Jinja
auth.py                     # Google OAuth callback, guarda last_login
routes/
  dashboard.py              # Dashboard principal, /plan, /goal/generate-plan, /upload-activities
  admin.py                  # Panel admin
ai_advisor.py               # Gemini: prescripción diaria, chat Sento, setup de objetivos,
                            # generación de plan de entrenamiento semana a semana
helpers.py                  # process_dashboard_data() — transforma raw data para el template
weekly_summarizer.py        # compute_weekly_summaries() / format_weekly_summaries_for_ai()
firestore_helper.py         # Operaciones Firestore
gcs_helper.py               # Operaciones GCS
tz_utils.py                 # now_cdmx(), today_tz(), filtro Jinja ts_cdmx
templates/
  fitness_report.html       # Dashboard del usuario
  training_plan.html        # Plan de entrenamiento semana a semana
  goal_setup.html           # Chat con Sento para definir objetivo + generar plan
  goal_history.html         # Objetivos archivados (ver sección "Objetivos archivados")
  admin_dashboard.html      # Panel admin
  _user_menu.html           # Partial: dropdown de usuario (Mi perfil / Objetivos anteriores / Salir).
                            # Incluido con {% include %} en toda página de usuario logueado —
                            # nunca copiar/pegar el menú, siempre incluir este partial
  _admin_menu.html          # Partial: links del navbar de admin. Incluido en toda página /admin/*
deploy.sh                   # Deploy completo a Cloud Run
```

---

## Paleta de colores

No hay un archivo CSS central — cada plantilla trae su propio bloque `<style>` con colores hardcodeados. La paleta de marca es:

- **Rojo Enérgico** `#E63946` — color primario de marca (botones, CTAs, acentos, headers destacados). Variante hover/oscura: `#C3303B`.
- **Gris Oscuro** `#2B2D42` — color secundario/base (fondos oscuros, headers, gradientes). Variantes de la misma familia usadas en gradientes: `#1E1F2E` (más oscuro), `#3A3C58` / `#555767` / `#757684` (más claros).

Estos reemplazan la paleta naranja/navy anterior (`#f97316`, `#ea580c`, `#1a1a2e`, `#16213e`, `#0f3460`, etc.). Al agregar una plantilla nueva o un componente con color, usar estos hex — no reintroducir la paleta anterior. Los colores semánticos de estado (verde éxito `#22c55e`/`#16a34a`, rojo error `#ef4444`, amarillo advertencia `#eab308`) son independientes de la marca y no se tocan.

---

## Flujo de login y dashboard

1. Login por Google OAuth (`auth.py`) — no hay ningún paso adicional de "conectar cuenta"; tras el login (y el assessment de perfil si es la primera vez) el usuario va directo a `routes/dashboard.py index()`.
2. `index()` carga los datos ya almacenados en GCS (`training_data_monthly.json`) para el usuario. Si no hay datos todavía, renderiza `fitness_report.html` con un esqueleto vacío y el CTA de "Importar CSV".
3. No hay pantalla de carga ni polling en el login — la carga de datos es síncrona porque no hay ninguna llamada externa lenta de por medio (a diferencia de cuando la app dependía de la API de Garmin).

---

## Importación de CSV (única vía de ingesta de datos)

- Endpoint: `POST /upload-activities`
- Acepta exports de Garmin Connect en español (headers en español) — `Actividades → Exportar CSV` desde Garmin Connect
- Deduplica por `startTimeLocal`
- Recalcula los resúmenes semanales (`weekly_summarizer.compute_weekly_summaries`) después de cada import
- Tipos de actividad mapeados en `_ACTIVITY_TYPE_MAP` (`routes/dashboard.py`) — replica la taxonomía de Garmin para que las actividades importadas se rendericen igual que si vinieran de la API
- Actualiza `last_refresh` del usuario en Firestore
- **No existe conexión en vivo a la API de Garmin.** La app se conectaba antes vía `garminconnect`/`garth` (API no oficial); esa integración fue eliminada por completo — ya no hay tokens OAuth1/OAuth2, ni cron de sincronización, ni reconexión. El CSV es la única fuente de datos.

---

## Onboarding — perfil del atleta (assessment)

- Formulario multi-paso en `templates/profile_setup.html` (motor de pasos dinámico en JS).
- Endpoints: `GET/POST /onboarding/profile` (`routes/onboarding.py`), guardado vía `firestore_helper.save_assessment`.
- El usuario elige las **disciplinas** que practica (multi-select) y, según lo elegido, se muestran campos específicos. Los pasos que aplican se recalculan dinámicamente (barra de progreso y contador incluidos).
- Estructura guardada (`assessment`):
```
birth_date, sex,
disciplines: ["run","bike","strength","yoga","swim","other"],
weekly_days, weekly_km,                 # correr
longest_run: {distance_km, time, date},
reference_times: {5k,10k,21k,42k},
cycling: {outdoor, indoor, weekly_days, weekly_km, longest_ride_km, ftp},
cross_training: {strength_days, yoga_days, swim_days, other_activity, other_days},
long_run_day, plan_start_preference, plan_start_date
```
- `ai_advisor._format_profile_for_ai()` convierte esto en texto para todos los prompts de Sento (disciplinas, volumen de bici, frecuencia de fuerza/yoga/etc.).
- Validación backend: `birth_date`, `sex` y ≥1 disciplina son obligatorios; los km de correr solo se exigen si `run` está entre las disciplinas.

---

## Sento (asistente IA)

- Solo responde temas de **fitness, entrenamiento, nutrición deportiva, descanso y competencias**
- Rechaza cualquier otro tema (programación, idiomas, historia, etc.)
- **Nunca menciona términos técnicos** (JSON, datos estructurados, bloque, etc.) al usuario
- Esta restricción está en el system prompt de todas las llamadas a Gemini en `ai_advisor.py`

**Prescripción del día** — se regenera si:
1. No existe prescripción previa, O
2. La prescripción existente no es de hoy, O
3. Hay actividades nuevas registradas hoy vs las que había antes del último import

**Contexto de la prescripción:**
- Actividades detalladas de las **últimas 6 semanas** (desde `raw_data`)
- Resúmenes semanales de los **últimos 4 meses** (`weekly_summaries`)
- Perfil del atleta: lesiones, disponibilidad (si están en el `training_goal`)
- Métricas fisiológicas del día si están disponibles en el CSV importado (VO2 Máx, RHR, estrés)

**Chat general (ask_ai_with_context):**
- Actividades detalladas de las últimas 6 semanas
- Resúmenes semanales de 26 semanas
- Resumen mensual de 6 meses
- Sento explica al usuario qué datos tiene y pide lo que le falta

---

## Objetivo de entrenamiento (goal_setup_chat)

Soporta objetivos de **carrera a pie y de ciclismo** (ruta y rodillo/indoor), además de objetivos mixtos. El flujo de definición sigue este orden obligatorio:
1. Pregunta **deporte y evento objetivo** (correr vs. ciclismo; si es bici, ruta/exterior vs. rodillo/indoor)
2. Pregunta sobre **lesiones o condiciones físicas**
3. Pregunta **disponibilidad** (días/semana + horas/semana)
4. Luego infiere/pregunta: ritmo/tiempo meta (correr) o potencia/FTP (bici), fecha del evento, días de rodillo, días de fuerza y días de movilidad/estiramiento

**Reglas de duración mínima de plan:**
Carrera a pie:
- 10K: mínimo 6 semanas · óptimo 8–12 semanas
- Media maratón: mínimo 8 semanas · óptimo 12–16 semanas
- Maratón: mínimo 12 semanas · óptimo 16–20 semanas
Ciclismo:
- Carrera de ruta / criterium: mínimo 8 semanas · óptimo 10–16 semanas
- Gran fondo / gravel (60–120 km): mínimo 10 semanas · óptimo 12–20 semanas
- MTB maratón / century (>120 km): mínimo 12 semanas · óptimo 16–24 semanas

**Campos del `training_goal` en Firestore:**
```
sport,                  # "run" | "bike" | "multi" (default "run")
race_type, target_pace_str, target_pace_min, target_pace_sec,
weekly_peak_km, ftp, indoor_days,   # ftp/indoor_days para ciclismo
easy_hr_max, tempo_hr_min, tempo_hr_max, interval_hr_min,
description, event_date,
injuries, availability_days, availability_hours_week,
strength_days, mobility_days, schedule_preferences,
plan_duration_weeks, plan_start_date
```

**Soporte multideporte:** el coach cubre carrera a pie y ciclismo (ruta + rodillo/indoor) y objetivos mixtos, más recomendaciones de fuerza y movilidad/estiramientos. La lógica sport-aware vive en `ai_advisor.py`: `_build_sport_specific_block()` (principios de ciclismo/potencia/FTP), y ramas por `sport` en `generate_daily_recommendation`, `goal_setup_chat` y `generate_training_plan_schedule`. Las actividades de bici se renderizan en km/h + potencia (no ritmo min/km).

---

## Plan de entrenamiento semana a semana

- Endpoint generación: `POST /goal/generate-plan` — llama Gemini, guarda en Firestore como `training_plan_schedule`
- Endpoint vista: `GET /plan` — renderiza `training_plan.html`
- La función `generate_training_plan_schedule()` en `ai_advisor.py` genera JSON estructurado con fases, semanas y workouts por día
- Tipos de workout: `rest`, `easy`, `tempo`, `intervals`, `long`, `cross`, `race`, `bike` (ruta), `bike_indoor` (rodillo), `mobility` (movilidad/estiramiento)
- Los workouts soportan `km` (distancia) y/o `minutes` (sesiones por tiempo: rodillo, fuerza, movilidad)
- La semana actual se detecta automáticamente con `plan_start_date` y la fecha de hoy
- **`plan_start_date` nunca es anterior a hoy** y **`plan_duration_weeks` se limita a las semanas que caben antes del `event_date`** (guard en `save_goal` y en `generate_training_plan_schedule`). Evita el bug de "hoy = semana 16" cuando el plan se ancla al evento y el inicio cae en el pasado.
- `generate_training_plan_schedule` usa `gemini-2.5-pro` con **hasta 3 reintentos** (a veces devuelve JSON mal formado); si los 3 fallan, devuelve `None`
- El plan incluye disclaimer visible de que es una recomendación de IA

**Flujo completo de configuración:**
1. Usuario chatea con Sento en `/goal/setup`
2. Sento genera la configuración → aparece tarjeta editable en el chat
3. Usuario confirma → `saveGoal()` en JS:
   - `POST /goal` guarda el objetivo en Firestore
   - `POST /goal/generate-plan` genera el plan semana a semana
   - Redirige a `/plan`

---

## Objetivos archivados (`goal_history`)

Cuando el objetivo activo de un usuario deja de ser vigente, se archiva en vez de eliminarse:

- **Trigger automático (evento cumplido):** en `routes/dashboard.py` `index()`, justo tras cargar `user_doc_pre`, `_archive_expired_goal_if_needed()` revisa `is_goal_expired()` (`helpers.py` — `event_date` estrictamente anterior a hoy). Si expiró, archiva y limpia `training_goal`/`training_plan_schedule` del doc activo antes de renderizar, así el Hero deja de mostrarlo (`goal_configured` vuelve a `False`).
- **Trigger manual (objetivo reemplazado):** en `save_goal()` (`POST /goal`), si ya existía un objetivo con un `event_date` distinto al nuevo, se archiva el anterior antes de guardar el nuevo. Editar el mismo objetivo (mismo `event_date`, p. ej. ajustar el ritmo) **no** archiva nada.
- **Almacenamiento:** subcolección Firestore `users/{uid}/goal_history/{auto_id}` — no es una colección nueva a nivel raíz, vive dentro del doc del usuario. Cada entrada guarda `training_goal`, `training_plan_schedule` (snapshot), `archive_reason` (`event_passed` | `replaced`), `archived_at`, y `summary` (texto corto).
- **`summary`:** se genera **una sola vez** al archivar, vía `ai_advisor.summarize_archived_goal()` (una llamada a Gemini flash; si falla, cae a un resumen de texto plano armado desde los campos del goal). No se vuelve a generar después.
- **Vista:** `GET /goals/history` → `templates/goal_history.html`, accesible desde el menú de usuario en el dashboard y en `/plan`.
- **Contexto para Sento:** `firestore_helper.get_goal_history(uid, max_days=90)` filtra a los últimos 90 días y se pasa como `goal_history` a `ask_ai_with_context()` y `goal_setup_chat()` en `ai_advisor.py`, formateado por `_format_goal_history()`. El costo marginal es bajo: con ~5 usuarios activos rara vez hay más de 1-2 objetivos archivados en esa ventana, y el resumen es de pocas líneas de texto plano (no se re-consulta Gemini en cada mensaje).

---

## Weekly summaries (resúmenes semanales)

- Se calculan en `compute_weekly_summaries()` de `weekly_summarizer.py`
- Se guardan en Firestore colección `weekly_summaries/{uid}`
- Se calculan **antes** de llamar a `generate_daily_recommendation()` para usarlos como contexto
- `format_weekly_summaries_for_ai()` los formatea como texto para los prompts

---

## GCS — estructura de datos por usuario

```
users/{google_user_id}/
  training_data_monthly.json   # Historial completo: actividades + métricas diarias por mes,
                               # construido a partir de los CSV importados.
                               # También contiene metadata.ai_recommendation y ai_recommendation_date
```

**Firestore — campos relevantes del documento de usuario:**
```
training_goal           # Objetivo configurado (ver campos arriba)
training_plan           # Plan subido como imagen (texto extraído por Gemini)
training_plan_schedule  # Plan generado semana a semana por Sento
last_refresh            # Timestamp del último import de CSV
timezone                # Zona horaria IANA del usuario
```

---

## Tests

```bash
.venv/bin/python -m pytest tests/ -v
```

**Regla:** Antes de cada cambio en `helpers.py`, `routes/admin.py`, `routes/dashboard.py` o `weekly_summarizer.py`, correr los tests. Si un cambio rompe un test, arreglarlo antes de continuar.

**Archivos de test del proyecto:**
```
tests/test_helpers.py          # process_dashboard_data, _merge_goal, is_goal_expired
tests/test_dashboard_auth.py   # login_required (gate de sesión, sin Firestore)
```

**Qué cubren los tests actuales:**
- `_merge_goal(None)` → devuelve todos los defaults (nunca KeyError en template)
- `process_dashboard_data(None/{}/ sin months)` → devuelve None (no crashea en admin)
- `process_dashboard_data(raw_mínimo)` → dict con todas las keys que usa el template
- Regression: `dashboard_data['_admin_viewing'] = True` no crashea cuando data es válida
- `login_required` solo exige `user_id` en sesión (sin gate adicional)

---

## Flujo de trabajo al completar un cambio (ORDEN OBLIGATORIO)

Al terminar cualquier flujo o funcionalidad, seguir SIEMPRE este orden, sin saltarse pasos:

1. **Correr las pruebas unitarias** — `.venv/bin/python -m pytest tests/ -v`
2. **Si el cambio introduce un flujo nuevo, agregar sus pruebas correspondientes** antes de continuar (no dejar flujos nuevos sin cobertura).
3. **Solo cuando las pruebas estén en verde**, actualizar los archivos de documentación (`.md`: `CLAUDE.md`, `README.md`) que hayan quedado desactualizados por el cambio.
4. **Luego hacer el commit** al repositorio.
5. **Al final, y solo al final, el despliegue a GCP** — `bash deploy.sh`.

Nunca commitear con pruebas en rojo, ni desplegar antes de commitear, ni desplegar sin haber actualizado la documentación.

---

## Reglas importantes — no hacer

- **No mockear Firestore/GCS en tests** — las divergencias mock/prod han causado bugs en producción
- **No mencionar términos técnicos al usuario** — Sento nunca habla de JSON, estructuras, bloques de código, etc.
- **No calcular weekly_summaries después del llamado a generate_daily_recommendation** — deben calcularse antes para pasarlos como contexto
- **Siempre correr tests antes de deployar** — `.venv/bin/python -m pytest tests/ -v`
- **Diseño responsivo obligatorio** — todos los cambios de UI deben funcionar en móvil (≤640px). Usar `@media (max-width: 640px)` y `@media (max-width: 700px)`. Revisar especialmente grids, tablas y tarjetas que en móvil deben colapsar a columna única o usar scroll horizontal controlado. Nunca dejar elementos que crezcan sin límite de ancho en flex layouts móviles (`flex: 0 0 <fixed>px` en vez de dejar grow implícito).
- **No copiar/pegar el menú de usuario o el navbar de admin en una plantilla nueva** — usar `{% include '_user_menu.html' %}` (páginas de usuario) o `{% include '_admin_menu.html' %}` (páginas `/admin/*`). Copiar el markup a mano fue la causa de que el dropdown apareciera incompleto o distinto según la página.
- **No reintroducir una conexión en vivo a la API de Garmin.** La integración no oficial (`garminconnect`/`garth`, tokens OAuth1/OAuth2, cron de sincronización, reconexión) fue eliminada deliberadamente por ser inestable (rate limits, revocación de acceso) y por privacidad. El CSV manual es la vía de ingesta soportada.

---

## Desarrollo local

```bash
LOCAL_DEV=1 PORT=8080 .venv/bin/python app.py    # http://localhost:8080
```

- **`LOCAL_DEV=1`** (solo local): pone `SESSION_COOKIE_SECURE=False` (necesario porque en `http://localhost` una cookie Secure no se guarda y rompería el OAuth) y **habilita la ruta `/auth/dev-login`**.
- **`/auth/dev-login`** (`auth.py`): salta Google OAuth y crea sesión como un usuario existente de Firestore (por email; default el admin, o `DEV_LOGIN_EMAIL=...`). **Inerte en producción** — sin `LOCAL_DEV=1` responde con redirect al login normal.
- ⚠️ **NUNCA** setear `LOCAL_DEV=1` en Cloud Run. No está en `deploy.sh`.
- Para que el login normal con Google funcione en local, la URI `http://localhost:8080/auth/callback` debe estar en las *Authorized redirect URIs* del cliente OAuth.

---

## Deploy

```bash
bash deploy.sh
```

El script maneja: build, push a Artifact Registry, deploy a Cloud Run. Asegúrate de tener las variables de entorno exportadas antes de correr:

```bash
export OAUTH_CLIENT_ID=...
export OAUTH_CLIENT_SECRET=...
export GEMINI_API_KEY=...
# (ver resto en deploy.sh)
```

**Región actual:** `us-east1` (primaria) + `us-central1` (respaldo)
**Memoria:** 2Gi | **Timeout:** 900s

---

## Zona horaria

Toda la lógica de fechas usa CDMX (UTC-6) como fallback. En código request-time usar siempre `today_tz(user_tz)` / `now_tz(user_tz)` pasando el timezone del usuario (guardado en `session['timezone']` y Firestore). En threads de background usar `now_cdmx()` / `today_cdmx()`.
