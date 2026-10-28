"use client";

import { useEffect, useMemo, useState } from "react";

type ImportStep = {
  action: string;
  name: string;
  status: string;
  id: string;
  detail: string;
};

type ApiCall = {
  mutation: string;
  service: string;
  inputType: string;
  title: string;
  input: Record<string, unknown>;
  apiFills: string[];
  previewNote?: string;
};

type ImportIssue = {
  code: string;
  row: number | null;
  column: string | null;
  message: string;
};

type PreviewResponse = {
  asOf: string;
  errors: ImportIssue[];
  calls: ApiCall[];
  skipped: ImportStep[];
  error: string;
};

export default function HomePage() {
  const [file, setFile] = useState<File | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [preview, setPreview] = useState<PreviewResponse | null>(null);
  const [selectedMutation, setSelectedMutation] = useState("");
  const [query, setQuery] = useState("");
  const [payloadCall, setPayloadCall] = useState<ApiCall | null>(null);

  const groups = useMemo(() => {
    const map = new Map<string, ApiCall[]>();
    for (const call of preview?.calls ?? []) {
      const list = map.get(call.mutation) ?? [];
      list.push(call);
      map.set(call.mutation, list);
    }
    return [...map.entries()];
  }, [preview]);

  const activeMutation = groups.some(([name]) => name === selectedMutation)
    ? selectedMutation
    : (groups[0]?.[0] ?? "");
  const activeCalls = groups.find(([name]) => name === activeMutation)?.[1] ?? [];
  const visibleCalls = activeCalls.filter((call) => {
    const needle = query.trim().toLowerCase();
    if (!needle) return true;
    return (
      call.title.toLowerCase().includes(needle) ||
      JSON.stringify(call.input).toLowerCase().includes(needle)
    );
  });

  useEffect(() => {
    if (!payloadCall) return;
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") setPayloadCall(null);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [payloadCall]);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!file) return;
    setLoading(true);
    setError("");
    try {
      const body = new FormData();
      body.append("file", file);
      const response = await fetch("/api/preview", { method: "POST", body });
      const raw = await response.text();
      let payload: { detail?: string } = {};
      try {
        payload = raw ? JSON.parse(raw) : {};
      } catch {
        throw new Error(
          response.status >= 500
            ? "Không kết nối được API Python. Chạy uvicorn ở thư mục backend, cổng 8000."
            : "API trả về dữ liệu không hợp lệ",
        );
      }
      if (!response.ok) {
        throw new Error(payload.detail || "Không đọc được file");
      }
      const data = payload as PreviewResponse;
      setPreview(data);
      setSelectedMutation(data.calls[0]?.mutation ?? "");
      setQuery("");
      if (data.error) setError(data.error);
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : "Không đọc được file";
      setError(
        message === "Failed to fetch"
          ? "Không kết nối được API Python. Chạy uvicorn ở thư mục backend, cổng 8000."
          : message,
      );
    } finally {
      setLoading(false);
    }
  }

  function refTitle(calls: ApiCall[], value: unknown) {
  const ref = String(value ?? "");
  const match = /^@([^:]+):(\d+)$/.exec(ref);
  if (!match) return ref;
  const index = Number(match[2]) - 1;
  const call = calls[index];
  if (!call || call.mutation !== match[1]) return ref;
  return call.title;
}

function displayDate(value: unknown) {
  const text = String(value ?? "");
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(text);
  if (!match) return text;
  return `${match[3]}/${match[2]}/${match[1]}`;
}

function displayMoney(value: unknown) {
  const number = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(number)) return "";
  return new Intl.NumberFormat("vi-VN").format(number);
}

function originalName(input: Record<string, unknown>) {
  const name = input.name;
  if (name && typeof name === "object" && "original" in name) {
    return String((name as { original?: string }).original ?? "");
  }
  return "";
}

function salesCells(call: ApiCall, calls: ApiCall[]): string[] {
  const input = call.input;
  const place = (value: unknown) => refTitle(calls, value);
  if (call.mutation === "createServiceCategory") return [originalName(input)];
  if (call.mutation === "createLocation") {
    const address = input.address as { text?: string } | undefined;
    return [String(input.name ?? ""), address?.text ?? ""];
  }
  if (call.mutation === "createService") {
    const locations = Array.isArray(input.locationIds) ? input.locationIds.map(place).join(", ") : "";
    return [originalName(input), locations];
  }
  if (call.mutation === "createFloor") {
    const kinds = Array.isArray(input.rentalTypes)
      ? input.rentalTypes.map((kind) => (kind === "area" ? "Khu vực" : "Phòng")).join(", ")
      : "";
    return [originalName(input), place(input.locationId), kinds];
  }
  if (call.mutation === "createRoom") {
    const packages = Array.isArray(input.packages) ? input.packages[0] as Record<string, unknown> | undefined : undefined;
    const priceType = packages?.priceType === "per-square-meter" ? "Theo m2" : "Theo phòng";
    return [
      originalName(input),
      place(input.locationId),
      place(input.floorId),
      String(input.area ?? ""),
      input.isZone ? "Có" : "Không",
      displayMoney(packages?.amount),
      priceType,
    ];
  }
  if (call.mutation === "createExtraServiceType") {
    const unit = input.unit as { original?: string } | undefined;
    return [originalName(input), place(input.locationId), displayMoney(input.price), unit?.original ?? ""];
  }
  if (call.mutation === "createCompany") {
    const company = input.companyName as { vi?: string } | undefined;
    return [company?.vi ?? "", String(input.email ?? ""), String(input.taxCode ?? ""), String(input.legalRepName ?? ""), place(input.locationId)];
  }
  if (call.mutation === "createIndividual") {
    return [String(input.contactName ?? ""), String(input.email ?? ""), place(input.locationId)];
  }
  if (call.mutation === "mutatePackages") {
    const packages = Array.isArray(input.packages) ? input.packages as Record<string, unknown>[] : [];
    const prices = packages.map((item) => displayMoney(item.amount)).join(", ");
    return [place(input.roomId), prices];
  }
  if (call.mutation === "createBooking") {
    const settings = input.paymentSettings as { duration?: number } | undefined;
    const deposit = input.deposit as { requiredAmount?: number } | undefined;
    const roomCount = Array.isArray(input.rooms) ? input.rooms.length : 0;
    return [
      place(input.primaryCustomerId),
      place(input.locationId),
      String(input.note ?? ""),
      displayDate(input.startTime),
      displayDate(input.endTime),
      settings?.duration ? `${settings.duration} tháng` : "",
      deposit?.requiredAmount ? displayMoney(deposit.requiredAmount) : "0",
      String(roomCount),
      String(input.currency ?? ""),
    ];
  }
  if (call.mutation === "updateBookingRoomServices") {
    const services = Array.isArray(input.roomServices) ? input.roomServices as Record<string, unknown>[] : [];
    const fee = services[0];
    const unit = fee?.unit as { original?: string } | undefined;
    return [place(input.id), displayMoney(fee?.price), unit?.original ?? ""];
  }
  if (call.mutation === "createInvoices") {
    const invoices = Array.isArray(input.invoices) ? input.invoices as Record<string, unknown>[] : [];
    const items = Array.isArray(invoices[0]?.invoiceItems) ? invoices[0].invoiceItems as unknown[] : [];
    return [place(invoices[0]?.bookingId), displayDate(input.issueDate), displayDate(input.dueDate), String(items.length)];
  }
  if (call.mutation === "createInvoicePaymentTransaction") {
    return [place(input.customerId), displayMoney(input.amount), displayDate(input.transactionDate), "Transfer"];
  }
  if (call.mutation === "createContract") {
    return [
      String(input.code ?? ""),
      String(input.name ?? ""),
      place(input.bookingId),
      displayDate(input.effectiveDate),
      displayDate(input.expiryDate),
    ];
  }
  if (call.mutation === "completeBooking") return [place(input.id)];
  return [];
}

const DATABASE_TABLES: Record<string, string> = {
  createServiceCategory: "property.servicecategories",
  createLocation: "property.locations",
  createService: "property.services",
  createFloor: "property.floors",
  createRoom: "property.rooms, property.packages",
  mutatePackages: "property.packages",
  createExtraServiceType: "reservation.extraservicetypes",
  createCompany: "customer.customers",
  createIndividual: "customer.customers",
  createBooking: "reservation.bookings, reservation.paymentperiods",
  updateBookingRoomServices: "reservation.bookings",
  createInvoices: "payment.invoices, payment.invoice_items",
  createInvoicePaymentTransaction: "payment.transactions, payment.invoice_allocations",
  completeBooking: "reservation.bookings",
  createContract: "contract.contracts",
  room: "property.rooms",
  booking: "reservation.bookings",
  extraServiceType: "reservation.extraservicetypes",
};

const SALES_COLUMNS: Record<string, string[]> = {
  createServiceCategory: ["Tên"],
  createLocation: ["Tòa", "Địa chỉ"],
  createService: ["Dịch vụ", "Tòa"],
  createFloor: ["Tầng", "Tòa", "Loại"],
  createRoom: ["Phòng", "Tòa", "Tầng", "Diện tích", "Zone", "Đơn giá", "Cách tính"],
  createExtraServiceType: ["Tên", "Tòa", "Đơn giá", "Đơn vị"],
  createCompany: ["Khách", "Email", "MST", "Người đại diện", "Tòa"],
  createIndividual: ["Khách", "Email", "Tòa"],
  mutatePackages: ["Phòng", "Đơn giá"],
  createBooking: ["Khách", "Tòa", "Ghi chú", "Bắt đầu", "Kết thúc", "Kỳ", "Cọc", "Số phòng", "Tiền tệ"],
  updateBookingRoomServices: ["Booking", "Phí dịch vụ/tháng", "Đơn vị"],
  createInvoices: ["Booking", "Ngày phát hành", "Hạn thanh toán", "Số dòng"],
  createInvoicePaymentTransaction: ["Khách", "Số tiền", "Ngày", "Phương thức"],
  completeBooking: ["Booking"],
  createContract: ["Số hợp đồng", "Mẫu hợp đồng", "Booking", "Bắt đầu", "Kết thúc"],
};

  return (
    <main className="app">
      <header className="header">
        <h1>SMARTOS Onboarding Data Migration</h1>
      </header>

      <form className="panel upload" onSubmit={onSubmit}>
        <input
          type="file"
          accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
          onChange={(event) => setFile(event.target.files?.[0] ?? null)}
        />
        <button type="submit" disabled={!file || loading}>
          {loading ? "Đang đọc..." : "Preview Migrated Data"}
        </button>
      </form>
      {error ? <p className="error">{error}</p> : null}

      {preview ? (
        <>
          <section className="panel summary">
            <span>
              Mốc ngày <strong>{preview.asOf}</strong>
            </span>
            <span>
              <strong>{preview.calls.length}</strong> lệnh API
            </span>
            <span>
              <strong>{preview.skipped.length}</strong> dòng bỏ qua
            </span>
            <span>
              <strong>{preview.errors.length}</strong> lỗi
            </span>
          </section>
          {preview.errors.length > 0 ? (
            <section className="panel warnings">
              <h2>
                File chưa nhập được: {preview.errors.length} lỗi. Sửa trong file Excel rồi tải lại.
              </h2>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Mã lỗi</th>
                      <th>Dòng</th>
                      <th>Cột</th>
                      <th>Thông báo</th>
                    </tr>
                  </thead>
                  <tbody>
                    {preview.errors.map((item, index) => (
                      <tr key={`${item.code}-${item.row}-${index}`}>
                        <td>{item.code}</td>
                        <td>{item.row ?? ""}</td>
                        <td>{item.column ?? ""}</td>
                        <td>{item.message}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          ) : (
            <p className="note">
              Đây là input gửi vào mutation. id, uniqueKey, searchText và mã BK, INV, TX, COM do API
              sinh.
            </p>
          )}
          {preview.skipped.length > 0 ? (
            <section className="panel warnings">
              <h2>{preview.skipped.length} records đã bỏ qua</h2>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Loại</th>
                      <th>Bảng</th>
                      <th>Tên</th>
                      <th>Lý do</th>
                    </tr>
                  </thead>
                  <tbody>
                    {preview.skipped.map((item, index) => (
                      <tr key={`${item.action}-${item.name}-${index}`}>
                        <td>{item.action}</td>
                        <td>{DATABASE_TABLES[item.action] ?? ""}</td>
                        <td>{item.name}</td>
                        <td>{item.detail}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          ) : null}

          {preview.calls.length > 0 ? (
            <section className="panel workspace">
              <ul className="table-list">
                {groups.map(([name, calls]) => (
                  <li key={name}>
                    <button
                      type="button"
                      className={name === activeMutation ? "active" : ""}
                      onClick={() => {
                        setSelectedMutation(name);
                        setQuery("");
                      }}
                    >
                      {name}
                      <span className="count">
                        {DATABASE_TABLES[name] ?? calls[0]?.service} · {calls.length} lệnh
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
              <div className="grid-pane">
                <div className="grid-toolbar">
                  <div>
                    <h2>{activeMutation}</h2>
                    <p>
                      {DATABASE_TABLES[activeMutation] ?? ""}
                      {visibleCalls.length ? ` · ${visibleCalls.length} lệnh` : ""}
                      {activeCalls[0]?.apiFills.length
                        ? ` · API tự sinh: ${activeCalls[0].apiFills.join(", ")}`
                        : ""}
                      {activeCalls[0]?.previewNote ? ` · ${activeCalls[0].previewNote}` : ""}
                      . Bấm một dòng để xem input.
                    </p>
                  </div>
                  <div className="toolbar-actions">
                    <input
                      value={query}
                      placeholder="Lọc trong nhóm này"
                      onChange={(event) => setQuery(event.target.value)}
                    />
                  </div>
                </div>
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        {(SALES_COLUMNS[activeMutation] ?? []).map((column) => (
                          <th key={column}>{column}</th>
                        ))}
                        <th>API payload</th>
                      </tr>
                    </thead>
                    <tbody>
                      {visibleCalls.map((call, index) => (
                        <tr key={`${call.mutation}-${index}`} onClick={() => setPayloadCall(call)}>
                          {salesCells(call, preview?.calls ?? []).map((value, cellIndex) => (
                            <td key={cellIndex}>{value}</td>
                          ))}
                          <td>{JSON.stringify(call.input)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </section>
          ) : null}
        </>
      ) : null}
      {payloadCall ? (
        <div className="popup-backdrop" onClick={() => setPayloadCall(null)}>
          <div
            className="popup"
            role="dialog"
            aria-modal="true"
            aria-label={payloadCall.mutation}
            onClick={(event) => event.stopPropagation()}
          >
            <div className="popup-header">
              <strong>
                {payloadCall.mutation} · {payloadCall.title}
              </strong>
              <button type="button" className="download" onClick={() => setPayloadCall(null)}>
                Đóng
              </button>
            </div>
            <pre>{JSON.stringify(payloadCall.input, null, 2)}</pre>
          </div>
        </div>
      ) : null}
    </main>
  );
}
