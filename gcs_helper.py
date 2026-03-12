import os
from pathlib import Path
from google.cloud import storage
import logging

logger = logging.getLogger(__name__)

class GCSHelper:
    def __init__(self, bucket_name: str):
        self.bucket_name = bucket_name
        # Note: If no credentials are provided, GCS defaults to the 
        # Application Default Credentials (e.g., from Cloud Run instance)
        try:
            self.client = storage.Client()
            self.bucket = self.client.bucket(bucket_name)
        except Exception as e:
            logger.error(f"Failed to initialize GCS client: {e}")
            self.client = None
            self.bucket = None

    def download_directory(self, gcs_prefix: str, local_dir: str):
        """Downloads all files from a GCS prefix into a local directory."""
        if not self.bucket: return False
        
        try:
            blobs = self.bucket.list_blobs(prefix=gcs_prefix)
            os.makedirs(local_dir, exist_ok=True)
            
            downloaded = False
            for blob in blobs:
                # Do not download dummy/placeholder directories
                if blob.name.endswith('/'):
                    continue
                
                # Extract filename without prefix
                filename = blob.name[len(gcs_prefix):].lstrip('/')
                local_path = os.path.join(local_dir, filename)
                
                # Make sure the local directory for this specific file exists
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                
                logger.info(f"Downloading {blob.name} to {local_path}...")
                blob.download_to_filename(local_path)
                downloaded = True
                
            return downloaded
        except Exception as e:
            logger.error(f"Error downloading from GCS: {e}")
            return False

    def upload_directory(self, local_dir: str, gcs_prefix: str):
        """Uploads a local directory into a GCS prefix."""
        if not self.bucket: return False
        
        try:
            upload_count = 0
            for root, _, files in os.walk(local_dir):
                for file in files:
                    local_path = os.path.join(root, file)
                    # compute relative path from the root of local_dir
                    rel_path = os.path.relpath(local_path, local_dir)
                    gcs_path = f"{gcs_prefix.rstrip('/')}/{rel_path}"
                    
                    blob = self.bucket.blob(gcs_path)
                    logger.info(f"Uploading {local_path} to {gcs_path}...")
                    blob.upload_from_filename(local_path)
                    upload_count += 1
                    
            return upload_count > 0
        except Exception as e:
            logger.error(f"Error uploading to GCS: {e}")
            return False
            
    def load_json(self, blob_name: str):
         """Downloads a file as string."""
         if not self.bucket: return None
         try:
              blob = self.bucket.blob(blob_name)
              if blob.exists():
                   return blob.download_as_string().decode('utf-8')
              return None
         except Exception as e:
              logger.error(f"Error downloading JSON {blob_name}: {e}")
              return None
              
    def save_json(self, blob_name: str, content: str):
         """Uploads a string to GCS."""
         if not self.bucket: return False
         try:
              blob = self.bucket.blob(blob_name)
              blob.upload_from_string(content, content_type='application/json')
              return True
         except Exception as e:
              logger.error(f"Error saving JSON {blob_name}: {e}")
              return False
