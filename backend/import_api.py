"""Build the PMS GraphQL mutations a sales user would call, in that order.

Preview records each call and does not write the database.
"""

from __future__ import annotations

import re
import unicodedata
from calendar import monthrange
from datetime import date, datetime, timedelta

from onboarding import (
    BUILDING_DUMMY,
    currency_code,
    deposit_amount,
    did,
    group_contracts,
    is_zone,
    load_rows,
    looks_like_company,
    parse_cycle,
)
from review import review_uncertain

CATEGORY_NAME = "Cho thuê mặt bằng"


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


def package_amount(space: dict, zone: bool) -> tuple[str, float]:
    rent = space["rent"]
    unit = space["unit_price"]
    area = space["area"] or 1
    if zone:
        amount = unit if unit is not None else rent / area
        return "per-square-meter", amount
    if space["price_type"] == "per-square-meter":
        return "per-room", rent
    return space["price_type"], unit if unit is not None else rent


class Importer:
    def __init__(self, client: SmartOS, urls: dict, options: dict):
        self.client = client
        self.urls = urls
        self.options = options
        self.steps: list[dict] = []
        self.locations: dict[str, str] = {}
        self.services: dict[str, str] = {}
        self.floors: dict[tuple[str, str], str] = {}
        self.rooms: dict[tuple[str, str, str], dict] = {}
        self.customers: dict[str, str] = {}
        self.extra_service_types: dict[str, str] = {}
        self.transfer_methods: dict[str, str] = {}
        self.contract_templates: dict[str, str] = {}

    def step(self, action: str, name: str, status: str, record_id: str = "", detail: str = ""):
        self.steps.append(
            {"action": action, "name": name, "status": status, "id": record_id, "detail": detail}
        )

    def run(self, as_of: date, contracts: list[dict]) -> dict:
        try:
            category_id = self.ensure_category()
            buildings = self.collect_buildings(contracts)
            for name, geo in buildings.items():
                self.ensure_location(name, geo)
            self.ensure_services(category_id, contracts)
            self.ensure_floors_and_rooms(contracts)
            self.ensure_extra_services(contracts)
            for contract in contracts:
                self.ensure_customer(contract)
                self.ensure_booking(as_of, contract)
        except SmartOSError as caught:
            self.step("error", "dừng", "error", detail=str(caught))
            return {"steps": self.steps, "error": str(caught)}
        return {"steps": self.steps, "error": ""}

    def collect_buildings(self, contracts: list[dict]) -> dict:
        found = {}
        for contract in contracts:
            name = contract["building"] or "Tòa không tên"
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

    def ensure_services(self, category_id: str, contracts: list[dict]):
        by_name: dict[str, set[str]] = {}
        for contract in contracts:
            service = contract["service"] or CATEGORY_NAME
            building = contract["building"] or "Tòa không tên"
            by_name.setdefault(service, set()).add(self.locations[building])
        for name, location_ids in by_name.items():
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
            self.step("service", name, "created", created["id"], "API gộp location nếu service đã có")

    def ensure_floors_and_rooms(self, contracts: list[dict]):
        grouped: dict[tuple[str, str], list] = {}
        for contract in contracts:
            building = contract["building"] or "Tòa không tên"
            service = contract["service"] or CATEGORY_NAME
            for space in contract["spaces"]:
                if space["rent_blocked"] or space["rent"] is None:
                    self.step("room", space["room"], "skipped", detail=f"Dòng {space['row']}: không có đơn giá thuê")
                    continue
                grouped.setdefault((building, space["floor"]), []).append((service, space))
        for (building, floor_name), spaces in grouped.items():
            floor_id = self.ensure_floor(building, floor_name, spaces)
            for service, space in spaces:
                self.ensure_room(building, floor_name, floor_id, service, space)

    def ensure_floor(self, building: str, floor_name: str, spaces: list) -> str:
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
        kinds = []
        for _service, space in spaces:
            kind = "area" if is_zone(_service, space) else "room"
            if kind not in kinds:
                kinds.append(kind)
        created = self.client.mutate(
            self.urls["property"],
            "createFloor",
            "CreateFloorCommandInput",
            "id",
            {
                "name": i18n(floor_name),
                "locationId": location_id,
                "rentalTypes": kinds or ["room"],
            },
        )
        self.floors[key] = created["id"]
        self.step("floor", f"{building} / {floor_name}", "created", created["id"])
        return created["id"]

    def ensure_room(self, building: str, floor_name: str, floor_id: str, service: str, space: dict):
        key = (building, floor_name, space["room"])
        zone = is_zone(service, space)
        price_type, amount = package_amount(space, zone)
        if amount <= 0:
            self.step("room", space["room"], "skipped", detail="Đơn giá không dương")
            return
        existing = self.rooms.get(key)
        if existing is None:
            found = self.client.items(
                self.urls["property"],
                "rooms",
                "RoomQueryInput",
                "id name { original } packages { id serviceId unit amount priceType }",
                {"floorId": {"eq": floor_id}},
            )
            match = next((item for item in found if item["name"]["original"] == space["room"]), None)
            if match:
                existing = {"id": match["id"], "packages": match.get("packages") or []}
                self.remember_original_amounts(existing["packages"])
                self.rooms[key] = existing
                self.step("room", f"{building} / {space['room']}", "reused", match["id"])
        if existing is None:
            area = space["area"] or 1
            payload = {
                "name": i18n(space["room"]),
                "area": area,
                "isZone": zone,
                "floorId": floor_id,
                "locationId": self.locations[building],
                "packages": [{
                    "priceType": price_type,
                    "unit": "months",
                    "amount": amount,
                    "serviceId": self.services[service],
                }],
            }
            if zone:
                payload["minRent"] = space["rent"]
                payload["lotCode"] = f"Z{did('lot', space['room'])[:4].upper()}"
            else:
                payload["capacity"] = max(1, round(area / 10))
                payload["tier"] = "Standard"
            created = self.client.mutate(
                self.urls["property"],
                "createRoom",
                "CreateRoomCommandInput",
                "id packages { id serviceId unit amount priceType }",
                payload,
            )
            existing = {"id": created["id"], "packages": created.get("packages") or []}
            self.remember_original_amounts(existing["packages"])
            self.rooms[key] = existing
            self.step("room", f"{building} / {space['room']}", "created", created["id"])
        self.attach_package(existing, service, price_type, amount, space["room"])

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
        self.remember_original_amounts(room["packages"])
        self.step("package", f"{room_name} / {service}", "created")

    def remember_original_amounts(self, packages: list):
        """The first catalog price stays originalAmount. Later bookings only override amount."""
        for package in packages:
            if package.get("originalAmount") is None:
                package["originalAmount"] = package.get("amount")

    def ensure_extra_services(self, contracts: list[dict]):
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
        if not any(
            isinstance(space.get("fee"), float) and space["fee"] > 0
            for contract in contracts
            for space in contract.get("spaces") or []
        ):
            self.step("extraServiceType", "Phí dịch vụ", "skipped", detail="File không có đơn giá phí dịch vụ")
            return
        seen = set()
        for contract in contracts:
            building = contract["building"] or "Tòa không tên"
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

    def customer_key(self, contract: dict) -> str:
        name = contract["name"] or "Khách không tên"
        if contract["tax"]:
            return f"tax:{contract['tax']}"
        kind = "company" if contract.get("as_company") or looks_like_company(name) else "name"
        return f"{kind}:{name}"

    def ensure_customer(self, contract: dict):
        name = contract["name"] or "Khách không tên"
        tax = contract["tax"]
        company = bool(tax) or contract.get("as_company") or looks_like_company(name)
        key = self.customer_key(contract)
        if key in self.customers:
            return
        emails = list(contract["emails"]) or [f"{name.lower().replace(' ', '.')}@onboarding.invalid"]
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
        building = contract["building"] or "Tòa không tên"
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

    def booking_note(self, contract: dict) -> str:
        building = contract["building"] or "Tòa không tên"
        service = contract["service"] or CATEGORY_NAME
        start = contract["start"].isoformat() if contract["start"] else ""
        number = contract["contract_no"] or f"row-{contract['stt']}"
        return f"import:{building}|{service}|{start}|{number}"

    def ensure_booking(self, as_of: date, contract: dict):
        name = contract["name"] or "Khách không tên"
        ready = []
        building = contract["building"] or "Tòa không tên"
        service = contract["service"] or CATEGORY_NAME
        for space in contract["spaces"]:
            room = self.rooms.get((building, space["floor"], space["room"]))
            if room:
                ready.append((space, room))
        if not ready or contract["start"] is None or contract["end"] is None:
            self.step("booking", name, "skipped", detail="Thiếu phòng có đơn giá hoặc thiếu ngày")
            return
        note = self.booking_note(contract)
        found = self.client.items(
            self.urls["reservation"],
            "bookings",
            "BookingQueryInput",
            "id note",
            {"note": {"eq": note}},
        )
        match = next((item for item in found if item.get("note") == note), None)
        if match:
            self.step("booking", name, "reused", match["id"])
            booking_id = match["id"]
            self.apply_service_fee(booking_id, building, ready, name)
        else:
            booking_id = self.create_booking(contract, ready, note, name)
        self.ensure_contract_record(contract, booking_id, name)
        self.invoice_elapsed_periods(as_of, contract, booking_id, name)
        if contract["end"] < as_of:
            self.complete_booking(booking_id, name)

    def create_booking(self, contract: dict, ready: list, note: str, name: str) -> str:
        building = contract["building"] or "Tòa không tên"
        service = contract["service"] or CATEGORY_NAME
        service_id = self.services[service]
        rooms = []
        monthly = 0.0
        for space, room in ready:
            package = next(
                (
                    item for item in room["packages"]
                    if item.get("serviceId") == service_id and item.get("unit") == "months"
                ),
                None,
            )
            if package is None:
                raise SmartOSError(f"Phòng {space['room']} chưa có package tháng cho {service}")
            zone = is_zone(service, space)
            _price_type, amount = package_amount(space, zone)
            rooms.append({"id": room["id"], "packages": [{"id": package["id"], "amount": amount}]})
            monthly += space["rent"]
        deposit, _warnings = deposit_amount(contract, monthly)
        mode, duration = parse_cycle(contract["cycle"])
        start = contract["start"]
        end = contract["end"]
        if end <= start:
            end = start + timedelta(days=1)
        if mode == "full":
            duration = span_months(start, end)
        else:
            duration = duration or 1
        first_end = min(add_months(start, duration) - timedelta(days=1), end)
        currency_warnings: list[str] = []
        currency = currency_code(contract["currency"], currency_warnings)
        customer_id = self.customers[self.customer_key(contract)]
        payload = {
            "locationId": self.locations[building],
            "rooms": rooms,
            "customerIds": [customer_id],
            "primaryCustomerId": customer_id,
            "startTime": iso(start),
            "endTime": iso(end),
            "selectTime": False,
            "monthlyCalculationType": "Cycle",
            "daysOfMonthCalculationType": "DaysOfCalendar",
            "numberOfPeople": 1,
            "applyTax": False,
            "currency": currency.upper(),
            "markAsConfirmed": True,
            "note": note,
            "paymentSettings": {
                "unit": "months",
                "duration": duration or 1,
                "firstPeriodPaymentStartDate": iso(start),
                "firstPeriodPaymentEndDate": iso(first_end),
            },
        }
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

    def apply_service_fee(self, booking_id: str, building: str, ready: list, name: str):
        """Write the contract fee onto booking.roomServices. The catalog price stays 0."""
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

    def ensure_contract_record(self, contract: dict, booking_id: str, name: str):
        template_name = contract.get("template")
        if not template_name:
            return
        number = contract.get("contract_no")
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
        start = contract["start"]
        end = contract["end"]
        if end <= start:
            end = start + timedelta(days=1)
        customer_id = self.customers[self.customer_key(contract)]
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

    def invoice_elapsed_periods(self, as_of: date, contract: dict, booking_id: str, name: str):
        start = contract["start"]
        end = contract["end"]
        if start is None or end is None:
            return
        mode, duration = parse_cycle(contract["cycle"])
        step = span_months(start, end) if mode == "full" else (duration or 1)
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
        monthly = sum((space.get("rent") or 0) for space in contract.get("spaces") or [])
        building = contract["building"] or "Tòa không tên"
        customer_id = self.customers[self.customer_key(contract)]
        method = self.transfer_method_id(self.locations[building])
        for index, period in enumerate(due, start=1):
            start = period["start"]
            end = period["end"]
            stored_period = by_start.get(start)
            amount = float(stored_period["totalAmount"]) if stored_period and stored_period.get("totalAmount") else monthly * period["months"]
            period_id = stored_period["id"] if stored_period else f"{booking_id}:period:{index}"
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
            payload = {
                "bookingId": booking_id,
                "invoices": [{"id": invoice["id"], "amount": amount}],
                "paymentMethodType": "bank",
                "amount": amount,
                "transactionDate": iso(start),
                "locationId": self.locations[building],
                "customerId": customer_id,
                "note": contract["contract_no"] or "",
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
        return payload.get("note") or field
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


def prepare_contracts(rows: list[dict]) -> tuple[list[dict], list[dict], str]:
    contracts = group_contracts(rows)
    suggestions, review_error = review_uncertain(contracts)
    return contracts, suggestions, review_error


def preview_workbook(path) -> dict:
    as_of, rows = load_rows(path)
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
    contracts, suggestions, review_error = prepare_contracts(rows)
    result = Importer(client, urls, options).run(as_of, contracts)
    return {
        "asOf": as_of.isoformat(),
        "calls": client.calls,
        "skipped": [item for item in result["steps"] if item["status"] == "skipped"],
        "suggestions": suggestions,
        "reviewError": review_error,
        "error": result.get("error") or "",
    }
