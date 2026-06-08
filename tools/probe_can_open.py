from __future__ import annotations

import argparse
import ctypes
import platform
import subprocess
import sys
from pathlib import Path


DLL_CANDIDATES = [
    Path(
        r"C:\Users\huxiaocheng1\Desktop\绿色软件\CANFD分析仪资料20250213(固件V2.11以上)\调试工具\CAN(FD)-bus综合应用软件ZCanPro\bin\x64\ControlCANFD.dll"
    ),
    Path(
        r"C:\Users\huxiaocheng1\Desktop\绿色软件\CANFD分析仪资料20250213(固件V2.11以上)\二次开发库V1.21\x64\ControlCANFD.dll"
    ),
]

DEFAULT_TYPES = [41, 20, 21, 3, 4]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="try device types 0..100")
    args = parser.parse_args()

    print(f"Python: {platform.python_version()} {platform.architecture()[0]}")
    holder = find_device_holder()
    if holder:
        print(f"{holder} is running and may hold the CAN device. Close it completely before probing.")
        print(f"Check with: Get-Process {holder.rsplit('.', 1)[0]}")
        print(f"Close with: Stop-Process -Name {holder.rsplit('.', 1)[0]}")
        sys.exit(2)

    device_types = list(range(0, 101)) if args.full else DEFAULT_TYPES
    for dll_path in DLL_CANDIDATES:
        probe_dll(dll_path, device_types)


def probe_dll(dll_path: Path, device_types: list[int]) -> None:
    print(f"\nDLL: {dll_path}")
    print(f"exists={dll_path.exists()}")
    if not dll_path.exists():
        return

    try:
        dll = ctypes.WinDLL(str(dll_path))
    except OSError as exc:
        print(f"load failed: {exc}")
        return

    required = ["ZCAN_OpenDevice", "ZCAN_CloseDevice"]
    missing = [name for name in required if not hasattr(dll, name)]
    if missing:
        print(f"missing: {', '.join(missing)}")
        return

    dll.ZCAN_OpenDevice.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
    dll.ZCAN_OpenDevice.restype = ctypes.c_void_p
    dll.ZCAN_CloseDevice.argtypes = [ctypes.c_void_p]
    dll.ZCAN_CloseDevice.restype = ctypes.c_uint

    successes = []
    for device_type in device_types:
        handle = dll.ZCAN_OpenDevice(device_type, 0, 0)
        print(f"ZCAN_OpenDevice(type={device_type}, index=0) -> handle={handle}")
        if handle:
            successes.append(device_type)
            dll.ZCAN_CloseDevice(handle)

    print(f"successes={successes}")


# Tools that open the CAN device exclusively and would block this probe.
DEVICE_HOLDER_PROCESSES = ("ZCANPRO.exe", "canfd-net-tool.exe", "CANTest.exe")


def find_device_holder() -> str | None:
    try:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except Exception:
        return None
    running = result.stdout.lower()
    for name in DEVICE_HOLDER_PROCESSES:
        if name.lower() in running:
            return name
    return None


if __name__ == "__main__":
    main()
