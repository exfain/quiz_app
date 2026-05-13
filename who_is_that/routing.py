from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r'ws/who_that/(?P<room_code>\w+)/$', consumers.WhoThatConsumer.as_asgi()),
]