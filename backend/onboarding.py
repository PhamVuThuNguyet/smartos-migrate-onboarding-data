"""Parse an onboarding Excel workbook into contracts.

The data sheet is found by header names, not by the active sheet. The as-of
date is the day the file is read. This module does not call the API.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path

import openpyxl

# Column indexes are 1-based, matching openpyxl. The header row is detected, not fixed.
COL = {
    "stt": 2,
    "name": 3,
    "building": 4,
    "service": 5,
    "floor": 6,
    "room": 7,
    "email": 8,
    "area": 9,
    "rent": 10,
    "fee": 11,
    "currency": 12,
    "tax": 13,
    "start": 14,
    "end": 15,
    "contract_status": 17,
    "deposit_label": 18,
    "deposit_amount": 19,
    "cycle": 20,
    "fx": 24,
    "contract_no": 26,
    "activity": 27,
    "note": 28,
}

# Fallback address and coordinates for known buildings. Not a surveyed location.
BUILDING_DUMMY = {
    "ATHENA": {
        "address": "Tòa ATHENA, TP. Hồ Chí Minh",
        "city": "Hồ Chí Minh",
        "lng": 106.7008,
        "lat": 10.7796,
    },
    "ARTEMIS": {
        "address": "Tòa ARTEMIS, quận Thanh Xuân, Hà Nội",
        "city": "Hà Nội",
        "lng": 105.8195,
        "lat": 20.9987,
    },
    "738 VÕ VĂN KIỆT": {
        "address": "738 Võ Văn Kiệt, TP. Hồ Chí Minh",
        "city": "Hồ Chí Minh",
        "lng": 106.6672,
        "lat": 10.7554,
    },
}


def did(kind: str, key: str) -> str:
    return hashlib.md5(f"{kind}:{key}".encode("utf-8")).hexdigest()[:24]


def blank(value) -> bool:
    return value is None or str(value).strip() == ""


def text(value) -> str | None:
    if blank(value):
        return None
    return re.sub(r"\s+", " ", str(value)).strip()


def slug(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value)
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = folded.replace("đ", "d").replace("Đ", "d")
    raw = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")
    return raw or "unknown"


def as_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def parse_number(value):
    """Return a float, the string 'ACC', or None when the cell is not a number."""
    if blank(value) or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return None
        return float(value)
    raw = str(value).strip()
    if raw.upper() == "ACC":
        return "ACC"
    compact = raw.replace(" ", "")
    if "," in compact and "." in compact:
        compact = compact.replace(",", "")
    elif re.fullmatch(r"\d{1,3}(,\d{3})+", compact):
        compact = compact.replace(",", "")
    elif "," in compact and "." not in compact:
        compact = compact.replace(",", ".")
    try:
        return float(compact)
    except ValueError:
        return None


def split_emails(value) -> list[str]:
    if blank(value):
        return []
    parts = re.split(r"[;/,\n]+", str(value))
    return [part.strip().lower() for part in parts if "@" in part]


def norm_tax(value) -> str | None:
    if blank(value):
        return None
    return re.sub(r"\s+", "", str(value)) or None


def looks_like_company(name: str | None) -> bool:
    """True when the name looks like a legal entity, even if the tax-code cell is empty."""
    if not name:
        return False
    key = slug(name)
    tokens = set(key.split("-"))
    if tokens & {"cty", "ctcp", "tnhh", "ltd", "jsc", "corp", "bank", "company"}:
        return True
    phrases = (
        "cong-ty",
        "co-phan",
        "ngan-hang",
        "tap-doan",
        "chi-nhanh",
        "doanh-nghiep",
        "co-ltd",
    )
    return any(phrase in key for phrase in phrases)


def parse_cycle(value) -> tuple[str, int | None]:
    raw = (text(value) or "").lower()
    if "toàn bộ" in raw:
        return "full", None
    if "quý" in raw:
        return "months", 3
    if "năm" in raw:
        return "months", 12
    match = re.search(r"(\d+)", raw)
    if match and "tháng" in raw:
        return "months", int(match.group(1))
    if "tháng" in raw:
        return "months", 1
    return "months", 1


def unit_price_from_formula(formula, fallback_total, area) -> tuple[str, float | None]:
    """A formula `=unitPrice*I{row}` is per square meter. A numeric cell is a flat monthly total."""
    if isinstance(formula, str) and formula.startswith("="):
        if re.search(r"\*\s*I\d+", formula, re.I):
            match = re.search(r"([\d.]+)\s*\*", formula)
            if match:
                return "per-square-meter", float(match.group(1))
            if isinstance(fallback_total, float) and area:
                return "per-square-meter", fallback_total / area
        if isinstance(fallback_total, float):
            return "per-room", fallback_total
    if isinstance(fallback_total, float):
        return "per-room", fallback_total
    return "per-room", None


HEADER_MARKS = {
    "stt": ("stt",),
    "name": ("khach",),
    "building": ("toa",),
    "room": ("phong", "khu"),
    "rent": ("gia", "thue"),
}


def sheet_header_row(worksheet):
    """Find the header row by column names. Does not depend on which sheet is active."""
    for row_index in range(1, 11):
        matched = True
        for key, marks in HEADER_MARKS.items():
            folded = slug(str(worksheet.cell(row_index, COL[key]).value or ""))
            if not any(mark in folded for mark in marks):
                matched = False
                break
        if matched:
            return row_index
    return None


def find_data_sheet(workbook):
    for worksheet in workbook.worksheets:
        header_row = sheet_header_row(worksheet)
        if header_row:
            return worksheet, header_row
    raise ValueError(
        "Không thấy hàng tiêu đề đúng format cột: STT, khách, tòa, khu vực/phòng, đơn giá"
    )


def header_column(worksheet, header_row: int, marks: tuple[str, ...]) -> int | None:
    """Return the 1-based column whose header contains every mark."""
    for column in range(1, (worksheet.max_column or 1) + 1):
        folded = slug(str(worksheet.cell(header_row, column).value or ""))
        if folded and all(mark in folded for mark in marks):
            return column
    return None


def load_rows(path: Path):
    values_book = openpyxl.load_workbook(path, data_only=True)
    formulas_book = openpyxl.load_workbook(path, data_only=False)
    values, header_row = find_data_sheet(values_book)
    formulas = formulas_book[values.title]
    template_column = header_column(values, header_row, ("mau", "hop-dong"))
    # As-of is the run date. Excel cell P2 is often =TODAY(); openpyxl only sees the cached value.
    as_of = date.today()
    rows = []
    for index in range(header_row + 1, values.max_row + 1):
        item = {"row": index, "template": None}
        empty = True
        for key, column in COL.items():
            item[key] = values.cell(index, column).value
            if not blank(item[key]):
                empty = False
        if template_column:
            item["template"] = values.cell(index, template_column).value
            if not blank(item["template"]):
                empty = False
        item["rent_formula"] = formulas.cell(index, COL["rent"]).value
        item["fee_formula"] = formulas.cell(index, COL["fee"]).value
        if not empty:
            rows.append(item)
    return as_of, rows


def expand_room_names(label: str) -> list[str]:
    """Split ranges and combined labels into individual rooms.

    T501 -> T506 becomes T501..T506. 1A+B becomes 1A and 1B.
    T221-224 + Hlang becomes T221..T224 and Hành lang.
    A descriptive name such as a lobby stays one zone.
    """

    def room_code(value: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z]*\d+(?:\.\d+)?[A-Za-z]?", value.strip()))

    def area_alias(token: str) -> str:
        key = slug(token)
        aliases = {
            "hlang": "Hành lang",
            "hanh-lang": "Hành lang",
            "hanh lang": "Hành lang",
        }
        return aliases.get(key, token.strip())

    def expand_range(segment: str) -> list[str] | None:
        match = re.match(
            r"^([A-Za-z]*)(\d+)(?:\.(\d+))?\s*(?:->|→|–|—|-)\s*([A-Za-z]*)(\d+)(?:\.(\d+))?\s*(.*)$",
            segment.strip(),
            re.IGNORECASE,
        )
        if not match:
            return None
        prefix, major, minor, prefix_end, major_end, minor_end, suffix = match.groups()
        prefix = prefix.upper()
        prefix_end = prefix_end.upper() or prefix
        if prefix != prefix_end:
            return None
        suffix = re.sub(r"^[\s.+-]+", "", suffix or "").strip()
        if minor or minor_end:
            if not (minor and minor_end and major == major_end):
                return None
            start, end = int(minor), int(minor_end)
            if end < start or end - start > 80:
                return None
            names = [f"{prefix}{major}.{number}" for number in range(start, end + 1)]
        else:
            start, end = int(major), int(major_end)
            if end < start or end - start > 80:
                return None
            names = [f"{prefix}{number}" for number in range(start, end + 1)]
        if suffix:
            names.append(area_alias(suffix))
        return names

    def expand_segment(segment: str) -> list[str]:
        pieces = [piece.strip() for piece in re.split(r"\s*-\s*", segment) if piece.strip()]
        if len(pieces) > 2 and all(room_code(piece) for piece in pieces):
            return pieces
        expanded = expand_range(segment)
        if expanded:
            return expanded
        if len(pieces) == 2 and all(room_code(piece) for piece in pieces):
            return pieces
        return [segment.strip()]

    def expand_plus(segment: str) -> list[str]:
        bits = [bit.strip() for bit in re.split(r"\s*\+\s*", segment) if bit.strip()]
        if len(bits) == 1:
            return expand_segment(bits[0])
        sibling = re.fullmatch(r"(\d+)([A-Za-z](?:\.\d+)?)", bits[0])
        if sibling and all(re.fullmatch(r"[A-Za-z]", bit) for bit in bits[1:]):
            number, suffix = sibling.groups()
            return [f"{number}{suffix}"] + [f"{number}{bit.upper()}" for bit in bits[1:]]
        names = expand_segment(bits[0])
        names.extend(area_alias(bit) for bit in bits[1:])
        return names

    parts = [part.strip() for part in re.split(r"\s*(?:,|;|&|/)\s*", label) if part.strip()]
    if not parts:
        return [label]
    if len(parts) == 1:
        return expand_plus(parts[0])
    expanded: list[str] = []
    recognized = 0
    for part in parts:
        names = expand_plus(part)
        if names != [part] or room_code(part):
            recognized += 1
        expanded.extend(names)
    if recognized == len(parts):
        return expanded
    return [label]


def space_from_row(row) -> dict:
    area_num = parse_number(row["area"])
    area = area_num if isinstance(area_num, float) and area_num > 0 else None
    rent_num = parse_number(row["rent"])
    fee_raw = text(row["fee"])
    if fee_raw and "không thu" in fee_raw.lower():
        fee_num = 0.0
    else:
        fee_num = parse_number(row["fee"])
    price_type, unit_price = unit_price_from_formula(
        row["rent_formula"], rent_num if isinstance(rent_num, float) else None, area
    )
    return {
        "row": row["row"],
        "floor": text(row["floor"]) or "Không rõ tầng",
        "room": text(row["room"]) or f"Khu vực dòng {row['row']}",
        "area": area,
        "area_raw": text(row["area"]),
        "area_dummy": area is None,
        "rent": rent_num if isinstance(rent_num, float) else None,
        "rent_blocked": rent_num == "ACC",
        "price_type": price_type,
        "unit_price": unit_price,
        "fee": fee_num if isinstance(fee_num, float) else None,
        "fee_raw": fee_raw,
        "floor_dummy": blank(row["floor"]),
        "room_dummy": blank(row["room"]),
    }


def spaces_from_row(row) -> list[dict]:
    space = space_from_row(row)
    names = expand_room_names(space["room"])
    if len(names) <= 1:
        if names:
            space["room"] = names[0]
        return [space]
    count = len(names)
    clones = []
    for name in names:
        clone = dict(space)
        clone["room"] = name
        if space["area"]:
            clone["area"] = space["area"] / count
        if space["rent"] is not None:
            clone["rent"] = space["rent"] / count
        if isinstance(space["fee"], float):
            clone["fee"] = space["fee"] / count
        if space["price_type"] == "per-room" and space["unit_price"] is not None:
            clone["unit_price"] = space["unit_price"] / count
        clones.append(clone)
    return clones


def row_has_space(row) -> bool:
    return not all(blank(row[key]) for key in ("floor", "room", "area", "rent", "fee"))


def group_contracts(rows: list[dict]) -> list[dict]:
    """A row with STT starts a contract. A following row without STT joins it.

    The continuation becomes its own contract when the contract number, service,
    or start date differs from the contract above.
    """
    contracts: list[dict] = []
    current = None

    def start(row, inherited=None):
        source = inherited or {}
        contract = {
            "stt": row["stt"] if not blank(row["stt"]) else source.get("stt"),
            "name": text(row["name"]) or source.get("name"),
            "building": text(row["building"]) or source.get("building"),
            "service": text(row["service"]) or source.get("service"),
            "emails": split_emails(row["email"]) or list(source.get("emails") or []),
            "tax": norm_tax(row["tax"]) or source.get("tax"),
            "start": as_date(row["start"]) or source.get("start"),
            "end": as_date(row["end"]) or source.get("end"),
            "currency": text(row["currency"]) or source.get("currency"),
            "cycle": text(row["cycle"]) or source.get("cycle"),
            "deposit_label": row["deposit_label"] if not blank(row["deposit_label"]) else source.get("deposit_label"),
            "deposit_amount": row["deposit_amount"] if not blank(row["deposit_amount"]) else None,
            "fx": text(row["fx"]) or source.get("fx"),
            "contract_no": text(row["contract_no"]) or None,
            "template": text(row.get("template")) or source.get("template"),
            "contract_status": text(row["contract_status"]) or source.get("contract_status"),
            "activity": text(row["activity"]) or source.get("activity"),
            "note": text(row["note"]) or source.get("note"),
            "spaces": [],
            "warnings": [],
        }
        if inherited and contract["contract_no"] is None:
            contract["contract_no"] = source.get("contract_no")
        if row_has_space(row):
            contract["spaces"].extend(spaces_from_row(row))
        contracts.append(contract)
        return contract

    for row in rows:
        if not blank(row["stt"]):
            current = start(row)
            continue
        if current is None or not row_has_space(row):
            continue
        different_contract = text(row["contract_no"]) and text(row["contract_no"]) != current["contract_no"]
        different_service = text(row["service"]) and text(row["service"]) != current["service"]
        different_start = as_date(row["start"]) and as_date(row["start"]) != current["start"]
        if different_contract or different_service or different_start:
            current = start(row, current)
        else:
            current["spaces"].extend(spaces_from_row(row))
    return contracts


def currency_code(value, warnings: list[str]) -> str:
    raw = (value or "").upper()
    if "USD" in raw:
        return "usd"
    if "VN" in raw:
        return "vnd"
    warnings.append(f"Thiếu loại giá, dummy currency=vnd (ô: {value})")
    return "vnd"


def deposit_amount(contract, monthly_rent: float) -> tuple[float, list[str]]:
    warnings = []
    label = contract["deposit_label"]
    amount = parse_number(contract["deposit_amount"])
    if isinstance(amount, float):
        return amount, warnings
    label_num = parse_number(label)
    if label_num == "ACC":
        warnings.append("Cọc ghi ACC, dummy requiredDepositAmount=0")
        return 0.0, warnings
    if isinstance(label_num, float) and label_num > 36:
        warnings.append("Cột số tháng cọc đang là số tiền, dùng số đó làm tiền cọc")
        return label_num, warnings
    raw = (text(label) or "").lower()
    if "không" in raw:
        return 0.0, warnings
    match = re.search(r"(\d+)", raw)
    if match:
        months = int(match.group(1))
        if monthly_rent <= 0:
            warnings.append("Có số tháng cọc nhưng chưa có tiền thuê, dummy tiền cọc=0")
            return 0.0, warnings
        return months * monthly_rent, warnings
    warnings.append("Không đọc được tiền cọc, dummy requiredDepositAmount=0")
    return 0.0, warnings


def is_zone(service: str, space: dict) -> bool:
    """A space is a zone when its area is not a number. The service name is not used."""
    return bool(space["area_dummy"])
