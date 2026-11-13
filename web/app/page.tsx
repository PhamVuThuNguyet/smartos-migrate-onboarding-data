"use client";

import { useEffect, useMemo, useRef, useState } from "react";

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
};

type ImportIssue = {
  code: string;
  row: number | null;
  column: string | null;
  message: string;
};

type ImportSummary = {
  created?: Record<string, number>;
  reused?: Record<string, number>;
  vacantRooms?: number;
};

type ErrorReport = {
  filename: string;
  content: string;
};

type PreviewResponse = {
  asOf: string;
  errors: ImportIssue[];
  calls: ApiCall[];
  skipped: ImportStep[];
  error: string;
  steps?: ImportStep[];
  imported?: boolean;
  summary?: ImportSummary;
  errorReport?: ErrorReport | null;
  accessToken?: string;
  refreshToken?: string;
};

type ImportJob = {
  id: string;
  status: string;
  phase?: string;
  message?: string;
  filename?: string;
  totalBookings?: number;
  doneBookings?: number;
  percent?: number;
  completedKeys?: string[];
  recentSteps?: ImportStep[];
  error?: string;
  result?: PreviewResponse | null;
};

type Me = {
  id?: string;
  accountId?: string;
  accountType?: string;
  fullName?: string;
  email?: string;
  phoneNumber?: string;
  phoneNumberDialIndex?: string;
  contactPhoneNumber?: string;
  contactPhoneNumberDialIndex?: string;
  avatar?: string;
  locationIds?: string[];
  businessId?: string;
  customerId?: string;
  namespace?: string;
  isActive?: boolean;
  fullPhoneNumber?: string;
  modules?: string[];
  policies?: string[];
};

type Session = {
  domain: string;
  accessToken: string;
  refreshToken: string;
  me: Me;
};

const SESSION_KEY = "smartos-migrate-session";
const API_ORIGIN = (process.env.NEXT_PUBLIC_MIGRATE_API_ORIGIN || "").replace(/\/$/, "");

function apiUrl(route: string) {
  return API_ORIGIN ? `${API_ORIGIN}/${route}` : `/api/${route}`;
}

function explainFetchFailure(caught: unknown, fallback: string) {
  const message = caught instanceof Error ? caught.message : fallback;
  if (message === "Failed to fetch" || message.includes("NetworkError")) {
    return API_ORIGIN
      ? `Không gọi được ${API_ORIGIN}. Uvicorn có thể đang reload, hoặc CORS/chặn kết nối. Thử lại khi API sẵn sàng.`
      : "Không kết nối được API Python. Chạy uvicorn ở thư mục backend, cổng 8000.";
  }
  return message;
}

async function readApiJson(response: Response) {
  const raw = await response.text();
  try {
    return raw ? JSON.parse(raw) : {};
  } catch {
    if (response.status === 502 || response.status === 504) {
      throw new Error(
        "Next.js hết thời gian chờ khi API Python vẫn đang chạy. Đặt NEXT_PUBLIC_MIGRATE_API_ORIGIN=http://127.0.0.1:8000 rồi restart next dev.",
      );
    }
    throw new Error(
      response.status >= 500
        ? `API trả HTTP ${response.status}, không phải JSON.`
        : "API trả về dữ liệu không hợp lệ",
    );
  }
}

function readSession(): Session | null {
  try {
    const raw = localStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Session;
    if (!parsed?.accessToken || !parsed?.domain) return null;
    return parsed;
  } catch {
    return null;
  }
}

function sessionLabel(session: Session) {
  return session.me.fullName || session.me.email || session.me.accountId || session.domain;
}

const FILE_ERROR_CODES = new Set([
  "E01",
  "E02",
  "E03",
  "E04",
  "E05",
  "E06",
  "E07",
  "E08",
  "E09",
  "E10",
  "E11",
  "E12",
  "E14",
]);

const RESULT_ORDER = [
  "room",
  "package",
  "booking",
  "customer",
  "invoice",
  "transaction",
  "contract",
  "completeBooking",
];

const RESULT_LABELS: Record<string, string> = {
  room: "Room",
  package: "Package",
  booking: "Booking",
  customer: "Khách",
  invoice: "Hóa đơn",
  transaction: "Giao dịch",
  contract: "Hợp đồng",
  completeBooking: "Booking đã kết thúc",
  location: "Tòa",
  floor: "Tầng",
  service: "Dịch vụ",
};

const DATABASE_TABLES: Record<string, string> = {
  createRoom: "property.rooms, property.packages",
  mutatePackages: "property.packages",
  createCompany: "customer.customers",
  createIndividual: "customer.customers",
  createBooking: "reservation.bookings, reservation.paymentperiods",
  createInvoices: "payment.invoices, payment.invoice_items",
  createInvoicePaymentTransaction: "payment.transactions, payment.invoice_allocations",
  completeBooking: "reservation.bookings",
  createContract: "contract.contracts",
  room: "property.rooms",
  customer: "customer.customers",
  booking: "reservation.bookings",
  location: "property.locations",
  floor: "property.floors",
  service: "property.services",
};

const SALES_COLUMNS: Record<string, string[]> = {
  createRoom: ["Phòng", "Tòa", "Tầng", "Room Type", "Diện tích", "Đơn giá", "Package Type"],
  createCompany: ["Khách", "Email", "MST", "Người đại diện", "Tòa"],
  createIndividual: ["Khách", "Email", "Tòa"],
  mutatePackages: ["Phòng", "Đơn giá"],
  createBooking: ["Khách", "Tòa", "Ghi chú", "Bắt đầu", "Kết thúc", "Kỳ", "Cọc", "Số phòng/zone", "Người", "Tiền tệ", "Tỷ giá"],
  createInvoices: ["Booking", "Ngày phát hành", "Hạn thanh toán", "Số dòng"],
  createInvoicePaymentTransaction: ["Khách", "Số tiền", "Ngày", "Phương thức"],
  completeBooking: ["Booking"],
  createContract: ["Số hợp đồng", "Mẫu hợp đồng", "Booking", "Bắt đầu", "Kết thúc"],
};

export default function HomePage() {
  const [file, setFile] = useState<File | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [preview, setPreview] = useState<PreviewResponse | null>(null);
  const [selectedMutation, setSelectedMutation] = useState("");
  const [query, setQuery] = useState("");
  const [payloadCall, setPayloadCall] = useState<ApiCall | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [loginOpen, setLoginOpen] = useState(false);
  const [loginLoading, setLoginLoading] = useState(false);
  const [loginError, setLoginError] = useState("");
  const [domain, setDomain] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [importing, setImporting] = useState(false);
  const [importJob, setImportJob] = useState<ImportJob | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const pendingImport = useRef(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

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
    const current = readSession();
    setSession(current);
    if (current?.me?.businessId) {
      console.log("me.businessId", current.me.businessId);
    }
  }, []);

  useEffect(() => {
    if (!payloadCall && !loginOpen && !confirmOpen) return;
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setPayloadCall(null);
        setLoginOpen(false);
        setConfirmOpen(false);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [payloadCall, loginOpen, confirmOpen]);

  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, []);

  function stopPolling() {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }

  function applyImportResult(data: PreviewResponse, current: Session) {
    setPreview(data);
    setSelectedMutation(data.calls[0]?.mutation ?? "");
    setQuery("");
    if (data.accessToken) {
      const next: Session = {
        domain: current.domain,
        accessToken: data.accessToken,
        refreshToken: data.refreshToken || current.refreshToken,
        me: current.me,
      };
      localStorage.setItem(SESSION_KEY, JSON.stringify(next));
      setSession(next);
    }
    if (data.error) setError(data.error);
  }

  async function pollJob(jobId: string, current: Session) {
    stopPolling();
    const tick = async () => {
      try {
        const response = await fetch(apiUrl(`jobs/${jobId}`));
        const job = (await readApiJson(response)) as ImportJob & { detail?: string };
        if (!response.ok) {
          throw new Error(job.detail || "Không đọc được tiến độ job");
        }
        setImportJob(job);
        if (job.status === "done") {
          stopPolling();
          setImporting(false);
          if (job.result) applyImportResult(job.result, current);
          return;
        }
        if (job.status === "error") {
          stopPolling();
          setImporting(false);
          setError(job.error || "Import lỗi");
          if (job.result) applyImportResult(job.result, current);
        }
      } catch (caught) {
        stopPolling();
        setImporting(false);
        setError(explainFetchFailure(caught, "Mất kết nối khi theo dõi job"));
      }
    };
    await tick();
    pollRef.current = setInterval(tick, 1500);
  }

  async function onResumeJob() {
    if (!importJob?.id || !session) return;
    setImporting(true);
    setError("");
    try {
      const response = await fetch(apiUrl(`jobs/${importJob.id}/resume`), { method: "POST" });
      const job = (await readApiJson(response)) as ImportJob & { detail?: string };
      if (!response.ok) {
        throw new Error(job.detail || "Không resume được job");
      }
      setImportJob(job);
      await pollJob(importJob.id, session);
    } catch (caught) {
      setImporting(false);
      setError(explainFetchFailure(caught, "Không resume được job"));
    }
  }

  async function onLogin(event: React.FormEvent) {
    event.preventDefault();
    setLoginLoading(true);
    setLoginError("");
    try {
      const response = await fetch(apiUrl("login"), {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ domain, username, password }),
      });
      const payload = (await readApiJson(response)) as { detail?: string } & Partial<Session>;
      if (!response.ok) {
        throw new Error(payload.detail || "Đăng nhập không thành công");
      }
      if (!payload.accessToken || !payload.domain) {
        throw new Error("Identity API không trả accessToken");
      }
      const next: Session = {
        domain: payload.domain,
        accessToken: payload.accessToken,
        refreshToken: payload.refreshToken || "",
        me: payload.me || {},
      };
      localStorage.setItem(SESSION_KEY, JSON.stringify(next));
      setSession(next);
      console.log("me.businessId", next.me.businessId);
      setLoginOpen(false);
      setPassword("");
      if (pendingImport.current) {
        pendingImport.current = false;
        setConfirmOpen(true);
      }
    } catch (caught) {
      setLoginError(explainFetchFailure(caught, "Đăng nhập không thành công"));
    } finally {
      setLoginLoading(false);
    }
  }

  async function onLogout() {
    const current = session;
    setSession(null);
    localStorage.removeItem(SESSION_KEY);
    if (!current) return;
    try {
      await fetch(apiUrl("logout"), {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ domain: current.domain, accessToken: current.accessToken }),
      });
    } catch {
    }
  }

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!file) return;
    setLoading(true);
    setError("");
    try {
      const body = new FormData();
      body.append("file", file);
      const response = await fetch(apiUrl("preview"), { method: "POST", body });
      const payload = (await readApiJson(response)) as { detail?: string };
      if (!response.ok) {
        throw new Error(payload.detail || "Không đọc được file");
      }
      const data = payload as PreviewResponse;
      setPreview(data);
      setSelectedMutation(data.calls[0]?.mutation ?? "");
      setQuery("");
      if (data.error) setError(data.error);
    } catch (caught) {
      setError(explainFetchFailure(caught, "Không đọc được file"));
    } finally {
      setLoading(false);
    }
  }

  const fileErrors = (preview?.errors ?? []).filter((item) => FILE_ERROR_CODES.has(item.code));
  const previewReady = Boolean(preview && fileErrors.length === 0);
  const createdCount = (preview?.steps ?? []).filter((item) => item.status === "created").length;
  const reusedCount = (preview?.steps ?? []).filter((item) => item.status === "reused").length;
  const created = preview?.summary?.created ?? {};
  const vacantRooms = preview?.summary?.vacantRooms ?? 0;

  function downloadErrorReport() {
    const report = preview?.errorReport;
    if (!report?.content) return;
    const binary = atob(report.content);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) {
      bytes[index] = binary.charCodeAt(index);
    }
    const blob = new Blob([bytes], {
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = report.filename || "bao-cao-loi.xlsx";
    link.click();
    URL.revokeObjectURL(url);
  }

  function onImportClick() {
    if (!file || !previewReady || loading || importing) return;
    if (!session) {
      pendingImport.current = true;
      setLoginOpen(true);
      return;
    }
    setConfirmOpen(true);
  }

  async function onConfirmImport() {
    if (!file || !session) return;
    setConfirmOpen(false);
    setImporting(true);
    setError("");
    setImportJob(null);
    try {
      const body = new FormData();
      body.append("file", file);
      body.append("domain", session.domain);
      body.append("accessToken", session.accessToken);
      body.append("refreshToken", session.refreshToken || "");
      const response = await fetch(apiUrl("import"), { method: "POST", body });
      const payload = (await readApiJson(response)) as {
        detail?: string;
        jobId?: string;
        async?: boolean;
      } & Partial<PreviewResponse>;
      if (!response.ok) {
        throw new Error(payload.detail || "Không ghi được dữ liệu");
      }
      if (payload.jobId) {
        setImportJob({
          id: payload.jobId,
          status: "queued",
          message: "Đã xếp hàng",
          percent: 0,
        });
        await pollJob(payload.jobId, session);
        return;
      }
      // Legacy sync response
      applyImportResult(payload as PreviewResponse, session);
      setImporting(false);
    } catch (caught) {
      setError(explainFetchFailure(caught, "Không ghi được dữ liệu"));
      setImporting(false);
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
  const parsed = Date.parse(text);
  if (!Number.isNaN(parsed)) {
    return new Intl.DateTimeFormat("vi-VN", {
      timeZone: "Asia/Ho_Chi_Minh",
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
    }).format(new Date(parsed));
  }
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(text);
  if (!match) return text;
  return `${match[3]}/${match[2]}/${match[1]}`;
}

function displayMoney(value: unknown) {
  const number = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(number)) return "";
  return new Intl.NumberFormat("vi-VN").format(number);
}

function displayRate(value: unknown) {
  const rows = Array.isArray(value) ? value as { rate?: unknown }[] : [];
  const rate = rows[0]?.rate;
  return rate == null || rate === "" ? "" : displayMoney(rate);
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
  if (call.mutation === "createRoom") {
    const packages = Array.isArray(input.packages) ? input.packages[0] as Record<string, unknown> | undefined : undefined;
    const rawType = String(packages?.priceType ?? "").toUpperCase().replace(/-/g, "_");
    const priceType =
      rawType === "PER_SQUARE_METER" ? "Price/m2"
        : rawType === "PER_PERSON" ? "Price/person"
          : "Price/room";
    const roomType = input.isZone === true ? "Zone" : "Room";
    return [
      originalName(input),
      place(input.locationId),
      place(input.floorId),
      roomType,
      String(input.area ?? ""),
      displayMoney(packages?.amount),
      priceType,
    ];
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
    const zoneCount = Array.isArray(input.zones) ? input.zones.length : 0;
    const roomCount = Array.isArray(input.rooms) ? input.rooms.length : 0;
    const resourceCount = zoneCount > 0 ? zoneCount : roomCount;
    return [
      place(input.primaryCustomerId),
      place(input.locationId),
      String(input.note ?? ""),
      displayDate(input.startTime),
      displayDate(input.endTime),
      settings?.duration ? `${settings.duration} tháng` : "",
      deposit?.requiredAmount ? displayMoney(deposit.requiredAmount) : "0",
      String(resourceCount),
      String(input.numberOfPeople ?? ""),
      String(input.currency ?? ""),
      displayRate(input.exchangeRates),
    ];
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

  return (
    <main className="app">
      <header className="header">
        <h1>SMARTOS Onboarding Data Migration</h1>
        {session ? (
          <div className="session">
            <div>
              <strong>{sessionLabel(session)}</strong>
              <span>{session.domain}</span>
            </div>
            <button type="button" className="download" onClick={onLogout}>
              Logout
            </button>
          </div>
        ) : (
          <button type="button" onClick={() => setLoginOpen(true)}>
            Login to PMS
          </button>
        )}
      </header>

      <form className="panel upload" onSubmit={onSubmit}>
        <input
          type="file"
          accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
          onChange={(event) => setFile(event.target.files?.[0] ?? null)}
        />
        <button type="submit" disabled={!file || loading || importing}>
          {loading ? "Reading..." : "Preview Migrated Data"}
        </button>
        <button
          type="button"
          disabled={!file || !previewReady || loading || importing}
          title={
            !previewReady
              ? "Preview successfully then upsert to database"
              : session
                ? "Upsert preview data to SmartOS"
                : "Login then upsert to database"
          }
          onClick={onImportClick}
        >
          {importing ? "Upserting..." : "Upsert"}
        </button>
      </form>
      {importJob && (importing || importJob.status === "error" || importJob.status === "running") ? (
        <section className="panel summary">
          <span>
            Job <strong>{importJob.id.slice(0, 8)}</strong> — {importJob.status}
          </span>
          <span>
            <strong>{importJob.percent ?? 0}%</strong>
            {importJob.totalBookings
              ? ` (${importJob.doneBookings ?? 0}/${importJob.totalBookings} booking)`
              : ""}
          </span>
          <span>{importJob.message || importJob.phase || ""}</span>
          {importJob.status === "error" ? (
            <button type="button" className="download" onClick={onResumeJob} disabled={importing}>
              Resume từ checkpoint
            </button>
          ) : null}
        </section>
      ) : null}
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
            {preview.imported || createdCount || reusedCount ? (
              <>
                <span>
                  <strong>{createdCount}</strong> tạo mới
                </span>
                <span>
                  <strong>{reusedCount}</strong> dùng lại
                </span>
              </>
            ) : null}
          </section>
          {preview.imported ? (
            <section className="panel results">
              <h2>Kết quả import</h2>
              <div className="result-grid">
                {RESULT_ORDER.map((key) => (
                  <span key={key}>
                    {RESULT_LABELS[key] ?? key} đã tạo{" "}
                    <strong>{created[key] ?? 0}</strong>
                  </span>
                ))}
                <span>
                  Phòng trống (không có Booking) <strong>{vacantRooms}</strong>
                </span>
              </div>
              <p className={preview.errors.length ? "note" : "note success"}>
                {preview.errors.length
                  ? `Đã ghi những bước thành công. ${preview.errors.length} lỗi PMS được ghi nhận, chỉnh sửa lại file và upload lại để tiếp tục upsert các record còn thiếu.`
                  : "Đã ghi dữ liệu vào SmartOS. Chạy lại file sẽ tiếp tục từ record còn thiếu."}
              </p>
            </section>
          ) : preview.errors.length === 0 ? (
            <p className="note">
              Đây là preview của input gửi vào mutation. Đăng nhập rồi bấm Upsert để ghi dữ liệu vào
              database.
            </p>
          ) : null}
          {preview.errors.length > 0 ? (
            <section className="panel warnings">
              <div className="panel-header">
                <h2>
                  {preview.imported
                    ? `Tổng số lỗi: ${preview.errors.length}`
                    : `File chưa nhập được: ${preview.errors.length} lỗi. Hãy chỉnh sửa lại các trường bị lỗi trong file Excel rồi upload lại file.`}
                </h2>
                {preview.errorReport ? (
                  <button type="button" className="download" onClick={downloadErrorReport}>
                    Tải file báo cáo lỗi
                  </button>
                ) : null}
              </div>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Mã lỗi</th>
                      <th>Dòng</th>
                      <th>Cột</th>
                      <th>Chi tiết lỗi</th>
                    </tr>
                  </thead>
                  <tbody>
                    {preview.errors.map((item, index) => (
                      <tr key={`${item.code}-${item.row}-${index}`}>
                        <td>{item.code}</td>
                        <td>{item.row ?? ""}</td>
                        <td>{item.column ?? ""}</td>
                        <td className="error-detail">{item.message}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          ) : null}
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
      {confirmOpen ? (
        <div className="popup-backdrop" onClick={() => setConfirmOpen(false)}>
          <div
            className="popup"
            role="dialog"
            aria-modal="true"
            aria-label="Ghi dữ liệu vào database"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="popup-header">
              <strong>Ghi dữ liệu vào database</strong>
              <button type="button" className="download" onClick={() => setConfirmOpen(false)}>
                Đóng
              </button>
            </div>
            <div className="login-form">
              <p>
                Ghi {preview?.calls.length ?? 0} lệnh API vào SmartOS của{" "}
                <strong>{session?.domain}</strong>. Location, tầng và dịch vụ phải đã có trên hệ
                thống.
              </p>
              <p className="popup-hint">
                Nếu một lệnh PMS lỗi, tool ghi nhận lỗi rồi chạy tiếp các bước còn lại. Chạy lại cùng
                file sẽ tiếp tục từ record còn thiếu và không tạo trùng invoice đã thanh toán.
              </p>
            </div>
            <div className="popup-actions">
              <button type="button" className="download" onClick={() => setConfirmOpen(false)}>
                Hủy
              </button>
              <button type="button" className="primary" onClick={onConfirmImport} disabled={importing}>
                {importing ? "Đang ghi..." : "Ghi vào database"}
              </button>
            </div>
          </div>
        </div>
      ) : null}
      {loginOpen ? (
        <div className="popup-backdrop" onClick={() => setLoginOpen(false)}>
          <div
            className="popup"
            role="dialog"
            aria-modal="true"
            aria-label="login to PMS"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="popup-header">
              <strong>Login to PMS</strong>
              <button type="button" className="download" onClick={() => setLoginOpen(false)}>
                Close
              </button>
            </div>
            <form className="login-form" onSubmit={onLogin}>
              <label>
                Business domain (namespace)
                <input
                  value={domain}
                  onChange={(event) => setDomain(event.target.value)}
                  placeholder="namespace"
                  autoComplete="url"
                  required
                />
              </label>
              <label>
                Username
                <input
                  value={username}
                  onChange={(event) => setUsername(event.target.value)}
                  placeholder="email"
                  autoComplete="username"
                  required
                />
              </label>
              <label>
                Password
                <input
                  type="password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  autoComplete="current-password"
                  required
                />
              </label>
              {loginError ? <p className="error">{loginError}</p> : null}
              <div className="popup-actions">
                <p className="popup-hint">Namespace is sent as header to identity-service.</p>
                <button type="submit" className="primary" disabled={loginLoading}>
                  {loginLoading ? "Logging in..." : "Login"}
                </button>
              </div>
            </form>
          </div>
        </div>
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
                Close
              </button>
            </div>
            <pre>{JSON.stringify(payloadCall.input, null, 2)}</pre>
          </div>
        </div>
      ) : null}
    </main>
  );
}
