from django.urls import path

from . import views

app_name = 'wer_weiss_mehr'

urlpatterns = [
    path('join/', views.join_view, name='join'),
    path('check-room/<str:room_code>/', views.check_room_code, name='check_room'),
    path('start/<str:room_code>/', views.start_game, name='start_game'),
    path('start-set/<str:room_code>/', views.start_game_set, name='start_game_set'),
    path('skip-tutorial-set/<str:room_code>/', views.skip_tutorial_set_view, name='skip_tutorial_set'),
    path('end-round/<str:room_code>/', views.end_round, name='end_round'),
    path('open-round/<str:room_code>/', views.open_round, name='open_round'),
    path('next-round/<str:room_code>/', views.next_round, name='next_round'),
    path('finish-set/<str:room_code>/', views.finish_current_set, name='finish_current_set'),
    path('clear-set/<str:room_code>/', views.clear_set_selection, name='clear_set_selection'),
    path('apply-correction/<str:room_code>/', views.apply_correction, name='apply_correction'),
    path('end/<str:room_code>/', views.end_game, name='end_game'),
    path('pending/<str:room_code>/', views.participant_pending_input, name='participant_pending_input'),
    path('submit/<str:room_code>/', views.participant_submit_answer, name='participant_submit_answer'),
    path('play/<str:room_code>/<str:participant_name>/', views.play, name='play'),
    path('state/<str:room_code>/', views.state, name='state'),
    path('leave/<str:room_code>/<str:participant_name>/', views.leave_game, name='leave_game'),
    path('api/<str:room_code>/participants/', views.api_participants, name='api_participants'),
]
