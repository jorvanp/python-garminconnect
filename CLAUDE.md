# CLAUDE.md — GarminConnect App

Contexto del proyecto para Claude y nuevos colaboradores.

---

## Qué hace la app

App web de coaching de fitness personalizado con login por cuenta de Google. El usuario puede conectar su cuenta Garmin, la app descarga su historial de actividades y métricas diarias, y el asistente IA **Sento** (basado en Gemini) genera prescripciones de entrenamiento diarias, responde preguntas de fitness, define objetivos y genera planes de entrenamiento semana a semana. Es **multideporte**: soporta objetivos de carrera a pie y de ciclismo (ruta y rodillo/indoor), más recomendaciones de fuerza y movilidad/estiramientos.

**Usuarios:** ~5 usuarios activos. App en producción en GCP.

---

## Stack

| Capa | Tecnología |
|---|---|
| Backend | Flask (Python), blueprints por módulo |
| Auth usuarios | Google OAuth 2.0 |
| Auth Garmin | API no oficial (`garminconnect` + `garth`) |
| IA | Google Gemini 2.5 Flash (asistente "Sento") |
| Base de datos | Firestore (usuarios, metadata, summaries) |
| Almacenamiento | GCS bucket `{PROJECT_ID}-garmin-data` |
| Deploy | Cloud Run — `bash deploy.sh` |
| Cron | Cloud Scheduler → endpoint `/internal/cron-refresh` |

---

## Archivos clave

```
app.py                      # Entrada Flask, registra blueprints, filtros Jinja
auth.py                     # Google OAuth callback, guarda last_login, setea needs_login_refresh
routes/
  dashboard.py              # Dashboard principal, loading screen, /prescription/status,
                            # /plan, /goal/generate-plan, /upload-activities, /garmin-reconnect
  cron.py                   # Cron diario de actualización de datos
  admin.py                  # Panel admin (toggle garmin_sync_disabled por usuario)
garmin_onboarding.py        # Estado en memoria de refreshes (_refreshing, _fetch_progress)
export_data.py              # Conexión a Garmin API, fetch de datos, init_api() con retry 429
ai_advisor.py               # Gemini: prescripción diaria, chat Sento, setup de objetivos,
                            # generación de plan de entrenamiento semana a semana
helpers.py                  # process_dashboard_data() — transforma raw data para el template
weekly_summarizer.py        # compute_weekly_summaries() / format_weekly_summaries_for_ai()
firestore_helper.py         # Operaciones Firestore
gcs_helper.py               # Operaciones GCS
tz_utils.py                 # now_cdmx(), today_tz(), filtro Jinja ts_cdmx
templates/
  fitness_report.html       # Dashboard del usuario
  dashboard_loading.html    # Pantalla de carga con polling a /prescription/status
  training_plan.html        # Plan de entrenamiento semana a semana
  goal_setup.html           # Chat con Sento para definir objetivo + generar plan
  admin_dashboard.html      # Panel admin
  _user_menu.html           # Partial: dropdown de usuario (Mi perfil / Objetivos anteriores / Salir).
                            # Incluido con {% include %} en toda página de usuario logueado —
                            # nunca copiar/pegar el menú, siempre incluir este partial
  _admin_menu.html          # Partial: links del navbar de admin. Incluido en toda página /admin/*
deploy.sh                   # Deploy completo a Cloud Run (incluye scheduler y secrets)
```

---

## Flujo de login y refresh

1. `auth.py` — setea `session['needs_login_refresh'] = True` en cada login OAuth
2. `routes/dashboard.py index()` — consume el flag con `session.pop('needs_login_refresh', False)`
3. Si hay flag (o no hay datos, o está refreshing) → muestra `dashboard_loading.html`
4. Antes de iniciar el thread: `refresh_pending(user_id)` setea `pct=1` para evitar que el estado stale del refresh anterior marque `ready=True` prematuramente
5. Thread en background ejecuta `_refresh_background()` → llama `fetch_data_current_month()` → genera prescripción IA si hay actividades nuevas hoy o la prescripción no es de hoy
6. `dashboard_loading.html` hace polling a `/prescription/status` cada 1.5-3s
7. `prescription_status` devuelve `ready=True` solo cuando `pct==100 AND not refreshing AND has_ai`
8. Al recibir `ready=True` → redirige a `/`

**Estado en memoria (garmin_onboarding.py):**
- `_refreshing`: set de user_ids actualmente en refresh
- `_fetch_progress`: dict `{user_id: {pct, msg, type}}`
- Se pierde si Cloud Run reinicia la instancia — `prescription_status` lo detecta (`pct==0 AND not refreshing`) y reinicia el refresh automáticamente

---

## Cron diario (5am CDMX / 11am UTC)

- Endpoint: `POST /internal/cron-refresh?apikey=CRON_API_KEY`
- Procesa usuarios **secuencialmente** (no en paralelo) — evita OOM y rate limits de Garmin
- Solo carga **últimos 14 días** (`fetch_data_recent`) — no los 6 meses completos
- **No genera prescripción IA** — se genera al primer login del usuario
- Delay de 20s entre usuarios
- Usuarios con `garmin_sync_disabled=True` son saltados (se registra en results["success"])

**Por qué secuencial:** Con 5 usuarios en paralelo × JSON de 6 meses cada uno = OOM (>512MB). Secuencial + 14 días = ~28 segundos total, flat en memoria.

---

## Tokens de Garmin

- `oauth1_token`: larga duración (meses/años), guardado en GCS por usuario
- `oauth2_token`: expira en 1 hora, se regenera del oauth1 via `sso.exchange()`
- `init_api()` en `export_data.py` tiene retry con backoff (15s → 30s → 60s) para 429

**Problema conocido — Rate limit 429:**
El rate limit de Garmin es **por cuenta de usuario** (no por IP). Ocurre cuando múltiples usuarios necesitan exchange de token simultáneamente. No se resuelve cambiando de región.
- Si ocurre: esperar 2-6 horas sin hacer más intentos
- Si persiste: regenerar tokens manualmente desde máquina local con `garth.login()` + `gsutil cp`
- Prevención: el cron guarda los tokens frescos — los usuarios que entren dentro de la hora siguiente no necesitan exchange

**Plan futuro:** Migrar a Garmin Health API oficial (webhook push). La arquitectura híbrida planeada es: unofficial API para carga inicial de 6 meses + webhook oficial para actualizaciones diarias.

---

## garmin_sync_disabled (flag admin)

Cuando un admin activa esta bandera por usuario en Firestore:
- El cron salta al usuario (no llama Garmin API)
- El login no dispara refresh de Garmin; muestra dashboard con banner de aviso
- El usuario puede importar actividades vía CSV para que Sento genere prescripción
- `_regenerate_ai_only()` genera prescripción desde datos existentes sin llamar a Garmin

---

## Importación de CSV

- Endpoint: `POST /upload-activities`
- Acepta exports de Garmin en español (headers en español)
- Deduplica por `startTimeLocal`
- Siempre regenera prescripción IA después del upload (aunque no haya actividades nuevas)
- Tipos de actividad mapeados en `_ACTIVITY_TYPE_MAP` (dashboard.py)

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
3. Hay actividades nuevas registradas hoy vs las que había antes del refresh

**Contexto de la prescripción:**
- Actividades detalladas de las **últimas 6 semanas** (desde `raw_data`)
- Resúmenes semanales de los **últimos 4 meses** (`weekly_summaries`)
- Perfil del atleta: lesiones, disponibilidad (si están en el `training_goal`)
- Métricas fisiológicas del día (VO2 Máx, RHR, estrés)

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

## Weekly summaries (resúmenes semanales)

- Se calculan en `compute_weekly_summaries()` de `weekly_summarizer.py`
- Se guardan en Firestore colección `weekly_summaries/{uid}`
- Se calculan **antes** de llamar a `generate_daily_recommendation()` para usarlos como contexto
- `format_weekly_summaries_for_ai()` los formatea como texto para los prompts

---

## GCS — estructura de datos por usuario

```
users/{google_user_id}/
  tokens/
    oauth1_token.json
    oauth2_token.json
  training_data_monthly.json   # Historial completo: actividades + métricas diarias por mes
                               # También contiene metadata.ai_recommendation y ai_recommendation_date
```

**Firestore — campos relevantes del documento de usuario:**
```
training_goal           # Objetivo configurado (ver campos arriba)
training_plan           # Plan subido como imagen (texto extraído por Gemini)
training_plan_schedule  # Plan generado semana a semana por Sento
garmin_sync_disabled    # Flag admin: deshabilita toda llamada a Garmin API
needs_garmin_reconnect  # Flag: token inválido, pedir reconexión al usuario
last_refresh            # Timestamp del último refresh exitoso
timezone                # Zona horaria IANA del usuario
```

---

## Tests

```bash
.venv/bin/python -m pytest tests/test_helpers.py -v
```

**Regla:** Antes de cada cambio en `helpers.py`, `routes/admin.py`, `routes/dashboard.py` o `weekly_summarizer.py`, correr los tests. Si un cambio rompe un test, arreglarlo antes de continuar.

**Archivos de test del proyecto (no del paquete garminconnect):**
```
tests/test_helpers.py    # process_dashboard_data, _merge_goal
```

**Qué cubren los tests actuales:**
- `_merge_goal(None)` → devuelve todos los defaults (nunca KeyError en template)
- `process_dashboard_data(None/{}/ sin months)` → devuelve None (no crashea en admin)
- `process_dashboard_data(raw_mínimo)` → dict con todas las keys que usa el template
- Regression: `dashboard_data['_admin_viewing'] = True` no crashea cuando data es válida

---

## Flujo de trabajo al completar un cambio (ORDEN OBLIGATORIO)

Al terminar cualquier flujo o funcionalidad, seguir SIEMPRE este orden, sin saltarse pasos:

1. **Correr las pruebas unitarias** — `.venv/bin/python -m pytest tests/test_helpers.py -v`
2. **Si el cambio introduce un flujo nuevo, agregar sus pruebas correspondientes** antes de continuar (no dejar flujos nuevos sin cobertura).
3. **Solo cuando las pruebas estén en verde**, actualizar los archivos de documentación (`.md`: `CLAUDE.md`, `README.md`) que hayan quedado desactualizados por el cambio.
4. **Luego hacer el commit** al repositorio.
5. **Al final, y solo al final, el despliegue a GCP** — `bash deploy.sh`.

Nunca commitear con pruebas en rojo, ni desplegar antes de commitear, ni desplegar sin haber actualizado la documentación.

---

## Reglas importantes — no hacer

- **No paralelizar usuarios en el cron** — OOM garantizado con más de 2 usuarios
- **No generar prescripción IA en el cron** — es lento y costoso; se genera al login
- **No usar `last_refresh[:10] == today` como trigger de refresh** — usar el session flag `needs_login_refresh`
- **No cargar 6 meses completos en el cron** — usar `fetch_data_recent` (14 días)
- **No mockear Firestore/GCS en tests** — las divergencias mock/prod han causado bugs en producción
- **No mencionar términos técnicos al usuario** — Sento nunca habla de JSON, estructuras, bloques de código, etc.
- **No calcular weekly_summaries después del llamado a generate_daily_recommendation** — deben calcularse antes para pasarlos como contexto
- **Siempre correr tests antes de deployar** — `.venv/bin/python -m pytest tests/test_helpers.py -v`
- **Diseño responsivo obligatorio** — todos los cambios de UI deben funcionar en móvil (≤640px). Usar `@media (max-width: 640px)` y `@media (max-width: 700px)`. Revisar especialmente grids, tablas y tarjetas que en móvil deben colapsar a columna única o usar scroll horizontal controlado. Nunca dejar elementos que crezcan sin límite de ancho en flex layouts móviles (`flex: 0 0 <fixed>px` en vez de dejar grow implícito).
- **No copiar/pegar el menú de usuario o el navbar de admin en una plantilla nueva** — usar `{% include '_user_menu.html' %}` (páginas de usuario) o `{% include '_admin_menu.html' %}` (páginas `/admin/*`). Copiar el markup a mano fue la causa de que el dropdown apareciera incompleto o distinto según la página.

---

## Desarrollo local

```bash
LOCAL_DEV=1 PORT=8080 .venv/bin/python app.py    # http://localhost:8080
```

- **`LOCAL_DEV=1`** (solo local): pone `SESSION_COOKIE_SECURE=False` (necesario porque en `http://localhost` una cookie Secure no se guarda y rompería el OAuth) y **habilita la ruta `/auth/dev-login`**.
- **`/auth/dev-login`** (`auth.py`): salta Google OAuth y crea sesión como un usuario existente de Firestore (por email; default el admin, o `DEV_LOGIN_EMAIL=...`). **Inerte en producción** — sin `LOCAL_DEV=1` responde con redirect al login normal.
- ⚠️ **NUNCA** setear `LOCAL_DEV=1` en Cloud Run. No está en `deploy.sh`.
- Para que el login normal con Google funcione en local, la URI `http://localhost:8080/auth/callback` debe estar en las *Authorized redirect URIs* del cliente OAuth.
- `garth` es dependencia transitiva de `garminconnect` (vendorizado) y **debe** estar en `requirements.txt`.

---

## Deploy

```bash
bash deploy.sh
```

El script maneja: build, push a Artifact Registry, deploy a Cloud Run, actualización de Cloud Scheduler. Asegúrate de tener las variables de entorno exportadas antes de correr:

```bash
export OAUTH_CLIENT_ID=...
export OAUTH_CLIENT_SECRET=...
export GEMINI_API_KEY=...
# (ver resto en deploy.sh)
```

**Región actual:** `us-east1`
**Memoria:** 2Gi | **Timeout:** 900s | **Scheduler deadline:** 840s

---

## Zona horaria

Toda la lógica de fechas usa CDMX (UTC-6) como fallback. En código request-time usar siempre `today_tz(user_tz)` / `now_tz(user_tz)` pasando el timezone del usuario (guardado en `session['timezone']` y Firestore). En threads de background y cron usar `now_cdmx()` / `today_cdmx()`.
