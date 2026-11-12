"""float32 存储与余弦的数值边界。"""

import math
import struct

import pytest

from litgleaner.domain.embedding import cosine, pack_vector, unpack_vector


def test_vector_roundtrip_is_explicit_little_endian_float32():
    raw = pack_vector((1.0, -0.5, 0.0), 3)
    assert raw == b"\x00\x00\x80?\x00\x00\x00\xbf\x00\x00\x00\x00"
    assert unpack_vector(raw, 3) == (1, -0.5, 0)
    assert unpack_vector(pack_vector((0.1,), 1), 1)[0] == pytest.approx(0.1)


@pytest.mark.parametrize(
    "values,dimensions",
    [
        ((), 0),
        ((1.0,), 2),
        ((True,), 1),
        ((1.0,), True),
        ((0.0, 0.0), 2),
        ((1e-60,), 1),
        ((1e100,), 1),
        ((math.inf,), 1),
        ((math.nan,), 1),
        (("bad",), 1),
        ((1j,), 1),
    ],
)
def test_invalid_vectors_cannot_be_serialized(values, dimensions):
    with pytest.raises(ValueError):
        pack_vector(values, dimensions)


@pytest.mark.parametrize(
    "raw,dimensions",
    [
        (b"", 1),
        (b"12345", 1),
        (b"1234", 0),
        (b"1234", True),
        (struct.pack("<f", math.inf), 1),
        (struct.pack("<f", math.nan), 1),
        (struct.pack("<f", 0.0), 1),
        (b"1234", 65537),
    ],
)
def test_invalid_stored_bytes_are_rejected(raw, dimensions):
    with pytest.raises(ValueError):
        unpack_vector(raw, dimensions)


def test_cosine_has_direction_scale_and_extreme_magnitude_stability():
    assert cosine((1, 0), (0, 1)) == 0
    assert cosine((1, 0), (-2, 0)) == -1
    assert cosine((3, 4), (6, 8)) == pytest.approx(1)
    assert cosine((1e308,) * 4, (1e308,) * 4) == pytest.approx(1)
    assert cosine((1e-300,) * 4, (1e300,) * 4) == pytest.approx(1)
    assert cosine((1, 1), (1, 0)) == pytest.approx(1 / math.sqrt(2))


@pytest.mark.parametrize(
    "left,right",
    [
        ((), ()),
        ((0, 0), (1, 0)),
        ((1,), (1, 0)),
        ((math.nan,), (1,)),
        ((1,), (math.inf,)),
    ],
)
def test_undefined_cosine_is_rejected(left, right):
    with pytest.raises(ValueError):
        cosine(left, right)
