"""No test connects to a host outside this machine (#633, #656).

Importing backend loads a developer's .env, which holds real provider
keys, and CI runs with network access. A test whose stub did not reach the
code under test then sent real requests to a model provider (about 2,000
token counts per full run, #633), or left hundreds of connections open
until the run stalled (#656).

``install()``, called by conftest.py before any test module is imported,
replaces the socket module's name lookups and ``connect`` with versions
that refuse a host outside this machine: the call raises
OutsideConnectionRefused, naming the host, before any packet is sent. It
is a BaseException, so neither the provider SDKs' retry loops (they catch
Exception and send again) nor the code's own fallbacks swallow it. Every
refusal is also recorded (``take``), and conftest.py fails the test that
made it even when something caught the exception.

Allowed: loopback addresses and the name localhost, Unix sockets, and the
hosts of the database and broker URLs in the environment (a test Postgres
or Redis the developer points the tests at, and the addresses those names
resolve to).

Not covered: sockets a C library opens without Python's socket module.
"""
import ipaddress
import os
import socket
import threading
from urllib.parse import urlsplit

# The URL schemes whose hosts are local services a run may use.
_SERVICE_SCHEMES = ("postgres", "postgresql", "redis", "rediss", "amqp")
_LOCAL_NAMES = frozenset(("", "localhost", "localhost.localdomain",
                          "ip6-localhost", "ip6-loopback"))


class OutsideConnectionRefused(BaseException):
    """A test tried to connect to a host outside this machine."""


_real = {}
_refused = []      # "host:port" of every refusal not yet taken
_resolved = set()  # addresses that an allowed name resolved to


def _text(host):
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode("ascii", "replace")
    return str(host).strip().rstrip(".").lower()


def _ip(host):
    try:
        return ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None


def _service_hosts():
    hosts = set()
    for value in os.environ.values():
        if "://" not in value:
            continue
        try:
            parts = urlsplit(value)
            hostname = parts.hostname
        except ValueError:
            continue
        if hostname and parts.scheme.split("+")[0] in _SERVICE_SCHEMES:
            hosts.add(_text(hostname))
    return hosts


def is_local(host):
    """True for a host a test may connect to (see the module docstring)."""
    if host is None:
        return True
    host = _text(host)
    if host in _LOCAL_NAMES or host.endswith(".localhost"):
        return True
    ip = _ip(host)
    if ip is not None:
        mapped = getattr(ip, "ipv4_mapped", None)
        if ip.is_loopback or ip.is_unspecified or (
                mapped is not None and mapped.is_loopback):
            return True
        if str(ip) in _resolved:
            return True
    return host in _service_hosts()


FROM_THREAD = " from thread "


def _refuse(host, port):
    where = _text(host) if port is None else f"{_text(host)}:{port}"
    thread = threading.current_thread()
    if thread is not threading.main_thread():
        # A thread an earlier test left running is not the running test.
        where += f"{FROM_THREAD}{thread.name!r}"
    _refused.append(where)
    raise OutsideConnectionRefused(
        f"A test tried to connect to {where}, a host outside this machine. "
        "Tests make no outside connections (backend/tests/network_guard.py):"
        " stub the client or the function that makes the call.")


def _remember(address):
    ip = _ip(_text(address))
    if ip is not None:
        _resolved.add(str(ip))


def _getaddrinfo(host, port, *args, **kwargs):
    if not is_local(host):
        _refuse(host, port)
    results = _real["getaddrinfo"](host, port, *args, **kwargs)
    for *_, sockaddr in results:
        if sockaddr and isinstance(sockaddr[0], (str, bytes)):
            _remember(sockaddr[0])
    return results


def _gethostbyname(host):
    if not is_local(host):
        _refuse(host, None)
    address = _real["gethostbyname"](host)
    _remember(address)
    return address


def _gethostbyname_ex(host):
    if not is_local(host):
        _refuse(host, None)
    name, aliases, addresses = _real["gethostbyname_ex"](host)
    for address in addresses:
        _remember(address)
    return name, aliases, addresses


def _target(sock, address):
    """(host, port) of an internet socket's address, else None (a Unix
    socket or another family: nothing outside this machine)."""
    if sock.family not in (socket.AF_INET, socket.AF_INET6):
        return None
    if not isinstance(address, tuple) or not address:
        return None
    return address[0], (address[1] if len(address) > 1 else None)


def _connect(self, address):
    target = _target(self, address)
    if target is not None and not is_local(target[0]):
        _refuse(*target)
    return _real["connect"](self, address)


def _connect_ex(self, address):
    target = _target(self, address)
    if target is not None and not is_local(target[0]):
        _refuse(*target)
    return _real["connect_ex"](self, address)


def install():
    """Refuse outside hosts from now on in this process. Idempotent."""
    if _real:
        return
    _real.update(getaddrinfo=socket.getaddrinfo,
                 gethostbyname=socket.gethostbyname,
                 gethostbyname_ex=socket.gethostbyname_ex,
                 connect=socket.socket.connect,
                 connect_ex=socket.socket.connect_ex)
    socket.getaddrinfo = _getaddrinfo
    socket.gethostbyname = _gethostbyname
    socket.gethostbyname_ex = _gethostbyname_ex
    socket.socket.connect = _connect
    socket.socket.connect_ex = _connect_ex


def take():
    """The refusals recorded since the last call ("host:port" each, with
    " from thread 'name'" when not the main thread's), which are then
    forgotten."""
    taken = _refused[:]
    del _refused[:len(taken)]
    return taken
