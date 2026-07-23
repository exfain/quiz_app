from django.urls import re_path

from . import consumers

websocket_urlpatterns = [
    re_path(r'ws/host-points/(?P<room_code>\w+)/$', consumers.HostPointsConsumer.as_asgi()),
]
