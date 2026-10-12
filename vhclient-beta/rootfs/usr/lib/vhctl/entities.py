"""Publish VirtualHere USB devices as Home Assistant MQTT entities.

Creates one device-registry device ("VirtualHere Client") with:
- switches for Auto-Find / Auto-Use All (controls)
- a switch per USB device (use / stop using)
- a sensor per USB device (status + all available attributes)
- client diagnostic sensor (device count)

Requires the Home Assistant MQTT integration. Discovery and switch commands use the broker from the Supervisor MQTT service.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any, Callable, Dict, Optional, Set, Tuple

from . import api, config, ha, sync

log = logging.getLogger("vhctl")

DEVICE_IDENTIFIER = "vhclient"
AVAILABILITY_TOPIC = "vhclient/status"
USB_COMMAND_TOPIC_RE = re.compile(r"^vhclient/usb/([^/]+)/set$")
CLIENT_COMMAND_TOPIC_RE = re.compile(r"^vhclient/client/([^/]+)/set$")
PUBLISHED_IDS_PATH = "/data/ha_entity_ids.json"
DISCOVERY_PREFIX = os.environ.get("HA_DISCOVERY_PREFIX", "homeassistant")
# Give up and turn off ha_entities after this many consecutive MQTT failures.
MQTT_MAX_FAILURES = int(os.environ.get("VHCTL_MQTT_MAX_FAILURES", "5"))

# Client-level controls (switches) and diagnostics (sensors).
CLIENT_SWITCHES = (
    ("autofind", "Auto-Find", "mdi:magnify-scan", "autofind"),
    ("auto_use_all", "Auto-Use All", "mdi:usb", "autouse"),
)
CLIENT_SENSORS = (
    ("device_count", "USB Device Count", "mdi:counter"),
)


def _slug(address: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", address)


def switch_unique_id(address: str) -> str:
    return f"vhclient_usb_{_slug(address)}"


def sensor_unique_id(address: str) -> str:
    return f"vhclient_usb_{_slug(address)}_info"


def client_entity_unique_id(key: str) -> str:
    return f"vhclient_client_{key}"


def switch_state_topic(address: str) -> str:
    return f"vhclient/usb/{_slug(address)}/state"


def switch_command_topic(address: str) -> str:
    return f"vhclient/usb/{_slug(address)}/set"


def switch_control_topic(address: str) -> str:
    """Per-switch availability: offline when another client holds the device."""
    return f"vhclient/usb/{_slug(address)}/controllable"


def sensor_state_topic(address: str) -> str:
    return f"vhclient/usb/{_slug(address)}/info"


def _is_controllable(in_use_by: str) -> bool:
    """True when this client may USE / STOP USING the device."""
    who = api.normalize_in_use_by(in_use_by)
    return who in ("NO ONE", "you")


def client_state_topic(key: str) -> str:
    return f"vhclient/client/{key}/state"


def client_command_topic(key: str) -> str:
    return f"vhclient/client/{key}/set"


def discovery_topic(platform: str, unique_id: str) -> str:
    return f"{DISCOVERY_PREFIX}/{platform}/{unique_id}/config"


def subscribe_topic(payload: Dict[str, Any], field: str = "command_topic") -> Optional[str]:
    """Return the topic Home Assistant will use from a discovery payload.

    A leading ``~`` is the discovery base-topic prefix and is expanded before
    subscribing. The value stored here is the exact topic to subscribe to.
    """
    raw = payload.get(field)
    if not isinstance(raw, str) or not raw.strip():
        return None
    topic = raw.strip()
    base = payload.get("~")
    if topic.startswith("~") and isinstance(base, str) and base.strip():
        topic = base.strip().rstrip("/") + topic[1:]
    return topic


def _origin(sw_version: str) -> dict[str, str]:
    origin = {
        "name": "VirtualHere Client",
        "support_url": (
            "https://git.carlscrona.se/haka/home-assistant-addons/src/branch/main/vhclient-beta"
        ),
    }
    if sw_version:
        origin["sw"] = sw_version
    return origin


def switch_discovery_payload(
    *,
    name: str,
    unique_id: str,
    command_topic: str,
    state_topic: str,
    icon: str,
    sw_version: str,
    availability: list[dict[str, str]] | None = None,
    availability_topic: str | None = None,
) -> dict[str, Any]:
    """Full MQTT switch discovery document.

    ``command_topic`` is both where Home Assistant publishes a click and where
    this addon subscribes. ``state_on`` / ``state_off`` match the retained
    state payloads this addon publishes.
    """
    payload: dict[str, Any] = {
        "name": name,
        "unique_id": unique_id,
        "object_id": unique_id,
        "command_topic": command_topic,
        "state_topic": state_topic,
        "payload_on": "ON",
        "payload_off": "OFF",
        "state_on": "ON",
        "state_off": "OFF",
        "qos": 0,
        "retain": False,
        "encoding": "utf-8",
        "optimistic": False,
        "icon": icon,
        "device": _device_block(sw_version),
        "origin": _origin(sw_version),
    }
    if availability is not None:
        payload["availability_mode"] = "all"
        payload["availability"] = availability
    elif availability_topic:
        payload["availability_topic"] = availability_topic
        payload["payload_available"] = "online"
        payload["payload_not_available"] = "offline"
    return payload


def entity_key(platform: str, key: str) -> str:
    return f"{platform}:{key}"


def _unique_id_for(platform: str, entity: str) -> Optional[str]:
    if entity.startswith("client_"):
        return client_entity_unique_id(entity[len("client_") :])
    if platform == "switch":
        return switch_unique_id(entity)
    if platform == "sensor":
        return sensor_unique_id(entity)
    return None


def _device_block(sw_version: str = "") -> dict[str, Any]:
    device: dict[str, Any] = {
        "identifiers": [DEVICE_IDENTIFIER],
        "name": "VirtualHere Client",
        "manufacturer": "VirtualHere",
        "model": "USB Client",
    }
    if sw_version:
        device["sw_version"] = sw_version
    return device


def _display_name(label: str, address: str) -> str:
    text = (label or "").strip()
    if not text:
        return address
    marker = f" ({address})"
    if marker in text:
        return text.split(marker, 1)[0].strip() or address
    return text


def _availability() -> dict[str, str]:
    return {
        "availability_topic": AVAILABILITY_TOPIC,
        "payload_available": "online",
        "payload_not_available": "offline",
    }


def _load_published() -> Set[str]:
    try:
        with open(PUBLISHED_IDS_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, list):
            return set()
        published: Set[str] = set()
        for item in data:
            text = str(item)
            if ":" not in text:
                # Legacy: bare VirtualHere address meant the use switch.
                published.add(entity_key("switch", text))
            else:
                published.add(text)
        return published
    except FileNotFoundError:
        return set()
    except Exception as exc:  # noqa: BLE001
        log.debug("Could not read %s: %s", PUBLISHED_IDS_PATH, exc)
    return set()


def _save_published(keys: Set[str]) -> None:
    try:
        with open(PUBLISHED_IDS_PATH, "w", encoding="utf-8") as handle:
            json.dump(sorted(keys), handle)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not persist HA entity ids: %s", exc)


def _set_device_use(address: str, use: bool) -> None:
    """Update usbdevices[].use for address via Supervisor options.

    Manual per-device control disables Auto-Use All so configure/sync apply USE
    for the selected devices only.
    """

    def mutate(options: Dict[str, Any]) -> bool:
        usbdevices = options.get("usbdevices") or []
        found = False
        updated: list[dict[str, Any]] = []
        changed = False
        for item in usbdevices:
            if not isinstance(item, dict):
                continue
            label = item.get("device")
            if not label:
                continue
            item_address = api.extract_address(str(label))
            if item_address == address:
                prior_use = bool(item.get("use"))
                entry = {"device": str(label), "use": use}
                updated.append(entry)
                found = True
                if prior_use != use:
                    changed = True
            else:
                updated.append({"device": str(label), "use": bool(item.get("use"))})

        if not found:
            updated.append({"device": address, "use": use})
            changed = True
            log.info("Adding usbdevices entry from HA switch for %s use=%s", address, use)
        elif changed:
            log.info("HA switch set %s use=%s", address, use)

        # A switch click always leaves Auto-Use All, even when the stored use
        # flag already matches (the switch can be on because the device is in use).
        if bool(options.get("autouse", True)):
            options["autouse"] = False
            changed = True
            log.info("Disabled Auto-Use All after HA USB switch change")

        if not changed:
            return False
        options["usbdevices"] = updated
        return True

    try:
        config.update_supervisor_options(mutate)
    except RuntimeError as exc:
        log.warning("Failed to save use flag for %s: %s", address, exc)


def _set_client_option(option_name: str, value: bool) -> None:
    """Update autofind/autouse in Supervisor options (applied by vhctl watch)."""

    def mutate(options: Dict[str, Any]) -> bool:
        if bool(options.get(option_name)) is value and option_name in options:
            return False
        options[option_name] = value
        log.info("HA switch set %s=%s", option_name, value)
        return True

    try:
        config.update_supervisor_options(mutate)
    except RuntimeError as exc:
        log.warning("Failed to save %s: %s", option_name, exc)


def _disable_ha_entities_option() -> None:
    """Persist ha_entities=false so the feature stays off until re-enabled."""

    def mutate(options: Dict[str, Any]) -> bool:
        if not options.get("ha_entities"):
            return False
        options["ha_entities"] = False
        return True

    try:
        if config.update_supervisor_options(mutate):
            log.error(
                "Disabled Home Assistant entities (ha_entities=false): MQTT unavailable "
                "after %s consecutive failures. Re-enable after MQTT is configured.",
                MQTT_MAX_FAILURES,
            )
    except RuntimeError as exc:
        log.warning("Failed to persist ha_entities=false: %s", exc)


def _parse_state_flags(state: str) -> Tuple[int, bool, bool]:
    try:
        value = int(state or "0")
    except ValueError:
        value = 0
    return value, bool(value & 1), bool(value & 2)


def _collect_device_info(
    address: str,
    *,
    listed: Optional[api.ListedDevice] = None,
    state_dev: Optional[api.ClientStateDevice] = None,
    info: Optional[api.DeviceInfo] = None,
) -> Dict[str, Any]:
    """Build a JSON-serializable info dict from all available Client API sources."""
    nickname = ""
    product = ""
    vendor = ""
    vendor_id = ""
    product_id = ""
    serial = ""
    in_use_by = "NO ONE"
    auto_use = False
    client_id = ""
    bound_host = ""
    state_raw = ""
    state_val = 0
    connected = False
    bound = False

    if info is not None:
        nickname = info.nickname or nickname
        product = info.product or product
        vendor = info.vendor or vendor
        vendor_id = info.vendor_id or vendor_id
        product_id = info.product_id or product_id
        serial = info.serial or serial
        in_use_by = api.effective_in_use_by(info.in_use_by)

    if state_dev is not None:
        nickname = nickname or state_dev.nickname
        product = product or state_dev.product
        client_id = state_dev.client_id
        bound_host = state_dev.bound_client_hostname
        state_raw = state_dev.state
        state_val, connected, bound = _parse_state_flags(state_dev.state)
        if state_dev.auto_use:
            auto_use = state_dev.auto_use.strip().lower() in ("1", "true", "yes", "on")
        if info is None:
            in_use_by = api.normalize_in_use_by(state_dev.in_use_by)

    if listed is not None:
        if listed.auto_use:
            auto_use = True
        if info is None and state_dev is None:
            in_use_by = api.normalize_in_use_by(listed.in_use_by)
        if not nickname and not product:
            product = listed.name

    # LIST / client-state "you" wins over a hostname left behind by a slug rename.
    if (listed is not None and listed.in_use_by_you) or (
        state_dev is not None and api.normalize_in_use_by(state_dev.in_use_by) == "you"
    ):
        in_use_by = "you"

    hub = address.rsplit(".", 1)[0] if "." in address else ""
    port = address.rsplit(".", 1)[1] if "." in address else ""
    display = api.prefer_name(nickname, product, vendor or address)

    return {
        "address": address,
        "hub": hub,
        "port": port,
        "name": display,
        "nickname": nickname,
        "product": product,
        "vendor": vendor,
        "vendor_id": vendor_id,
        "product_id": product_id,
        "serial": serial,
        "in_use_by": in_use_by,
        "in_use_by_you": in_use_by == "you",
        "controllable": _is_controllable(in_use_by),
        "auto_use": auto_use,
        "client_id": client_id,
        "bound_client_hostname": bound_host,
        "state": state_val,
        "state_raw": state_raw,
        "connected": connected,
        "bound": bound,
    }


class HaEntityManager:
    """Manage MQTT discovery + state for VirtualHere USB entities."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._enabled = False
        self._published: Set[str] = _load_published()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._mqtt_failures = 0
        self._gave_up = False
        self._addresses: Set[str] = {
            key.split(":", 1)[1]
            for key in self._published
            if key.startswith("switch:") and not key.startswith("switch:client_")
        }
        # command_topic strings taken from the discovery payloads we publish.
        self._command_topics_by_key: Dict[str, str] = {}
        self._discovery_logged: Dict[str, str] = {}

    def start(self) -> None:
        if self._gave_up:
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._listen_commands,
            name="vhctl-ha-entities",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def sync(self, options: Dict[str, Any], sw_version: str = "") -> None:
        """Create/update/remove HA entities to match current options."""
        want_enabled = bool(options.get("ha_entities"))
        with self._lock:
            if not want_enabled:
                if self._gave_up:
                    self._gave_up = False
                    self._mqtt_failures = 0
                if self._enabled or self._published:
                    self._clear_all_locked()
                self._enabled = False
                self._stop.set()
                return

            if self._gave_up:
                return

            self._enabled = True
            self.start()

            current: Dict[str, Dict[str, Any]] = {}
            for item in options.get("usbdevices") or []:
                if not isinstance(item, dict):
                    continue
                label = item.get("device")
                if not label:
                    continue
                address = api.extract_address(str(label))
                if not address:
                    continue
                current[address] = {
                    "name": _display_name(str(label), address),
                    "use": bool(item.get("use")),
                }

            list_status = None
            try:
                list_status = api.list_status()
            except Exception:  # noqa: BLE001
                pass

            listed_by_addr = {
                d.address: d for d in (list_status.devices if list_status else [])
            }
            state_by_addr: Dict[str, api.ClientStateDevice] = {}
            try:
                state_by_addr = {d.address: d for d in api.iter_client_state_devices()}
            except Exception:  # noqa: BLE001
                pass

            in_use_you = {addr for addr, d in listed_by_addr.items() if d.in_use_by_you}

            desired_keys = {entity_key("switch", addr) for addr in current}
            desired_keys |= {entity_key("sensor", addr) for addr in current}
            desired_keys |= {
                entity_key("switch", f"client_{key}") for key, _, _, _ in CLIENT_SWITCHES
            }
            desired_keys |= {entity_key("sensor", f"client_{key}") for key, _, _ in CLIENT_SENSORS}

            for key in list(self._published - desired_keys):
                if not self._unpublish_key_locked(key):
                    return

            if not self._publish_client_controls_locked(options, list_status, sw_version):
                return

            for address, meta in current.items():
                try:
                    device_info = api.device_info(address)
                except Exception:  # noqa: BLE001
                    device_info = None
                info = _collect_device_info(
                    address,
                    listed=listed_by_addr.get(address),
                    state_dev=state_by_addr.get(address),
                    info=device_info,
                )
                # Prefer the live nickname/product over the options label when present.
                name = info.get("name") or meta["name"]
                info["use"] = meta["use"]
                info["name"] = name

                controllable = bool(info.get("controllable", True))
                if not self._publish_switch_locked(address, name, sw_version):
                    return
                # In use by anyone (you or another) shows ON; free shows OFF unless configured.
                in_use_by_you = bool(info.get("in_use_by_you")) or address in in_use_you
                in_use_other = not controllable and not in_use_by_you
                state = "ON" if (meta["use"] or in_use_by_you or in_use_other) else "OFF"
                if not self._publish_switch_state_locked(address, state):
                    return
                if not self._publish_switch_control_locked(address, controllable):
                    return

                if not self._publish_device_sensor_locked(address, name, sw_version):
                    return
                if not self._publish_device_sensor_state_locked(address, info):
                    return

            if not self._publish_client_sensors_locked(len(current), sw_version):
                return

            if not self._mqtt_call(lambda: ha.mqtt_publish(AVAILABILITY_TOPIC, "online", retain=True)):
                return

            self._addresses = set(current)
            _save_published(self._published)

    def _mqtt_call(self, action: Callable[[], None]) -> bool:
        try:
            action()
            self._mqtt_failures = 0
            return True
        except Exception as exc:  # noqa: BLE001
            return not self._record_mqtt_failure(exc)

    def _record_mqtt_failure(self, exc: Exception) -> bool:
        self._mqtt_failures += 1
        remaining = MQTT_MAX_FAILURES - self._mqtt_failures
        if self._mqtt_failures >= MQTT_MAX_FAILURES:
            log.error(
                "Home Assistant MQTT failed %s/%s times (%s); disabling ha_entities",
                self._mqtt_failures,
                MQTT_MAX_FAILURES,
                exc,
            )
            self._give_up_locked()
            return True
        log.warning(
            "Home Assistant MQTT failed (%s); try %s/%s (%s left). "
            "Enable the MQTT integration to use HA entities.",
            exc,
            self._mqtt_failures,
            MQTT_MAX_FAILURES,
            max(0, remaining),
        )
        return False

    def _give_up_locked(self) -> None:
        if self._gave_up:
            return
        self._gave_up = True
        self._enabled = False
        self._stop.set()
        self._published.clear()
        self._addresses.clear()
        self._command_topics_by_key.clear()
        self._discovery_logged.clear()
        _save_published(self._published)
        _disable_ha_entities_option()

    def _publish_discovery(
        self,
        platform: str,
        key: str,
        unique_id: str,
        payload: dict[str, Any],
    ) -> bool:
        def publish() -> None:
            topic = discovery_topic(platform, unique_id)
            body = json.dumps(payload, separators=(",", ":"))
            ha.mqtt_publish(topic, body, retain=True)
            published_key = entity_key(platform, key)
            self._published.add(published_key)
            command = subscribe_topic(payload, "command_topic")
            if command:
                self._command_topics_by_key[published_key] = command
            else:
                self._command_topics_by_key.pop(published_key, None)
            if self._discovery_logged.get(published_key) != body:
                self._discovery_logged[published_key] = body
                if command:
                    log.info(
                        "MQTT discovery %s command_topic=%s state_topic=%s",
                        topic,
                        command,
                        subscribe_topic(payload, "state_topic"),
                    )
                log.info("MQTT discovery payload %s %s", topic, body)

        return self._mqtt_call(publish)

    def _publish_switch_locked(self, address: str, name: str, sw_version: str) -> bool:
        # Addon online AND device not held by another client.
        unique_id = switch_unique_id(address)
        payload = switch_discovery_payload(
            name=name,
            unique_id=unique_id,
            command_topic=switch_command_topic(address),
            state_topic=switch_state_topic(address),
            icon="mdi:usb-port",
            sw_version=sw_version,
            availability=[
                {
                    "topic": AVAILABILITY_TOPIC,
                    "payload_available": "online",
                    "payload_not_available": "offline",
                },
                {
                    "topic": switch_control_topic(address),
                    "payload_available": "online",
                    "payload_not_available": "offline",
                },
            ],
        )
        return self._publish_discovery("switch", address, unique_id, payload)

    def _publish_switch_state_locked(self, address: str, state: str) -> bool:
        return self._mqtt_call(
            lambda: ha.mqtt_publish(switch_state_topic(address), state, retain=True)
        )

    def _publish_switch_control_locked(self, address: str, controllable: bool) -> bool:
        payload = "online" if controllable else "offline"
        return self._mqtt_call(
            lambda: ha.mqtt_publish(switch_control_topic(address), payload, retain=True)
        )

    def _publish_device_sensor_locked(self, address: str, name: str, sw_version: str) -> bool:
        topic = sensor_state_topic(address)
        payload = {
            "name": f"{name} Info",
            "unique_id": sensor_unique_id(address),
            "state_topic": topic,
            "json_attributes_topic": topic,
            "value_template": "{{ value_json.in_use_by }}",
            "json_attributes_template": "{{ value_json | tojson }}",
            "icon": "mdi:usb",
            "device": _device_block(sw_version),
            "object_id": sensor_unique_id(address),
            "entity_category": "diagnostic",
            **_availability(),
        }
        return self._publish_discovery("sensor", address, sensor_unique_id(address), payload)

    def _publish_device_sensor_state_locked(self, address: str, info: Dict[str, Any]) -> bool:
        return self._mqtt_call(
            lambda: ha.mqtt_publish(
                sensor_state_topic(address),
                json.dumps(info, separators=(",", ":")),
                retain=True,
            )
        )

    def _publish_client_controls_locked(
        self,
        options: Dict[str, Any],
        list_status: Optional[api.ListStatus],
        sw_version: str,
    ) -> bool:
        """Publish Auto-Find / Auto-Use All as control switches (not diagnostics)."""
        autofind = bool(options.get("autofind", True))
        if list_status is not None and list_status.autofind is not None:
            autofind = bool(list_status.autofind)
        autouse = bool(options.get("autouse", True))
        if list_status is not None and list_status.auto_use_all is not None:
            autouse = bool(list_status.auto_use_all)
        states = {
            "autofind": autofind,
            "auto_use_all": autouse,
        }
        for key, name, icon, _option in CLIENT_SWITCHES:
            unique_id = client_entity_unique_id(key)
            payload = switch_discovery_payload(
                name=name,
                unique_id=unique_id,
                command_topic=client_command_topic(key),
                state_topic=client_state_topic(key),
                icon=icon,
                sw_version=sw_version,
                availability_topic=AVAILABILITY_TOPIC,
            )
            if not self._publish_discovery("switch", f"client_{key}", unique_id, payload):
                return False
            state = "ON" if states.get(key) else "OFF"
            if not self._mqtt_call(
                lambda t=client_state_topic(key), v=state: ha.mqtt_publish(t, v, retain=True)
            ):
                return False
        return True

    def _publish_client_sensors_locked(self, device_count: int, sw_version: str) -> bool:
        for key, name, icon in CLIENT_SENSORS:
            unique_id = client_entity_unique_id(key)
            topic = client_state_topic(key)
            payload = {
                "name": name,
                "unique_id": unique_id,
                "state_topic": topic,
                "icon": icon,
                "device": _device_block(sw_version),
                "object_id": unique_id,
                "entity_category": "diagnostic",
                **_availability(),
            }
            if key == "device_count":
                payload["state_class"] = "measurement"
            if not self._publish_discovery("sensor", f"client_{key}", unique_id, payload):
                return False
            if not self._mqtt_call(
                lambda t=topic, v=str(device_count): ha.mqtt_publish(t, v, retain=True)
            ):
                return False
        return True

    def _unpublish_key_locked(self, key: str) -> bool:
        if ":" not in key:
            return True
        platform, entity = key.split(":", 1)
        unique_id = _unique_id_for(platform, entity)
        if unique_id is None:
            self._published.discard(key)
            return True

        ok = self._mqtt_call(
            lambda: ha.mqtt_publish(discovery_topic(platform, unique_id), "", retain=True)
        )
        self._published.discard(key)
        self._command_topics_by_key.pop(key, None)
        self._discovery_logged.pop(key, None)
        if platform == "switch" and not entity.startswith("client_"):
            self._addresses.discard(entity)
        return ok

    def _clear_all_locked(self) -> None:
        log.info("Removing Home Assistant VirtualHere USB entities")
        for key in list(self._published):
            if ":" not in key:
                self._published.discard(key)
                continue
            platform, entity = key.split(":", 1)
            unique_id = _unique_id_for(platform, entity)
            if unique_id is None:
                self._published.discard(key)
                continue
            try:
                ha.mqtt_publish(discovery_topic(platform, unique_id), "", retain=True)
            except Exception as exc:  # noqa: BLE001
                log.debug("Unpublish %s failed during clear: %s", key, exc)
            self._published.discard(key)
        try:
            ha.mqtt_publish(AVAILABILITY_TOPIC, "offline", retain=True)
        except Exception as exc:  # noqa: BLE001
            log.debug("Availability offline publish failed during clear: %s", exc)
        self._addresses.clear()
        self._command_topics_by_key.clear()
        self._discovery_logged.clear()
        _save_published(self._published)

    def _address_for_slug(self, slug: str) -> Optional[str]:
        wanted = slug.lower()
        with self._lock:
            candidates = list(self._addresses)
        for candidate in candidates:
            if _slug(candidate).lower() == wanted:
                return candidate
        try:
            options = config.read_options()
        except Exception:  # noqa: BLE001
            options = {}
        for item in options.get("usbdevices") or []:
            if not isinstance(item, dict):
                continue
            address = api.extract_address(str(item.get("device") or ""))
            if address and _slug(address).lower() == wanted:
                return address
        return None

    def _handle_usb_command(self, slug: str, payload: str) -> None:
        address = self._address_for_slug(slug)
        if address is None:
            log.warning("Ignoring HA switch command for unknown device slug %s", slug)
            return
        want = payload.strip().upper() in ("ON", "TRUE", "1")
        log.info("HA switch %s -> %s", address, "ON" if want else "OFF")

        in_use_by = "NO ONE"
        try:
            info = api.device_info(address)
            if info is not None:
                in_use_by = api.effective_in_use_by(info.in_use_by)
        except Exception:  # noqa: BLE001
            pass
        if not _is_controllable(in_use_by):
            log.info(
                "Ignoring HA switch for %s; in use by another client (%s)",
                address,
                in_use_by,
            )
            with self._lock:
                self._publish_switch_control_locked(address, False)
                self._publish_switch_state_locked(address, "ON")
            return

        _set_device_use(address, want)
        prevent = False
        try:
            prevent = bool(config.read_options().get("preventconfig"))
        except Exception:  # noqa: BLE001
            prevent = False
        accepted = True
        if prevent:
            log.info("preventconfig enabled; not applying HA switch for %s", address)
        else:
            accepted, _changed = sync.apply_device_use(address, want)
        with self._lock:
            if accepted:
                self._publish_switch_control_locked(address, True)
                self._publish_switch_state_locked(address, "ON" if want else "OFF")
            else:
                self._publish_switch_control_locked(address, _is_controllable(in_use_by))
                self._publish_switch_state_locked(
                    address, "ON" if in_use_by != "NO ONE" else "OFF"
                )

    def _handle_client_command(self, key: str, payload: str) -> None:
        option_name = None
        for switch_key, _name, _icon, option in CLIENT_SWITCHES:
            if switch_key == key:
                option_name = option
                break
        if option_name is None:
            log.warning("Ignoring HA client switch for unknown key %s", key)
            return
        want = payload.strip().upper() in ("ON", "TRUE", "1")
        _set_client_option(option_name, want)
        try:
            if not config.read_options().get("preventconfig"):
                sync.configure()
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to apply %s from HA switch: %s", option_name, exc)
        with self._lock:
            self._mqtt_call(
                lambda: ha.mqtt_publish(
                    client_state_topic(key), "ON" if want else "OFF", retain=True
                )
            )

    def command_topics(self) -> Set[str]:
        """Exact ``command_topic`` values from the discovery payloads last published.

        Wildcards are omitted. Home Assistant assigns those a separate MQTT
        subscription id, and a click that also matches an exact topic can be
        dropped instead of delivered.
        """
        with self._lock:
            return {topic for topic in self._command_topics_by_key.values() if topic}

    def _listen_commands(self) -> None:
        recent: Dict[tuple[str, str], float] = {}

        def on_message(topic: str, payload: str) -> None:
            if not self._enabled or self._gave_up:
                return
            now = time.monotonic()
            key = (topic, payload)
            previous = recent.get(key)
            if previous is not None and now - previous < 1.0:
                return
            recent[key] = now
            for stale in [item for item, seen in recent.items() if now - seen > 5.0]:
                recent.pop(stale, None)
            log.info("MQTT command %s = %s", topic, payload)
            usb_match = USB_COMMAND_TOPIC_RE.match(topic)
            if usb_match:
                self._handle_usb_command(usb_match.group(1), payload)
                return
            client_match = CLIENT_COMMAND_TOPIC_RE.match(topic)
            if client_match:
                self._handle_client_command(client_match.group(1), payload)
            else:
                log.info("Ignoring MQTT message on %s", topic)

        def on_failure(exc: Exception, failures: int) -> None:
            log.warning(
                "MQTT subscribe failed (%s); try %s/%s",
                exc,
                failures,
                MQTT_MAX_FAILURES,
            )

        def on_give_up(exc: Exception) -> None:
            with self._lock:
                if not self._gave_up:
                    log.error(
                        "MQTT broker connection failed %s times (%s); disabling ha_entities",
                        MQTT_MAX_FAILURES,
                        exc,
                    )
                    self._mqtt_failures = MQTT_MAX_FAILURES
                    self._give_up_locked()

        def on_ready() -> None:
            if self._stop.is_set() or self._gave_up or not self._enabled:
                return
            try:
                options = config.read_options()
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not refresh MQTT discovery: %s", exc)
                return
            log.info("Publishing MQTT discovery after broker connect")
            self.sync(options)

        ha.run_mqtt_subscriber(
            self.command_topics,
            on_message,
            should_stop=lambda: self._stop.is_set() or self._gave_up,
            max_failures=MQTT_MAX_FAILURES,
            on_failure=on_failure,
            on_give_up=on_give_up,
            availability_topic=AVAILABILITY_TOPIC,
            birth_topic=f"{DISCOVERY_PREFIX}/status",
            on_ready=on_ready,
        )


# Process-wide manager used by the watch loop.
manager = HaEntityManager()
