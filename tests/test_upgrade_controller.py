import struct
import unittest

from d7_pmu_iap_tool.can.can_frame import CanFrame
from d7_pmu_iap_tool.iap.firmware_image import FirmwareImage
from d7_pmu_iap_tool.iap.iap_protocol import (
    CMD_FILL_SEGMENT_DATA,
    CMD_GET_RUN_ROLE,
    CMD_JUMP_TO_APP,
    CMD_SET_FIRMWARE_SIZE,
    CMD_SET_SEGMENT_INFO,
    CMD_VALIDATE_SEGMENT_DATA,
    IapProtocol,
    checksum8,
)
from d7_pmu_iap_tool.iap.iap_upgrade_controller import IapUpgradeController, UpgradeOptions


class FakeCanDriver:
    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []
        self._is_open = False
        self._last_error = ""

    def open(self, device_type, device_index, channel, baudrate):
        self._is_open = True
        return True

    def close(self):
        self._is_open = False

    def is_open(self):
        return self._is_open

    def send(self, frame):
        self.sent.append(frame)
        return True

    def receive(self, timeout_ms):
        if not self.replies:
            return None
        return CanFrame(id=0x7FF, data=self.replies.pop(0))

    @property
    def last_error(self):
        return self._last_error


def ack(command, params=b"\x00\x00\x00"):
    data = bytes([0x16, 0x19, command]) + params[:3].ljust(3, b"\x00") + bytes([0xE9])
    return data + bytes([checksum8(data)])


def segment_info_ack(params):
    data = bytes([0x16, 0x19, CMD_SET_SEGMENT_INFO]) + params
    return data + bytes([checksum8(data)])


def data_frame_acks(protocol, section_data):
    return [ack(CMD_FILL_SEGMENT_DATA) for _ in protocol.segment_data_frames(section_data)]


class UpgradeControllerTests(unittest.TestCase):
    def test_runs_full_upgrade_from_app_to_boot_to_jump(self):
        image = FirmwareImage.from_bytes(struct.pack("<II", 0x20001000, 0x000202C9) + bytes(range(256)) * 5)
        protocol = IapProtocol()
        sections = image.sections(1024)
        replies = [
            ack(CMD_GET_RUN_ROLE, b"\x00\x00\x00\x00"),
            ack(CMD_GET_RUN_ROLE, b"\x01\x00\x00\x00"),
            ack(CMD_SET_FIRMWARE_SIZE, bytes([image.size >> 16, (image.size >> 8) & 0xFF, image.size & 0xFF])),
            segment_info_ack(b"\x04\x00\x00\x00"),
            *data_frame_acks(protocol, sections[0].data),
            ack(CMD_VALIDATE_SEGMENT_DATA, b"\x01\x00\x00\x00"),
            segment_info_ack(b"\x01\x08\x00\x01"),
            *data_frame_acks(protocol, sections[1].data),
            ack(CMD_VALIDATE_SEGMENT_DATA, b"\x01\x00\x00\x00"),
            ack(CMD_GET_RUN_ROLE, b"\x00\x00\x00\x00"),
        ]
        driver = FakeCanDriver(replies)
        logs = []
        progress = []
        frame_events = []
        controller = IapUpgradeController(
            driver,
            protocol,
            on_log=logs.append,
            on_progress=progress.append,
            on_frame=lambda direction, frame: frame_events.append((direction, frame)),
        )

        controller.upgrade(
            image,
            UpgradeOptions(
                device_type=4,
                device_index=0,
                channel=0,
                baudrate=1_000_000,
                boot_wait_ms=0,
                app_start_wait_ms=0,
                data_frame_delay_ms=0,
            ),
        )

        sent_commands = [frame.data[2] for frame in driver.sent]
        self.assertEqual(sent_commands[:4], [CMD_GET_RUN_ROLE, 0x01, CMD_GET_RUN_ROLE, CMD_SET_FIRMWARE_SIZE])
        self.assertEqual(sent_commands.count(0x07), 258)
        self.assertEqual(sent_commands[-2:], [CMD_JUMP_TO_APP, CMD_GET_RUN_ROLE])
        self.assertEqual(progress[-1], 100)
        self.assertEqual([direction for direction, _ in frame_events].count("RX"), len(replies))
        self.assertIn(("TX", CMD_JUMP_TO_APP), [(direction, frame.data[2]) for direction, frame in frame_events])
        self.assertIn(("RX", CMD_FILL_SEGMENT_DATA), [(direction, frame.data[2]) for direction, frame in frame_events])
        self.assertTrue(any("当前角色 APP" in line for line in logs))

    def test_fails_when_segment_write_ack_is_not_success(self):
        image = FirmwareImage.from_bytes(struct.pack("<II", 0x20001000, 0x000202C9) + b"\x01\x02\x03\x04")
        replies = [
            ack(CMD_GET_RUN_ROLE, b"\x01\x00\x00\x00"),
            ack(CMD_SET_FIRMWARE_SIZE, bytes([0, 0, image.size])),
            segment_info_ack(b"\x00\x0c\x00\x00"),
            ack(CMD_FILL_SEGMENT_DATA),
            ack(CMD_FILL_SEGMENT_DATA),
            ack(CMD_FILL_SEGMENT_DATA),
            ack(CMD_VALIDATE_SEGMENT_DATA, b"\x00\x00\x00\x00"),
        ]
        controller = IapUpgradeController(FakeCanDriver(replies), IapProtocol())

        with self.assertRaises(RuntimeError):
            controller.upgrade(image, UpgradeOptions(data_frame_delay_ms=0))

    def test_retries_validate_segment_when_ack_reports_failure(self):
        image = FirmwareImage.from_bytes(struct.pack("<II", 0x20001000, 0x000202C9) + b"\x01\x02\x03\x04")
        replies = [
            ack(CMD_GET_RUN_ROLE, b"\x01\x00\x00\x00"),
            ack(CMD_SET_FIRMWARE_SIZE, bytes([0, 0, image.size])),
            segment_info_ack(b"\x00\x0c\x00\x00"),
            ack(CMD_FILL_SEGMENT_DATA),
            ack(CMD_FILL_SEGMENT_DATA),
            ack(CMD_FILL_SEGMENT_DATA),
            ack(CMD_VALIDATE_SEGMENT_DATA, b"\x00\x00\x00\x00"),
            ack(CMD_VALIDATE_SEGMENT_DATA, b"\x01\x00\x00\x00"),
            ack(CMD_GET_RUN_ROLE, b"\x00\x00\x00\x00"),
        ]
        driver = FakeCanDriver(replies)
        controller = IapUpgradeController(driver, IapProtocol())

        controller.upgrade(image, UpgradeOptions(app_start_wait_ms=0, data_frame_delay_ms=0))

        self.assertEqual([frame.data[2] for frame in driver.sent].count(CMD_VALIDATE_SEGMENT_DATA), 2)

    def test_fails_when_app_role_does_not_respond_after_jump(self):
        image = FirmwareImage.from_bytes(struct.pack("<II", 0x20001000, 0x000202C9) + b"\x01\x02\x03\x04")
        replies = [
            ack(CMD_GET_RUN_ROLE, b"\x01\x00\x00\x00"),
            ack(CMD_SET_FIRMWARE_SIZE, bytes([0, 0, image.size])),
            segment_info_ack(b"\x00\x0c\x00\x00"),
            ack(CMD_FILL_SEGMENT_DATA),
            ack(CMD_FILL_SEGMENT_DATA),
            ack(CMD_FILL_SEGMENT_DATA),
            ack(CMD_VALIDATE_SEGMENT_DATA, b"\x01\x00\x00\x00"),
        ]
        driver = FakeCanDriver(replies)
        controller = IapUpgradeController(driver, IapProtocol())

        with self.assertRaisesRegex(RuntimeError, "等待 APP 启动超时"):
            controller.upgrade(
                image,
                UpgradeOptions(app_start_wait_ms=0, app_total_wait_ms=1, data_frame_delay_ms=0),
            )

        self.assertEqual([frame.data[2] for frame in driver.sent][-2:], [CMD_JUMP_TO_APP, CMD_GET_RUN_ROLE])


if __name__ == "__main__":
    unittest.main()
