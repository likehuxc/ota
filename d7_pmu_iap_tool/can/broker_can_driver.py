"""64-bit CanDriver that proxies to the 32-bit zlgcan broker process.

PySide6 (the GUI) is 64-bit only; ZLG's ``zlgcan.dll`` is 32-bit. This driver
implements the :class:`CanDriver` interface in the 64-bit GUI process by
launching ``zlgcan_broker.py`` under a 32-bit Python interpreter and exchanging
line-delimited JSON with it (see ``zlgcan_broker.py`` for the protocol).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from d7_pmu_iap_tool.can.can_frame import CanDriver, CanFrame

_BROKER_SCRIPT = Path(__file__).resolve().with_name("zlgcan_broker.py")
_CREATE_NO_WINDOW = 0x08000000  # Windows: don't pop a console for the broker


def find_python32() -> list[str] | None:
    """Return an argv prefix that launches a 32-bit Python, or None."""
    # 1) Explicit override.
    override = os.environ.get("D7_PYTHON32")
    if override and Path(override).exists():
        return [override]
    # 2) The Windows 'py' launcher with the 32-bit selector.
    py = shutil.which("py")
    if py:
        try:
            out = subprocess.run(
                [py, "-3-32", "-c", "import struct;print(struct.calcsize('P'))"],
                capture_output=True, text=True, timeout=10,
            )
            if out.returncode == 0 and out.stdout.strip() == "4":
                return [py, "-3-32"]
        except Exception:
            pass
    # 3) Common per-user 32-bit install locations.
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        base = Path(local) / "Programs" / "Python"
        for cand in sorted(base.glob("Python*-32/python.exe"), reverse=True):
            if cand.exists():
                return [str(cand)]
    return None


class ZlgCanBrokerDriver(CanDriver):
    def __init__(self, dll_path: str | Path, python32: list[str] | None = None) -> None:
        self.dll_path = str(dll_path)
        self._python32 = python32
        self._proc: subprocess.Popen | None = None
        self._is_open = False
        self._last_error = ""
        self._device_type = 0
        self._device_index = 0
        self._channel = 0

    # -- process lifecycle -------------------------------------------------
    def _ensure_broker(self) -> bool:
        if self._proc and self._proc.poll() is None:
            return True
        py = self._python32 or find_python32()
        if not py:
            self._last_error = "找不到 32 位 Python（请安装，或设置 D7_PYTHON32 指向 32 位 python.exe）"
            return False
        try:
            self._proc = subprocess.Popen(
                [*py, "-u", str(_BROKER_SCRIPT)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                cwd=str(_BROKER_SCRIPT.parents[2]),
                creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except OSError as exc:
            self._last_error = f"启动 CAN 子进程失败：{exc}"
            self._proc = None
            return False
        return True

    def _request(self, payload: dict) -> dict | None:
        if not self._ensure_broker():
            return None
        assert self._proc and self._proc.stdin and self._proc.stdout
        try:
            self._proc.stdin.write(json.dumps(payload) + "\n")
            self._proc.stdin.flush()
            line = self._proc.stdout.readline()
        except (OSError, ValueError) as exc:
            self._last_error = f"CAN 子进程通信失败：{exc}"
            return None
        if not line:
            self._last_error = "CAN 子进程已退出"
            self._proc = None
            return None
        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            self._last_error = f"CAN 子进程返回无法解析：{exc}"
            return None

    def shutdown(self) -> None:
        """Terminate the broker process (call when the app closes)."""
        if self._proc and self._proc.poll() is None:
            try:
                self._request({"cmd": "shutdown"})
                self._proc.wait(timeout=2)
            except Exception:
                self._proc.kill()
        self._proc = None
        self._is_open = False

    def __del__(self) -> None:
        try:
            self.shutdown()
        except Exception:
            pass

    # -- CanDriver interface ----------------------------------------------
    def open(self, device_type: int, device_index: int, channel: int, baudrate: int) -> bool:
        self._last_error = ""
        response = self._request({
            "cmd": "open",
            "dll": self.dll_path,
            "device_type": device_type,
            "device_index": device_index,
            "channel": channel,
            "baudrate": baudrate,
        })
        if response is None:
            return False
        if response.get("ok"):
            self._is_open = True
            self._device_type = response.get("device_type", device_type)
            self._device_index = response.get("device_index", device_index)
            self._channel = response.get("channel", channel)
            return True
        self._last_error = response.get("error", "open failed")
        return False

    @property
    def device_type(self) -> int:
        return self._device_type

    @property
    def device_index(self) -> int:
        return self._device_index

    @property
    def channel(self) -> int:
        return self._channel

    def close(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._request({"cmd": "close"})
        self._is_open = False

    def is_open(self) -> bool:
        return self._is_open

    def send(self, frame: CanFrame) -> bool:
        response = self._request({
            "cmd": "send",
            "frame": {
                "id": frame.id,
                "data": bytes(frame.data[: frame.dlc]).hex(),
                "extended": frame.extended,
                "remote": frame.remote,
            },
        })
        if response is None:
            return False
        if response.get("ok"):
            return True
        self._last_error = response.get("error", "send failed")
        return False

    def receive(self, timeout_ms: int) -> CanFrame | None:
        response = self._request({"cmd": "receive", "timeout_ms": timeout_ms})
        if response is None or not response.get("ok"):
            if response is not None:
                self._last_error = response.get("error", "receive failed")
            return None
        payload = response.get("frame")
        if not payload:
            return None
        return CanFrame(
            id=int(payload["id"]),
            data=bytes.fromhex(payload.get("data", "")),
            extended=bool(payload.get("extended", False)),
            remote=bool(payload.get("remote", False)),
        )

    @property
    def last_error(self) -> str:
        return self._last_error
