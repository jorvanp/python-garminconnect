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
REGION="us-central1"
BUCKET_NAME="${PROJECT_ID}-garmin-data"

echo "=========================================================="
echo "🚀 Iniciando despliegue de Garmin Dashboard en GCP"
echo "Proyecto: $PROJECT_ID"
echo "Bucket: $BUCKET_NAME"
echo "=========================================================="

# 1. Preparar el Bucket (Si no existe, se crea)
echo "📦 1/4 Verificando Google Cloud Storage..."
if ! gsutil ls -b "gs://$BUCKET_NAME" &> /dev/null; then
    echo "Creando bucket gs://$BUCKET_NAME ..."
    gsutil mb -l $REGION "gs://$BUCKET_NAME"
    
    # Hacer el bucket privado pero el contenido solo accesible por la app
    gsutil uniformbucketlevelaccess set on "gs://$BUCKET_NAME"
    
    # Otorgar acceso de lectura/escritura a la cuenta por defecto de Cloud Run
    PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format="value(projectNumber)")
    gcloud storage buckets add-iam-policy-binding gs://$BUCKET_NAME \
        --member="serviceAccount:$PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
        --role="roles/storage.objectAdmin"
else
    echo "Bucket gs://$BUCKET_NAME ya existe."
    PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format="value(projectNumber)")
    gcloud storage buckets add-iam-policy-binding gs://$BUCKET_NAME \
        --member="serviceAccount:$PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
        --role="roles/storage.objectAdmin"
fi

# 2. Subir Archivos Iniciales al Bucket
# Token (si existe localmente) y Data JSON
echo "🔑 2/4 Sincronizando datos locales hacia la nube..."
if [ -d "$HOME/.garminconnect" ]; then
    echo "Subiendo tokens de sesión de Garmin..."
    gsutil -m cp -r "$HOME/.garminconnect/*" "gs://$BUCKET_NAME/tokens/"
else
    echo "⚠️  No se encontró ~/.garminconnect. El servicio requerirá login manual o fallará la recarga."
fi

if [ -f "training_data_monthly.json" ]; then
    echo "Subiendo JSON base..."
    gsutil cp training_data_monthly.json "gs://$BUCKET_NAME/"
fi

# 3. Desplegar en Cloud Run (directamente desde código fuente)
echo "☁️ 3/4 Desplegando aplicación en Cloud Run..."
# Requiere habilitar las APIs la primera vez
gcloud services enable run.googleapis.com cloudbuild.googleapis.com cloudscheduler.googleapis.com &> /dev/null || true

# Generar un pasword simple para el cronjob
CRON_KEY=$(openssl rand -hex 16)

SERVICE_URL=$(gcloud run deploy $SERVICE_NAME \
    --source . \
    --region $REGION \
    --allow-unauthenticated \
    --set-env-vars=GARMIN_BUCKET=$BUCKET_NAME,CRON_API_KEY=$CRON_KEY,GEMINI_API_KEY=${GEMINI_API_KEY:-} \
    --format="value(status.url)")

echo "✅ Servicio desplegado en: $SERVICE_URL"

# 4. Configurar el Cloud Scheduler (Cron Job a las 4am)
echo "⏰ 4/4 Configuracion el Trabajo Programado (4:00 AM)..."
JOB_NAME="garmin-daily-refresh"

# Borrar si ya existe para re-crearlo
gcloud scheduler jobs delete $JOB_NAME --location $REGION --quiet &> /dev/null || true

gcloud scheduler jobs create http $JOB_NAME \
    --location $REGION \
    --schedule="0 4 * * *" \
    --time-zone="America/Costa_Rica" \
    --uri="$SERVICE_URL/refresh?apikey=$CRON_KEY" \
    --http-method=GET

echo "=========================================================="
echo "🎉 ¡Despliegue Completado Exitosamente!"
echo "🌎 Tu dashboard está en vivo en: $SERVICE_URL"
echo "🔄 Recarga manual en: $SERVICE_URL/refresh?apikey=$CRON_KEY"
echo "=========================================================="
