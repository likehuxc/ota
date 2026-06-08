import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from widget import DEFAULT_DLL_PATH, Widget


class WidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_default_connection_values_match_plan(self):
        widget = Widget()

        self.assertEqual(widget.device_type_input.value(), 3)
        self.assertEqual(widget.device_index_input.value(), 0)
        self.assertEqual(widget.channel_input.currentData(), 0)
        self.assertEqual(widget.baudrate_input.currentData(), 1_000_000)
        self.assertEqual(widget.target_id_input.text(), "0x19")
        self.assertEqual(widget.can_id_input.text(), "0x7ff")
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

        widget._set_connection_status(True, "已打开：设备 4，索引 0，通道 0，1000000 bps")

        self.assertEqual(widget.can_status_label.text(), "已打开：设备 4，索引 0，通道 0，1000000 bps")
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


if __name__ == "__main__":
    unittest.main()
