from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r"ws/clue-rush/(?P<room_code>\w+)/$", consumers.ClueRushGameConsumer.as_asgi()),
]
