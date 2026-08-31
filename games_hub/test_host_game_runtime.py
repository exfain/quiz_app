import shutil
import subprocess
from pathlib import Path
from unittest import skipUnless

from django.contrib.staticfiles import finders
from django.test import SimpleTestCase, TestCase

from games_hub.authoritative_state import attach_snapshot_metadata
from games_hub.models import GameRuntimeState


class HostGameRuntimeContractTests(SimpleTestCase):
    monitor_templates = {
        'buzzer': Path('templates/admin_dashboard/buzzer_monitor.html'),
        'host_points': Path('templates/admin_dashboard/host_points_monitor.html'),
        'wann_war_das': Path('templates/admin_dashboard/wann_war_das_monitor.html'),
        'wer_weiss_mehr': Path('templates/admin_dashboard/wer_weiss_mehr_monitor.html'),
    }

    def test_all_newer_host_monitors_use_the_shared_runtime_adapter(self):
        for game_key, template_path in self.monitor_templates.items():
            with self.subTest(game_key=game_key):
                source = template_path.read_text(encoding='utf-8')
                self.assertIn("js/authoritative-game-state.js", source)
                self.assertIn("js/host-game-runtime.js", source)
                self.assertIn('window.HostGameRuntime.create({', source)
                self.assertIn('hostRuntime.connectSocket({', source)
                self.assertIn('hostRuntime.acceptSnapshot(', source)

    def test_wann_war_das_uses_absolute_clock_without_full_state_polling(self):
        source = self.monitor_templates['wann_war_das'].read_text(encoding='utf-8')

        self.assertNotIn("setInterval(() => send({ type: 'get_state' }), 1000)", source)
        self.assertIn('hostRuntime.serverNow() - startedAt', source)
        self.assertIn('hostRuntime.remainingMilliseconds(state)', source)
        self.assertIn('window.setTimeout(() => hostRuntime.requestState()', source)
        self.assertIn('Math.floor(elapsedSeconds / secondsPerStep)', source)

    def test_wer_weiss_mehr_orders_http_and_websocket_state_and_uses_server_clock(self):
        source = self.monitor_templates['wer_weiss_mehr'].read_text(encoding='utf-8')

        self.assertIn('hostRuntime.fetchSnapshot(', source)
        self.assertIn("key: 'wer-weiss-mehr-state'", source)
        self.assertIn("hostRuntime.acceptSnapshot(nextState, {source: 'application'})", source)
        self.assertIn('hostRuntime.remainingMilliseconds(s)', source)
        self.assertNotIn('new Date(s.timer.ends_at).getTime() - Date.now()', source)
        self.assertNotIn('pendingWsMessages', source)
        self.assertIn("hostRuntime.beginAction('open-round'", source)

    def test_runtime_contains_revision_http_pending_reconnect_and_error_guards(self):
        script_path = finders.find('js/host-game-runtime.js')
        self.assertIsNotNone(script_path)
        source = Path(script_path).read_text(encoding='utf-8')

        self.assertIn('incomingRevision < revision', source)
        self.assertIn('incomingRevision === revision', source)
        self.assertIn('previous.controller.abort()', source)
        self.assertIn('current !== request', source)
        self.assertIn('pendingActions.has(actionKey)', source)
        self.assertIn("options.onConnectionChange('reconnecting')", source)
        self.assertIn("code: 'socket_disconnected'", source)
        self.assertIn('deadline - serverNow()', source)

    @skipUnless(shutil.which('node'), 'Node.js is required for the host runtime regression test.')
    def test_runtime_rejects_stale_state_and_reconstructs_server_time(self):
        script_path = Path(finders.find('js/host-game-runtime.js')).resolve()
        node_script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

let monotonicNow = 1000;
const rendered = [];
const errors = [];
global.window = global;
global.performance = {now: () => monotonicNow};
global.AuthoritativeGameState = {
    acceptSnapshot: () => true,
    createActionId: () => 'action-id',
};
class FakeWebSocket {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSED = 3;
    constructor() {
        this.readyState = FakeWebSocket.CONNECTING;
        this.sent = [];
        FakeWebSocket.instances.push(this);
    }
    send(raw) { this.sent.push(JSON.parse(raw)); }
    close() { this.readyState = FakeWebSocket.CLOSED; }
}
FakeWebSocket.instances = [];
global.WebSocket = FakeWebSocket;
vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'));

const runtime = HostGameRuntime.create({
    render: state => rendered.push(state.state_revision),
    onError: error => errors.push(error.code),
});
const base = {
    game_key: 'test', room_code: 'ROOM01', session_id: 'SESSION1',
    server_now: '2026-01-01T00:00:00.000Z',
};
assert.strictEqual(runtime.acceptSnapshot({...base, state_revision: 2}), true);
assert.strictEqual(runtime.acceptSnapshot({...base, state_revision: 1}), false);
assert.strictEqual(runtime.acceptSnapshot({...base, state_revision: 2}), false);
assert.strictEqual(runtime.acceptSnapshot({...base, state_revision: 3}), true);
assert.deepStrictEqual(rendered, [2, 3]);

assert.strictEqual(runtime.beginAction('round'), true);
assert.strictEqual(runtime.beginAction('round'), false);
runtime.acceptSnapshot({...base, state_revision: 3});
assert.strictEqual(runtime.beginAction('round'), false);
runtime.acceptSnapshot({...base, state_revision: 4});
assert.strictEqual(runtime.beginAction('round'), true);
runtime.completeAction('round');

runtime.acceptSnapshot({
    ...base,
    state_revision: 5,
    ends_at: '2026-01-01T00:01:40.000Z',
});
monotonicNow += 25000;
assert.strictEqual(Math.round(runtime.remainingMilliseconds(runtime.snapshot) / 1000), 75);
monotonicNow += 25000;
assert.strictEqual(Math.round(runtime.remainingMilliseconds(runtime.snapshot) / 1000), 50);
monotonicNow += 40000;
assert.strictEqual(Math.round(runtime.remainingMilliseconds(runtime.snapshot) / 1000), 10);

const socketRuntime = HostGameRuntime.create({
    render: state => rendered.push(state.state_revision),
    onError: error => errors.push(error.code),
});
socketRuntime.connectSocket({url: 'ws://test', reconnectDelay: 100000, onMessage: data => {
    socketRuntime.acceptSnapshot(data);
}});
const socket = FakeWebSocket.instances[0];
socket.readyState = FakeWebSocket.OPEN;
socket.onopen();
socket.onmessage({data: JSON.stringify({...base, state_revision: 6})});
const beforeDisconnect = rendered.length;
socket.onclose();
assert.strictEqual(rendered.length, beforeDisconnect);
socketRuntime.dispose();

let pendingFetches = [];
global.fetch = (url, options) => new Promise(resolve => pendingFetches.push({resolve, options}));
const httpRuntime = HostGameRuntime.create({render: state => rendered.push(state.state_revision)});
const first = httpRuntime.fetchSnapshot('/state', {}, {key: 'state'});
const second = httpRuntime.fetchSnapshot('/state', {}, {key: 'state'});
assert.strictEqual(pendingFetches[0].options.signal.aborted, true);
pendingFetches[1].resolve({ok: true, json: async () => ({...base, state_revision: 8})});
second.then(() => {
    pendingFetches[0].resolve({ok: true, json: async () => ({...base, state_revision: 7})});
    return first;
}).then(() => {
    assert.strictEqual(httpRuntime.revision, 8);
    assert.strictEqual(rendered.at(-1), 8);
}).catch(error => {
    console.error(error);
    process.exitCode = 1;
});
"""
        result = subprocess.run(
            ['node', '-e', node_script, str(script_path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f'Node runtime test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}',
        )


class HostSnapshotRevisionTests(TestCase):
    def test_public_host_changes_advance_revision_without_audience_or_timer_churn(self):
        base = {
            'game': {'status': 'active'},
            'participants': [{'name': 'Alice', 'score': 0}],
            'timer': {'remaining_seconds': 30},
            '_revision_state': {
                'game': {'status': 'active'},
                'participants': [{'name': 'Alice', 'score': 0}],
            },
        }
        initial = attach_snapshot_metadata(
            base,
            game_key='host_points',
            room_code='RUNTIME-REVISION',
        )
        runtime = GameRuntimeState.objects.get(room_code='RUNTIME-REVISION')
        initial_updated_at = runtime.updated_at
        audience_variant = attach_snapshot_metadata(
            {
                **base,
                'participant': {'name': 'Alice'},
                'timer': {'remaining_seconds': 15},
            },
            game_key='host_points',
            room_code='RUNTIME-REVISION',
        )
        runtime.refresh_from_db()
        score_changed = attach_snapshot_metadata(
            {
                **base,
                'participants': [{'name': 'Alice', 'score': 1}],
                '_revision_state': {
                    'game': {'status': 'active'},
                    'participants': [{'name': 'Alice', 'score': 1}],
                },
            },
            game_key='host_points',
            room_code='RUNTIME-REVISION',
        )

        self.assertEqual(audience_variant['state_revision'], initial['state_revision'])
        self.assertEqual(runtime.updated_at, initial_updated_at)
        self.assertGreater(score_changed['state_revision'], initial['state_revision'])
        self.assertNotIn('_revision_state', score_changed)
