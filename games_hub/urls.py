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
    path('api/session/<str:session_code>/scoring-settings/', views.update_session_scoring_settings, name='update_session_scoring_settings'),
    path('api/session/<str:session_code>/check-in/', views.session_check_in_state_api, name='session_check_in_state_api'),
    path('api/session/<str:session_code>/check-in/start/', views.start_check_in, name='start_check_in'),
    path('api/session/<str:session_code>/check-in/complete/', views.complete_check_in, name='complete_check_in'),
    path('api/session/<str:session_code>/check-in/reset/', views.reset_check_in, name='reset_check_in'),
    path('api/session/<str:session_code>/check-in/participant/', views.participant_check_in_api, name='participant_check_in_api'),
    path('api/session/<str:session_code>/check-in/set-participant/', views.set_check_in_participant, name='set_check_in_participant'),
    path('api/spectate/<str:session_code>/state/', views.spectate_session_state, name='spectate_session_state'),
    path('api/session/<str:session_code>/add-step/', views.add_step_to_session, name='add_step_to_session'),
    path('api/session/<str:session_code>/activate-game/', views.activate_session_game, name='activate_session_game'),
    path('api/session/<str:session_code>/lobby-presence/', views.session_lobby_presence_api, name='session_lobby_presence_api'),
    path('api/session/<str:session_code>/recall-countdown/', views.session_recall_countdown_state_api, name='session_recall_countdown_state_api'),
    path('api/session/<str:session_code>/start-recall-countdown/', views.start_recall_countdown, name='start_recall_countdown'),
    path('api/session/<str:session_code>/recall-to-lobby/', views.recall_session_participants_to_lobby, name='recall_session_participants_to_lobby'),
    path('api/session/<str:session_code>/participant-return-to-lobby/', views.participant_return_to_lobby, name='participant_return_to_lobby'),
    path('api/participant/score/', views.set_hub_participant_score, name='set_hub_participant_score'),
    path('api/games/<str:game_key>/questions/', views.get_available_questions, name='get_available_questions'),
    path('api/games/<str:game_key>/instances/', views.get_game_instances, name='get_game_instances'),
    path('api/session/<str:session_code>/reorder-steps/', views.reorder_steps, name='reorder_steps'),
    path('api/session/<str:session_code>/delete-step/<int:step_id>/', views.delete_step, name='delete_step'),
    path('api/session/<str:session_code>/vote/', views.submit_vote, name='submit_vote'),
    path('api/session/<str:session_code>/votes/', views.get_votes, name='get_votes'),
    path('api/session/<str:session_code>/voting/', views.configure_voting, name='configure_voting'),
]
