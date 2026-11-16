import contextlib
import io
import os
import socket
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from launch_website import LOGIN_MARKER, choose_port, dashboard_ready, main, wait_for_dashboard


class LauncherTests(unittest.TestCase):
    def setUp(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(self.server.status)
                self.send_header('Content-Type', self.server.content_type)
                self.end_headers()
                self.wfile.write(self.server.body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.status = 200
        self.server.content_type = 'text/html; charset=utf-8'
        self.server.body = LOGIN_MARKER
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_loopback_service_is_not_reused_for_wildcard_django_binding(self):
        with self.assertRaises(RuntimeError):
            choose_port(first_port=self.port, attempts=1)
        selected = choose_port(first_port=self.port, attempts=20)
        self.assertGreater(selected, self.port)
        with socket.socket() as probe:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(('0.0.0.0', selected))

    def test_actual_dashboard_response_is_ready(self):
        self.assertTrue(dashboard_ready(self.port))

    def test_godot_transport_error_is_not_a_ready_dashboard(self):
        self.server.status = 401
        self.server.content_type = 'application/json'
        self.server.body = b'{"code":"TRANSPORT_AUTH_REQUIRED"}'
        self.assertFalse(dashboard_ready(self.port))

    def test_unrelated_successful_service_is_not_a_ready_dashboard(self):
        self.server.body = b'<html>Other service</html>'
        self.assertFalse(dashboard_ready(self.port))

    def test_startup_waits_until_dashboard_is_ready(self):
        with patch('launch_website.dashboard_ready', side_effect=[False, True]) as ready, patch('launch_website.time.sleep'):
            self.assertTrue(wait_for_dashboard(self.port))
        self.assertEqual(ready.call_count, 2)

    def test_failed_startup_returns_error_without_claiming_success(self):
        with patch('launch_website.wait_for_dashboard', return_value=False), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['wait', str(self.port)]), 1)

    def test_wait_timeout_is_bounded(self):
        with patch('launch_website.dashboard_ready', return_value=False), patch('launch_website.time.monotonic', side_effect=[0, 0, 61]), patch('launch_website.time.sleep'):
            self.assertFalse(wait_for_dashboard(self.port, timeout=60))

    @unittest.skipUnless(os.name == 'nt', 'Windows batch launcher')
    def test_batch_port_selection_runs_in_cmd(self):
        root = Path(__file__).resolve().parent.parent
        source = (root / 'launch-website.bat').read_text(encoding='utf-8')
        selection = next(line for line in source.splitlines() if line.startswith('for /f '))
        with tempfile.TemporaryDirectory(prefix='qa launcher ') as directory:
            script = Path(directory) / 'port selection.cmd'
            script.write_text('@echo off\nsetlocal\nset "QUIZ_WEBSITE_PORT="\n' + selection
                + '\nif not defined QUIZ_WEBSITE_PORT exit /b 1\necho %QUIZ_WEBSITE_PORT%\n', encoding='ascii')
            result = subprocess.run(['cmd', '/d', '/c', str(script)], cwd=root, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        selected = int(result.stdout.strip())
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(('0.0.0.0', selected))


if __name__ == '__main__':
    unittest.main()
