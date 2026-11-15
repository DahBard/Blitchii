"""Debug server client for Lichii Bench.

The wire protocol is the one tools/nx2/debug_mcp.py's Emulator speaks: one JSON line
{"id", "method", "params"} per request, one JSON line back with "result" or "error". The
difference is the retry rule. Emulator.call sends a request again after any socket error, which
for `input` would press a button twice. Here only read-only methods are sent again; after a
broken connection on any other method the caller gets ConnectionLost and must look at the screen
before doing anything else.
"""

import json
import math
import socket

READ_ONLY = frozenset({"status", "frame_times", "screenshot"})
DEFAULT_ADDRESS = "127.0.0.1:6545"


class ClientError(Exception):
    pass


class NotListening(ClientError):
    """Nothing accepted the connection: no request was sent."""


class ServerError(ClientError):
    """The server answered with an error."""


class ConnectionLost(ClientError):
    """The connection broke after a request that changes state was sent: it may have run."""


def default_timeout(method, params):
    """At least as long as the server's own limit for the request (as debug_mcp does)."""
    if method == "frame_times":
        return math.ceil(params.get("seconds", 5)) + 5
    if method == "screenshot":
        return int(params.get("timeout_s", 5)) + 5
    if method == "status":
        return params.get("fps_window_ms", 500) / 1000.0 + 5
    if method in ("input", "input_sequence"):
        steps = params.get("steps", []) if method == "input_sequence" else [params]
        return int(params.get("timeout_s", sum(int(s.get("frames", 0)) for s in steps) // 20 + 10)) + 5
    return 10


class Client:
    def __init__(self, address=DEFAULT_ADDRESS, connect=socket.create_connection):
        host, _, port = address.rpartition(":")
        self.host = host.strip("[]") or "127.0.0.1"
        self.port = int(port)
        self.connect = connect
        self.sock = None
        self.buffer = b""
        self.next_id = 1

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.buffer = b""

    def _once(self, method, params, timeout):
        sent = False
        try:
            if self.sock is None:
                try:
                    self.sock = self.connect((self.host, self.port), 5)
                except OSError as error:
                    raise NotListening(f"nothing listens on {self.host}:{self.port}: {error}") from error
            self.sock.settimeout(timeout)
            request_id = self.next_id
            self.next_id += 1
            self.sock.sendall(json.dumps({"id": request_id, "method": method, "params": params}).encode() + b"\n")
            sent = True
            while b"\n" not in self.buffer:
                chunk = self.sock.recv(1 << 20)
                if not chunk:
                    raise ConnectionResetError("the emulator closed the connection")
                self.buffer += chunk
            raw, _, self.buffer = self.buffer.partition(b"\n")
            return json.loads(raw)
        except NotListening:
            self.close()
            raise
        except (OSError, ValueError) as error:
            self.close()
            if sent and method not in READ_ONLY:
                raise ConnectionLost(f"{method}: {error}") from error
            raise ClientError(f"{method}: {error}") from error

    def call(self, method, params=None, timeout=None):
        params = params or {}
        timeout = timeout or default_timeout(method, params)
        attempts = 2 if method in READ_ONLY else 1
        for attempt in range(attempts):
            try:
                response = self._once(method, params, timeout)
                break
            except (NotListening, ConnectionLost):
                raise
            except ClientError:
                if attempt + 1 == attempts:
                    raise
        if "error" in response:
            raise ServerError(f"{method}: {response['error']}")
        return response.get("result")
