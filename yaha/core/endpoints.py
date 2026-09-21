"""Remote agent communication through ENDPOINTS (EPs).

An ENDPOINT is addressed by a URL - ``http(s)://host[:port]/path/version`` - and
plays BOTH roles under that ONE name:

* it is an EVENT NAME (a stage can declare ``Wait Event: <EP>``), and
* it is a MESSAGE QUEUE (``send_tool`` enqueues; a wait takes one message).

Two kinds exist:

* a SERVICE EP is the listener side. A stage that waits on one makes the runtime
  bind a listener for it; an inbound connect CREATES a CEP and prompts that stage
  WITH the CEP;
* a CEP (communication EP) is ONE conversation. It is a LEVEL signal over a FIFO
  queue: set while messages are queued, unset when drained. It is deliberately
  NOT an ``Event`` - there is no one-shot pulse, no latch and no auto-reset, so a
  wait takes one message and clears nothing.

Transport: HTTP with long-poll, no NEW dependency. Every request is
client-initiated; a message the server initiates is queued and delivered when the
client polls, so an agent cannot tell which side initiated.
"""

import json
import re
import threading
import time
import urllib.error
from collections import namedtuple
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# --- addressing -----------------------------------------------------------

EP_SCHEMES = ("http", "https")
_VERSION_RE = re.compile(r"^v(\d+)$")

EpAddress = namedtuple("EpAddress", "scheme host port path version")


class EndpointError(Exception):
    """A malformed endpoint, or a refused endpoint operation (cap, version...)."""


def is_ep(name):
    """True when a name is an endpoint URL rather than a local event name.

    ONE definition, shared by the wait gate, the tools and the startup
    validator, so 'is this an endpoint?' can never differ between call sites.
    """
    text = str(name or "").strip().lower()
    return text.startswith("http://") or text.startswith("https://")


def parse_ep(url):
    """Split an endpoint URL into ``(scheme, host, port, path, version)``.

    The LAST path segment must be the protocol version (``v1``), so a future v2
    cannot be silently misread by a v1 peer. Raises EndpointError for anything
    that is not a well-formed endpoint.
    """
    text = str(url or "").strip()
    if not text:
        raise EndpointError("empty endpoint URL")
    parts = urlparse(text)
    if parts.scheme not in EP_SCHEMES:
        raise EndpointError(
            "unsupported endpoint scheme %r (expected http or https)" % parts.scheme)
    if not parts.hostname:
        raise EndpointError("endpoint URL has no host: %r" % text)
    try:
        port = parts.port
    except ValueError:
        raise EndpointError("endpoint URL has an invalid port: %r" % text)
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2:
        raise EndpointError(
            "endpoint path must end in a version segment (e.g. /crew/v1): %r" % text)
    version = segments[-1]
    if not _VERSION_RE.match(version):
        raise EndpointError(
            "endpoint path must end in a version segment like v1: %r" % text)
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return EpAddress(scheme=parts.scheme,
                     host=parts.hostname,
                     port=port,
                     path="/" + "/".join(segments[:-1]),
                     version=version)


# --- storage --------------------------------------------------------------

QUEUE_CAP = 100
MAX_CEPS = 32
POLL_TIMEOUT_S = 25
BACKOFF_BASE_S = 1
BACKOFF_MAX_S = 30
MAX_CONSECUTIVE_ERRORS = 5
# How long the scheduler may wait for endpoint activity before it declares a
# deadlock anyway. Bounded on purpose: a peer that never arrives must end the
# run LOUDLY rather than hang it forever.
ENDPOINT_IDLE_TIMEOUT_S = 60.0


class MessageQueue:
    """Bounded FIFO whose ``level`` is the message-queue signal a CEP waits on.

    ``put`` REFUSES (returns False) rather than evicting when the bound is hit,
    so a cap is always REPORTED and never a silent drop. ``take_wait`` backs the
    long-poll read.
    """

    def __init__(self, cap=QUEUE_CAP):
        self.cap = max(1, int(cap))
        self._items = []
        self._cond = threading.Condition()

    def put(self, item):
        with self._cond:
            if len(self._items) >= self.cap:
                return False
            self._items.append(item)
            self._cond.notify_all()
            return True

    def take(self):
        with self._cond:
            return self._items.pop(0) if self._items else None

    def take_wait(self, timeout):
        """Block up to ``timeout`` seconds for one item, else return None."""
        deadline = time.monotonic() + float(timeout)
        with self._cond:
            while not self._items:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)
            return self._items.pop(0)

    def notify(self):
        with self._cond:
            self._cond.notify_all()

    @property
    def level(self):
        with self._cond:
            return bool(self._items)

    def __len__(self):
        with self._cond:
            return len(self._items)


class Cep:
    """One conversation: a level signal (``inbox``) plus a platform to send on.

    ``direction`` is ``inbound`` on the listener side (messages arrive from the
    peer's HTTP POSTs and are fetched by the peer's polls) or ``outbound`` on the
    connecting side (a poller thread fills ``inbox``). The LEVEL the wait gate
    reads is always ``inbox``: set while the peer has messages waiting for us.
    """

    def __init__(self, url, peer="", cap=QUEUE_CAP, direction="inbound", token=""):
        self.url = str(url)
        self.token = str(token or "")
        self.peer = str(peer or "")
        self.direction = str(direction)
        self.inbox = MessageQueue(cap=cap)
        self.outbox = MessageQueue(cap=cap)
        self.closed = False

    # -- the level the wait gate reads -------------------------------------

    def is_set(self):
        """LEVEL read: True while the peer has messages for us. Never clears."""
        return False if self.closed else self.inbox.level

    def take(self):
        return None if self.closed else self.inbox.take()

    def close(self):
        self.closed = True
        self.inbox.notify()
        self.outbox.notify()

    def __repr__(self):
        return "<Cep %s %s %s>" % (self.url, self.direction,
                                   "closed" if self.closed else "open")

# --- HTTP plumbing --------------------------------------------------------


def _bearer(headers, query):
    """The shared token a request presented, from the header or the query."""
    header = str(headers.get("Authorization") or "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    values = query.get("token") or []
    return str(values[0]).strip() if values else ""


def http_post(url, payload=None, token=None, timeout=10.0):
    """POST JSON and return the decoded reply; raises on a transport error."""
    import urllib.request
    data = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + str(token))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def http_get(url, token=None, timeout=POLL_TIMEOUT_S + 10.0):
    """GET, returning the decoded reply, or None for an empty (204) long-poll."""
    import urllib.request
    request = urllib.request.Request(url, method="GET")
    if token:
        request.add_header("Authorization", "Bearer " + str(token))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if int(getattr(response, "status", 200) or 200) == 204:
            return None
        text = response.read().decode("utf-8")
        return json.loads(text) if text else None


class _EndpointHandler(BaseHTTPRequestHandler):
    """Serves one run's endpoints: the connect handshake, send, poll and close.

    Keep-alive is on (``protocol_version = HTTP/1.1``) so ONE connection carries
    a CEP pair, and every request is CLIENT-initiated: the listener never calls
    back, it only answers. ``registry`` is injected per server class.
    """

    protocol_version = "HTTP/1.1"
    server_version = "yaha-endpoint/1"
    registry = None

    def log_message(self, *args):  # keep the TUI clean
        pass

    # -- helpers -----------------------------------------------------------

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception:
            return {}

    def _reply(self, code, payload=None):
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _route(self):
        from urllib.parse import parse_qs, urlparse
        parts = urlparse(self.path)
        path = parts.path.rstrip("/") or "/"
        query = parse_qs(parts.query)
        service, cep_id = self.registry.find_route(path)
        return service, cep_id, query

    # -- verbs -------------------------------------------------------------

    def do_POST(self):
        service, cep_id, query = self._route()
        if service is None:
            return self._reply(404, {"error": "unknown endpoint"})
        if not service.authorized(self.headers, query):
            return self._reply(401, {"error": "unauthorized"})
        if cep_id is None:
            try:
                cep = service.accept(peer=self.client_address[0])
            except EndpointError as exc:
                return self._reply(429, {"error": str(exc)})
            return self._reply(200, {"cep": cep.url})
        cep = service.ceps.get(cep_id)
        if cep is None:
            return self._reply(404, {"error": "unknown conversation"})
        message = str(self._body().get("message") or "")
        if not cep.inbox.put(message):
            return self._reply(429, {"error": "conversation queue is full"})
        self.registry.wake(cep.url)
        return self._reply(200, {"ok": True})

    def do_GET(self):
        service, cep_id, query = self._route()
        if service is None:
            return self._reply(404, {"error": "unknown endpoint"})
        if not service.authorized(self.headers, query):
            return self._reply(401, {"error": "unauthorized"})
        if cep_id is None:
            return self._reply(405, {"error": "a service endpoint has no GET"})
        cep = service.ceps.get(cep_id)
        if cep is None:
            return self._reply(404, {"error": "unknown conversation"})
        timeout = query.get("timeout") or []
        try:
            seconds = float(timeout[0]) if timeout else POLL_TIMEOUT_S
        except ValueError:
            seconds = POLL_TIMEOUT_S
        message = cep.outbox.take_wait(max(0.0, min(seconds, POLL_TIMEOUT_S)))
        if message is None:
            return self._reply(204)
        return self._reply(200, {"message": message})

    def do_DELETE(self):
        service, cep_id, query = self._route()
        if service is None:
            return self._reply(404, {"error": "unknown endpoint"})
        if not service.authorized(self.headers, query):
            return self._reply(401, {"error": "unauthorized"})
        if cep_id is None:
            return self._reply(405, {"error": "a service endpoint cannot be closed"})
        service.close_cep(cep_id)
        return self._reply(200, {"ok": True})

class ServiceEndpoint:
    """The LISTENER side of an EP: an accept queue plus the conversations it made.

    Its LEVEL signal is ``pending``: the CEP urls of connections a client has
    already opened but that no stage has been handed yet. So 'waiting on a
    service EP' and 'waiting on a CEP' are the same read - a level over a queue -
    which is what keeps the wait gate uniform.
    """

    def __init__(self, address, registry, token=None, cap=QUEUE_CAP):
        self.address = address
        self.registry = registry
        self.token = str(token or "")
        self.cap = int(cap)
        self.host = address.host
        self.port = int(address.port)
        self.pending = MessageQueue(cap=cap)
        self.ceps = {}
        self.listener = None
        self._counter = 0
        self._lock = threading.Lock()

    @property
    def started(self):
        return self.listener is not None

    @property
    def url(self):
        """The canonical URL (the REAL port once the listener is up)."""
        return "%s://%s:%d%s/%s" % (self.address.scheme, self.host, self.port,
                                     self.address.path, self.address.version)

    @property
    def prefix(self):
        return "%s/%s" % (self.address.path, self.address.version)

    def matches(self, path):
        return path == self.prefix

    def cep_matches(self, path):
        """The conversation id in a CEP request path, or None."""
        head = self.address.path + "/cep/"
        if not path.startswith(head):
            return None
        rest = path[len(head):].split("/")
        if len(rest) == 2 and rest[1] == self.address.version:
            return rest[0]
        return None

    def start(self):
        if self.listener is not None:
            return self
        listener = self.registry.listener_for(self.host, self.port)
        self.host = listener.host
        self.port = listener.port
        self.listener = listener
        return self

    def stop(self):
        for cid in list(self.ceps):
            self.close_cep(cid)
        self.pending.notify()

    def authorized(self, headers, query):
        """A service with no token accepts everyone; else the token must match."""
        if not self.token:
            return True
        return _bearer(headers, query) == self.token

    def accept(self, peer=""):
        """Open a conversation for an inbound connect; hands back its CEP."""
        if self.registry.count_ceps() >= self.registry.max_ceps:
            raise EndpointError(
                "conversation cap reached (%d)" % self.registry.max_ceps)
        with self._lock:
            self._counter += 1
            cid = str(self._counter)
        url = "%s://%s:%d%s/cep/%s/%s" % (
            self.address.scheme, self.host, self.port, self.address.path,
            cid, self.address.version)
        cep = Cep(url, peer=peer, cap=self.cap, direction="inbound")
        self.ceps[cid] = cep
        self.registry.register_cep(cep)
        if not self.pending.put(cep.url):
            self.close_cep(cid)
            raise EndpointError("too many unaccepted connections")
        self.registry.wake(self.url)
        return cep

    def close_cep(self, cid):
        cep = self.ceps.pop(cid, None)
        if cep is not None:
            cep.close()
            self.registry.forget_cep(cep.url)


class ServiceListener:
    """One ``ThreadingHTTPServer`` for a host:port, shared by its endpoints."""

    def __init__(self, registry, host, port):
        self.registry = registry
        self.host = host
        self.port = int(port)
        self.server = None
        self.thread = None

    def start(self):
        handler = type("YahaEndpointHandler", (_EndpointHandler,),
                       {"registry": self.registry})
        server = ThreadingHTTPServer((self.host, self.port), handler)
        server.daemon_threads = True
        self.server = server
        self.host, self.port = server.server_address[0], server.server_address[1]
        self.thread = threading.Thread(target=server.serve_forever, daemon=True,
                                       name="yaha-endpoint-listener")
        self.thread.start()
        return self

    def stop(self):
        server, self.server = self.server, None
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass

def http_delete(url, token=None, timeout=10.0):
    """DELETE (used to close a conversation); returns the decoded reply."""
    import urllib.request
    request = urllib.request.Request(url, method="DELETE")
    if token:
        request.add_header("Authorization", "Bearer " + str(token))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        text = response.read().decode("utf-8")
        return json.loads(text) if text else {}


class RemoteChannel:
    """The CONNECTING side's poller: fills a local CEP's inbox from the peer.

    Exactly the strategy approved at the Approve gate: backpressure first (never
    poll while the local inbox is full, so a slow stage cannot be flooded), then
    one long-poll with a FINITE timeout; re-poll immediately after a message or
    an empty timeout so a backlog drains at full speed; exponential backoff
    1s -> 30s on transport errors, and after MAX_CONSECUTIVE_ERRORS in a row the
    channel is DEGRADED and a waiting stage is woken so it can fail recoverably
    instead of hanging. A later success clears the counter.
    """

    def __init__(self, registry, cep, token=None, poll_timeout=POLL_TIMEOUT_S):
        self.registry = registry
        self.cep = cep
        self.token = str(token or "")
        self.poll_timeout = float(poll_timeout)
        self.consecutive_errors = 0
        self.degraded = False
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="yaha-remote-poll")
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def poll_url(self):
        from urllib.parse import urlencode, urlparse, urlunparse
        parts = urlparse(self.cep.url)
        query = {"timeout": self.poll_timeout}
        if self.token:
            query["token"] = self.token
        return urlunparse(parts._replace(query=urlencode(query)))

    def poll_once(self):
        """One long-poll; returns the message, or None when the poll was empty."""
        reply = http_get(self.poll_url(), token=self.token)
        if not reply:
            return None
        return reply.get("message")

    def _run(self):
        backoff = BACKOFF_BASE_S
        while not self._stop.is_set() and not self.cep.closed:
            if len(self.cep.inbox) >= self.cep.inbox.cap:
                self._stop.wait(0.2)          # backpressure: leave it on the peer
                continue
            try:
                message = self.poll_once()
            except Exception:
                self.consecutive_errors += 1
                if self.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    self.degraded = True
                    self.registry.wake(self.cep.url)
                self._stop.wait(backoff)
                backoff = min(BACKOFF_MAX_S, backoff * 2)
                continue
            self.consecutive_errors = 0
            self.degraded = False
            backoff = BACKOFF_BASE_S
            if message is not None:
                self.cep.inbox.put(message)
                self.registry.wake(self.cep.url)


class EndpointRegistry:
    """Run-global endpoint state: the service EPs (via listeners) and the CEPs.

    Lives on ``AgentRuntime`` next to ``EventRegistry`` and ``BindingRegistry``,
    so every agent on the run resolves the same endpoints. ``reclaim()`` closes
    everything when the run ends, which is what makes `close_tool` a courtesy
    rather than a requirement.
    """

    def __init__(self, wake=None, token=None, max_ceps=MAX_CEPS, cap=QUEUE_CAP):
        self._wake = wake
        self.token = str(token or "")
        self.max_ceps = int(max_ceps)
        self.cap = int(cap)
        self.services = {}
        self.ceps = {}
        self.channels = {}
        self.listeners = {}
        self._lock = threading.Lock()

    # -- run plumbing ------------------------------------------------------

    def wake(self, name):
        """Release every agent parked on an endpoint name."""
        if self._wake is None:
            return 0
        try:
            return self._wake(name)
        except Exception:
            return 0

    def count_ceps(self):
        with self._lock:
            return len(self.ceps)

    def register_cep(self, cep):
        with self._lock:
            self.ceps[cep.url] = cep
        return cep

    def forget_cep(self, url):
        with self._lock:
            return self.ceps.pop(str(url), None)

    def listener_for(self, host, port):
        """The shared listener for a requested host:port (started on demand)."""
        key = (str(host), int(port))
        with self._lock:
            listener = self.listeners.get(key)
            if listener is None:
                listener = ServiceListener(self, host, port).start()
                self.listeners[key] = listener
        return listener

    def service_ep(self, url):
        """The STARTED listener endpoint for a service EP URL (created once)."""
        address = parse_ep(url)
        key = (address.scheme, address.host, address.port, address.path, address.version)
        with self._lock:
            service = self.services.get(key)
            if service is None:
                service = ServiceEndpoint(address, self, token=self.token, cap=self.cap)
                self.services[key] = service
        return service.start()

    def find_route(self, path):
        """Map a request path to (service endpoint, conversation id or None)."""
        with self._lock:
            services = list(self.services.values())
        for service in services:
            if not service.started:
                continue
            cid = service.cep_matches(path)
            if cid is not None:
                return service, cid
        for service in services:
            if service.started and service.matches(path):
                return service, None
        return None, None

    def is_live(self, name):
        """True while a peer could still satisfy a wait on that endpoint name.

        The scheduler consults this before declaring a deadlock: an endpoint wait
        is NOT stuck just because no agent is runnable, because its producer is a
        remote peer rather than a handle in the pool. A conversation (CEP) is
        live while it is open and its poller is not degraded; a service endpoint
        is live while its listener is up.
        """
        text = str(name or "").strip()
        cep = self.ceps.get(text)
        if cep is not None:
            if cep.closed:
                return False
            channel = self.channels.get(text)
            if channel is not None and getattr(channel, "degraded", False):
                return False
            return True
        for service in list(self.services.values()):
            if service.started and service.url == text:
                return True
        return False

    # -- the three operations the tools need -------------------------------

    def connect(self, service_url, token=None, timeout=10.0):
        """Open a conversation with a remote service EP; returns the local CEP."""
        parse_ep(service_url)
        token = str(token or self.token or "")
        try:
            reply = http_post(service_url, {"connect": True}, token=token,
                              timeout=timeout)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("error", "")
            except Exception:
                detail = ""
            raise EndpointError(
                "service refused the connection (%d)%s"
                % (exc.code, (": " + detail) if detail else ""))
        cep_url = str((reply or {}).get("cep") or "")
        if not cep_url:
            raise EndpointError("service endpoint returned no conversation")
        if self.count_ceps() >= self.max_ceps:
            raise EndpointError("conversation cap reached (%d)" % self.max_ceps)
        cep = Cep(cep_url, peer=service_url, cap=self.cap,
                  direction="outbound", token=token)
        self.register_cep(cep)
        self.channels[cep.url] = RemoteChannel(self, cep, token=token).start()
        return cep

    def send(self, cep, message):
        """Send one message on a conversation, from whichever end we are."""
        if cep is None or cep.closed:
            raise EndpointError("the conversation is closed")
        text = str(message)
        if cep.direction == "inbound":
            if not cep.outbox.put(text):
                raise EndpointError("the outgoing queue is full")
            return True
        reply = http_post(cep.url, {"message": text}, token=cep.token, timeout=10.0)
        return bool((reply or {}).get("ok"))

    def close(self, cep):
        """Politely close a conversation (the peer is told; may already be gone)."""
        if cep is None:
            return False
        channel = self.channels.pop(cep.url, None)
        if channel is not None:
            channel.stop()
        if cep.direction == "outbound" and not cep.closed:
            try:
                http_delete(cep.url, token=cep.token)
            except Exception:
                pass
        for service in list(self.services.values()):
            for cid, candidate in list(service.ceps.items()):
                if candidate is cep:
                    service.ceps.pop(cid, None)
        cep.close()
        self.forget_cep(cep.url)
        return True

    # -- teardown ----------------------------------------------------------

    def reclaim(self):
        """Close every conversation, poller and listener (run end)."""
        for channel in list(self.channels.values()):
            channel.stop()
        self.channels.clear()
        for service in list(self.services.values()):
            service.stop()
        self.services.clear()
        with self._lock:
            self.ceps.clear()
        for listener in list(self.listeners.values()):
            listener.stop()
        self.listeners.clear()

    def snapshot(self):
        """A small, test-friendly description of the endpoint state."""
        with self._lock:
            ceps = sorted(self.ceps)
        return {
            "services": sorted(s.url for s in self.services.values()),
            "ceps": ceps,
            "channels": sorted(self.channels),
            "listeners": sorted("%s:%d" % k for k in self.listeners),
        }
