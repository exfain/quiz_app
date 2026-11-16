import socket
from ipaddress import IPv4Address, ip_address
from urllib.parse import urlsplit, urlunsplit

from django.urls import reverse


def _usable_ipv4(value):
    try:
        address = IPv4Address(value)
    except (ValueError, TypeError):
        return None
    if address.is_loopback or address.is_unspecified or address.is_link_local or address.is_multicast or address.is_reserved:
        return None
    return str(address)

def get_server_ip(default='127.0.0.1'):
    """Return the server's primary non-loopback IPv4 address."""
    try:
        # The IP here doesn't need to be reachable; no packets are sent.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(('8.8.8.8', 80))
            address = _usable_ipv4(probe.getsockname()[0])
            if address:
                return address
    except OSError:
        pass
    # LAN-only networks may have no default route to the internet.
    try:
        for entry in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_DGRAM):
            address = _usable_ipv4(entry[4][0])
            if address:
                return address
    except OSError:
        pass
    return default


def build_share_url(request, path):
    """Replace a local-only browser hostname, preserving scheme, port and path."""
    url = request.build_absolute_uri(path)
    parts = urlsplit(url)
    hostname = (parts.hostname or '').lower().rstrip('.')
    local_only = hostname == 'localhost' or hostname.endswith('.localhost')
    try:
        address = ip_address(hostname)
        local_only = local_only or address.is_loopback or address.is_unspecified
    except ValueError:
        pass
    if not local_only:
        return url
    address = _usable_ipv4(get_server_ip(default=None))
    if not address:
        return url
    netloc = f'{address}:{parts.port}' if parts.port is not None else address
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def session_join_url(request, session_code):
    return build_share_url(request, reverse('games_hub:lobby', args=[session_code]))
