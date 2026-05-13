"""
ASGI config for games_website project.
"""

import os
from django.core.asgi import get_asgi_application
from django.contrib.staticfiles.handlers import ASGIStaticFilesHandler
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'games_website.settings')

# Initialize Django ASGI application early to ensure the AppRegistry
# is populated before importing code that may import ORM models.
django_asgi_app = ASGIStaticFilesHandler(get_asgi_application())

# Import routing from all apps
import QuizGame.routing
import Assign.routing
import where_is_this.routing
import Estimation.routing
import who_is_lying.routing
import who_is_that.routing
import black_jack_quiz.routing
import games_hub.routing
import clue_rush.routing
import sorting_ladder.routing

# Combine all WebSocket URL patterns
websocket_urlpatterns = []
websocket_urlpatterns.extend(QuizGame.routing.websocket_urlpatterns)
websocket_urlpatterns.extend(Assign.routing.websocket_urlpatterns)
websocket_urlpatterns.extend(where_is_this.routing.websocket_urlpatterns)
websocket_urlpatterns.extend(Estimation.routing.websocket_urlpatterns)
websocket_urlpatterns.extend(who_is_lying.routing.websocket_urlpatterns)
websocket_urlpatterns.extend(who_is_that.routing.websocket_urlpatterns)
websocket_urlpatterns.extend(black_jack_quiz.routing.websocket_urlpatterns)
websocket_urlpatterns.extend(games_hub.routing.websocket_urlpatterns)
websocket_urlpatterns.extend(clue_rush.routing.websocket_urlpatterns)
websocket_urlpatterns.extend(sorting_ladder.routing.websocket_urlpatterns)

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": AuthMiddlewareStack(
        URLRouter(
            websocket_urlpatterns
        )
    ),
})