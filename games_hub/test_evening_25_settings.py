"""Independent scoring oracle for 25 official identities across whole evenings."""
from decimal import Decimal
from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from . import test_session_evening_simulations as reference
from .models import HubParticipant
from .views import get_leaderboard_data
from .session_results import freeze_step_result


class TwentyFivePlayerSettingsTests(TestCase):
    _session=reference.SessionEveningSimulationTests._session
    _complete_check_in=reference.SessionEveningSimulationTests._complete_check_in
    _add_completed_quiz_game=reference.SessionEveningSimulationTests._add_completed_quiz_game

    def setUp(self):
        self.user=User.objects.create_user('evening-matrix-host')

    def exercise(self,mode,weighting,step,cap):
        names=[f'Team {i:02d}' for i in range(1,26)]
        hub=self._session(f'M25{mode[0]}{weighting[0]}{str(step).replace(".","")}',mode,weighting)
        hub.weighting_step=float(step);hub.weighting_cap=float(cap);hub.save()
        self._complete_check_in(hub,names)
        raw={name:Decimal(0) for name in names};expected=raw.copy()
        for number in range(1,6):
            scores={name:(i*(number+1)+number)%7 for i,name in enumerate(names)}
            # One official participant may have no answer rows in this game.
            scores[names[-1]]=0
            game,game_step=self._add_completed_quiz_game(hub,number-1,f'Spiel {number}',{n:v for n,v in scores.items() if n!=names[-1]})
            weight=min(Decimal(1)+step*(number-1),cap) if weighting=='linear_cap' else Decimal(1)
            for name,score in scores.items():
                raw[name]+=score
                base=25-sum(other>score for other in scores.values()) if mode=='ranking' else score
                expected[name]+=Decimal(base)*weight
            data=get_leaderboard_data(hub)
            self.assertEqual(len(data['participants']),25)
            self.assertEqual(Decimal(str(data['instances'][f'step:{game_step.pk}']['weight'])),weight)
            for row in data['participants']:
                self.assertEqual(Decimal(str(row['total_score'])),raw[row['name']])
                self.assertEqual(Decimal(str(row['weighted_score'])),expected[row['name']])
            # Snapshot values remain stable after later source edits.
            freeze_step_result(game_step,game)
            game.participants.filter(name=names[0]).update(total_score=999)
        HubParticipant.objects.create(session=hub,nickname='Zu spät',scoring_eligible=False)
        HubParticipant.objects.filter(session=hub,nickname=names[-1]).update(score_adjustment=-2)
        hub.ended_at=timezone.now();hub.is_active=False;hub.save();hub.refresh_from_db()
        data=get_leaderboard_data(hub)
        self.assertEqual({p['name'] for p in data['participants']},set(names))
        for row in data['participants']:
            adjusted=expected[row['name']]-(2 if row['name']==names[-1] else 0)
            self.assertEqual(Decimal(str(row['total_with_adjustment'])),adjusted)
            rank=1+sum((value-(2 if name==names[-1] else 0))>adjusted for name,value in expected.items())
            self.assertEqual(row['rank'],rank)

    def test_simple_without_weighting(self):self.exercise('simple','none',Decimal('0.15'),Decimal('2'))
    def test_simple_linear_weighting(self):self.exercise('simple','linear_cap',Decimal('0.15'),Decimal('2'))
    def test_simple_reaches_cap(self):self.exercise('simple','linear_cap',Decimal('0.30'),Decimal('1.25'))
    def test_ranking_without_weighting(self):self.exercise('ranking','none',Decimal('0.15'),Decimal('2'))
    def test_ranking_linear_weighting(self):self.exercise('ranking','linear_cap',Decimal('0.15'),Decimal('2'))
    def test_ranking_reaches_cap(self):self.exercise('ranking','linear_cap',Decimal('0.30'),Decimal('1.25'))
