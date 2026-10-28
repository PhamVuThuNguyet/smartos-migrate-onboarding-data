import datetime
import json
from pathlib import Path

from openpyxl import Workbook

from import_api import preview_workbook

wb = Workbook()
ws = wb.active
def write_row(sheet, row_index, values):
    for column, value in values.items():
        sheet.cell(row_index, column, value)

write_row(ws, 1, {
    2: "STT", 3: "Khach hang", 4: "Toa nha", 5: "Dich vu cung cap", 6: "Tang",
    7: "Khu vuc/Phong", 8: "Email", 9: "Dien tich", 10: "Don gia thue", 12: "Tien te",
    13: "MST", 14: "Ngay bat dau", 15: "Ngay ket thuc",     18: "Tien coc", 20: "Ky thanh toan",
    26: "So hop dong", 30: "Mẫu hợp đồng",
})
write_row(ws, 2, {
    2: 1, 3: "CONG TY TNHH ABC", 4: "Athena", 5: "Cho thue van phong", 6: "5", 7: "T501",
    8: "a@b.com", 9: 100, 10: 10000000, 12: "VND", 13: "0312345678",
    14: datetime.date(2026, 1, 1), 15: datetime.date(2026, 3, 31),
    18: "3 tháng", 20: "Hàng tháng", 26: "HD-001", 30: "Mẫu văn phòng",
})
write_row(ws, 3, {
    2: 2, 3: "CONG TY TNHH ABC", 4: "Athena", 5: "Cho thue van phong", 6: "5", 7: "T501",
    8: "a@b.com", 9: 100, 10: 12000000, 12: "VND", 13: "0312345678",
    14: datetime.date(2026, 8, 1), 15: datetime.date(2027, 2, 1),
    18: "3 tháng", 20: "Hàng tháng", 26: "HD-002", 30: "Mẫu văn phòng",
})
path = Path("_rules.xlsx")
wb.save(path)
out = preview_workbook(path)
path.unlink()
calls = out["calls"]
packages = []
bookings = []
invoices = []
transactions = []
contracts = []
for call in calls:
    kind = call["mutation"]
    payload = call["input"]
    if kind == "createRoom":
        packages.append(payload["packages"][0]["amount"])
    if kind == "mutatePackages":
        packages.append([item["amount"] for item in payload["packages"]])
    if kind == "createBooking":
        room_packages = payload["rooms"][0]["packages"][0]
        bookings.append({
            "amount": room_packages.get("amount"),
            "firstEnd": payload["paymentSettings"]["firstPeriodPaymentEndDate"],
            "method": (payload.get("deposit") or {}).get("paymentMethodId"),
        })
    if kind == "createInvoices":
        invoices.append(payload["issueDate"][:10])
    if kind == "createInvoicePaymentTransaction":
        transactions.append({
            "date": payload["transactionDate"][:10],
            "type": payload["paymentMethodType"],
            "method": payload.get("paymentMethodId"),
        })
    if kind == "createContract":
        contracts.append({
            "code": payload["code"],
            "template": payload["contractTemplateId"],
            "name": payload["name"],
        })
summary = {
    "mutations": [call["mutation"] for call in calls],
    "catalogPackages": packages,
    "bookings": bookings,
    "invoiceIssueDates": invoices,
    "transactions": transactions,
    "contracts": contracts,
    "completes": sum(1 for call in calls if call["mutation"] == "completeBooking"),
}
Path("_rules.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print("ok", len(invoices), "invoices", summary["completes"], "completes")
