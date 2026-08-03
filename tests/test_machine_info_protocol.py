from __future__ import annotations

import unittest

from d7_pmu_iap_tool.can.can_frame import CanFrame
from d7_pmu_iap_tool.machine_info_protocol import (
    MACHINE_INFO_FIELDS,
    build_machine_info_request,
    format_machine_info_value,
    parse_machine_info_response,
)


class MachineInfoProtocolTests(unittest.TestCase):
    def test_request_uses_00_53_slot_and_xor(self):
        frame = build_machine_info_request(0x00, 0x1C)

        self.assertEqual(frame.id, 0x00)
        self.assertEqual(bytes(frame.data[:8]), bytes.fromhex("00 53 1C 00 00 00 00 4F"))

    def test_mock_float_response_is_parsed_and_formatted(self):
        frame = CanFrame(id=0x08, data=bytes.fromhex("53 00 3E 0F 5C 29 02 15"))

        response = parse_machine_info_response(frame)

        self.assertIsNotNone(response)
        assert response is not None
        self.assertEqual(response.slot, 0)
        self.assertEqual(response.raw, bytes.fromhex("3E 0F 5C 29"))
        self.assertEqual(format_machine_info_value(MACHINE_INFO_FIELDS[0], response.raw), "0.14")

    def test_byte_and_product_fields_follow_orin_layout(self):
        host_core = next(field for field in MACHINE_INFO_FIELDS if field.key == "host_core_board_type")
        product = next(field for field in MACHINE_INFO_FIELDS if field.key == "product_type")

        self.assertEqual(format_machine_info_value(host_core, bytes.fromhex("00 06 00 00")), "6")
        self.assertEqual(
            format_machine_info_value(product, bytes.fromhex("00 00 00 1A")),
            "model=26, version=0.0",
        )

    def test_invalid_checksum_is_ignored(self):
        frame = CanFrame(id=0x08, data=bytes.fromhex("53 00 3E 0F 5C 29 02 00"))

        self.assertIsNone(parse_machine_info_response(frame))


if __name__ == "__main__":
    unittest.main()
