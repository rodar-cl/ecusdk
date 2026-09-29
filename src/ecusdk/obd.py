"""DTCs y codecs OBD-II mínimos."""

from __future__ import annotations

from dataclasses import dataclass, field

from ecusdk.errors import ObdError


@dataclass(slots=True)
class DtcStore:
    codes: set[str] = field(default_factory=lambda: set[str]())

    def add(self, code: str) -> None:
        _validate_dtc(code)
        self.codes.add(code.upper())

    def remove(self, code: str) -> None:
        self.codes.discard(code.upper())

    def clear(self) -> None:
        self.codes.clear()

    def list(self) -> list[str]:
        return sorted(self.codes)


def _validate_dtc(code: str) -> None:
    if (
        len(code) != 5
        or code[0].upper() not in "PBCU"
        or code[1].upper() not in "0123"
        or any(digit.upper() not in "0123456789ABCDEF" for digit in code[2:])
    ):
        raise ValueError(f"DTC inválido: {code}")


def encode_dtc(code: str) -> bytes:
    _validate_dtc(code)
    prefix = "PBCU".index(code[0].upper())
    suffix = code[1:].upper()
    return bytes(
        (
            (prefix << 6) | int(suffix[0], 16) << 4 | int(suffix[1], 16),
            int(suffix[2:], 16),
        )
    )


def decode_dtc(data: bytes) -> str:
    if len(data) != 2:
        raise ValueError("un DTC requiere dos bytes")
    prefix = "PBCU"[(data[0] >> 6) & 0x03]
    return f"{prefix}{data[0] & 0x3F:02X}{data[1]:02X}"


def obd_request(mode: int, pid: int = 0) -> bytes:
    if not 0 <= mode <= 0xFF or not 0 <= pid <= 0xFF:
        raise ObdError("mode y pid deben ser bytes")
    return bytes((mode, pid))
