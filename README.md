# 🏃 Sento Run

App web de coaching de fitness personalizado, desplegada en Google Cloud Platform. Los usuarios importan sus actividades vía CSV exportado desde Garmin Connect, y el asistente IA **Sento** genera planes de entrenamiento semana a semana, responde preguntas de fitness y registra el progreso hacia una meta de carrera a pie o de ciclismo (multideporte).

---

## Stack

| Capa | Tecnología |
|---|---|
| Backend | Flask (Python), blueprints por módulo |
| Auth usuarios | Google OAuth 2.0 |
| Ingesta de datos | Import manual de CSV exportado desde Garmin Connect |
| IA | Google Gemini 2.5 Flash / Pro (asistente "Sento") |
| Base de datos | Firestore |
| Almacenamiento | Google Cloud Storage |
| Deploy | Cloud Run (multi-región: us-east1 + us-central1) |

---

## Estructura del proyecto

```
app.py                      # Entrada Flask, registra blueprints
auth.py                     # Google OAuth callback
routes/
  dashboard.py              # Dashboard principal, prescripción, plan, importación de CSV
  admin.py                  # Panel de administración
  onboarding.py             # Assessment de perfil del atleta
ai_advisor.py               # Gemini: prescripción, chat Sento, generación de plan
helpers.py                  # process_dashboard_data() — transforma datos para el template
weekly_summarizer.py        # Resúmenes semanales de entrenamiento
firestore_helper.py         # Operaciones Firestore
gcs_helper.py                # Operaciones GCS
tz_utils.py                 # Utilidades de zona horaria (CDMX)
templates/
  fitness_report.html       # Dashboard del usuario
  training_plan.html        # Plan de entrenamiento semana a semana
  goal_setup.html           # Chat con Sento para definir objetivo
  admin_dashboard.html      # Panel admin
deploy.sh                   # Deploy completo a Cloud Run
tests/
  test_helpers.py           # Tests de process_dashboard_data y _merge_goal
  test_dashboard_auth.py    # Tests del gate de login
```

---

## Funcionalidades principales

- **Plan de entrenamiento personalizado (multideporte)**: Sento genera un plan semana a semana según el objetivo del usuario — carrera a pie (10K, media, maratón, trail) o ciclismo (ruta, gran fondo, gravel, MTB; exterior o rodillo/indoor) — con su disponibilidad, historial de actividades, zonas de FC/potencia y sesiones de fuerza y movilidad
- **Chat con Sento**: El asistente IA responde preguntas sobre entrenamiento, nutrición deportiva y recuperación. Limita su contexto exclusivamente a fitness
- **Importación de actividades**: Los usuarios suben exports CSV de Garmin Connect — es la única vía de ingesta de datos, no hay conexión API en vivo
- **Panel de administración**: Gestión de usuarios, toggles globales de funcionalidades, visualización del dashboard de cualquier usuario

---

## Deploy

```bash
# Exportar variables de entorno necesarias
export OAUTH_CLIENT_ID=...
export OAUTH_CLIENT_SECRET=...
export GEMINI_API_KEY=...

bash deploy.sh
```

El script maneja: build con Cloud Buildpacks, push a Artifact Registry, deploy a Cloud Run en us-east1 y us-central1.

**URLs de producción:**
- Primaria: `https://garmin-dashboard-dcilayq2hq-ue.a.run.app`
- Respaldo: `https://garmin-dashboard-dcilayq2hq-uc.a.run.app`

---

## Desarrollo local

```bash
# Crear entorno virtual
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Variables de entorno mínimas
export GOOGLE_APPLICATION_CREDENTIALS=path/to/service-account.json
export OAUTH_CLIENT_ID=...
export OAUTH_CLIENT_SECRET=...
export GEMINI_API_KEY=...

# Correr en local (LOCAL_DEV ajusta la cookie de sesión para http://localhost
# y habilita /auth/dev-login para saltar Google OAuth en desarrollo)
LOCAL_DEV=1 PORT=8080 python app.py    # http://localhost:8080
```

- Para el login normal con Google en local, agrega `http://localhost:8080/auth/callback` a las *Authorized redirect URIs* del cliente OAuth.
- Alternativa sin OAuth: visita `http://localhost:8080/auth/dev-login` (solo con `LOCAL_DEV=1`) para entrar como un usuario existente de Firestore. `DEV_LOGIN_EMAIL` elige cuál.

---

## Tests

```bash
.venv/bin/python -m pytest tests/ -v
```

Correr antes de cada cambio en código Python del backend. Ver `CLAUDE.md` para detalles del proyecto.

---

## Variables de entorno

| Variable | Descripción |
|---|---|
| `OAUTH_CLIENT_ID` | Google OAuth Client ID |
| `OAUTH_CLIENT_SECRET` | Google OAuth Client Secret |
| `GEMINI_API_KEY` | API key de Google Gemini |
| `GARMIN_BUCKET` | Nombre del bucket GCS (default: `garminconnect-489920-garmin-data`) |
| `SESSION_SECRET` | Secret para sesiones Flask |
