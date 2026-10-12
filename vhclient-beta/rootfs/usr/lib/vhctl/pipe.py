"""Linux named-pipe client for the VirtualHere Client API.

Writes commands to /tmp/vhclient and reads responses from /tmp/vhclient_response.
See https://www.virtualhere.com/client_api
"""

from __future__ import annotations

import os
import select
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Optional

REQUEST_PIPE = "/tmp/vhclient"
RESPONSE_PIPE = "/tmp/vhclient_response"
VHCLIENT_BIN = "/usr/bin/vhclient"
DEFAULT_TIMEOUT = 5.0

# VirtualHere exposes a single request/response pipe pair; serialize all IPC
# so health checks, watch, entity sync, and MQTT handlers cannot interleave.
_IPC_LOCK = threading.Lock()


class PipeError(Exception):
    """Raised when IPC with the VirtualHere client fails."""


@dataclass
class PipeResult:
    text: str
    ok: bool
    failed: bool
    error: bool

    @classmethod
    def from_response(cls, text: str) -> "PipeResult":
        stripped = (text or "").strip()
        upper = stripped.upper()
        error = upper.startswith("ERROR")
        failed = upper.startswith("FAILED") or error
        # LIST / DEVICE INFO / GET CLIENT STATE return content, not always "OK"
        ok = (not failed) and (stripped != "")
        if upper == "OK" or upper.startswith("OK"):
            ok = True
            failed = False
            error = False
        return cls(text=stripped, ok=ok, failed=failed, error=error)


def pipes_ready() -> bool:
    return os.path.exists(REQUEST_PIPE)


def _read_response(timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not os.path.exists(RESPONSE_PIPE):
            time.sleep(0.05)
            continue
        try:
            fd = os.open(RESPONSE_PIPE, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            time.sleep(0.05)
            continue
        try:
            chunks: list[bytes] = []
            while time.monotonic() < deadline:
                remaining = max(0.0, deadline - time.monotonic())
                ready, _, _ = select.select([fd], [], [], min(remaining, 0.5))
                if not ready:
                    if chunks:
                        break
                    continue
                data = os.read(fd, 65536)
                if not data:
                    break
                chunks.append(data)
                # VirtualHere often finishes a response in one read; give a short
                # grace period for trailing bytes then return.
                time.sleep(0.05)
                ready, _, _ = select.select([fd], [], [], 0)
                if not ready:
                    break
            raw = b"".join(chunks)
            if not raw:
                continue
            # Official Python sample treats the last byte as an exit code.
            if len(raw) > 1 and raw[-1] < 32 and raw[-1] in (0, 1, 2):
                raw = raw[:-1]
            return raw.decode("utf-8", errors="replace")
        finally:
            os.close(fd)
    raise PipeError(f"Timeout waiting for response on {RESPONSE_PIPE}")


def _write_request(command: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    payload = command if command.endswith("\n") else f"{command}\n"
    while time.monotonic() < deadline:
        if not os.path.exists(REQUEST_PIPE):
            time.sleep(0.05)
            continue
        try:
            fd = os.open(REQUEST_PIPE, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            time.sleep(0.05)
            continue
        try:
            remaining = payload.encode("utf-8")
            while remaining:
                ready_timeout = max(0.0, deadline - time.monotonic())
                _, ready, _ = select.select([], [fd], [], ready_timeout)
                if not ready:
                    raise PipeError(f"Timeout writing to {REQUEST_PIPE}")
                written = os.write(fd, remaining)
                remaining = remaining[written:]
            return
        finally:
            os.close(fd)
    raise PipeError(f"Timeout opening {REQUEST_PIPE}")


def _command_via_t(command: str, timeout: float) -> PipeResult:
    try:
        completed = subprocess.run(
            [VHCLIENT_BIN, "-t", command],
            capture_output=True,
            text=True,
            timeout=timeout + 1.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PipeError(f"vhclient -t failed: {exc}") from exc
    text = (completed.stdout or completed.stderr or "").strip()
    return PipeResult.from_response(text)


def send_command(
    command: str,
    timeout: float = DEFAULT_TIMEOUT,
    allow_t_fallback: bool = True,
) -> PipeResult:
    """Send a Client API command and return the response."""
    with _IPC_LOCK:
        if pipes_ready():
            try:
                _write_request(command, timeout=timeout)
                text = _read_response(timeout=timeout)
                return PipeResult.from_response(text)
            except PipeError:
                if not allow_t_fallback:
                    raise
        elif not allow_t_fallback:
            raise PipeError(f"Request pipe missing: {REQUEST_PIPE}")

        return _command_via_t(command, timeout=timeout)


def wait_for_pipes(timeout: float = 60.0, poll: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pipes_ready():
            try:
                result = send_command("LIST", timeout=2.0, allow_t_fallback=True)
                if result.ok or "VirtualHere" in result.text or "Auto-Find" in result.text:
                    return True
            except PipeError:
                pass
        time.sleep(poll)
    return False
