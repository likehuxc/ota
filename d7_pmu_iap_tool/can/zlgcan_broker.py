"""32-bit CAN broker process.

The GUI runs under 64-bit Python (PySide6 has no 32-bit build), but ZLG's
``zlgcan.dll`` is 32-bit. This broker is launched by a 32-bit Python interpreter,
loads the DLL in-process via :class:`ZlgVciCanDriver`, and exposes open/close/
send/receive over a line-delimited JSON protocol on stdin/stdout. The 64-bit
side talks to it through ``ZlgCanBrokerDriver`` (see ``broker_can_driver.py``).

Protocol: one JSON object per line in, one JSON object per line out.

  -> {"cmd": "open", "dll": "...", "device_type": 3, "device_index": 0,
      "channel": 0, "baudrate": 500000}
  <- {"ok": true}                      | {"ok": false, "error": "..."}
  -> {"cmd": "send", "frame": {"id": 2047, "data": "161902",
      "extended": false, "remote": false}}
  <- {"ok": true}                      | {"ok": false, "error": "..."}
  -> {"cmd": "receive", "timeout_ms": 50}
  <- {"ok": true, "frame": {...}}      | {"ok": true, "frame": null}
  -> {"cmd": "close" | "is_open" | "last_error" | "ping" | "shutdown"}

stdout carries ONLY protocol lines; diagnostics go to stderr.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Make the repo importable regardless of the working directory (the driver
# chdir()s into the DLL folder when opening the device).
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from d7_pmu_iap_tool.can.can_frame import CanFrame  # noqa: E402
from d7_pmu_iap_tool.can.zlg_vci_can_driver import ZlgVciCanDriver  # noqa: E402


def _frame_to_json(frame: CanFrame) -> dict:
    return {
        "id": frame.id,
        "data": bytes(frame.data[: frame.dlc]).hex(),
        "extended": frame.extended,
        "remote": frame.remote,
        "fd": frame.fd,
        "brs": frame.brs,
        "esi": frame.esi,
    }


def _frame_from_json(payload: dict) -> CanFrame:
    return CanFrame(
        id=int(payload["id"]),
        data=bytes.fromhex(payload.get("data", "")),
        extended=bool(payload.get("extended", False)),
        remote=bool(payload.get("remote", False)),
        fd=bool(payload.get("fd", False)),
        brs=bool(payload.get("brs", False)),
        esi=bool(payload.get("esi", False)),
    )


def _handle(driver: ZlgVciCanDriver, request: dict) -> dict:
    cmd = request.get("cmd")
    if cmd == "ping":
        return {"ok": True}
    if cmd == "open":
        driver.set_dll_path(request["dll"])
        open_args = (
            int(request["device_type"]),
            int(request["device_index"]),
            int(request["channel"]),
            int(request["baudrate"]),
        )
        if request.get("can_fd"):
            ok = driver.open(
                *open_args,
                can_fd=True,
                data_baudrate=int(request.get("data_baudrate", request["baudrate"])),
            )
        else:
            ok = driver.open(*open_args)
        if not ok:
            return {"ok": False, "error": driver.last_error}
        return {
            "ok": True,
            "device_type": driver.device_type,
            "device_index": driver.device_index,
            "channel": driver.channel,
            "can_fd": bool(getattr(driver, "can_fd", False)),
            "data_baudrate": getattr(driver, "data_baudrate", None),
        }
    if cmd == "close":
        driver.close()
        return {"ok": True}
    if cmd == "is_open":
        return {"ok": True, "value": driver.is_open()}
    if cmd == "send":
        ok = driver.send(_frame_from_json(request["frame"]))
        return {"ok": ok} if ok else {"ok": False, "error": driver.last_error}
    if cmd == "receive":
        frame = driver.receive(int(request.get("timeout_ms", 0)))
        return {"ok": True, "frame": _frame_to_json(frame) if frame else None}
    if cmd == "last_error":
        return {"ok": True, "value": driver.last_error}
    if cmd == "shutdown":
        return {"ok": True, "shutdown": True}
    return {"ok": False, "error": f"unknown cmd: {cmd!r}"}


def main() -> None:
    driver = ZlgVciCanDriver()
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            response = _handle(driver, request)
        except Exception as exc:  # never let one bad command kill the broker
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        out.write(json.dumps(response) + "\n")
        out.flush()
        if response.get("shutdown"):
            break
    try:
        driver.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
