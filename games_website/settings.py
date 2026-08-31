"""
Django settings for games_website project.
"""

from pathlib import Path
import os
from dotenv import load_dotenv

load_dotenv('secrets.env')

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', 'your_default_secret_key_for_dev')

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.environ.get('DJANGO_DEBUG', 'true').strip().lower() in {
    '1',
    'true',
    'yes',
    'on',
}

ALLOWED_HOSTS = [
    "*"
]

INSTALLED_APPS = [
    'daphne',  # Put daphne FIRST - this is key!
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'games_website',
    'admin_dashboard',
    'QuizGame.apps.QuizgameConfig',
    'who_is_lying.apps.WhoIsLyingConfig',
    'where_is_this.apps.WhereIsThisConfig',
    'Assign.apps.AssignConfig',
    'Estimation.apps.EstimationConfig',
    'who_is_that.apps.WhoIsThatConfig',
    'black_jack_quiz.apps.BlackJackQuizConfig',
    'games_hub.apps.GamesHubConfig',
    'channels',
    'clue_rush.apps.ClueRushConfig',
    'sorting_ladder.apps.SortingLadderConfig',
    'wer_weiss_mehr.apps.WerWeissMehrConfig',
    'buzzer.apps.BuzzerConfig',
    'host_points.apps.HostPointsConfig',
    'wann_war_das.apps.WannWarDasConfig',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'games_website.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'libraries': {
                'session_game_tags': 'games_hub.templatetags.session_game_tags',
            },
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'games_website.wsgi.application'
ASGI_APPLICATION = 'games_website.asgi.application'

# Database
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
        # Live-browser and Channels requests use separate threads against the
        # same SQLite file. Let short concurrent writes serialize cleanly.
        'OPTIONS': {
            'timeout': 20,
        },
        'TEST': {
            # Dateibasierte Test-DB – erforderlich für ChannelsLiveServerTestCase
            # (In-Memory-SQLite kann nicht zwischen ASGI-Server-Thread und
            # Test-Thread geteilt werden)
            'NAME': BASE_DIR / 'test_db.sqlite3',
        },
    },
    'supabase': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.getenv("SUPABASE_DB_NAME"),
        'USER': os.getenv("SUPABASE_DB_USER"),
        'PASSWORD': os.getenv("SUPABASE_DB_PASSWORD"),
        'HOST': os.getenv("SUPABASE_DB_HOST"),
        'PORT': os.getenv("SUPABASE_DB_PORT"),
    }
}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [
    BASE_DIR / 'static',
]

# Media files (user uploads)
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Browser sockets heartbeat every 15 seconds; six missed heartbeats expire
# connection presence without changing the participant's game membership.
SOCKET_PRESENCE_TTL_SECONDS = 90

# Channel layers use process-local memory only for local development and tests.
# Production defaults to Redis so broadcasts remain consistent across workers.
CHANNEL_LAYER_BACKEND = os.environ.get(
    'DJANGO_CHANNEL_LAYER',
    'memory' if DEBUG else 'redis',
).strip().lower()
if CHANNEL_LAYER_BACKEND == 'memory':
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels.layers.InMemoryChannelLayer",
        }
    }
elif CHANNEL_LAYER_BACKEND == 'redis':
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {
                "hosts": [os.environ.get(
                    'CHANNEL_REDIS_URL',
                    'redis://127.0.0.1:6379/0',
                )],
            },
        },
    }
else:
    raise ValueError(
        "DJANGO_CHANNEL_LAYER must be either 'memory' or 'redis'."
    )

# Login URLs
LOGIN_URL = '/admin-dashboard/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/'
