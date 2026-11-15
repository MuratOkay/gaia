"""Semantic-type detectors: positive and negative fixtures, checksums included."""

from __future__ import annotations

import random

import pytest

from gaia.profiling.detectors import detect


def _types(values, kind="text"):
    return {h.semantic_type: h.match_rate for h in detect(values, kind)}


def _tckn(rng):
    d = [rng.randint(1, 9)] + [rng.randint(0, 9) for _ in range(8)]
    d10 = ((d[0] + d[2] + d[4] + d[6] + d[8]) * 7 - (d[1] + d[3] + d[5] + d[7])) % 10
    return "".join(map(str, [*d, d10, (sum(d) + d10) % 10]))


def _iban(rng):
    bban = f"{rng.randint(0, 99999):05d}0{rng.randint(0, 10**16 - 1):016d}"
    digits = "".join(str(int(c, 36)) for c in bban + "TR00")
    return f"TR{98 - int(digits) % 97:02d}{bban}"


def _luhn_card(rng):
    body = [4] + [rng.randint(0, 9) for _ in range(14)]
    total = 0
    for i, d in enumerate(reversed(body)):
        d = d * 2 if i % 2 == 0 else d
        total += d - 9 if d > 9 else d
    return "".join(map(str, body)) + str((10 - total % 10) % 10)


RNG = random.Random(7)


@pytest.mark.parametrize(
    ("name", "values"),
    [
        ("email", ["ayse.yilmaz12@example.com", "j.smith@example.org", "x_y+z@mail.example.net"]),
        ("url", ["https://example.com/a?b=1", "http://shop.example.org"]),
        ("iban", [_iban(RNG) for _ in range(20)]),
        ("card_number", [_luhn_card(RNG) for _ in range(20)]),
        ("national_id", [_tckn(RNG) for _ in range(20)]),
        ("phone", ["+90 532 111 22 33", "905321112233", "0212 555 12 12", "+1 (415) 555-0100"]),
        ("guid", ["6F9619FF-8B86-D011-B42D-00C04FC964FF", "{c56a4180-65aa-42ec-a945-5fd21dec0538}"]),
        ("hash", ["a" * 64, "0123456789abcdef" * 4]),
        ("ip_address", ["10.1.2.3", "192.168.0.1", "2001:db8::1"]),
        ("currency_code", ["TRY", "USD", "EUR"]),
        ("country_code", ["TR", "DE", "US"]),
        ("date_text", ["2025-01-31", "31.01.2025", "01/02/2024"]),
        ("datetime_text", ["2025-01-31T10:15:00", "2025-01-31 10:15:00+03:00"]),
        ("json", ['{"a": 1}', "[1, 2]"]),
        ("xml", ["<a><b>1</b></a>", "<root/>"]),
        ("numeric_text", ["15000.00", "25000.00", "-3.5"]),
        ("boolean_text", ["Y", "N", "Y"]),
    ],
)
def test_positive_fixtures(name, values):
    assert _types(values).get(name) == 1.0


@pytest.mark.parametrize(("name", "values"), [("date_int", ["20250131", "19991231"]),
                                              ("time_int", ["80000", "235959", "101500"])])
def test_integer_detectors(name, values):
    assert _types(values, "integer").get(name) == 1.0
    assert name not in _types(values, "text")  # only applied to integer columns


def test_checksums_reject_random_digits():
    rng = random.Random(1)
    digits11 = ["".join(str(rng.randint(1, 9)) for _ in range(11)) for _ in range(200)]
    digits16 = ["4" + "".join(str(rng.randint(0, 9)) for _ in range(15)) for _ in range(200)]
    iban_like = ["TR" + "".join(str(rng.randint(0, 9)) for _ in range(24)) for _ in range(200)]
    assert "national_id" not in _types(digits11)
    assert "card_number" not in _types(digits16)
    assert "iban" not in _types(iban_like)


@pytest.mark.parametrize(
    ("name", "values"),
    [
        ("email", ["Ayse Yilmaz", "not an email", "a@b"]),
        ("date_int", ["20251340", "12345678", "99999999"]),  # not calendar dates
        ("phone", ["900000001", "900000002", "12345678901"]),  # ids, not phones
        ("boolean_text", ["OK", "CN", "RT"]),
        ("country_code", ["TR"]),  # one value is not a list of countries
    ],
)
def test_negative_fixtures(name, values):
    kind = "integer" if name == "date_int" else "text"
    assert name not in _types(values, kind)


def test_rates_and_limits():
    mixed = ["a@example.com"] * 97 + ["broken"] * 3
    assert _types(mixed)["email"] == pytest.approx(0.97)
    assert detect([], "text") == [] and detect(["x"], "bit") == []
    assert detect([None, "", "a@example.com"], "text")[0].sample_size == 1
