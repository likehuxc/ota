from __future__ import annotations

from dataclasses import dataclass

from d7_pmu_iap_tool.can.can_frame import CanFrame
from d7_pmu_iap_tool.iap.d7_crc32 import d7_crc32

PROTOCOL_HEAD = 0x16
PROTOCOL_TAIL = (~PROTOCOL_HEAD) & 0xFF

CMD_REBOOT_TO_BOOTLOADER = 0x01
CMD_GET_RUN_ROLE = 0x02
CMD_GET_SOFT_VERSION = 0x03
CMD_ENABLE_CAN = 0x04
CMD_SET_FIRMWARE_SIZE = 0x05
CMD_SET_SEGMENT_INFO = 0x06
CMD_FILL_SEGMENT_DATA = 0x07
CMD_VALIDATE_SEGMENT_DATA = 0x08
CMD_JUMP_TO_APP = 0x09
CMD_GET_BOARD_CHIP_TYPE = 0x10

RUN_ROLE_APP = "APP"
RUN_ROLE_BOOTLOADER = "BOOT"


@dataclass(frozen=True)
class IapAck:
    command: int
    params: bytes


class IapProtocol:
    def __init__(self, target_id: int = 0x19, can_id: int = 0x7FF) -> None:
        self.target_id = target_id & 0xFF
        self.can_id = can_id

    def reboot_to_bootloader(self) -> CanFrame:
        return self._tail_command(CMD_REBOOT_TO_BOOTLOADER)

    def query_role(self) -> CanFrame:
        return self._tail_command(CMD_GET_RUN_ROLE)

    def get_software_version(self) -> CanFrame:
        return self._tail_command(CMD_GET_SOFT_VERSION)

    def set_can_messages_enabled(self, enabled: bool) -> CanFrame:
        return self._tail_command(CMD_ENABLE_CAN, bytes([1 if enabled else 0, 0, 0]))

    def set_firmware_size(self, total_size: int) -> CanFrame:
        if not 0 <= total_size <= 0xFFFFFF:
            raise ValueError("firmware size must fit in 24 bits")
        params = bytes([(total_size >> 16) & 0xFF, (total_size >> 8) & 0xFF, total_size & 0xFF])
        return self._tail_command(CMD_SET_FIRMWARE_SIZE, params)

    def set_segment_info(self, section_num: int, section_size: int) -> CanFrame:
        if not 0 <= section_num <= 0xFFFF:
            raise ValueError("section number must fit in 16 bits")
        if not 0 <= section_size <= 1024:
            raise ValueError("section size must be <= 1024")
        params = bytes([
            (section_size >> 8) & 0xFF,
            section_size & 0xFF,
            (section_num >> 8) & 0xFF,
            section_num & 0xFF,
        ])
        return self._checksum_command(CMD_SET_SEGMENT_INFO, params)

    def segment_data_frames(self, section_data: bytes) -> list[CanFrame]:
        frames: list[CanFrame] = []
        for offset in range(0, len(section_data), 5):
            payload = section_data[offset:offset + 5].ljust(5, b"\x00")
            frames.append(CanFrame(id=self.can_id, data=bytes([PROTOCOL_HEAD, self.target_id, CMD_FILL_SEGMENT_DATA]) + payload))
        return frames

    def validate_segment(self, section_num: int, section_data: bytes, crc_head_extra: int = 0) -> CanFrame:
        head = bytes([
            (section_num >> 8) & 0xFF,
            section_num & 0xFF,
            PROTOCOL_HEAD,
            self.target_id,
            CMD_VALIDATE_SEGMENT_DATA,
            crc_head_extra & 0xFF,
        ])
        crc = d7_crc32(head + bytes(section_data))
        data = bytes([
            PROTOCOL_HEAD,
            self.target_id,
            CMD_VALIDATE_SEGMENT_DATA,
            crc_head_extra & 0xFF,
            (crc >> 24) & 0xFF,
            (crc >> 16) & 0xFF,
            (crc >> 8) & 0xFF,
            crc & 0xFF,
        ])
        return CanFrame(id=self.can_id, data=data)

    def jump_to_app(self) -> CanFrame:
        return self._tail_command(CMD_JUMP_TO_APP)

    def parse_ack(self, data: bytes, expected_cmd: int | None = None) -> IapAck:
        frame = bytes(data)
        if len(frame) != 8:
            raise ValueError("ACK must be exactly 8 bytes")
        if frame[0] != PROTOCOL_HEAD:
            raise ValueError(f"ACK protocol head mismatch: 0x{frame[0]:02X}")
        if frame[1] != self.target_id:
            raise ValueError(f"ACK target id mismatch: 0x{frame[1]:02X}")
        if expected_cmd is not None and frame[2] != expected_cmd:
            raise ValueError(f"ACK command mismatch: got 0x{frame[2]:02X}, expected 0x{expected_cmd:02X}")
        expected_checksum = checksum8(frame[:7])
        if frame[7] != expected_checksum:
            raise ValueError(f"ACK checksum mismatch: got 0x{frame[7]:02X}, expected 0x{expected_checksum:02X}")
        if frame[2] not in (CMD_SET_SEGMENT_INFO, CMD_VALIDATE_SEGMENT_DATA) and frame[6] != PROTOCOL_TAIL:
            raise ValueError(f"ACK frame tail mismatch: got 0x{frame[6]:02X}, expected 0x{PROTOCOL_TAIL:02X}")
        return IapAck(command=frame[2], params=frame[3:7])

    def ack_role(self, ack: IapAck) -> str:
        if ack.command != CMD_GET_RUN_ROLE:
            raise ValueError("ACK is not a role response")
        return RUN_ROLE_BOOTLOADER if ack.params[0] == 1 else RUN_ROLE_APP

    def _tail_command(self, command: int, params: bytes = b"\x00\x00\x00") -> CanFrame:
        if len(params) != 3:
            raise ValueError("IAP tail command params must be exactly 3 bytes")
        data = bytes([PROTOCOL_HEAD, self.target_id, command]) + params + bytes([PROTOCOL_TAIL])
        return CanFrame(id=self.can_id, data=data + bytes([checksum8(data)]))

    def _checksum_command(self, command: int, params: bytes = b"\x00\x00\x00\x00") -> CanFrame:
        if len(params) != 4:
            raise ValueError("IAP command params must be exactly 4 bytes")
        data = bytes([PROTOCOL_HEAD, self.target_id, command]) + params
        return CanFrame(id=self.can_id, data=data + bytes([checksum8(data)]))


def checksum8(data: bytes) -> int:
    return sum(data) & 0xFF
