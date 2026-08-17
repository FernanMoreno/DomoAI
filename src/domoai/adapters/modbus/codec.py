"""Pure Modbus register encoding and canonical value conversion."""

from __future__ import annotations

import math
import struct

from domoai.adapters.modbus.config import ModbusPoint

RawValue = bool | int


def decode_point(point: ModbusPoint, values: tuple[RawValue, ...]) -> bool | int | float:
    if len(values) != point.register_count:
        raise ValueError("Modbus response has an unexpected value count")
    if point.data_type == "bool":
        if not isinstance(values[0], bool):
            raise ValueError("Modbus boolean response is not boolean")
        return values[0]
    registers = _validate_registers(values)
    raw: int | float
    if point.data_type == "uint16":
        raw = _decode_integer(registers[0], signed=False, byte_order=point.byte_order)
    elif point.data_type == "int16":
        raw = _decode_integer(registers[0], signed=True, byte_order=point.byte_order)
    else:
        raw = _decode_float32(registers, point)
    value = raw * point.scale + point.offset
    if not math.isfinite(value):
        raise ValueError("Modbus conversion produced a non-finite value")
    if point.data_type in {"uint16", "int16"} and float(value).is_integer():
        return int(value)
    return value


def encode_point(point: ModbusPoint, value: bool | int | float) -> tuple[RawValue, ...]:
    if point.data_type == "bool":
        if not isinstance(value, bool):
            raise ValueError("Modbus boolean command requires a boolean")
        return (value,)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Modbus numeric command requires a number")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError("Modbus command must be finite")
    raw = (numeric - point.offset) / point.scale
    if not math.isfinite(raw):
        raise ValueError("Modbus command conversion produced a non-finite value")
    if point.data_type in {"uint16", "int16"} and not raw.is_integer():
        raise ValueError("Modbus integer command conversion is not integral")
    if point.data_type == "uint16":
        if not 0 <= raw <= 65535:
            raise ValueError("Modbus uint16 command is out of range")
        return (_encode_integer(int(raw), signed=False, byte_order=point.byte_order),)
    if point.data_type == "int16":
        if not -32768 <= raw <= 32767:
            raise ValueError("Modbus int16 command is out of range")
        return (_encode_integer(int(raw), signed=True, byte_order=point.byte_order),)
    try:
        packed = struct.pack(">f", raw)
    except (OverflowError, struct.error) as error:
        raise ValueError("Modbus float32 command is out of range") from error
    words = [packed[:2], packed[2:]]
    if point.byte_order == "little":
        words = [word[::-1] for word in words]
    if point.word_order == "little":
        words.reverse()
    return tuple(int.from_bytes(word, byteorder="big") for word in words)


def _validate_registers(values: tuple[RawValue, ...]) -> tuple[int, ...]:
    registers: list[int] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 65535:
            raise ValueError("Modbus register response is outside 0..65535")
        registers.append(value)
    return tuple(registers)


def _decode_integer(value: int, *, signed: bool, byte_order: str) -> int:
    raw = value.to_bytes(2, byteorder="big")
    if byte_order == "little":
        raw = raw[::-1]
    return int.from_bytes(raw, byteorder="big", signed=signed)


def _encode_integer(value: int, *, signed: bool, byte_order: str) -> int:
    raw = value.to_bytes(2, byteorder="big", signed=signed)
    if byte_order == "little":
        raw = raw[::-1]
    return int.from_bytes(raw, byteorder="big")


def _decode_float32(registers: tuple[int, ...], point: ModbusPoint) -> float:
    words = [register.to_bytes(2, byteorder="big") for register in registers]
    if point.byte_order == "little":
        words = [word[::-1] for word in words]
    if point.word_order == "little":
        words.reverse()
    return float(struct.unpack(">f", b"".join(words))[0])
