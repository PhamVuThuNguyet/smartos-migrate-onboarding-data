"""Build the PMS GraphQL mutations a sales user would call, in that order.

Preview records each call and does not write the database.
"""

from __future__ import annotations

import re
import unicodedata
from calendar import monthrange
from datetime import date, datetime, timedelta

from onboarding import issue, looks_like_company, read_onboarding

CATEGORY_NAME = "Cho thuê mặt bằng"

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


class SmartOSError(Exception):
    pass


def i18n(value: str) -> dict:
    return {"original": value, "translations": [{"lang": "vi", "content": value}]}


def iso(day: date) -> str:
    return f"{day.isoformat()}T00:00:00.000Z"


def as_day(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and len(value) >= 10:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


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
    """Split a contract into billing periods from its start date.

    A monthly cycle is start, start+1 month, start+2 months, and so on.
    A period ends the day before the next one starts, and the last period
    stops at the contract end.
    """
    if end <= start:
        end = start + timedelta(days=1)
    step = max(1, step_months)
    periods = []
    cursor = start
    guard = 0
    while cursor <= end and guard < 600:
        guard += 1
        period_end = min(add_months(cursor, step) - timedelta(days=1), end)
        periods.append({"start": cursor, "end": period_end, "months": step if period_end == add_months(cursor, step) - timedelta(days=1) else _covered_months(cursor, period_end)})
        nxt = period_end + timedelta(days=1)
        if nxt <= cursor:
            break
        cursor = nxt
    return periods


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


def working_day() -> dict:
    opened = {"isOpened": True, "open": 8, "close": 18}
    closed = {"isOpened": False, "open": 8, "close": 18}
    return {
        "isDifferent": False,
        "monday": opened,
        "tuesday": opened,
        "wednesday": opened,
        "thursday": opened,
        "friday": opened,
        "saturday": closed,
        "sunday": closed,
    }


class Importer:
    def __init__(self, client: SmartOS, urls: dict, options: dict):
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
        self.extra_service_types: dict[str, str] = {}
        self.transfer_methods: dict[str, str] = {}
        self.contract_templates: dict[str, str] = {}

    def step(self, action: str, name: str, status: str, record_id: str = "", detail: str = ""):
        self.steps.append(
            {"action": action, "name": name, "status": status, "id": record_id, "detail": detail}
        )

    def run(self, as_of: date, spaces: list[dict], bookings: list[dict]) -> dict:
        try:
            category_id = self.ensure_category()
            for name, geo in self.collect_buildings(spaces).items():
                self.ensure_location(name, geo)
            self.ensure_services(category_id, spaces)
            self.ensure_floors_and_rooms(spaces)
            self.ensure_extra_services(bookings)
            # SmartOS marks a customer's first open booking as the main booking.
            for booking in sorted(bookings, key=lambda item: not item["main"]):
                self.ensure_customer(booking)
                self.ensure_booking(as_of, booking)
        except SmartOSError as caught:
            self.step("error", "dừng", "error", detail=str(caught))
            return {"steps": self.steps, "error": str(caught)}
        return {"steps": self.steps, "error": ""}

    def collect_buildings(self, spaces: list[dict]) -> dict:
        found = {}
        for space in spaces:
            name = space["building"]
            found.setdefault(name, BUILDING_DUMMY.get(name, {
                "address": f"{name}, Việt Nam",
                "city": "Hồ Chí Minh",
                "lng": 106.7,
                "lat": 10.77,
            }))
        return found

    def ensure_category(self) -> str:
        given = (self.options.get("categoryId") or "").strip()
        if given:
            self.step("serviceCategory", CATEGORY_NAME, "reused", given, "Dùng categoryId đã có")
            return given
        found = self.client.items(
            self.urls["property"],
            "serviceCategories",
            "ServiceCategoryQueryInput",
            "id name { original }",
            {"name": {"original": {"eq": CATEGORY_NAME}}},
        )
        match = next((item for item in found if item["name"]["original"] == CATEGORY_NAME), None)
        if match:
            self.step("serviceCategory", CATEGORY_NAME, "reused", match["id"])
            return match["id"]
        icon = (self.options.get("categoryIcon") or "").strip()
        if not icon:
            raise SmartOSError("Thiếu categoryIcon (file key) hoặc categoryId của category đã có")
        created = self.client.mutate(
            self.urls["property"],
            "createServiceCategory",
            "CreateServiceCategoryCommandInput",
            "id",
            {
                "name": i18n(CATEGORY_NAME),
                "icon": icon,
                "priority": 1,
                "color": (self.options.get("categoryColor") or "#1C1917").strip(),
            },
        )
        self.step("serviceCategory", CATEGORY_NAME, "created", created["id"])
        return created["id"]

    def ensure_location(self, name: str, geo: dict):
        found = self.client.items(
            self.urls["property"],
            "locations",
            "LocationQueryInput",
            "id name",
            {"name": {"eq": name}},
        )
        match = next((item for item in found if item["name"] == name), None)
        if match:
            self.locations[name] = match["id"]
            self.step("location", name, "reused", match["id"])
            return
        created = self.client.mutate(
            self.urls["property"],
            "createLocation",
            "CreateLocationCommandInput",
            "id",
            {
                "name": name,
                "address": {"text": geo["address"], "city": geo["city"], "country": "VN"},
                "geoLocation": {"coordinates": [geo["lng"], geo["lat"]]},
                "workingDay": working_day(),
                "serviceIds": [],
                "representativeId": self.options["representativeId"].strip(),
            },
        )
        self.locations[name] = created["id"]
        self.step("location", name, "created", created["id"])

    def ensure_services(self, category_id: str, spaces: list[dict]):
        by_name: dict[str, set[str]] = {}
        for space in spaces:
            by_name.setdefault(space["service"], set()).add(self.locations[space["building"]])
        for name, location_ids in by_name.items():
            folded = search_text(name)
            found = self.client.items(
                self.urls["property"],
                "services",
                "ServiceQueryInput",
                "id name { original } searchText",
                {"searchText": {"eq": folded}},
            )
            match = next(
                (
                    item for item in found
                    if item.get("searchText") == folded or (item.get("name") or {}).get("original") == name
                ),
                None,
            )
            if match:
                self.services[name] = match["id"]
                self.step("service", name, "reused", match["id"])
                continue
            created = self.client.mutate(
                self.urls["property"],
                "createService",
                "CreateServiceCommandInput",
                "id",
                {
                    "name": i18n(name),
                    "isActive": True,
                    "isPublic": True,
                    "categoryId": category_id,
                    "locationIds": sorted(location_ids),
                },
            )
            self.services[name] = created["id"]
            self.step("service", name, "created", created["id"])

    def ensure_floors_and_rooms(self, spaces: list[dict]):
        grouped: dict[tuple[str, str], list[dict]] = {}
        for space in spaces:
            grouped.setdefault((space["building"], space["floor"]), []).append(space)
        for (building, floor_name), members in grouped.items():
            floor_id = self.ensure_floor(building, floor_name)
            for space in members:
                self.ensure_room(building, floor_id, space)

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
        if match:
            self.floors[key] = match["id"]
            self.step("floor", f"{building} / {floor_name}", "reused", match["id"])
            return match["id"]
        created = self.client.mutate(
            self.urls["property"],
            "createFloor",
            "CreateFloorCommandInput",
            "id",
            {
                "name": i18n(floor_name),
                "locationId": location_id,
                "rentalTypes": ["room"],
            },
        )
        self.floors[key] = created["id"]
        self.step("floor", f"{building} / {floor_name}", "created", created["id"])
        return created["id"]

    def ensure_room(self, building: str, floor_id: str, space: dict):
        """One template row is one room with one monthly package."""
        service = space["service"]
        amount = space["rent"]
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
            self.attach_package(room, service, "per-room", amount, space["room"])
            return
        created = self.client.mutate(
            self.urls["property"],
            "createRoom",
            "CreateRoomCommandInput",
            "id packages { id serviceId unit amount priceType }",
            {
                "name": i18n(space["room"]),
                "area": space["area"],
                "isZone": False,
                "floorId": floor_id,
                "locationId": self.locations[building],
                "capacity": max(1, round(space["area"] / 10)),
                "tier": "Standard",
                "packages": [{
                    "priceType": "per-room",
                    "unit": "months",
                    "amount": amount,
                    "serviceId": self.services[service],
                }],
            },
        )
        self.rooms[(building, space["room"])] = {"id": created["id"], "packages": created.get("packages") or []}
        self.step("room", f"{building} / {space['room']}", "created", created["id"])

    def attach_package(self, room: dict, service: str, price_type: str, amount: float, room_name: str):
        service_id = self.services[service]
        for package in room["packages"]:
            if package.get("serviceId") == service_id and package.get("unit") == "months":
                return
        kept = [
            {
                "priceType": item["priceType"],
                "unit": item["unit"],
                "amount": item["amount"],
                "serviceId": item["serviceId"],
            }
            for item in room["packages"]
        ]
        kept.append({
            "priceType": price_type,
            "unit": "months",
            "amount": amount,
            "serviceId": service_id,
        })
        updated = self.client.mutate(
            self.urls["property"],
            "mutatePackages",
            "MutatePackagesCommandInput",
            "id serviceId unit amount priceType",
            {"roomId": room["id"], "packages": kept},
        )
        room["packages"] = updated if isinstance(updated, list) else [updated]
        self.step("package", f"{room_name} / {service}", "created")

    def ensure_extra_services(self, bookings: list[dict]):
        category_id = (self.options.get("extraServiceCategoryId") or "").strip()
        unit_id = (self.options.get("unitId") or "").strip()
        if not category_id or not unit_id:
            self.step(
                "extraServiceType",
                "Phí dịch vụ",
                "skipped",
                detail="Thiếu extraServiceCategoryId hoặc unitId, bỏ qua phí dịch vụ",
            )
            return
        if not any(space["fee"] > 0 for booking in bookings for space in booking["spaces"]):
            self.step("extraServiceType", "Phí dịch vụ", "skipped", detail="Không booking nào có phí dịch vụ")
            return
        seen = set()
        for booking in bookings:
            building = booking["building"]
            if building in seen:
                continue
            seen.add(building)
            location_id = self.locations[building]
            found = self.client.items(
                self.urls["reservation"],
                "extraServiceTypes",
                "ExtraServiceTypeQueryInput",
                "id name { original }",
                {"locationId": {"eq": location_id}},
            )
            match = next((item for item in found if item["name"]["original"] == "Phí dịch vụ"), None)
            if match:
                self.extra_service_types[building] = match["id"]
                self.step("extraServiceType", f"{building} / Phí dịch vụ", "reused", match["id"])
                continue
            created = self.client.mutate(
                self.urls["reservation"],
                "createExtraServiceType",
                "CreateExtraServiceTypeCommandInput",
                "id",
                {
                    "name": i18n("Phí dịch vụ"),
                    "price": 0,
                    "unit": i18n("tháng"),
                    "unitId": unit_id,
                    "extraServiceCategoryId": category_id,
                    "locationId": location_id,
                },
            )
            self.extra_service_types[building] = created["id"]
            self.step("extraServiceType", f"{building} / Phí dịch vụ", "created", created["id"])

    def customer_key(self, booking: dict) -> str:
        name = booking["name"]
        if booking["tax"]:
            return f"tax:{booking['tax']}"
        kind = "company" if looks_like_company(name) else "name"
        return f"{kind}:{name}"

    def ensure_customer(self, booking: dict):
        name = booking["name"]
        tax = booking["tax"]
        company = bool(tax) or looks_like_company(name)
        key = self.customer_key(booking)
        if key in self.customers:
            return
        emails = list(booking["emails"]) or [f"{name.lower().replace(' ', '.')}@onboarding.invalid"]
        if tax:
            found = self.client.items(
                self.urls["customer"],
                "customers",
                "CustomerQueryInput",
                "id taxCode",
                {"taxCode": {"eq": tax}},
            )
            match = next((item for item in found if item.get("taxCode") == tax), None)
        else:
            found = self.client.items(
                self.urls["customer"],
                "customers",
                "CustomerQueryInput",
                "id email",
                {"email": {"eq": emails[0].lower()}},
            )
            match = next((item for item in found if (item.get("email") or "").lower() == emails[0].lower()), None)
        if match:
            self.customers[key] = match["id"]
            self.step("customer", name, "reused", match["id"])
            return
        building = booking["building"]
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
        self.customers[key] = created["id"]
        self.step("customer", name, "created", created["id"])

    def find_bookings(self, booking: dict, ready: list) -> list:
        """The booking SmartOS already has for this customer, location, room set and exact times.

        `roomIds.in` is an intersection, so the room set is compared again here.
        Canceled bookings are ignored.
        """
        start, end = booking["start"], booking["end"]
        room_ids = [room["id"] for _space, room in ready]
        found = self.client.items(
            self.urls["reservation"],
            "bookings",
            "BookingQueryInput",
            "id code status startTime endTime roomIds roomServices { extraServiceTypeId }",
            {
                "locationId": {"eq": self.locations[booking["building"]]},
                "primaryCustomerId": {"eq": self.customers[self.customer_key(booking)]},
                "startTime": {"eq": iso(start)},
                "endTime": {"eq": iso(end)},
                "status": {"ne": "canceled"},
                "roomIds": {"in": room_ids},
            },
        )
        wanted = set(room_ids)
        matches = []
        for item in found:
            if item.get("status") == "canceled":
                continue
            if as_day(item.get("startTime")) != start or as_day(item.get("endTime")) != end:
                continue
            if {str(room_id) for room_id in item.get("roomIds") or []} == wanted:
                matches.append(item)
        return matches

    def overlapping_booking(self, booking: dict, ready: list) -> dict | None:
        """E13: an existing room already has a booking that overlaps this one.

        Same window as getBookingsForFloorPlan: startTime < end and endTime > start.
        A booking that only touches the boundary is not an overlap.
        """
        reused = [(space, room) for space, room in ready if room.get("reused")]
        if not reused:
            return None
        start, end = booking["start"], booking["end"]
        by_id = {room["id"]: space for space, room in reused}
        found = self.client.items(
            self.urls["reservation"],
            "bookings",
            "BookingQueryInput",
            "id code status startTime endTime roomIds",
            {
                "roomIds": {"in": list(by_id)},
                "startTime": {"lt": iso(end)},
                "endTime": {"gt": iso(start)},
                "status": {"ne": "canceled"},
            },
        )
        for item in found:
            if item.get("status") == "canceled":
                continue
            item_start, item_end = as_day(item.get("startTime")), as_day(item.get("endTime"))
            if item_start is None or item_end is None or not (item_start < end and item_end > start):
                continue
            overlap = {str(room_id) for room_id in item.get("roomIds") or []} & set(by_id)
            space = by_id[next(iter(overlap))] if overlap else reused[0][0]
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
        ready = [
            (space, self.rooms[(building, space["room"])])
            for space in booking["spaces"]
            if (building, space["room"]) in self.rooms
        ]
        if not ready:
            self.step("booking", name, "skipped", detail="Không có phòng nào được tạo cho booking này")
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
            self.apply_service_fee(booking_id, building, ready, name, existing)
        else:
            clash = self.overlapping_booking(booking, ready)
            if clash:
                self.issues.append(clash)
                self.step("booking", name, "skipped", detail=clash["message"])
                return
            booking_id = self.create_booking(booking, ready, name)
        self.ensure_contract_record(booking, booking_id, name)
        self.invoice_elapsed_periods(as_of, booking, booking_id, name, reused=existing is not None)
        if booking["end"] < as_of:
            if existing and existing.get("status") == "completed":
                self.step("completeBooking", name, "reused", booking_id)
            else:
                self.complete_booking(booking_id, name)

    def create_booking(self, booking: dict, ready: list, name: str) -> str:
        building = booking["building"]
        rooms: dict[str, dict] = {}
        for space, room in ready:
            service = space["service"]
            service_id = self.services[service]
            package = next(
                (
                    item for item in room["packages"]
                    if item.get("serviceId") == service_id and item.get("unit") == "months"
                ),
                None,
            )
            if package is None:
                raise SmartOSError(f"Phòng {space['room']} chưa có package tháng cho {service}")
            entry = rooms.setdefault(room["id"], {"id": room["id"], "packages": []})
            entry["packages"].append({"id": package["id"], "amount": space["rent"]})
        deposit = booking["deposit"]
        mode, duration = booking["cycle"]
        start = booking["start"]
        end = booking["end"]
        duration = span_months(start, end) if mode == "full" else duration
        first_end = min(add_months(start, duration) - timedelta(days=1), end)
        customer_id = self.customers[self.customer_key(booking)]
        payload = {
            "locationId": self.locations[building],
            "rooms": list(rooms.values()),
            "customerIds": [customer_id],
            "primaryCustomerId": customer_id,
            "startTime": iso(start),
            "endTime": iso(end),
            "selectTime": False,
            "monthlyCalculationType": "Cycle",
            "daysOfMonthCalculationType": "DaysOfCalendar",
            "numberOfPeople": 1,
            "applyTax": False,
            "currency": booking["currency"],
            "markAsConfirmed": True,
            "paymentSettings": {
                "unit": "months",
                "duration": duration,
                "firstPeriodPaymentStartDate": iso(start),
                "firstPeriodPaymentEndDate": iso(first_end),
            },
        }
        if booking["note"]:
            payload["note"] = booking["note"]
        if booking["currency"] == "USD":
            payload["exchangeRates"] = [{"currency": "USD", "rate": round(booking["fx"])}]
        if deposit > 0:
            deposit_input = {
                "requiredAmount": deposit,
                "paidAmount": deposit,
                "transactionDate": iso(start),
            }
            method = self.transfer_method_id(self.locations[building])
            if method:
                deposit_input["paymentMethodId"] = method
            payload["deposit"] = deposit_input
        created = self.client.mutate(
            self.urls["reservation"],
            "createBooking",
            "CreateBookingCommandInput",
            "id code",
            payload,
        )
        self.step("booking", name, "created", created["id"], created.get("code", ""))
        self.apply_service_fee(created["id"], building, ready, name)
        return created["id"]

    def apply_service_fee(self, booking_id: str, building: str, ready: list, name: str, existing: dict | None = None):
        """Write the contract fee onto booking.roomServices. The catalog price stays 0.

        A booking that already has room services is left alone, so a rerun does not
        overwrite a fee changed in PMS.
        """
        if existing and existing.get("roomServices"):
            self.step("serviceFee", name, "reused", booking_id)
            return
        type_id = self.extra_service_types.get(building)
        monthly_fee = sum((space.get("fee") or 0) for space, _room in ready)
        if not type_id or monthly_fee <= 0:
            return
        self.client.mutate(
            self.urls["reservation"],
            "updateBookingRoomServices",
            "UpdateBookingRoomServicesCommandInput",
            "success",
            {
                "id": booking_id,
                "roomServices": [{
                    "extraServiceTypeId": type_id,
                    "type": "EXTRA_SERVICE",
                    "price": monthly_fee,
                    "name": i18n("Phí dịch vụ"),
                    "unit": i18n("tháng"),
                    "quantity": 1,
                }],
            },
        )
        self.step("serviceFee", name, "created", booking_id, str(monthly_fee))

    def transfer_method_id(self, location_id: str) -> str:
        """Default payment method is Transfer, stored as PaymentMethodType bank."""
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
        match = next((item for item in found if item.get("type") == "bank"), None)
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
                "effectiveDate": iso(start),
                "expiryDate": iso(end),
                "bookingStartTime": iso(start),
                "bookingEndTime": iso(end),
                "bookingNumberOfPeople": 1,
                "roomServices": [],
                "contractTemplateId": template_id,
                "bookingId": booking_id,
                "customerIds": [customer_id],
            },
        )
        self.step("contract", number, "created", created["id"], template_name)

    def invoices_by_period(self, booking_id: str) -> dict:
        """Invoices of this booking, keyed by the payment period on the rent line."""
        found = self.client.items(
            self.urls["payment"],
            "invoices",
            "InvoiceQueryInput",
            "id code paidAmount status invoiceItems { paymentPeriodId } transactions { id }",
            {"bookings": {"bookingId": {"eq": booking_id}}},
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

    def invoice_elapsed_periods(self, as_of: date, booking: dict, booking_id: str, name: str, reused: bool = False):
        start = booking["start"]
        end = booking["end"]
        mode, duration = booking["cycle"]
        step = span_months(start, end) if mode == "full" else duration
        due = [period for period in cycle_periods(start, end, step) if period["end"] < as_of]
        if not due:
            return
        stored = self.client.items(
            self.urls["reservation"],
            "paymentPeriods",
            "PaymentPeriodQueryInput",
            "id totalAmount startDate endDate",
            {"bookingId": {"eq": booking_id}},
        )
        by_start = {as_day(item.get("startDate")): item for item in stored}
        existing = self.invoices_by_period(booking_id)
        monthly = sum(space["rent"] for space in booking["spaces"])
        building = booking["building"]
        customer_id = self.customers[self.customer_key(booking)]
        method = self.transfer_method_id(self.locations[building])
        for index, period in enumerate(due, start=1):
            start = period["start"]
            end = period["end"]
            stored_period = by_start.get(start)
            if stored_period is None and reused:
                self.step("invoice", name, "skipped", detail=f"Không thấy payment period bắt đầu {start.isoformat()}")
                continue
            amount = float(stored_period["totalAmount"]) if stored_period and stored_period.get("totalAmount") else monthly * period["months"]
            period_id = stored_period["id"] if stored_period else f"{booking_id}:period:{index}"
            invoice = existing.get(str(period_id))
            if invoice:
                self.step("invoice", name, "reused", invoice["id"], invoice.get("code") or "")
                if amount <= 0 or self.invoice_collected(invoice, amount):
                    if amount > 0:
                        self.step("transaction", name, "reused", invoice["id"])
                    continue
                self.collect_period(booking, booking_id, name, invoice["id"], amount, start, building, customer_id, method)
                continue
            invoices = self.client.mutate(
                self.urls["payment"],
                "createInvoices",
                "CreateInvoicesCommandInput",
                "id code",
                {
                    "issueDate": iso(start),
                    "dueDate": iso(end if end >= start else start),
                    "saveAndConfirm": True,
                    "isSingleInvoice": True,
                    "locationId": self.locations[building],
                    "invoices": [{
                        "type": "booking",
                        "bookingId": booking_id,
                        "invoiceItems": [{
                            "category": "payment-period",
                            "name": i18n("Tiền thuê"),
                            "startTime": iso(start),
                            "endTime": iso(end),
                            "quantity": 1,
                            "unit": i18n("kỳ"),
                            "unitPrice": amount,
                            "discountAmount": 0,
                            "paymentPeriodId": period_id,
                        }],
                    }],
                },
            )
            invoice = invoices[0] if isinstance(invoices, list) else invoices
            self.step("invoice", name, "created", invoice["id"], invoice.get("code", ""))
            if amount <= 0:
                continue
            self.collect_period(booking, booking_id, name, invoice["id"], amount, start, building, customer_id, method)

    def collect_period(self, booking, booking_id, name, invoice_id, amount, start, building, customer_id, method):
        payload = {
            "bookingId": booking_id,
            "invoices": [{"id": invoice_id, "amount": amount}],
            "paymentMethodType": "bank",
            "amount": amount,
            "transactionDate": iso(start),
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
    """Does not call the network. Records each mutation and returns a symbolic id for later calls."""

    def __init__(self):
        self.calls: list[dict] = []
        self._bookings: dict[str, dict] = {}

    def items(self, url: str, field: str, input_type: str, selection: str, filt: dict) -> list:
        if field == "paymentMethods":
            return [{"id": "@paymentMethod:bank", "type": "bank"}]
        if field == "contractTemplates":
            folded = (filt.get("searchText") or {}).get("eq") or ""
            return [{"id": f"@contractTemplate:{folded}", "name": folded, "searchText": folded}]
        if field != "paymentPeriods":
            return []
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
        return [
            {
                "id": f"{booking_id}:period:{index}",
                "totalAmount": monthly * period["months"],
                "startDate": iso(period["start"]),
                "endDate": iso(period["end"]),
            }
            for index, period in enumerate(cycle_periods(start, end, duration), start=1)
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
            self.calls[-1]["previewNote"] = (
                "Mỗi payment period có endDate trước ngày chạy file là một invoice. "
                "issueDate và transactionDate là startDate của period đó. "
                "Payment method mặc định là Transfer (bank)."
            )
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
    if field in {"createServiceCategory", "createService", "createFloor", "createRoom", "createExtraServiceType"}:
        return (payload.get("name") or {}).get("original") or field
    if field == "createLocation":
        return payload.get("name") or field
    if field == "createCompany":
        return (payload.get("companyName") or {}).get("vi") or field
    if field == "createIndividual":
        return payload.get("contactName") or field
    if field == "createBooking":
        return payload.get("note") or payload.get("startTime") or field
    if field == "updateBookingRoomServices":
        services = payload.get("roomServices") or [{}]
        return str((services[0] or {}).get("price", field))
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
    "createServiceCategory": ["id", "searchText"],
    "createLocation": ["id", "uniqueKey", "searchText", "totalRooms", "totalArea", "totalFloors"],
    "createService": ["id", "uniqueKey", "searchText"],
    "createFloor": ["id", "uniqueKey", "searchText", "priority", "totalRooms"],
    "createRoom": ["id", "uniqueKey", "searchText", "priority", "totalPackages", "serviceIds"],
    "mutatePackages": ["id", "uniqueKey", "searchText"],
    "createExtraServiceType": ["id", "uniqueKey", "searchText"],
    "createCompany": ["id", "companyCode", "uniqueKey", "searchText"],
    "createIndividual": ["id", "individualCode", "searchText"],
    "createBooking": ["id", "code", "paymentPeriods"],
    "updateBookingRoomServices": ["roomServices"],
    "createInvoices": ["id", "code", "codeNumber"],
    "createInvoicePaymentTransaction": ["id", "code", "transactionTypeId"],
    "createContract": ["id", "code", "status"],
}


def preview_workbook(path) -> dict:
    data = read_onboarding(path)
    as_of = data["asOf"]
    if data["errors"]:
        return {"asOf": as_of.isoformat(), "errors": data["errors"], "calls": [], "skipped": [], "error": ""}
    client = PreviewClient()
    urls = {key: key for key in ("property", "customer", "reservation", "payment", "contract")}
    options = {
        "representativeId": "<representativeId>",
        "categoryIcon": "<categoryIcon>",
        "categoryColor": "#1C1917",
        "extraServiceCategoryId": "<extraServiceCategoryId>",
        "unitId": "<unitId>",
        "paymentMethodId": "",
    }
    importer = Importer(client, urls, options)
    result = importer.run(as_of, data["spaces"], data["bookings"])
    return {
        "asOf": as_of.isoformat(),
        "errors": importer.issues,
        "calls": client.calls,
        "skipped": [item for item in result["steps"] if item["status"] == "skipped"],
        "error": result.get("error") or "",
    }
