from django.conf import settings
from django.core.management.base import BaseCommand
import os
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive

class Command(BaseCommand):
    help = "Fetch a specific backup file from Google Drive"

    def handle(self, *args, **options):
        # 1. Authentication (Keeping your working service account logic)
        auth_settings = {
            "client_config_backend": "service",
            "service_config": {
                "client_json_file_path": "service_account.json",
            }
        }
        gauth = GoogleAuth(settings=auth_settings)
        gauth.ServiceAuth()
        drive = GoogleDrive(gauth)

        # 2. Configuration
        folder_id = "1_XebhiJ24FYLwQvMdaxr2am5looLNYu1"
        target_filename = "game_results.xlsx" # Example name
        download_path = os.path.join(settings.BASE_DIR, "game_results.xlsx")

        # 3. Search for the file in the specific folder
        # The query 'q' filters by parent folder, filename, and ensures it's not trashed
        query = f"'{folder_id}' in parents and title = '{target_filename}' and trashed = false"
        file_list = drive.ListFile({'q': query}).GetList()

        if not file_list:
            self.stdout.write(self.style.ERROR(f"File '{target_filename}' not found in folder."))
            return

        # 4. Fetch the first match and download
        gfile = file_list[0]
        self.stdout.write(f"Downloading {gfile['title']} (ID: {gfile['id']})...")
        
        # This pulls the actual content into your local file path
        gfile.GetContentFile(download_path)

        self.stdout.write(self.style.SUCCESS(f"Successfully downloaded to {download_path}"))