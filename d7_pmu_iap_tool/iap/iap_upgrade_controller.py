from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Event
from typing import Callable

from d7_pmu_iap_tool.can.can_frame import CanDriver, CanFrame
from d7_pmu_iap_tool.iap.firmware_image import FirmwareImage
from d7_pmu_iap_tool.iap.iap_protocol import (
    CMD_FILL_SEGMENT_DATA,
    CMD_GET_RUN_ROLE,
    CMD_JUMP_TO_APP,
    CMD_SET_FIRMWARE_SIZE,
    CMD_SET_SEGMENT_INFO,
    CMD_VALIDATE_SEGMENT_DATA,
    IapAck,
    IapProtocol,
    RUN_ROLE_APP,
    RUN_ROLE_BOOTLOADER,
)

LogCallback = Callable[[str], None]
ProgressCallback = Callable[[int], None]
FrameCallback = Callable[[str, CanFrame], None]


@dataclass(frozen=True)
class UpgradeOptions:
    device_type: int = 3
    device_index: int = 0
    channel: int = 0
    baudrate: int = 1_000_000
    ack_timeout_ms: int = 500
    erase_timeout_ms: int = 3_000
    write_timeout_ms: int = 3_000
    boot_wait_ms: int = 3_000
    boot_total_wait_ms: int = 10_000
    app_start_wait_ms: int = 1_000
    app_total_wait_ms: int = 5_000
    data_frame_delay_ms: int = 2
    set_firmware_retries: int = 1
    set_segment_retries: int = 2
    validate_segment_retries: int = 2


class IapUpgradeController:
    def __init__(
        self,
        driver: CanDriver,
        protocol: IapProtocol | None = None,
        on_log: LogCallback | None = None,
        on_progress: ProgressCallback | None = None,
        on_frame: FrameCallback | None = None,
    ) -> None:
        self.driver = driver
        self.protocol = protocol or IapProtocol()
        self.on_log = on_log or (lambda message: None)
        self.on_progress = on_progress or (lambda percent: None)
        self.on_frame = on_frame or (lambda direction, frame: None)
        self._cancel_event = Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def upgrade(self, image: FirmwareImage, options: UpgradeOptions | None = None) -> None:
        opts = options or UpgradeOptions()
        self._cancel_event.clear()
        self.on_progress(0)
        self._open_if_needed(opts)

        for warning in image.warnings:
            self._log(f"警告：{warning}")

        role = self.query_role(opts.ack_timeout_ms)
        if role == RUN_ROLE_APP:
            self._log("当前角色 APP，准备重启进 BOOT")
            self._send(self.protocol.reboot_to_bootloader())
            self._sleep_with_cancel(opts.boot_wait_ms)
            role = self._wait_for_boot(opts)
        else:
            self._log("当前角色 BOOT")

        if role != RUN_ROLE_BOOTLOADER:
            raise RuntimeError("设备未进入 BOOT，停止升级")

        self._set_firmware_size(image, opts)
        sent_bytes = 0
        sections = image.sections(1024)
        self._log(f"固件大小 {image.size} bytes，共 {len(sections)} 段")

        for section in sections:
            self._check_cancelled()
            self._send_segment(section.number, section.data, opts)
            sent_bytes += len(section.data)
            percent = min(99, int(sent_bytes * 100 / image.size))
            self.on_progress(percent)

        self._send(self.protocol.jump_to_app())
        self._sleep_with_cancel(opts.app_start_wait_ms)
        role = self._wait_for_app(opts)
        if role != RUN_ROLE_APP:
            raise RuntimeError("等待 APP 启动超时")
        self.on_progress(100)
        self._log("确认已跳转到 APP")

    def query_role(self, timeout_ms: int = 500) -> str:
        ack = self._command_with_ack(self.protocol.query_role(), CMD_GET_RUN_ROLE, timeout_ms)
        role = self.protocol.ack_role(ack)
        self._log(f"当前角色 {role}")
        return role

    def _open_if_needed(self, options: UpgradeOptions) -> None:
        if self.driver.is_open():
            return
        if not self.driver.open(options.device_type, options.device_index, options.channel, options.baudrate):
            raise RuntimeError(f"打开 CAN 设备失败：{self.driver.last_error}")
        self._log(f"CAN 已打开：deviceType={options.device_type}, index={options.device_index}, channel={options.channel}, baudrate={options.baudrate}")

    def _wait_for_boot(self, options: UpgradeOptions) -> str:
        deadline = time.monotonic() + options.boot_total_wait_ms / 1000
        role = RUN_ROLE_APP
        while time.monotonic() < deadline:
            self._check_cancelled()
            try:
                role = self.query_role(options.ack_timeout_ms)
            except RuntimeError:
                time.sleep(0.2)
                continue
            if role == RUN_ROLE_BOOTLOADER:
                return role
            time.sleep(0.2)
        return role

    def _wait_for_app(self, options: UpgradeOptions) -> str:
        deadline = time.monotonic() + options.app_total_wait_ms / 1000
        role = RUN_ROLE_BOOTLOADER
        while time.monotonic() < deadline:
            self._check_cancelled()
            try:
                role = self.query_role(options.ack_timeout_ms)
            except RuntimeError:
                time.sleep(0.2)
                continue
            if role == RUN_ROLE_APP:
                return role
            time.sleep(0.2)
        return role

    def _set_firmware_size(self, image: FirmwareImage, options: UpgradeOptions) -> None:
        self._retry_command(
            lambda: self.protocol.set_firmware_size(image.size),
            CMD_SET_FIRMWARE_SIZE,
            options.erase_timeout_ms,
            options.set_firmware_retries,
        )
        self._log("APP 区擦除/固件大小设置完成")

    def _send_segment(self, section_num: int, section_data: bytes, options: UpgradeOptions) -> None:
        self._retry_command(
            lambda: self.protocol.set_segment_info(section_num=section_num, section_size=len(section_data)),
            CMD_SET_SEGMENT_INFO,
            options.ack_timeout_ms,
            options.set_segment_retries,
        )

        for frame in self.protocol.segment_data_frames(section_data):
            self._check_cancelled()
            self._command_with_ack(frame, CMD_FILL_SEGMENT_DATA, options.ack_timeout_ms)
            if options.data_frame_delay_ms > 0:
                time.sleep(options.data_frame_delay_ms / 1000)

        self._retry_validate_segment(section_num, section_data, options)
        self._log(f"段 {section_num} 写入成功，size={len(section_data)}")

    def _retry_validate_segment(self, section_num: int, section_data: bytes, options: UpgradeOptions) -> None:
        last_error = ""
        for _ in range(options.validate_segment_retries + 1):
            self._check_cancelled()
            try:
                ack = self._command_with_ack(
                    self.protocol.validate_segment(section_num, section_data),
                    CMD_VALIDATE_SEGMENT_DATA,
                    options.write_timeout_ms,
                )
            except RuntimeError as exc:
                last_error = str(exc)
                continue
            if ack.params[0] == 1:
                return
            last_error = f"段 {section_num} 写入失败，ACK byte3={ack.params[0]}"
        raise RuntimeError(last_error)

    def _retry_command(
        self,
        frame_factory: Callable[[], CanFrame],
        expected_cmd: int,
        timeout_ms: int,
        retries: int,
    ) -> IapAck:
        last_error: Exception | None = None
        for _ in range(retries + 1):
            self._check_cancelled()
            try:
                return self._command_with_ack(frame_factory(), expected_cmd, timeout_ms)
            except RuntimeError as exc:
                last_error = exc
        raise RuntimeError(str(last_error) if last_error else "命令重试失败")

    def _command_with_ack(self, frame: CanFrame, expected_cmd: int, timeout_ms: int) -> IapAck:
        self._send(frame)
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                raise RuntimeError(f"等待 0x{expected_cmd:02X} ACK 超时")
            ack_frame = self.driver.receive(remaining_ms)
            if ack_frame is None:
                raise RuntimeError(f"等待 0x{expected_cmd:02X} ACK 超时")
            self.on_frame("RX", ack_frame)
            if ack_frame.id != self.protocol.can_id:
                continue
            try:
                return self.protocol.parse_ack(ack_frame.data, expected_cmd=expected_cmd)
            except ValueError as exc:
                raise RuntimeError(str(exc)) from exc

    def _send(self, frame: CanFrame) -> None:
        self._check_cancelled()
        if not self.driver.send(frame):
            raise RuntimeError(f"发送 CAN 帧失败：{self.driver.last_error}")
        self.on_frame("TX", frame)

    def _check_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise RuntimeError("用户已停止升级")

    def _log(self, message: str) -> None:
        self.on_log(message)

    def _sleep_with_cancel(self, duration_ms: int) -> None:
        deadline = time.monotonic() + duration_ms / 1000
        while time.monotonic() < deadline:
            self._check_cancelled()
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
