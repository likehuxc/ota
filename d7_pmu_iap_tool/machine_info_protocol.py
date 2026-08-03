from __future__ import annotations

import struct
from dataclasses import dataclass

from d7_pmu_iap_tool.can.can_frame import CanFrame


MACHINE_INFO_RESPONSE_CAN_ID = 0x08
MACHINE_INFO_SUCCESS = 0x02


@dataclass(frozen=True)
class MachineInfoField:
    key: str
    slot: int
    value_type: str
    byte_index: int | None = None

    @property
    def type_label(self) -> str:
        if self.value_type == "byte":
            return f"byte[{self.byte_index}]"
        return self.value_type


@dataclass(frozen=True)
class MachineInfoResponse:
    slot: int
    raw: bytes
    status: int


MACHINE_INFO_FIELDS = (
    MachineInfoField("wheel_diameter", 0, "float"),
    MachineInfoField("wheel_perimeter", 1, "float"),
    MachineInfoField("wheel_base", 2, "float"),
    MachineInfoField("pulse_per_circle", 3, "float"),
    MachineInfoField("sample_times_per_pulse", 4, "float"),
    MachineInfoField("reduction_ratio", 5, "float"),
    MachineInfoField("is_encoder_count_inv", 6, "int"),
    MachineInfoField("uwb_tag_pcb_major_version", 7, "int"),
    MachineInfoField("uwb_tag_pcb_minor_version", 8, "int"),
    MachineInfoField("chassis_pcb_major_version", 9, "int"),
    MachineInfoField("chassis_pcb_minor_version", 10, "int"),
    MachineInfoField("infrared_sensor_version", 11, "int"),
    MachineInfoField("lds_sensor_version", 12, "int"),
    MachineInfoField("motor_version", 13, "int"),
    MachineInfoField("weigh_sensor_version", 14, "int"),
    MachineInfoField("battery_version", 15, "int"),
    MachineInfoField("uwb_tag_pcb_mpd_year", 16, "int"),
    MachineInfoField("uwb_tag_pcb_mpd_month", 17, "int"),
    MachineInfoField("uwb_tag_pcb_mpd_day", 18, "int"),
    MachineInfoField("chassis_pcb_mpd_year", 19, "int"),
    MachineInfoField("chassis_pcb_mpd_month", 20, "int"),
    MachineInfoField("chassis_pcb_mpd_day", 21, "int"),
    MachineInfoField("slam_camera_version", 22, "int"),
    MachineInfoField("esp32_type", 23, "byte", 3),
    MachineInfoField("rgbd_type", 23, "byte", 2),
    MachineInfoField("machine_type", 23, "byte", 1),
    MachineInfoField("audio_version", 23, "byte", 0),
    MachineInfoField("lora_type", 24, "byte", 3),
    MachineInfoField("scan_code_device", 24, "byte", 2),
    MachineInfoField("monocular_camera", 24, "byte", 1),
    MachineInfoField("slam_core", 24, "byte", 0),
    MachineInfoField("product_type", 27, "product"),
    MachineInfoField("npu_type", 28, "byte", 3),
    MachineInfoField("matrix_mic_type", 28, "byte", 2),
    MachineInfoField("host_core_board_type", 28, "byte", 1),
    MachineInfoField("head_board_type", 28, "byte", 0),
    MachineInfoField("lidar_communicate_type", 29, "byte", 3),
    MachineInfoField("lidar_type", 29, "byte", 2),
    MachineInfoField("lte_type", 29, "byte", 1),
    MachineInfoField("cabin_door_motor_type", 29, "byte", 0),
    MachineInfoField("chassis_board_type", 30, "byte", 1),
    MachineInfoField("function_board_type", 30, "byte", 2),
    MachineInfoField("cabin_door_board_type", 30, "byte", 0),
    MachineInfoField("laser_projection", 31, "byte", 3),
    MachineInfoField("vedio_output", 31, "byte", 2),
    MachineInfoField("distribution_area_light", 31, "byte", 1),
    MachineInfoField("magic_sensor", 31, "byte", 0),
    MachineInfoField("power_board_type", 32, "byte", 3),
    MachineInfoField("usbcan_board_type", 32, "byte", 2),
    MachineInfoField("pdu1_board_type", 32, "byte", 1),
    MachineInfoField("pdu2_board_type", 32, "byte", 0),
    MachineInfoField("rgbd_angle_type", 34, "byte", 3),
    MachineInfoField("lidar_communicate_type_second", 34, "byte", 2),
    MachineInfoField("lidar_type_second", 34, "byte", 1),
    MachineInfoField("machine_color", 34, "byte", 0),
)


def build_machine_info_request(can_id: int, slot: int) -> CanFrame:
    if not 0 <= can_id <= 0x7FF:
        raise ValueError("MachineInfo 请求 CAN ID 必须是标准帧 ID")
    if not 0 <= slot < 76:
        raise ValueError("MachineInfo slot 必须在 0 到 75 之间")
    body = bytes((0x00, 0x53, slot, 0x00, 0x00, 0x00, 0x00))
    return CanFrame(id=can_id, data=body + bytes((_xor_checksum(body),)))


def parse_machine_info_response(
    frame: CanFrame,
    response_can_id: int = MACHINE_INFO_RESPONSE_CAN_ID,
) -> MachineInfoResponse | None:
    if frame.id != response_can_id or frame.extended or frame.remote or frame.dlc != 8:
        return None
    payload = bytes(frame.data[:8])
    if payload[0] != 0x53 or payload[7] != _xor_checksum(payload[:7]):
        return None
    return MachineInfoResponse(slot=payload[1], raw=payload[2:6], status=payload[6])


def format_machine_info_value(field: MachineInfoField, raw: bytes) -> str:
    if len(raw) != 4:
        raise ValueError("MachineInfo 数据必须是 4 字节")
    if field.value_type == "float":
        return f"{struct.unpack('>f', raw)[0]:.7g}"
    if field.value_type == "int":
        return str(int.from_bytes(raw, "big"))
    if field.value_type == "byte" and field.byte_index is not None:
        return str(raw[field.byte_index])
    if field.value_type == "product":
        model = int.from_bytes(raw[2:4], "big")
        return f"model={model}, version={raw[1]}.{raw[0]}"
    raise ValueError(f"不支持的 MachineInfo 类型：{field.value_type}")


def _xor_checksum(data: bytes) -> int:
    checksum = 0
    for value in data:
        checksum ^= value
    return checksum
