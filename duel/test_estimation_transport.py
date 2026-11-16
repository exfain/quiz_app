"""HTTP integration through project routing, auth and CSRF middleware.

All four clients use the real stored-phase controller and command/submission
services. Credentials are existing Django sessions and signed lobby tokens;
no permissions, services or projections are stubbed.
"""
import json
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth.models import User
from django.db import IntegrityError, connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import resolve, reverse

from games_hub.lobby_join import issue_rejoin_token
from games_hub.models import HubParticipant, HubSession, HubGameStep
from . import test_estimation_rounds as fixtures
from .commands import ActorContext
from .estimation_rounds import reconcile_estimation_deadline
from .models import (
    DuelGameInstance, DuelUnit, DuelSubmission, DuelScoreEntry, DuelActionReceipt,
    DuelEstimationRoundState as State, EstimationPhase as Phase, ReceiptActorType,
)


class EstimationTransportTests(fixtures.PhaseFixture):
    def setUp(self):
        super().setUp()
        self.hub.creator = self.user
        self.hub.save(update_fields=["creator"])
        self.step.game_format = "duel"
        self.step.save(update_fields=["game_format"])
        self.host_client = Client(enforce_csrf_checks=True)
        self.host_client.force_login(self.user)
        self.client_a, self.client_b = [self.player_client(p) for p in (self.party_a, self.party_b)]
        self.spectator = Client(enforce_csrf_checks=True)
        self.clients = (("host", self.host_client), ("participant", self.client_a),
                        ("participant", self.client_b), ("spectator", self.spectator))
        # The GETs provide the normal CSRF cookies, never an auth shortcut.
        for role, client in self.clients:
            self.assertEqual(client.get(self.url(role)).status_code, 200)

    def player_client(self, participant):
        return Client(enforce_csrf_checks=True,
            HTTP_AUTHORIZATION="Bearer " + issue_rejoin_token(participant))

    def url(self, role="participant", **kwargs):
        return reverse("duel:estimation_" + role, kwargs=dict(
            session_code=self.hub.code, instance_id=self.instance.pk, unit_id=self.unit.pk) | kwargs)

    def post(self, client, role, data, **kwargs):
        with patch("django.utils.timezone.now", return_value=self.clock+timedelta(seconds=10)):
            return client.post(self.url(role, **kwargs), data=json.dumps(data), content_type="application/json",
                HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value)

    def host_action(self, action, **data):
        return self.post(self.host_client, "host", {"action": action, "client_action_id": str(uuid4()), **data})

    def submit(self, client=None, value="95", **data):
        return self.post(client or self.client_a, "participant", {
            "client_action_id": str(uuid4()), "payload": {"value": value}, **data})

    def begin(self):
        self.assertEqual(self.host_action("start", duration_seconds=60).status_code, 200)

    def both_http(self):
        self.begin()
        self.assertEqual(self.submit().status_code, 200)
        self.assertEqual(self.submit(self.client_b, "120").status_code, 200)

    def assert_private(self, data):
        self.assertNotIn("answers", data)
        self.assertNotIn("target", data)
        self.assertNotIn("points", data)
        self.assertNotIn("round_outcome", data)
        self.assertNotIn("match", data)

    def test_routing_and_initial_reads_are_role_scoped_and_database_read_only(self):
        before = State.objects.values().get()
        for role, client in self.clients:
            with self.subTest(role=role), CaptureQueriesContext(connection) as queries:
                response = client.get(self.url(role))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["role"], role)
            self.assertEqual(response.json()["snapshot"]["phase"], "prepared")
            self.assert_private(response.json()["snapshot"])
            self.assertIn("no-store", response["Cache-Control"])
            self.assertIn("Authorization", response["Vary"])
            self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        self.assertEqual(State.objects.values().get(), before)
        self.assertEqual(HubParticipant.objects.filter(session=self.hub).count(), 3)
        self.assertEqual(DuelActionReceipt.objects.count(), 1)
        self.assertEqual(resolve(self.url("host")).view_name, "duel:estimation_host")
        self.assertEqual(resolve(reverse("estimation:join")).app_name, "estimation")

    def test_authenticated_host_and_two_separate_parties_complete_real_http_flow(self):
        self.both_http()
        self.assertEqual(self.phase(), Phase.AWAITING_REVEAL)
        self.assertEqual(sorted(DuelSubmission.objects.values_list("participant_id", flat=True)),
                         sorted([self.party_a.pk, self.party_b.pk]))
        self.assertEqual(sorted(DuelSubmission.objects.values_list("acceptance_sequence", flat=True)), [1, 2])
        self.assertFalse(DuelScoreEntry.objects.exists())
        for action, phase in (("show_answers", "show_answers"), ("show_solution", "show_solution"),
                              ("show_result", "show_result"), ("complete", "completed")):
            response = self.host_action(action)
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(response.json()["snapshot"]["phase"], phase)
        self.assertEqual(DuelScoreEntry.objects.count(), 2)
        self.assertEqual(self.spectator.get(self.url("spectator")).json()["snapshot"]["points"], {"a": 1, "b": 0})
        host_receipt = DuelActionReceipt.objects.get(action_type="duel.estimation.start_round")
        self.assertEqual(host_receipt.actor_key, str(self.user.pk))

    def test_foreign_session_match_and_unit_are_not_selected_by_unchecked_ids(self):
        foreign_hub = HubSession.objects.create(code="FOREIGN-HTTP", creator=self.user)
        foreign_step = HubGameStep.objects.create(session=foreign_hub, order=0, game_key="estimation",
            game_format="duel", room_code=self.quiz.room_code)
        a, b = [HubParticipant.objects.create(session=foreign_hub, nickname=n) for n in ("Foreign A", "Foreign B")]
        foreign = DuelGameInstance.objects.create(hub_session=foreign_hub, hub_game_step=foreign_step,
            game_key="estimation", participant_a=a, participant_b=b)
        unit = DuelUnit.objects.create(game_instance=foreign, number=1)
        for role, client in self.clients:
            self.assertEqual(client.get(self.url(role, instance_id=foreign.pk)).status_code, 404)
            self.assertEqual(client.get(self.url(role, unit_id=unit.pk)).status_code, 404)
        response = self.client_a.get(self.url(instance_id=foreign.pk, unit_id=unit.pk, session_code=foreign_hub.code))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(DuelActionReceipt.objects.count(), 1)

    def test_other_pairing_in_same_session_excludes_nonparties(self):
        foreign = DuelGameInstance.objects.create(hub_session=self.hub, hub_game_step=self.step,
            game_key="estimation", participant_a=self.party_b, participant_b=self.third)
        unit = DuelUnit.objects.create(game_instance=foreign, number=1)
        response = self.client_a.get(self.url(instance_id=foreign.pk, unit_id=unit.pk))
        self.assertEqual(response.status_code, 403)

    def test_missing_forged_wrong_salt_and_third_party_tokens_are_rejected(self):
        from django.core import signing
        for token in (None, "not-a-token", issue_rejoin_token(self.party_a)+"x",
                      issue_rejoin_token(self.third), signing.dumps({"session_id": self.hub.pk, "participant_id": self.party_a.pk})):
            client = Client(enforce_csrf_checks=True, HTTP_AUTHORIZATION="Bearer " + token if token else "")
            self.assertEqual(client.get(self.url()).status_code, 403)
        self.assertFalse(DuelSubmission.objects.exists())

    def test_host_requires_real_user_and_both_game_and_session_authority(self):
        self.assertEqual(self.client_a.get(self.url("host"), HTTP_X_ROLE="host").status_code, 403)
        self.assertEqual(self.spectator.get(self.url("host")).status_code, 403)
        outsider = User.objects.create_user("outside-host", is_staff=True)
        client = Client()
        client.force_login(outsider)
        self.assertEqual(client.get(self.url("host")).status_code, 403)
        self.hub.creator = outsider
        self.hub.save(update_fields=["creator"])
        self.assertEqual(self.host_client.get(self.url("host")).status_code, 403)
        self.assertEqual(client.get(self.url("host")).status_code, 403)  # session owner alone is insufficient

    def test_participant_cannot_submit_a_host_action_or_select_the_opponent(self):
        self.begin()
        before = State.objects.values().get()
        response = self.post(self.client_a, "host", {"action": "show_solution", "client_action_id": str(uuid4())})
        self.assertEqual(response.status_code, 403)
        for extra in ({"participant_id": self.party_b.pk}, {"actor_type": "host"}, {"role": "host"},
                      {"action": "show_result"}, {"acceptance_sequence": 1}, {"target": 100}):
            self.assertEqual(self.submit(**extra).status_code, 400)
        self.assertEqual(State.objects.values().get(), before)
        self.assertFalse(DuelSubmission.objects.exists())

    def test_host_has_no_participant_identity_without_a_signed_lobby_credential(self):
        self.assertEqual(self.host_client.get(self.url()).status_code, 403)

    def test_unreleased_data_are_absent_for_every_role_and_every_response(self):
        self.begin()
        first = self.submit()
        self.assert_private(first.json()["snapshot"])
        self.assertEqual(first.json()["snapshot"]["own_answer"]["value"], "95")
        other = self.client_b.get(self.url()).json()["snapshot"]
        self.assert_private(other)
        self.assertIsNone(other["own_answer"])
        self.assertNotIn("payload", first.json()["action"])
        for role, client in self.clients:
            self.assert_private(client.get(self.url(role)).json()["snapshot"])
        second = self.submit(self.client_b, "120")
        self.assert_private(second.json()["snapshot"])
        self.assertEqual(second.json()["snapshot"]["phase"], "awaiting_reveal")
        for phase, action in (("show_answers", "show_answers"), ("show_solution", "show_solution"),
                              ("show_result", "show_result")):
            self.host_action(action)
            for role, client in self.clients:
                state = client.get(self.url(role)).json()["snapshot"]
                self.assertEqual(state["phase"], phase)
                self.assertEqual(state["answers"]["b"]["value"], "120")
                if phase == "show_answers":
                    self.assertNotIn("target", state)
                else:
                    self.assertEqual(state["target"]["value"], "100")
                if phase != "show_result":
                    self.assertNotIn("points", state)

    def test_caller_cannot_select_a_future_projection_phase(self):
        self.begin()
        for role, client in self.clients:
            for query in ("reveal=true", "phase=show_result", "participant_id="+str(self.party_b.pk)):
                response = client.get(self.url(role)+"?"+query)
                self.assertEqual(response.status_code, 400)
                self.assertNotIn("target", response.json())
        self.assertEqual(self.phase(), Phase.ANSWERING)

    def test_deadline_is_checked_inside_commit_and_get_does_not_reconcile(self):
        self.begin()
        deadline = State.objects.get(unit=self.unit).deadline
        with patch("django.utils.timezone.now", return_value=deadline):
            response = self.client_a.post(self.url(), data=json.dumps({"client_action_id": str(uuid4()),
                "payload": {"value": "95"}}), content_type="application/json",
                HTTP_X_CSRFTOKEN=self.client_a.cookies["csrftoken"].value)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["action"]["code"], "answer_deadline_passed")
            with CaptureQueriesContext(connection) as queries:
                snapshot = self.client_a.get(self.url()).json()["snapshot"]
            self.assertTrue(snapshot["reconciliation_required"])
            self.assertFalse(snapshot["can_answer"])
            self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
            # The internal entry point remains explicit, not a polling side effect.
            reconcile_estimation_deadline(instance_id=self.instance.pk, unit_id=self.unit.pk,
                actor=ActorContext(ReceiptActorType.HOST, self.user.pk), client_action_id=str(uuid4()))
        self.assertEqual(self.phase(), Phase.AWAITING_REVEAL)
        self.assertFalse(DuelSubmission.objects.exists())
        self.assertFalse(DuelScoreEntry.objects.exists())

    def test_identical_and_conflicting_submission_retries_use_existing_receipts(self):
        self.begin()
        key = str(uuid4())
        first = self.submit(client_action_id=key)
        row = DuelSubmission.objects.values().get()
        retry = self.submit(client_action_id=key)
        self.assertEqual(retry.status_code, 200)
        self.assertTrue(retry.json()["action"]["replayed"])
        self.assertEqual(retry.json()["action"]["state_revision"], first.json()["action"]["state_revision"])
        self.assertEqual(DuelSubmission.objects.values().get(), row)
        conflict = self.submit(value="96", client_action_id=key)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["error"]["code"], "action_id_conflict")
        self.assertEqual(self.submit(value="96").status_code, 409)
        self.assertEqual(DuelSubmission.objects.values().get(), row)

    def test_host_retry_after_phase_change_returns_safe_current_projection(self):
        self.both_http()
        key = str(uuid4())
        self.assertEqual(self.host_action("show_answers", client_action_id=key).status_code, 200)
        self.host_action("show_solution")
        state = State.objects.values().get()
        retry = self.host_action("show_answers", client_action_id=key)
        self.assertTrue(retry.json()["action"]["replayed"])
        self.assertEqual(retry.json()["snapshot"]["phase"], "show_solution")
        self.assertEqual(State.objects.values().get(), state)
        conflict = self.host_action("show_result", client_action_id=key)
        self.assertEqual(conflict.status_code, 409)
        self.assertFalse(DuelScoreEntry.objects.exists())

    def test_reload_new_clients_reconstruct_each_saved_phase_and_own_commit(self):
        self.both_http()
        for action in (None, "show_answers", "show_solution", "show_result", "complete"):
            if action:
                self.assertEqual(self.host_action(action).status_code, 200)
            clients = [("participant", self.player_client(self.party_a)), ("participant", self.player_client(self.party_b)),
                       ("spectator", Client()), ("host", Client())]
            clients[-1][1].force_login(self.user)
            for role, client in clients:
                with patch("django.utils.timezone.now", return_value=self.clock+timedelta(seconds=15)):
                    original = dict(self.host_client.get(self.url("host")).json()["snapshot"])
                    state = client.get(self.url(role)).json()["snapshot"]
                self.assertEqual(state["phase"], original["phase"])
                self.assertEqual(state["deadline"], original["deadline"])
                self.assertEqual(state["state_revision"], original["state_revision"])
                if action is None and role == "participant":
                    self.assertTrue(state["committed"])
                    self.assertIsNotNone(state["own_answer"])
                if action is None:
                    self.assert_private(state)

    def test_nonduel_formats_and_other_game_types_are_never_transport_enabled(self):
        for value in ("standard", "tournament", "one_vs_many"):
            HubGameStep.objects.filter(pk=self.step.pk).update(game_format=value)
            for role, client in self.clients:
                self.assertEqual(client.get(self.url(role)).status_code, 404)
        HubGameStep.objects.filter(pk=self.step.pk).update(game_format="duel")
        DuelGameInstance.objects.filter(pk=self.instance.pk).update(game_key="buzzer")
        self.assertEqual(self.spectator.get(self.url("spectator")).status_code, 404)

    def test_pure_duel_session_also_uses_the_same_verified_transport(self):
        self.hub.session_type = "duel"
        self.hub.save(update_fields=["session_type"])
        self.begin()
        self.assertEqual(self.submit().status_code, 200)

    def test_csrf_and_read_only_spectator_cannot_be_bypassed(self):
        data = {"action": "start", "duration_seconds": 60, "client_action_id": str(uuid4())}
        self.assertEqual(self.host_client.post(self.url("host"), data=json.dumps(data), content_type="application/json").status_code, 403)
        self.assertEqual(self.spectator.post(self.url("spectator"), data=json.dumps(data), content_type="application/json").status_code, 403)
        self.assertEqual(self.host_client.put(self.url("spectator"),
            HTTP_X_CSRFTOKEN=self.host_client.cookies["csrftoken"].value).status_code, 405)
        self.assertEqual(self.post(self.host_client, "spectator", data).status_code, 405)
        self.assertEqual(self.host_client.get(self.url("host"), HTTP_X_ROLE="spectator").json()["role"], "host")
        self.assertEqual(self.phase(), Phase.PREPARED)

    def test_malformed_json_and_unsupported_commands_make_no_writes(self):
        for body in ('[]', 'null', '{', '{"action":"start","action":"show_result"}', '{"a":NaN}'):
            response = self.host_client.post(self.url("host"), data=body, content_type="application/json",
                HTTP_X_CSRFTOKEN=self.host_client.cookies["csrftoken"].value)
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.host_action("reconcile_deadline").status_code, 400)
        self.assertEqual(self.host_action("bind_question").status_code, 400)
        self.assertEqual(self.host_action("show_result").status_code, 409)
        for bad_id in (False, 2, [], {}, "not-a-uuid"):
            self.assertEqual(self.submit(draft_id=bad_id).status_code, 400)
        self.assertEqual(self.phase(), Phase.PREPARED)
        self.assertFalse(DuelSubmission.objects.exists())
        self.assertFalse(DuelScoreEntry.objects.exists())

    def test_missing_answers_can_be_revealed_and_complete_zero_zero(self):
        self.begin()
        deadline = State.objects.get(unit=self.unit).deadline
        with patch("django.utils.timezone.now", return_value=deadline):
            reconcile_estimation_deadline(instance_id=self.instance.pk, unit_id=self.unit.pk,
                actor=ActorContext(ReceiptActorType.HOST, self.user.pk), client_action_id=str(uuid4()))
        self.clock = deadline
        self.assertEqual(self.host_action("show_answers").status_code, 200)
        self.assertEqual(self.host_action("show_solution").status_code, 200)
        result = self.host_action("show_result")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['snapshot']['points'], {'a':0, 'b':0})
        self.assertEqual(self.host_action("complete").status_code, 200)
        public = self.spectator.get(self.url("spectator")).json()["snapshot"]
        self.assertEqual(public["answers"], {"a": None, "b": None})
        self.assertEqual(public["target"]["value"], "100")
        self.assertEqual(public['points'], {'a':0,'b':0})
        self.assertEqual(public['round_outcome'], 'no_winner')
        self.assertEqual(DuelScoreEntry.objects.count(), 2)

    def test_match_win_still_allows_final_presentation_but_no_new_game_action(self):
        self.configure(threshold=1)
        self.both_http()
        for action in ("show_answers", "show_solution", "show_result", "complete"):
            self.assertEqual(self.host_action(action).status_code, 200)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.match_status, "completed")
        self.assertEqual(self.host_action("start", duration_seconds=60).status_code, 409)
        self.assertEqual(self.submit(value="50").status_code, 409)
        self.assertEqual(DuelScoreEntry.objects.count(), 2)

    def test_exception_responses_never_disclose_material_or_leave_partial_commits(self):
        self.begin()
        self.instance.refresh_from_db()
        revision = self.instance.state_revision
        before = State.objects.values().get()
        with patch.object(DuelActionReceipt, "save", side_effect=IntegrityError("secret target: 100")):
            with self.assertLogs("duel.views", level="ERROR"):
                response = self.submit()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"error": {"code": "internal_error"}})
        self.assertNotIn("100", response.content.decode())
        self.assertFalse(DuelSubmission.objects.exists())
        self.assertEqual(State.objects.values().get(), before)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.state_revision, revision)
        self.assertEqual(DuelActionReceipt.objects.count(), 2)  # binding + start
        with patch.object(State, "full_clean", side_effect=RuntimeError("secret solution: 100")):
            with self.assertLogs("duel.views", level="ERROR"):
                response = self.spectator.get(self.url("spectator"))
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"error": {"code": "internal_error"}})
