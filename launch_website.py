"""Port selection and readiness checks for the Windows website launcher."""
import argparse
import socket
import sys
import time
import subprocess
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener


LOGIN_MARKER = b'<title>Admin Login - QuizMaster</title>'


def choose_port(first_port=8000, attempts=20):
    for port in range(first_port, min(first_port + attempts, 65536)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                # A loopback-only service can take precedence over a wildcard
                # listener on Windows. Check the full binding used by Django.
                probe.bind(('0.0.0.0', port))
            except OSError:
                continue
            return port
    raise RuntimeError('Kein freier Port fuer die Website gefunden.')


def dashboard_ready(port):
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(f'http://127.0.0.1:{port}/admin-dashboard/login/', timeout=1) as response:
            return (
                response.status == 200
                and response.headers.get_content_type() == 'text/html'
                and LOGIN_MARKER in response.read(131072)
            )
    except HTTPError as error:
        error.close()
        return False
    except (OSError, URLError):
        return False


def wait_for_dashboard(port, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if dashboard_ready(port):
            return True
        time.sleep(0.25)
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('port')
    wait = commands.add_parser('wait')
    wait.add_argument('port', type=int)
    serve = commands.add_parser('serve')
    serve.add_argument('port', type=int)
    args = parser.parse_args(argv)
    if args.command == 'port':
        try:
            print(choose_port())
        except RuntimeError as error:
            print(f'ERROR: {error}', file=sys.stderr)
            return 1
        return 0
    if not 1 <= args.port <= 65535:
        parser.error('Port muss zwischen 1 und 65535 liegen.')
    if args.command == 'serve':
        return serve_local(args.port)
    if not wait_for_dashboard(args.port):
        print('ERROR: Das QuizMaster-Dashboard ist nicht erreichbar. Bitte die Django-Servermeldung pruefen.', file=sys.stderr)
        return 1
    return 0


def serve_local(port):
    """One local launcher owns server and worker lifetimes; no autoreloader duplicates."""
    root = Path(__file__).resolve().parent
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    worker = subprocess.Popen([sys.executable, 'manage.py', 'duel_deadlines'], cwd=root, creationflags=flags)
    try:
        return subprocess.call([sys.executable, 'manage.py', 'runserver', f'0.0.0.0:{port}'], cwd=root)
    finally:
        worker.terminate()
        try:
            worker.wait(timeout=5)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait()


if __name__ == '__main__':
    sys.exit(main())
