from django.urls import path

from . import views

app_name = 'wann_war_das'

urlpatterns = [
    path('join/', views.join_view, name='join'),
    path('play/<str:room_code>/<str:participant_name>/', views.play_view, name='play'),
    path('result/<str:room_code>/<str:participant_name>/', views.result_view, name='result'),
    path('state/<str:room_code>/', views.state_view, name='state'),
    path('submit/<str:room_code>/<str:participant_name>/', views.submit_answer, name='submit_answer'),
]
