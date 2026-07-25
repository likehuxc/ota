from __future__ import annotations

import ctypes
import os
import threading
from pathlib import Path

from d7_pmu_iap_tool.can.can_frame import CanDriver, CanFrame

STATUS_OK = 1

# ZLG zlgcan device types. The connected hardware is a (cloned) ZLG USBCAN-I
# (type 3); USBCAN-II is type 4. zlgcan.dll loads kerneldlls\usbcan.dll for both.
ZCAN_USBCAN1 = 3
ZCAN_USBCAN2 = 4
DEFAULT_DEVICE_TYPE = ZCAN_USBCAN1
DEVICE_TYPE_CANDIDATES = (ZCAN_USBCAN1, ZCAN_USBCAN2)

INVALID_DEVICE_HANDLE = 0
INVALID_CHANNEL_HANDLE = 0
TYPE_CAN = 0
TYPE_CANFD = 1


class ZcanChannelCanInitConfig(ctypes.Structure):
    _fields_ = [
        ("acc_code", ctypes.c_uint),
        ("acc_mask", ctypes.c_uint),
        ("reserved", ctypes.c_uint),
        ("filter", ctypes.c_ubyte),
        ("timing0", ctypes.c_ubyte),
        ("timing1", ctypes.c_ubyte),
        ("mode", ctypes.c_ubyte),
    ]


class ZcanChannelCanfdInitConfig(ctypes.Structure):
    _fields_ = [
        ("acc_code", ctypes.c_uint),
        ("acc_mask", ctypes.c_uint),
        ("abit_timing", ctypes.c_uint),
        ("dbit_timing", ctypes.c_uint),
        ("brp", ctypes.c_uint),
        ("filter", ctypes.c_ubyte),
        ("mode", ctypes.c_ubyte),
        ("pad", ctypes.c_ushort),
        ("reserved", ctypes.c_uint),
    ]


class ZcanChannelInitUnion(ctypes.Union):
    _fields_ = [
        ("can", ZcanChannelCanInitConfig),
        ("canfd", ZcanChannelCanfdInitConfig),
    ]


class ZcanChannelInitConfig(ctypes.Structure):
    _fields_ = [
        ("can_type", ctypes.c_uint),
        ("config", ZcanChannelInitUnion),
    ]


class ZcanCanFrame(ctypes.Structure):
    _fields_ = [
        ("can_id", ctypes.c_uint, 29),
        ("err", ctypes.c_uint, 1),
        ("rtr", ctypes.c_uint, 1),
        ("eff", ctypes.c_uint, 1),
        ("can_dlc", ctypes.c_ubyte),
        ("__pad", ctypes.c_ubyte),
        ("__res0", ctypes.c_ubyte),
        ("__res1", ctypes.c_ubyte),
        ("data", ctypes.c_ubyte * 8),
    ]


class ZcanTransmitData(ctypes.Structure):
    _fields_ = [
        ("frame", ZcanCanFrame),
        ("transmit_type", ctypes.c_uint),
    ]


class ZcanReceiveData(ctypes.Structure):
    _fields_ = [
        ("frame", ZcanCanFrame),
        ("timestamp", ctypes.c_ulonglong),
    ]


class ZcanCanfdFrame(ctypes.Structure):
    _fields_ = [
        ("can_id", ctypes.c_uint, 29),
        ("err", ctypes.c_uint, 1),
        ("rtr", ctypes.c_uint, 1),
        ("eff", ctypes.c_uint, 1),
        ("len", ctypes.c_ubyte),
        ("brs", ctypes.c_ubyte, 1),
        ("esi", ctypes.c_ubyte, 1),
        ("__res", ctypes.c_ubyte, 6),
        ("__res0", ctypes.c_ubyte),
        ("__res1", ctypes.c_ubyte),
        ("data", ctypes.c_ubyte * 64),
    ]


class ZcanTransmitFdData(ctypes.Structure):
    _fields_ = [
        ("frame", ZcanCanfdFrame),
        ("transmit_type", ctypes.c_uint),
    ]


class ZcanReceiveFdData(ctypes.Structure):
    _fields_ = [
        ("frame", ZcanCanfdFrame),
        ("timestamp", ctypes.c_ulonglong),
    ]


class ZlgVciCanDriver(CanDriver):
    """In-process CAN driver for a ZLG USBCAN-I using ZLG's ``zlgcan.dll``.

    This is the classic CAN 2.0 bring-up: ``ZCAN_OpenDevice`` returns a device
    handle, ``ZCAN_InitCAN`` (with SJA1000 Timing0/Timing1 for the baud rate)
    returns a channel handle, then ``ZCAN_StartCAN``. zlgcan.dll dynamically
    loads ``kerneldlls\\usbcan.dll`` from its own directory, so the DLL folder is
    made the working directory at load time.

    IMPORTANT: zlgcan.dll is 32-bit, so this driver only runs under a 32-bit
    Python interpreter. The 64-bit GUI drives it out-of-process via the broker
    (see ``zlgcan_broker.py`` / the broker proxy driver).
    """

    def __init__(self, dll_path: str | Path | None = None, *, change_working_directory: bool = True) -> None:
        self.dll_path = str(dll_path) if dll_path else ""
        self._change_working_directory = change_working_directory
        self._dll_directory_handle = None
        self._dll = None
        self._is_open = False
        self._last_error = ""
        self._device_type = DEFAULT_DEVICE_TYPE
        self._device_index = 0
        self._channel = 0
        self._device_handle = INVALID_DEVICE_HANDLE
        self._channel_handle = INVALID_CHANNEL_HANDLE
        self._io_lock = threading.RLock()
        self._can_fd = False
        self._data_baudrate: int | None = None

    def set_dll_path(self, dll_path: str | Path) -> None:
        if self._is_open:
            raise RuntimeError("CAN device is open; close it before changing DLL")
        self.dll_path = str(dll_path)
        self._dll = None

    def open(
        self,
        device_type: int,
        device_index: int,
        channel: int,
        baudrate: int,
        *,
        can_fd: bool = False,
        data_baudrate: int | None = None,
    ) -> bool:
        self._last_error = ""
        self._can_fd = can_fd
        self._data_baudrate = int(data_baudrate or baudrate) if can_fd else None
        if not self._load_dll():
            return False
        if not self._resolve_functions():
            return False

        timing0 = timing1 = 0
        if not can_fd:
            try:
                timing0, timing1 = timing_for_baudrate(baudrate)
            except ValueError as exc:
                self._last_error = str(exc)
                return False

        errors = []
        candidates = (device_type,) if can_fd else _candidate_device_types(device_type)
        for candidate_type in candidates:
            error = self._open_with_device_type(
                candidate_type,
                device_index,
                channel,
                timing0,
                timing1,
                can_fd=can_fd,
                arbitration_baudrate=baudrate,
                data_baudrate=self._data_baudrate,
            )
            if error is None:
                return True
            errors.append(f"{candidate_type}: {error}")

        self._last_error = "Open CAN device failed. Tried " + "; ".join(errors)
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

    @property
    def can_fd(self) -> bool:
        return self._can_fd

    @property
    def data_baudrate(self) -> int | None:
        return self._data_baudrate

    def _open_with_device_type(
        self,
        device_type: int,
        device_index: int,
        channel: int,
        timing0: int,
        timing1: int,
        *,
        can_fd: bool = False,
        arbitration_baudrate: int = 1_000_000,
        data_baudrate: int | None = None,
    ) -> str | None:
        device_handle = self._dll.ZCAN_OpenDevice(device_type, device_index, 0)
        if not device_handle:
            return "ZCAN_OpenDevice failed"

        config = ZcanChannelInitConfig()
        if can_fd:
            if not self._configure_canfd_baudrates(
                device_handle,
                channel,
                arbitration_baudrate,
                int(data_baudrate or arbitration_baudrate),
            ):
                self._dll.ZCAN_CloseDevice(device_handle)
                return self._last_error
            config.can_type = TYPE_CANFD
            config.config.canfd.acc_code = 0x00000000
            config.config.canfd.acc_mask = 0xFFFFFFFF
            config.config.canfd.filter = 0
            config.config.canfd.mode = 0
        else:
            config.can_type = TYPE_CAN
            config.config.can.acc_code = 0x00000000
            config.config.can.acc_mask = 0xFFFFFFFF
            config.config.can.reserved = 0
            config.config.can.filter = 0
            config.config.can.timing0 = timing0
            config.config.can.timing1 = timing1
            config.config.can.mode = 0

        channel_handle = self._dll.ZCAN_InitCAN(device_handle, channel, ctypes.byref(config))
        if not channel_handle:
            self._dll.ZCAN_CloseDevice(device_handle)
            return f"ZCAN_InitCAN failed for channel {channel}"

        if self._dll.ZCAN_StartCAN(channel_handle) != STATUS_OK:
            self._dll.ZCAN_ResetCAN(channel_handle)
            self._dll.ZCAN_CloseDevice(device_handle)
            return f"ZCAN_StartCAN failed for channel {channel}"

        self._device_handle = device_handle
        self._channel_handle = channel_handle
        self._device_type = device_type
        self._device_index = device_index
        self._channel = channel
        self._is_open = True
        return None

    def _configure_canfd_baudrates(
        self,
        device_handle: int,
        channel: int,
        arbitration_baudrate: int,
        data_baudrate: int,
    ) -> bool:
        if hasattr(self._dll, "ZCAN_SetAbitBaud") and hasattr(self._dll, "ZCAN_SetDbitBaud"):
            if self._dll.ZCAN_SetAbitBaud(device_handle, channel, arbitration_baudrate) != STATUS_OK:
                self._last_error = f"CAN FD 仲裁域波特率设置失败：{arbitration_baudrate}"
                return False
            if self._dll.ZCAN_SetDbitBaud(device_handle, channel, data_baudrate) != STATUS_OK:
                self._last_error = f"CAN FD 数据域波特率设置失败：{data_baudrate}"
                return False
            if hasattr(self._dll, "ZCAN_SetCANFDStandard"):
                if self._dll.ZCAN_SetCANFDStandard(device_handle, channel, 0) != STATUS_OK:
                    self._last_error = "CAN FD ISO 模式设置失败"
                    return False
            return True

        if hasattr(self._dll, "ZCAN_SetValue"):
            settings = (
                (f"{channel}/canfd_abit_baud_rate", str(arbitration_baudrate)),
                (f"{channel}/canfd_dbit_baud_rate", str(data_baudrate)),
            )
            for path, value in settings:
                result = self._dll.ZCAN_SetValue(device_handle, path.encode("ascii"), value.encode("ascii"))
                if result != STATUS_OK:
                    self._last_error = f"CAN FD 参数设置失败：{path}={value}"
                    return False
            return True

        self._last_error = "当前 DLL 缺少 CAN FD 波特率配置接口"
        return False

    def close(self) -> None:
        with self._io_lock:
            if self._dll and self._is_open:
                if self._channel_handle != INVALID_CHANNEL_HANDLE:
                    self._dll.ZCAN_ResetCAN(self._channel_handle)
                if self._device_handle != INVALID_DEVICE_HANDLE:
                    self._dll.ZCAN_CloseDevice(self._device_handle)
            self._is_open = False
            self._channel_handle = INVALID_CHANNEL_HANDLE
            self._device_handle = INVALID_DEVICE_HANDLE

    def is_open(self) -> bool:
        return self._is_open

    def send(self, frame: CanFrame) -> bool:
        with self._io_lock:
            if not self._is_open or not self._dll:
                self._last_error = "CAN device is not open"
                return False
            if frame.fd:
                if not self._can_fd:
                    self._last_error = "CAN FD frame requires a CAN FD channel"
                    return False
                data = frame_to_zcan_transmit_fd_data(frame)
                sent = self._dll.ZCAN_TransmitFD(self._channel_handle, ctypes.byref(data), 1)
            else:
                data = frame_to_zcan_transmit_data(frame)
                sent = self._dll.ZCAN_Transmit(self._channel_handle, ctypes.byref(data), 1)
            if sent != 1:
                self._last_error = f"ZCAN_Transmit failed, sent={sent}"
                return False
            return True

    def receive(self, timeout_ms: int) -> CanFrame | None:
        with self._io_lock:
            if not self._is_open or not self._dll:
                self._last_error = "CAN device is not open"
                return None
            if self._can_fd:
                fd_buffer = (ZcanReceiveFdData * 1)()
                count = self._dll.ZCAN_ReceiveFD(self._channel_handle, ctypes.byref(fd_buffer), 1, timeout_ms)
                buffer = fd_buffer
            else:
                can_buffer = (ZcanReceiveData * 1)()
                count = self._dll.ZCAN_Receive(self._channel_handle, ctypes.byref(can_buffer), 1, timeout_ms)
                buffer = can_buffer
            if count == 0:
                return None
            if count == 0xFFFFFFFF:
                self._last_error = "ZCAN_Receive failed"
                return None
            if self._can_fd:
                return zcan_receive_fd_data_to_frame(buffer[0])
            return zcan_receive_data_to_frame(buffer[0])

    @property
    def last_error(self) -> str:
        return self._last_error

    def _load_dll(self) -> bool:
        if self._dll:
            return True
        if not self.dll_path:
            self._last_error = "未选择 ControlCANFD.dll"
            return False
        path = Path(self.dll_path)
        if not path.exists():
            self._last_error = f"DLL 不存在：{path}"
            return False
        # zlgcan.dll loads device backends from its sibling kerneldlls\ folder,
        # resolved relative to the process working directory, so cd into it.
        dll_dir = str(path.parent)
        try:
            if self._change_working_directory:
                os.chdir(dll_dir)
            if hasattr(os, "add_dll_directory"):
                self._dll_directory_handle = os.add_dll_directory(dll_dir)
        except OSError:
            pass
        try:
            self._dll = ctypes.WinDLL(str(path))
        except OSError as exc:
            self._last_error = f"DLL 加载失败：{exc}"
            return False
        return True

    def _resolve_functions(self) -> bool:
        assert self._dll is not None
        required = (
            "ZCAN_OpenDevice",
            "ZCAN_CloseDevice",
            "ZCAN_InitCAN",
            "ZCAN_StartCAN",
            "ZCAN_ResetCAN",
            "ZCAN_Transmit",
            "ZCAN_Receive",
        )
        if self._can_fd:
            required += ("ZCAN_TransmitFD", "ZCAN_ReceiveFD")
        missing = [name for name in required if not hasattr(self._dll, name)]
        if missing:
            self._last_error = "DLL 缺少函数：" + ", ".join(missing)
            return False

        self._dll.ZCAN_OpenDevice.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
        self._dll.ZCAN_OpenDevice.restype = ctypes.c_void_p
        self._dll.ZCAN_CloseDevice.argtypes = [ctypes.c_void_p]
        self._dll.ZCAN_CloseDevice.restype = ctypes.c_uint
        self._dll.ZCAN_InitCAN.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p]
        self._dll.ZCAN_InitCAN.restype = ctypes.c_void_p
        self._dll.ZCAN_StartCAN.argtypes = [ctypes.c_void_p]
        self._dll.ZCAN_StartCAN.restype = ctypes.c_uint
        self._dll.ZCAN_ResetCAN.argtypes = [ctypes.c_void_p]
        self._dll.ZCAN_ResetCAN.restype = ctypes.c_uint
        self._dll.ZCAN_Transmit.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
        self._dll.ZCAN_Transmit.restype = ctypes.c_uint
        self._dll.ZCAN_Receive.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_int]
        self._dll.ZCAN_Receive.restype = ctypes.c_uint
        if self._can_fd:
            if hasattr(self._dll, "ZCAN_SetValue"):
                self._dll.ZCAN_SetValue.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
                self._dll.ZCAN_SetValue.restype = ctypes.c_uint
            if hasattr(self._dll, "ZCAN_SetAbitBaud"):
                self._dll.ZCAN_SetAbitBaud.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
                self._dll.ZCAN_SetAbitBaud.restype = ctypes.c_uint
            if hasattr(self._dll, "ZCAN_SetDbitBaud"):
                self._dll.ZCAN_SetDbitBaud.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
                self._dll.ZCAN_SetDbitBaud.restype = ctypes.c_uint
            if hasattr(self._dll, "ZCAN_SetCANFDStandard"):
                self._dll.ZCAN_SetCANFDStandard.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
                self._dll.ZCAN_SetCANFDStandard.restype = ctypes.c_uint
            self._dll.ZCAN_TransmitFD.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
            self._dll.ZCAN_TransmitFD.restype = ctypes.c_uint
            self._dll.ZCAN_ReceiveFD.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_int]
            self._dll.ZCAN_ReceiveFD.restype = ctypes.c_uint
        return True


def timing_for_baudrate(baudrate: int) -> tuple[int, int]:
    """SJA1000 Timing0/Timing1 register pair for a classic-CAN baud rate (16MHz)."""
    timing = {
        1_000_000: (0x00, 0x14),
        500_000: (0x00, 0x1C),
        250_000: (0x01, 0x1C),
        125_000: (0x03, 0x1C),
        100_000: (0x04, 0x1C),
    }
    try:
        return timing[baudrate]
    except KeyError as exc:
        raise ValueError(f"unsupported baudrate: {baudrate}") from exc


def _candidate_device_types(requested_type: int) -> tuple[int, ...]:
    candidates = [requested_type, *DEVICE_TYPE_CANDIDATES]
    return tuple(dict.fromkeys(candidates))


def frame_to_zcan_transmit_data(frame: CanFrame) -> ZcanTransmitData:
    obj = ZcanTransmitData()
    obj.transmit_type = 0
    obj.frame.can_id = frame.id
    obj.frame.err = 0
    obj.frame.rtr = 1 if frame.remote else 0
    obj.frame.eff = 1 if frame.extended else 0
    obj.frame.can_dlc = frame.dlc
    for index, byte in enumerate(frame.data[: frame.dlc]):
        obj.frame.data[index] = byte
    return obj


def zcan_receive_data_to_frame(obj: ZcanReceiveData) -> CanFrame:
    data = bytes(obj.frame.data[: obj.frame.can_dlc])
    return CanFrame(id=obj.frame.can_id, data=data, extended=bool(obj.frame.eff), remote=bool(obj.frame.rtr))


def frame_to_zcan_transmit_fd_data(frame: CanFrame) -> ZcanTransmitFdData:
    if not frame.fd:
        raise ValueError("expected a CAN FD frame")
    obj = ZcanTransmitFdData()
    obj.transmit_type = 0
    obj.frame.can_id = frame.id
    obj.frame.err = 0
    obj.frame.rtr = 0
    obj.frame.eff = 1 if frame.extended else 0
    obj.frame.len = frame.dlc
    obj.frame.brs = 1 if frame.brs else 0
    obj.frame.esi = 1 if frame.esi else 0
    for index, byte in enumerate(frame.data[: frame.dlc]):
        obj.frame.data[index] = byte
    return obj


def zcan_receive_fd_data_to_frame(obj: ZcanReceiveFdData) -> CanFrame:
    data = bytes(obj.frame.data[: obj.frame.len])
    return CanFrame(
        id=obj.frame.can_id,
        data=data,
        extended=bool(obj.frame.eff),
        fd=True,
        brs=bool(obj.frame.brs),
        esi=bool(obj.frame.esi),
    )
