from django.urls import path
from . import views

app_name = 'games_hub'

urlpatterns = [
    path('create/', views.create_session, name='create_session'),
    path('join/', views.join_session, name='join_session'),
    path('lobby/<str:session_code>/', views.lobby, name='lobby'),
    path('monitor/<str:session_code>/', views.monitor, name='monitor'),
    path('spectate/<str:session_code>/', views.spectate_session, name='spectate_session'),
    path('session/<str:session_code>/leaderboard/', views.session_leaderboard, name='session_leaderboard'),
    # API endpoints
    path('api/session/<str:session_code>/leaderboard/', views.session_leaderboard_api, name='session_leaderboard_api'),
    path('api/session/<str:session_code>/add-step/', views.add_step_to_session, name='add_step_to_session'),
    path('api/participant/score/', views.set_hub_participant_score, name='set_hub_participant_score'),
    path('api/games/<str:game_key>/questions/', views.get_available_questions, name='get_available_questions'),
    path('api/games/<str:game_key>/instances/', views.get_game_instances, name='get_game_instances'),
    path('api/session/<str:session_code>/reorder-steps/', views.reorder_steps, name='reorder_steps'),
    path('api/session/<str:session_code>/delete-step/<int:step_id>/', views.delete_step, name='delete_step'),
    path('api/session/<str:session_code>/vote/', views.submit_vote, name='submit_vote'),
    path('api/session/<str:session_code>/votes/', views.get_votes, name='get_votes'),
]
