from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r'ws/where/(?P<room_code>\w+)/$', consumers.WhereConsumer.as_asgi()),
]