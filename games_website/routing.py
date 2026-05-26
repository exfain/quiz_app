import os
from django.core.asgi import get_asgi_application
from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator

# Set default settings module
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'games_website.settings')

# Initialize Django ASGI application early to ensure the AppRegistry
# is populated before importing code that may import ORM models.
django_asgi_app = get_asgi_application()

# Import routing after Django setup
from QuizGame.routing import websocket_urlpatterns as quiz_ws
from Estimation.routing import websocket_urlpatterns as estimation_ws
from Assign.routing import websocket_urlpatterns as assign_ws
from where_is_this.routing import websocket_urlpatterns as where_ws
from who_is_lying.routing import websocket_urlpatterns as who_ws
from who_is_that.routing import websocket_urlpatterns as who_that_ws
from black_jack_quiz.routing import websocket_urlpatterns as blackjack_ws
from clue_rush.routing import websocket_urlpatterns as clue_rush_ws
from games_hub.routing import websocket_urlpatterns as hub_ws
from sorting_ladder.routing import websocket_urlpatterns as sorting_ladder_ws
from wer_weiss_mehr.routing import websocket_urlpatterns as wer_weiss_mehr_ws

application = ProtocolTypeRouter({
    # HTTP requests are handled by Django's default ASGI application
    "http": django_asgi_app,
    
    # WebSocket requests are handled by our custom routing
    "websocket": AllowedHostsOriginValidator(
        AuthMiddlewareStack(
            URLRouter(
                quiz_ws
                + estimation_ws
                + assign_ws
                + where_ws
                + who_ws
                + who_that_ws
                + blackjack_ws
                + clue_rush_ws
                + hub_ws
                + sorting_ladder_ws
                + wer_weiss_mehr_ws
            )
        )
    ),
})
