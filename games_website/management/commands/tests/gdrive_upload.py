from django.conf import settings
from django.core.management.base import BaseCommand
import datetime
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive

class Command(BaseCommand):

    def handle(self, *args, **options):
        # Create instance of GoogleAuth
        gauth = GoogleAuth()
        gauth.LocalWebserverAuth()

        drive = GoogleDrive(gauth)

        filename = settings.DATABASES['default']['NAME']
        today = datetime.date.today().isoformat()

        gfile = drive.CreateFile({
            "title": f"db_backup_{today}.sqlite3",
            "parents": [{"id": "1_XebhiJ24FYLwQvMdaxr2am5looLNYu1"}],
            "supportsAllDrives": True
        })

        gfile.SetContentFile(filename)
        gfile.Upload(param={"supportsAllDrives": True})

        self.stdout.write(self.style.SUCCESS("Backup uploaded"))
