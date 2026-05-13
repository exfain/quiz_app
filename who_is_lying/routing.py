from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r'ws/who/(?P<room_code>\w+)/$', consumers.WhoConsumer.as_asgi()),
]