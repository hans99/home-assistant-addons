"""VirtualHere Client API command wrappers."""

from __future__ import annotations

import base64
import os
import platform
import re
import socket
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from .pipe import PipeError, PipeResult, send_command

# VirtualHere device addresses look like "readynas.11" / "SERVER.71" (host.port).
# Hub lines use "host:port" and are not matched here.
DEVICE_ADDRESS_RE = re.compile(r"\(([^()\s]+\.\d+)\)")
BARE_ADDRESS_RE = re.compile(r"^[^()\s]+\.\d+$")
DEVICE_LINE_RE = re.compile(r"^\s*(\*)?-->\s*(.*)$")
IN_USE_START_RE = re.compile(r"\(In-use by", re.IGNORECASE)


def normalize_in_use_by(value: str) -> str:
    """Normalize Client API / DEVICE INFO in-use strings."""
    text = (value or "").strip()
    upper = text.upper()
    if upper in ("", "NO ONE", "NOONE"):
        return "NO ONE"
    if upper in ("YOU", "ME"):
        return "you"
    return text


def _name_variants(name: str) -> Set[str]:
    short = (name or "").split(".")[0].strip().lower()
    if not short:
        return set()
    return {short, short.replace("_", "-"), short.replace("-", "_")}


_LOCAL_NAMES: Optional[Set[str]] = None
_LOCAL_IPS: Optional[Set[str]] = None


def local_identity_names() -> Set[str]:
    """Names that mean this addon after a slug rename.

    Supervisor sets the container hostname from the app slug (``_`` becomes ``-``).
    VirtualHere may still report an older hostname from ``vhclient.ini``.
    """
    global _LOCAL_NAMES
    if _LOCAL_NAMES is None:
        names: Set[str] = set()
        for raw in (platform.node(), socket.gethostname(), os.environ.get("HOSTNAME", "")):
            names |= _name_variants(raw)
        _LOCAL_NAMES = names
    return _LOCAL_NAMES


def decode_bound_ip(value: str) -> str:
    """Decode GET CLIENT STATE boundConnectionIp (dotted IPv4 or base64 uint32)."""
    text = (value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", text):
        return "" if text == "0.0.0.0" else text
    # VirtualHere sends a base64 uint32 such as "wKgB7w==", not free text.
    if "=" not in text:
        return ""
    try:
        data = base64.b64decode(text, validate=True)
    except Exception:  # noqa: BLE001
        return ""
    if len(data) < 4 or data[:4] == b"\x00\x00\x00\x00":
        return ""
    return ".".join(str(byte) for byte in data[:4])


def local_ipv4_addresses() -> Set[str]:
    """IPv4 addresses of this host (host network: the HA host address)."""
    global _LOCAL_IPS
    if _LOCAL_IPS is None:
        ips: Set[str] = set()
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ips.add(info[4][0])
        except OSError:
            pass
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(("192.0.2.1", 1))
                ips.add(sock.getsockname()[0])
        except OSError:
            pass
        ips.discard("0.0.0.0")
        ips.discard("127.0.0.1")
        _LOCAL_IPS = ips
    return _LOCAL_IPS


def client_is_local(bound_host: str, client_id: str = "", bound_ip: str = "") -> bool:
    """True when VirtualHere is describing this client, not another one.

    A slug change renames the container hostname. Match the current hostname,
    underscore/hyphen variants, a client id that contains that hostname, and
    the bound connection IP.
    """
    names = local_identity_names()
    if names & _name_variants(bound_host):
        return True
    client = (client_id or "").strip().lower()
    if client and (client in names or any(f"({name})" in client for name in names)):
        return True
    ip = decode_bound_ip(bound_ip)
    return bool(ip) and ip in local_ipv4_addresses()


def format_device_label(display_name: str, address: str, in_use_by: str = "") -> str:
    """Build a config list label: prefer nickname/name, always include in-use status."""
    name = (display_name or "").strip() or address
    who = normalize_in_use_by(in_use_by)
    base = f"{name} ({address})"
    if who == "you":
        return f"{base} (In-use by you)"
    if who == "NO ONE":
        return f"{base} (In-use by: NO ONE)"
    return f"{base} (In-use by: {who})"


def prefer_name(nickname: str = "", product: str = "", fallback: str = "") -> str:
    """Prefer VirtualHere nickname/name over USB product string."""
    for candidate in (nickname, product, fallback):
        text = (candidate or "").strip()
        if text:
            return text
    return ""


@dataclass
class DeviceInfo:
    address: str
    vendor: str = ""
    vendor_id: str = ""
    product: str = ""
    product_id: str = ""
    serial: str = ""
    in_use_by: str = ""
    nickname: str = ""

    @property
    def in_use_by_you(self) -> bool:
        return normalize_in_use_by(self.in_use_by) == "you"

    @property
    def in_use_by_no_one(self) -> bool:
        return normalize_in_use_by(self.in_use_by) == "NO ONE"

    @property
    def display_name(self) -> str:
        return prefer_name(self.nickname, self.product, self.vendor or self.address)

    @property
    def label(self) -> str:
        return format_device_label(self.display_name, self.address, self.in_use_by)


@dataclass
class ListedDevice:
    address: str
    label: str
    name: str = ""
    auto_use: bool = False
    in_use_status: str = ""
    in_use_by: str = ""

    @property
    def in_use_by_you(self) -> bool:
        return normalize_in_use_by(self.in_use_by or self.in_use_status) == "you"

    @property
    def in_use(self) -> bool:
        return normalize_in_use_by(self.in_use_by or self.in_use_status) != "NO ONE"


@dataclass
class ClientStateDevice:
    address: str
    nickname: str = ""
    product: str = ""
    client_id: str = ""
    state: str = ""
    auto_use: str = ""
    bound_client_hostname: str = ""
    bound_connection_ip: str = ""

    @property
    def display_name(self) -> str:
        return prefer_name(self.nickname, self.product, self.address)

    @property
    def in_use_by(self) -> str:
        # Device state: DISCONNECTED=0, CONNECTED=1, BOUND=2 (bitwise).
        try:
            state_val = int(self.state or "0")
        except ValueError:
            state_val = 0
        bound = bool(state_val & 2)
        client = (self.client_id or "").strip()
        host = (self.bound_client_hostname or "").strip()
        if client_is_local(host, client, self.bound_connection_ip):
            return "you"
        if not bound and not client and not host:
            return "NO ONE"
        if client:
            return client
        if host:
            return host
        return "you" if bound else "NO ONE"

    @property
    def label(self) -> str:
        return format_device_label(self.display_name, self.address, self.in_use_by)


@dataclass
class ListStatus:
    devices: List[ListedDevice] = field(default_factory=list)
    autofind: Optional[bool] = None
    auto_use_all: Optional[bool] = None
    raw: str = ""


def cmd(command: str) -> PipeResult:
    return send_command(command)


def list_raw() -> PipeResult:
    return cmd("LIST")


def _balanced_suffix(text: str, start: int) -> str:
    """Return substring from start through matching closing parenthesis."""
    if start < 0 or start >= len(text) or text[start] != "(":
        return text[start:].strip()
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start : index + 1].strip()
    return text[start:].strip()


def parse_device_line(line: str) -> Optional[ListedDevice]:
    match = DEVICE_LINE_RE.match(line)
    if not match:
        return None
    auto_use = bool(match.group(1))
    rest = match.group(2).strip()
    addr_match = DEVICE_ADDRESS_RE.search(rest)
    if not addr_match:
        return None

    address = addr_match.group(1)
    name = rest[: addr_match.start()].strip()
    after = rest[addr_match.end() :]
    in_use_status = ""
    in_use_by = "NO ONE"
    in_use_match = IN_USE_START_RE.search(after)
    if in_use_match:
        # Absolute index in `rest` for balanced-paren extraction.
        abs_start = addr_match.end() + in_use_match.start()
        in_use_status = _balanced_suffix(rest, abs_start)
        lower = in_use_status.lower()
        if "in-use by you" in lower or "in use by you" in lower:
            in_use_by = "you"
        else:
            # "(In-use by: name at ip)" or similar
            detail = re.sub(r"^\(In-use by:?\s*", "", in_use_status, flags=re.I).strip()
            # Drop only the wrapping ")", so a client id like "root (vhclient-beta)" stays intact.
            if detail.endswith(")"):
                detail = detail[:-1].strip()
            in_use_by = detail or "you"
        if in_use_by not in ("you", "NO ONE") and client_is_local(in_use_by, in_use_by):
            in_use_by = "you"

    label = format_device_label(name, address, in_use_by)

    return ListedDevice(
        address=address,
        label=label,
        name=name,
        auto_use=auto_use,
        in_use_status=in_use_status,
        in_use_by=in_use_by,
    )


def parse_list(text: str) -> ListStatus:
    status = ListStatus(raw=text)
    seen: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        lower = stripped.lower()
        if lower.startswith("auto-find currently"):
            status.autofind = "on" in lower
            continue
        if lower.startswith("auto-use all currently"):
            status.auto_use_all = "on" in lower
            continue
        device = parse_device_line(line)
        if device is None:
            continue
        if device.address in seen:
            continue
        seen.add(device.address)
        status.devices.append(device)
    return status


def list_status() -> ListStatus:
    result = list_raw()
    if result.failed:
        raise PipeError(f"LIST failed: {result.text}")
    return parse_list(result.text)


def get_client_state_xml() -> str:
    result = cmd("GET CLIENT STATE")
    if result.failed or not result.text:
        raise PipeError(f"GET CLIENT STATE failed: {result.text}")
    return result.text


def get_client_state_root() -> ET.Element:
    return ET.fromstring(get_client_state_xml())


def iter_client_state_devices(root: Optional[ET.Element] = None) -> List[ClientStateDevice]:
    """Parse devices from GET CLIENT STATE, preferring nickname over product."""
    if root is None:
        root = get_client_state_root()

    devices: List[ClientStateDevice] = []
    seen: set[str] = set()

    for server in list(root):
        connection = server.find("connection")
        hostname = ""
        if connection is not None:
            hostname = (connection.get("hostname") or connection.get("host") or "").strip()

        for device in server.findall("device"):
            port = (device.get("address") or "").strip()
            if not port:
                continue
            if hostname and "." not in port and ":" not in port:
                full_address = f"{hostname}.{port}"
            else:
                full_address = port

            if full_address in seen:
                continue
            seen.add(full_address)

            nickname = (device.get("nickname") or device.get("name") or "").strip()
            product = (device.get("product") or "").strip()
            devices.append(
                ClientStateDevice(
                    address=full_address,
                    nickname=nickname,
                    product=product,
                    client_id=(device.get("clientId") or "").strip(),
                    state=(device.get("state") or "").strip(),
                    auto_use=(device.get("autoUse") or "").strip(),
                    bound_client_hostname=(
                        device.get("boundClientHostname") or device.get("boundHostname") or ""
                    ).strip(),
                    bound_connection_ip=(
                        device.get("boundConnectionIp") or device.get("boundClientIp") or ""
                    ).strip(),
                )
            )
    return devices


def parse_device_info(text: str) -> Optional[DeviceInfo]:
    if not text or not text.strip():
        return None
    fields: Dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip().upper()] = value.strip()
    address = fields.get("ADDRESS", "")
    if not address:
        return None
    return DeviceInfo(
        address=address,
        vendor=fields.get("VENDOR", ""),
        vendor_id=fields.get("VENDOR ID", ""),
        product=fields.get("PRODUCT", ""),
        product_id=fields.get("PRODUCT ID", ""),
        serial=fields.get("SERIAL", ""),
        in_use_by=fields.get("IN USE BY", ""),
        nickname=fields.get("NICKNAME", "") or fields.get("NAME", ""),
    )


def device_info(address: str) -> Optional[DeviceInfo]:
    result = cmd(f"DEVICE INFO,{address}")
    if result.failed:
        raise PipeError(f"DEVICE INFO,{address} failed: {result.text}")
    return parse_device_info(result.text)


def use_device(address: str) -> PipeResult:
    return cmd(f"USE,{address}")


def stop_using(address: str) -> PipeResult:
    return cmd(f"STOP USING,{address}")


def auto_use_device(address: str) -> PipeResult:
    """Toggle Auto-Use for this device on any port."""
    return cmd(f"AUTO USE DEVICE,{address}")


def auto_use_all() -> PipeResult:
    """Turn Auto-Use All on (API documents this as enable)."""
    return cmd("AUTO USE ALL")


def auto_use_clear_all() -> PipeResult:
    return cmd("AUTO USE CLEAR ALL")


def autofind_toggle() -> PipeResult:
    """Toggle Auto-Find (API documents AUTOFIND as turn off; it toggles in practice)."""
    return cmd("AUTOFIND")


def manual_hub_list() -> List[str]:
    result = cmd("MANUAL HUB LIST")
    if result.failed:
        raise PipeError(f"MANUAL HUB LIST failed: {result.text}")
    hubs: List[str] = []
    for line in result.text.splitlines():
        line = line.strip()
        if not line or line.upper().startswith("OK"):
            continue
        # Typical lines are hub addresses; skip banners.
        if " " in line and ":" not in line:
            continue
        hubs.append(line)
    return hubs


def manual_hub_add(address: str) -> PipeResult:
    return cmd(f"MANUAL HUB ADD,{address}")


def manual_hub_remove(address: str) -> PipeResult:
    return cmd(f"MANUAL HUB REMOVE,{address}")


def extract_address(label_or_address: str) -> str:
    """Extract VirtualHere device address (host.port) from a label or bare address."""
    text = (label_or_address or "").strip()
    if not text:
        return ""
    if BARE_ADDRESS_RE.fullmatch(text):
        return text
    match = DEVICE_ADDRESS_RE.search(text)
    if match:
        return match.group(1)
    return ""
