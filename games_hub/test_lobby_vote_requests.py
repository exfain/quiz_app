"""Coalesce lobby bursts while preserving the final personalised vote read."""
import time

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from QuizGame.models import Quiz

from .models import HubSession,HubParticipant,HubGameStep
from .voting import open_session_voting
from .test_session_repairs_browser import SessionBrowserBase


class LobbyVoteRequestTests(SessionBrowserBase):
    def test_state_burst_has_one_inflight_vote_read_and_a_fresh_trailing_read(self):
        def material():
            self.user=User.objects.create_user('vote-load-host')
            self.hub=HubSession.objects.create(code='VOTELOAD',creator=self.user,is_active=True,started_at=timezone.now(),check_in_status='completed',locked_participant_count=1)
            self.party=HubParticipant.objects.create(session=self.hub,nickname='Vote Team',is_active=True,scoring_eligible=True,checked_in_at=timezone.now())
            game=Quiz.objects.create(creator=self.user,title='Nächstes Spiel')
            HubGameStep.objects.create(session=self.hub,game_key='quiz',room_code=game.room_code,order=1)
            self.assertTrue(open_session_voting(self.hub,'normal')['success'])
        self.db(material)
        context=self.context(player=self.party)
        held=[];requests=[]
        def delay_response(route):
            response=route.fetch()
            requests.append(route.request.url);held.append((route,response))
        context.route('**/hub/api/session/VOTELOAD/votes/**',delay_response)
        page=context.new_page();page.set_default_timeout(15000)
        page.on('pageerror',lambda error:self.errors.append(str(error)))
        try:
            page.goto(self.live_server_url+reverse('games_hub:lobby',args=[self.hub.code]),wait_until='domcontentloaded')
            page.wait_for_function("document.getElementById('joinCard').style.display==='none'")
            page.evaluate("""() => {
                window.qaLobbyStates=0;window.qaVoteUpdates=0;
                window.hubLobbySocket.addEventListener('message',event=>{
                    const data=JSON.parse(event.data);
                    if(data.type==='state')window.qaLobbyStates++;
                    if(data.type==='vote_update')window.qaVoteUpdates++;
                });
                for(let i=0;i<20;i++)window.hubLobbySocket.send(JSON.stringify({type:'get_state'}));
            }""")
            page.wait_for_function('window.qaLobbyStates===20')
            self.assertEqual(len(requests),1,'Overlapping vote GETs amplify each lobby broadcast')
            page.evaluate("window.hubLobbySocket.send(JSON.stringify({type:'vote',step_order:1}))")
            page.wait_for_function('window.qaVoteUpdates>0')
            route,response=held.pop(0);route.fulfill(response=response)
            deadline=time.monotonic()+5
            while not held and time.monotonic()<deadline:page.wait_for_timeout(20)
            self.assertEqual(len(requests),2,'Exactly one trailing read must include updates received in flight')
            route,response=held.pop(0)
            self.assertEqual(response.json()['voting']['my_vote_step_order'],1)
            route.fulfill(response=response)
            page.wait_for_selector('.vote-card.voted[data-order="1"]')
            self.assertIn('1 vote',page.locator('.vote-card[data-order="1"] .vote-count').inner_text())
            self.assertFalse(self.errors,self.errors)
        finally:
            context.unroute_all(behavior='wait')
            for route,response in held:route.fulfill(response=response)
