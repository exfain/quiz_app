from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r'ws/assign/(?P<room_code>\w+)/$', consumers.AssignConsumer.as_asgi()),
]