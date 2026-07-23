from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CanFrame:
    id: int
    data: bytes = field(default_factory=bytes)
    extended: bool = False
    remote: bool = False
    fd: bool = False
    brs: bool = False
    esi: bool = False

    def __post_init__(self) -> None:
        frame_data = bytes(self.data)
        max_length = 64 if self.fd else 8
        if len(frame_data) > max_length:
            frame_type = "CAN FD" if self.fd else "CAN 2.0"
            raise ValueError(f"{frame_type} frame data cannot exceed {max_length} bytes")
        if self.fd and self.remote:
            raise ValueError("CAN FD does not support remote frames")
        if not self.fd and (self.brs or self.esi):
            raise ValueError("BRS/ESI flags are only valid for CAN FD frames")
        object.__setattr__(self, "data", frame_data.ljust(max_length, b"\x00"))
        object.__setattr__(self, "dlc", len(frame_data))


class CanDriver:
    def open(
        self,
        device_type: int,
        device_index: int,
        channel: int,
        baudrate: int,
        *,
        can_fd: bool = False,
        data_baudrate: int | None = None,
    ) -> bool:
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
