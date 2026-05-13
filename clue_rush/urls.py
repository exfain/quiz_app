from django.urls import path
from . import views

app_name = 'clue_rush'

urlpatterns = [
    # Join flow
    path('join/', views.join_view, name='join'),
    path('check-room/<str:room_code>/', views.check_room_code, name='check_room'),

    # Play flow
    path('play/<str:room_code>/<str:participant_name>/', views.play, name='play'),
    path('result/<str:room_code>/<str:participant_name>/', views.result, name='result'),

    # Actions
    path('submit-guess/<str:room_code>/<str:participant_name>/', views.submit_guess, name='submit_guess'),
    path('status/<str:room_code>/<str:participant_name>/', views.get_game_status, name='game_status'),
    path('leave/<str:room_code>/<str:participant_name>/', views.leave_game, name='leave_game'),

    # Public API endpoints
    path('api/<str:room_code>/participants/', views.api_participants, name='api_participants'),
    path('api/<str:room_code>/leaderboard/', views.api_leaderboard, name='api_leaderboard'),
]
