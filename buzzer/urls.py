from django.urls import path

from . import views

app_name = 'buzzer'

urlpatterns = [
    path('join/', views.buzzer_join_view, name='join'),
    path('play/<str:room_code>/<str:participant_name>/', views.buzzer_play, name='play'),
    path('result/<str:room_code>/<str:participant_name>/', views.buzzer_result, name='result'),
    path('state/<str:room_code>/', views.buzzer_state, name='state'),
    path('buzz/<str:room_code>/', views.buzzer_buzz, name='buzz'),
]
