"""Second layer: Nova 2 Lite corrects contract fields the parser did not settle.

A contract is sent only when at least one field is flagged. Nova returns a
patch for those keys. A key is applied only when confidence is high and the
value matches the schema. This module does not write the database.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from onboarding import (
    deposit_amount,
    expand_room_names,
    looks_like_company,
    parse_cycle,
    parse_number,
    text,
)

MODEL_ID = "global.amazon.nova-2-lite-v1:0"
SCHEMA_KEYS = ("customerType", "deposit", "cycle", "rooms", "currency", "fee")
# A room label that still looks like a list or a range after the parser kept it whole.
ROOM_HINT = re.compile(r"(\+|->|→|–|—|-|,|;|&|/|đến|tới|toi|~|\bvà\b|\band\b|dãy|day\b)", re.I)


def load_env() -> None:
    candidates = [
        Path(__file__).resolve().parent / ".env",
        Path(__file__).resolve().parents[2] / "smartos-ai-core" / ".env",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
        if os.environ.get("AWS_ACCESS_KEY_ID"):
            return


def plain(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, float) and value != value:
        return None
    return value


def contract_row(contract: dict) -> dict:
    """Columns the parser already read, so Nova sees the whole contract."""
    return {
        "stt": plain(contract.get("stt")),
        "customer": contract.get("name"),
        "tax": contract.get("tax"),
        "emails": contract.get("emails") or [],
        "building": contract.get("building"),
        "service": contract.get("service"),
        "start": plain(contract.get("start")),
        "end": plain(contract.get("end")),
        "currency": contract.get("currency"),
        "cycle": text(contract.get("cycle")),
        "depositLabel": text(contract.get("deposit_label")),
        "depositAmount": plain(contract.get("deposit_amount")),
        "contractNo": contract.get("contract_no"),
        "note": contract.get("note"),
        "spaces": [space_row(space) for space in contract.get("spaces") or []],
    }


def space_row(space: dict) -> dict:
    return {
        "excelRow": space.get("row"),
        "floor": space.get("floor"),
        "room": space.get("room"),
        "area": space.get("area"),
        "areaRaw": space.get("area_raw"),
        "rent": space.get("rent"),
        "fee": space.get("fee"),
        "feeRaw": space.get("fee_raw"),
        "priceType": space.get("price_type"),
    }


def _customer_uncertain(contract: dict) -> bool:
    name = contract.get("name") or ""
    if not name or contract.get("tax") or looks_like_company(name):
        return False
    words = name.split()
    # A short personal name is already settled. One token, digits, or a long label is not.
    if 2 <= len(words) <= 4 and all(re.fullmatch(r"[^\W\d_]+", word, re.UNICODE) for word in words):
        return False
    return True


def _deposit_uncertain(contract: dict) -> bool:
    if isinstance(parse_number(contract.get("deposit_amount")), float):
        return False
    label = text(contract.get("deposit_label"))
    if not label or parse_number(label) == "ACC":
        return False
    raw = label.lower()
    if "không" in raw and not re.search(r"\d", raw):
        return False
    if re.fullmatch(r"\d+\s*tháng", raw):
        return False
    number = parse_number(label)
    if isinstance(number, float) and number > 36:
        return False
    return True


def _cycle_uncertain(contract: dict) -> bool:
    raw = (text(contract.get("cycle")) or "").lower()
    if not raw:
        return False
    settled = (
        r"(hàng\s+)?tháng",
        r"\d+\s*tháng",
        r"(hàng\s+)?quý",
        r"\d+\s*quý",
        r"(hàng\s+)?năm",
        r"\d+\s*năm",
        r".*toàn bộ.*",
    )
    return not any(re.fullmatch(pattern, raw) for pattern in settled)


def _currency_uncertain(contract: dict) -> bool:
    raw = text(contract.get("currency"))
    if not raw:
        return False
    upper = raw.upper()
    return "USD" not in upper and "VN" not in upper


def _room_uncertain(space: dict) -> bool:
    room = space.get("room") or ""
    if not room or space.get("room_dummy"):
        return False
    if expand_room_names(room) != [room]:
        return False
    return bool(ROOM_HINT.search(room) or len(re.findall(r"\d+", room)) >= 2)


def _fee_uncertain(space: dict) -> bool:
    raw = space.get("fee_raw")
    if not raw or "không thu" in raw.lower():
        return False
    return not isinstance(space.get("fee"), float)


def contract_flags(contract: dict) -> list[str]:
    """Field keys the parser did not settle for this contract."""
    flags = []
    if _customer_uncertain(contract):
        flags.append("customerType")
    if _deposit_uncertain(contract):
        flags.append("deposit")
    if _cycle_uncertain(contract):
        flags.append("cycle")
    if _currency_uncertain(contract):
        flags.append("currency")
    if any(_room_uncertain(space) for space in contract.get("spaces") or []):
        flags.append("rooms")
    if any(_fee_uncertain(space) for space in contract.get("spaces") or []):
        flags.append("fee")
    return flags


def _deposit_parser(contract: dict) -> dict:
    label = text(contract.get("deposit_label")) or ""
    amount = parse_number(contract.get("deposit_amount"))
    if isinstance(amount, float):
        return {"amount": amount}
    if label and "không" in label.lower() and not re.search(r"\d", label):
        return {"none": True}
    if re.fullmatch(r"\d+\s*tháng", label.lower()):
        return {"months": int(re.search(r"\d+", label).group())}
    number = parse_number(label)
    if isinstance(number, float) and number > 36:
        return {"amount": number}
    if isinstance(number, float):
        return {"months": int(number)}
    match = re.search(r"\d+", label)
    if match:
        return {"months": int(match.group())}
    parsed, _warnings = deposit_amount(contract, 1)
    return {"amount": parsed}


def _cycle_parser(contract: dict) -> dict:
    mode, months = parse_cycle(contract.get("cycle"))
    if mode == "full":
        return {"full": True}
    return {"months": months or 1}


def _currency_parser(contract: dict) -> str:
    raw = (text(contract.get("currency")) or "").upper()
    if "USD" in raw:
        return "USD"
    return "VND"


def parser_result(contract: dict) -> dict:
    name = contract.get("name") or ""
    customer_type = "company" if contract.get("tax") or contract.get("as_company") or looks_like_company(name) else "individual"
    fees = [space.get("fee") for space in contract.get("spaces") or []]
    fee = sum(item for item in fees if isinstance(item, float)) if any(isinstance(item, float) for item in fees) else None
    return {
        "customerType": customer_type,
        "deposit": _deposit_parser(contract),
        "cycle": _cycle_parser(contract),
        "rooms": [space.get("room") for space in contract.get("spaces") or []],
        "currency": _currency_parser(contract),
        "fee": fee,
    }


def flagged_contracts(contracts: list[dict]) -> list[dict]:
    reviews = []
    for index, contract in enumerate(contracts):
        flags = contract_flags(contract)
        if not flags:
            continue
        reviews.append({
            "id": f"contract:{index}",
            "index": index,
            "flags": flags,
            "row": contract_row(contract),
            "parserResult": parser_result(contract),
        })
    return reviews


def review_uncertain(contracts: list[dict]) -> tuple[list[dict], str]:
    cases = flagged_contracts(contracts)
    if not cases:
        return [], ""
    load_env()
    if not os.environ.get("AWS_ACCESS_KEY_ID") or not os.environ.get("AWS_SECRET_ACCESS_KEY"):
        return [], "Chưa có AWS_ACCESS_KEY_ID và AWS_SECRET_ACCESS_KEY trong backend/.env"
    try:
        suggestions = []
        for start in range(0, len(cases), 20):
            suggestions.extend(_ask(cases[start:start + 20]))
    except Exception as error:
        return [], f"Nova không trả lời: {error}"
    apply_corrections(contracts, suggestions)
    return suggestions, ""


def apply_corrections(contracts: list[dict], suggestions: list[dict]) -> None:
    appliers = {
        "customerType": _apply_customer,
        "deposit": _apply_deposit,
        "cycle": _apply_cycle,
        "rooms": _apply_rooms,
        "currency": _apply_currency,
        "fee": _apply_fee,
    }
    for item in suggestions:
        applier = appliers.get(item.get("kind"))
        contract = contracts[item["index"]] if 0 <= item.get("index", -1) < len(contracts) else None
        if item.get("confidence") != "high" or item.get("value") is None or applier is None or contract is None:
            item["applied"] = False
            continue
        item["applied"] = applier(contract, item["value"])


def _apply_customer(contract: dict, value) -> bool:
    kind = value if isinstance(value, str) else None
    if isinstance(value, dict):
        kind = value.get("customerType")
    if kind not in {"company", "individual"} or contract.get("tax"):
        return False
    contract["as_company"] = kind == "company"
    return True


def _apply_deposit(contract: dict, value) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("none") is True:
        contract["deposit_label"] = "không"
        contract["deposit_amount"] = None
        return True
    if isinstance(value.get("months"), int) and not isinstance(value.get("months"), bool) and value["months"] > 0:
        contract["deposit_label"] = f"{value['months']} tháng"
        contract["deposit_amount"] = None
        return True
    if isinstance(value.get("amount"), (int, float)) and not isinstance(value.get("amount"), bool) and value["amount"] >= 0:
        contract["deposit_amount"] = float(value["amount"])
        return True
    return False


def _apply_cycle(contract: dict, value) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("full") is True:
        contract["cycle"] = "Đã thanh toán toàn bộ"
        return True
    if isinstance(value.get("months"), int) and not isinstance(value.get("months"), bool) and value["months"] > 0:
        contract["cycle"] = f"{value['months']} tháng"
        return True
    return False


def _apply_rooms(contract: dict, value) -> bool:
    replacements = _room_replacements(contract, value)
    if not replacements:
        return False
    rewritten = []
    changed = False
    for space in contract.get("spaces") or []:
        names = replacements.get(space.get("room"))
        if not names or not _room_uncertain(space):
            rewritten.append(space)
            continue
        rewritten.extend(_split_space(space, names))
        changed = True
    if changed:
        contract["spaces"] = rewritten
    return changed


def _room_replacements(contract: dict, value) -> dict[str, list[str]]:
    uncertain = []
    for space in contract.get("spaces") or []:
        if _room_uncertain(space) and space.get("room") not in uncertain:
            uncertain.append(space.get("room"))
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        if len(uncertain) != 1:
            return {}
        names = _clean_rooms(value)
        return {uncertain[0]: names} if names else {}
    found = {}
    if not isinstance(value, list):
        return {}
    for item in value:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        names = _clean_rooms(item.get("names"))
        if label in uncertain and names:
            found[label] = names
    return found


def _clean_rooms(names) -> list[str]:
    if not isinstance(names, list):
        return []
    cleaned = [str(name).strip() for name in names if str(name).strip()]
    if not cleaned or len(cleaned) > 40:
        return []
    return cleaned


def _apply_currency(contract: dict, value) -> bool:
    raw = value if isinstance(value, str) else ""
    if isinstance(value, dict):
        raw = str(value.get("currency") or "")
    upper = raw.upper()
    if "USD" in upper or upper in {"DOLLAR", "DOLLARS"}:
        contract["currency"] = "USD"
        return True
    if "VN" in upper or upper in {"DONG", "ĐỒNG"}:
        contract["currency"] = "VND"
        return True
    return False


def _apply_fee(contract: dict, value) -> bool:
    spaces = [space for space in contract.get("spaces") or [] if _fee_uncertain(space)]
    if not spaces:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        rows = {space.get("row") for space in spaces}
        if len(rows) != 1:
            return False
        share = float(value) / len(spaces)
        for space in spaces:
            space["fee"] = share
        return True
    if not isinstance(value, list):
        return False
    hit = False
    for item in value:
        if not isinstance(item, dict):
            continue
        amount = item.get("amount")
        if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount < 0:
            continue
        group = [space for space in spaces if space.get("row") == item.get("excelRow")]
        if not group:
            continue
        share = float(amount) / len(group)
        for space in group:
            space["fee"] = share
        hit = True
    return hit


def _split_space(space: dict, names: list[str]) -> list[dict]:
    if len(names) == 1:
        clone = dict(space)
        clone["room"] = names[0]
        return [clone]
    count = len(names)
    clones = []
    for name in names:
        clone = dict(space)
        clone["room"] = name
        if space.get("area"):
            clone["area"] = space["area"] / count
        if space.get("rent") is not None:
            clone["rent"] = space["rent"] / count
        if isinstance(space.get("fee"), float):
            clone["fee"] = space["fee"] / count
        if space.get("price_type") == "per-room" and space.get("unit_price") is not None:
            clone["unit_price"] = space["unit_price"] / count
        clones.append(clone)
    return clones


def _field_text(case: dict, key: str) -> str:
    row = case["row"]
    if key == "customerType":
        return row.get("customer") or ""
    if key == "deposit":
        return row.get("depositLabel") or ""
    if key == "cycle":
        return row.get("cycle") or ""
    if key == "currency":
        return row.get("currency") or ""
    if key == "rooms":
        labels = [space.get("room") or "" for space in row.get("spaces") or []]
        return ", ".join(label for label in labels if label)
    raws = [space.get("feeRaw") or "" for space in row.get("spaces") or [] if space.get("feeRaw")]
    return ", ".join(raws)


def _ask(cases: list[dict]) -> list[dict]:
    import boto3
    from botocore.config import Config

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "ap-southeast-1"
    model_id = os.environ.get("BEDROCK_MODEL_ID") or MODEL_ID
    client = boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=Config(connect_timeout=5, read_timeout=60, retries={"max_attempts": 2, "mode": "standard"}),
    )
    prompt = json.dumps({"cases": [{k: v for k, v in case.items() if k != "index"} for case in cases]}, ensure_ascii=False)
    response = client.converse(
        modelId=model_id,
        system=[{
            "text": (
                "Bạn sửa field parser chưa chốt trong một hợp đồng onboarding SmartOS. "
                "Mỗi case có flags là các key được phép sửa, row là cả dòng parser đã đọc, "
                "parserResult là kết quả parser đang giữ. Chỉ trả key nằm trong flags. "
                "Bỏ key nào row không đủ căn cứ để sửa. Không bịa mã phòng hay số tiền. "
                "confidence high chỉ khi row cho đủ căn cứ. "
                "customerType value là \"company\" hoặc \"individual\". "
                "deposit value là {\"amount\":number} hoặc {\"months\":number} hoặc {\"none\":true}. "
                "cycle value là {\"months\":number} hoặc {\"full\":true}. "
                "rooms value là [\"tên phòng\"] khi chỉ một nhãn chưa tách, "
                "hoặc [{\"label\":\"nhãn gốc\",\"names\":[\"tên phòng\"]}] khi có nhiều nhãn. "
                "currency value là \"VND\" hoặc \"USD\". "
                "fee value là số tiền tháng của một excelRow, hoặc [{\"excelRow\":number,\"amount\":number}]. "
                "Trả về một JSON object, không markdown. "
                '{"suggestions":[{"id":"contract:0","fields":[{"key":"cycle","confidence":"high","reason":"...","value":{"months":6}}]}]}'
            )
        }],
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 4000, "temperature": 0},
    )
    blocks = response.get("output", {}).get("message", {}).get("content", [])
    raw = "\n".join(block.get("text", "") for block in blocks if block.get("text")).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Nova không trả JSON")
    payload = json.loads(raw[start:end + 1])
    by_id = {case["id"]: case for case in cases}
    suggestions = []
    for item in payload.get("suggestions") or []:
        case = by_id.get(item.get("id"))
        if not case:
            continue
        for field in item.get("fields") or []:
            key = field.get("key")
            if key not in case["flags"] or key not in SCHEMA_KEYS or "value" not in field:
                continue
            suggestions.append({
                "id": f"{case['id']}:{key}",
                "index": case["index"],
                "kind": key,
                "text": _field_text(case, key),
                "parser": json.dumps(case["parserResult"].get(key), ensure_ascii=False),
                "value": field["value"],
                "confidence": field.get("confidence") if field.get("confidence") in {"high", "low"} else "low",
                "reason": str(field.get("reason") or ""),
                "applied": False,
            })
    return suggestions
