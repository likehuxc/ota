"""32-bit probe for the pirated ZLG USBCAN-I clone.

Run with a 32-bit Python interpreter, e.g.:

    py -3.12-32 tools\\probe_can_open32.py

Close ZCanPro first (USB CAN devices are exclusive — only one process can
open them at a time). The goal is to find which device type + API opens the
box using the 32-bit Win32 ControlCANFD.dll that ZCanPro itself uses.
"""

from __future__ import annotations

import ctypes
import os
import platform
import subprocess
import sys

# The 32-bit zlgcan.dll that the INSTALLED ZCanPro actually loads. It loads
# device-specific backends from the sibling kerneldlls\ folder (usbcan.dll for
# the classic USBCAN-I/II), so we must run with this directory as CWD.
WIN32_DLL = r"C:\Program Files (x86)\ZCANPRO\zlgcan.dll"

DEVICE_HOLDER_PROCESSES = ("ZCANPRO.exe", "canfd-net-tool.exe", "CANTest.exe")
# USBCAN-I is conventionally type 3, USBCAN-II type 4; include the CANFD ids too.
CANDIDATE_TYPES = (3, 4, 1, 2, 5, 6, 19, 20, 21, 41, 42, 43)


class VciBoardInfo(ctypes.Structure):
    _fields_ = [
        ("hw_Version", ctypes.c_ushort),
        ("fw_Version", ctypes.c_ushort),
        ("dr_Version", ctypes.c_ushort),
        ("in_Version", ctypes.c_ushort),
        ("irq_Num", ctypes.c_ushort),
        ("can_Num", ctypes.c_ubyte),
        ("str_Serial_Num", ctypes.c_char * 20),
        ("str_hw_Type", ctypes.c_char * 40),
        ("Reserved", ctypes.c_ushort * 4),
    ]


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


def main() -> None:
    arch = platform.architecture()[0]
    print(f"Python: {platform.python_version()} {arch}")
    if arch != "32bit":
        print("!! This must be run with a 32-bit Python. Aborting.")
        sys.exit(1)

    holder = find_device_holder()
    if holder:
        print(f"!! {holder} is running and holds the device. Close it first. Aborting.")
        sys.exit(2)

    if not os.path.exists(WIN32_DLL):
        print(f"!! DLL not found: {WIN32_DLL}")
        sys.exit(1)

    dll_dir = os.path.dirname(WIN32_DLL)
    os.chdir(dll_dir)
    try:
        os.add_dll_directory(dll_dir)
    except Exception:
        pass

    try:
        dll = ctypes.WinDLL(WIN32_DLL)
    except OSError as exc:
        print(f"!! load failed: {exc}")
        sys.exit(1)
    print(f"Loaded: {WIN32_DLL}\n")

    probe_vci(dll)
    print()
    probe_zcan(dll)


def probe_vci(dll) -> None:
    print("=== VCI_* API (classic) ===")
    if not hasattr(dll, "VCI_OpenDevice"):
        print("VCI_OpenDevice not exported")
        return
    dll.VCI_OpenDevice.argtypes = [ctypes.c_uint] * 3
    dll.VCI_OpenDevice.restype = ctypes.c_uint
    dll.VCI_CloseDevice.argtypes = [ctypes.c_uint] * 2
    dll.VCI_CloseDevice.restype = ctypes.c_uint
    has_info = hasattr(dll, "VCI_ReadBoardInfo")
    if has_info:
        dll.VCI_ReadBoardInfo.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
        dll.VCI_ReadBoardInfo.restype = ctypes.c_uint

    for t in CANDIDATE_TYPES:
        r = dll.VCI_OpenDevice(t, 0, 0)
        marker = "  <<< OPENED" if r == 1 else ""
        print(f"VCI_OpenDevice(type={t}, index=0) -> {r}{marker}")
        if r == 1:
            if has_info:
                info = VciBoardInfo()
                if dll.VCI_ReadBoardInfo(t, 0, ctypes.byref(info)) == 1:
                    print(
                        f"    board: hw_type={info.str_hw_Type.decode(errors='replace')!r} "
                        f"can_Num={info.can_Num} serial={info.str_Serial_Num.decode(errors='replace')!r}"
                    )
            dll.VCI_CloseDevice(t, 0)


def probe_zcan(dll) -> None:
    print("=== ZCAN_* API (handle-based) ===")
    if not hasattr(dll, "ZCAN_OpenDevice"):
        print("ZCAN_OpenDevice not exported")
        return
    dll.ZCAN_OpenDevice.argtypes = [ctypes.c_uint] * 3
    dll.ZCAN_OpenDevice.restype = ctypes.c_void_p
    dll.ZCAN_CloseDevice.argtypes = [ctypes.c_void_p]
    dll.ZCAN_CloseDevice.restype = ctypes.c_uint

    for t in CANDIDATE_TYPES:
        h = dll.ZCAN_OpenDevice(t, 0, 0)
        marker = "  <<< OPENED" if h else ""
        print(f"ZCAN_OpenDevice(type={t}, index=0) -> handle={h}{marker}")
        if h:
            dll.ZCAN_CloseDevice(h)


if __name__ == "__main__":
    main()
