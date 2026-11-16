"""Explicit adapters for manual games and the new bid game."""
from buzzer.models import BuzzerGame
from host_points.models import HostPointsGame
from .models import BidGame

CONTROL_GAMES = {"bid": (BidGame, "Gebotsspiel"), "buzzer": (BuzzerGame, "Buzzer"), "host_points": (HostPointsGame, "Host-Punktevergabe")}


def validate_config(key, config):
    if key == "host_points" and config.mode.value != "target_score":
        raise ValueError("Host-Punktevergabe unterstützt vorerst nur Zielpunktzahl; eine Best-of-Rundensiegerregel fehlt.")
