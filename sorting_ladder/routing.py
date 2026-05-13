from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r'ws/sorting-ladder/(?P<room_code>\w+)/$', consumers.SortingLadderGameConsumer.as_asgi()),
]