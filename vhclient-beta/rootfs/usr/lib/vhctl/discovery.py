"""Trigger Home Assistant USB discovery after VirtualHere attach."""

from __future__ import annotations

import logging
import time

from . import ha

log = logging.getLogger("vhctl")


def ha_usb_scan(timeout: float = 15.0) -> bool:
    """Ask Home Assistant Core to scan for new USB hardware (websocket usb/scan)."""
    try:
        with ha.HaWebsocket(timeout=timeout) as ws:
            msg = ws.call({"type": "usb/scan"}, timeout=timeout)
            if msg.get("success"):
                log.info("Triggered Home Assistant USB discovery (usb/scan)")
                return True
            log.warning("usb/scan failed: %s", msg)
            return False
    except Exception as exc:  # noqa: BLE001
        log.warning("Home Assistant usb/scan failed: %s", exc)
        return False


def trigger_hardware_discovery(delay_seconds: float = 2.0) -> None:
    """Notify HA that new USB hardware may be available after VirtualHere bind.

    Gives the kernel/usbip stack a moment to create device nodes, then asks
    Home Assistant Core to run usb/scan via homeassistant_api.
    """
    if delay_seconds > 0:
        time.sleep(delay_seconds)
    ha_usb_scan()
