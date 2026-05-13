from django.urls import path
from . import views

app_name = 'where_is_this'

urlpatterns = [
    # Where Quiz Join Flow
    path('join/', views.where_join_view, name='join'),
    path('check-room/<str:room_code>/', views.check_room_code, name='check_room'),
    
    # Quiz Play Flow
    path('play/<str:room_code>/<str:participant_name>/', views.where_play, name='play'),
    path('result/<str:room_code>/<str:participant_name>/', views.where_result, name='result'),
    
    # Quiz Actions (AJAX endpoints)
    path('submit-answer/<str:room_code>/<str:participant_name>/', views.submit_answer, name='submit_answer'),
    path('status/<str:room_code>/<str:participant_name>/', views.get_quiz_status, name='quiz_status'),
    path('leave/<str:room_code>/<str:participant_name>/', views.leave_quiz, name='leave_quiz'),
    
    # Public API endpoints
    path('api/<str:room_code>/participants/', views.api_quiz_participants, name='api_participants'),
    path('api/<str:room_code>/leaderboard/', views.api_quiz_leaderboard, name='api_leaderboard'),
]