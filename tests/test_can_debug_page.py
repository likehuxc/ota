from __future__ import annotations

import csv
from pathlib import Path
import tempfile
import unittest

from PySide6.QtWidgets import QApplication

from d7_pmu_iap_tool.can.can_frame import CanFrame
from d7_pmu_iap_tool.can_debug_page import (
    CanDebugConfig,
    CanDebugPage,
    build_battery_frame,
    build_light_frame,
    xor_checksum,
)


class CanDebugProtocolTests(unittest.TestCase):
    def test_battery_frame_matches_mcu_uplink_format(self):
        frame = build_battery_frame(0x41, 0x02, 52_000)

        self.assertEqual(frame.id, 0x41)
        self.assertEqual(bytes(frame.data[:7]), bytes.fromhex("82 02 00 00 CB 20 01"))
        self.assertEqual(frame.data[7], xor_checksum(bytes(frame.data[:7])))

    def test_light_frame_matches_pd_can_0x90_layout(self):
        frame = build_light_frame(0x08, 200, 2, 3, 4, 500, (1, 2, 3))

        self.assertEqual(frame.id, 0x08)
        self.assertEqual(bytes(frame.data[:7]), bytes.fromhex("90 C8 02 03 04 01 F4"))
        self.assertEqual(frame.data[7], xor_checksum(bytes(frame.data[:7])))

    def test_rgb_mode_places_rgb_in_parameter_bytes(self):
        frame = build_light_frame(0x08, 203, 0, 5, 9, 900, (0x12, 0x34, 0x56))

        self.assertEqual(bytes(frame.data[:7]), bytes.fromhex("90 CB 00 05 12 34 56"))


class CanDebugPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.sent: list[CanFrame] = []
        self.opened: list[CanDebugConfig] = []
        self.closed = 0
        self.page = CanDebugPage(
            send_frame=self.sent.append,
            open_can=self.opened.append,
            close_can=self._close,
            default_canfd_dll=r"C:\Vendor\ControlCANFD.dll",
        )

    def tearDown(self):
        self.page.stop_battery_simulation()
        self.page.close()

    def _close(self):
        self.closed += 1

    def test_connection_config_is_forwarded(self):
        self.page.channel_input.setCurrentIndex(1)
        self.page.baudrate_input.setCurrentIndex(1)

        self.page.open_can()

        self.assertEqual(self.opened, [CanDebugConfig(
            dll_path=r"C:\Vendor\ControlCANFD.dll",
            device_type=41,
            channel=1,
            arbitration_baudrate=500_000,
            data_baudrate=5_000_000,
        )])

    def test_battery_cycle_sends_voltage_current_and_soc(self):
        self.page.set_connected(True, "已连接")
        self.page.battery_voltage_input.setValue(48.5)
        self.page.battery_current_input.setValue(-2.25)
        self.page.battery_capacity_input.setValue(65)

        self.page.start_battery_simulation()
        self.page.stop_battery_simulation()

        self.assertEqual(len(self.sent), 3)
        self.assertEqual({frame.id for frame in self.sent}, {0x41})
        self.assertEqual([frame.data[1] for frame in self.sent], [0x02, 0x0E, 0x81])
        self.assertEqual(int.from_bytes(self.sent[0].data[2:6], "big"), 48_500)
        self.assertEqual(int.from_bytes(self.sent[1].data[2:6], "big", signed=True), -2_250)
        self.assertEqual(int.from_bytes(self.sent[2].data[2:6], "big"), 65)

    def test_light_all_strips_sends_four_checked_frames(self):
        self.page.set_connected(True, "已连接")
        self.page.light_strip_input.setCurrentIndex(4)
        self.page.light_mode_input.setCurrentIndex(1)

        self.page.send_light_command()

        self.assertEqual(len(self.sent), 4)
        self.assertEqual([frame.data[1] for frame in self.sent], [200, 201, 202, 203])
        self.assertTrue(all(frame.data[7] == xor_checksum(bytes(frame.data[:7])) for frame in self.sent))

    def test_capture_filters_pauses_and_exports_complete_history(self):
        self.page.handle_frame("RX", CanFrame(id=0x41, data=b"\x01"))
        self.page.handle_frame("TX", CanFrame(id=0x08, data=b"\x02"))
        self.page.filter_input.setText("0x41")
        self.page.render_records()
        self.assertEqual(self.page.frame_table.rowCount(), 1)

        self.page.toggle_pause()
        self.page.handle_frame("RX", CanFrame(id=0x41, data=b"\x03"))
        self.assertEqual(self.page.frame_table.rowCount(), 1)
        self.page.toggle_pause()
        self.assertEqual(self.page.frame_table.rowCount(), 2)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.csv"
            count = self.page._save_records(path)
            rows = list(csv.reader(path.open(encoding="utf-8-sig")))

        self.assertEqual(count, 3)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0], ["sequence", "time", "direction", "can_id", "len", "data", "parsed"])

    def test_parser_interface_is_optional_and_injectable(self):
        self.page.parse_input.setChecked(True)
        self.assertEqual(self.page.parse_status.text(), "暂无解析器")
        self.page.set_parser(lambda direction, frame: f"{direction}:0x{frame.id:X}")

        self.page.handle_frame("RX", CanFrame(id=0x123, data=b"\x01"))

        self.assertEqual(self.page.frame_table.item(0, 6).text(), "RX:0x123")


if __name__ == "__main__":
    unittest.main()
