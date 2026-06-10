import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QScrollArea

from d7_pmu_iap_tool.can.can_frame import CanFrame
from widget import DEFAULT_DLL_PATH, MAX_CAN_ROWS, Widget


class FakeDriver:
    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.sent = []
        self._open = True
        self._last_error = ""

    def open(self, device_type, device_index, channel, baudrate):
        self._open = True
        return True

    def close(self):
        self._open = False

    def shutdown(self):
        self._open = False

    def is_open(self):
        return self._open

    def send(self, frame):
        self.sent.append(frame)
        return True

    def receive(self, timeout_ms):
        if not self.replies:
            return None
        return self.replies.pop(0)

    @property
    def last_error(self):
        return self._last_error


class WidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_default_connection_values_match_plan(self):
        widget = Widget()

        self.assertFalse(hasattr(widget, "device_type_input"))
        self.assertFalse(hasattr(widget, "device_index_input"))
        self.assertEqual(widget.channel_input.currentData(), 0)
        self.assertEqual(widget.baudrate_input.currentData(), 1_000_000)
        self.assertEqual(widget.target_id_input.text(), "0x19")
        self.assertEqual(widget.can_id_input.text(), "0x7ff")
        self.assertEqual(widget._make_options().device_type, 3)
        self.assertEqual(widget._make_options().device_index, 0)
        self.assertEqual(widget.progress_bar.value(), 0)

    def test_device_selector_updates_target_and_iap_can_id(self):
        widget = Widget()

        labels = [widget.device_profile_input.itemText(index) for index in range(widget.device_profile_input.count())]
        self.assertEqual(labels, ["D7-CT01", "D7-CT02", "D7-沛城电池"])
        self.assertEqual(widget.device_profile_input.currentText(), "D7-CT02")
        self.assertEqual(widget.target_id_input.text(), "0x19")
        self.assertEqual(widget.can_id_input.text(), "0x7ff")
        self.assertEqual(widget.send_can_id_input.text(), "0x7ff")

        widget.device_profile_input.setCurrentIndex(widget.device_profile_input.findText("D7-CT01"))

        self.assertEqual(widget.target_id_input.text(), "0x18")
        self.assertEqual(widget.can_id_input.text(), "0x7ff")
        self.assertEqual(widget._make_protocol().target_id, 0x18)
        self.assertEqual(widget._make_protocol().can_id, 0x7FF)

        widget.device_profile_input.setCurrentIndex(widget.device_profile_input.findText("D7-沛城电池"))

        self.assertEqual(widget.target_id_input.text(), "0x41")
        self.assertEqual(widget.can_id_input.text(), "0x7ff")
        self.assertEqual(widget.send_can_id_input.text(), "0x7ff")
        self.assertEqual(widget._make_options().pre_upgrade_wakeup_ms, 1000)

    def test_ui_uses_html_preview_structure_and_copy(self):
        widget = Widget()

        self.assertEqual(widget.windowTitle(), "D7 CAN IAP")
        self.assertEqual(widget.title_label.text(), "D7 CAN IAP")
        self.assertEqual(widget.device_profile_input.currentText(), "D7-CT02")
        self.assertIsInstance(widget.scroll_area, QScrollArea)
        self.assertEqual(
            [label.text() for label in widget.step_title_labels],
            ["连接设备", "选择固件", "查询角色", "升级写入", "完成校验"],
        )
        self.assertEqual(widget.conn_group.title(), "设备连接")
        self.assertEqual(widget.file_group.title(), "固件文件")
        self.assertEqual(widget.upgrade_group.title(), "升级控制")
        self.assertEqual(widget.device_status_group.title(), "设备状态")
        self.assertEqual(widget.send_group.title(), "CAN 发送调试")
        self.assertEqual(widget.can_group.title(), "CAN 监控")
        self.assertEqual(widget.system_log_group.title(), "系统日志")
        self.assertEqual(widget.role_value_label.text(), "APP")

    def test_firmware_card_only_shows_size_metric(self):
        widget = Widget()

        self.assertEqual([label.text() for label in widget.firmware_metric_title_labels], ["Size"])

    def test_can_monitor_uses_pause_toggle_instead_of_show_all_button(self):
        widget = Widget()

        self.assertEqual(widget.pause_can_display_button.text(), "暂停显示")
        self.assertFalse(hasattr(widget, "clear_filter_button"))

    def test_default_dll_path_uses_local_installation(self):
        widget = Widget()

        driver = widget._make_driver()

        self.assertEqual(driver.dll_path, str(DEFAULT_DLL_PATH))

    def test_dll_picker_is_shown_but_default_path_is_used_until_changed(self):
        widget = Widget()

        self.assertEqual(widget.dll_browse_button.text(), "选择 DLL")
        self.assertEqual(widget.dll_config_value.text(), "使用默认 zlgcan.dll")
        self.assertIn(str(DEFAULT_DLL_PATH), widget.dll_path_label.text())

    def test_selected_dll_path_is_used_for_driver(self):
        widget = Widget()

        selected_path = Path(r"C:\Vendor\zlgcan.dll")
        widget._set_dll_path(selected_path)
        driver = widget._make_driver()

        self.assertEqual(driver.dll_path, str(selected_path))
        self.assertEqual(widget.dll_config_value.text(), "已选择本地 DLL")
        self.assertIn(str(selected_path), widget.dll_path_label.text())

    def test_can_status_starts_disconnected(self):
        widget = Widget()

        self.assertEqual(widget.can_status_label.text(), "未连接")
        self.assertTrue(widget.open_button.isEnabled())
        self.assertFalse(widget.close_button.isEnabled())
        self.assertFalse(widget.query_role_button.isEnabled())
        self.assertFalse(widget.start_upgrade_button.isEnabled())

    def test_can_status_updates_when_connection_changes(self):
        widget = Widget()

        widget._set_connection_status(True, "已打开：通道 0，1000000 bps")

        self.assertEqual(widget.can_status_label.text(), "已打开：通道 0，1000000 bps")
        self.assertFalse(widget.open_button.isEnabled())
        self.assertTrue(widget.close_button.isEnabled())
        self.assertTrue(widget.query_role_button.isEnabled())
        self.assertTrue(widget.start_upgrade_button.isEnabled())

        widget._set_connection_status(False, "未连接")

        self.assertEqual(widget.can_status_label.text(), "未连接")
        self.assertTrue(widget.open_button.isEnabled())
        self.assertFalse(widget.close_button.isEnabled())
        self.assertFalse(widget.query_role_button.isEnabled())
        self.assertFalse(widget.start_upgrade_button.isEnabled())

    def test_query_role_updates_current_device_role_display(self):
        widget = Widget()
        widget.driver = FakeDriver()
        widget._set_connection_status(True, "已打开：通道 0，1000000 bps")

        with patch("widget.IapUpgradeController") as controller_cls:
            controller_cls.return_value.query_role.return_value = "BOOT"
            widget.query_role()

        self.assertEqual(widget.role_value_label.text(), "BOOT")
        self.assertEqual(widget.step_desc_labels[2].text(), "当前角色 BOOT")

    def test_upgrade_log_current_role_updates_current_device_role_display(self):
        widget = Widget()

        widget._handle_upgrade_log("当前角色 BOOT")

        self.assertEqual(widget.role_value_label.text(), "BOOT")

    def test_dialog_position_is_horizontally_centered_and_one_third_down(self):
        widget = Widget()
        widget.setGeometry(90, 150, 900, 600)

        position = widget._dialog_position_for_size(240, 120)

        self.assertEqual(position.x(), 420)
        self.assertEqual(position.y(), 290)

    def test_upgrade_success_uses_positioned_information_dialog(self):
        widget = Widget()
        shown = []
        widget._upgrade_started_at = 100.0
        widget._monotonic = lambda: 165.432

        widget._show_information = lambda title, message: shown.append((title, message))

        with patch("widget.QMessageBox.information"):
            widget._upgrade_succeeded()

        self.assertEqual(shown, [("升级成功", "升级成功，设备已跳转到 APP。\n升级耗时：00:01:05.432")])
        self.assertEqual(widget.upgrade_elapsed_value.text(), "00:01:05.432")

    def test_upgrade_elapsed_time_is_formatted_for_hours_minutes_seconds(self):
        widget = Widget()

        self.assertEqual(widget._format_elapsed_time(3_723.045), "01:02:03.045")

    def test_upgrade_failure_records_elapsed_time_in_log(self):
        widget = Widget()
        widget._upgrade_started_at = 10.0
        widget._monotonic = lambda: 12.5
        warnings = []
        widget._show_error = lambda message: warnings.append(message)

        widget._upgrade_failed("ACK 超时")

        self.assertEqual(widget.upgrade_elapsed_value.text(), "00:00:02.500")
        self.assertIn("升级耗时：00:00:02.500", widget.log_output.toPlainText())
        self.assertEqual(warnings, ["ACK 超时\n升级耗时：00:00:02.500"])

    def test_received_can_frame_is_shown_in_table(self):
        widget = Widget()
        widget.driver = FakeDriver([CanFrame(id=0x123, data=b"\x01\x02\x03")])
        widget._set_connection_status(True, "已打开：通道 0，1000000 bps")

        widget._poll_can_frames()

        self.assertEqual(widget.can_table.rowCount(), 1)
        self.assertEqual(widget.can_table.item(0, 0).text(), "RX")
        self.assertEqual(widget.can_table.item(0, 2).text(), "0x123")
        self.assertEqual(widget.can_table.item(0, 3).text(), "3")
        self.assertEqual(widget.can_table.item(0, 4).text(), "01 02 03")

    def test_manual_send_parses_hex_data_and_records_tx(self):
        widget = Widget()
        driver = FakeDriver()
        widget.driver = driver
        widget._set_connection_status(True, "已打开：通道 0，1000000 bps")
        widget.send_can_id_input.setText("0x321")
        widget.send_data_input.setText("16 19 02")

        widget.send_can_frame()

        self.assertEqual(driver.sent[0].id, 0x321)
        self.assertEqual(driver.sent[0].data[: driver.sent[0].dlc], b"\x16\x19\x02")
        self.assertEqual(widget.can_table.item(0, 0).text(), "TX")
        self.assertEqual(widget.can_table.item(0, 2).text(), "0x321")

    def test_system_log_is_in_left_panel_and_can_area_owns_the_right_panel(self):
        widget = Widget()

        self.assertTrue(widget.system_log_group.isAncestorOf(widget.log_output))
        self.assertIs(widget.can_group.parentWidget(), widget.right_panel)
        self.assertIs(widget.send_group.parentWidget(), widget.right_panel)
        self.assertGreaterEqual(widget.right_panel.layout().count(), 3)

    def test_send_data_input_column_is_the_wide_column(self):
        widget = Widget()

        layout = widget.send_form_layout

        self.assertGreater(layout.columnStretch(1), layout.columnStretch(0))
        self.assertGreater(layout.columnStretch(1), layout.columnStretch(2))
        self.assertLessEqual(widget.send_can_id_input.maximumWidth(), 130)

    def test_new_can_ids_are_added_to_filter_chips_while_running(self):
        widget = Widget()

        widget._append_can_frame("RX", CanFrame(id=0x305, data=b"\x01"))

        self.assertIn(0x305, widget.known_can_ids)
        self.assertIn(0x305, widget.can_filter_buttons)
        self.assertEqual(widget.can_filter_buttons[0x305].text(), "0x305")

    def test_can_filter_shows_only_matching_ids_and_can_be_cleared(self):
        widget = Widget()
        widget._append_can_frame("RX", CanFrame(id=0x41, data=b"\x01"))
        widget._append_can_frame("RX", CanFrame(id=0x123, data=b"\x02"))

        widget.can_filter_input.setText("0x41")
        widget.apply_can_filter()

        self.assertEqual(widget.can_table.rowCount(), 1)
        self.assertEqual(widget.can_table.item(0, 2).text(), "0x41")
        self.assertIn("1 / 2", widget.can_filter_status_label.text())

        widget.clear_can_filter()

        self.assertEqual(widget.can_table.rowCount(), 2)
        self.assertEqual(widget.filter_can_ids, set())

    def test_pause_can_display_keeps_recording_but_stops_table_updates(self):
        widget = Widget()
        widget._append_can_frame("RX", CanFrame(id=0x41, data=b"\x01"))

        widget.toggle_can_display_pause()
        widget._append_can_frame("RX", CanFrame(id=0x42, data=b"\x02"))

        self.assertEqual(widget.pause_can_display_button.text(), "继续显示")
        self.assertEqual(widget.can_table.rowCount(), 1)
        self.assertEqual(len(widget._can_records), 2)
        self.assertEqual(len(widget._rx_save_records), 2)

        widget.toggle_can_display_pause()

        self.assertEqual(widget.pause_can_display_button.text(), "暂停显示")
        self.assertEqual(widget.can_table.rowCount(), 2)
        self.assertEqual(widget.can_table.item(1, 2).text(), "0x42")

    def test_can_filter_accepts_comma_or_space_separated_ids(self):
        widget = Widget()

        parsed = widget._parse_filter_ids("0x41, 0x7ff 123")

        self.assertEqual(parsed, {0x41, 0x7FF, 0x123})

    def test_appending_many_frames_does_not_rerender_the_whole_table(self):
        widget = Widget()

        def fail_if_full_rerender_is_used():
            raise AssertionError("append path should not rerender the whole CAN table")

        widget._render_can_table = fail_if_full_rerender_is_used

        for index in range(60):
            widget._append_can_frame("RX", CanFrame(id=0x41, data=bytes([index & 0xFF])))

        self.assertEqual(widget.can_table.rowCount(), 60)

    def test_save_rx_can_records_exports_all_received_rows_ignoring_filter(self):
        widget = Widget()
        widget._append_can_frame("RX", CanFrame(id=0x41, data=b"\x01"))
        widget._append_can_frame("TX", CanFrame(id=0x7FF, data=b"\x02"))
        widget._append_can_frame("RX", CanFrame(id=0x305, data=b"\x03"))
        widget.can_filter_input.setText("0x41")
        widget.apply_can_filter()

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "rx.csv"
            widget._save_rx_can_records(output_path)
            content = output_path.read_text(encoding="utf-8")

        self.assertIn("direction,time,canid,len,data", content)
        self.assertIn("RX,", content)
        self.assertIn("0x41", content)
        self.assertIn("0x305", content)
        self.assertNotIn("TX,", content)
        self.assertNotIn("0x7FF", content)

    def test_save_rx_can_records_keeps_rows_beyond_display_limit(self):
        widget = Widget()
        for index in range(MAX_CAN_ROWS + 1):
            widget._append_can_frame("RX", CanFrame(id=0x41, data=bytes([index & 0xFF])))

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "rx.csv"
            saved_count = widget._save_rx_can_records(output_path)
            lines = output_path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(widget.can_table.rowCount(), MAX_CAN_ROWS)
        self.assertEqual(saved_count, MAX_CAN_ROWS + 1)
        self.assertEqual(len(lines), MAX_CAN_ROWS + 2)


if __name__ == "__main__":
    unittest.main()
