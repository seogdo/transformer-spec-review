"""Small, explicit data contract for the Day 1 deterministic reference engine.

Input dictionaries keep the future HTTP/PDF adapter independent of the engine.
The adapter must provide document identity, human-selected document precedence,
and source locations.  An ``approved`` string is demo metadata, not a verified
digital signature or evidence that a real person approved a document.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import json
import re
from typing import Any, Mapping


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    canonical_unit: str
    factors: Mapping[str, Decimal]
    allowed_values: frozenset[str] = frozenset()
    value_aliases: Mapping[str, str] = field(default_factory=dict)


# These units and categories define this synthetic exercise only.  They do not
# enumerate every valid transformer configuration or impose an industry limit.
FIELD_SPECS = (
    FieldSpec("rated_power", "정격용량", "kVA", {"kVA": Decimal(1), "MVA": Decimal(1000)}),
    FieldSpec("hv_voltage", "고압측 정격전압", "V", {"V": Decimal(1), "kV": Decimal(1000)}),
    FieldSpec("lv_voltage", "저압측 정격전압", "V", {"V": Decimal(1), "kV": Decimal(1000)}),
    FieldSpec("frequency", "정격주파수", "Hz", {"Hz": Decimal(1)}),
    FieldSpec("phases", "상수", "count", {"count": Decimal(1)}),
    FieldSpec("vector_group", "결선 그룹", "", {}, frozenset({"Dyn11", "Dyn1", "Yyn0", "Yd11", "YNd11", "YNyn0"})),
    FieldSpec("cooling_type", "냉각 방식", "", {}, frozenset({"ONAN", "ONAF", "OFAF", "ODAF", "AN", "AF"})),
    FieldSpec("fan_supply_voltage", "팬 보조전원 정격전압", "V", {"V": Decimal(1)}),
    FieldSpec("tap_changer_type", "탭절환기 방식", "", {}, frozenset({"OLTC", "DETC"}), {
        "부하시": "OLTC", "부하시 탭절환기": "OLTC", "부하시탭절환기": "OLTC",
        "무전압": "DETC", "무전압 탭절환기": "DETC", "무전압탭절환기": "DETC",
    }),
    FieldSpec("installation_type", "설치 구분", "", {}, frozenset({"옥내형", "옥외형"}), {
        "옥내": "옥내형", "실내": "옥내형", "INDOOR": "옥내형",
        "옥외": "옥외형", "실외": "옥외형", "OUTDOOR": "옥외형",
    }),
)
FIELD_KEYS = tuple(spec.key for spec in FIELD_SPECS)
STATUSES = ("MATCH", "MISMATCH", "MISSING", "NEEDS_REVIEW")
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")
# Resource-protection limits for the parser, not electrical acceptance limits.
# A short input such as 1e999999999 must not expand into a huge output string.
MAX_NUMERIC_TEXT_LENGTH = 256
MAX_SIGNIFICANT_DIGITS = 100
MAX_ABS_DECIMAL_EXPONENT = 1000


class UncertainValue(ValueError):
    """A value must be reviewed by a human instead of guessed or coerced."""


@dataclass(frozen=True)
class NormalizedValue:
    comparable: Decimal | str
    value: str
    unit: str
    condition: str


def canonical_enum_value(spec: FieldSpec, value: Any) -> str:
    """Resolve only explicitly supported, whole enum values.

    No substring search, fuzzy match, negation removal, or engineering inference
    is permitted. Alias-enabled fields tolerate case and repeated whitespace,
    but preserve the original entry/quote for evidence. Other existing enum
    fields keep their original exact-match behavior. In particular, '무부하',
    '옥외형 부속함', and '옥내 또는 옥외' are not aliases of a single value.
    """
    if not isinstance(value, str) or len(value) > MAX_NUMERIC_TEXT_LENGTH:
        raise UncertainValue("지원하는 단일 열거값을 확인할 수 없습니다.")
    raw = value.strip()
    if raw in spec.allowed_values:
        return raw
    if spec.value_aliases:
        token = " ".join(raw.split()).casefold()
        aliases = {**{canonical: canonical for canonical in spec.allowed_values},
                   **spec.value_aliases}
        for alias, canonical in aliases.items():
            if token == " ".join(alias.split()).casefold() and canonical in spec.allowed_values:
                return canonical
    raise UncertainValue("지원하는 단일 열거값 또는 명시된 동의 표기가 아닙니다.")


def is_missing(entry: Any) -> bool:
    """Treat only absent/null/blank scalar values as missing, not malformed data."""
    if entry is None:
        return True
    if not isinstance(entry, Mapping):
        return False
    value = entry.get("value")
    return value is None or (isinstance(value, str) and not value.strip())


def source_for(document: Mapping[str, Any], entry: Any) -> dict[str, Any] | None:
    """Check source metadata presence only; original PDF text is not available.

    A page number must be a positive integer.  Booleans are rejected explicitly
    because bool is a subclass of int in Python.  Quotes are not fabricated when
    absent.  Whether a quote really occurs on that page remains unverified.
    """
    if not isinstance(entry, Mapping):
        return None
    page, quote = entry.get("page"), entry.get("quote")
    doc_id, title = document.get("id"), document.get("title")
    if (
        not isinstance(page, int) or isinstance(page, bool) or page <= 0
        or not isinstance(quote, str) or not quote.strip()
        or not isinstance(doc_id, str) or not doc_id.strip()
        or not isinstance(title, str) or not title.strip()
    ):
        return None
    return {"document_id": doc_id, "title": title, "page": page, "quote": quote}


def normalize(spec: FieldSpec, entry: Any) -> NormalizedValue:
    """Normalize one unambiguous field without rounding or engineering guesses.

    Strings such as ``380-440``, ``380/440``, ``>=380`` and lists are deliberately
    not interpreted.  Context must be explicitly identical on both sides; an
    absent context is not silently equated to a supplied operating condition.
    """
    if not isinstance(entry, Mapping) or is_missing(entry):
        raise UncertainValue("값이 없거나 필드 구조가 올바르지 않습니다.")
    value, unit = entry.get("value"), entry.get("unit", "")
    if not isinstance(unit, str):
        raise UncertainValue("단위가 하나의 문자열로 명시되지 않았습니다.")
    condition = entry.get("condition", "")
    if not isinstance(condition, (str, Mapping)):
        raise UncertainValue("비교 조건이 단일 문자열 또는 조건 객체가 아닙니다.")
    try:
        context = json.dumps(condition, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        raise UncertainValue("비교 조건을 명확하게 해석할 수 없습니다.") from None
    if spec.allowed_values:
        if unit.strip():
            raise UncertainValue("열거값에 수치 단위가 포함됐습니다.")
        canonical = canonical_enum_value(spec, value)
        return NormalizedValue(canonical, canonical, "", context)
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise UncertainValue("복수값 또는 수치로 분리할 수 없는 값입니다.")
    raw = str(value).strip()
    if len(raw) > MAX_NUMERIC_TEXT_LENGTH:
        raise UncertainValue("수치 표현이 파서 지원 길이를 초과했습니다. 공학적 부적합 판정은 아닙니다.")
    if not _NUMBER.fullmatch(raw):
        raise UncertainValue("범위·복수값·비교식 또는 해석할 수 없는 수치입니다.")
    if unit.strip() not in spec.factors:
        raise UncertainValue("지원하지 않거나 누락된 단위입니다.")
    try:
        number = Decimal(raw)
        # Avoid the active Decimal context rounding large significant numbers.
        # Supported conversion factors are powers of ten, so shifting the tuple
        # exponent performs an exact conversion even above 28 significant digits.
        factor = spec.factors[unit.strip()]
        shift = factor.adjusted()
        parts = number.as_tuple()
        if (
            len(parts.digits) > MAX_SIGNIFICANT_DIGITS
            or abs(parts.exponent) > MAX_ABS_DECIMAL_EXPONENT
            or abs(parts.exponent + shift) > MAX_ABS_DECIMAL_EXPONENT
            or abs(number.adjusted() + shift) > MAX_ABS_DECIMAL_EXPONENT
        ):
            raise UncertainValue("수치 정밀도·지수가 파서 지원 범위를 초과했습니다. 공학적 부적합 판정은 아닙니다.")
        number = Decimal((parts.sign, parts.digits, parts.exponent + shift))
    except UncertainValue:
        raise
    except (InvalidOperation, ValueError, OverflowError):
        raise UncertainValue("수치를 안전하게 변환할 수 없습니다.") from None
    if not number.is_finite() or number <= 0:
        raise UncertainValue("양의 유한한 수치만 이 데모에서 비교합니다.")
    if spec.key == "phases" and number != number.to_integral_value():
        raise UncertainValue("상수가 정수가 아닙니다.")
    # Keep JSON portable by returning canonical decimal strings, never floats.
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return NormalizedValue(number, text, spec.canonical_unit, context)
