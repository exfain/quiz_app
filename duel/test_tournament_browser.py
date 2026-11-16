"""Real Hub, group dashboard, pair transports and independent browser identities."""
from django.urls import reverse
from games_hub import test_session_repairs_browser as browser_reference
from games_hub.models import HubParticipant, HubGameStep
from QuizGame.models import Quiz, QuizQuestion
from Estimation.models import EstimationQuestion
from django.utils import timezone
from . import test_tournaments as fixture
from . import test_playable_browser as reference
from .models import DuelTournament, DuelTournamentPair, DuelTournamentSeat, DuelSubmission, DuelScoreEntry


class TournamentBrowserTests(browser_reference.SessionBrowserBase):
    click_response = reference.PlayableEstimationBrowserTests.click_response

    def setUp(self):
        super().setUp(); hostname=self.host
        self.db(lambda:fixture.TournamentTests.setUp(self));self.host=hostname
        self.client.force_login(self.user)
        def material():
            q=EstimationQuestion.objects.create(created_by=self.user,question_text='Neue Frage für Set zwei?',correct_answer=200,unit='meters')
            self.quiz.selected_questions.add(q)
            quiz=Quiz.objects.create(creator=self.user,title='Normales Spiel danach')
            mc=QuizQuestion.objects.create(created_by=self.user,question_text='2+2?',question_type='multiple_choice',option_a='4',option_b='5',correct_answer='A')
            quiz.selected_questions.add(mc);HubGameStep.objects.create(session=self.hub,order=1,game_key='quiz',room_code=quiz.room_code)
            return q,quiz
        self.second_question,self.normal_game=self.db(material)

    def setup_pages(self,odd=False):
        if odd:
            self.parties.append(self.db(lambda:HubParticipant.objects.create(session=self.hub,nickname='Epsilon',scoring_eligible=True,checked_in_at=timezone.now())))
            self.hub.locked_participant_count=5;self.hub.save()
        lobby=reverse('games_hub:lobby',args=[self.hub.code])
        players={p.pk:self.page(lobby,player=p,theme='vhs') for p in self.parties}
        for page in players.values():page.wait_for_function("document.getElementById('joinCard').style.display==='none'")
        host=self.page(reverse('games_hub:monitor',args=[self.hub.code]),host=True)
        spectator=self.page(reverse('games_hub:spectate_session',args=[self.hub.code]))
        self.click_response(host,'#step-0 .tournament-start','/activate-game/');host.wait_for_url('**/duel/tournament/**/host/')
        for page in players.values():page.wait_for_url('**/duel/tournament/**/participant/')
        spectator.wait_for_url('**/duel/tournament/**/spectator/')
        self.run=DuelTournament.objects.get(step=self.step)
        return host,spectator,players

    def test_host_controls_wait_for_the_first_authoritative_snapshot(self):
        from . import tournaments
        self.db(lambda:tournaments.start_tournament(user=self.user, session_code=self.hub.code, step_id=self.step.pk))
        self.run = DuelTournament.objects.get(step=self.step)
        self.db(lambda:tournaments.draw_set(user=self.user, tournament_id=self.run.pk, previous_set_id=None,
            selection={'id':self.question.pk,'duration_seconds':120}))
        pair = self.run.sets.get().pairs.first()
        path = reverse('duel:match_page', args=[self.hub.code, pair.game_instance_id, 'host'])
        api = reverse('duel:match_api', args=[self.hub.code, pair.game_instance_id, 'host'])
        context = self.context(host=True); page = context.new_page(); pending=[]; group_requests=[]
        page.on('request', lambda r:group_requests.append(r.url) if '/api/tournament/' in r.url else None)
        page.route('**'+api, lambda route:pending.append(route))
        page.goto(self.live_server_url+path)
        # Server-rendered tournament metadata must not enable match controls.
        page.wait_for_function("document.getElementById('duration').value==='120'")
        self.assertTrue(pending)
        self.assertFalse(group_requests)
        self.assertTrue(page.locator('#host-controls').is_hidden())
        for route in list(pending): route.continue_()
        page.unroute('**'+api)
        page.wait_for_selector('#host-controls:not([hidden])')
        self.assertIn('vorbereitet', page.inner_text('#phase'))
        self.assertEqual(page.locator('#duel-match').get_attribute('data-phase'), 'prepared')

    def draw_browser(self,host,question):
        host.select_option('#material',str(question.pk));host.fill('#duration','120')
        self.click_response(host,'#draw','/api/tournament/');self.run.refresh_from_db()
        host.wait_for_function('n=>document.getElementById("tournament").dataset.set===String(n)',arg=self.run.current_set)
        return list(self.run.sets.get(number=self.run.current_set).pairs.select_related('game_instance').order_by('position'))

    def pair_hosts(self,host,pairs,players):
        controls=[]
        for pair in pairs:
            url=reverse('duel:match_page',args=[self.hub.code,pair.game_instance_id,'host'])
            page=host.context.new_page();page.on('pageerror',lambda e:self.errors.append(str(e)))
            page.goto(self.live_server_url+url);controls.append(page)
            for pk in (pair.game_instance.participant_a_id,pair.game_instance.participant_b_id):
                players[pk].wait_for_url(f'**/{pair.game_instance_id}/participant/')
        # The first slot intro is shared, not restarted per pairing or set.
        next(iter(players.values())).wait_for_selector('#sessionGameStartIntro[hidden]',state='attached',timeout=25000)
        for page in controls:
            page.wait_for_selector('#start:not([hidden])');page.wait_for_function("document.getElementById('duration').value==='120'")
            events=[]
            page.on('request',lambda r,events=events:events.append(('request',r.url)) if r.method=='POST' else None)
            page.on('response',lambda r,events=events:events.append(('response',r.status,r.url)) if r.request.method=='POST' else None)
            try:
                self.click_response(page,'#start','/units/')
            except Exception:
                print('Tournament start diagnostic:',page.url,events,
                    page.evaluate("({phase:document.getElementById('phase').textContent,error:document.getElementById('error').textContent,duration:document.getElementById('duration').value})"))
                raise
        for pair in pairs:
            for pk in (pair.game_instance.participant_a_id,pair.game_instance.participant_b_id):players[pk].wait_for_selector('#estimate:not([disabled])')
        return controls

    def answers(self,pair,players,value,tie=False):
        for pk,answer in ((pair.game_instance.participant_a_id,value),(pair.game_instance.participant_b_id,value if tie else value+20)):
            page=players[pk];page.fill('#estimate',str(answer));self.click_response(page,'#commit','/units/')
            page.wait_for_selector('#estimate[disabled]',state='attached')

    def release(self,page):
        for action in ('show_answers','show_solution','show_result','complete'):
            page.wait_for_selector('#'+action+':not([hidden])');self.click_response(page,'#'+action,'/units/')

    def end_and_return(self,host,spectator,players):
        host.wait_for_selector('#end:not([hidden])');self.click_response(host,'#end','/api/tournament/')
        host.wait_for_selector('#return:not([hidden])');spectator.wait_for_selector('#return:not([hidden])')
        for page in players.values():
            if '/duel/estimation/' in page.url:page.click('#tournament-pair a')
            page.wait_for_url('**/duel/tournament/**/participant/');page.wait_for_selector('#return:not([hidden])');page.click('#return')
            page.wait_for_url('**/hub/lobby/MIXED/**');page.wait_for_function("document.getElementById('joinCard').style.display==='none'")
        host.click('#return');host.wait_for_url('**/hub/monitor/MIXED/')
        spectator.click('#return');spectator.wait_for_url('**/hub/spectate/MIXED/')

    def test_two_sets_parallel_pairs_staggered_answers_rejoin_reveal_gate_end_and_normal_game(self):
        host,spectator,players=self.setup_pages()
        previous_ids=set()
        for number,question in ((1,self.question),(2,self.second_question)):
            pairs=self.draw_browser(host,question);self.assertEqual(len(pairs),2)
            self.assertTrue(previous_ids.isdisjoint({p.game_instance_id for p in pairs}));previous_ids|={p.game_instance_id for p in pairs}
            controls=self.pair_hosts(host,pairs,players)
            value=100 if number==1 else 200
            self.answers(pairs[0],players,value,tie=number==2)
            with controls[0].expect_response(lambda r:'/units/' in r.url and r.request.method=='POST') as blocked:controls[0].click('#show_answers')
            self.assertEqual(blocked.value.status,409)
            early=spectator.evaluate('async()=> (await fetch(document.getElementById("tournament").dataset.api)).json()')
            self.assertFalse(any('points' in p for p in early['pairs']))
            self.assertEqual(len(early['results']), (number-1)*2)
            self.assertTrue(all(row['set'] < number for row in early['results']))
            private_url=reverse('duel:match_api',args=[self.hub.code,pairs[0].game_instance_id,'spectator'])
            private=spectator.evaluate('async url=>(await fetch(url)).json()',private_url)
            self.assertTrue(private['unit']['tournament_waiting']);self.assertNotIn('target',private['unit']);self.assertNotIn('answers',private['unit']);self.assertIsNone(private['match'])
            # A real signed rejoin via the normal lobby restores this pairing.
            pk=pairs[0].game_instance.participant_a_id;original=DuelSubmission.objects.get(unit__game_instance=pairs[0].game_instance,participant_id=pk).pk
            players[pk].goto(self.live_server_url+reverse('games_hub:lobby',args=[self.hub.code]))
            players[pk].wait_for_url(f'**/{pairs[0].game_instance_id}/participant/')
            self.assertEqual(DuelSubmission.objects.get(unit__game_instance=pairs[0].game_instance,participant_id=pk).pk,original)
            self.answers(pairs[1],players,value,tie=number==2)
            for page in controls:self.release(page);page.close()
            host.wait_for_function("!document.getElementById('draw').disabled")
            spectator.wait_for_function('n=>document.querySelectorAll("#results p").length===n',arg=number*2)
        self.assertEqual(DuelTournamentPair.objects.count(),4)
        self.assertEqual(DuelTournamentSeat.objects.filter(tournament_set__tournament=self.run).count(),8)
        self.end_and_return(host,spectator,players)
        host.click(f'.launch-btn[data-room-code="{self.normal_game.room_code}"]');host.wait_for_url('**/quiz/**/monitor/**')
        host.wait_for_selector('#startQuizBtn:not([disabled])')
        host.wait_for_function('window.adminGameMonitor?.websocket?.readyState===WebSocket.OPEN')
        host.click('#startQuizBtn')
        for page in players.values():page.wait_for_url('**/quiz/play/**')
        self.assertEqual(self.normal_game.participants.filter(hub_session_code=self.hub.code).count(),4)
        self.assertFalse(self.errors,self.errors)

    def test_five_people_have_one_persisted_bye_and_repeated_start_does_not_redraw(self):
        host,spectator,players=self.setup_pages(odd=True);pairs=self.draw_browser(host,self.question)
        seats=self.run.sets.get().seats;bye=seats.get(pair__isnull=True)
        players[bye.participant_id].wait_for_url('**/duel/tournament/**/participant/')
        players[bye.participant_id].wait_for_function("document.getElementById('bye').textContent.includes('Freilos')")
        # Repeat the real Hub start; it returns the same persisted organization.
        response=host.evaluate("""async data => (await fetch('/hub/api/session/MIXED/activate-game/',{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':document.querySelector('[name=csrfmiddlewaretoken]').value},body:JSON.stringify(data)})).json()""",
            {'action':'start_tournament','step_id':self.step.pk,'game_key':'estimation','room_code':self.quiz.room_code})
        self.assertIn(str(self.run.pk),response['host_url']);self.assertEqual(seats.get(pair__isnull=True).pk,bye.pk)
        controls=self.pair_hosts(host,pairs,{pk:page for pk,page in players.items() if pk!=bye.participant_id})
        for pair in pairs:self.answers(pair,players,100)
        for page in controls:self.release(page);page.close()
        self.assertFalse(DuelScoreEntry.objects.filter(participant_id=bye.participant_id).exists())
        self.end_and_return(host,spectator,players);self.assertFalse(self.errors,self.errors)
