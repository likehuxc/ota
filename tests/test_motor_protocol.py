import math
import unittest

from d7_pmu_iap_tool.can.can_frame import CanFrame
from d7_pmu_iap_tool.motor_protocol import (
    decode_human_state,
    decode_motor_feedback,
    disable_frame,
    position_command_frame,
    position_rad_to_raw,
    position_raw_to_rad,
)


class MotorProtocolTests(unittest.TestCase):
    def test_can_fd_frame_accepts_motor_payload(self):
        frame = CanFrame(id=0x300, data=bytes(range(12)), fd=True, brs=True)

        self.assertEqual(frame.dlc, 12)
        self.assertEqual(len(frame.data), 64)
        self.assertTrue(frame.fd)
        self.assertTrue(frame.brs)

    def test_classic_frame_still_rejects_more_than_eight_bytes(self):
        with self.assertRaises(ValueError):
            CanFrame(id=0x123, data=bytes(range(9)))

    def test_known_position_values_match_capture(self):
        self.assertAlmostEqual(position_raw_to_rad(0x7F75), -0.0211, places=4)
        self.assertAlmostEqual(position_raw_to_rad(0x7AF0), -0.1977, places=4)
        self.assertEqual(position_rad_to_raw(position_raw_to_rad(0x7AF0)), 0x7AF0)

    def test_builds_captured_position_command(self):
        frame = position_command_frame(0x01, position_raw_to_rad(0x7AF0))

        self.assertEqual(frame.id, 0x300)
        self.assertEqual(frame.dlc, 12)
        self.assertEqual(bytes(frame.data[:12]), bytes.fromhex("40 00 40 28 1C 01 06 01 00 41 F0 7A"))

    def test_disable_frame_uses_direct_device_id(self):
        frame = disable_frame(0x01)

        self.assertEqual(frame.id, 0x01)
        self.assertEqual(frame.dlc, 16)
        self.assertEqual(bytes(frame.data[:16]), bytes.fromhex("40 40 00 19 24 01 00 01 10 01 10 20 00 00 00 00"))

    def test_decodes_32_byte_feedback(self):
        frame = CanFrame(
            id=0x101,
            data=bytes.fromhex(
                "27 4D 40 38 6C 01 1A 01 00 42 00 00 00 FF 7F 51 78 FF 7F F0 7A FF 7F FF 7F 0F 0D E4 19 0D E4 19"
            ),
            fd=True,
        )

        feedback = decode_motor_feedback(frame, 0x01)

        self.assertIsNotNone(feedback)
        self.assertEqual(feedback.status, 0x42)
        self.assertEqual(feedback.position_raw, 0x7AF0)
        self.assertAlmostEqual(feedback.position_rad, -0.1977, places=4)
        self.assertTrue(math.isclose(feedback.voltage_v, 47.0, abs_tol=0.1))
        self.assertEqual(feedback.motor_temperature_c, 15)
        self.assertEqual(feedback.driver_temperature_c, 13)

    def test_decodes_human_state(self):
        frame = CanFrame(
            id=0x101,
            data=bytes.fromhex("26 4D 00 39 24 01 00 01 10 42 10 20 02 00 01 20"),
            fd=True,
        )

        self.assertEqual(decode_human_state(frame, 0x01), 2)


if __name__ == "__main__":
    unittest.main()
