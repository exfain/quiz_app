from django.urls import re_path

from . import consumers


websocket_urlpatterns = [
    re_path(r'ws/wer-weiss-mehr/(?P<room_code>\w+)/$', consumers.WerWeissMehrConsumer.as_asgi()),
]

