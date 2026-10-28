"""Read the onboarding template, validate it, and group booked rows into bookings.

Checks run in two layers. File errors (E01-E03) stop the read. Row errors are
collected so the user fixes the file in one pass. The as-of date is the day the
file is read. This module does not call the API.
"""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path

import openpyxl

# Template columns, in order. A header matches when it starts with the label; `*` is ignored.
COLUMNS = (
    ("building", "Toà nhà"),
    ("floor", "Tầng"),
    ("room", "Khu vực/phòng"),
    ("area", "Diện tích (m2)"),
    ("service", "Dịch vụ cung cấp"),
    ("rent", "Đơn giá cho thuê/tháng"),
    ("fee", "Đơn giá phí dịch vụ mặc định/tháng"),
    ("currency", "Đơn vị tiền tệ"),
    ("fx", "Tỷ giá"),
    ("name", "Tên khách hàng"),
    ("tax", "MST"),
    ("email", "Email"),
    ("start", "Ngày bắt đầu"),
    ("end", "Ngày kết thúc"),
    ("deposit", "Tiền cọc"),
    ("cycle", "Kỳ thanh toán"),
    ("contract_no", "Số hợp đồng"),
    ("template", "Mẫu hợp đồng"),
    ("note", "Ghi chú đặt chỗ"),
    ("main", "Đặt chỗ chính"),
)
LABELS = dict(COLUMNS)
REQUIRED = ("building", "room", "area", "service", "rent")
# Rows of one booking must agree on these columns (E12).
BOOKING_FIELDS = ("email", "fx", "deposit", "contract_no", "template", "note", "main")
HEADER_SCAN_ROWS = 10
HEADER_SCAN_COLUMNS = 60
TRUE_FLAGS = {"co", "x", "true", "yes", "1"}
FALSE_FLAGS = {"", "khong", "false", "no", "0"}


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


def issue(code: str, message: str, row: int | None = None, column: str | None = None) -> dict:
    return {"code": code, "row": row, "column": column, "message": message}


def parse_number(value) -> float | None:
    if blank(value) or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return None
        return float(value)
    compact = str(value).strip().replace(" ", "")
    if "," in compact and "." in compact:
        compact = compact.replace(",", "")
    elif re.fullmatch(r"\d{1,3}(,\d{3})+", compact):
        compact = compact.replace(",", "")
    elif "," in compact:
        compact = compact.replace(",", ".")
    try:
        return float(compact)
    except ValueError:
        return None


def parse_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = text(value) or ""
    for pattern in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, pattern).date()
        except ValueError:
            continue
    return None


def parse_currency(value) -> str | None:
    return {"vnd": "VND", "usd": "USD"}.get(slug(str(value or "")))


def parse_flag(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    key = slug(str(value or "")) if not blank(value) else ""
    if key in TRUE_FLAGS:
        return True
    if key in FALSE_FLAGS:
        return False
    return None


def parse_cycle(value) -> tuple[str, int | None] | None:
    """Return ("months", n) or ("full", None). None when the cell is not a known cycle."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return ("months", int(value)) if value > 0 and float(value).is_integer() else None
    raw = (text(value) or "").lower()
    if "toàn bộ" in raw:
        return "full", None
    match = re.search(r"\d+", raw)
    count = int(match.group()) if match else 1
    if count <= 0:
        return None
    if "quý" in raw:
        return "months", 3 * count
    if "năm" in raw:
        return "months", 12 * count
    if "tháng" in raw or re.fullmatch(r"\d+", raw):
        return "months", count
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


def header_matches(value, label: str) -> bool:
    if blank(value):
        return False
    found = slug(str(value).replace("*", ""))
    expected = slug(label)
    return found == expected or found.startswith(expected + "-")


def locate_header(workbook):
    """The sheet and header row that match the most template labels."""
    best = (0, workbook.worksheets[0], 1)
    for sheet in workbook.worksheets:
        last_column = min(sheet.max_column or 1, HEADER_SCAN_COLUMNS)
        for row in range(1, min(sheet.max_row or 1, HEADER_SCAN_ROWS) + 1):
            cells = [sheet.cell(row, column).value for column in range(1, last_column + 1)]
            hits = sum(1 for _key, label in COLUMNS if any(header_matches(cell, label) for cell in cells))
            if hits > best[0]:
                best = (hits, sheet, row)
    return best[1], best[2]


def check_file(sheet, header_row: int) -> tuple[int, list[dict]]:
    """E02 for each template column out of place, E03 for each merged range. Returns the first column."""
    errors = []
    first_column = 1
    while first_column < HEADER_SCAN_COLUMNS and blank(sheet.cell(header_row, first_column).value):
        first_column += 1
    for offset, (_key, label) in enumerate(COLUMNS):
        cell = sheet.cell(header_row, first_column + offset)
        if header_matches(cell.value, label):
            continue
        found = text(cell.value)
        where = f'ô {cell.coordinate} đang là "{found}"' if found else f"ô {cell.coordinate} đang trống"
        errors.append(issue("E02", f'Cột "{label}" không đúng mẫu ({where})', column=label))
    for merged in sheet.merged_cells.ranges:
        if merged.max_row >= header_row:
            errors.append(issue("E03", f"Ô {merged.coord} đang bị merge, vui lòng bỏ merge"))
    return first_column, errors


def read_row(number: int, raw: dict) -> tuple[dict, list[dict]]:
    errors: list[dict] = []

    def fail(code: str, key: str, message: str):
        errors.append(issue(code, message, number, LABELS[key]))

    for key in REQUIRED:
        if blank(raw[key]):
            fail("E04", key, f'Dòng {number}: cột "{LABELS[key]}" bắt buộc nhập')

    area = parse_number(raw["area"])
    if not blank(raw["area"]) and not (area and area > 0):
        fail("E05", "area", f"Dòng {number}: Diện tích phải là số lớn hơn 0")
    rent = parse_number(raw["rent"])
    if not blank(raw["rent"]) and not (rent and rent > 0):
        fail("E14", "rent", f'Dòng {number}: cột "{LABELS["rent"]}" phải là số lớn hơn 0')
    fee_text = (text(raw["fee"]) or "").lower()
    fee = 0.0 if not fee_text or "không" in fee_text else parse_number(raw["fee"])
    if fee is None or fee < 0:
        fail("E14", "fee", f'Dòng {number}: cột "{LABELS["fee"]}" phải là số không âm')

    currency = parse_currency(raw["currency"])
    fx = parse_number(raw["fx"])
    if currency is None:
        fail("E06", "currency", f"Dòng {number}: Đơn vị tiền tệ chỉ nhận VNĐ hoặc USD")
    elif currency == "USD" and not (fx and fx > 0):
        fail("E07", "fx", f"Dòng {number}: Tỷ giá bắt buộc khi tiền tệ là USD")
    elif currency == "VND" and not blank(raw["fx"]):
        fail("E08", "fx", f"Dòng {number}: Tỷ giá phải để trống khi tiền tệ là VNĐ")

    space = {
        "row": number,
        "building": text(raw["building"]),
        "floor": text(raw["floor"]) or "Không rõ tầng",
        "room": text(raw["room"]),
        "area": area,
        "service": text(raw["service"]),
        "rent": rent,
        "fee": fee or 0.0,
        "currency": currency,
        "fx": fx if currency == "USD" else None,
        "name": text(raw["name"]),
    }
    if not space["name"]:
        return space, errors

    name = space["name"]
    missing = [key for key in ("start", "end", "cycle") if blank(raw[key])]
    if missing:
        columns = ", ".join(f'"{LABELS[key]}"' for key in missing)
        fail("E09", missing[0], f'Dòng {number}: thiếu thông tin thuê của khách "{name}" (cột {columns})')
    start = parse_date(raw["start"])
    end = parse_date(raw["end"])
    for key, value in (("start", start), ("end", end)):
        if not blank(raw[key]) and value is None:
            fail("E10", key, f'Dòng {number}: cột "{LABELS[key]}" sai định dạng ngày')
    if start and end and end <= start:
        fail("E10", "end", f"Dòng {number}: ngày kết thúc phải sau ngày bắt đầu")
    cycle = parse_cycle(raw["cycle"])
    if not blank(raw["cycle"]) and cycle is None:
        fail(
            "E14",
            "cycle",
            f'Dòng {number}: cột "Kỳ thanh toán" không đọc được, dùng Hàng tháng, 3 tháng, Hàng quý, Hàng năm '
            "hoặc Đã thanh toán toàn bộ",
        )
    deposit = 0.0 if blank(raw["deposit"]) else parse_number(raw["deposit"])
    if deposit is None or deposit < 0:
        fail("E14", "deposit", f'Dòng {number}: cột "Tiền cọc" phải là số không âm')
    main = parse_flag(raw["main"])
    if main is None:
        fail("E14", "main", f'Dòng {number}: cột "Đặt chỗ chính" chỉ nhận "Có" hoặc để trống')

    space.update({
        "tax": norm_tax(raw["tax"]),
        "email": sorted(set(split_emails(raw["email"]))),
        "start": start,
        "end": end,
        "cycle": cycle,
        "deposit": deposit or 0.0,
        "contract_no": text(raw["contract_no"]),
        "template": text(raw["template"]),
        "note": text(raw["note"]),
        "main": bool(main),
    })
    return space, errors


def duplicate_rooms(spaces: list[dict]) -> list[dict]:
    """E11: a room name appears twice in one building."""
    errors = []
    seen: dict[tuple[str, str], int] = {}
    for space in spaces:
        if not space["building"] or not space["room"]:
            continue
        key = (slug(space["building"]), slug(space["room"]))
        if key in seen:
            errors.append(issue(
                "E11",
                f'Dòng {space["row"]}: phòng "{space["room"]}" bị trùng với dòng {seen[key]}',
                space["row"],
                LABELS["room"],
            ))
        else:
            seen[key] = space["row"]
    return errors


def customer_of(space: dict) -> tuple[str, str | None]:
    return slug(space["name"]), space["tax"]


def group_bookings(spaces: list[dict]) -> tuple[list[dict], list[dict]]:
    """Rows of one customer (name and tax code) with the same start and end date form one booking.

    A booking has one location, one payment cycle and one currency, so rows
    that differ there become separate bookings.
    """
    groups: dict[tuple, list[dict]] = {}
    for space in spaces:
        if not space["name"]:
            continue
        key = (
            slug(space["building"]),
            *customer_of(space),
            space["start"],
            space["end"],
            space["cycle"],
            space["currency"],
        )
        groups.setdefault(key, []).append(space)

    errors = []
    bookings = []
    for members in groups.values():
        first = members[0]
        for space in members[1:]:
            different = [LABELS[key] for key in BOOKING_FIELDS if space[key] != first[key]]
            if different:
                columns = ", ".join(f'"{label}"' for label in different)
                errors.append(issue(
                    "E12",
                    f'Dòng {space["row"]}: thông tin Booking không khớp với dòng {first["row"]} (cột {columns})',
                    space["row"],
                    different[0],
                ))
        bookings.append({
            "rows": [space["row"] for space in members],
            "building": first["building"],
            "service": " + ".join(dict.fromkeys(space["service"] for space in members)),
            "name": first["name"],
            "tax": first["tax"],
            "emails": first["email"],
            "start": first["start"],
            "end": first["end"],
            "cycle": first["cycle"],
            "currency": first["currency"],
            "fx": first["fx"],
            "deposit": first["deposit"],
            "contract_no": first["contract_no"],
            "template": first["template"],
            "note": first["note"],
            "main": first["main"],
            "spaces": members,
        })

    mains: dict[tuple, int] = {}
    for booking in bookings:
        if not booking["main"]:
            continue
        customer = customer_of(booking["spaces"][0])
        if customer in mains:
            row = booking["rows"][0]
            errors.append(issue(
                "E12",
                f'Dòng {row}: khách "{booking["name"]}" có nhiều hơn một Đặt chỗ chính (dòng {mains[customer]})',
                row,
                LABELS["main"],
            ))
        else:
            mains[customer] = booking["rows"][0]
    return bookings, errors


def read_onboarding(path: Path) -> dict:
    """Return the as-of date, file and row errors, every room row, and the bookings."""
    as_of = date.today()
    result = {"asOf": as_of, "errors": [], "spaces": [], "bookings": []}
    try:
        workbook = openpyxl.load_workbook(path, data_only=True)
    except Exception:
        result["errors"] = [issue("E01", "File không đúng định dạng .xlsx")]
        return result
    sheet, header_row = locate_header(workbook)
    first_column, file_errors = check_file(sheet, header_row)
    if file_errors:
        result["errors"] = file_errors
        return result

    every_space = []
    spaces = []
    errors = []
    for number in range(header_row + 1, (sheet.max_row or header_row) + 1):
        raw = {
            key: sheet.cell(number, first_column + offset).value
            for offset, (key, _label) in enumerate(COLUMNS)
        }
        if all(blank(value) for value in raw.values()):
            continue
        space, row_errors = read_row(number, raw)
        errors.extend(row_errors)
        every_space.append(space)
        if not row_errors:
            spaces.append(space)
    errors.extend(duplicate_rooms(every_space))
    bookings, booking_errors = group_bookings(spaces)
    errors.extend(booking_errors)
    errors.sort(key=lambda item: (item["row"] or 0, item["code"]))
    result.update({"errors": errors, "spaces": spaces, "bookings": bookings})
    return result
