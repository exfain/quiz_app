from django.urls import path

from . import views
from . import browser
from . import quiz_transport
from . import oral_transport
from .oral_material import ORAL_GAMES
from .turn_material import TURN_GAMES
from . import turn_transport
from . import input_transport
from . import control_transport
from . import encounters
from . import tournament_transport
from .control_games import CONTROL_GAMES
from .input_material import INPUT_GAMES

app_name = "duel"

_unit = "api/estimation/<str:session_code>/<uuid:instance_id>/units/<int:unit_id>/"
urlpatterns = [
    path('tournament/<str:session_code>/<uuid:tournament_id>/<str:role>/', tournament_transport.page, name='tournament_page'),
    path('api/tournament/<str:session_code>/<uuid:tournament_id>/<str:role>/', tournament_transport.api, name='tournament_api'),
    path('api/encounter/<str:session_code>/<str:role>/', encounters.routing, name='encounter_routing'),
    path("api/input-image/<str:session_code>/<uuid:instance_id>/<int:unit_id>/<str:role>/", input_transport.image, name="input_image"),
    path("api/oral-image/<str:session_code>/<uuid:instance_id>/<int:unit_id>/<str:role>/", oral_transport.image, name="oral_image"),
    path("quiz/<str:session_code>/<uuid:instance_id>/<str:role>/", browser.match_page, {"game_key": "quiz"}, name="quiz_match_page"),
    path("api/quiz/<str:session_code>/<uuid:instance_id>/<str:role>/", browser.match_api, {"game_key": "quiz"}, name="quiz_match_api"),
    path("api/quiz/<str:session_code>/<uuid:instance_id>/units/<int:unit_id>/participant/", quiz_transport.unit_api, {"role": "participant"}, name="quiz_participant"),
    path("api/quiz/<str:session_code>/<uuid:instance_id>/units/<int:unit_id>/host/", quiz_transport.unit_api, {"role": "host"}, name="quiz_host"),
    path("api/quiz/<str:session_code>/<uuid:instance_id>/units/<int:unit_id>/spectator/", quiz_transport.unit_api, {"role": "spectator"}, name="quiz_spectator"),
    path("api/session/<str:session_code>/", browser.session_api, name="session_api"),
    path("estimation/<str:session_code>/<uuid:instance_id>/<str:role>/", browser.match_page, name="match_page"),
    path("api/estimation/<str:session_code>/<uuid:instance_id>/<str:role>/", browser.match_api, name="match_api"),
    path(_unit + "host/", views.estimation_host, name="estimation_host"),
    path(_unit + "participant/", views.estimation_participant, name="estimation_participant"),
    path(_unit + "spectator/", views.estimation_spectator, name="estimation_spectator"),
]
for key in (*ORAL_GAMES, *TURN_GAMES, *INPUT_GAMES, *CONTROL_GAMES):
    urlpatterns += [
        path(f"{key}/<str:session_code>/<uuid:instance_id>/<str:role>/", browser.match_page, {"game_key":key}, name=key+"_match_page"),
        path(f"api/{key}/<str:session_code>/<uuid:instance_id>/<str:role>/", browser.match_api, {"game_key":key}, name=key+"_match_api"),
    ]
    for role in ("host","participant","spectator"):
        urlpatterns.append(path(f"api/{key}/<str:session_code>/<uuid:instance_id>/units/<int:unit_id>/{role}/",
            control_transport.unit_api if key in CONTROL_GAMES else input_transport.unit_api if key in INPUT_GAMES else turn_transport.unit_api if key in TURN_GAMES else oral_transport.unit_api, {"role":role,"game_key":key}, name=key+"_"+role))
