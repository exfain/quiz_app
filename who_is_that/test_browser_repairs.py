"""Real browser regressions for both participant themes and the host monitor."""
import base64
import json
import os
import time
from datetime import timedelta
from unittest.mock import patch
from channels.testing import ChannelsLiveServerTestCase
from django.conf import settings
from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils import timezone
from games_hub.authoritative_state import current_snapshot
from games_hub.models import HubParticipant
from games_hub.playwright_e2e import start_chromium_browser, install_browser_test_stubs
from . import tests as fixtures
from .models import WhoThatAnswer, WhoThatParticipant, WhoThatQuestion


def report(case, **facts):
    # Each scenario asserts its original failure and records browser JS errors.
    assert not facts.get('errors'), facts
    if case == 'browser_expired_host':
        assert facts['navigations_in_1_2_seconds'] <= 1, facts
        assert facts['back_buttons'] == 1 and facts['end_buttons'] == 0, facts
    elif case == 'browser_normal_flow':
        assert facts['standard_and_vhs_answers'] == 2 and facts['scores'] == [1, 0], facts
        assert facts['back_visible'] and facts['overview_reached'], facts
        assert not facts['alice_answer_sent_to_bob_before_end'], facts
    elif case == 'browser_special_names':
        for item in facts['cases']:
            assert not item['errors'] and not item['rejected_frames'], item
            assert item['sent_names'] == [item['name']] and item['saved_answers'] == 1, item
    elif case == 'browser_score_modal':
        assert facts['bootstrap'] != 'stub', facts
        assert facts['visible'] and not facts['nested_in_main'] and facts['input_receives_click'], facts
    elif case == 'browser_draft_reload':
        assert facts['stored_before_reload'] and facts['input_after_reload'] == 'Ada Lovelace', facts
        assert facts['scored_text'] == 'Ada Lovelace' and facts['scored_points'] == 1, facts
    elif case == 'browser_auto_end_next':
        assert facts['pending_answer_scored'] == 1 and facts['next_question_current'], facts
        assert facts['next_input'] == '', facts


class WhoThatBrowserRepairTests(ChannelsLiveServerTestCase):
    action = fixtures.WhoThatAutomaticQuestionStartTests.action
    present = fixtures.WhoThatAutomaticQuestionStartTests.present
    def start(self, age=3):
        from concurrent.futures import ThreadPoolExecutor
        from django.db import connections
        def operation():
            try:
                self.configured = current_snapshot('who_that', self.quiz.room_code, self.hub.code)
                result = self.present(at=timezone.now() - timedelta(seconds=age))
                self.assertTrue(result.accepted, result.message)
                self.quiz.refresh_from_db()
                self.quiz_session.refresh_from_db()
                return result
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(operation).result()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.environment = patch.dict(os.environ, {'DJANGO_ALLOW_ASYNC_UNSAFE': '1'})
        cls.environment.start()
        cls.pw, cls.browser = start_chromium_browser(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.environment.stop()
        super().tearDownClass()

    def setUp(self):
        fixtures.WhoThatAutomaticQuestionStartTests.setUp(self)
        self.admin = self.host
        self.host = "localhost"
        self.contexts = []
        self.errors = []
        self.hub.started_at = self.quiz.started_at - timedelta(seconds=1)
        self.hub.save(update_fields=['started_at'])
        self.hub.steps.update(order=0)
        HubParticipant.objects.create(session=self.hub, nickname='Alice')
        HubParticipant.objects.create(session=self.hub, nickname='Bob')
        self.bob = WhoThatParticipant.objects.create(quiz=self.quiz, name='Bob', hub_session_code=self.hub.code)
        self.question.time_limit = 90
        self.question.image.save('audit.png', ContentFile(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nWQAAAAASUVORK5CYII=')), save=False)
        self.question.save(update_fields=['time_limit','image'])

    def tearDown(self):
        for context in self.contexts:
            context.close()
        time.sleep(.3)

    def page(self, label, theme='standard', host=False):
        context = self.browser.new_context()
        self.contexts.append(context)
        install_browser_test_stubs(context)
        context.add_init_script('localStorage.setItem("participant_interface_theme", '+json.dumps(theme)+');')
        if host:
            self.client.force_login(self.admin)
            context.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': self.client.cookies[settings.SESSION_COOKIE_NAME].value, 'url': self.live_server_url}])
        page = context.new_page()
        page.on('pageerror', lambda error: self.errors.append(label+': '+str(error)))
        return page

    def monitor_url(self):
        return self.live_server_url+reverse('admin_dashboard:who_that_monitor', args=[self.quiz.room_code])+'?hub_session='+self.hub.code

    def player_url(self, name):
        return self.live_server_url+reverse('who_is_that:play', args=[self.quiz.room_code,name])+'?hub_session='+self.hub.code

    def test_expired_host_recovery_and_overview(self):
        self.start(age=120)
        page = self.page('expired-host', host=True)
        requests = []
        page.on('request', lambda request: requests.append(request.url) if request.is_navigation_request() else None)
        try:
            page.goto(self.monitor_url(), wait_until='commit', timeout=10000)
            page.wait_for_timeout(1200)
            report('browser_expired_host', navigations_in_1_2_seconds=len(requests), back_buttons=page.locator('#backToQuestionOverviewBtn').count(),
                   end_buttons=page.locator('#endQuestionBtn').count(), errors=self.errors)
        finally:
            page.close()

    def test_normal_two_players_and_host_review(self):
        self.start()
        host = self.page('host', host=True)
        alice, bob = self.page('alice'), self.page('bob', theme='vhs')
        bob_frames = []
        bob.on('websocket', lambda socket: socket.on('framereceived', lambda payload: bob_frames.append(json.loads(payload))))
        host.goto(self.monitor_url())
        alice.goto(self.player_url('Alice'))
        bob.goto(self.player_url('Bob'))
        alice.wait_for_selector('#nameInput:not([disabled])', timeout=15000)
        bob.wait_for_selector('#nameInput:not([disabled])', timeout=15000)
        alice.fill('#nameInput', self.question.correct_answer)
        alice.click('#submitAnswerBtn')
        alice.wait_for_timeout(600)
        leaked = [frame['answer'] for frame in bob_frames if frame.get('type')=='participant_answered' and frame.get('answer',{}).get('participant_name')=='Alice']
        bob.fill('#nameInput', 'No idea')
        bob.click('#submitAnswerBtn')
        bob.wait_for_timeout(600)
        host.wait_for_selector('#endQuestionBtn', timeout=15000)
        host.click('#endQuestionBtn')
        host.wait_for_selector('#backToQuestionOverviewBtn', timeout=15000)
        native_review = host.locator('#backToQuestionOverviewBtn').is_visible()
        host.click('#backToQuestionOverviewBtn')
        host.wait_for_selector('#questionSelection', timeout=15000)
        self.participant.refresh_from_db()
        self.bob.refresh_from_db()
        report('browser_normal_flow', standard_and_vhs_answers=WhoThatAnswer.objects.count(), scores=[self.participant.total_score,self.bob.total_score],
               back_visible=native_review, overview_reached=host.locator('#questionSelection').is_visible(),
               alice_answer_sent_to_bob_before_end=bool(leaked), leaked_correctness=leaked[0].get('is_correct') if leaked else None, errors=self.errors)

    def test_special_names(self):
        self.start()
        cases = []
        for name, theme in [('Team <West','standard'),('Trailing '+chr(92),'vhs')]:
            WhoThatParticipant.objects.create(quiz=self.quiz, name=name, hub_session_code=self.hub.code)
            HubParticipant.objects.create(session=self.hub, nickname=name)
            page = self.page(name, theme=theme)
            sent, received = [], []
            def instrument(socket):
                socket.on('framesent', lambda value: sent.append(json.loads(value)))
                socket.on('framereceived', lambda value: received.append(json.loads(value)))
            page.on('websocket', instrument)
            before = len(self.errors)
            page.goto(self.player_url(name))
            page.wait_for_timeout(400)
            if len(self.errors)==before:
                page.fill('#nameInput', self.question.correct_answer)
                page.click('#submitAnswerBtn')
                page.wait_for_timeout(600)
            cases.append({'name':name,'errors':self.errors[before:], 'sent_names':[frame.get('participant_name') for frame in sent if frame.get('type')=='participant_submit_answer'],
                          'saved_answers':WhoThatAnswer.objects.filter(participant__name=name).count(),
                          'rejected_frames':[frame.get('code') for frame in received if frame.get('type')=='action_rejected']})
            page.close()
        report('browser_special_names', cases=cases)



    def test_score_modal_is_clickable(self):
        self.start()
        host=self.page('score-host',host=True)
        host.goto(self.monitor_url())
        host.click('.edit-score-btn[data-participant-id="'+str(self.participant.pk)+'"]')
        host.wait_for_timeout(400)
        result=host.evaluate('''() => {
            const modal=document.getElementById('editScoreModal');
            const input=document.getElementById('editScoreInput');
            const box=input.getBoundingClientRect();
            const hit=document.elementFromPoint(box.x+box.width/2,box.y+box.height/2);
            return {bootstrap: window.bootstrap?.Modal?.VERSION || 'stub', visible: modal.classList.contains('show'),
                    nested_in_main: !!modal.closest('main'), input_receives_click: !!hit && modal.contains(hit), top_element: hit?.className};
        }''')
        report('browser_score_modal', **result)

    def test_draft_reload_and_end(self):
        self.start()
        alice=self.page('draft-alice')
        alice.goto(self.player_url('Alice'))
        alice.wait_for_selector('#nameInput:not([disabled])',timeout=15000)
        alice.fill('#nameInput',self.question.correct_answer)
        deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            self.quiz_session.refresh_from_db()
            if self.quiz_session.pending_answers:
                break
            alice.wait_for_timeout(100)
        stored_before=bool(self.quiz_session.pending_answers)
        # Both edits happen before another acknowledgement can reach the browser.
        alice.evaluate('''(answer) => {
            const input = document.getElementById('nameInput');
            input.value = '';
            input.dispatchEvent(new Event('input', {bubbles: true}));
            input.value = answer;
            input.dispatchEvent(new Event('input', {bubbles: true}));
        }''', self.question.correct_answer)
        alice.wait_for_timeout(300)
        self.quiz_session.refresh_from_db()
        self.assertEqual(self.quiz_session.pending_answers[str(self.participant.pk)]['user_answer'], self.question.correct_answer)
        alice.reload()
        alice.wait_for_selector('#nameInput:not([disabled])',timeout=15000)
        alice.wait_for_function('(expected) => document.getElementById("nameInput").value === expected', arg=self.question.correct_answer)
        value_after=alice.input_value('#nameInput')
        host=self.page('draft-host',host=True)
        host.goto(self.monitor_url())
        host.wait_for_selector('#endQuestionBtn',timeout=15000)
        host.click('#endQuestionBtn')
        host.wait_for_selector('#backToQuestionOverviewBtn',timeout=15000)
        answer=WhoThatAnswer.objects.filter(participant=self.participant).first()
        report('browser_draft_reload', stored_before_reload=stored_before, input_after_reload=value_after,
               scored_text=answer.user_answer if answer else None, scored_points=answer.points_earned if answer else None)



    def test_automatic_end_and_next_question(self):
        self.question.time_limit=8
        self.question.save(update_fields=['time_limit'])
        next_question=WhoThatQuestion.objects.create(created_by=self.admin,question_text='Next person',image=self.question.image.name,correct_answer='Next',time_limit=30)
        self.quiz.selected_questions.add(next_question)
        self.start()
        host=self.page('auto-host',host=True)
        alice=self.page('auto-alice')
        host.goto(self.monitor_url())
        alice.goto(self.player_url('Alice'))
        alice.wait_for_selector('#nameInput:not([disabled])',timeout=10000)
        alice.fill('#nameInput',self.question.correct_answer)
        host.wait_for_selector('#backToQuestionOverviewBtn',timeout=15000)
        answer=WhoThatAnswer.objects.filter(participant=self.participant,question=self.question).first()
        host.click('#backToQuestionOverviewBtn')
        selector='.send-question-btn[data-question-id="'+str(next_question.pk)+'"]'
        host.wait_for_selector(selector,timeout=15000)
        host.wait_for_function('() => window.adminGameMonitor?.websocket?.readyState === WebSocket.OPEN')
        host.click(selector)
        host.click(selector)
        alice.wait_for_function('() => document.getElementById("questionPhoto")?.getAttribute("src") && !document.getElementById("nameInput").disabled',timeout=15000)
        self.quiz.refresh_from_db()
        report('browser_auto_end_next', pending_answer_scored=answer.points_earned if answer else None,
               next_question_current=self.quiz.current_question_id==next_question.pk, next_input=alice.input_value('#nameInput'), errors=self.errors)

    def test_vhs_pause_resume_and_repeat_with_own_points(self):
        self.start()
        host = self.page('pause-host', host=True)
        alice = self.page('pause-vhs', theme='vhs')
        host.on('dialog', lambda dialog: dialog.accept())
        host.goto(self.monitor_url())
        alice.goto(self.player_url('Alice'))
        alice.wait_for_selector('#nameInput:not([disabled])')
        alice.fill('#nameInput', self.question.correct_answer)
        alice.wait_for_timeout(300)
        host.click('#setInactiveBtn')
        host.wait_for_selector('#startQuizBtn')
        alice.wait_for_function('() => document.getElementById("nameInput").disabled')
        self.assertEqual(alice.input_value('#nameInput'), self.question.correct_answer)
        host.wait_for_function('() => window.adminGameMonitor?.websocket?.readyState === WebSocket.OPEN')
        host.click('#startQuizBtn')
        alice.wait_for_selector('#nameInput:not([disabled])')
        self.assertEqual(alice.input_value('#nameInput'), self.question.correct_answer)
        alice.click('#submitAnswerBtn')
        alice.wait_for_selector('#answerSubmittedState:not(.d-none)')
        host.wait_for_selector('#endQuestionBtn')
        host.click('#endQuestionBtn')
        host.wait_for_selector('#backToQuestionOverviewBtn')
        host.click('#backToQuestionOverviewBtn')
        selector = f'.send-question-btn[data-question-id="{self.question.pk}"]'
        host.wait_for_selector(selector)
        host.wait_for_function('() => window.adminGameMonitor?.websocket?.readyState === WebSocket.OPEN')
        host.click(selector)
        host.click(selector)
        alice.wait_for_selector('#nameInput:not([disabled])')
        self.assertEqual(alice.input_value('#nameInput'), '')
        self.assertEqual(alice.locator('#vhsWhoThatProgress').inner_text(), '2/2')
        alice.fill('#nameInput', self.question.correct_answer)
        alice.click('#submitAnswerBtn')
        alice.wait_for_selector('#answerSubmittedState:not(.d-none)')
        host.wait_for_selector('#endQuestionBtn')
        host.click('#endQuestionBtn')
        host.wait_for_selector('#backToQuestionOverviewBtn')
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.total_score, 2)
        self.assertEqual(WhoThatAnswer.objects.filter(participant=self.participant).count(), 2)
        self.assertFalse(self.errors)
