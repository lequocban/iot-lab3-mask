# Kế hoạch và triển khai cảnh báo Supabase + Telegram

## 1. Kết quả đã chuẩn bị và phần bạn cần thiết lập

Mã nguồn đã có quy tắc xác nhận cảnh báo, hàng đợi nền, lưu ảnh có bounding box,
ghi log vào Supabase và gửi ảnh qua Telegram. Giao diện có checkbox bật/tắt
và bảng trạng thái riêng. Các kiểm thử tự động dùng dịch vụ giả lập; chưa có
khóa của bạn nên chưa thử ghi Supabase hoặc gửi Telegram thật.

Bạn cần tạo project Supabase, bot Telegram, chạy SQL và điền Environment trên
Render theo các bước bên dưới. Không cần đưa khóa bí mật vào mã nguồn hoặc chat.

Luồng xử lý:

```text
Laptop bật “Bật lưu và gửi cảnh báo”
  → gửi ảnh + mã phiên + lựa chọn notify tới /predict
  → Render suy luận, cập nhật lịch sử ba ảnh của phiên đó
  → nếu cùng loại vi phạm được xác nhận và hết cooldown:
       xếp một tác vụ nền, trả kết quả nhận dạng ngay
       tác vụ nền ghi log → tạo ảnh có khung → lưu Storage → gửi Telegram
       cập nhật kết quả gửi vào log
  → giao diện hiển thị trạng thái qua các ảnh tiếp theo hoặc nút “Cập nhật”
```

Phạm vi lần nâng cấp này là cảnh báo toàn khung hình. Chưa có vùng ROI hoặc
theo dõi danh tính. Ba nhãn và model ONNX hiện tại được giữ nguyên.

## 2. Quy tắc cảnh báo mặc định

Mục tiêu: nhận cảnh báo có bằng chứng và hạn chế gửi lặp.

- Chỉ hai nhãn `without_mask` và `mask_weared_incorrect` kích hoạt cảnh báo.
- Dùng ba ảnh kết quả gần nhất của cùng phiên camera, trong cửa sổ 8 giây.
- Một loại vi phạm phải xuất hiện ở ít nhất 2/3 ảnh; loại đó cũng phải còn
  xuất hiện trong ảnh hiện tại được chọn gửi, tránh gửi ảnh không còn vi phạm.
- Phải có đủ ba quan sát trước khi xác nhận lần đầu.
- Sau khi nhận một sự kiện, nghỉ mặc định 30 giây trước sự kiện tiếp theo.
- Cooldown áp dụng chung trong một tiến trình API, nên đổi tab hoặc mã phiên
  không lập tức vượt qua khoảng nghỉ. Có thể đổi bằng `ALERT_COOLDOWN_SECONDS`,
  giới hạn cấu hình 10–3.600 giây.
- Nếu vi phạm vẫn kéo dài, hệ thống có thể tiếp tục gửi sau mỗi khoảng nghỉ;
  không phải chỉ gửi đúng một lần cho cả phiên.
- Hàng đợi tối đa hai tác vụ chờ và một tác vụ đang xử lý. Khi đầy, không báo
  nhầm ảnh đã được nhận; giao diện trả trạng thái hàng đợi đầy.
- Không có khuôn mặt đủ confidence không được coi là xác nhận mọi người đeo đúng.

Lịch sử xét theo loại vi phạm trong khung hình, không theo một người cụ thể.
Quy tắc này giảm một số cảnh báo thoáng qua nhưng tăng độ trễ xác nhận và có
thể bỏ qua vi phạm ngắn. Nó không cải thiện recall của bản thân mô hình.

## 3. Tạo Supabase và cấu trúc lưu trữ

Mục tiêu: có database và nơi lưu ảnh riêng tư trên Cloud.

1. Mở [Supabase Dashboard](https://supabase.com/dashboard), đăng nhập và tạo
   project mới. Chọn Free và khu vực gần Render nếu có.
2. Đặt mật khẩu database của riêng bạn; code này dùng Data API nên không cần
   đưa mật khẩu database vào Render.
3. Chờ project sẵn sàng, mở SQL Editor > New query.
4. Mở file `supabase/alerts.sql` trong bộ triển khai, sao chép toàn bộ và Run.
5. Kiểm tra Table Editor có bảng `public.mask_alerts`.
6. Kiểm tra Storage có bucket `mask-alerts`, chế độ PRIVATE.
7. Tìm Project URL ở Connect hoặc Integrations > Data API. Dạng cần dùng:

```text
https://PROJECT_REF.supabase.co
```

8. Mở Settings > API Keys, tạo/copy một **Secret key**, dạng `sb_secret_...`.
   Điền nó vào Environment của Render ở bước 5, không đưa vào giao diện camera.

SQL bật RLS và không cấp quyền cho `anon`/`authenticated`; chỉ backend có key
đặc quyền sử dụng. Script cũng đặt bucket `mask-alerts` về private nếu đã tồn tại.
Code hỗ trợ legacy service_role JWT nhưng ưu tiên Secret key mới. Secret key
mới được gửi bằng header `apikey`, không dùng nó như một JWT Bearer.

Nguồn: [Data REST API](https://supabase.com/docs/guides/api),
[API keys](https://supabase.com/docs/guides/getting-started/api-keys),
[Storage access control](https://supabase.com/docs/guides/storage/security/access-control).

Kết quả mong đợi: có bảng trống và bucket private. Chưa cần tạo policy public
hoặc tắt RLS để “sửa lỗi quyền”. Lưu ảnh vào Storage; không lưu dữ liệu ảnh
base64 trực tiếp trong bảng Postgres.

## 4. Tạo bot Telegram và lấy chat ID

Mục tiêu: điện thoại của bạn nhận được cảnh báo từ bot do bạn sở hữu.

1. Trong Telegram, tìm đúng tài khoản **@BotFather**.
2. Gửi `/newbot`, đặt tên hiển thị và username kết thúc bằng `bot` theo hướng dẫn.
3. Giữ bot token BotFather trả về để điền vào Render. Token là bí mật.
4. Mở chat với bot mới, nhấn Start, rồi gửi một tin nhắn như `lab3`.
5. Tại thư mục lab3 trên máy, chạy công cụ chỉ đọc ID chat:

```powershell
cd "D:\STUDY\NAM_4_2026-2027\HK1\IoT\lab3"
.\.venv\Scripts\python.exe telegram_setup.py
```

6. Nhập token ở lời nhắc được ẩn; công cụ không in token. Lấy ID của chat
   bạn vừa nhắn, điền vào `TELEGRAM_CHAT_ID` ở bước 5.

Không cần thêm bot bên thứ ba để lấy ID. Công cụ không gửi thông báo thử, chỉ
đọc các update gần đây; nếu không có ID, gửi tin nhắn mới cho bot rồi chạy lại.
Nếu dùng group, phải thêm bot vào group và cho phép gửi ảnh; ID group thường
là số âm. Bản hướng dẫn ưu tiên chat riêng để demo đơn giản.

Nguồn: [Tạo bot bằng BotFather](https://core.telegram.org/bots/tutorial),
[getUpdates](https://core.telegram.org/bots/api#getupdates).

## 5. Cập nhật mã trên GitHub và Environment trên Render

Mục tiêu: Render có bộ code mới và đọc cấu hình từ môi trường máy chủ.

Bộ `deploy-render` đã được đồng bộ từ mã nguồn. Nếu sửa thêm file ở lab3,
chạy lại `prepare_render.py` trước khi commit trong repo deploy-render.

```powershell
cd "D:\STUDY\NAM_4_2026-2027\HK1\IoT\lab3\deploy-render"
git status
git add app.py alerts.py Dockerfile .dockerignore .gitignore render.yaml web/index.html deployment-manifest.json supabase/alerts.sql .env.example HUONG_DAN_CANH_BAO.md telegram_setup.py
git commit -m "Add Supabase alert logs and Telegram photo notifications"
git push
```

Chỉ commit những file ở lệnh trên. `.env.example` chỉ chứa tên biến và giá trị
mẫu rỗng. Không commit file chứa token thật.

Trong Render > service hiện tại > Environment, giữ các cấu hình model trước
đó và thêm/điền các biến sau:

```dotenv
ALERTS_ENABLED=1
API_KEY=<khóa tự đặt, ít nhất 16 ký tự; khuyến nghị chuỗi ngẫu nhiên dài>
SUPABASE_URL=https://PROJECT_REF.supabase.co
SUPABASE_SECRET_KEY=<secret key sb_secret_... từ Supabase>
SUPABASE_ALERT_BUCKET=mask-alerts
TELEGRAM_BOT_TOKEN=<token từ BotFather>
TELEGRAM_CHAT_ID=<ID chat riêng của bạn>
ALERT_COOLDOWN_SECONDS=30
ORT_THREADS=1
MODEL_PATH=assets/model.onnx
```

Các dấu `<...>` là chỉ dẫn thay thế, không phải giá trị dùng nguyên văn.
Nếu cần tạo API_KEY, tự chạy trên máy và giữ kết quả để nhập Render/giao diện:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Save và Deploy/Restart theo giao diện Render. `render.yaml` mặc định để
`ALERTS_ENABLED=0` nhằm triển khai code an toàn trước khi có tài khoản; với
Web Service tạo thủ công, cấu hình Environment trên dashboard là nơi bạn bật.
Nếu dùng Blueprint, chỉnh giá trị không bí mật trong Blueprint theo cấu hình
của mình; các khóa vẫn chỉ đặt bằng cơ chế secret của Render.

Kết quả mong đợi: service Live, `/health` vẫn ok. Nếu thiếu/sai định dạng cấu
hình cảnh báo, API nhận dạng vẫn khởi động được nhưng phần cảnh báo báo thiếu
cấu hình. Không cần cài Supabase SDK hoặc Telegram SDK; code sử dụng HTTPS
REST và các thư viện sẵn có, tránh tăng dependencies trên Render Free.

## 6. Bật chức năng trên giao diện và demo

Mục tiêu: xác nhận toàn bộ luồng thật sau khi đã cấu hình dịch vụ.

1. Mở URL gốc Render, Ctrl+F5.
2. Mở mục Kết nối API, nhập **API_KEY bạn tự đặt ở Render**. Không nhập key
   Supabase hoặc bot token vào trình duyệt. API_KEY này bảo vệ cả predict và
   endpoint xem trạng thái cảnh báo.
3. Nhấn Cập nhật trong khung Cảnh báo & thông báo. Phải thấy “Đã cấu hình”.
   Điều này chỉ xác nhận cấu hình có đủ/đúng định dạng; chưa chứng minh các
   credential được nhà cung cấp chấp nhận.
4. Tick “Bật lưu và gửi cảnh báo”, chọn số lượt phù hợp và bật camera.
5. Thử không đeo hoặc kéo khẩu trang xuống mũi đủ lâu để có ba quan sát.
6. Khi đủ quy tắc, giao diện hiện Đã xếp hàng. Các ảnh sau hoặc nút Cập nhật
   sẽ cho biết kết quả lưu log, lưu ảnh và gửi Telegram riêng biệt.
7. Mở Telegram trên điện thoại: có ảnh cảnh báo và caption tiếng Việt.
8. Mở Supabase Table Editor: có một row với cùng mã sự kiện. Mở Storage,
   đường dẫn ngày/UUID.jpg phải có ảnh tương ứng, có khung và nhãn.
9. Giữ nguyên vi phạm trong cooldown: không phát thêm ngay. Có thể tiếp tục
   cảnh báo khi hết 30 giây nếu vi phạm còn được xác nhận.
10. Bỏ tick để ngừng nhận cảnh báo mới. Tác vụ đã xếp hàng vẫn được xử lý.

Kết quả mong đợi: `log_status=saved`, `storage_status=saved` và
`telegram_status=sent` trên giao diện. Cột log_status là trạng thái runtime
trên API, không phải cột database. Database có trạng thái ảnh/Telegram và lỗi.

Ảnh gửi là chính ảnh của request kích hoạt, được backend vẽ bounding box và
confidence; không chụp một ảnh khác sau khi phát hiện. Nhãn trên ảnh dùng tên
ASCII của dataset để chạy được cả trên Docker tối giản, caption dùng tiếng Việt.
Ảnh được upload trực tiếp dạng multipart tới Telegram; không công khai bucket
hoặc tạo URL chứa secret để Telegram đọc ảnh.

Bot dùng `sendPhoto`: Telegram nhận file JPEG nhỏ và caption. Code không bật
paid broadcasts. Nguồn: [sendPhoto](https://core.telegram.org/bots/api#sendphoto),
[giới hạn bot](https://core.telegram.org/bots/faq#how-do-i-avoid-hitting-limits).

## 7. Dữ liệu được lưu và cách hiểu độ trễ mới

Mỗi row `mask_alerts` gồm ID UUID, thời gian UTC, ID phiên, loại vi phạm,
số lượng từng lớp, các detection/confidence/box, SHA256 model, inference_ms,
cloud_pipeline_ms, đường dẫn ảnh và kết quả Telegram.

- `pending`: tác vụ chưa hoàn tất.
- `saved`: đã được Supabase xác nhận ghi/lưu ở bước tương ứng.
- `sent`: Telegram Bot API đã trả thành công và message_id. Không có nghĩa
  người nhận đã đọc hoặc điện thoại chắc chắn đã hiện push notification.
- `failed`: bước đó bị từ chối hoặc lỗi xác định được.
- `unknown`: timeout/lỗi mơ hồ; có thể Telegram đã nhận ảnh nhưng server chưa
  nhận được xác nhận. Không tự gửi lại để tránh thông báo trùng.

Ba phép đo lab inference/RTT/E2E vẫn theo mốc cũ. Việc xác nhận/xếp hàng có
overhead nhỏ trong RTT; upload Supabase và gửi Telegram không được chờ trong
request predict. Các tác vụ nền vẫn chia sẻ CPU/mạng nên không hứa hiệu năng
hoàn toàn không thay đổi.

`telegram_ack_ms` được tính trên đồng hồ đơn điệu của server, từ lúc nhận sự
kiện vào hàng đợi đến khi Telegram trả thành công. Nó gồm chờ worker, tạo ảnh,
các bước Supabase trước đó và request Telegram. Không gồm thời gian chờ đủ
ba ảnh và không đo lúc điện thoại phát thông báo. Không lấy timestamp laptop
trừ timestamp Cloud để tính độ trễ.

Nút Cập nhật đọc trạng thái bộ nhớ của tiến trình, không phải toàn bộ lịch sử
Supabase. Sau restart, danh sách runtime có thể trống; log đã ghi ở Supabase
vẫn còn. File SQL cung cấp bảng để xem lịch sử bằng dashboard.

## 8. Xử lý lỗi và giới hạn của bản demo

- **HTTP 401 từ API:** nhập đúng API_KEY; sau khi đổi key, dừng và bật lại phiên.
- **Thiếu cấu hình:** xem tên biến trong bảng cảnh báo; điền Environment và redeploy.
- **supabase_http_401/403:** kiểm tra Project URL, backend Secret key, bảng và quyền.
- **supabase_http_404:** kiểm tra đã chạy SQL và bucket đúng tên; đừng tắt RLS.
- **supabase_http_400:** xem schema/kiểu dữ liệu trong SQL; code không trả nguyên
  response nhà cung cấp có thể chứa thông tin nhạy cảm.
- **telegram_http_400:** thường cần kiểm tra chat ID và đã Start bot hay chưa.
- **telegram_http_401:** bot token chưa đúng hoặc đã bị thu hồi.
- **telegram_http_403:** bot bị chặn hoặc thiếu quyền gửi vào chat/group.
- **telegram_http_429:** đang bị giới hạn; chờ, tăng cooldown, kiểm tra nơi khác
  có đang dùng cùng bot. Code không tự gửi lại ngay.
- **Telegram unknown:** kiểm tra điện thoại trước khi quyết định thử lại.
- **Ảnh lỗi nhưng Telegram gửi được:** hai đích độc lập; sửa Supabase, không
  giả định cả hai cùng thành công. Giao diện cho biết riêng từng trạng thái.

Queue và cooldown hiện nằm trong bộ nhớ, phù hợp demo một worker. Chúng không
bền qua restart và không phối hợp nhiều instance. Sự kiện đang chờ có thể mất
nếu tiến trình bị dừng trước khi ghi vào Supabase; row pending có thể chưa
được hoàn tất nếu dừng giữa chừng. Bản này không cam kết giao nhận exactly-once.
Nếu cần vận hành lâu dài, bước tiếp theo là outbox bền trong database, worker
riêng và cơ chế đối soát/retry có kiểm soát.

Supabase Free hiện có 500 MB database và 1 GB Storage; project có thể pause sau
một tuần không hoạt động. Cần mở kiểm tra trước buổi demo. Log nghiệp vụ trong
bảng này khác với thời hạn lưu log nội bộ của dịch vụ Supabase.
[Nguồn giá và giới hạn](https://supabase.com/pricing)

Chỉ lưu ảnh cảnh báo, không lưu mọi frame. Bạn quản lý và xóa dữ liệu demo
bằng Supabase dashboard; bản này chưa tự đặt lịch xóa ảnh/log. Khi xóa row,
ảnh Storage không tự biến mất và ngược lại.

## 9. File đã thêm/sửa và kiểm thử

- `alerts.py`: cấu hình, xác nhận, cooldown, hàng đợi và tích hợp HTTPS.
- `app.py`: nối predict với cảnh báo và endpoint trạng thái có bảo vệ API key.
- `web/index.html`: checkbox, trạng thái và danh sách sự kiện gần đây.
- `supabase/alerts.sql`: bảng và bucket private.
- `telegram_setup.py`: công cụ local lấy chat ID, không gửi tin nhắn.
- `.env.example`: danh sách biến môi trường rỗng, không tự động nạp.
- Dockerfile, dockerignore, prepare_render.py: đưa module mới vào gói triển khai.
- `tests/test_alerts.py`: kiểm thử dịch vụ giả lập, không gửi dữ liệu thật.

Chạy kiểm thử với môi trường đã cài requirements-test.txt:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_alerts.py tests/test_contract.py -q
```

Các test kiểm tra quyền/opt-in, cùng loại vi phạm qua nhiều ảnh, tách phiên,
hết hạn lịch sử, cooldown, queue đầy, lưu ảnh và nội dung multipart, lỗi
Supabase/Telegram, timeout không gửi lặp và đường suy luận không chờ network.
Kiểm thử giả lập không thay cho bước 6 với tài khoản thật của bạn.
