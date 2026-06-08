import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

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

    def test_default_dll_path_uses_local_installation(self):
        widget = Widget()

        driver = widget._make_driver()

        self.assertEqual(driver.dll_path, str(DEFAULT_DLL_PATH))

    def test_dll_picker_is_not_shown_when_default_path_is_used(self):
        widget = Widget()

        self.assertFalse(hasattr(widget, "dll_browse_button"))
        self.assertIn(str(DEFAULT_DLL_PATH), widget.dll_path_label.text())

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

        self.assertIs(widget.log_output.parentWidget(), widget.system_log_group)
        self.assertIs(widget.can_group.parentWidget(), widget.right_panel)
        self.assertIs(widget.send_group.parentWidget(), widget.conn_group)
        self.assertEqual(widget.right_panel.layout().count(), 1)

    def test_send_data_input_column_is_the_wide_column(self):
        widget = Widget()

        layout = widget.send_group.layout()

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
