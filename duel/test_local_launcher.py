from types import SimpleNamespace
from unittest.mock import Mock, patch
from django.test import SimpleTestCase
from launch_website import serve_local


class LocalDuelWorkerLauncherTests(SimpleTestCase):
    def test_local_server_launch_also_runs_worker_and_cleans_it_up(self):
        worker=SimpleNamespace(terminate=Mock(),wait=Mock(return_value=0))
        with patch('launch_website.subprocess.Popen',return_value=worker) as spawn:
            with patch('launch_website.subprocess.call',return_value=0) as server:
                self.assertEqual(serve_local(8123),0)
        self.assertIn('duel_deadlines',spawn.call_args.args[0])
        self.assertIn('0.0.0.0:8123',server.call_args.args[0])
        worker.terminate.assert_called_once()
        worker.wait.assert_called_once_with(timeout=5)

    def test_failed_server_launch_still_terminates_worker(self):
        worker=SimpleNamespace(terminate=Mock(),wait=Mock())
        with patch('launch_website.subprocess.Popen',return_value=worker):
            with patch('launch_website.subprocess.call',side_effect=OSError('server failed')):
                with self.assertRaises(OSError):serve_local(8123)
        worker.terminate.assert_called_once()
