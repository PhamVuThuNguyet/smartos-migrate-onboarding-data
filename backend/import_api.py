"""Onboarding Excel → PMS GraphQL mutations."""

from __future__ import annotations

import base64
import re
import threading
import unicodedata
from calendar import monthrange
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable

from identity import IdentityError, graphql, namespace_of, refresh_tokens, service_graphql_url
from onboarding import (
    LABELS,
    build_error_report,
    is_valid_email,
    issue,
    looks_like_company,
    norm_tax,
    read_onboarding,
    slug,
)

# Parallel createInvoices for elapsed periods (each call still one issueDate).
INVOICE_WORKERS = 5

PMS_ERROR = "E16"

VN_TZ = timezone(timedelta(hours=7))
UTC = timezone.utc
PRICE_TYPE_PER_ROOM = "PER_ROOM"
PACKAGE_UNIT_MONTHS = "MONTHS"
PAYMENT_UNIT_MONTHS = "MONTHS"


class SmartOSError(Exception):
    pass


def i18n(value: str) -> dict:
    return {"original": value, "translations": [{"lang": "vi", "content": value}]}


def graphql_enum(value) -> str:
    """GraphQL enum names are UPPER_SNAKE; BE stores kebab-case values."""
    return str(value or "").strip().upper().replace("-", "_")


def is_month_unit(value) -> bool:
    return graphql_enum(value) == PACKAGE_UNIT_MONTHS


def is_bank_method(value) -> bool:
    return graphql_enum(value) == "BANK"


def excel_fx_rate(source: dict) -> int | None:
    """Integer USD/VND rate from Excel column Tỷ giá."""
    raw = source.get("fx")
    if raw is None:
        return None
    try:
        rate = int(round(float(raw)))
    except (TypeError, ValueError):
        return None
    if rate < 1:
        return None
    return rate


def pms_issue_placement(message: str, booking: dict | None = None) -> tuple[str, str]:
    text = str(message or "").strip()
    lowered = text.lower()
    if "giá quy đổi ngoại tệ" in lowered or "hasnofxpricing" in lowered.replace(" ", ""):
        return LABELS["room"], text
    if "tỷ giá" in lowered and ("trống" in lowered or "không hợp lệ" in lowered):
        return LABELS["fx"], text
    if "package" in lowered or "gói" in lowered:
        return LABELS["room"], text
    if "phương thức thanh toán" in lowered or "paymentmethod" in lowered.replace(" ", ""):
        return LABELS["building"], text
    if "email" in lowered:
        return LABELS["email"], text
    if "mã số thuế" in lowered or "tax" in lowered:
        return LABELS["tax"], text
    if "mẫu hợp đồng" in lowered or "contract template" in lowered:
        return LABELS["template"], text
    if "số hợp đồng" in lowered:
        return LABELS["contract_no"], text
    if "kỳ thanh toán" in lowered or "payment period" in lowered:
        return LABELS["cycle"], text
    if "tầng" in lowered:
        return LABELS["floor"], text
    if "tòa" in lowered or "location" in lowered:
        return LABELS["building"], text
    if "dịch vụ" in lowered or "service" in lowered:
        return LABELS["service"], text
    return LABELS["room"], text


def usd_exchange_rates(source: dict) -> list[dict]:
    """createBooking/createInvoices exchangeRates from Excel Tỷ giá."""
    if source.get("currency") != "USD":
        return []
    rate = excel_fx_rate(source)
    if not rate:
        return []
    return [{"currency": "USD", "rate": rate}]


def to_vnd_amount(source: dict, amount: float) -> float:
    """Payment transaction only: USD rows × Excel Tỷ giá → VND. Invoice stays native currency."""
    value = float(amount or 0)
    if source.get("currency") != "USD":
        return value
    rate = excel_fx_rate(source)
    if not rate:
        raise SmartOSError(
            f'Dòng {source.get("rows", ["?"])[0]}: tiền tệ USD nhưng cột "Tỷ giá" trống hoặc không hợp lệ'
        )
    return round(value * rate)


def _as_iso(moment: datetime) -> str:
    utc = moment.astimezone(UTC)
    millis = utc.microsecond // 1000
    return utc.strftime("%Y-%m-%dT%H:%M:%S") + f".{millis:03d}Z"


def start_of_day(day: date) -> str:
    """Local 00:00:00.000 in Asia/Ho_Chi_Minh, as UTC."""
    return _as_iso(datetime.combine(day, time.min, tzinfo=VN_TZ))


def end_of_day(day: date) -> str:
    """Local 23:59:59.999 in Asia/Ho_Chi_Minh, as UTC."""
    return _as_iso(datetime.combine(day, time(23, 59, 59, 999000), tzinfo=VN_TZ))


def as_day(value) -> date | None:
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        return moment.astimezone(VN_TZ).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and len(value) >= 10:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(VN_TZ).date()
        except ValueError:
            return None
    return None


def booking_status(value) -> str:
    return str(value or "").replace("-", "_").lower()


def is_canceled_booking(value) -> bool:
    return booking_status(value) in {"canceled", "cancelled"}


def is_completed_booking(value) -> bool:
    return booking_status(value) == "completed"


def search_text(value: str) -> str:
    """Same folding as SmartOS getSearchText: drop accents, spaces become hyphens."""
    folded = unicodedata.normalize("NFKD", value or "")
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = folded.replace("đ", "d").replace("Đ", "d")
    return re.sub(r" +", "-", folded.strip()).lower()


def add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    return day.replace(day=min(day.day, monthrange(year, month)[1]), year=year, month=month)


def cycle_periods(start: date, end: date, step_months: int) -> list[dict]:
    """Billing periods anchored on the contract start day (PMS MonthlyCalculationType.Cycle)."""
    if end <= start:
        end = start + timedelta(days=1)
    step = max(1, step_months)
    periods = []
    index = 0
    cursor = start
    guard = 0
    while cursor <= end and guard < 600:
        guard += 1
        next_start = add_months(start, (index + 1) * step)
        full_end = next_start - timedelta(days=1)
        period_end = min(full_end, end)
        if period_end < cursor:
            break
        periods.append({
            "start": cursor,
            "end": period_end,
            "months": step if period_end == full_end else _covered_months(cursor, period_end),
        })
        cursor = period_end + timedelta(days=1)
        if cursor < next_start:
            cursor = next_start
        if cursor <= periods[-1]["start"]:
            break
        index += 1
    return periods


def calendar_periods(start: date, end: date, step_months: int) -> list[dict]:
    """Billing periods aligned to calendar months (PMS MonthlyCalculationType.Calendar).

    Example start 24/1, step 1: 24/1–31/1, 1/2–28/2, 1/3–31/3, …
    Matches payment-period.service calculatePeriodTimeRange for Calendar + MONTHS.
    """
    if end <= start:
        end = start + timedelta(days=1)
    step = max(1, step_months)
    periods = []
    anchor_month = date(start.year, start.month, 1)
    index = 0
    guard = 0
    while guard < 600:
        guard += 1
        raw_start = add_months(anchor_month, index * step)
        # scheduleAnchor.add((idx+1)*step, month).subtract(1, month).endOf('month')
        end_marker = add_months(add_months(start, (index + 1) * step), -1)
        raw_end = date(
            end_marker.year,
            end_marker.month,
            monthrange(end_marker.year, end_marker.month)[1],
        )
        if raw_start > end:
            break
        period_start = max(raw_start, start)
        period_end = min(raw_end, end)
        if period_start <= period_end:
            full = period_start == raw_start and period_end == raw_end
            periods.append({
                "start": period_start,
                "end": period_end,
                "months": step if full else _covered_months(period_start, period_end),
            })
        if raw_end >= end:
            break
        index += 1
    return periods


def booking_periods(
    start: date,
    end: date,
    step_months: int,
    monthly_calc: str | None = "Cycle",
) -> list[dict]:
    if str(monthly_calc or "Cycle").strip().lower() == "calendar":
        return calendar_periods(start, end, step_months)
    return cycle_periods(start, end, step_months)


def elapsed_pms_periods(stored: list, booking_start: date, booking_end: date, as_of: date) -> list[tuple[dict, dict]]:
    """Payment periods PMS already created that ended before the import date."""
    rows = []
    for item in stored:
        start = as_day(item.get("startDate"))
        end = as_day(item.get("endDate"))
        if not start or not end or end >= as_of:
            continue
        if end < booking_start or start > booking_end:
            continue
        rows.append(({"start": start, "end": end, "months": _covered_months(start, end)}, item))
    rows.sort(key=lambda row: row[0]["start"])
    return rows


def _covered_months(start: date, end: date) -> int:
    exclusive = end + timedelta(days=1)
    months = (exclusive.year - start.year) * 12 + (exclusive.month - start.month)
    if exclusive.day < start.day:
        months -= 1
    return max(1, months)


def span_months(start: date, end: date) -> int:
    """Months needed for one period that reaches the contract end."""
    months = _covered_months(start, end)
    while add_months(start, months) - timedelta(days=1) < end and months < 600:
        months += 1
    return months


class Importer:
    def __init__(self, client, urls: dict, options: dict):
        self.client = client
        self.urls = urls
        self.options = options
        self.steps: list[dict] = []
        self.issues: list[dict] = []
        self.locations: dict[str, str] = {}
        self.services: dict[str, str] = {}
        self.floors: dict[tuple[str, str], str] = {}
        self.rooms: dict[tuple[str, str], dict] = {}
        self.customers: dict[str, str] = {}
        self.transfer_methods: dict[str, str] = {}
        self.contract_templates: dict[str, str] = {}
        self.booked_rooms: set[tuple[str, str]] = set()
        self.tax_flags: dict[str, bool] = {}
        self._state_lock = threading.Lock()

    def step(self, action: str, name: str, status: str, record_id: str = "", detail: str = ""):
        item = {"action": action, "name": name, "status": status, "id": record_id, "detail": detail}
        with self._state_lock:
            self.steps.append(item)

    def record_issue(
        self,
        message: str,
        row: int | None = None,
        column: str | None = None,
        action: str = "error",
        name: str = "",
        code: str = PMS_ERROR,
    ):
        with self._state_lock:
            self.issues.append(issue(code, message, row, column))
        self.step(action, name or (f"dòng {row}" if row else "lỗi"), "error", detail=message)

    def booking_resume_key(self, booking: dict) -> str:
        mode, duration = booking.get("cycle") or ("months", 1)
        return "|".join(
            [
                self.customer_key(booking) if booking.get("name") else "",
                str(booking.get("building") or ""),
                str(booking.get("start") or ""),
                str(booking.get("end") or ""),
                str(booking.get("currency") or ""),
                f"{mode}:{duration}",
                str(booking.get("monthly_calc") or "Cycle"),
            ]
        )

    def booking_has_issue(self, booking: dict) -> bool:
        rows = {row for row in (booking.get("rows") or []) if row is not None}
        if not rows:
            return False
        with self._state_lock:
            return any(item.get("row") in rows for item in self.issues)

    def record_booking_issue(self, booking: dict, error, action: str = "booking"):
        column, detail = pms_issue_placement(str(error), booking)
        name = booking.get("name") or ""
        spaces = booking.get("spaces") or []
        if column == LABELS["room"] and spaces:
            for space in spaces:
                self.record_issue(detail, space["row"], column, action, f"{name} / {space['room']}")
            return
        for row in booking.get("rows") or [None]:
            self.record_issue(detail, row, column, action, name)

    def summary(self) -> dict:
        created: dict[str, int] = {}
        reused: dict[str, int] = {}
        for item in self.steps:
            if item["status"] == "created":
                created[item["action"]] = created.get(item["action"], 0) + 1
            elif item["status"] == "reused":
                reused[item["action"]] = reused.get(item["action"], 0) + 1
        return {
            "created": created,
            "reused": reused,
            "vacantRooms": sum(1 for key in self.rooms if key not in self.booked_rooms),
        }

    def mark_booked(self, booking: dict):
        building = booking["building"]
        for space in booking["spaces"]:
            key = (building, space["room"])
            if key in self.rooms:
                self.booked_rooms.add(key)

    def run(
        self,
        as_of: date,
        spaces: list[dict],
        bookings: list[dict],
        *,
        skip_booking_keys: set[str] | None = None,
        on_progress: Callable[[dict], None] | None = None,
    ) -> dict:
        skip = set(skip_booking_keys or ())
        completed: list[str] = []
        ordered = sorted(bookings, key=lambda item: not item["main"])
        total = len(ordered)
        processed = 0

        def emit(phase: str, message: str, done: int | None = None):
            if not on_progress:
                return
            payload = {
                "phase": phase,
                "message": message,
                "totalBookings": total,
                "doneBookings": done if done is not None else processed,
                "completedKeys": list(completed),
                "recentSteps": list(self.steps[-40:]),
            }
            on_progress(payload)

        emit("catalog", "Đang resolve location / service", 0)
        self.resolve_catalog(spaces)
        emit("rooms", "Đang tạo / reuse phòng", 0)
        self.ensure_floors_and_rooms(spaces)

        for booking in ordered:
            key = self.booking_resume_key(booking)
            name = booking["name"]
            if key in skip:
                self.step("booking", name, "reused", detail="checkpoint skip")
                if key not in completed:
                    completed.append(key)
                processed += 1
                emit("bookings", f"Bỏ qua (đã xong): {name}", processed)
                continue
            try:
                if self.skip_invalid_email(booking):
                    # Permanent skip — do not retry on resume
                    completed.append(key)
                    processed += 1
                    emit("bookings", f"Bỏ qua email: {name}", processed)
                    continue
                if booking["building"] not in self.locations:
                    self.step(
                        "booking",
                        name,
                        "skipped",
                        detail=f'Tòa "{booking["building"]}" chưa có trên hệ thống',
                    )
                    processed += 1
                    emit("bookings", f"Bỏ qua tòa: {name}", processed)
                    continue
                emit("bookings", f"Đang xử lý: {name}", processed)
                self.ensure_customer(booking)
                self.ensure_booking(as_of, booking)
            except SmartOSError as caught:
                self.record_booking_issue(booking, caught)
            except Exception as caught:
                self.record_booking_issue(booking, caught)
            # Only checkpoint successful bookings so resume retries PMS/invoice failures.
            if not self.booking_has_issue(booking):
                completed.append(key)
            processed += 1
            emit("bookings", f"Xong: {name}", processed)

        self.issues.sort(key=lambda item: (item.get("row") is None, item.get("row") or 0, item.get("code") or ""))
        emit("finishing", "Hoàn tất", total)
        return {"steps": self.steps, "error": "", "completedKeys": completed}

    def resolve_catalog(self, spaces: list[dict]):
        for name in dict.fromkeys(space["building"] for space in spaces):
            try:
                self.ensure_location(name)
            except SmartOSError as caught:
                row = next((space["row"] for space in spaces if space["building"] == name), None)
                self.record_issue(str(caught), row, "Toà nhà", "location", name)
            except Exception as caught:
                row = next((space["row"] for space in spaces if space["building"] == name), None)
                self.record_issue(str(caught), row, "Toà nhà", "location", name)
        for name in dict.fromkeys(space["service"] for space in spaces):
            try:
                self.ensure_service(name)
            except SmartOSError as caught:
                row = next((space["row"] for space in spaces if space["service"] == name), None)
                self.record_issue(str(caught), row, "Dịch vụ cung cấp", "service", name)
            except Exception as caught:
                row = next((space["row"] for space in spaces if space["service"] == name), None)
                self.record_issue(str(caught), row, "Dịch vụ cung cấp", "service", name)

    def ensure_location(self, name: str):
        folded = search_text(name)
        found = self.client.items(
            self.urls["property"],
            "locations",
            "LocationQueryInput",
            "id name searchText",
            {},
        )
        match = next(
            (
                item for item in found
                if item.get("name") == name or item.get("searchText") == folded
            ),
            None,
        )
        if not match:
            raise SmartOSError(
                f'Tòa nhà "{name}" chưa có trên hệ thống. Tạo location trước khi import phòng.'
            )
        self.locations[name] = match["id"]
        self.step("location", name, "reused", match["id"])

    def ensure_service(self, name: str):
        folded = search_text(name)
        found = self._list_services(folded)
        match = next((item for item in found if _service_matches(item, name, folded)), None)
        if not match:
            raise SmartOSError(_service_missing_message(name, found))
        self.services[name] = match["id"]
        self.step("service", name, "reused", match["id"])

    def _list_services(self, folded: str) -> list[dict]:
        found: list[dict] = []
        seen: set[str] = set()

        def add(rows):
            for item in rows or []:
                sid = str(item.get("id") or "")
                if sid and sid in seen:
                    continue
                if sid:
                    seen.add(sid)
                found.append(item)

        for filt in ({}, {"searchText": {"like": f"%{folded}%"}}):
            try:
                add(self._query_services(filt))
            except SmartOSError:
                continue
        add(self._services_on_locations())
        return found

    def _query_services(self, filt: dict) -> list:
        last_error = None
        for selection in (
            "id name { original translations { lang content } } searchText locationIds isActive",
            "id name { original } searchText",
            "id name searchText",
        ):
            try:
                return self.client.items(
                    self.urls["property"],
                    "services",
                    "ServiceQueryInput",
                    selection,
                    filt,
                )
            except SmartOSError as error:
                last_error = error
        if last_error:
            raise last_error
        return []

    def _services_on_locations(self) -> list[dict]:
        graphql = getattr(self.client, "graphql", None)
        if not callable(graphql):
            return []
        rows = []
        query = """
        query($id: ObjectID!) {
          location(id: $id) {
            services {
              id
              name { original translations { lang content } }
              searchText
            }
          }
        }
        """
        simple = """
        query($id: ObjectID!) {
          location(id: $id) {
            services { id name { original } searchText }
          }
        }
        """
        for loc_id in self.locations.values():
            data = None
            for body in (query, simple):
                try:
                    data = graphql(self.urls["property"], body, {"id": loc_id})
                    break
                except SmartOSError:
                    continue
            rows.extend(((data or {}).get("location") or {}).get("services") or [])
        return rows

    def ensure_floors_and_rooms(self, spaces: list[dict]):
        grouped: dict[tuple[str, str], list[dict]] = {}
        for space in spaces:
            grouped.setdefault((space["building"], space["floor"]), []).append(space)
        for (building, floor_name), members in grouped.items():
            if building not in self.locations:
                continue
            try:
                floor_id = self.ensure_floor(building, floor_name)
            except SmartOSError as caught:
                self.record_issue(str(caught), members[0]["row"], "Tầng", "floor", f"{building} / {floor_name}")
                continue
            except Exception as caught:
                self.record_issue(str(caught), members[0]["row"], "Tầng", "floor", f"{building} / {floor_name}")
                continue
            for space in members:
                if space["service"] not in self.services:
                    continue
                try:
                    self.ensure_room(building, floor_id, space)
                except SmartOSError as caught:
                    self.record_issue(
                        str(caught),
                        space["row"],
                        "Khu vực/phòng",
                        "room",
                        f"{building} / {space['room']}",
                    )
                except Exception as caught:
                    self.record_issue(
                        str(caught),
                        space["row"],
                        "Khu vực/phòng",
                        "room",
                        f"{building} / {space['room']}",
                    )

    def ensure_floor(self, building: str, floor_name: str) -> str:
        key = (building, floor_name)
        if key in self.floors:
            return self.floors[key]
        location_id = self.locations[building]
        found = self.client.items(
            self.urls["property"],
            "floors",
            "FloorQueryInput",
            "id name { original }",
            {"locationId": {"eq": location_id}},
        )
        match = next((item for item in found if item["name"]["original"] == floor_name), None)
        if not match:
            raise SmartOSError(
                f'Tầng "{floor_name}" của tòa "{building}" chưa có trên hệ thống. Tạo floor trước khi import phòng.'
            )
        self.floors[key] = match["id"]
        self.step("floor", f"{building} / {floor_name}", "reused", match["id"])
        return match["id"]

    def ensure_room(self, building: str, floor_id: str, space: dict):
        service = space["service"]
        found = self.client.items(
            self.urls["property"],
            "rooms",
            "RoomQueryInput",
            "id name { original } packages { id serviceId unit amount priceType }",
            {"floorId": {"eq": floor_id}},
        )
        match = next((item for item in found if item["name"]["original"] == space["room"]), None)
        if match:
            room = {"id": match["id"], "packages": match.get("packages") or [], "reused": True}
            self.rooms[(building, space["room"])] = room
            self.step("room", f"{building} / {space['room']}", "reused", match["id"])
            self.attach_package(room, space)
            return
        created = self.client.mutate(
            self.urls["property"],
            "createRoom",
            "CreateRoomCommandInput",
            "id packages { id serviceId unit amount priceType }",
            {
                "name": i18n(space["room"]),
                "area": space["area"],
                "floorId": floor_id,
                "locationId": self.locations[building],
                "capacity": max(1, round(space["area"] / 10)),
                "tier": "Standard",
                "packages": [{
                    "priceType": PRICE_TYPE_PER_ROOM,
                    "unit": PACKAGE_UNIT_MONTHS,
                    "amount": self.package_amount(space),
                    "serviceId": self.services[service],
                }],
            },
        )
        room = {"id": created["id"], "packages": created.get("packages") or []}
        self.rooms[(building, space["room"])] = room
        self.step("room", f"{building} / {space['room']}", "created", created["id"])
        self.step("package", f"{space['room']} / {service}", "created")
        self.attach_package(room, space)

    def package_amount(self, space: dict) -> float:
        """Package.amount is the Excel unit price as-is (USD rows stay USD, never × Tỷ giá)."""
        return float(space["rent"] or 0)

    def usd_alternative_amounts(self, space: dict) -> list[dict]:
        if space.get("currency") != "USD":
            return []
        return [{"currency": "USD", "amount": self.package_amount(space), "isAutoCalculated": False}]

    def package_input(self, item: dict, space: dict | None = None) -> dict:
        payload = {
            "priceType": graphql_enum(item.get("priceType") or PRICE_TYPE_PER_ROOM),
            "unit": graphql_enum(item.get("unit") or PACKAGE_UNIT_MONTHS),
            "amount": item["amount"],
            "serviceId": item["serviceId"],
        }
        alternatives = self.usd_alternative_amounts(space) if space else []
        if alternatives:
            payload["amount"] = self.package_amount(space)
            payload["alternativeAmounts"] = alternatives
        return payload

    def attach_package(self, room: dict, space: dict):
        service = space["service"]
        service_id = self.services[service]
        existing = next(
            (
                item
                for item in room["packages"]
                if item.get("serviceId") == service_id and is_month_unit(item.get("unit"))
            ),
            None,
        )
        if existing:
            if space.get("currency") == "USD":
                self.stamp_usd_pricing(room, space)
            return
        kept = [self.package_input(item) for item in room["packages"]]
        kept.append(self.package_input({
            "priceType": PRICE_TYPE_PER_ROOM,
            "unit": PACKAGE_UNIT_MONTHS,
            "amount": self.package_amount(space),
            "serviceId": service_id,
        }, space))
        self.write_packages(room, kept)
        self.step("package", f"{space['room']} / {service}", "created")

    def stamp_usd_pricing(self, room: dict, space: dict):
        """Rewrite package.amount to Excel USD and stamp alternativeAmounts (no VND conversion)."""
        service_id = self.services[space["service"]]
        kept = []
        for item in room["packages"]:
            match = item.get("serviceId") == service_id and is_month_unit(item.get("unit"))
            kept.append(self.package_input(item, space if match else None))
        self.write_packages(room, kept)

    def write_packages(self, room: dict, packages: list[dict]):
        updated = self.client.mutate(
            self.urls["property"],
            "mutatePackages",
            "MutatePackagesCommandInput",
            "id serviceId unit amount priceType",
            {"roomId": room["id"], "packages": packages},
        )
        room["packages"] = updated if isinstance(updated, list) else [updated]

    def customer_key(self, booking: dict) -> str:
        name = booking["name"]
        if booking["tax"]:
            return f"tax:{booking['tax']}"
        kind = "company" if looks_like_company(name) else "name"
        return f"{kind}:{slug(name)}"

    def skip_invalid_email(self, booking: dict) -> bool:
        invalid = [email for email in booking["emails"] if not is_valid_email(email)]
        if not invalid:
            return False
        name = booking["name"]
        shown = ", ".join(invalid)
        self.step("customer", name, "skipped", detail=f"Email không hợp lệ: {shown}")
        self.step("booking", name, "skipped", detail="Bỏ qua vì email không hợp lệ")
        return True

    def ensure_customer(self, booking: dict):
        name = booking["name"]
        tax = booking["tax"]
        company = bool(tax) or looks_like_company(name)
        key = self.customer_key(booking)
        if key in self.customers:
            return
        emails = list(booking["emails"]) or [f"{name.lower().replace(' ', '.')}@onboarding.invalid"]
        match = self.find_customer(tax=tax, email=None if tax else emails[0])
        if match:
            self.customers[key] = match["id"]
            self.step("customer", name, "reused", match["id"])
            return
        building = booking["building"]
        try:
            if company:
                members = [
                    {"contactName": email.split("@", 1)[0], "email": email}
                    for email in emails[1:]
                ]
                payload = {
                    "companyName": {"vi": name},
                    "email": emails[0],
                    "legalRepName": name,
                    "legalRepRole": "Người đại diện",
                    "members": members,
                    "locationId": self.locations[building],
                }
                if tax:
                    payload["taxCode"] = tax
                created = self.client.mutate(
                    self.urls["customer"],
                    "createCompany",
                    "CreateCompanyCommandInput",
                    "id",
                    payload,
                )
            else:
                created = self.client.mutate(
                    self.urls["customer"],
                    "createIndividual",
                    "CreateIndividualCommandInput",
                    "id",
                    {
                        "contactName": name,
                        "email": emails[0],
                        "locationId": self.locations[building],
                    },
                )
        except SmartOSError as error:
            recovered = self.find_customer_after_conflict(str(error), tax, emails[0])
            if not recovered:
                raise
            self.customers[key] = recovered["id"]
            self.step("customer", name, "reused", recovered["id"], str(error))
            return
        self.customers[key] = created["id"]
        self.step("customer", name, "created", created["id"])

    def find_customer(self, tax: str | None = None, email: str | None = None):
        needles = []
        if tax:
            needles.extend([tax, search_text(tax)])
        if email:
            needles.extend([email, search_text(email)])
        filters = [{"searchText": {"like": needle}} for needle in needles if needle]
        filters.append({})
        for filt in filters:
            try:
                found = self.client.items(
                    self.urls["customer"],
                    "customers",
                    "CustomerQueryInput",
                    "id taxCode email searchText",
                    filt,
                )
            except SmartOSError:
                continue
            for item in found:
                if tax and norm_tax(item.get("taxCode")) == norm_tax(tax):
                    return item
                if not tax and email and (item.get("email") or "").lower() == email.lower():
                    return item
        return None

    def find_customer_after_conflict(self, message: str, tax: str | None, email: str):
        lowered = message.lower()
        if tax and ("mã số thuế" in lowered or "tax code" in lowered or "taxcode" in lowered):
            return self.find_customer(tax=tax)
        if "email" in lowered and (
            "đã tồn tại" in lowered or "already exists" in lowered or "conflict" in lowered
        ):
            return self.find_customer(email=email)
        return None

    def find_bookings(self, booking: dict, ready: list) -> list:
        start, end = booking["start"], booking["end"]
        room_ids = [room["id"] for _space, room in ready]
        found = self.client.items(
            self.urls["reservation"],
            "bookings",
            "BookingQueryInput",
            "id code status startTime endTime roomIds",
            {
                "locationId": {"eq": self.locations[booking["building"]]},
                "primaryCustomerId": {"eq": self.customers[self.customer_key(booking)]},
            },
        )
        wanted = set(room_ids)
        matches = []
        for item in found:
            if is_canceled_booking(item.get("status")):
                continue
            if as_day(item.get("startTime")) != start or as_day(item.get("endTime")) != end:
                continue
            if {str(room_id) for room_id in item.get("roomIds") or []} == wanted:
                matches.append(item)
        return matches

    def overlapping_booking(self, booking: dict, ready: list) -> dict | None:
        reused = [(space, room) for space, room in ready if room.get("reused")]
        if not reused:
            return None
        start, end = booking["start"], booking["end"]
        by_id = {str(room["id"]): space for space, room in reused}
        found = self.client.items(
            self.urls["reservation"],
            "bookings",
            "BookingQueryInput",
            "id code status startTime endTime roomIds",
            {
                "locationId": {"eq": self.locations[reused[0][0]["building"]]},
            },
        )
        for item in found:
            if is_canceled_booking(item.get("status")):
                continue
            item_start, item_end = as_day(item.get("startTime")), as_day(item.get("endTime"))
            if item_start is None or item_end is None or not (item_start < end and item_end > start):
                continue
            overlap = {str(room_id) for room_id in item.get("roomIds") or []} & set(by_id)
            if not overlap:
                continue
            space = by_id[next(iter(overlap))]
            return issue(
                "E13",
                f'Dòng {space["row"]}: phòng "{space["room"]}" đã có booking trùng thời gian '
                f'({item.get("code") or item["id"]})',
                space["row"],
                "Khu vực/phòng",
            )
        return None

    def ensure_booking(self, as_of: date, booking: dict):
        name = booking["name"]
        building = booking["building"]
        spaces = booking["spaces"]
        ready = [
            (space, self.rooms[(building, space["room"])])
            for space in spaces
            if (building, space["room"]) in self.rooms
        ]
        if len(ready) != len(spaces):
            missing = ", ".join(
                space["room"] for space in spaces if (building, space["room"]) not in self.rooms
            )
            self.step(
                "booking",
                name,
                "skipped",
                detail=f"Không tạo booking vì còn phòng chưa sẵn sàng: {missing}",
            )
            return
        matches = self.find_bookings(booking, ready)
        if len(matches) > 1:
            codes = ", ".join(item.get("code") or item["id"] for item in matches)
            clash = issue(
                "E15",
                f'Dòng {booking["rows"][0]}: có {len(matches)} booking cùng khách, cùng phòng và cùng thời gian ({codes})',
                booking["rows"][0],
                "Khu vực/phòng",
            )
            self.issues.append(clash)
            self.step("booking", name, "skipped", detail=clash["message"])
            return
        existing = matches[0] if matches else None
        if existing:
            self.step("booking", name, "reused", existing["id"])
            booking_id = existing["id"]
        else:
            clash = self.overlapping_booking(booking, ready)
            if clash:
                self.issues.append(clash)
                self.step("booking", name, "skipped", detail=clash["message"])
                return
            try:
                booking_id = self.create_booking(booking, ready, name)
            except (SmartOSError, Exception) as caught:
                self.record_booking_issue(booking, caught)
                return
        self.mark_booked(booking)
        try:
            self.ensure_contract_record(booking, booking_id, name)
        except (SmartOSError, Exception) as caught:
            self.record_booking_issue(booking, caught, "contract")
        try:
            self.invoice_elapsed_periods(as_of, booking, booking_id, name, reused=existing is not None)
        except (SmartOSError, Exception) as caught:
            self.record_booking_issue(booking, caught, "invoice")
        if booking["end"] < as_of:
            try:
                if existing and is_completed_booking(existing.get("status")):
                    self.step("completeBooking", name, "reused", booking_id)
                else:
                    self.complete_booking(booking_id, name)
            except (SmartOSError, Exception) as caught:
                self.record_booking_issue(booking, caught, "completeBooking")

    def location_apply_tax(self, location_id: str) -> bool:
        """Mirror PMS customer create: applyTax from bookingPolicy.isTaxApplied."""
        with self._state_lock:
            if location_id in self.tax_flags:
                return self.tax_flags[location_id]
        flag = False
        graphql_fn = getattr(self.client, "graphql", None)
        if callable(graphql_fn):
            try:
                data = graphql_fn(
                    self.urls["reservation"],
                    "query($locationId: ObjectID!) { bookingPolicy(locationId: $locationId) { isTaxApplied } }",
                    {"locationId": location_id},
                )
                flag = bool((data.get("bookingPolicy") or {}).get("isTaxApplied"))
            except Exception:
                flag = False
        with self._state_lock:
            self.tax_flags[location_id] = flag
        return flag

    def create_booking(self, booking: dict, ready: list, name: str) -> str:
        building = booking["building"]
        rooms: dict[str, dict] = {}
        for space, room in ready:
            service = space["service"]
            service_id = self.services[service]
            package = next(
                (
                    item for item in room["packages"]
                    if item.get("serviceId") == service_id and is_month_unit(item.get("unit"))
                ),
                None,
            )
            if package is None:
                raise SmartOSError(f"Phòng {space['room']} chưa có package tháng cho {service}")
            entry = rooms.setdefault(room["id"], {"id": room["id"], "packages": []})
            entry["packages"].append({"id": package["id"], "amount": float(space["rent"] or 0)})
        deposit = float(booking["deposit"] or 0)
        mode, duration = booking["cycle"]
        start = booking["start"]
        end = booking["end"]
        duration = span_months(start, end) if mode == "full" else duration
        monthly_calc = booking.get("monthly_calc") or "Cycle"
        location_id = self.locations[building]
        # PMS "ngày thanh toán" has start+end; both take booking/period start (not kỳ end).
        payment_day = start_of_day(start)
        customer_id = self.customers[self.customer_key(booking)]
        foreign = booking.get("currency") == "USD"
        payload = {
            "locationId": location_id,
            "rooms": list(rooms.values()),
            "customerIds": [customer_id],
            "primaryCustomerId": customer_id,
            "startTime": start_of_day(start),
            "endTime": end_of_day(end),
            "selectTime": False,
            "monthlyCalculationType": monthly_calc,
            "daysOfMonthCalculationType": "DaysOfCalendar",
            "numberOfPeople": 1,
            "applyTax": self.location_apply_tax(location_id),
            "currency": "USD" if foreign else "VND",
            "markAsConfirmed": True,
            "paymentSettings": {
                "unit": PAYMENT_UNIT_MONTHS,
                "duration": duration,
                "firstPeriodPaymentStartDate": payment_day,
                "firstPeriodPaymentEndDate": payment_day,
            },
        }
        if booking["note"]:
            payload["note"] = booking["note"]
        if foreign:
            rates = usd_exchange_rates(booking)
            if not rates:
                raise SmartOSError(
                    f'Dòng {booking["rows"][0]}: tiền tệ USD nhưng cột "Tỷ giá" trống hoặc không hợp lệ'
                )
            payload["exchangeRates"] = rates
        if deposit > 0:
            method = self.transfer_method_id(self.locations[building])
            if not method:
                raise SmartOSError(
                    f'Tòa "{building}" chưa có phương thức thanh toán Transfer (bank) '
                    "để ghi nhận tiền cọc đã thu"
                )
            payload["deposit"] = {
                "requiredAmount": deposit,
                "paidAmount": deposit,
                "transactionDate": start_of_day(start),
                "paymentMethodId": method,
            }
        created = self.client.mutate(
            self.urls["reservation"],
            "createBooking",
            "CreateBookingCommandInput",
            "id code",
            payload,
        )
        self.step("booking", name, "created", created["id"], created.get("code", ""))
        return created["id"]

    def transfer_method_id(self, location_id: str) -> str:
        explicit = (self.options.get("paymentMethodId") or "").strip()
        if explicit:
            return explicit
        cached = self.transfer_methods.get(location_id)
        if cached:
            return cached
        found = self.client.items(
            self.urls["payment"],
            "paymentMethods",
            "PaymentMethodQueryInput",
            "id type",
            {"locationId": {"eq": location_id}, "type": {"eq": "bank"}},
        )
        match = next((item for item in found if is_bank_method(item.get("type"))), None)
        if not match:
            return ""
        self.transfer_methods[location_id] = match["id"]
        return match["id"]

    def template_id(self, template_name: str) -> str:
        cached = self.contract_templates.get(template_name)
        if cached:
            return cached
        url = (self.urls.get("contract") or "").strip()
        if not url:
            return ""
        folded = search_text(template_name)
        found = self.client.items(
            url,
            "contractTemplates",
            "ContractTemplateQueryInput",
            "id name searchText",
            {"searchText": {"eq": folded}, "isActive": {"eq": True}},
        )
        match = next(
            (
                item for item in found
                if item.get("name") == template_name or item.get("searchText") == folded
            ),
            None,
        )
        if not match:
            return ""
        self.contract_templates[template_name] = match["id"]
        return match["id"]

    def ensure_contract_record(self, booking: dict, booking_id: str, name: str):
        template_name = booking["template"]
        if not template_name:
            return
        number = booking["contract_no"]
        if not number:
            self.step("contract", name, "skipped", detail="Có mẫu hợp đồng nhưng thiếu số hợp đồng")
            return
        if not (self.urls.get("contract") or "").strip():
            self.step("contract", name, "skipped", detail="Thiếu URL GraphQL contract")
            return
        template_id = self.template_id(template_name)
        if not template_id:
            self.step("contract", template_name, "skipped", detail="Không thấy contract template cùng tên")
            return
        found = self.client.items(
            self.urls["contract"],
            "contracts",
            "ContractQueryInput",
            "id code",
            {"code": {"eq": number}},
        )
        match = next((item for item in found if item.get("code") == number), None)
        if match:
            self.step("contract", number, "reused", match["id"])
            return
        start = booking["start"]
        end = booking["end"]
        customer_id = self.customers[self.customer_key(booking)]
        created = self.client.mutate(
            self.urls["contract"],
            "createContract",
            "CreateContractCommandInput",
            "id code",
            {
                "code": number,
                "name": template_name,
                "effectiveDate": start_of_day(start),
                "expiryDate": end_of_day(end),
                "bookingStartTime": start_of_day(start),
                "bookingEndTime": end_of_day(end),
                "bookingNumberOfPeople": 1,
                "roomServices": [],
                "contractTemplateId": template_id,
                "bookingId": booking_id,
                "customerIds": [customer_id],
            },
        )
        self.step("contract", number, "created", created["id"], template_name)

    def invoices_by_period(self, booking_id: str) -> dict:
        found = self.client.items(
            self.urls["payment"],
            "invoices",
            "InvoiceQueryInput",
            "id code paidAmount status invoiceItems { paymentPeriodId } transactions { id }",
            {"bookings": {"some": {"bookingId": {"eq": booking_id}}}},
        )
        by_period = {}
        for invoice in found:
            for item in invoice.get("invoiceItems") or []:
                period_id = item.get("paymentPeriodId")
                if period_id:
                    by_period[str(period_id)] = invoice
        return by_period

    def invoice_collected(self, invoice: dict, amount: float) -> bool:
        paid = invoice.get("paidAmount")
        if isinstance(paid, (int, float)) and not isinstance(paid, bool) and paid + 0.01 >= amount:
            return True
        if invoice.get("transactions"):
            return True
        return str(invoice.get("status") or "").lower() == "paid"

    def invoice_item_for_period(self, booking: dict, period: dict, period_id: str, amount: float) -> dict:
        # Both payment line dates use the same period start timestamp (not period end / end-of-day).
        day = start_of_day(period["start"])
        building = booking["building"]
        room_id = next(
            (
                self.rooms[building, space["room"]]["id"]
                for space in booking["spaces"]
                if (building, space["room"]) in self.rooms
            ),
            "",
        )
        if not room_id:
            raise SmartOSError("Không có phòng để lập hóa đơn cho kỳ thanh toán")
        return {
            "category": graphql_enum("payment-period"),
            "name": i18n("Tiền thuê"),
            "startTime": day,
            "endTime": day,
            "quantity": 1,
            "timeQuantity": [{
                "unit": graphql_enum("months"),
                "startTime": day,
                "endTime": day,
                "quantity": 1,
                "unitPrice": amount,
            }],
            "unit": i18n("kỳ"),
            "unitPrice": amount,
            "discountAmount": 0,
            "paymentPeriodId": period_id,
            "metadata": {
                "roomId": room_id,
                "packageUnit": graphql_enum("months"),
                "monthlyCalculationType": booking.get("monthly_calc") or "Cycle",
                "daysOfMonthCalculationType": "DaysOfCalendar",
            },
        }

    def invoice_elapsed_periods(self, as_of: date, booking: dict, booking_id: str, name: str, reused: bool = False):
        start = booking["start"]
        end = booking["end"]
        stored = self.client.items(
            self.urls["reservation"],
            "paymentPeriods",
            "PaymentPeriodQueryInput",
            "id totalAmount startDate endDate",
            {"bookingId": {"eq": booking_id}},
        )
        due = elapsed_pms_periods(stored, start, end, as_of)
        if not due:
            if reused:
                return
            mode, duration = booking["cycle"]
            step = span_months(start, end) if mode == "full" else duration
            due = [
                (period, None)
                for period in booking_periods(start, end, step, booking.get("monthly_calc"))
                if period["end"] < as_of
            ]
        if not due:
            return
        existing = self.invoices_by_period(booking_id)
        monthly = sum(float(space["rent"] or 0) for space in booking["spaces"])
        building = booking["building"]
        customer_id = self.customers[self.customer_key(booking)]
        method = self.transfer_method_id(self.locations[building])

        to_create: list[dict] = []
        for index, (period, stored_period) in enumerate(due, start=1):
            try:
                plan = self._period_invoice_plan(
                    booking, booking_id, period, index, stored_period, existing, monthly,
                )
                if plan["existing"]:
                    invoice = plan["existing"]
                    self.step("invoice", name, "reused", invoice["id"], invoice.get("code") or "")
                    if plan["invoice_amount"] <= 0 or self.invoice_collected(invoice, plan["invoice_amount"]):
                        if plan["invoice_amount"] > 0:
                            self.step("transaction", name, "reused", invoice["id"])
                        continue
                    self.collect_period(
                        booking, booking_id, name, invoice["id"], plan["payment_amount"],
                        plan["start"], building, customer_id, method,
                    )
                    continue
                to_create.append(plan)
            except (SmartOSError, Exception) as caught:
                self.record_booking_issue(booking, caught, "invoice")

        created_pays: list[dict] = []
        if to_create:
            workers = min(INVOICE_WORKERS, len(to_create))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(
                        self._create_period_invoice,
                        booking, booking_id, name, plan, building,
                    ): plan
                    for plan in to_create
                }
                for future in as_completed(futures):
                    plan = futures[future]
                    try:
                        invoice = future.result()
                        if plan["invoice_amount"] > 0 and invoice:
                            created_pays.append({
                                "invoice_id": invoice["id"],
                                "payment_amount": plan["payment_amount"],
                                "start": plan["start"],
                            })
                    except (SmartOSError, Exception) as caught:
                        self.record_booking_issue(booking, caught, "invoice")

        for pay in created_pays:
            try:
                self.collect_period(
                    booking, booking_id, name, pay["invoice_id"], pay["payment_amount"],
                    pay["start"], building, customer_id, method,
                )
            except (SmartOSError, Exception) as caught:
                self.record_booking_issue(booking, caught, "invoice")

    def _period_invoice_plan(
        self, booking, booking_id, period, index, stored_period, existing, monthly,
    ) -> dict:
        start = period["start"]
        if booking.get("currency") == "USD":
            invoice_amount = monthly * period["months"]
        elif stored_period and stored_period.get("totalAmount") is not None:
            invoice_amount = float(stored_period["totalAmount"])
        else:
            invoice_amount = monthly * period["months"]
        payment_amount = to_vnd_amount(booking, invoice_amount)
        period_id = stored_period["id"] if stored_period else f"{booking_id}:period:{index}"
        return {
            "period": period,
            "period_id": period_id,
            "start": start,
            "invoice_amount": invoice_amount,
            "payment_amount": payment_amount,
            "existing": existing.get(str(period_id)),
        }

    def _create_period_invoice(self, booking, booking_id, name, plan: dict, building: str) -> dict:
        """createInvoices for one period (issueDate is command-level → one call per period date)."""
        period = plan["period"]
        invoice_amount = plan["invoice_amount"]
        period_id = plan["period_id"]
        start = plan["start"]
        invoice_input = {
            "type": graphql_enum("booking"),
            "bookingId": booking_id,
            "invoiceItems": [self.invoice_item_for_period(booking, period, period_id, invoice_amount)],
        }
        rates = usd_exchange_rates(booking)
        if rates:
            invoice_input["exchangeRates"] = rates
            invoice_input["bookingExchangeRates"] = rates
        day = start_of_day(start)
        payload = {
            "issueDate": day,
            "dueDate": day,
            "saveAndConfirm": True,
            "isSingleInvoice": True,
            "locationId": self.locations[building],
            "invoices": [invoice_input],
        }
        if rates:
            payload["exchangeRates"] = rates
        invoices = self.client.mutate(
            self.urls["payment"],
            "createInvoices",
            "CreateInvoicesCommandInput",
            "id code",
            payload,
        )
        invoice = invoices[0] if isinstance(invoices, list) else invoices
        self.step("invoice", name, "created", invoice["id"], invoice.get("code", ""))
        return invoice

    def collect_period(self, booking, booking_id, name, invoice_id, amount, start, building, customer_id, method):
        payload = {
            "bookingId": booking_id,
            "invoices": [{"id": invoice_id, "amount": amount}],
            "paymentMethodType": graphql_enum("bank"),
            "amount": amount,
            "transactionDate": start_of_day(start),
            "locationId": self.locations[building],
            "customerId": customer_id,
            "note": booking["contract_no"] or "",
        }
        if method:
            payload["paymentMethodId"] = method
        transaction = self.client.mutate(
            self.urls["payment"],
            "createInvoicePaymentTransaction",
            "CreateInvoicePaymentTransactionCommandInput",
            "id code",
            payload,
        )
        self.step("transaction", name, "created", transaction["id"], transaction.get("code", ""))

    def complete_booking(self, booking_id: str, name: str):
        self.client.call(
            self.urls["reservation"],
            "mutation($id: ObjectID!) { completeBooking(id: $id) { success } }",
            {"id": booking_id},
        )
        self.step("completeBooking", name, "created", booking_id)


class PreviewClient:
    """Records mutations locally; does not call PMS."""

    def __init__(self):
        self.calls: list[dict] = []
        self._bookings: dict[str, dict] = {}
        self.locations: dict[str, dict] = {}
        self.floors: list[dict] = []
        self.services: dict[str, dict] = {}

    def seed_catalog(self, spaces: list[dict]):
        for space in spaces:
            building = space["building"]
            if building not in self.locations:
                self.locations[building] = {"id": f"@location:{building}", "name": building}
            location_id = self.locations[building]["id"]
            floor_name = space["floor"]
            if not any(item["locationId"] == location_id and item["name"]["original"] == floor_name for item in self.floors):
                self.floors.append({
                    "id": f"@floor:{building}/{floor_name}",
                    "name": {"original": floor_name},
                    "locationId": location_id,
                })
            folded = search_text(space["service"])
            self.services.setdefault(folded, {
                "id": f"@service:{space['service']}",
                "name": {"original": space["service"]},
                "searchText": folded,
            })

    def items(self, url: str, field: str, input_type: str, selection: str, filt: dict) -> list:
        if field == "paymentPeriods":
            return self._preview_periods(filt)
        if field == "paymentMethods":
            return [{"id": "@paymentMethod:bank", "type": "bank"}]
        if field == "contractTemplates":
            folded = (filt.get("searchText") or {}).get("eq") or ""
            return [{"id": f"@contractTemplate:{folded}", "name": folded, "searchText": folded}]
        if field == "locations":
            folded = (filt.get("searchText") or {}).get("eq")
            name = (filt.get("name") or {}).get("eq")
            if not folded and not name:
                return list(self.locations.values())
            if folded:
                item = next(
                    (value for value in self.locations.values() if search_text(value["name"]) == folded),
                    None,
                )
                return [item] if item else []
            item = self.locations.get(name)
            return [item] if item else []
        if field == "floors":
            location_id = (filt.get("locationId") or {}).get("eq")
            return [item for item in self.floors if item["locationId"] == location_id]
        if field == "services":
            folded = (filt.get("searchText") or {}).get("eq")
            item = self.services.get(folded)
            return [item] if item else list(self.services.values())
        return []

    def _preview_periods(self, filt: dict) -> list:
        booking_id = (filt.get("bookingId") or {}).get("eq")
        booking = self._bookings.get(booking_id)
        if not booking:
            return []
        monthly = 0
        for room in booking.get("rooms") or []:
            for package in room.get("packages") or []:
                monthly += package.get("amount") or 0
        start = as_day(booking.get("startTime"))
        end = as_day(booking.get("endTime"))
        duration = int((booking.get("paymentSettings") or {}).get("duration") or 1)
        if not start or not end:
            return []
        monthly_calc = booking.get("monthlyCalculationType") or "Cycle"
        return [
            {
                "id": f"{booking_id}:period:{index}",
                "totalAmount": monthly * period["months"],
                "startDate": start_of_day(period["start"]),
                "endDate": end_of_day(period["end"]),
            }
            for index, period in enumerate(
                booking_periods(start, end, duration, monthly_calc),
                start=1,
            )
        ]

    def mutate(self, url: str, field: str, input_type: str, selection: str, payload: dict):
        ref = f"@{field}:{len(self.calls) + 1}"
        self.calls.append({
            "mutation": field,
            "service": url,
            "inputType": input_type,
            "title": call_title(field, payload),
            "input": payload,
            "apiFills": API_FILLS.get(field, ["id"]),
        })
        if field == "createBooking":
            self._bookings[ref] = payload
            return {"id": ref, "code": None}
        if field == "createRoom":
            package = payload["packages"][0]
            return {
                "id": ref,
                "packages": [{
                    "id": f"{ref}:package",
                    "serviceId": package["serviceId"],
                    "unit": package["unit"],
                    "amount": package["amount"],
                    "originalAmount": package["amount"],
                    "priceType": package["priceType"],
                }],
            }
        if field == "mutatePackages":
            return [
                {
                    "id": f"{ref}:{index}",
                    "serviceId": package["serviceId"],
                    "unit": package["unit"],
                    "amount": package["amount"],
                    "priceType": package["priceType"],
                }
                for index, package in enumerate(payload["packages"])
            ]
        if field == "createInvoices":
            return [{"id": ref, "code": None}]
        if field == "createInvoicePaymentTransaction":
            return {"id": ref, "code": None}
        return {"id": ref}

    def call(self, url: str, query: str, variables: dict | None = None) -> dict:
        self.calls.append({
            "mutation": "completeBooking",
            "service": url,
            "inputType": "id",
            "title": (variables or {}).get("id", ""),
            "input": variables or {},
            "apiFills": [],
        })
        return {"completeBooking": {"success": True}}


def call_title(field: str, payload: dict) -> str:
    if field == "createRoom":
        return (payload.get("name") or {}).get("original") or field
    if field == "createCompany":
        return (payload.get("companyName") or {}).get("vi") or field
    if field == "createIndividual":
        return payload.get("contactName") or field
    if field == "createBooking":
        return payload.get("note") or payload.get("startTime") or field
    if field == "createInvoices":
        booking = (payload.get("invoices") or [{}])[0].get("bookingId", "")
        return str(booking)
    if field == "mutatePackages":
        return payload.get("roomId") or field
    if field == "createInvoicePaymentTransaction":
        return payload.get("note") or payload.get("bookingId") or field
    if field == "createContract":
        return payload.get("code") or field
    return field


API_FILLS = {
    "createRoom": ["id", "uniqueKey", "searchText", "priority", "totalPackages", "serviceIds"],
    "mutatePackages": ["id", "uniqueKey", "searchText"],
    "createCompany": ["id", "companyCode", "uniqueKey", "searchText"],
    "createIndividual": ["id", "individualCode", "searchText"],
    "createBooking": ["id", "code", "paymentPeriods"],
    "createInvoices": ["id", "code", "codeNumber"],
    "createInvoicePaymentTransaction": ["id", "code", "transactionTypeId"],
    "createContract": ["id", "code", "status"],
}


def _workbook_result(
    as_of,
    errors,
    calls,
    skipped,
    error,
    steps=None,
    imported=False,
    summary=None,
    path=None,
) -> dict:
    payload = {
        "asOf": as_of.isoformat() if hasattr(as_of, "isoformat") else as_of,
        "errors": errors,
        "calls": calls,
        "skipped": skipped,
        "error": error,
        "steps": steps or [],
        "imported": imported,
        "summary": summary or {"created": {}, "reused": {}, "vacantRooms": 0},
    }
    if errors and path:
        try:
            payload["errorReport"] = {
                "filename": "bao-cao-loi.xlsx",
                "content": base64.b64encode(build_error_report(path, errors)).decode("ascii"),
            }
        except Exception:
            payload["errorReport"] = None
    return payload


def preview_workbook(path, domain: str = "", access_token: str = "", refresh_token: str = "") -> dict:
    data = read_onboarding(path)
    as_of = data["asOf"]
    if data["errors"]:
        return _workbook_result(as_of, data["errors"], [], [], "", path=path)
    client = PreviewClient()
    client.seed_catalog(data["spaces"])
    urls = {key: key for key in ("property", "customer", "reservation", "payment", "contract")}
    importer = Importer(client, urls, {"paymentMethodId": ""})
    result = importer.run(as_of, data["spaces"], data["bookings"])
    return _workbook_result(
        as_of,
        importer.issues,
        client.calls,
        [item for item in result["steps"] if item["status"] == "skipped"],
        result.get("error") or "",
        result["steps"],
        summary=importer.summary(),
        path=path,
    )


class LiveClient:

    def __init__(self, namespace: str, access_token: str, refresh_token: str = ""):
        self.namespace = namespace
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def refresh_if_possible(self):
        if not self.refresh_token:
            return
        try:
            tokens = refresh_tokens(self.namespace, self.refresh_token)
        except IdentityError:
            return
        self.access_token = tokens["accessToken"]
        self.refresh_token = tokens["refreshToken"]

    def graphql(self, url: str, query: str, variables: dict | None = None, retry: bool = True) -> dict:
        try:
            with self._lock:
                token = self.access_token
                refresh = self.refresh_token
                ns = self.namespace
            return graphql(query, variables, ns, token, url=url, timeout=60)
        except IdentityError as error:
            if retry and refresh and (error.status_code == 401 or error.code == "SESSION_EXPIRED"):
                with self._lock:
                    self.refresh_if_possible()
                return self.graphql(url, query, variables, retry=False)
            raise SmartOSError(str(error)) from error

    def items(self, url: str, field: str, input_type: str, selection: str, filt: dict) -> list:
        if field == "paymentMethods":
            location_id = (filt.get("locationId") or {}).get("eq")
            if not location_id:
                return []
            data = self.graphql(
                url,
                f"query($locationId: ObjectID!) {{ paymentMethods(locationId: $locationId) {{ {selection} }} }}",
                {"locationId": location_id},
            )
            rows = data.get("paymentMethods") or []
            wanted = (filt.get("type") or {}).get("eq")
            return [
                item
                for item in rows
                if not wanted or graphql_enum(item.get("type")) == graphql_enum(wanted)
            ]

        extra_defs = ""
        extra_args = ""
        extra_vars: dict = {}
        graphql_filter = _graphql_filter(field, filt)
        if field == "floors":
            location_id = (filt.get("locationId") or {}).get("eq")
            if not location_id:
                return []
            extra_defs = ", $locationId: ObjectID!"
            extra_args = ", locationId: $locationId"
            extra_vars["locationId"] = location_id

        collected = []
        page = 1
        size = 100
        while page <= 50:
            payload: dict = {"paginate": {"page": page, "size": size}}
            if graphql_filter:
                payload["filter"] = graphql_filter
            try:
                data = self.graphql(
                    url,
                    f"""
                    query($input: {input_type}!{extra_defs}) {{
                      {field}(input: $input{extra_args}) {{
                        items {{ {selection} }}
                      }}
                    }}
                    """,
                    {"input": payload, **extra_vars},
                )
            except SmartOSError as error:
                raise SmartOSError(f"{error} [{field}]") from error
            result = data.get(field)
            batch = _rows_from_list(result)
            collected.extend(batch)
            if len(batch) < size:
                break
            page += 1
        return collected

    def mutate(self, url: str, field: str, input_type: str, selection: str, payload: dict):
        try:
            data = self.graphql(
                url,
                f"""
                mutation($input: {input_type}!) {{
                  {field}(input: $input) {{
                    {selection}
                  }}
                }}
                """,
                {"input": payload},
            )
        except SmartOSError as error:
            raise SmartOSError(f"{error} [{field}]") from error
        with self._lock:
            self.calls.append({
                "mutation": field,
                "service": url,
                "inputType": input_type,
                "title": call_title(field, payload),
                "input": payload,
                "apiFills": API_FILLS.get(field, ["id"]),
            })
        return data.get(field)

    def call(self, url: str, query: str, variables: dict | None = None) -> dict:
        data = self.graphql(url, query, variables)
        with self._lock:
            self.calls.append({
                "mutation": "completeBooking",
                "service": url,
                "inputType": "id",
                "title": (variables or {}).get("id", ""),
                "input": variables or {},
                "apiFills": [],
            })
        return data


def _i18n_texts(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    texts = []
    original = value.get("original")
    if original:
        texts.append(str(original).strip())
    for key in ("en", "vi", "fr", "ja"):
        content = value.get(key)
        if content:
            texts.append(str(content).strip())
    for row in value.get("translations") or []:
        if not isinstance(row, dict):
            continue
        content = row.get("content")
        if content:
            texts.append(str(content).strip())
    return [item for item in texts if item]


def _service_matches(item: dict, name: str, folded: str) -> bool:
    """Match Excel name against original, translations, or joined searchText slugs."""
    labels = _i18n_texts(item.get("name"))
    stored = str(item.get("searchText") or "")
    wanted = name.strip().casefold()
    if any(label.casefold() == wanted or search_text(label) == folded for label in labels):
        return True
    if folded and folded in stored:
        return True
    return folded in {part for part in stored.split(":") if part}


def _service_missing_message(name: str, found: list[dict]) -> str:
    labels = []
    for item in found:
        labels.extend(_i18n_texts(item.get("name")) or [str(item.get("searchText") or item.get("id") or "")])
    shown = ", ".join(item for item in dict.fromkeys(labels) if item)
    if shown:
        return (
            f'Dịch vụ "{name}" không khớp tên trên hệ thống. '
            f'PMS đang có: {shown}. Đổi tên cột Dịch vụ cho đúng hoặc gán service vào tòa nhà.'
        )
    return (
        f'Dịch vụ "{name}" chưa có trên hệ thống. '
        "Tạo service và gán vào tòa nhà trước khi import phòng."
    )


def _graphql_filter(field: str, filt: dict) -> dict:
    """LocationFilterInput has searchText, not name. Drop name from every list filter."""
    out = {}
    for key, value in (filt or {}).items():
        if key == "name":
            eq = value.get("eq") if isinstance(value, dict) else None
            if eq and "searchText" not in (filt or {}) and "searchText" not in out:
                out["searchText"] = {"eq": search_text(eq)}
            continue
        out[key] = value
    if field == "locations":
        return {key: value for key, value in out.items() if key != "name"}
    return out


def _rows_from_list(result) -> list:
    if result is None:
        return []
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in ("items", "data", "nodes"):
            value = result.get(key)
            if isinstance(value, list):
                return value
    return []


def import_workbook(
    path,
    domain: str,
    access_token: str,
    refresh_token: str = "",
    *,
    skip_booking_keys: set[str] | None = None,
    on_progress: Callable[[dict], None] | None = None,
) -> dict:
    data = read_onboarding(path)
    as_of = data["asOf"]
    if data["errors"]:
        return _workbook_result(as_of, data["errors"], [], [], "", path=path)
    namespace = namespace_of(domain)
    if not namespace or not (access_token or "").strip():
        raise SmartOSError("Cần đăng nhập trước khi ghi dữ liệu")
    client = LiveClient(namespace, access_token.strip(), (refresh_token or "").strip())
    client.refresh_if_possible()
    urls = {
        key: service_graphql_url(key)
        for key in ("property", "customer", "reservation", "payment", "contract")
    }
    importer = Importer(client, urls, {"paymentMethodId": ""})
    result = importer.run(
        as_of,
        data["spaces"],
        data["bookings"],
        skip_booking_keys=skip_booking_keys,
        on_progress=on_progress,
    )
    payload = _workbook_result(
        as_of,
        importer.issues,
        client.calls,
        [item for item in result["steps"] if item["status"] == "skipped"],
        "",
        result["steps"],
        imported=True,
        summary=importer.summary(),
        path=path,
    )
    payload["accessToken"] = client.access_token
    payload["refreshToken"] = client.refresh_token
    payload["completedKeys"] = result.get("completedKeys") or []
    return payload


def run_import_job(job_id: str) -> None:
    """Background worker entry: load job, import with checkpoint progress."""
    from jobs import cleanup_workbook, load_job, public_job, save_job, update_progress, workbook_path

    job = load_job(job_id)
    if not job:
        return
    path = workbook_path(job_id)
    if not path.exists():
        update_progress(job_id, status="error", phase="error", error="Không tìm thấy file job", message="Lỗi")
        return

    skip = set(job.get("completedKeys") or [])
    update_progress(
        job_id,
        status="running",
        phase="starting",
        message="Bắt đầu import",
        error="",
    )

    def on_progress(info: dict):
        update_progress(
            job_id,
            status="running",
            phase=info.get("phase"),
            message=info.get("message"),
            total_bookings=info.get("totalBookings"),
            done_bookings=info.get("doneBookings"),
            completed_keys=info.get("completedKeys"),
            recent_steps=info.get("recentSteps"),
        )

    try:
        result = import_workbook(
            path,
            job["domain"],
            job["accessToken"],
            job.get("refreshToken") or "",
            skip_booking_keys=skip,
            on_progress=on_progress,
        )
        # Persist refreshed tokens for resume
        job = load_job(job_id) or job
        if result.get("accessToken"):
            job["accessToken"] = result["accessToken"]
        if result.get("refreshToken"):
            job["refreshToken"] = result["refreshToken"]
        job["completedKeys"] = result.get("completedKeys") or job.get("completedKeys") or []
        save_job(job)
        # Drop heavy call list from stored result if huge — keep for UI but cap
        stored = dict(result)
        calls = stored.get("calls") or []
        if len(calls) > 500:
            stored["calls"] = calls[-500:]
            stored["callsTruncated"] = True
        update_progress(
            job_id,
            status="done",
            phase="done",
            message="Import xong",
            percent=100,
            done_bookings=job.get("totalBookings") or len(job.get("completedKeys") or []),
            completed_keys=job.get("completedKeys"),
            result=stored,
            error="",
        )
        cleanup_workbook(job_id)
    except Exception as caught:
        update_progress(
            job_id,
            status="error",
            phase="error",
            message="Import lỗi",
            error=str(caught),
        )
