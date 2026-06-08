from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

SRAM_BASE = 0x20000000
SRAM_SIZE = 192 * 1024
APP_BASE = 0x00020000
MAX_APP_SIZE = 376 * 1024


@dataclass(frozen=True)
class FirmwareSection:
    number: int
    data: bytes


@dataclass(frozen=True)
class FirmwareImage:
    data: bytes
    initial_sp: int
    reset_vector: int
    warnings: tuple[str, ...] = ()

    @classmethod
    def from_file(cls, path: str | Path) -> "FirmwareImage":
        return cls.from_bytes(Path(path).read_bytes())

    @classmethod
    def from_bytes(cls, data: bytes) -> "FirmwareImage":
        image_data = bytes(data)
        if not image_data:
            raise ValueError("firmware image is empty")
        if len(image_data) > MAX_APP_SIZE:
            raise ValueError(f"firmware image is too large: {len(image_data)} bytes")
        if len(image_data) < 8:
            raise ValueError("firmware image is too small to contain a vector table")

        initial_sp, reset_vector = struct.unpack_from("<II", image_data)
        if not (SRAM_BASE < initial_sp <= SRAM_BASE + SRAM_SIZE):
            raise ValueError(f"initial SP 0x{initial_sp:08X} is outside SRAM")

        warnings: list[str] = []
        if reset_vector < APP_BASE:
            warnings.append("当前 bin 疑似按 0x00000000 链接，不是 0x20000 APP 镜像。升级后 boot 可能无法跳转。")
        elif reset_vector >= APP_BASE + MAX_APP_SIZE:
            warnings.append(f"ResetVector 0x{reset_vector:08X} 超出预期 APP 区间，请确认链接脚本。")

        return cls(data=image_data, initial_sp=initial_sp, reset_vector=reset_vector, warnings=tuple(warnings))

    @property
    def size(self) -> int:
        return len(self.data)

    def sections(self, section_size: int = 1024) -> list[FirmwareSection]:
        if section_size <= 0 or section_size > 1024:
            raise ValueError("section size must be in 1..1024")
        return [
            FirmwareSection(number=index // section_size, data=self.data[index:index + section_size])
            for index in range(0, len(self.data), section_size)
        ]
