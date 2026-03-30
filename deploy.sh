#!/bin/bash
set -e

# Configuración de Variables
PROJECT_ID=$(gcloud config get-value project)
if [ -z "$PROJECT_ID" ]; then
    echo "❌ No hay proyecto GCP configurado."
    echo "Corre: gcloud init, o gcloud config set project [TU_PROYECTO]"
    exit 1
fi

SERVICE_NAME="garmin-dashboard"
PRIMARY_REGION="us-east1"
SECONDARY_REGION="us-central1"
BUCKET_NAME="${PROJECT_ID}-garmin-data"

echo "=========================================================="
echo "🚀 Iniciando despliegue de Garmin Dashboard (multi-región)"
echo "Proyecto: $PROJECT_ID"
echo "Regiones: $PRIMARY_REGION (primaria) + $SECONDARY_REGION (respaldo)"
echo "Bucket: $BUCKET_NAME"
echo "=========================================================="

# 0. Habilitar APIs necesarias
echo "⚙️  0/5 Habilitando APIs de GCP..."
gcloud services enable \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    cloudscheduler.googleapis.com \
    firestore.googleapis.com \
    artifactregistry.googleapis.com \
    &> /dev/null || true

# 0b. Crear base de datos Firestore si no existe (modo nativo, región US)
echo "🗄️  Verificando Firestore..."
if ! gcloud firestore databases describe --database="(default)" &> /dev/null 2>&1; then
    echo "Creando base de datos Firestore (modo nativo)..."
    gcloud firestore databases create --location=nam5 --type=firestore-native &> /dev/null || true
else
    echo "Firestore ya existe."
fi

# 1. Preparar el Bucket
echo "📦 1/5 Verificando Google Cloud Storage..."
PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format="value(projectNumber)")
if ! gsutil ls -b "gs://$BUCKET_NAME" &> /dev/null; then
    echo "Creando bucket gs://$BUCKET_NAME ..."
    gsutil mb -l $PRIMARY_REGION "gs://$BUCKET_NAME"
    gsutil uniformbucketlevelaccess set on "gs://$BUCKET_NAME"
fi

gcloud storage buckets add-iam-policy-binding gs://$BUCKET_NAME \
    --member="serviceAccount:$PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
    --role="roles/storage.objectAdmin" &> /dev/null || true

gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:$PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
    --role="roles/datastore.user" &> /dev/null || true

# 2. Credenciales OAuth de Google
echo "🔑 2/5 Verificando credenciales OAuth..."
if [ -z "$OAUTH_CLIENT_ID" ] || [ -z "$OAUTH_CLIENT_SECRET" ]; then
    # Intentar recuperar desde el servicio primario desplegado
    if [ -z "$OAUTH_CLIENT_ID" ]; then
        OAUTH_CLIENT_ID=$(gcloud run services describe $SERVICE_NAME --region $PRIMARY_REGION \
            --format="yaml(spec.template.spec.containers[0].env)" 2>/dev/null \
            | grep -A1 "OAUTH_CLIENT_ID" | grep "value:" | awk '{print $2}' || echo "")
    fi
    if [ -z "$OAUTH_CLIENT_SECRET" ]; then
        OAUTH_CLIENT_SECRET=$(gcloud run services describe $SERVICE_NAME --region $PRIMARY_REGION \
            --format="yaml(spec.template.spec.containers[0].env)" 2>/dev/null \
            | grep -A1 "OAUTH_CLIENT_SECRET" | grep "value:" | awk '{print $2}' || echo "")
    fi
    if [ -z "$OAUTH_CLIENT_ID" ] || [ -z "$OAUTH_CLIENT_SECRET" ]; then
        echo "⛔ OAUTH_CLIENT_ID u OAUTH_CLIENT_SECRET no están configuradas."
        echo "   Exporta antes de correr este script:"
        echo "      export OAUTH_CLIENT_ID='tu-client-id'"
        echo "      export OAUTH_CLIENT_SECRET='tu-client-secret'"
        exit 1
    fi
    echo "✅ Credenciales OAuth recuperadas desde Cloud Run."
else
    echo "✅ Credenciales OAuth detectadas en entorno local."
fi

# Recuperar GEMINI_API_KEY
if [ -z "$GEMINI_API_KEY" ]; then
    GEMINI_API_KEY=$(gcloud run services describe $SERVICE_NAME --region $PRIMARY_REGION \
        --format="yaml(spec.template.spec.containers[0].env)" 2>/dev/null \
        | grep -A1 "GEMINI_API_KEY" | grep "value:" | awk '{print $2}' || echo "")
    if [ -n "$GEMINI_API_KEY" ]; then
        echo "✅ GEMINI_API_KEY recuperada desde Cloud Run."
    else
        echo "⚠️  GEMINI_API_KEY no encontrada. El chat de IA no funcionará."
    fi
fi

# Reusar SESSION_SECRET si ya existe (evita cerrar sesiones activas en cada deploy)
EXISTING_SECRET=$(gcloud run services describe $SERVICE_NAME --region $PRIMARY_REGION \
    --format="yaml(spec.template.spec.containers[0].env)" 2>/dev/null \
    | grep -A1 "SESSION_SECRET" | grep "value:" | awk '{print $2}' || echo "")
if [ -n "$EXISTING_SECRET" ]; then
    SESSION_SECRET="$EXISTING_SECRET"
    echo "✅ SESSION_SECRET reutilizada (sesiones activas no se interrumpen)."
else
    SESSION_SECRET=$(openssl rand -hex 32)
    echo "🔑 SESSION_SECRET generada."
fi

# CRON_KEY: reusar si existe para no romper el scheduler
EXISTING_CRON_KEY=$(gcloud run services describe $SERVICE_NAME --region $PRIMARY_REGION \
    --format="yaml(spec.template.spec.containers[0].env)" 2>/dev/null \
    | grep -A1 "CRON_API_KEY" | grep "value:" | awk '{print $2}' || echo "")
if [ -n "$EXISTING_CRON_KEY" ]; then
    CRON_KEY="$EXISTING_CRON_KEY"
    echo "✅ CRON_API_KEY reutilizada."
else
    CRON_KEY=$(openssl rand -hex 16)
    echo "🔑 CRON_API_KEY generada."
fi

ENV_VARS="GARMIN_BUCKET=$BUCKET_NAME,CRON_API_KEY=$CRON_KEY,GEMINI_API_KEY=${GEMINI_API_KEY:-},OAUTH_CLIENT_ID=$OAUTH_CLIENT_ID,OAUTH_CLIENT_SECRET=$OAUTH_CLIENT_SECRET,SESSION_SECRET=$SESSION_SECRET"

# 3. Construir imagen una sola vez y desplegar en ambas regiones
echo "☁️  3/5 Construyendo imagen y desplegando en ambas regiones..."

# Crear repositorio de Artifact Registry si no existe
AR_REPO="garmin-dashboard"
gcloud artifacts repositories create $AR_REPO \
    --repository-format=docker \
    --location=$PRIMARY_REGION \
    --quiet &> /dev/null || true

IMAGE="$PRIMARY_REGION-docker.pkg.dev/$PROJECT_ID/$AR_REPO/$SERVICE_NAME:latest"

echo "   📦 Construyendo imagen con Cloud Buildpacks..."
gcloud builds submit --pack image=$IMAGE --quiet

echo "   🌎 Desplegando en $PRIMARY_REGION (primaria)..."
PRIMARY_URL=$(gcloud run deploy $SERVICE_NAME \
    --image $IMAGE \
    --region $PRIMARY_REGION \
    --allow-unauthenticated \
    --timeout=900 \
    --memory=2Gi \
    --set-env-vars="$ENV_VARS" \
    --format="value(status.url)")
echo "   ✅ $PRIMARY_REGION: $PRIMARY_URL"

echo "   🌎 Desplegando en $SECONDARY_REGION (respaldo)..."
SECONDARY_URL=$(gcloud run deploy $SERVICE_NAME \
    --image $IMAGE \
    --region $SECONDARY_REGION \
    --allow-unauthenticated \
    --timeout=900 \
    --memory=2Gi \
    --set-env-vars="$ENV_VARS" \
    --format="value(status.url)")
echo "   ✅ $SECONDARY_REGION: $SECONDARY_URL"

# 4. Cloud Scheduler solo en región primaria (evita doble ejecución del cron)
echo "⏰ 4/5 Configurando cron job en región primaria ($PRIMARY_REGION)..."

JOB_NAME="garmin-hourly-refresh"
gcloud scheduler jobs delete $JOB_NAME --location $PRIMARY_REGION --quiet &> /dev/null || true
gcloud scheduler jobs create http $JOB_NAME \
    --location $PRIMARY_REGION \
    --schedule="0 5 * * *" \
    --time-zone="America/Mexico_City" \
    --uri="$PRIMARY_URL/internal/cron-refresh?apikey=$CRON_KEY" \
    --http-method=GET \
    --attempt-deadline=840s

WARMUP_JOB="garmin-token-warmup"
gcloud scheduler jobs delete $WARMUP_JOB --location $PRIMARY_REGION --quiet &> /dev/null || true
gcloud scheduler jobs create http $WARMUP_JOB \
    --location $PRIMARY_REGION \
    --schedule="0 8 * * *" \
    --time-zone="America/Mexico_City" \
    --uri="$PRIMARY_URL/internal/token-warmup?apikey=$CRON_KEY" \
    --http-method=GET \
    --attempt-deadline=840s

# 5. Inicializar configuración de Firestore
echo "🗄️  5/5 Inicializando configuración en Firestore..."
python3 - <<PYEOF
import os
os.environ.setdefault('GOOGLE_CLOUD_PROJECT', '$PROJECT_ID')
try:
    from google.cloud import firestore
    db = firestore.Client(project='$PROJECT_ID')
    db.collection('system').document('config').set({
        'max_refresh_today': 10,
        'max_users': 20,
    }, merge=True)
    print("   ✅ Configuración de Firestore inicializada.")
except Exception as e:
    print(f"   ⚠️  No se pudo inicializar Firestore: {e}")
PYEOF

echo "=========================================================="
echo "🎉 ¡Despliegue Multi-Región Completado!"
echo ""
echo "🌎 Región primaria  ($PRIMARY_REGION):  $PRIMARY_URL"
echo "🌎 Región respaldo  ($SECONDARY_REGION): $SECONDARY_URL"
echo ""
echo "⏰ Cron activo en: $PRIMARY_REGION"
echo ""
echo "📋 CAMBIAR A REGIÓN DE RESPALDO (si la primaria tiene problemas):"
echo "   Actualiza el redirect URI en Google Console a:"
echo "   $SECONDARY_URL/auth/callback"
echo "=========================================================="
