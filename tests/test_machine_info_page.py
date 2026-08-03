from __future__ import annotations

import unittest

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from d7_pmu_iap_tool.can.can_frame import CanFrame
from d7_pmu_iap_tool.machine_info_page import MachineInfoCanConfig, MachineInfoPage


class MachineInfoPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.sent: list[CanFrame] = []
        self.opened: list[MachineInfoCanConfig] = []
        self.closed = 0
        self.page = MachineInfoPage(
            send_frame=self.sent.append,
            open_can=self.opened.append,
            close_can=self._close,
            default_canfd_dll=r"C:\Vendor\ControlCANFD.dll",
        )

    def tearDown(self):
        self.page.close()

    def _close(self):
        self.closed += 1

    def _row(self, key: str) -> int:
        for row in range(self.page.table.rowCount()):
            if self.page.table.item(row, 2).text() == key:
                return row
        self.fail(f"未找到 MachineInfo 字段：{key}")

    def test_connection_config_is_forwarded(self):
        self.page.channel_input.setCurrentIndex(1)
        self.page.baudrate_input.setCurrentIndex(1)

        self.page.open_can()

        self.assertEqual(self.opened, [MachineInfoCanConfig(
            dll_path=r"C:\Vendor\ControlCANFD.dll",
            device_type=41,
            channel=1,
            arbitration_baudrate=500_000,
            data_baudrate=5_000_000,
        )])

    def test_read_sends_each_selected_slot_once(self):
        self.page.set_connected(True, "已连接")
        for key in ("wheel_diameter", "esp32_type", "machine_type"):
            row = self._row(key)
            self.page.table.item(row, 0).setCheckState(Qt.CheckState.Checked)

        self.page.read_selected()

        self.assertEqual(len(self.sent), 2)
        self.assertEqual({frame.data[2] for frame in self.sent}, {0, 23})
        self.assertTrue(all(bytes(frame.data[:2]) == b"\x00\x53" for frame in self.sent))
        self.assertEqual(self.page.status_label.text(), "已发送 2 个 slot，等待响应")

    def test_response_updates_value_raw_data_and_status(self):
        self.page.set_connected(True, "已连接")
        row = self._row("wheel_diameter")
        self.page.table.item(row, 0).setCheckState(Qt.CheckState.Checked)
        self.page.read_selected()

        self.page.handle_frame("RX", CanFrame(id=0x08, data=bytes.fromhex("53 00 3E 0F 5C 29 02 15")))

        self.assertEqual(self.page.table.item(row, 4).text(), "0.14")
        self.assertEqual(self.page.table.item(row, 5).text(), "3E 0F 5C 29")
        self.assertEqual(self.page.table.item(row, 6).text(), "已读取")
        self.assertEqual(self.page.status_label.text(), "读取完成 · 1/1 slot")

    def test_partial_send_failure_tracks_only_sent_slots(self):
        self.page.set_connected(True, "已连接")
        for key in ("wheel_diameter", "esp32_type"):
            row = self._row(key)
            self.page.table.item(row, 0).setCheckState(Qt.CheckState.Checked)
        errors: list[str] = []
        send_count = 0

        def send_with_failure(frame: CanFrame) -> None:
            nonlocal send_count
            send_count += 1
            if send_count == 2:
                raise RuntimeError("模拟发送失败")
            self.sent.append(frame)

        self.page._send_frame_callback = send_with_failure
        self.page._show_error = errors.append

        self.page.read_selected()

        self.assertEqual({frame.data[2] for frame in self.sent}, {0})
        self.assertEqual(self.page._requested_slots, {0})
        self.assertEqual(self.page.status_label.text(), "发送中断 · 已发送 1/2 slot")
        self.assertEqual(errors, ["模拟发送失败"])

    def test_response_is_ignored_while_disconnected(self):
        row = self._row("wheel_diameter")

        self.page.handle_frame("RX", CanFrame(id=0x08, data=bytes.fromhex("53 00 3E 0F 5C 29 02 15")))

        self.assertEqual(self.page.table.item(row, 4).text(), "—")
        self.assertEqual(self.page.table.item(row, 6).text(), "未读取")

    def test_failure_status_does_not_report_completion(self):
        self.page.set_connected(True, "已连接")
        row = self._row("wheel_diameter")
        self.page.table.item(row, 0).setCheckState(Qt.CheckState.Checked)
        self.page.read_selected()

        self.page.handle_frame("RX", CanFrame(id=0x08, data=bytes.fromhex("53 00 00 00 00 00 03 50")))

        self.assertEqual(self.page.table.item(row, 4).text(), "—")
        self.assertEqual(self.page.table.item(row, 6).text(), "状态 0x03")
        self.assertEqual(self.page.status_label.text(), "读取结束 · 成功 0 / 失败 1 slot")
        self.assertEqual(self.page._received_slots, set())
        self.assertEqual(self.page._failed_slots, {0})

    def test_mixed_success_and_failure_finishes_with_summary(self):
        self.page.set_connected(True, "已连接")
        for key in ("wheel_diameter", "wheel_perimeter"):
            row = self._row(key)
            self.page.table.item(row, 0).setCheckState(Qt.CheckState.Checked)
        self.page.read_selected()

        self.page.handle_frame("RX", CanFrame(id=0x08, data=bytes.fromhex("53 00 00 00 00 00 03 50")))
        self.page.handle_frame("RX", CanFrame(id=0x08, data=bytes.fromhex("53 01 3E E1 30 7B 02 C4")))

        self.assertEqual(self.page.status_label.text(), "读取结束 · 成功 1 / 失败 1 slot")
        self.assertEqual(self.page._received_slots, {1})
        self.assertEqual(self.page._failed_slots, {0})

    def test_compact_width_hides_secondary_columns(self):
        self.page.show()
        self.page.resize(800, 600)
        self.app.processEvents()

        self.assertTrue(self.page.table.isColumnHidden(3))
        self.assertTrue(self.page.table.isColumnHidden(5))
        self.assertFalse(self.page.table.isColumnHidden(2))
        self.assertFalse(self.page.table.isColumnHidden(4))

        self.page.resize(1100, 700)
        self.app.processEvents()

        self.assertFalse(self.page.table.isColumnHidden(3))
        self.assertFalse(self.page.table.isColumnHidden(5))


if __name__ == "__main__":
    unittest.main()
