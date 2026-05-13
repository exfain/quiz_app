from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r'ws/blackjack/(?P<room_code>\w+)/$', consumers.BlackJackConsumer.as_asgi()),
]