"""Recover joins occurring between host HTML rendering and socket subscription."""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from QuizGame.models import Quiz, QuizParticipant
from .models import HubSession, HubGameStep, HubParticipant
from .test_session_repairs_browser import SessionBrowserBase


class QuizMonitorRosterScopeTests(TestCase):
    def test_existing_roster_api_is_host_only_and_filters_the_requested_session(self):
        owner=User.objects.create_superuser('roster-owner','','pw')
        other=User.objects.create_user('roster-other',is_staff=True)
        quiz=Quiz.objects.create(creator=owner)
        own=QuizParticipant.objects.create(quiz=quiz,name='Current',hub_session_code='CURRENT')
        QuizParticipant.objects.create(quiz=quiz,name='Previous',hub_session_code='PREVIOUS')
        url=reverse('admin_dashboard:api_participants',args=[quiz.room_code])+'?hub_session=CURRENT'
        self.assertEqual(self.client.get(url).status_code,302)
        self.client.force_login(other)
        self.assertEqual(self.client.get(url).status_code,403)
        self.client.force_login(owner)
        response=self.client.get(url)
        self.assertEqual(response.status_code,200)
        self.assertEqual([p['id'] for p in response.json()['participants']],[own.pk])


class QuizMonitorRosterResumeTests(SessionBrowserBase):
    def test_25_joins_between_html_and_socket_are_recovered_without_new_join_events(self):
        def material():
            self.user=User.objects.create_superuser('roster-resume','','pw')
            self.hub=HubSession.objects.create(code='ROSTER25',creator=self.user,is_active=True,started_at=timezone.now())
            self.quiz=Quiz.objects.create(creator=self.user,title='Roster resume',status='active',started_at=timezone.now())
            HubGameStep.objects.create(session=self.hub,game_key='quiz',room_code=self.quiz.room_code,order=0)
        self.db(material)
        self.client.force_login(self.user)
        context=self.context(host=True)
        path=reverse('admin_dashboard:quiz_monitor',args=[self.quiz.room_code])+'?hub_session='+self.hub.code
        def render_then_join(route):
            response=route.fetch()
            self.assertIn('id="participantCount">0</span>',response.text())
            def joins():
                for n in range(25):
                    name=f'Team {n+1:02d}'
                    HubParticipant.objects.create(session=self.hub,nickname=name)
                    QuizParticipant.objects.create(quiz=self.quiz,name=name,hub_session_code=self.hub.code)
            self.db(joins)
            route.fulfill(response=response)
        context.route('**'+path,render_then_join,times=1)
        page=context.new_page();page.set_default_timeout(15000)
        page.on('pageerror',lambda error:self.errors.append(str(error)))
        page.goto(self.live_server_url+path,wait_until='domcontentloaded')
        page.wait_for_function("window.adminGameMonitor?.websocket?.readyState===WebSocket.OPEN")
        page.wait_for_function("document.getElementById('participantCount').textContent==='25'")
        self.assertEqual(page.locator('#participantsList .participant-item').count(),25)
        page.reload()
        page.wait_for_function("document.getElementById('participantCount').textContent==='25'")
        self.assertEqual(page.locator('#participantsList .participant-item').count(),25)
        self.assertFalse(self.errors,self.errors)
