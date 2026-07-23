from django.urls import re_path

from . import consumers


websocket_urlpatterns = [
    re_path(r'ws/wann-war-das/(?P<room_code>\w+)/$', consumers.WannWarDasConsumer.as_asgi()),
]
