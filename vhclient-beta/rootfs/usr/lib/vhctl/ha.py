"""Home Assistant Core API helpers (REST + WebSocket via Supervisor proxy)."""

from __future__ import annotations

import base64
import json
import logging
import os
import socket
import struct
import urllib.error
import urllib.request
from typing import Any, Callable, Optional, Set
from urllib.parse import urlparse

from . import config

log = logging.getLogger("vhctl")

HA_API_BASE = os.environ.get("HA_API_URL", "http://supervisor/core/api").rstrip("/")
HA_WS_URL = os.environ.get("HA_WS_URL", "ws://supervisor/core/websocket")


def core_request(
    method: str,
    path: str,
    payload: Optional[dict[str, Any]] = None,
    timeout: float = 30.0,
) -> Any:
    """Call Home Assistant Core REST API through the Supervisor proxy."""
    token = config.supervisor_token()
    if not token:
        raise RuntimeError("SUPERVISOR_TOKEN is not set")

    url = f"{HA_API_BASE}{path}"
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HA {method} {path} failed: {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"HA {method} {path} failed: {exc}") from exc

    if not body:
        return None
    return json.loads(body)


def call_service(
    domain: str,
    service: str,
    service_data: Optional[dict[str, Any]] = None,
    timeout: float = 30.0,
) -> Any:
    return core_request(
        "POST",
        f"/services/{domain}/{service}",
        payload=service_data or {},
        timeout=timeout,
    )


def mqtt_publish(
    topic: str,
    payload: str,
    *,
    retain: bool = False,
    qos: int = 0,
) -> None:
    """Publish on the MQTT broker from the Supervisor services API.

    Discovery, state, and availability go to the same broker Home Assistant
    uses, which is where switch command topics are delivered.
    """
    from . import mqtt as mqtt_client

    mqtt_client.broker.publish(topic, payload, retain=retain, qos=qos)


class HaWebsocket:
    """Minimal authenticated Home Assistant WebSocket client."""

    def __init__(self, timeout: float = 30.0) -> None:
        self._timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._ws: Optional[_WsConn] = None
        self._next_id = 1

    def connect(self) -> None:
        token = config.supervisor_token()
        if not token:
            raise RuntimeError("SUPERVISOR_TOKEN is not set")

        parsed = urlparse(HA_WS_URL)
        host = parsed.hostname or "supervisor"
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path or "/core/websocket"
        host_header = host if parsed.port is None else f"{host}:{port}"

        sock = socket.create_connection((host, port), timeout=self._timeout)
        self._sock = sock
        self._ws = _WsConn(sock)
        self._ws.handshake(host_header, path)

        msg = self._ws.recv_json(timeout=self._timeout)
        if msg.get("type") != "auth_required":
            raise RuntimeError(f"Expected auth_required, got {msg.get('type')}")

        self._ws.send_json({"type": "auth", "access_token": token})
        msg = self._ws.recv_json(timeout=self._timeout)
        if msg.get("type") != "auth_ok":
            raise RuntimeError(f"Home Assistant auth failed: {msg}")

    def close(self) -> None:
        if self._ws is not None:
            self._ws.close()
        self._ws = None
        self._sock = None

    def __enter__(self) -> "HaWebsocket":
        self.connect()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _alloc_id(self) -> int:
        msg_id = self._next_id
        self._next_id += 1
        return msg_id

    def send(self, message: dict[str, Any]) -> int:
        if self._ws is None:
            raise RuntimeError("WebSocket not connected")
        msg_id = message.get("id")
        if msg_id is None:
            msg_id = self._alloc_id()
            message = {**message, "id": msg_id}
        self._ws.send_json(message)
        return int(msg_id)

    def recv(self, timeout: Optional[float] = None) -> dict[str, Any]:
        if self._ws is None:
            raise RuntimeError("WebSocket not connected")
        return self._ws.recv_json(timeout=self._timeout if timeout is None else timeout)

    def call(self, message: dict[str, Any], timeout: Optional[float] = None) -> dict[str, Any]:
        msg_id = self.send(message)
        while True:
            msg = self.recv(timeout=timeout)
            if msg.get("type") == "result" and msg.get("id") == msg_id:
                return msg
            # Ignore unrelated events while waiting for this result.


class _WsConn:
    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buf = bytearray()

    def handshake(self, host: str, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self._sock.sendall(req.encode("ascii"))
        while b"\r\n\r\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket handshake closed early")
            self._buf.extend(chunk)
        header_end = self._buf.find(b"\r\n\r\n")
        status_line = bytes(self._buf[:header_end]).split(b"\r\n", 1)[0].decode(
            "ascii", errors="replace"
        )
        del self._buf[: header_end + 4]
        if "101" not in status_line:
            raise RuntimeError(f"WebSocket upgrade failed: {status_line}")

    def _read_exact(self, size: int) -> bytes:
        while len(self._buf) < size:
            chunk = self._sock.recv(max(4096, size - len(self._buf)))
            if not chunk:
                raise RuntimeError("Socket closed while reading")
            self._buf.extend(chunk)
        data = bytes(self._buf[:size])
        del self._buf[:size]
        return data

    def recv_json(self, timeout: float = 10.0) -> dict[str, Any]:
        self._sock.settimeout(timeout)
        while True:
            header = self._read_exact(2)
            fin_opcode = header[0]
            opcode = fin_opcode & 0x0F
            length = header[1] & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            payload = self._read_exact(length) if length else b""
            if opcode == 0x8:
                raise RuntimeError("WebSocket closed by peer")
            if opcode == 0x9:  # ping
                self._send_control(0xA, payload)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode != 0x1:
                raise RuntimeError(f"Unexpected WebSocket opcode {opcode}")
            return json.loads(payload.decode("utf-8"))

    def _send_control(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode, 0x80 | len(payload)])
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def send_json(self, message: dict[str, Any]) -> None:
        payload = json.dumps(message).encode("utf-8")
        header = bytearray([0x81])  # text, FIN
        mask_bit = 0x80
        length = len(payload)
        if length < 126:
            header.append(mask_bit | length)
        elif length < 65536:
            header.append(mask_bit | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(mask_bit | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


def run_mqtt_subscriber(
    topics: Callable[[], Set[str]],
    on_message: Callable[[str, str], None],
    should_stop: Callable[[], bool],
    reconnect_seconds: float = 5.0,
    max_failures: int = 0,
    on_failure: Optional[Callable[[Exception, int], None]] = None,
    on_give_up: Optional[Callable[[Exception], None]] = None,
    *,
    availability_topic: str = "vhclient/status",
    birth_topic: str = "homeassistant/status",
    on_ready: Optional[Callable[[], None]] = None,
) -> None:
    """Block, subscribed to the broker from the Supervisor MQTT service."""
    from . import mqtt as mqtt_client

    mqtt_client.broker.run(
        topics,
        on_message,
        should_stop,
        availability_topic=availability_topic,
        birth_topic=birth_topic,
        on_ready=on_ready,
        reconnect_seconds=reconnect_seconds,
        max_failures=max_failures,
        on_failure=on_failure,
        on_give_up=on_give_up,
    )
