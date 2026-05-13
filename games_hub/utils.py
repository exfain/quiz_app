import socket

def get_server_ip(default='127.0.0.1'):
    """Return the server's primary non-loopback IPv4 address."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # The IP here doesn't need to be reachable; no packets are sent.
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = default
    finally:
        s.close()
    return ip
