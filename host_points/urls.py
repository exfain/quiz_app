from django.urls import path

from . import views

app_name = 'host_points'

urlpatterns = [
    path('join/', views.host_points_join_view, name='join'),
    path('play/<str:room_code>/<str:participant_name>/', views.host_points_play, name='play'),
    path('result/<str:room_code>/<str:participant_name>/', views.host_points_result, name='result'),
    path('state/<str:room_code>/', views.host_points_state, name='state'),
    path('adjust-score/<str:room_code>/', views.host_points_adjust_score, name='adjust_score'),
]
