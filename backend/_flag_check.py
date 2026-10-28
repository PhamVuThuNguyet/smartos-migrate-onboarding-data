"""Local checks for contract flags and patch apply. Does not call Nova."""

from review import apply_corrections, contract_flags

def contract(**extra):
    base = {
        "name": "Nguyễn Thị Vân",
        "tax": None,
        "currency": "VNĐ",
        "cycle": "6 tháng",
        "deposit_label": "3 tháng",
        "deposit_amount": None,
        "spaces": [{
            "row": 5,
            "room": "T501",
            "room_dummy": False,
            "fee": 1000.0,
            "fee_raw": "1000",
            "area": 10,
            "rent": 1000,
            "price_type": "per-room",
            "unit_price": 1000,
        }],
    }
    base.update(extra)
    return base

checks = []

def expect(name, condition):
    checks.append((name, bool(condition)))

settled = contract()
expect("personal name settled", contract_flags(settled) == [])

brand = contract(name="VPBank")
expect("brand flagged", contract_flags(brand) == ["customerType"])

half_year = contract(cycle="mỗi nửa năm")
expect("half year flagged", "cycle" in contract_flags(half_year))

quarters = contract(deposit_label="2 quý")
expect("two quarters flagged", "deposit" in contract_flags(quarters))

dollar = contract(currency="Dollar")
expect("dollar flagged", "currency" in contract_flags(dollar))

vague_fee = contract(spaces=[{
    "row": 8, "room": "Sảnh", "room_dummy": False, "fee": None, "fee_raw": "theo thực tế",
}])
expect("fee text flagged", contract_flags(vague_fee) == ["fee"])

range_label = contract(spaces=[{
    "row": 9, "room": "Dãy T5", "room_dummy": False, "fee": 1.0, "fee_raw": "1",
}])
expect("day label flagged", contract_flags(range_label) == ["rooms"])

known_range = contract(spaces=[{
    "row": 10, "room": "T501", "room_dummy": False, "fee": 1.0, "fee_raw": "1",
}])
expect("split room settled", "rooms" not in contract_flags(known_range))

target = contract(name="VPBank", cycle="mỗi nửa năm", deposit_label="2 quý", currency="Dollar")
apply_corrections([target], [
    {"index": 0, "kind": "customerType", "confidence": "high", "value": "company"},
    {"index": 0, "kind": "cycle", "confidence": "high", "value": {"months": 6}},
    {"index": 0, "kind": "deposit", "confidence": "low", "value": {"months": 6}},
    {"index": 0, "kind": "currency", "confidence": "high", "value": "USD"},
    {"index": 0, "kind": "unknown", "confidence": "high", "value": {"x": 1}},
])
expect("company applied", target["as_company"] is True)
expect("cycle applied", target["cycle"] == "6 tháng")
expect("low deposit kept", target["deposit_label"] == "2 quý")
expect("currency applied", target["currency"] == "USD")

room = contract(spaces=[{
    "row": 11, "room": "Dãy T5", "room_dummy": False,
    "fee": 1000.0, "fee_raw": "1000", "area": 40, "rent": 400, "price_type": "per-room", "unit_price": 400,
}])
apply_corrections([room], [{
    "index": 0, "kind": "rooms", "confidence": "high",
    "value": ["T5A", "T5B"],
}])
expect("rooms split", [space["room"] for space in room["spaces"]] == ["T5A", "T5B"])
expect("rent split", room["spaces"][0]["rent"] == 200)

fee = vague_fee
apply_corrections([fee], [{"index": 0, "kind": "fee", "confidence": "high", "value": 1500000}])
expect("fee applied", fee["spaces"][0]["fee"] == 1500000)

failed = [name for name, ok in checks if not ok]
print("PASS" if not failed else "FAIL " + ", ".join(failed))
