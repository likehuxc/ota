import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from d7_pmu_iap_tool.can.can_frame import CanFrame
from d7_pmu_iap_tool.motor_test_page import PositionHeartbeatWorker


class PositionHeartbeatWorkerTests(unittest.TestCase):
    def test_worker_sends_periodically_until_requested_to_stop(self):
        sent_at = []
        worker = None

        def send(_frame):
            sent_at.append(time.monotonic())
            if len(sent_at) == 10:
                worker.request_stop()

        worker = PositionHeartbeatWorker(send, CanFrame(id=0x300, data=b"\x01", fd=True), 50)

        worker.run()

        self.assertEqual(len(sent_at), 10)
        self.assertLess(sent_at[-1] - sent_at[0], 0.3)
        gaps = [right - left for left, right in zip(sent_at, sent_at[1:])]
        self.assertLess(max(gaps), 0.06)


if __name__ == "__main__":
    unittest.main()
