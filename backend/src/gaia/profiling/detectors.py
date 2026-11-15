"""Semantic-type detectors over sampled values (Phase 3A).

A detector answers "what kind of value is this?" for one value; a column's
verdict is the share of its non-null sampled values that validate
(``DetectorHit.match_rate``). Where a format has a checksum, the detector uses
it, so random digits do not pass as identifiers:

* IBAN — ISO 13616 mod-97 and the country's length;
* card numbers — Luhn;
* Turkish national ID (TCKN) — both check digits;
* dates — must be real calendar dates, not just eight digits.

Detectors are applied only to plausible column types (``date_int`` to integers,
text formats to text). Results below ``MIN_RATE`` are dropped; the reasoning
side decides what is strong (>= 0.95) or weak (>= 0.80).

Generic types (``numeric_text``) are reported next to specific ones; the
evidence ledger lets the specific one win (3B).
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Callable, Sequence
from datetime import date, datetime

from gaia.profiling.summary import DetectorHit

__all__ = ["DETECTORS", "MIN_RATE", "detect"]

MIN_RATE = 0.5
MAX_VALUES = 1000

_EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_URL = re.compile(r"^(https?|ftp)://[^\s/$.?#].[^\s]*$", re.IGNORECASE)
_GUID = re.compile(
    r"^\{?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}?$"
)
_HASH = re.compile(r"^([0-9a-fA-F]{32}|[0-9a-fA-F]{40}|[0-9a-fA-F]{64}|[0-9a-fA-F]{128})$")
_PHONE_CHARS = re.compile(r"^\+?[\d\s().\-]{7,24}$")
_NUMBER = re.compile(r"^[+-]?(\d+([.,]\d+)?|[.,]\d+)$")
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DMY_DATE = re.compile(r"^(\d{1,2})[./](\d{1,2})[./](\d{4})$")
_ISO_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?([+-]\d{2}:?\d{2}|Z)?$"
)

_IBAN_LENGTHS = {
    "TR": 26,
    "DE": 22,
    "GB": 22,
    "FR": 27,
    "NL": 18,
    "IT": 27,
    "ES": 24,
    "AT": 20,
    "BE": 16,
    "CH": 21,
    "PL": 28,
    "SE": 24,
    "DK": 18,
    "NO": 15,
    "FI": 18,
    "IE": 22,
    "PT": 25,
    "GR": 27,
    "LU": 20,
    "CZ": 24,
    "HU": 28,
    "RO": 24,
    "BG": 22,
    "HR": 21,
    "SI": 19,
    "SK": 24,
    "LT": 20,
    "LV": 21,
    "EE": 20,
    "CY": 28,
    "MT": 31,
    "SA": 24,
    "AE": 23,
    "QA": 29,
    "KW": 30,
    "AZ": 28,
    "GE": 22,
    "UA": 29,
}
# ISO 4217 codes in common circulation (the full list changes rarely; unknown
# codes simply do not validate).
_CURRENCIES = frozenset(
    """
AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BRL BSD BTN BWP
BYN BZD CAD CDF CHF CLP CNY COP CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP
GEL GHS GIP GMD GNF GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR
KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK
MXN MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR
SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH
UGX USD UYU UZS VES VND VUV WST XAF XCD XOF XPF YER ZAR ZMW ZWL
""".split()
)
_COUNTRIES = frozenset(
    """
AD AE AF AG AL AM AO AR AT AU AZ BA BB BD BE BF BG BH BI BJ BN BO BR BS BT BW BY BZ CA CD CF
CG CH CI CL CM CN CO CR CU CV CY CZ DE DJ DK DM DO DZ EC EE EG ER ES ET FI FJ FR GA GB GD GE
GH GM GN GQ GR GT GW GY HK HN HR HT HU ID IE IL IN IQ IR IS IT JM JO JP KE KG KH KI KM KN KP
KR KW KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MG MK ML MM MN MR MT MU MV MW MX MY MZ
NA NE NG NI NL NO NP NZ OM PA PE PG PH PK PL PT PY QA RO RS RU RW SA SB SC SD SE SG SI SK SL
SM SN SO SR SS ST SV SY SZ TD TG TH TJ TL TM TN TO TR TT TV TW TZ UA UG US UY UZ VA VC VE VN
VU WS YE ZA ZM ZW
""".split()
)
_BOOLEAN = frozenset(
    {
        "y",
        "n",
        "yes",
        "no",
        "t",
        "f",
        "true",
        "false",
        "0",
        "1",
        "e",
        "h",
        "evet",
        "hayir",
        "hayır",
        "on",
        "off",
    }
)


def _iban(v: str) -> bool:
    s = v.replace(" ", "").upper()
    if len(s) < 15 or not s[:2].isalpha() or not s[2:4].isdigit():
        return False
    expected = _IBAN_LENGTHS.get(s[:2])
    if expected is not None and len(s) != expected:
        return False
    if not s.isalnum():
        return False
    digits = "".join(str(int(ch, 36)) for ch in s[4:] + s[:4])
    return int(digits) % 97 == 1


def _luhn(v: str) -> bool:
    s = re.sub(r"[\s\-]", "", v)
    if not s.isdigit() or not 13 <= len(s) <= 19:
        return False
    total = 0
    for i, ch in enumerate(reversed(s)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _tckn(v: str) -> bool:
    s = v.strip()
    if len(s) != 11 or not s.isdigit() or s[0] == "0":
        return False
    d = list(map(int, s))
    d10 = ((d[0] + d[2] + d[4] + d[6] + d[8]) * 7 - (d[1] + d[3] + d[5] + d[7])) % 10
    return d[9] == d10 and d[10] == sum(d[:10]) % 10


def _phone(v: str) -> bool:
    if not _PHONE_CHARS.match(v):
        return False
    digits = re.sub(r"\D", "", v)
    if not 10 <= len(digits) <= 15:
        return False
    if v.strip().startswith("+"):
        return True
    # Without "+": only national formats that are clearly phones (TR mobile/landline).
    return bool(re.match(r"^(90|0)?[2-5]\d{9}$", digits))


def _date_text(v: str) -> bool:
    s = v.strip()
    m = _ISO_DATE.match(s)
    try:
        if m:
            date(int(m[1]), int(m[2]), int(m[3]))
            return 1900 <= int(m[1]) <= 2100
        m = _DMY_DATE.match(s)
        if m:
            date(int(m[3]), int(m[2]), int(m[1]))
            return 1900 <= int(m[3]) <= 2100
    except ValueError:
        return False
    return False


def _date_int(v: str) -> bool:
    s = v.strip()
    if len(s) != 8 or not s.isdigit():
        return False
    try:
        d = date(int(s[:4]), int(s[4:6]), int(s[6:]))
    except ValueError:
        return False
    return 1900 <= d.year <= 2100


def _time_int(v: str) -> bool:
    s = v.strip()
    if not s.isdigit() or not 5 <= len(s) <= 6:
        return False
    n = int(s)
    return n // 10000 <= 23 and (n // 100) % 100 <= 59 and n % 100 <= 59


def _datetime_text(v: str) -> bool:
    s = v.strip()
    if not _ISO_DATETIME.match(s):
        return False
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _json(v: str) -> bool:
    s = v.strip()
    if not s or s[0] not in "{[":
        return False
    try:
        json.loads(s)
        return True
    except ValueError:
        return False


def _xml(v: str) -> bool:
    s = v.strip()
    if not (s.startswith("<") and s.endswith(">")):
        return False
    from xml.etree import ElementTree

    try:
        ElementTree.fromstring(s)  # noqa: S314 - parses our own sample, never resolved
        return True
    except ElementTree.ParseError:
        return False


def _ip(v: str) -> bool:
    try:
        ipaddress.ip_address(v.strip())
        return True
    except ValueError:
        return False


# (type, applies to: text | integer | any, validator)
DETECTORS: tuple[tuple[str, str, Callable[[str], bool]], ...] = (
    ("email", "text", lambda v: bool(_EMAIL.match(v.strip()))),
    ("url", "text", lambda v: bool(_URL.match(v.strip()))),
    ("iban", "text", _iban),
    ("card_number", "text", _luhn),
    ("national_id", "text", _tckn),
    ("phone", "text", _phone),
    ("guid", "text", lambda v: bool(_GUID.match(v.strip()))),
    ("hash", "text", lambda v: bool(_HASH.match(v.strip()))),
    ("ip_address", "text", _ip),
    ("currency_code", "text", lambda v: v.strip().upper() in _CURRENCIES and len(v.strip()) == 3),
    ("country_code", "text", lambda v: v.strip().upper() in _COUNTRIES and len(v.strip()) == 2),
    ("date_text", "text", _date_text),
    ("datetime_text", "text", _datetime_text),
    ("json", "text", _json),
    ("xml", "text", _xml),
    ("numeric_text", "text", lambda v: bool(_NUMBER.match(v.strip()))),
    ("date_int", "integer", _date_int),
    ("time_int", "integer", _time_int),
)


def detect(values: Sequence[str], kind: str) -> list[DetectorHit]:
    """Detector hits for one column's non-null sampled values.

    ``kind`` is ``text`` or ``integer`` (other kinds get no detectors).
    """
    sample = [str(v) for v in values if v is not None and str(v).strip() != ""][:MAX_VALUES]
    if not sample or kind not in ("text", "integer"):
        return []
    hits: list[DetectorHit] = []
    for name, applies, check in DETECTORS:
        if applies != kind:
            continue
        rate = sum(1 for v in sample if check(v)) / len(sample)
        if rate >= MIN_RATE:
            hits.append(DetectorHit(name, round(rate, 4), len(sample)))
    # Yes/no text: a small closed set of boolean tokens.
    distinct = {v.strip().lower() for v in sample}
    if kind == "text" and 1 <= len(distinct) <= 3 and distinct <= _BOOLEAN:
        hits.append(DetectorHit("boolean_text", 1.0, len(sample)))
    # Country and currency lists overlap with ordinary two/three-letter codes;
    # they only count with a reasonable number of distinct values.
    hits = [
        h
        for h in hits
        if h.semantic_type not in ("country_code", "currency_code") or len(distinct) >= 2
    ]
    return sorted(hits, key=lambda h: (-h.match_rate, h.semantic_type))
