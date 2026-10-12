"""Watch addon options and re-apply VirtualHere Client API settings without restart."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Dict, Optional

from . import api, config, discovery, entities, sync
from .pipe import PipeError

log = logging.getLogger("vhctl")

DEFAULT_DISCOVER_SECONDS = 30.0
MIN_DISCOVER_SECONDS = 5.0
MAX_DISCOVER_SECONDS = 3600.0


def _in_use_map() -> Optional[Dict[str, str]]:
    """address -> who is using it. None when LIST cannot be read."""
    try:
        status = api.list_status()
    except PipeError:
        return None
    return {d.address: api.normalize_in_use_by(d.in_use_by) for d in status.devices}


def _intent_from_options(options: Dict[str, Any]) -> Dict[str, Any]:
    """User-controlled settings only (ignore device label text / in-use suffixes)."""
    devices = []
    for item in options.get("usbdevices") or []:
        if not isinstance(item, dict):
            continue
        label = item.get("device")
        if not label:
            continue
        address = api.extract_address(str(label))
        if not address:
            continue
        devices.append({"address": address, "use": bool(item.get("use"))})
    devices.sort(key=lambda d: d["address"])
    return {
        "autouse": bool(options.get("autouse", True)),
        "autofind": bool(options.get("autofind", True)),
        "preventconfig": bool(options.get("preventconfig", False)),
        "ha_entities": bool(options.get("ha_entities", False)),
        "manualhub": (options.get("manualhub") or "")
        if isinstance(options.get("manualhub"), str)
        else "",
        "usbdevices": devices,
    }


def _intent_hash(options: Dict[str, Any]) -> str:
    payload = json.dumps(_intent_from_options(options), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _use_map(intent: Dict[str, Any]) -> Dict[str, bool]:
    return {d["address"]: bool(d["use"]) for d in intent.get("usbdevices") or []}


def _manual_use_flags_changed(previous: Dict[str, Any], current: Dict[str, Any]) -> bool:
    """True when a per-device use flag changed (not merely a newly discovered device)."""
    old_use = _use_map(previous)
    new_use = _use_map(current)
    for address, use in new_use.items():
        if address in old_use and old_use[address] != use:
            return True
        # Brand-new list entry with use=true is an explicit manual attach.
        if address not in old_use and use:
            return True
    return False


def _load_options() -> Optional[Dict[str, Any]]:
    try:
        return config.read_options()
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not read options: %s", exc)
        return None


def _discover_interval(options: Optional[Dict[str, Any]], fallback: float) -> float:
    """Read discover_interval from options; clamp to schema range."""
    value = fallback
    if options is not None and options.get("discover_interval") is not None:
        try:
            value = float(options.get("discover_interval"))
        except (TypeError, ValueError):
            log.warning("Invalid discover_interval; using %.0fs", fallback)
            value = fallback
    return max(MIN_DISCOVER_SECONDS, min(MAX_DISCOVER_SECONDS, value))


def _sync_ha_entities(options: Optional[Dict[str, Any]]) -> None:
    if options is None:
        return
    try:
        entities.manager.sync(options)
    except Exception as exc:  # noqa: BLE001
        log.warning("HA entity sync failed: %s", exc)


def watch(
    poll_seconds: float = 2.0,
    discover_seconds: float = DEFAULT_DISCOVER_SECONDS,
    health_host: str = "0.0.0.0",
    health_port: int = 8199,
) -> int:
    """Apply options on change and periodically refresh the USB device list."""
    from . import health

    health.start_health_server(host=health_host, port=health_port)

    last_intent_hash = ""
    last_intent: Dict[str, Any] = {}
    last_discover = 0.0
    last_logged_interval: Optional[float] = None
    last_in_use: Dict[str, str] = _in_use_map() or {}

    # Initial apply once IPC is expected to be up (caller should depend on wait).
    options = _load_options()
    interval = _discover_interval(options, discover_seconds)
    log.info(
        "Watching options (poll=%.1fs, discover every %.0fs); interval is configurable",
        poll_seconds,
        interval,
    )
    last_logged_interval = interval

    if options is not None:
        last_intent = _intent_from_options(options)
        last_intent_hash = _intent_hash(options)
        sync.apply_all()
        last_discover = time.monotonic()
        # Discover may rewrite labels; refresh intent so we don't loop.
        options = _load_options()
        if options is not None:
            last_intent = _intent_from_options(options)
            last_intent_hash = _intent_hash(options)
            interval = _discover_interval(options, discover_seconds)
        _sync_ha_entities(options)
        # Baseline after initial sync so the first poll does not look like a change.
        last_in_use = _in_use_map() or last_in_use

    while True:
        time.sleep(poll_seconds)
        options = _load_options()
        if options is None:
            continue

        interval = _discover_interval(options, discover_seconds)
        if last_logged_interval != interval:
            log.info("USB device refresh interval set to %.0fs", interval)
            last_logged_interval = interval

        current_intent = _intent_from_options(options)
        intent_hash = _intent_hash(options)
        now = time.monotonic()

        if intent_hash != last_intent_hash:
            if _manual_use_flags_changed(last_intent, current_intent):
                # Config UI or entity switch changed a per-device use flag.
                config.disable_autouse_all()
                options = _load_options()
                if options is not None:
                    current_intent = _intent_from_options(options)
                    intent_hash = _intent_hash(options)
            log.info("Configuration changed; applying without restart")
            sync.apply_all()
            options = _load_options()
            if options is not None:
                last_intent = _intent_from_options(options)
                last_intent_hash = _intent_hash(options)
                interval = _discover_interval(options, discover_seconds)
            else:
                last_intent = current_intent
                last_intent_hash = intent_hash
            _sync_ha_entities(options)
            last_in_use = _in_use_map() or last_in_use
            last_discover = now
            continue

        # Use / stop-using (this client or another) does not change addon options.
        # Refresh config labels and HA entities as soon as in-use status changes.
        current_in_use = _in_use_map()
        if current_in_use is not None and current_in_use != last_in_use:
            you_before = {addr for addr, who in last_in_use.items() if who == "you"}
            you_now = {addr for addr, who in current_in_use.items() if who == "you"}
            log.info(
                "USB in-use changed (%s); updating config and HA entities",
                ", ".join(
                    f"{addr}: {last_in_use.get(addr, 'absent')} -> {who}"
                    for addr, who in sorted(current_in_use.items())
                    if last_in_use.get(addr) != who
                )
                or "device list",
            )
            if you_before != you_now:
                discovery.trigger_hardware_discovery()
            sync.discover()
            options = _load_options()
            if options is not None:
                last_intent = _intent_from_options(options)
                last_intent_hash = _intent_hash(options)
            _sync_ha_entities(options)
            last_in_use = current_in_use
            last_discover = now
            continue

        if now - last_discover >= interval:
            log.info("Periodic USB device list refresh")
            sync.discover()
            options = _load_options()
            if options is not None:
                # Labels / new devices may change; use-flag intent stays unless user edits.
                last_intent = _intent_from_options(options)
                last_intent_hash = _intent_hash(options)
            _sync_ha_entities(options)
            refreshed = _in_use_map()
            if refreshed is not None:
                last_in_use = refreshed
            last_discover = now
