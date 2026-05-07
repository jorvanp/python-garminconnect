#!/usr/bin/env python3
"""
Script de login local de Garmin.
Ejecutar desde tu Mac (NO desde Cloud Run).

Uso:
    .venv/bin/python local_garmin_login.py <email> <uid_google>

El script:
  1. Hace login con tus credenciales de Garmin (desde tu IP local)
  2. Guarda los tokens en /tmp/garmin_tokens_<uid>/
  3. Sube los tokens a GCS: gs://<bucket>/users/<uid>/tokens/

Ejemplo:
    .venv/bin/python local_garmin_login.py jorvanp@gmail.com 103635582291846257919
"""
import getpass
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

BUCKET = os.environ.get("GARMIN_BUCKET", "garminconnect-489920-garmin-data")


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    garmin_email = sys.argv[1]
    user_uid = sys.argv[2]

    print(f"\nConectando Garmin para: {garmin_email}")
    print(f"UID de Google: {user_uid}")
    print(f"Bucket GCS: {BUCKET}\n")

    garmin_password = getpass.getpass("Contraseña de Garmin Connect: ")

    print("\nAutenticando con Garmin...")
    from garminconnect import Garmin
    garmin = Garmin(email=garmin_email, password=garmin_password)
    garmin.login()
    print("✅ Login exitoso\n")

    tmp_dir = tempfile.mkdtemp(prefix=f"garmin_tokens_{user_uid}_")
    garmin.garth.dump(tmp_dir)
    print(f"Tokens guardados en: {tmp_dir}")
    for f in Path(tmp_dir).iterdir():
        print(f"  {f.name}: {f.stat().st_size} bytes")

    print(f"\nSubiendo tokens a GCS gs://{BUCKET}/users/{user_uid}/tokens/ ...")
    from gcs_helper import GCSHelper
    gcs = GCSHelper(BUCKET)
    success = gcs.upload_directory(tmp_dir, f"users/{user_uid}/tokens/")
    shutil.rmtree(tmp_dir, ignore_errors=True)

    if success:
        print("✅ Tokens subidos a GCS correctamente.")
        print("\nListo. El usuario puede entrar a la app y el refresh funcionará automáticamente.")
    else:
        print("❌ Error al subir tokens a GCS. Verifica tus credenciales de gcloud.")
        sys.exit(1)


if __name__ == "__main__":
    main()
