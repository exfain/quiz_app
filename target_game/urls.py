from django.urls import path
from . import views, content

app_name='target_game'
urlpatterns=[path('manage/',content.manage,name='manage'),path('join/',views.join,name='join'),
    path('play/<str:room>/<str:nickname>/',views.page,{'role':'participant'},name='play'),
    path('monitor/<str:room>/',views.page,{'role':'host'},name='monitor'),
    path('spectate/<str:room>/',views.page,{'role':'spectator'},name='spectate'),
    path('api/<str:code>/<str:room>/<str:role>/',views.api,name='api')]
