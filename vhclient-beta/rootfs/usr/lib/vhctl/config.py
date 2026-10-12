"""Home Assistant app options access for vhctl."""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("vhctl")

OPTIONS_PATH = "/data/options.json"
SUPERVISOR_BASE = os.environ.get("SUPERVISOR_URL", "http://supervisor").rstrip("/")

# Serialize read-modify-write of Supervisor options across discover / MQTT / watch.
_OPTIONS_LOCK = threading.Lock()


def read_options_file() -> Dict[str, Any]:
    with open(OPTIONS_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def read_options() -> Dict[str, Any]:
    """Prefer live Supervisor options; fall back to /data/options.json."""
    try:
        return get_supervisor_options()
    except Exception as exc:  # noqa: BLE001
        log.debug("Supervisor options unavailable (%s); using %s", exc, OPTIONS_PATH)
        return read_options_file()


def supervisor_token() -> Optional[str]:
    return os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN")


def supervisor_request(method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Any:
    token = supervisor_token()
    if not token:
        raise RuntimeError("SUPERVISOR_TOKEN is not set")

    url = f"{SUPERVISOR_BASE}{path}"
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Supervisor {method} {path} failed: {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Supervisor {method} {path} failed: {exc}") from exc

    if not body:
        return None
    parsed = json.loads(body)
    if isinstance(parsed, dict) and "data" in parsed:
        return parsed["data"]
    return parsed


def get_supervisor_options() -> Dict[str, Any]:
    data = supervisor_request("GET", "/addons/self/options/config")
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected Supervisor options response")
    return data


def set_supervisor_options(options: Dict[str, Any]) -> None:
    supervisor_request("POST", "/addons/self/options", {"options": options})


def update_supervisor_options(mutator: Callable[[Dict[str, Any]], bool]) -> bool:
    """Atomically read-modify-write Supervisor options.

    ``mutator`` receives the current options dict and should mutate it in place,
    returning True when a write is required. Concurrent callers are serialized
    so discover refreshes cannot overwrite MQTT switch updates (and vice versa).
    """
    with _OPTIONS_LOCK:
        options = get_supervisor_options()
        if not mutator(options):
            return False
        set_supervisor_options(options)
        return True


def disable_autouse_all() -> bool:
    """Turn off Auto-Use All in options (manual per-device control takes over)."""

    def mutate(options: Dict[str, Any]) -> bool:
        if not bool(options.get("autouse", True)):
            return False
        options["autouse"] = False
        return True

    try:
        if update_supervisor_options(mutate):
            log.info("Disabled Auto-Use All after manual USB device use change")
            return True
    except RuntimeError as exc:
        log.warning("Failed to disable Auto-Use All: %s", exc)
    return False


def ensure_usbdevices(devices: List[str]) -> None:
    """Merge discovered devices into usbdevices, keyed by VirtualHere address.

    Identity is the host.port address (e.g. readynas.11), never status text like
    "(In-use by you)". Labels are refreshed each discover so in-use status stays current.
    """
    # Local import avoids a circular dependency at module load.
    from .api import extract_address

    def mutate(options: Dict[str, Any]) -> bool:
        existing = options.get("usbdevices") or []
        merged: List[Dict[str, Any]] = []
        by_address: Dict[str, Dict[str, Any]] = {}
        changed = False

        # Collapse any duplicates / junk already stored in options.
        for item in existing:
            if not isinstance(item, dict):
                changed = True
                continue
            label = item.get("device")
            if not label:
                changed = True
                continue
            address = extract_address(str(label))
            if not address:
                changed = True
                log.info("Removing usbdevices entry without VH address: %s", label)
                continue
            if address in by_address:
                prior = by_address[address]
                if item.get("use") and not prior.get("use"):
                    prior["use"] = True
                changed = True
                log.info("Removing duplicate usbdevices entry for %s", address)
                continue
            entry = {"device": str(label), "use": bool(item.get("use"))}
            by_address[address] = entry
            merged.append(entry)

        for label in devices:
            address = extract_address(label)
            if not address:
                log.warning("Skipping discovered label without VH address: %s", label)
                continue
            if address in by_address:
                current = by_address[address]
                if current.get("device") != label:
                    current["device"] = label
                    changed = True
                    log.info("Updating label for %s -> %s", address, label)
                continue
            entry = {"device": label, "use": False}
            by_address[address] = entry
            merged.append(entry)
            changed = True
            log.info("Adding to config: %s", label)

        if not changed:
            return False
        options["usbdevices"] = merged
        return True

    try:
        update_supervisor_options(mutate)
    except RuntimeError as exc:
        log.warning("Cannot update usbdevices via Supervisor API: %s", exc)
