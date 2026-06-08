from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CanFrame:
    id: int
    data: bytes = field(default_factory=bytes)
    extended: bool = False
    remote: bool = False

    def __post_init__(self) -> None:
        frame_data = bytes(self.data)
        if len(frame_data) > 8:
            raise ValueError("CAN 2.0 frame data cannot exceed 8 bytes")
        object.__setattr__(self, "data", frame_data.ljust(8, b"\x00"))
        object.__setattr__(self, "dlc", len(frame_data))


class CanDriver:
    def open(self, device_type: int, device_index: int, channel: int, baudrate: int) -> bool:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def is_open(self) -> bool:
        raise NotImplementedError

    def send(self, frame: CanFrame) -> bool:
        raise NotImplementedError

    def receive(self, timeout_ms: int) -> CanFrame | None:
        raise NotImplementedError

    @property
    def last_error(self) -> str:
        raise NotImplementedError
