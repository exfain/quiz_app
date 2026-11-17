"""Tutorials use the current signed socket and map round, not legacy events."""
from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import User
from django.test import TransactionTestCase
from django.utils import timezone

from games_hub.models import HubGameStep, HubParticipant, HubSession
from games_hub import tutorial_runtime, unit_tutorial_runtime
from .consumers import WhereConsumer
from .identity import issue_player_token
from .models import WhereQuiz, WhereQuestion, WhereParticipant
from . import runtime


class WhereTutorialTransportTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user('where-tutorial-host', is_staff=True)
        self.hub = HubSession.objects.create(code='WTUTORIAL', creator=self.user, is_active=True,
            started_at=timezone.now(), check_in_status='completed', locked_participant_count=1)
        HubParticipant.objects.create(session=self.hub, nickname='Alpha', scoring_eligible=True)
        self.question = WhereQuestion.objects.create(created_by=self.user, question_text='Training Berlin',
            correct_latitude=52.52, correct_longitude=13.4, time_limit=60)
        self.game = WhereQuiz.objects.create(creator=self.user, tutorial_enabled=True, tutorial_text='Karte ausprobieren',
            tutorial_question=self.question)
        self.regular = WhereQuestion.objects.create(created_by=self.user, question_text='Wertungsfrage',
            correct_latitude=0, correct_longitude=0, time_limit=60)
        self.game.selected_questions.add(self.regular)
        HubGameStep.objects.create(session=self.hub, order=0, game_key='where', room_code=self.game.room_code)

    def test_tutorial_start_ack_rejoin_warning_and_unscored_round_use_real_transport(self):
        async def scenario():
            host = WebsocketCommunicator(WhereConsumer.as_asgi(), '/ws/where/test/')
            host.scope.update(user=self.user, url_route={'kwargs': {'room_code':self.game.room_code}})
            await host.connect(); await host.receive_json_from()
            await host.send_json_to({'type':'host_join', 'hub_session':self.hub.code})
            await host.receive_json_from()
            await host.send_json_to({'type':'admin_start_quiz','show_tutorial':True,'play_tutorial':True,'start_request_id':'start'})
            # The start broadcasts a state as well as its explicit acknowledgement.
            while (await host.receive_json_from()).get('type') != 'host_start_confirmed':
                pass
            return host
        # Keep one event loop for the complete socket lifecycle.
        async def full():
            host = await scenario()
            try:
                from channels.db import database_sync_to_async
                player = await database_sync_to_async(WhereParticipant.objects.get)(quiz=self.game, name='Alpha')
                socket = WebsocketCommunicator(WhereConsumer.as_asgi(), '/ws/where/test/')
                socket.scope.update(url_route={'kwargs':{'room_code':self.game.room_code}})
                await socket.connect(); await socket.receive_json_from()
                await socket.send_json_to({'type':'participant_join','participant_name':'Alpha',
                    'hub_session':self.hub.code,'player_token':issue_player_token(player)})
                state = await socket.receive_json_from()
                self.assertEqual(state['tutorial']['tutorial_text'],'Karte ausprobieren')
                await host.send_json_to({'type':'admin_send_question','question_id':self.regular.pk})
                while True:
                    warning = await host.receive_json_from()
                    if warning['type']=='tutorial_ack_warning':break
                self.assertEqual(warning['completed'],0)
                await socket.send_json_to({'type':'tutorial_completed'})
                while True:
                    state=await socket.receive_json_from()
                    if state.get('type')=='where_state' and state.get('tutorial') is None:break
                await host.send_json_to({'type':'admin_send_question','question_id':self.regular.pk})
                while True:
                    state=await socket.receive_json_from()
                    if state.get('question'):break
                self.assertEqual(state['question']['id'],self.question.pk)
                self.assertTrue(state['is_tutorial_round'])
                self.assertEqual(state['scoreboard'],[])
                await database_sync_to_async(runtime.reveal)(self.game)
                self.assertFalse((await database_sync_to_async(unit_tutorial_runtime.get_unit_tutorial_state)(
                    'where',self.game.room_code,self.hub.code))['current_unit_is_tutorial'])
                self.assertEqual(await database_sync_to_async(lambda:player.quiz.participants.get(pk=player.pk).total_score)(),0)
                await socket.disconnect()
            finally:await host.disconnect()
        async_to_sync(full)()

    def test_missing_tutorial_does_not_activate_game(self):
        self.game.tutorial_question=None;self.game.save()
        async def scenario():
            socket=WebsocketCommunicator(WhereConsumer.as_asgi(),'/ws/where/test/')
            socket.scope.update(user=self.user,url_route={'kwargs':{'room_code':self.game.room_code}})
            await socket.connect();await socket.receive_json_from()
            await socket.send_json_to({'type':'host_join','hub_session':self.hub.code});await socket.receive_json_from()
            await socket.send_json_to({'type':'admin_start_quiz','play_tutorial':True})
            self.assertEqual((await socket.receive_json_from())['type'],'tutorial_question_missing')
            await socket.disconnect()
        async_to_sync(scenario)()
        self.game.refresh_from_db();self.assertEqual(self.game.status,'waiting')
