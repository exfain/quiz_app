"""Local verification only: file SQLite, isolated media and no external services.

Use --settings=games_website.test_settings. QUIZ_QA_ROOT optionally selects the
temporary directory shared by test subprocesses, never the normal app database.
"""
import os
import tempfile
from pathlib import Path

from .settings import *

QA_ROOT = Path(os.environ.get('QUIZ_QA_ROOT') or tempfile.mkdtemp(prefix='quiz-qa-')).resolve()
os.environ['QUIZ_QA_ROOT'] = str(QA_ROOT)
QA_ROOT.mkdir(parents=True, exist_ok=True)
DATABASES = {alias: {
    'ENGINE': 'django.db.backends.sqlite3',
    'NAME': QA_ROOT / f'{alias}-runtime.sqlite3',
    'OPTIONS': {'timeout': 20, 'transaction_mode': 'IMMEDIATE'},
    'TEST': {'NAME': QA_ROOT / f'{alias}-tests.sqlite3'},
} for alias in ('default', 'supabase')}
DATABASES['supabase']['TEST']['MIGRATE'] = False
MEDIA_ROOT = QA_ROOT / 'media'
CHANNEL_LAYERS = {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}}
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
