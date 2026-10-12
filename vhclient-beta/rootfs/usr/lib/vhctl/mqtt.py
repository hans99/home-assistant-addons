"""MQTT broker client for Home Assistant discovery and switch commands.

Home Assistant apps do not receive switch clicks through the Core websocket.
The app communication guide says to read the broker from the Supervisor
services API and connect to it:

    GET /services/mqtt
    host, port, username, password, ssl, protocol

The MQTT discovery guide then requires this client to:

- publish retained discovery and state
- subscribe to each switch ``command_topic``
- set a retained availability last-will of ``offline``
- subscribe to the birth topic (``homeassistant/status`` by default) and
  publish discovery again when the payload is ``online``
"""

from __future__ import annotations

import logging
import queue
import socket
import struct
import threading
import time
from typing import Any, Callable, Optional, Set

from . import config

log = logging.getLogger("vhctl")

# Supervisor DNS on Home Assistant OS. host_network apps do not get Docker's
# embedded DNS, so ``core-mosquitto`` is resolved here when the system
# resolver cannot.
_SUPERVISOR_DNS = "172.30.32.3"
_CONNECT_TIMEOUT = 20.0
_PUBLISH_TIMEOUT = 10.0


def mqtt_service() -> dict[str, Any]:
    """Return the Supervisor MQTT service record (host, port, username, password)."""
    data = config.supervisor_request("GET", "/services/mqtt")
    if not isinstance(data, dict) or not str(data.get("host") or "").strip():
        raise RuntimeError(
            "MQTT service is not available. Install the Mosquitto app and the "
            "Home Assistant MQTT integration."
        )
    return data


def resolve_broker_host(host: str, *, ssl: bool) -> str:
    """Return a connectable broker address.

    A TLS connection keeps the hostname so certificate checks still work.
    Plain connections fall back to an A lookup on the Supervisor DNS.
    """
    name = host.strip()
    if not name:
        raise RuntimeError("MQTT service host is empty")
    try:
        socket.getaddrinfo(name, None)
        return name
    except socket.gaierror:
        if ssl:
            raise RuntimeError(
                f"MQTT host {name} did not resolve; TLS connections keep the hostname"
            ) from None
    for candidate in (name, f"{name}.local.hass.io"):
        ip = dns_lookup_a(candidate, _SUPERVISOR_DNS)
        if ip:
            log.info("Resolved MQTT host %s to %s via Supervisor DNS", candidate, ip)
            return ip
    raise RuntimeError(f"MQTT host {name} did not resolve")


def dns_lookup_a(name: str, server: str, timeout: float = 2.0) -> Optional[str]:
    """Resolve one A record through a specific DNS server."""
    query = _dns_query(name)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout)
        sock.sendto(query, (server, 53))
        data, _addr = sock.recvfrom(512)
    except OSError:
        return None
    finally:
        sock.close()
    return _dns_first_a(data, query[:2])


def _dns_query(name: str) -> bytes:
    header = struct.pack("!HHHHHH", 0x4242, 0x0100, 1, 0, 0, 0)
    labels = b"".join(bytes([len(part)]) + part.encode("ascii") for part in name.split(".") if part)
    return header + labels + b"\x00" + struct.pack("!HH", 1, 1)


def _dns_first_a(data: bytes, query_id: bytes) -> Optional[str]:
    if len(data) < 12 or data[:2] != query_id:
        return None
    ancount = struct.unpack("!H", data[6:8])[0]
    offset = 12
    offset = _skip_name(data, offset)
    if offset < 0 or offset + 4 > len(data):
        return None
    offset += 4  # question type and class
    for _ in range(ancount):
        offset = _skip_name(data, offset)
        if offset < 0 or offset + 10 > len(data):
            return None
        rtype, rclass, _ttl, rdlen = struct.unpack("!HHIH", data[offset : offset + 10])
        offset += 10
        if offset + rdlen > len(data):
            return None
        if rtype == 1 and rclass == 1 and rdlen == 4:
            return socket.inet_ntoa(data[offset : offset + 4])
        offset += rdlen
    return None


def _skip_name(data: bytes, offset: int) -> int:
    if offset < 0 or offset >= len(data):
        return -1
    while offset < len(data):
        length = data[offset]
        if length == 0:
            return offset + 1
        if length & 0xC0 == 0xC0:
            return offset + 2
        offset += 1 + length
    return -1


class MqttBroker:
    """One paho session shared by discovery publishes and switch subscriptions."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._connected = threading.Event()
        self._queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self._client: Any = None
        self._subscribed: Set[str] = set()

    def publish(self, topic: str, payload: str, *, retain: bool = False, qos: int = 0) -> None:
        client = self._require_client()
        info = client.publish(topic, payload, qos=qos, retain=retain)
        rc = getattr(info, "rc", 0)
        if rc not in (0, None):
            raise RuntimeError(f"MQTT publish to {topic} failed (rc={rc})")
        wait = getattr(info, "wait_for_publish", None)
        if callable(wait):
            wait(timeout=_PUBLISH_TIMEOUT)
        published = getattr(info, "is_published", None)
        if callable(published) and not published():
            raise RuntimeError(f"MQTT publish to {topic} was not acknowledged")

    def wait_until_connected(self, timeout: float = _CONNECT_TIMEOUT) -> None:
        if not self._connected.wait(timeout):
            raise RuntimeError(
                "MQTT broker is not connected. Check the Mosquitto app and the "
                "Home Assistant MQTT integration."
            )

    def run(
        self,
        topics: Callable[[], Set[str]],
        on_message: Callable[[str, str], None],
        should_stop: Callable[[], bool],
        *,
        availability_topic: str,
        birth_topic: str,
        on_ready: Optional[Callable[[], None]] = None,
        reconnect_seconds: float = 5.0,
        max_failures: int = 0,
        on_failure: Optional[Callable[[Exception, int], None]] = None,
        on_give_up: Optional[Callable[[Exception], None]] = None,
    ) -> None:
        """Connect, subscribe, and dispatch until should_stop()."""
        import paho.mqtt.client as mqtt

        failures = 0
        while not should_stop():
            client: Any = None
            try:
                service = mqtt_service()
                client = self._open(mqtt, service, availability_topic)
                self.wait_until_connected()
                failures = 0
                log.info(
                    "Listening for switch commands on the MQTT broker (%s)",
                    ", ".join(sorted(topics())) or "no command topics yet",
                )
                disconnected_since: Optional[float] = None
                while not should_stop():
                    if self._connected.is_set():
                        disconnected_since = None
                        self._sync_subscriptions(topics(), birth_topic)
                    else:
                        now = time.monotonic()
                        if disconnected_since is None:
                            disconnected_since = now
                        elif now - disconnected_since > 30:
                            raise RuntimeError("MQTT broker stayed disconnected")
                    try:
                        topic, payload = self._queue.get(timeout=1.0)
                    except queue.Empty:
                        continue
                    self._dispatch(topic, payload, birth_topic, on_message, on_ready)
            except Exception as exc:  # noqa: BLE001
                if should_stop():
                    break
                failures += 1
                if on_failure is not None:
                    on_failure(exc, failures)
                else:
                    log.warning(
                        "MQTT broker connection failed (%s); retry %s%s",
                        exc,
                        failures,
                        f"/{max_failures}" if max_failures > 0 else "",
                    )
                if max_failures > 0 and failures >= max_failures:
                    if on_give_up is not None:
                        on_give_up(exc)
                    else:
                        log.error("MQTT broker connection failed %s times; giving up", failures)
                    break
                time.sleep(reconnect_seconds)
            finally:
                self._close(client)

    def _require_client(self) -> Any:
        self.wait_until_connected()
        with self._lock:
            client = self._client
        if client is None:
            raise RuntimeError("MQTT broker is not connected")
        return client

    def _open(self, mqtt: Any, service: dict[str, Any], availability_topic: str) -> Any:
        host = str(service.get("host") or "").strip()
        port = int(service.get("port") or 1883)
        username = service.get("username")
        password = service.get("password")
        use_ssl = bool(service.get("ssl"))
        protocol_name = str(service.get("protocol") or "3.1.1")
        protocol = mqtt.MQTTv31 if protocol_name == "3.1" else mqtt.MQTTv311
        address = resolve_broker_host(host, ssl=use_ssl)

        client = mqtt.Client(
            client_id="vhclient-beta",
            clean_session=True,
            protocol=protocol,
        )
        if isinstance(username, str) and username:
            client.username_pw_set(username, "" if password is None else str(password))
        if use_ssl:
            client.tls_set()
        client.will_set(availability_topic, payload="offline", qos=0, retain=True)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=1, max_delay=30)

        with self._lock:
            self._client = client
            self._subscribed.clear()
        self._connected.clear()
        log.info(
            "Connecting to MQTT broker %s:%s as %s (protocol %s)",
            address,
            port,
            username or "(anonymous)",
            protocol_name,
        )
        previous_timeout = socket.getdefaulttimeout()
        try:
            socket.setdefaulttimeout(_CONNECT_TIMEOUT)
            client.connect(address, port, keepalive=60)
            client.loop_start()
        except Exception:
            self._close(client)
            raise
        finally:
            socket.setdefaulttimeout(previous_timeout)
        return client

    def _close(self, client: Any) -> None:
        self._connected.clear()
        with self._lock:
            if self._client is client:
                self._client = None
            self._subscribed.clear()
        if client is None:
            return
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        try:
            client.loop_stop()
        except Exception:  # noqa: BLE001
            pass

    def _on_connect(self, client: Any, _userdata: Any, _flags: Any, rc: int) -> None:
        if rc != 0:
            log.warning("MQTT broker rejected the connection (rc=%s)", rc)
            return
        with self._lock:
            self._subscribed.clear()
        self._connected.set()
        log.info("Connected to MQTT broker")
        self._queue.put(("__mqtt_connected__", ""))

    def _on_disconnect(self, _client: Any, _userdata: Any, rc: int) -> None:
        self._connected.clear()
        with self._lock:
            self._subscribed.clear()
        if rc != 0:
            log.warning("MQTT broker disconnected (rc=%s)", rc)

    def _on_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        raw = message.payload
        if isinstance(raw, bytes):
            payload = raw.decode("utf-8", errors="replace")
        else:
            payload = str(raw)
        self._queue.put((str(message.topic), payload))

    def _sync_subscriptions(self, command_topics: Set[str], birth_topic: str) -> None:
        wanted = {topic for topic in command_topics if topic}
        if birth_topic:
            wanted.add(birth_topic)
        with self._lock:
            client = self._client
            subscribed = set(self._subscribed)
        if client is None or not self._connected.is_set():
            return
        for topic in sorted(wanted - subscribed):
            result, _mid = client.subscribe(topic, qos=0)
            if result != 0:
                raise RuntimeError(f"MQTT subscribe failed for {topic} (rc={result})")
            with self._lock:
                self._subscribed.add(topic)
            log.info("Subscribed to MQTT topic %s", topic)
        for topic in sorted(subscribed - wanted):
            client.unsubscribe(topic)
            with self._lock:
                self._subscribed.discard(topic)

    def _dispatch(
        self,
        topic: str,
        payload: str,
        birth_topic: str,
        on_message: Callable[[str, str], None],
        on_ready: Optional[Callable[[], None]],
    ) -> None:
        try:
            if topic == "__mqtt_connected__":
                if on_ready is not None:
                    on_ready()
                return
            if topic == birth_topic and payload.strip().lower() == "online":
                log.info("MQTT birth %s = online; publishing discovery again", birth_topic)
                if on_ready is not None:
                    on_ready()
                return
            on_message(topic, payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("MQTT message handler failed for %s: %s", topic, exc)


broker = MqttBroker()
