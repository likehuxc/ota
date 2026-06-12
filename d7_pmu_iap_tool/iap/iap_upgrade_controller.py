from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Event
from typing import Callable

from d7_pmu_iap_tool.can.can_frame import CanDriver, CanFrame
from d7_pmu_iap_tool.iap.firmware_image import FirmwareImage
from d7_pmu_iap_tool.iap.iap_protocol import (
    CMD_ENABLE_CAN,
    CMD_FILL_SEGMENT_DATA,
    CMD_GET_RUN_ROLE,
    CMD_JUMP_TO_APP,
    CMD_SET_FIRMWARE_SIZE,
    CMD_SET_SEGMENT_INFO,
    CMD_VALIDATE_SEGMENT_DATA,
    IapAck,
    IapProtocol,
    PROTOCOL_HEAD,
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
    pre_upgrade_wakeup_ms: int = 0
    disable_target_can_messages: bool = False
    data_frame_delay_ms: int = 2
    wait_data_frame_ack: bool = False
    data_frame_ack_timeout_ms: int = 200
    ignore_validate_ack_failure: bool = False
    set_firmware_retries: int = 1
    set_segment_retries: int = 2
    validate_segment_retries: int = 2


def build_upgrade_preview_frames(
    protocol: IapProtocol,
    image: FirmwareImage,
    options: UpgradeOptions | None = None,
    max_data_frames: int = 10,
) -> list[tuple[str, CanFrame]]:
    if max_data_frames < 0:
        raise ValueError("max_data_frames must be >= 0")

    opts = options or UpgradeOptions()
    frames: list[tuple[str, CanFrame]] = []
    if opts.pre_upgrade_wakeup_ms > 0:
        frames.append(("预唤醒查询角色 0x02", protocol.query_role()))

    frames.extend([
        ("查询当前角色 0x02", protocol.query_role()),
        ("软件复位进 BOOT 0x01", protocol.reboot_to_bootloader()),
        ("复位后查询角色 0x02", protocol.query_role()),
    ])
    if opts.disable_target_can_messages:
        frames.append(("关闭 CAN 消息发送 0x04", protocol.set_can_messages_enabled(False)))
    frames.append((f"升级相关信息 0x05 size={image.size}", protocol.set_firmware_size(image.size)))

    first_section = image.sections(1024)[0]
    frames.append((
        f"首段段信息 0x06 section={first_section.number} size={len(first_section.data)}",
        protocol.set_segment_info(section_num=first_section.number, section_size=len(first_section.data)),
    ))
    for index, frame in enumerate(protocol.segment_data_frames(first_section.data)[:max_data_frames], start=1):
        frames.append((f"首段自升级数据包 {CMD_FILL_SEGMENT_DATA:#04x} #{index}", frame))
    return frames


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
        self._last_ack_frame: CanFrame | None = None

    def cancel(self) -> None:
        self._cancel_event.set()

    def upgrade(self, image: FirmwareImage, options: UpgradeOptions | None = None) -> None:
        opts = options or UpgradeOptions()
        self._cancel_event.clear()
        self.on_progress(0)
        self._open_if_needed(opts)

        if opts.pre_upgrade_wakeup_ms > 0:
            self._send(self.protocol.query_role())
            self._sleep_with_cancel(opts.pre_upgrade_wakeup_ms)

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

        if opts.disable_target_can_messages:
            self._set_can_messages_enabled(False, opts)
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

    def _set_can_messages_enabled(self, enabled: bool, options: UpgradeOptions) -> None:
        self._retry_command(
            lambda: self.protocol.set_can_messages_enabled(enabled),
            CMD_ENABLE_CAN,
            options.ack_timeout_ms,
            options.set_segment_retries,
        )
        self._log(f"目标设备 CAN 消息发送已{'打开' if enabled else '关闭'}")

    def _send_segment(self, section_num: int, section_data: bytes, options: UpgradeOptions) -> None:
        segment_info_frame = self.protocol.set_segment_info(section_num=section_num, section_size=len(section_data))
        self._retry_command(
            lambda: segment_info_frame,
            CMD_SET_SEGMENT_INFO,
            options.ack_timeout_ms,
            options.set_segment_retries,
        )

        for frame_index, frame in enumerate(self.protocol.segment_data_frames(section_data), start=1):
            self._check_cancelled()
            if options.wait_data_frame_ack:
                try:
                    self._command_with_ack(
                        frame,
                        CMD_FILL_SEGMENT_DATA,
                        options.data_frame_ack_timeout_ms,
                        accept_tx_can_id_ack=False,
                    )
                except RuntimeError as exc:
                    raise RuntimeError(f"段 {section_num} 数据包 {frame_index} 等待 0x07 ACK 失败：{exc}") from exc
            else:
                self._send(frame)
            if options.data_frame_delay_ms > 0:
                time.sleep(options.data_frame_delay_ms / 1000)

        if self._retry_validate_segment(section_num, section_data, options):
            self._log(f"段 {section_num} 写入成功，size={len(section_data)}")
        else:
            self._log(f"段 {section_num} 已忽略校验 ACK 失败，继续发送后续段")

    def _retry_validate_segment(self, section_num: int, section_data: bytes, options: UpgradeOptions) -> bool:
        last_error = ""
        attempts = 1 if options.ignore_validate_ack_failure else options.validate_segment_retries + 1
        validate_timeout_ms = options.ack_timeout_ms if options.ignore_validate_ack_failure else options.write_timeout_ms
        for _ in range(attempts):
            self._check_cancelled()
            validate_frame = self.protocol.validate_segment(section_num, section_data)
            try:
                ack = self._command_with_ack(
                    validate_frame,
                    CMD_VALIDATE_SEGMENT_DATA,
                    validate_timeout_ms,
                )
            except RuntimeError as exc:
                last_error = str(exc)
                if options.ignore_validate_ack_failure:
                    self._log(f"忽略段 {section_num} 校验 ACK 错误，继续下一段：{last_error}")
                    return False
                continue
            if ack.params[0] == 1:
                return True
            self._log(
                f"段 {section_num} 写入失败 ACK：byte3={ack.params[0]}, "
                f"{self._format_last_ack_frame()}"
            )
            last_error = f"段 {section_num} 写入失败，ACK byte3={ack.params[0]}"
            if options.ignore_validate_ack_failure:
                self._log(f"忽略段 {section_num} 校验失败 ACK，继续下一段：{last_error}")
                return False
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

    def _command_with_ack(
        self,
        frame: CanFrame,
        expected_cmd: int,
        timeout_ms: int,
        accept_tx_can_id_ack: bool = True,
    ) -> IapAck:
        self._last_ack_frame = None
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
            ack_ids = (self.protocol.can_id, self.protocol.target_id) if accept_tx_can_id_ack else (self.protocol.target_id,)
            if ack_frame.id not in ack_ids:
                continue
            if not self._is_expected_ack_frame(ack_frame, expected_cmd):
                continue
            try:
                ack = self.protocol.parse_ack(ack_frame.data, expected_cmd=expected_cmd)
                self._last_ack_frame = ack_frame
                return ack
            except ValueError as exc:
                self._log(
                    f"ACK 解析失败：expected=0x{expected_cmd:02X}, "
                    f"RX ID=0x{ack_frame.id:X}, Len={ack_frame.dlc}, "
                    f"Data={self._format_frame_data(ack_frame)}，原因：{exc}"
                )
                raise RuntimeError(str(exc)) from exc

    def _is_expected_ack_frame(self, frame: CanFrame, expected_cmd: int) -> bool:
        data = frame.data
        return (
            len(data) >= 3
            and data[0] == PROTOCOL_HEAD
            and data[1] == self.protocol.target_id
            and data[2] == expected_cmd
        )

    def _format_last_ack_frame(self) -> str:
        if self._last_ack_frame is None:
            return "RX ACK 帧未记录"
        frame = self._last_ack_frame
        return f"RX {self._format_frame_summary(frame)}"

    def _format_frame_summary(self, frame: CanFrame) -> str:
        return f"ID=0x{frame.id:X}, Len={frame.dlc}, Data={self._format_frame_data(frame)}"

    @staticmethod
    def _format_frame_data(frame: CanFrame) -> str:
        return bytes(frame.data[: frame.dlc]).hex(" ").upper()

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
