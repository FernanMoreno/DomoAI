from __future__ import annotations

import pytest

from domoai.adapters.modbus.codec import decode_point, encode_point
from domoai.adapters.modbus.config import ModbusPoint


def test_codec_converts_signed_scaled_register() -> None:
    point = ModbusPoint(area="input_register", address=0, data_type="int16", scale=0.1)

    assert decode_point(point, (215,)) == 21.5
    assert encode_point(point, 21.5) == (215,)


def test_codec_round_trips_float32_word_and_byte_orders() -> None:
    for byte_order in ("big", "little"):
        for word_order in ("big", "little"):
            point = ModbusPoint(
                area="holding_register",
                address=0,
                data_type="float32",
                byte_order=byte_order,
                word_order=word_order,
            )
            encoded = encode_point(point, 21.5)
            assert len(encoded) == 2
            assert decode_point(point, encoded) == pytest.approx(21.5)


def test_codec_rejects_wrong_width_and_non_finite_values() -> None:
    point = ModbusPoint(area="holding_register", address=0, data_type="uint16")

    with pytest.raises(ValueError, match="value count"):
        decode_point(point, ())
    with pytest.raises(ValueError, match="finite"):
        encode_point(point, float("nan"))


def test_codec_rejects_boolean_register_and_out_of_range_integer() -> None:
    with pytest.raises(ValueError, match="boolean"):
        decode_point(
            ModbusPoint(area="coil", address=0, data_type="bool"),
            (1,),
        )
    with pytest.raises(ValueError, match="range"):
        encode_point(ModbusPoint(area="holding_register", address=0, data_type="uint16"), 65536)
