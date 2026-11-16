"""Five isolated Chromium contexts: explicit pair, rejoin, next Multiplayer."""
from django.urls import reverse
import os
import tempfile
from games_hub import test_session_repairs_browser as browser_reference
from games_hub.models import HubGameStep
from QuizGame.models import Quiz, QuizQuestion
from . import test_encounters as reference
from . import test_playable_browser as duel_reference
from .models import DuelGameInstance


class MixedEncounterBrowserTests(browser_reference.SessionBrowserBase):
    click_response = duel_reference.PlayableEstimationBrowserTests.click_response

    def setUp(self):
        super().setUp()
        live_host = self.host
        self.db(lambda: reference.EncounterTests.setUp(self))
        self.host = live_host  # Channels uses this hostname to build live URLs.
        self.client.force_login(self.user)
        def material():
            HubGameStep.objects.create(session=self.hub, order=1, game_key='estimation', room_code=self.quiz.room_code)
            quiz = Quiz.objects.create(creator=self.user, title='Alle spielen wieder mit')
            question = QuizQuestion.objects.create(created_by=self.user, question_text='Zwei plus zwei?', question_type='multiple_choice', option_a='4', option_b='5', correct_answer='A')
            quiz.selected_questions.add(question)
            HubGameStep.objects.create(session=self.hub, order=2, game_key='quiz', room_code=quiz.room_code)
            return quiz
        self.next_game = self.db(material)

    def wait_phase(self, page, phase):
        page.wait_for_function('phase=>document.getElementById("duel-match")?.dataset.phase===phase', arg=phase)

    def test_explicit_pair_both_formats_five_contexts_rejoin_return_then_all_play_normally(self):
        lobby = reverse('games_hub:lobby', args=[self.hub.code])
        players = [self.page(lobby, player=p, theme='vhs') for p in self.parties]
        for page in players: page.wait_for_function("document.getElementById('joinCard').style.display==='none'")
        host = self.page(reverse('games_hub:monitor', args=[self.hub.code]), host=True)
        spectator = self.page(reverse('games_hub:spectate_session', args=[self.hub.code]), theme='vhs')
        for slot, (mode, threshold, sides, rounds) in enumerate((('best_of_n', 7, (0, 2), 4), ('target_score', 1, (1, 2), 1))):
            form = host.locator(f'#step-{slot} .encounter-config')
            form.locator('.encounter-party').first.locator('option').nth(1).wait_for(state='attached')
            for select, side in zip(form.locator('.encounter-party').all(), sides): select.select_option(str(self.parties[side].pk))
            form.locator('.encounter-mode').select_option(mode); form.locator('.encounter-value').fill(str(threshold))
            if slot == 0: host.screenshot(path=os.path.join(tempfile.gettempdir(), 'duel-mixed-pairing.png'), full_page=True)
            self.click_response(host, f'#step-{slot} .encounter-start', '/activate-game/')
            host.wait_for_url('**/duel/estimation/**/host/')
            selected = [players[i] for i in sides]; other = players[next(i for i in range(3) if i not in sides)]
            for page in selected: page.wait_for_url('**/duel/estimation/**/participant/')
            spectator.wait_for_url('**/duel/estimation/**/spectator/')
            selected[0].wait_for_selector('#sessionGameStartIntro:not([hidden])')
            selected[0].wait_for_selector('#sessionGameStartIntro[hidden]', state='attached', timeout=25000)
            selected[0].wait_for_selector('#waiting:not([hidden])')
            self.assertIn('/hub/lobby/', other.url); self.assertTrue(other.locator('[data-watch]').is_visible())
            if slot == 0:
                with other.expect_popup() as watching: other.click('[data-watch]')
                watching.value.wait_for_selector('#duel-match[data-role=spectator]'); watching.value.close()
                other.reload(); other.wait_for_function("document.getElementById('joinCard').style.display==='none'")
                status = other.evaluate("""async url => {
                    const c=JSON.parse(localStorage.getItem('hub_lobby_rejoin:MIXED'));
                    return (await fetch(url,{headers:{Authorization:'Bearer '+c.token}})).status;
                }""", host.locator('#duel-match').get_attribute('data-api').replace('/host/', '/participant/'))
                self.assertEqual(status, 403)
            for number in range(rounds):
                host.select_option('#question-select', str(self.question.pk)); self.click_response(host, '#prepare', '/duel/api/estimation/')
                host.wait_for_selector('#start:not([hidden])'); host.fill('#duration', '60'); self.click_response(host, '#start', '/units/')
                for page in selected: page.wait_for_selector('#estimate:not([disabled])')
                for page, value in zip(selected, ('100', '120')):
                    page.fill('#estimate', value); self.click_response(page, '#commit', '/units/')
                    page.wait_for_selector('#estimate[disabled]', state='attached')
                if number == 0:
                    selected[0].reload(); selected[0].wait_for_selector('#estimate[disabled]', state='attached')
                    self.assertIn('eingeloggt', selected[0].inner_text('#own-status'))
                    selected[1].goto(self.live_server_url+lobby)  # Actual signed rejoin via Hub.
                    selected[1].wait_for_url('**/duel/estimation/**/participant/')
                    selected[1].wait_for_selector('#estimate[disabled]', state='attached')
                self.assertTrue(spectator.locator('#answers-card').is_hidden()); self.assertTrue(spectator.locator('#solution-card').is_hidden())
                self.click_response(host, '#show_answers', '/units/'); spectator.wait_for_selector('#answers-card:not([hidden])')
                self.assertTrue(spectator.locator('#solution-card').is_hidden())
                self.click_response(host, '#show_solution', '/units/'); spectator.wait_for_selector('#solution-card:not([hidden])')
                self.click_response(host, '#show_result', '/units/'); spectator.wait_for_selector('#result-card:not([hidden])')
                self.assertIn(self.parties[sides[0]].nickname, spectator.inner_text('#result'))
                if mode == 'target_score': self.assertEqual(spectator.locator('progress').count(), 2)
                self.click_response(host, '#complete', '/units/')
                if number+1 < rounds: host.wait_for_selector('#prepare:not([hidden])')
            self.assertIn('Matchsieg', spectator.inner_text('#result'))
            if slot == 0: spectator.screenshot(path=os.path.join(tempfile.gettempdir(), 'duel-mixed-result.png'), full_page=True)
            instance = DuelGameInstance.objects.get(hub_game_step__order=slot)
            self.assertEqual(instance.match_status, 'completed'); self.assertEqual(instance.match_outcome.winner_id, self.parties[sides[0]].pk)
            for page in (host, spectator, *selected): page.wait_for_selector('#return:not([hidden])'); page.click('#return')
            host.wait_for_url('**/hub/monitor/MIXED/')
            for page in players: page.wait_for_url('**/hub/lobby/MIXED/**'); page.wait_for_function("document.getElementById('joinCard').style.display==='none'")
            spectator.wait_for_url('**/hub/spectate/MIXED/')
            host.wait_for_function("document.querySelector('[data-result]').textContent.includes('Duellsieger')")
        # Normal Hub entry, normal Quick Quiz monitor and normal server start.
        host.click(f'.launch-btn[data-room-code="{self.next_game.room_code}"]')
        host.wait_for_url('**/quiz/**/monitor/**'); host.wait_for_selector('#startQuizBtn:not([disabled])')
        # The existing monitor deliberately allows retry after a disconnected
        # start; a visible button alone is not a completed socket handshake.
        host.wait_for_function('window.adminGameMonitor?.websocket?.readyState===WebSocket.OPEN')
        host.click('#startQuizBtn')
        for page in players: page.wait_for_url('**/quiz/play/**'); page.wait_for_selector('#sessionGameStartIntro:not([hidden])')
        self.assertEqual(self.next_game.participants.filter(hub_session_code=self.hub.code).count(), 3)
        self.assertEqual(DuelGameInstance.objects.count(), 2)
        self.assertFalse(self.errors, self.errors)
