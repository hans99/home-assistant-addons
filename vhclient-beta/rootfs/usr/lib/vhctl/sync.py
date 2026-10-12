"""Orchestrate VirtualHere Client API configuration and device sync."""

from __future__ import annotations

import logging
from typing import Optional, Set

from . import api, config, discovery
from .pipe import PipeError, wait_for_pipes

log = logging.getLogger("vhctl")


def wait(timeout: float = 60.0) -> int:
    log.info("Waiting for VirtualHere client IPC ...")
    if not wait_for_pipes(timeout=timeout):
        log.warning("Timed out waiting for VirtualHere IPC; continuing anyway")
        return 0

    try:
        xml = api.get_client_state_xml()
        if "<connection" in xml:
            for line in xml.splitlines():
                if "<connection" in line:
                    log.info("Server: %s", line.strip())
                    break
        else:
            log.info("IPC ready (no hub connection reported yet)")
    except PipeError as exc:
        log.info("IPC ready (%s)", exc)
    return 0


def _set_autofind(desired: bool, current: Optional[bool]) -> None:
    if current is desired:
        log.info("Auto-Find already %s", "on" if desired else "off")
        return
    # AUTOFIND toggles; call once when state differs.
    result = api.autofind_toggle()
    log.info("AUTOFIND -> %s", result.text)
    # Verify and toggle again if still wrong (best-effort).
    try:
        status = api.list_status()
        if status.autofind is not None and status.autofind != desired:
            result = api.autofind_toggle()
            log.info("AUTOFIND (retry) -> %s", result.text)
    except PipeError as exc:
        log.warning("Could not verify Auto-Find: %s", exc)


def _set_autouse_all(desired: bool, current: Optional[bool]) -> None:
    if desired:
        if current is True:
            log.info("Auto-Use All already on")
            return
        result = api.auto_use_all()
        log.info("AUTO USE ALL -> %s", result.text)
        return

    # Turning off: clear all auto-use settings.
    if current is False:
        # Still clear device-level auto-use that may linger.
        result = api.auto_use_clear_all()
        log.info("AUTO USE CLEAR ALL -> %s", result.text)
        return
    result = api.auto_use_clear_all()
    log.info("AUTO USE CLEAR ALL -> %s", result.text)


def _sync_manual_hub(manualhub: str) -> None:
    desired = (manualhub or "").strip()
    try:
        hubs = api.manual_hub_list()
    except PipeError as exc:
        log.warning("MANUAL HUB LIST failed: %s", exc)
        hubs = []

    normalized = {h.strip() for h in hubs if h.strip()}
    if not desired:
        for hub in list(normalized):
            result = api.manual_hub_remove(hub)
            log.info("MANUAL HUB REMOVE,%s -> %s", hub, result.text)
        return

    if desired not in normalized:
        # Remove others first to match a single configured hub.
        for hub in list(normalized):
            if hub == desired:
                continue
            result = api.manual_hub_remove(hub)
            log.info("MANUAL HUB REMOVE,%s -> %s", hub, result.text)
        result = api.manual_hub_add(desired)
        log.info("MANUAL HUB ADD,%s -> %s", desired, result.text)
    else:
        log.info("Manual hub already present: %s", desired)


def configure() -> int:
    options = config.read_options()
    if options.get("preventconfig"):
        log.info("preventconfig enabled; skipping API configure")
        return 0

    try:
        status = api.list_status()
    except PipeError as exc:
        log.error("Cannot configure: %s", exc)
        return 1

    _set_autofind(bool(options.get("autofind", True)), status.autofind)
    _set_autouse_all(bool(options.get("autouse", True)), status.auto_use_all)

    manualhub = options.get("manualhub") or ""
    if isinstance(manualhub, str):
        _sync_manual_hub(manualhub)

    return 0


def discover() -> int:
    """Refresh usbdevices labels from GET CLIENT STATE (nickname > product)."""
    labels: list[str] = []

    try:
        state_devices = api.iter_client_state_devices()
    except Exception as exc:  # noqa: BLE001 - fall back to LIST
        log.warning("GET CLIENT STATE discover failed (%s); falling back to LIST", exc)
        state_devices = []

    if state_devices:
        for device in state_devices:
            label = device.label
            labels.append(label)
            log.info("Found device: %s", label)
    else:
        try:
            status = api.list_status()
        except PipeError as exc:
            log.error("Cannot discover devices: %s", exc)
            return 1
        for device in status.devices:
            # LIST already prefers nickname when set; always keep in-use status.
            label = device.label
            labels.append(label)
            log.info("Found device: %s", label)

    config.ensure_usbdevices(labels)
    return 0


def _auto_use_addresses(status: api.ListStatus) -> Set[str]:
    return {d.address for d in status.devices if d.auto_use}


def _ensure_auto_use(address: str, want_auto: bool, currently_auto: bool) -> None:
    if want_auto == currently_auto:
        return
    # AUTO USE DEVICE is a toggle — only call when state must change.
    # Caller must already have run DEVICE INFO for this address.
    result = api.auto_use_device(address)
    log.info("AUTO USE DEVICE,%s -> %s", address, result.text)


def sync_devices() -> int:
    options = config.read_options()
    if options.get("preventconfig"):
        log.info("preventconfig enabled; skipping device sync")
        return 0
    if options.get("autouse"):
        log.info("Auto-Use All enabled; skipping per-device sync")
        return 0

    usbdevices = options.get("usbdevices") or []
    if not usbdevices:
        log.info("No usbdevices configured")
        return 0

    try:
        status = api.list_status()
    except PipeError as exc:
        log.error("Cannot sync devices: %s", exc)
        return 1

    auto_addresses = _auto_use_addresses(status)
    hardware_changed = False

    for item in usbdevices:
        if not isinstance(item, dict):
            continue
        label = item.get("device")
        if not label:
            continue
        want_use = bool(item.get("use"))
        address = api.extract_address(str(label))
        if not address:
            continue

        # Required gate: DEVICE INFO before any AUTO USE / USE decision.
        try:
            info = api.device_info(address)
        except PipeError as exc:
            log.warning("DEVICE INFO,%s failed: %s", address, exc)
            continue

        if info is None:
            log.info("DEVICE INFO,%s empty; device not present, skipping", address)
            continue

        log.info(
            "DEVICE INFO,%s IN USE BY: %s",
            address,
            info.in_use_by or "NO ONE",
        )
        currently_auto = address in auto_addresses

        if want_use:
            if info.in_use_by_you:
                _ensure_auto_use(address, want_auto=True, currently_auto=currently_auto)
            elif info.in_use_by_no_one:
                result = api.use_device(address)
                log.info("USE,%s -> %s", address, result.text)
                hardware_changed = True
                # Re-check DEVICE INFO before toggling AUTO USE.
                try:
                    info_after = api.device_info(address)
                except PipeError as exc:
                    log.warning("DEVICE INFO,%s after USE failed: %s", address, exc)
                    continue
                if info_after is None:
                    continue
                try:
                    currently_auto = address in _auto_use_addresses(api.list_status())
                except PipeError:
                    currently_auto = False
                _ensure_auto_use(address, want_auto=True, currently_auto=currently_auto)
            else:
                log.info(
                    "Skipping %s; in use by someone else (%s)",
                    address,
                    info.in_use_by,
                )
        else:
            if info.in_use_by_you:
                result = api.stop_using(address)
                log.info("STOP USING,%s -> %s", address, result.text)
                hardware_changed = True
            _ensure_auto_use(address, want_auto=False, currently_auto=currently_auto)

    if hardware_changed:
        discovery.trigger_hardware_discovery()

    return 0


def apply_all() -> int:
    """Apply global options, sync device use, then refresh USB list labels."""
    rc = configure()
    # Sync before discover so in-use labels reflect the result of USE/STOP USING.
    sync_rc = sync_devices()
    discover_rc = discover()
    if rc or sync_rc or discover_rc:
        return 1
    return 0
