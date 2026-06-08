import json
import unittest

from d7_pmu_iap_tool.can.broker_can_driver import ZlgCanBrokerDriver
from d7_pmu_iap_tool.can.can_frame import CanFrame
from d7_pmu_iap_tool.can.zlgcan_broker import _handle


class FakeDriver:
    def __init__(self):
        self.dll_path = ""
        self.opened_with = None
        self._open = False
        self.sent = []
        self.last_error = ""
        self.to_receive = None
        self.closed = False
        self.device_type = 3
        self.device_index = 0
        self.channel = 0

    def set_dll_path(self, p):
        self.dll_path = p

    def open(self, device_type, device_index, channel, baudrate):
        self.opened_with = (device_type, device_index, channel, baudrate)
        self._open = True
        return True

    def close(self):
        self.closed = True
        self._open = False

    def is_open(self):
        return self._open

    def send(self, frame):
        self.sent.append(frame)
        return True

    def receive(self, timeout_ms):
        return self.to_receive


class BrokerHandleTests(unittest.TestCase):
    def test_ping(self):
        self.assertEqual(_handle(FakeDriver(), {"cmd": "ping"}), {"ok": True})

    def test_open_passes_args_and_dll(self):
        drv = FakeDriver()
        resp = _handle(drv, {
            "cmd": "open", "dll": "z.dll",
            "device_type": 3, "device_index": 0, "channel": 0, "baudrate": 500000,
        })
        self.assertEqual(resp["ok"], True)
        self.assertEqual(resp["device_type"], 3)
        self.assertEqual(drv.dll_path, "z.dll")
        self.assertEqual(drv.opened_with, (3, 0, 0, 500000))

    def test_open_failure_returns_error(self):
        drv = FakeDriver()
        drv.open = lambda *a: False
        drv.last_error = "boom"
        resp = _handle(drv, {
            "cmd": "open", "dll": "z.dll",
            "device_type": 3, "device_index": 0, "channel": 0, "baudrate": 500000,
        })
        self.assertEqual(resp, {"ok": False, "error": "boom"})

    def test_send_roundtrips_frame(self):
        drv = FakeDriver()
        resp = _handle(drv, {"cmd": "send", "frame": {
            "id": 0x7FF, "data": "161902", "extended": False, "remote": False,
        }})
        self.assertEqual(resp, {"ok": True})
        self.assertEqual(drv.sent[0].id, 0x7FF)
        self.assertEqual(drv.sent[0].data[:3], b"\x16\x19\x02")

    def test_receive_serializes_frame(self):
        drv = FakeDriver()
        drv.to_receive = CanFrame(id=0x123, data=b"\x01\x02", extended=True)
        resp = _handle(drv, {"cmd": "receive", "timeout_ms": 10})
        self.assertEqual(resp["ok"], True)
        self.assertEqual(resp["frame"]["id"], 0x123)
        self.assertEqual(resp["frame"]["data"], "0102")
        self.assertTrue(resp["frame"]["extended"])

    def test_receive_none(self):
        resp = _handle(FakeDriver(), {"cmd": "receive", "timeout_ms": 10})
        self.assertEqual(resp, {"ok": True, "frame": None})

    def test_is_open_and_close_and_shutdown(self):
        drv = FakeDriver()
        _handle(drv, {"cmd": "open", "dll": "z", "device_type": 3,
                      "device_index": 0, "channel": 0, "baudrate": 500000})
        self.assertEqual(_handle(drv, {"cmd": "is_open"}), {"ok": True, "value": True})
        self.assertEqual(_handle(drv, {"cmd": "close"}), {"ok": True})
        self.assertTrue(drv.closed)
        self.assertEqual(_handle(drv, {"cmd": "shutdown"}), {"ok": True, "shutdown": True})

    def test_unknown_cmd(self):
        resp = _handle(FakeDriver(), {"cmd": "nope"})
        self.assertFalse(resp["ok"])
        self.assertIn("unknown cmd", resp["error"])


class FakeStdin:
    def __init__(self):
        self.lines = []

    def write(self, s):
        self.lines.append(s)

    def flush(self):
        pass


class FakeStdout:
    def __init__(self, responses):
        self._responses = [json.dumps(r) + "\n" for r in responses]

    def readline(self):
        return self._responses.pop(0) if self._responses else ""


class FakeProc:
    def __init__(self, responses):
        self.stdin = FakeStdin()
        self.stdout = FakeStdout(responses)

    def poll(self):
        return None  # still running


class BrokerProxyTests(unittest.TestCase):
    def _driver(self, responses):
        drv = ZlgCanBrokerDriver("z.dll")
        drv._proc = FakeProc(responses)
        return drv

    def test_open_sends_request_and_marks_open(self):
        drv = self._driver([{"ok": True}])
        ok = drv.open(3, 0, 1, 500000)
        self.assertTrue(ok)
        self.assertTrue(drv.is_open())
        sent = json.loads(drv._proc.stdin.lines[0])
        self.assertEqual(sent["cmd"], "open")
        self.assertEqual(sent["dll"], "z.dll")
        self.assertEqual((sent["device_type"], sent["channel"], sent["baudrate"]), (3, 1, 500000))

    def test_open_failure_records_error(self):
        drv = self._driver([{"ok": False, "error": "no device"}])
        self.assertFalse(drv.open(3, 0, 0, 500000))
        self.assertFalse(drv.is_open())
        self.assertEqual(drv.last_error, "no device")

    def test_send_serializes_frame(self):
        drv = self._driver([{"ok": True}])
        self.assertTrue(drv.send(CanFrame(id=0x7FF, data=b"\x16\x19\x02")))
        sent = json.loads(drv._proc.stdin.lines[0])
        self.assertEqual(sent["cmd"], "send")
        self.assertEqual(sent["frame"]["id"], 0x7FF)
        self.assertEqual(sent["frame"]["data"], "161902")

    def test_receive_parses_frame(self):
        drv = self._driver([{"ok": True, "frame": {
            "id": 0x123, "data": "0102", "extended": True, "remote": False}}])
        frame = drv.receive(50)
        self.assertEqual(frame.id, 0x123)
        self.assertEqual(frame.data[:2], b"\x01\x02")
        self.assertTrue(frame.extended)

    def test_receive_none_when_no_frame(self):
        drv = self._driver([{"ok": True, "frame": None}])
        self.assertIsNone(drv.receive(50))

    def test_broker_exit_sets_error(self):
        drv = self._driver([])  # readline returns "" -> broker gone
        self.assertFalse(drv.open(3, 0, 0, 500000))
        self.assertIn("退出", drv.last_error)


if __name__ == "__main__":
    unittest.main()
