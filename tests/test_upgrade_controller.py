import struct
import unittest

from d7_pmu_iap_tool.can.can_frame import CanFrame
from d7_pmu_iap_tool.iap.firmware_image import FirmwareImage
from d7_pmu_iap_tool.iap.iap_protocol import (
    CMD_ENABLE_CAN,
    CMD_FILL_SEGMENT_DATA,
    CMD_GET_RUN_ROLE,
    CMD_GET_SOFT_VERSION,
    CMD_JUMP_TO_APP,
    CMD_SET_FIRMWARE_SIZE,
    CMD_SET_SEGMENT_INFO,
    CMD_VALIDATE_SEGMENT_DATA,
    IapProtocol,
    checksum8,
)
from d7_pmu_iap_tool.iap.iap_upgrade_controller import IapUpgradeController, UpgradeOptions
from d7_pmu_iap_tool.iap.iap_upgrade_controller import build_upgrade_preview_frames


class FakeCanDriver:
    def __init__(self, replies, reply_id=0x7FF):
        self.replies = list(replies)
        self.reply_id = reply_id
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
        reply = self.replies.pop(0)
        if isinstance(reply, CanFrame):
            return reply
        return CanFrame(id=self.reply_id, data=reply)

    @property
    def last_error(self):
        return self._last_error


def ack(command, params=b"\x00\x00\x00", target_id=0x19):
    data = bytes([0x16, target_id, command]) + params[:3].ljust(3, b"\x00") + bytes([0xE9])
    return data + bytes([checksum8(data)])


def segment_info_ack(params, target_id=0x19):
    data = bytes([0x16, target_id, CMD_SET_SEGMENT_INFO]) + params
    return data + bytes([checksum8(data)])


def validate_failure_ack(target_id=0x19):
    data = bytes([0x16, target_id, CMD_VALIDATE_SEGMENT_DATA, 0, 0x21, 0x23, 0x80])
    return data + bytes([checksum8(data)])


class UpgradeControllerTests(unittest.TestCase):
    def test_builds_battery_upgrade_preview_frames_without_waiting_for_ack(self):
        image = FirmwareImage.from_bytes(struct.pack("<II", 0x20001000, 0x000202C9) + bytes(range(64)))
        protocol = IapProtocol(target_id=0x41, can_id=0x7FF)

        preview_frames = build_upgrade_preview_frames(
            protocol,
            image,
            UpgradeOptions(pre_upgrade_wakeup_ms=1000, disable_target_can_messages=True),
            max_data_frames=10,
        )

        commands = [frame.data[2] for _, frame in preview_frames]
        self.assertEqual(
            commands,
            [
                CMD_GET_RUN_ROLE,
                CMD_GET_RUN_ROLE,
                0x01,
                CMD_GET_RUN_ROLE,
                CMD_ENABLE_CAN,
                CMD_SET_FIRMWARE_SIZE,
                CMD_SET_SEGMENT_INFO,
            ]
            + [CMD_FILL_SEGMENT_DATA] * 10,
        )
        self.assertEqual(bytes(preview_frames[0][1].data), bytes([0x16, 0x41, CMD_GET_RUN_ROLE, 0, 0, 0, 0xE9, 0x42]))
        self.assertEqual(bytes(preview_frames[4][1].data), bytes([0x16, 0x41, CMD_ENABLE_CAN, 0, 0, 0, 0xE9, 0x44]))
        self.assertEqual(bytes(preview_frames[6][1].data), bytes([0x16, 0x41, CMD_SET_SEGMENT_INFO, 0, image.size, 0, 0, 0xA5]))
        self.assertTrue(all("自升级数据包" in label for label, _ in preview_frames[-10:]))

    def test_regular_upgrade_preview_does_not_disable_can_messages(self):
        image = FirmwareImage.from_bytes(struct.pack("<II", 0x20001000, 0x000202C9) + bytes(range(64)))
        protocol = IapProtocol(target_id=0x19, can_id=0x7FF)

        preview_frames = build_upgrade_preview_frames(protocol, image, UpgradeOptions(), max_data_frames=1)

        self.assertNotIn(CMD_ENABLE_CAN, [frame.data[2] for _, frame in preview_frames])

    def test_accepts_ack_from_each_target_can_id(self):
        for target_id in (0x18, 0x19, 0x41):
            with self.subTest(target_id=target_id):
                protocol = IapProtocol(target_id=target_id, can_id=0x7FF)
                driver = FakeCanDriver(
                    [ack(CMD_GET_RUN_ROLE, b"\x00\x00\x00\x00", target_id=target_id)],
                    reply_id=target_id,
                )
                logs = []
                controller = IapUpgradeController(driver, protocol, on_log=logs.append)

                role = controller.query_role()

                self.assertEqual(role, "APP")
                self.assertEqual(driver.sent[0].id, 0x7FF)
                self.assertTrue(any("当前角色 APP" in line for line in logs))

    def test_logs_ack_frame_when_parse_fails(self):
        driver = FakeCanDriver([bytes([0x16, 0x19, CMD_GET_RUN_ROLE, 1, 0, 0, 0, 0x32])])
        logs = []
        controller = IapUpgradeController(driver, IapProtocol(), on_log=logs.append)

        with self.assertRaisesRegex(RuntimeError, "ACK frame tail mismatch"):
            controller.query_role()

        self.assertTrue(any("ACK 解析失败" in line for line in logs))
        self.assertTrue(any("RX ID=0x7FF" in line for line in logs))
        self.assertTrue(any("Data=16 19 02 01 00 00 00 32" in line for line in logs))

    def test_queries_and_logs_software_version(self):
        driver = FakeCanDriver([ack(CMD_GET_SOFT_VERSION, b"\x01\x0C\x03")])
        logs = []
        controller = IapUpgradeController(driver, IapProtocol(), on_log=logs.append)

        version = controller.query_software_version()

        self.assertEqual(version, "1.12.3")
        self.assertEqual(driver.sent[0].data[2], CMD_GET_SOFT_VERSION)
        self.assertIn("当前软件版本 1.12.3", logs)

    def test_runs_full_upgrade_from_app_to_boot_to_jump(self):
        image = FirmwareImage.from_bytes(struct.pack("<II", 0x20001000, 0x000202C9) + bytes(range(256)) * 5)
        protocol = IapProtocol()
        replies = [
            ack(CMD_GET_RUN_ROLE, b"\x00\x00\x00\x00"),
            ack(CMD_GET_RUN_ROLE, b"\x01\x00\x00\x00"),
            ack(CMD_SET_FIRMWARE_SIZE, bytes([image.size >> 16, (image.size >> 8) & 0xFF, image.size & 0xFF])),
            segment_info_ack(b"\x04\x00\x00\x00"),
            ack(CMD_VALIDATE_SEGMENT_DATA, b"\x01\x00\x00\x00"),
            segment_info_ack(b"\x01\x08\x00\x01"),
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
        self.assertNotIn(("RX", CMD_FILL_SEGMENT_DATA), [(direction, frame.data[2]) for direction, frame in frame_events])
        self.assertTrue(any("当前角色 APP" in line for line in logs))

    def test_firmware_data_frames_do_not_wait_for_ack(self):
        image = FirmwareImage.from_bytes(struct.pack("<II", 0x20001000, 0x000202C9) + b"\x01\x02\x03\x04")
        replies = [
            ack(CMD_GET_RUN_ROLE, b"\x01\x00\x00\x00"),
            ack(CMD_SET_FIRMWARE_SIZE, bytes([0, 0, image.size])),
            segment_info_ack(b"\x00\x0c\x00\x00"),
            ack(CMD_VALIDATE_SEGMENT_DATA, b"\x01\x00\x00\x00"),
            ack(CMD_GET_RUN_ROLE, b"\x00\x00\x00\x00"),
        ]
        driver = FakeCanDriver(replies)
        controller = IapUpgradeController(driver, IapProtocol())

        controller.upgrade(image, UpgradeOptions(app_start_wait_ms=0, data_frame_delay_ms=0))

        sent_commands = [frame.data[2] for frame in driver.sent]
        self.assertEqual(sent_commands.count(CMD_FILL_SEGMENT_DATA), 3)
        self.assertEqual(driver.replies, [])

    def test_firmware_data_frames_can_wait_for_ack(self):
        image = FirmwareImage.from_bytes(struct.pack("<II", 0x20001000, 0x000202C9) + b"\x01\x02\x03\x04")
        protocol = IapProtocol()
        echoed_tx_frame = CanFrame(id=protocol.can_id, data=protocol.segment_data_frames(image.data)[0].data)
        replies = [
            ack(CMD_GET_RUN_ROLE, b"\x01\x00\x00\x00"),
            ack(CMD_SET_FIRMWARE_SIZE, bytes([0, 0, image.size])),
            segment_info_ack(b"\x00\x0c\x00\x00"),
            echoed_tx_frame,
            CanFrame(id=protocol.target_id, data=ack(CMD_FILL_SEGMENT_DATA)),
            CanFrame(id=protocol.target_id, data=ack(CMD_FILL_SEGMENT_DATA)),
            CanFrame(id=protocol.target_id, data=ack(CMD_FILL_SEGMENT_DATA)),
            ack(CMD_VALIDATE_SEGMENT_DATA, b"\x01\x00\x00\x00"),
            ack(CMD_GET_RUN_ROLE, b"\x00\x00\x00\x00"),
        ]
        driver = FakeCanDriver(replies)
        controller = IapUpgradeController(driver, protocol)

        controller.upgrade(
            image,
            UpgradeOptions(app_start_wait_ms=0, data_frame_delay_ms=0, wait_data_frame_ack=True),
        )

        sent_commands = [frame.data[2] for frame in driver.sent]
        self.assertEqual(sent_commands.count(CMD_FILL_SEGMENT_DATA), 3)
        self.assertEqual(driver.replies, [])

    def test_logs_segment_info_and_crc_frames(self):
        image = FirmwareImage.from_bytes(struct.pack("<II", 0x20001000, 0x000202C9) + b"\x01\x02\x03\x04")
        protocol = IapProtocol()
        expected_crc = int.from_bytes(protocol.validate_segment(0, image.data).data[4:8], "big")
        replies = [
            ack(CMD_GET_RUN_ROLE, b"\x01\x00\x00\x00"),
            ack(CMD_SET_FIRMWARE_SIZE, bytes([0, 0, image.size])),
            segment_info_ack(b"\x00\x0c\x00\x00"),
            ack(CMD_VALIDATE_SEGMENT_DATA, b"\x01\x00\x00\x00"),
            ack(CMD_GET_RUN_ROLE, b"\x00\x00\x00\x00"),
        ]
        tx_frames = []
        controller = IapUpgradeController(
            FakeCanDriver(replies),
            protocol,
            on_frame=lambda direction, frame: tx_frames.append(frame) if direction == "TX" else None,
        )

        controller.upgrade(image, UpgradeOptions(app_start_wait_ms=0, data_frame_delay_ms=0))

        sent = [bytes(frame.data[: frame.dlc]) for frame in tx_frames]
        self.assertIn(bytes.fromhex("16 19 06 00 0C 00 00 41".replace(" ", "")), sent)
        self.assertIn(bytes.fromhex("16 19 07 00 10 00 20 C9".replace(" ", "")), sent)
        self.assertIn(bytes.fromhex("16 19 07 03 04 00 00 00".replace(" ", "")), sent)
        validate_tx = [f for f in tx_frames if f.data[2] == CMD_VALIDATE_SEGMENT_DATA]
        self.assertTrue(validate_tx)
        self.assertEqual(int.from_bytes(validate_tx[0].data[4:8], "big"), expected_crc)

    def test_ignores_echoed_data_frames_while_waiting_for_validate_ack(self):
        image = FirmwareImage.from_bytes(struct.pack("<II", 0x20001000, 0x000202C9) + b"\x01\x02\x03\x04")
        echoed_data_frame = bytes([0x16, 0x19, CMD_FILL_SEGMENT_DATA, 0, 0, 0, 0xE9, 0x1F])
        replies = [
            ack(CMD_GET_RUN_ROLE, b"\x01\x00\x00\x00"),
            ack(CMD_SET_FIRMWARE_SIZE, bytes([0, 0, image.size])),
            segment_info_ack(b"\x00\x0c\x00\x00"),
            echoed_data_frame,
            ack(CMD_VALIDATE_SEGMENT_DATA, b"\x01\x00\x00\x00"),
            ack(CMD_GET_RUN_ROLE, b"\x00\x00\x00\x00"),
        ]
        driver = FakeCanDriver(replies)
        controller = IapUpgradeController(driver, IapProtocol())

        controller.upgrade(image, UpgradeOptions(app_start_wait_ms=0, data_frame_delay_ms=0))

        self.assertEqual(driver.replies, [])

    def test_fails_when_segment_validate_ack_is_not_success(self):
        image = FirmwareImage.from_bytes(struct.pack("<II", 0x20001000, 0x000202C9) + b"\x01\x02\x03\x04")
        replies = [
            ack(CMD_GET_RUN_ROLE, b"\x01\x00\x00\x00"),
            ack(CMD_SET_FIRMWARE_SIZE, bytes([0, 0, image.size])),
            segment_info_ack(b"\x00\x0c\x00\x00"),
            validate_failure_ack(),
            validate_failure_ack(),
            validate_failure_ack(),
        ]
        logs = []
        controller = IapUpgradeController(FakeCanDriver(replies), IapProtocol(), on_log=logs.append)

        with self.assertRaisesRegex(RuntimeError, "段 0 写入失败，ACK byte3=0"):
            controller.upgrade(image, UpgradeOptions(data_frame_delay_ms=0))

        self.assertTrue(any("段 0 写入失败 ACK：byte3=0" in line for line in logs))
        self.assertTrue(any("RX ID=0x7FF" in line for line in logs))
        self.assertTrue(any("Data=16 19 08 00 21 23 80 FB" in line for line in logs))

    def test_retries_validate_segment_when_ack_reports_failure(self):
        image = FirmwareImage.from_bytes(struct.pack("<II", 0x20001000, 0x000202C9) + b"\x01\x02\x03\x04")
        replies = [
            ack(CMD_GET_RUN_ROLE, b"\x01\x00\x00\x00"),
            ack(CMD_SET_FIRMWARE_SIZE, bytes([0, 0, image.size])),
            segment_info_ack(b"\x00\x0c\x00\x00"),
            ack(CMD_VALIDATE_SEGMENT_DATA, b"\x00\x00\x00\x00"),
            ack(CMD_VALIDATE_SEGMENT_DATA, b"\x01\x00\x00\x00"),
            ack(CMD_GET_RUN_ROLE, b"\x00\x00\x00\x00"),
        ]
        driver = FakeCanDriver(replies)
        controller = IapUpgradeController(driver, IapProtocol())

        controller.upgrade(image, UpgradeOptions(app_start_wait_ms=0, data_frame_delay_ms=0))

        self.assertEqual([frame.data[2] for frame in driver.sent].count(CMD_VALIDATE_SEGMENT_DATA), 2)

    def test_can_ignore_segment_validate_failure_and_continue_to_jump(self):
        image = FirmwareImage.from_bytes(struct.pack("<II", 0x20001000, 0x000202C9) + bytes(range(256)) * 5)
        replies = [
            ack(CMD_GET_RUN_ROLE, b"\x01\x00\x00\x00"),
            ack(CMD_SET_FIRMWARE_SIZE, bytes([image.size >> 16, (image.size >> 8) & 0xFF, image.size & 0xFF])),
            segment_info_ack(b"\x04\x00\x00\x00"),
            validate_failure_ack(),
            segment_info_ack(b"\x01\x08\x00\x01"),
            validate_failure_ack(),
            ack(CMD_GET_RUN_ROLE, b"\x00\x00\x00\x00"),
        ]
        logs = []
        driver = FakeCanDriver(replies)
        controller = IapUpgradeController(driver, IapProtocol(), on_log=logs.append)

        controller.upgrade(
            image,
            UpgradeOptions(
                app_start_wait_ms=0,
                data_frame_delay_ms=0,
                ignore_validate_ack_failure=True,
            ),
        )

        sent_commands = [frame.data[2] for frame in driver.sent]
        self.assertEqual(sent_commands.count(CMD_SET_SEGMENT_INFO), 2)
        self.assertEqual(sent_commands.count(CMD_VALIDATE_SEGMENT_DATA), 2)
        self.assertEqual(sent_commands[-2:], [CMD_JUMP_TO_APP, CMD_GET_RUN_ROLE])
        self.assertTrue(any("忽略段 0 校验失败 ACK" in line for line in logs))
        self.assertTrue(any("段 1 已忽略校验 ACK 失败，继续发送后续段" in line for line in logs))

    def test_fails_when_app_role_does_not_respond_after_jump(self):
        image = FirmwareImage.from_bytes(struct.pack("<II", 0x20001000, 0x000202C9) + b"\x01\x02\x03\x04")
        replies = [
            ack(CMD_GET_RUN_ROLE, b"\x01\x00\x00\x00"),
            ack(CMD_SET_FIRMWARE_SIZE, bytes([0, 0, image.size])),
            segment_info_ack(b"\x00\x0c\x00\x00"),
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

    def test_peicheng_battery_can_send_wakeup_query_before_upgrade_flow(self):
        image = FirmwareImage.from_bytes(struct.pack("<II", 0x20001000, 0x000202C9) + b"\x01\x02\x03\x04")
        protocol = IapProtocol(target_id=0x41, can_id=0x7FF)
        replies = [
            ack(CMD_GET_RUN_ROLE, b"\x01\x00\x00\x00", target_id=0x41),
            ack(CMD_ENABLE_CAN, b"\x00\x00\x00\x00", target_id=0x41),
            ack(CMD_SET_FIRMWARE_SIZE, bytes([0, 0, image.size]), target_id=0x41),
            segment_info_ack(b"\x00\x0c\x00\x00", target_id=0x41),
            ack(CMD_VALIDATE_SEGMENT_DATA, b"\x01\x00\x00\x00", target_id=0x41),
            ack(CMD_GET_RUN_ROLE, b"\x00\x00\x00\x00", target_id=0x41),
        ]
        driver = FakeCanDriver(replies)
        controller = IapUpgradeController(driver, protocol)
        sleeps = []
        controller._sleep_with_cancel = sleeps.append

        controller.upgrade(
            image,
            UpgradeOptions(
                pre_upgrade_wakeup_ms=1000,
                disable_target_can_messages=True,
                app_start_wait_ms=0,
                data_frame_delay_ms=0,
            ),
        )

        self.assertEqual([frame.data[2] for frame in driver.sent[:4]], [
            CMD_GET_RUN_ROLE,
            CMD_GET_RUN_ROLE,
            CMD_ENABLE_CAN,
            CMD_SET_FIRMWARE_SIZE,
        ])
        self.assertEqual(bytes(driver.sent[0].data), bytes([0x16, 0x41, CMD_GET_RUN_ROLE, 0, 0, 0, 0xE9, 0x42]))
        self.assertEqual(bytes(driver.sent[2].data), bytes([0x16, 0x41, CMD_ENABLE_CAN, 0, 0, 0, 0xE9, 0x44]))
        self.assertEqual(sleeps[0], 1000)


if __name__ == "__main__":
    unittest.main()
