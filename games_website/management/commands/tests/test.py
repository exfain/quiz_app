from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from googleapiclient.http import MediaFileUpload
import io
from googleapiclient.errors import HttpError

scope = ['https://www.googleapis.com/auth/drive']
service_account_json_key = '/home/codex/Projects/games-website/service_account.json'
credentials = service_account.Credentials.from_service_account_file(
                              filename=service_account_json_key, 
                              scopes=scope)
service = build('drive', 'v3', credentials=credentials)

file_metadata = {'name': 'gdrive_upload.py', 'parents': ['1_XebhiJ24FYLwQvMdaxr2am5looLNYu1']}
media = MediaFileUpload('/home/codex/Projects/games-website/games_website/management/commands/gdrive_upload.py',
                        mimetype='text/csv')

file = service.files().create(body=file_metadata, media_body=media,
                              fields='id', supportsAllDrives=True).execute()