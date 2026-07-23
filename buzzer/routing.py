from django.urls import re_path

from . import consumers

websocket_urlpatterns = [
    re_path(r'ws/buzzer/(?P<room_code>\w+)/$', consumers.BuzzerConsumer.as_asgi()),
]
