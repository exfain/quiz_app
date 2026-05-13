import requests
from django.core.management.base import BaseCommand
from django.conf import settings

class Command(BaseCommand):
    help = 'Backs up local SQLite database via REST API'

    def handle(self, *args, **options):
        # 1. Configuration
        project_id = "czp0is8vvz.g1"
        api_key = "Q6b4jHPxkl7EaoVUVRaOsp1i7WPddYN1xAE1IhD4nv8"
        db_name = "games-website-backup.sqlite"
        local_db_path = settings.DATABASES['default']['NAME']
        print(local_db_path)
        
        # 2. Construct URL and Headers
        # Note: Port 8090 is typically the default for the SQLite Cloud Web API
        url = f"https://{project_id}.sqlite.cloud/v2/weblite/{db_name}"
        
        headers = {
            'Content-Type': 'application/octet-stream',
            'Authorization': f'Bearer sqlitecloud://{project_id}.sqlite.cloud:8860?apikey={api_key}'
        }

        self.stdout.write(f"Uploading {local_db_path} to {url}...")

        try:
            # 3. Read file and POST binary data
            with open(local_db_path, 'rb') as f:

                # Add location parameter with database name
                params = {'location': db_name}

                response = requests.post(url, headers=headers, data=f, params=params)

            # 4. Check results
            if response.status_code in [200, 201]:
                self.stdout.write(self.style.SUCCESS("Backup successful!"))
            else:
                self.stdout.write(self.style.ERROR(f"Failed: {response.status_code} - {response.text}"))
                
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"An error occurred: {e}"))