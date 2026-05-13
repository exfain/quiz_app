from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r"ws/hub/(?P<session_code>\w+)/$", consumers.HubConsumer.as_asgi()),
]
