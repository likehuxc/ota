import struct
import unittest

from d7_pmu_iap_tool.iap.d7_crc32 import d7_crc32, d7_crc32_append
from d7_pmu_iap_tool.iap.firmware_image import FirmwareImage
from d7_pmu_iap_tool.iap.iap_protocol import IapAck, IapProtocol


class D7Crc32Tests(unittest.TestCase):
    def test_matches_board_crc32_vectors(self):
        self.assertEqual(d7_crc32(b""), 0xFFFFFFFF)
        self.assertEqual(d7_crc32(b"123456789"), 0x1556F485)
        self.assertEqual(d7_crc32(bytes(range(16))), 0xEB99FA90)

    def test_append_matches_one_shot(self):
        current = d7_crc32_append(0xFFFFFFFF, b"123")
        current = d7_crc32_append(current, b"456789")

        self.assertEqual(current, d7_crc32(b"123456789"))


class IapProtocolTests(unittest.TestCase):
    def test_builds_checksum_command_frames(self):
        protocol = IapProtocol(target_id=0x19, can_id=0x7FF)

        frame = protocol.query_role()

        self.assertEqual(frame.id, 0x7FF)
        self.assertFalse(frame.extended)
        self.assertEqual(frame.dlc, 8)
        self.assertEqual(bytes(frame.data), bytes([0x16, 0x19, 0x02, 0, 0, 0, 0xE9, 0x1A]))

    def test_builds_firmware_size_as_24_bit_big_endian(self):
        protocol = IapProtocol(target_id=0x19, can_id=0x7FF)

        frame = protocol.set_firmware_size(0x012345)

        self.assertEqual(bytes(frame.data), bytes([0x16, 0x19, 0x05, 0x01, 0x23, 0x45, 0xE9, 0x86]))

    def test_builds_segment_info_without_tail_byte(self):
        protocol = IapProtocol(target_id=0x19, can_id=0x7FF)

        frame = protocol.set_segment_info(section_num=3, section_size=0x0123)

        self.assertEqual(bytes(frame.data), bytes([0x16, 0x19, 0x06, 0x01, 0x23, 0x00, 0x03, 0x5C]))

    def test_builds_segment_data_frames_with_zero_padding(self):
        protocol = IapProtocol(target_id=0x19, can_id=0x7FF)

        frames = protocol.segment_data_frames(b"\x01\x02\x03\x04\x05\x06\x07")

        self.assertEqual([bytes(frame.data) for frame in frames], [
            bytes([0x16, 0x19, 0x07, 1, 2, 3, 4, 5]),
            bytes([0x16, 0x19, 0x07, 6, 7, 0, 0, 0]),
        ])

    def test_builds_validate_segment_frame_crc_including_byte3(self):
        protocol = IapProtocol(target_id=0x19, can_id=0x7FF)

        frame = protocol.validate_segment(section_num=3, section_data=bytes(range(1, 12)), crc_head_extra=0)

        self.assertEqual(bytes(frame.data), bytes([0x16, 0x19, 0x08, 0x00, 0xBD, 0xC3, 0x00, 0x2A]))

    def test_parses_ack_and_rejects_bad_tail(self):
        protocol = IapProtocol(target_id=0x19, can_id=0x7FF)

        ack = protocol.parse_ack(bytes([0x16, 0x19, 0x02, 1, 0, 0, 0xE9, 0x1B]), expected_cmd=0x02)

        self.assertEqual(ack, IapAck(command=0x02, params=bytes([1, 0, 0, 0xE9])))
        with self.assertRaises(ValueError):
            protocol.parse_ack(bytes([0x16, 0x19, 0x02, 1, 0, 0, 0, 0x32]), expected_cmd=0x02)


class FirmwareImageTests(unittest.TestCase):
    def test_validates_and_splits_app_image(self):
        vector_table = struct.pack("<II", 0x20001000, 0x000202C9)
        data = vector_table + bytes(range(256)) * 5

        image = FirmwareImage.from_bytes(data)

        self.assertEqual(image.size, len(data))
        self.assertEqual(image.initial_sp, 0x20001000)
        self.assertEqual(image.reset_vector, 0x000202C9)
        self.assertEqual([len(section.data) for section in image.sections(1024)], [1024, len(data) - 1024])

    def test_warns_for_bin_linked_at_zero(self):
        vector_table = struct.pack("<II", 0x20001000, 0x000002C9)

        image = FirmwareImage.from_bytes(vector_table + b"\x00" * 32)

        self.assertTrue(any("0x00000000" in warning for warning in image.warnings))

    def test_rejects_invalid_stack_pointer(self):
        vector_table = struct.pack("<II", 0x10000000, 0x000202C9)

        with self.assertRaises(ValueError):
            FirmwareImage.from_bytes(vector_table + b"\x00" * 32)


if __name__ == "__main__":
    unittest.main()
