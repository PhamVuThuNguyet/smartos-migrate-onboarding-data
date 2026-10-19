# Migrate onboarding Excel vào SmartOS

Tool đọc sổ Excel cùng format cột với file sales đang dùng, dựng payload của các mutation PMS, rồi cho sales và dev xem trước. Bấm tạo thật thì gọi lần lượt đúng API mà frontend đang gọi. Mã `BK` / `INV` / `TX` / `COM`, `uniqueKey` và `searchText` do API sinh.

File cùng format cột, dữ liệu khác, vẫn chạy. Tool tìm sheet có hàng tiêu đề STT, khách, tòa, khu vực/phòng, đơn giá. Mốc ngày là ngày chạy file.

## Thứ tự gọi API

1. `createServiceCategory` → `property.servicecategories`
2. `createLocation` → `property.locations`
3. `createService` → `property.services`
4. `createFloor` → `property.floors`
5. `createRoom` → `property.rooms` và `property.packages`
6. `createExtraServiceType` → `reservation.extraservicetypes` (chỉ khi file có phí dịch vụ)
7. `createCompany` hoặc `createIndividual` → `customer.customers`
8. `createBooking` → `reservation.bookings` và `reservation.paymentperiods`
9. Payment period có `endDate` trước ngày chạy file: mỗi period một `createInvoices` và một `createInvoicePaymentTransaction`. `completeBooking` khi `endTime` của booking cũng trước ngày đó
10. Có cột “Mẫu hợp đồng”: `createContract` với số hợp đồng và `contractTemplateId`

## Các bước đọc file và ghi record

### 1. Service — `property.services`

Command: `CreateService`.

Cột `Dịch vụ cung cấp`.

Khóa upsert: tên dịch vụ trong business. `categoryName` không có trong Excel, điền dummy `Cho thuê mặt bằng`.

### 2. Location — `property.locations`

Command: `CreateLocation`.

Cột `Tòa nhà`.

Khóa upsert: tên tòa. `serviceIds` được cộng dồn mỗi khi một dịch vụ xuất hiện tại tòa đó. Địa chỉ, tọa độ, giờ làm việc, timezone và người đại diện không có trong file, xem bảng dummy.

`serviceIds` được phép rỗng lúc tạo location, nhưng service gắn vào tòa phải tồn tại trước.

### 3. Floor — `property.floors`

Command: `CreateFloor`.

Cột `Tầng`. Khóa upsert: `tòa + tên tầng`.

### 4. Room và Package — `property.rooms`, `property.packages`

Command: `CreateRoom`. Package được tạo trong cùng command, không có API package riêng trong luồng này.

Cột `Khu vực/phòng`, `Diện tích (m2)`, `Đơn giá cho thuê/tháng`.

Dải phòng được tách thành từng phòng. `T501 -> T506` thành T501 đến T506. `1A+B` thành 1A và 1B. `T221-224 + Hlang` thành T221 đến T224 và khu vực Hành lang. Cùng quy tắc với `T209-T217`, `T201-208`, `301->303`, và danh sách `T109, T110, T123A`. Diện tích, tiền thuê và phí dịch vụ của cả dải được chia đều cho từng phòng và khu vực phụ. Tên mô tả như sảnh hoặc kiot giữ một khu vực.

Khóa phòng: `tòa + tầng + tên khu vực`. Khóa package: `roomId + dịch vụ + unit months`.

- Công thức `=số*I{dòng}` → `priceType = per-square-meter`, `amount` = đơn giá/m2.
- Ô là số thành phẩm → `priceType = per-room`, `amount` = tổng tiền tháng.

`CreateRoom` bắt `amount > 0`. Khu vực chỉ có phí dịch vụ, không có đơn giá thuê (một số dòng Big C: hố pít, bể tách dầu, phòng máy phát) bị bỏ và ghi cảnh báo. Giá `ACC` cũng bị bỏ.

Nếu cùng một phòng gặp giá thuê khác ở hợp đồng sau, package giữ số tiền đầu tiên. Số đó là `originalAmount` khi gắn vào booking. Booking gửi `amount` bằng giá thuê của dòng đó.

Zone (`isZone = true`) khi ô diện tích không phải số. Phòng thường có `tier = standard`.

### 5. Phí dịch vụ — `reservation.extraservicetypes`

Command: `CreateExtraServiceType`.

Cột `Đơn giá phí dịch vụ mặc định/tháng`. Một catalog cho mỗi tòa, tên `Phí dịch vụ`, giá catalog `0`. Số tiền của từng hợp đồng được gửi sau `createBooking` bằng `updateBookingRoomServices`: `price` là tổng fee của các room trong hợp đồng, `type` là `EXTRA_SERVICE`, `quantity` là 1. Ô `Không thu phí dịch vụ` hoặc trống thì phí = 0, không tạo type và không gọi update.

### 6. Khách hàng — `customer.customers`

Command: `CreateCompany` khi có MST hoặc tên là company. Tên cá nhân thì `CreateIndividual`.

Cột `Tên khách hàng`, `Email`, `Thông tin DN thuê (MST)`.

Khóa upsert: `tax:{mst}` sau khi xóa khoảng trắng, hoặc tên company / tên khách khi không có MST. Email trong một ô, cách nhau bởi `;` `/` `,` hoặc xuống dòng: email đầu là email chính, các email sau là member.

### 7. Booking và kỳ thanh toán — `reservation.bookings`, `reservation.paymentperiods`

Command: `CreateBooking`, bên trong gọi `initPaymentPeriods`.

Một hợp đồng = một booking. Khóa: `tòa + dịch vụ + ngày bắt đầu + số hợp đồng`. Cùng số hợp đồng nhưng khác dịch vụ hoặc khác ngày (Vietcombank văn phòng và ATM đều `47-195/DVT-HĐDV`) là hai booking.

`markAsConfirmed = true` → status `confirmed`. `numberOfPeople = 1`, `applyTax = false`. Số hợp đồng nằm trong `note` của booking và trong `code` của contract.

`paymentSettings.duration` lấy từ cột `Kỳ thanh toán`. `CreateBooking` tự sinh payment period. Phí dịch vụ không nằm trong package thuê: sau booking, `updateBookingRoomServices` gửi tổng fee của hợp đồng.

Cọc lớn hơn 0 thì `createBooking` gửi `deposit.requiredAmount`, `paidAmount` và `transactionDate` là ngày bắt đầu. `paymentMethodId` mặc định là payment method Transfer (`bank`) của location. Form có id thì dùng id đó.

### 8. Invoice theo payment period

Kỳ thanh toán được tính từ ngày bắt đầu hợp đồng. Hàng tháng thì mỗi kỳ dài một tháng, `3 tháng` thì mỗi kỳ dài ba tháng, `6 tháng` là sáu tháng, hàng năm là mười hai tháng. Một kỳ chỉ thành invoice khi ngày kết thúc của kỳ đó trước ngày chạy file. `issueDate` và `transactionDate` là ngày bắt đầu của kỳ. `paymentMethodType` là `bank` (Transfer).

`completeBooking` chỉ chạy khi `endTime` của booking sớm hơn ngày chạy file.

### 9. Contract

Cột `Mẫu hợp đồng` là tên trong `contract.contracttemplates`. Tool lấy `contractTemplateId` theo tên đó, rồi gọi `createContract` với `code` là số hợp đồng và template id vừa tìm.

## Gộp dòng Excel thành hợp đồng

Dòng có STT mở một hợp đồng mới. Dòng không STT là dòng nối của hợp đồng phía trên.

Dòng nối trở thành hợp đồng mới khi một trong ba cột khác hợp đồng hiện tại: `Số hợp đồng`, `Dịch vụ cung cấp`, `Ngày bắt đầu`. Thiếu thì kế thừa tên khách, tòa, email, MST. Techcombank dòng quảng cáo (số hợp đồng và ngày khác phần thương mại) được tách booking riêng. Các dòng Big C, Vietcombank tầng 2, Techcombank T242 giữ cùng một booking, nhiều phòng.

Ba booking nhiều phòng trong file này: Big C, Vietcombank Thanh Xuân, Techcombank thương mại.

## Cột không ghi vào database

| Cột | Lý do |
| --- | --- |
| STT | Chỉ để nhận dòng mở hợp đồng |
| Thời hạn HĐ còn | Công thức `ngày kết thúc − P2` |
| Trạng thái cập nhật lên hệ thống | Checklist sales (`Hoàn thành`), không phải `booking.status` |
| Giá/tháng, Theo giá, Đơn giá $/m2 | Trống hoặc trùng đơn giá đã đọc từ cột đơn giá thuê |
| Đơn giá sau gia hạn | Trống trong file này, chưa có đợt tăng giá để ghi `paymentSettingsHistory` |

`Trạng thái`, `Trạng thái hợp đồng`, tỷ giá và ghi chú được đọc từ Excel nhưng không gửi trong payload booking. Status booking lấy từ `endTime` so với ngày chạy file.

## Giá trị điền khi Excel không có

Payload preview dùng các giá trị sau khi cột tương ứng trống.

| Field bắt buộc | Khi nào thiếu | Giá trị điền |
| --- | --- | --- |
| `Service.categoryName` | Luôn thiếu | `Cho thuê mặt bằng` |
| `Location.address` | Luôn thiếu | ATHENA → Tòa ATHENA, TP.HCM. ARTEMIS → Thanh Xuân, Hà Nội (suy từ địa chỉ khách trong sheet). 738 Võ Văn Kiệt → chính địa chỉ đó, TP.HCM. Tòa lạ → `{tên}, Việt Nam` |
| `Location.geoLocation` | Luôn thiếu | Điểm gần khu vực ở trên. Không phải tọa độ khảo sát |
| `Location.timezone` | Luôn thiếu | `Asia/Ho_Chi_Minh` |
| `Location.workingDay` | Luôn thiếu | T2–T6, 08:00–18:00 |
| `Location.representativeId` | Luôn thiếu trong Excel | Id `BusinessUser` nhập ở form tạo thật. Preview dùng placeholder |
| `Room.area` | Ô không phải số (sảnh, ngoài trời) | `1`, và phòng thành zone |
| `Room.name` / `Floor.name` | Ô trống | `Khu vực dòng {n}` / `Không rõ tầng` |
| `Room.capacity` | Phòng thường, file không có sức chứa | `max(1, round(diện tích / 10))` |
| `Room.lotCode` | Zone, API bắt buộc | `Z` + 4 ký tự từ hash tên khu vực |
| `Room.minRent` | Zone | Bằng đơn giá thuê tháng của dòng |
| `Room.tier` | Phòng thường | `standard` |
| `Customer.legalRepName` | Company, `CreateCompany` bắt buộc | Nguyên cột tên khách hàng |
| `Customer.legalRepRole` | Company | `Người đại diện` |
| `Customer.email` | Trống hoặc không có `@` | `{slug tên}@onboarding.invalid` |
| `Customer.type` | Không có MST và tên không phải company | `individual` |
| `Booking.numberOfPeople` | Luôn thiếu, API `min 1` | `1` |
| `Booking.applyTax` | File ghi giá chưa VAT, không có thuế suất | `false` |
| `Booking.currency` | Ô loại giá trống | `vnd` |
| `Booking.requiredDepositAmount` | Không đọc được cột cọc | `0` |
| `paymentSettings.duration` | Thiếu kỳ thanh toán | `1` tháng |
| `ExtraServiceType.price` | Catalog dùng chung cả location | `0`. Số tiền hợp đồng đi qua `updateBookingRoomServices` |
| `Booking.deposit` | Cọc lớn hơn 0 | `requiredAmount`, `paidAmount`, ngày bắt đầu. Payment method mặc định là Transfer |
| `Transaction.paymentMethodType` | Mọi transaction thu | `bank` (Transfer) |
| `Invoice.issueDate` | Payment period đã kết thúc | `startDate` của payment period |
| `Invoice.dueDate` | Payment period đã kết thúc | `endDate` của payment period |

## Rules đọc các cột có dữ liệu nhưng không sạch

| Cột | Rule |
| --- | --- |
| MST | Xóa mọi khoảng trắng. `0302 229 420` thành `0302229420`. Mã chi nhánh `-002` giữ nguyên, coi là khách khác pháp nhân mẹ |
| Tiền | `226,867,498` và `5,601.25` đều đọc được. Chuỗi `ACC` không phải số |
| Cọc | Có cột Tiền cọc thì dùng số đó. `3 tháng` / `03 tháng` / `02 tháng` = số tháng × tổng tiền thuê tháng của các khu vực. Câu chứa `không` → 0. Số > 36 ở cột số tháng cọc được hiểu là số tiền (hai dòng quảng cáo ghi 35.000.000 và 20.000.000 nhầm cột) |
| Kỳ thanh toán | `Hàng tháng` / `1 tháng` → 1. `Hàng quý` / `3 tháng` → 3. `6 tháng` → 6. `Hàng năm` / `12 tháng` → 12. `2 tháng` → 2. `Đã TT toàn bộ tiền thuê theo HĐ` → một kỳ từ đầu đến hết hạn |
| Loại giá | `VNĐ` gửi `VND`, `USD` gửi `USD`. Đây là `Booking.currency`, không phải `Package.priceType` |
| Tỷ giá | Cột được đọc nhưng không gửi trong payload booking |
| Diện tích zone | Chữ như `Sảnh`, `Ngoài trời` không đưa vào `area` |

## Việc bản này cố ý chưa làm

- Không prorate kỳ lẻ theo lịch. `PaymentPeriodService` của reservation mới là cách tính tiền khi import thật.
- Không điền đợt tăng giá (`Đơn giá sau gia hạn` đang trống).
- Không tạo user đăng nhập, giấy tờ, hay file PDF hợp đồng. Cột ghi chú không gửi trong payload booking.
- `representativeId` phải trỏ tới một `BusinessUser` có sẵn khi tạo thật.
- Invoice được tạo cho từng payment period có `endDate` trước ngày chạy file. `completeBooking` chỉ chạy khi `endTime` của booking cũng trước ngày đó.
