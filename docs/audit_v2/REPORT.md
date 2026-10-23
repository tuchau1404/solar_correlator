# Audit và bản vá ứng viên — 18/10/2026

## Kết luận

**Chưa xác định được nguyên nhân trực tiếp gây overflow trên Pi.** Log người dùng chứng minh CH1 đã mất dữ liệu sau khi khóa k=0; không có timing/occupancy/flags của binary thực để phân biệt backlog, DSP chậm, scheduling và tranh tài nguyên. Đây là bản vá orchestration và bổ sung bằng chứng, chưa phải chứng nhận hết lỗi hoặc đủ hiệu năng 10 MSPS.

CONFIRMED từ source: main khác bài test về callback continuity, số lần xác nhận, ngưỡng PNR, retries và vị trí tạo TimeAligner. Main cấp phát queue storage khoảng 9,895,936 byte payload sau prime. Các khác biệt đã được sửa có giới hạn; không đổi DSP math, buffer capacity, Fs, M, UDP hoặc file được bảo vệ.

## Nguồn và phạm vi

Đầu vào duy nhất: `solar_correlator_storage_v1.zip` có sẵn từ cuộc trò chuyện.
SHA-256: `7096eced3f64c12f9724064fb3d4d2828137aecc9243562040d3efc9af6ef74f`.
Đọc và giải nén vào baseline riêng, sửa bản sao. Không truy cập remote repo. CMake chạy lệnh git đọc provenance local rồi fallback SOURCE_REVISION; không fetch/pull. Không có AGENTS.md trong archive.

`src/main.cpp`: `b546d632fb5e44ab42fcb61137e0dbc2d0e245fefca5589059a584b7428d6579`.
`tests/alignment_test.cpp`: `477c4798dd6a0b815ac507f1ea4520bab8646a0ae3b468fc0504e52beae66489`.
`src/startup_alignment.hpp`: `1bc1763dfa4b921ceed7dfc142ed42000a1933c398b21c1a0fadb342ee38df75`.
Ba file local đều khớp ZIP. Chưa nhận source hiện tại từ Pi để chứng minh Pi chạy đúng các byte này. Xem input_hashes.json và changes.json để kiểm kê đầy đủ.

## Phát hiện

| Mức độ / trạng thái | File, điều kiện và bằng chứng | Tác động / xử lý |
|---|---|---|
| Cao — CONFIRMED | main callback bỏ qua firstSampleNum và return của ring.write; test kiểm tra cả hai | Có thể mất mẫu từ API mà không có ring overflow; áp dụng context/sequence check và dừng producer có cleanup như test. Test callback kiểm tra gap, reset, null params, wrap uint32, ring đầy. Không khẳng định đây là nguyên nhân overflow log. |
| Cao — CONFIRMED | main hậu kiểm 1 lần PNR>=10; test helper hậu kiểm 3 cửa sổ mới PNR>=20, mặc định 8 attempts | Main nay dùng cùng helper và defaults; vẫn hỗ trợ --max-align-retries. Lỗi/timeout/drop không được biến thành lock. |
| Cao — CONFIRMED về vị trí; HYPOTHESIS về hiệu năng | TimeAligner được tạo sau stream gate; storage queue/metadata/sockets tạo sau prime | Đưa FFT plans, vector, metadata, sockets và queue trước stream gate, sau hai acquisition fork. Worker chỉ kích hoạt sau alignment/calibration. Test deferred xác minh metadata và phi. Chưa đo chi phí cấp phát trên Pi. |
| Cao — CONFIRMED | main lấy overflow hiện tại làm baseline sau warmup | Có thể hợp thức hóa drop trước alignment; nay baseline=0, callback dừng ngay khi write thất bại; health check chạy trong acquisition/giữa cycles. |
| Trung bình — CONFIRMED | test chấp nhận abs(k)<=1, main/provenance yêu cầu k=0 | Không sao chép tolerance ±1 vào science main. Helper thêm option default=1 giữ nguyên test; main chọn 0. Test xác nhận +1 bị từ chối ở main policy. |
| Trung bình — CONFIRMED | test auto_reset_hardware chạy rm wildcard, sudo USB reset, service restart; không dùng main lock | Không đưa các lệnh này vào main; chúng có thể ảnh hưởng tiến trình khác và bỏ qua exit status. Không sửa bài test tham chiếu. |
| Chưa xác minh | Chi phí DSP/SDR callbacks/GPIO shell, throttling, SD I/O, worker và scheduling | Cần trace Pi và A/B. Thread worker vẫn khởi tạo Budget/Aggregator và I/O bất đồng bộ sau activation; bản vá không chứng minh tranh tài nguyên đã được loại bỏ. |

## Đối chiếu alignment đầy đủ

| Bước | alignment_test | main cũ | Quyết định trong bản vá |
|---|---|---|---|
| Reset/daemon | Xóa SHM wildcard, reset USB, restart, chờ 2.5s | SingleInstanceLock và cleanup có chủ sở hữu | Giữ main, tránh reset phạm vi rộng |
| Serial CH1/CH2 | Hai serial đầu theo enumeration | Sort hoặc CLI explicit | Giữ mapping main, không đảo baseline khoa học |
| SHM/readiness | Tên suffix PID; callback continuity | Tên cố định và lock; thiếu sequence check | Giữ lock/tên, áp dụng sequence check của test |
| SDR config | Config gain/antenna | Config gain/antenna, explicit IF/notches | Không đổi RF settings |
| Stream gate | Hai store liên tiếp, chờ 200ms | Hai store liên tiếp, chờ 250ms | Dùng 200ms; không gọi đây là hardware T0 đồng thời |
| Noise/GPIO | Guard trước gate | Guard và kiểm tra lệnh bổ sung | Giữ thứ tự noise/switch và kiểm tra main |
| TimeAligner | Tạo trước stream gate | Tạo sau gate | Theo test: tạo trước gate |
| Snapshot/retry | Flush trước mỗi candidate, wait có health/deadline | Retry advance paired, một hậu kiểm | Dùng helper test; flush chỉ khi chưa công nhận lock |
| Dấu offset | k>0 skip CH1; k<0 skip CH2 | Cùng dấu | Giữ; kiểm tra short skip |
| Fresh confirmations | Advance cùng N trên cả hai, 3 lần | Advance N, 1 lần | Theo test: 3 lần, không tái dùng cửa sổ |
| PNR và lock | >=20 dB, abs(k)<=1, 8 attempts | >=10 dB, k=0, 5 attempts | >=20, 8 attempts, nhưng k=0 ở main |
| Mẫu đến trong FFT | Producer chạy, peek không consume | Tương tự | Không thêm consumer; trace đo backlog; overflow vẫn fail-closed |
| Handoff/prime | Drain paired phục vụ stability test | Antenna settle, paired watermark, prime Welch | Giữ handoff riêng của main; không thay Welch bằng drain test |
| Stability | Giữ noise, kiểm tra mỗi 5s trong 60s | Quan trắc anten, Welch liên tục | Không sao chép vòng test hoặc suy ra performance từ test |
| Switch về anten | Khi kết thúc test | Trước quan trắc, 100ms settle và paired discard | Giữ main, thêm trace; watermark không bảo đảm đã hết dữ liệu USB in-flight |
| Overflow/timeout/cleanup | Require healthy, dừng children | Supervisor và fail-closed | Giữ guard; tăng kiểm tra continuity; không recovery quan trắc sau mất mẫu |

Bài test tham chiếu giữ nguyên byte. Chỉ helper dùng chung được thêm tùy chọn residual; default và thuật toán của bài test giữ nguyên. Thay đổi dòng thông báo LOCKED để dùng được trong cả hai chương trình.

## Producer/consumer và timeline

Ring.write từ chối **toàn bộ một callback** khi thiếu chỗ và tăng overflow counter 1. Do đó `0->8` là 8 lần write bị từ chối, không phải 8 mẫu. Cần callback size để tính số mẫu mất.

Capacity=4,194,304 mẫu, Fs=10,000,000 mẫu/s: sức chứa danh định 0.4194304s/kênh. Headroom thực=(capacity-occupancy)/Fs, còn phụ thuộc burst và scheduling.

Welch N=2048, hop=1024, M=1953: mỗi cycle tiến 1,999,872 mẫu = 0.1999872s; support có overlap 2,000,896 mẫu = 0.2000896s. DSP wait cả hai có >=hop trước khi đọc mỗi kênh hop; không có một blocking wait dài riêng CH1 rồi CH2. Deadline steady_clock 5s bao gồm wait và compute; ring có thể tràn trước deadline rất lâu.

| Pha | Trước | Sau | Timing Pi |
|---|---|---|---|
| Trước stream gate | FXCorrelator plans | FXCorrelator + TimeAligner + output vectors + telemetry + deferred storage queue | NOT MEASURED |
| Warmup | Gate rồi 250ms không consume, flush | Gate rồi 200ms, fail khi discontinuity, flush trước lock | NOT MEASURED |
| Alignment | Một hậu kiểm | Ba hậu kiểm mới, ngưỡng test | NOT MEASURED |
| Sau calibration | Switch/settle/paired discard/prime | Activate worker với metadata/phi cuối, switch/settle/paired discard/prime | NOT MEASURED |
| Sau prime | Tạo sockets, metadata, queue, worker | Không còn các constructor lớn này | NOT MEASURED |
| Quan trắc | Welch, UDP, queue submit | Giữ math/ABI; thêm health giữa cycles, trace giới hạn | NOT MEASURED |

Trace lưu tối đa 128 mốc trong RAM, steady wall và CPU của **thread cha**, occupancy và overflow tại mốc. Không đo CPU toàn hệ thống/child hoặc high-water liên tục. In khi thoát scope/dừng, không in mỗi FFT. Thu first 8 cycles và lỗi cycle; trace có thể hết chỗ khi người dùng đặt rất nhiều retries. Wall-CPU gồm wait/scheduling/I/O, không phải phép đo riêng thời gian chờ mẫu. CPU clock đọc thất bại sẽ không cho số CPU đáng tin cậy. Logging có sẵn của ứng dụng vẫn còn.

## Mười giả thuyết và trạng thái

1. Release flags binary trên Pi: NOT VERIFIED; kiểm tra compile_commands và đúng binary.
2. Cấp phát lớn khi đang streaming: CONFIRMED trong source cũ; quan hệ nhân quả với log chưa đo.
3. Backlog sau alignment: HYPOTHESIS; dùng snapshot/lock/prime trace.
4. Handoff mất quan hệ mẫu: paired watermark cũ giữ cùng số mẫu; chưa có bằng chứng sai dấu. Bảo toàn, không flush sau lock.
5. DSP không theo kịp: NOT VERIFIED trên Pi; host math PASS không chứng minh tốc độ Pi.
6. Đọc kênh bất cân đối: mỗi hop wait cả hai, đọc paired; vẫn cần đo producer burst/skew.
7. Storage/maintenance tranh tài nguyên: HYPOTHESIS; chạy A/B/C/D.
8. Deadline/cancel: thông báo cũ gộp ba nguyên nhân; log overflow khẳng định ít nhất có drop. Chưa xác minh deadline độc lập.
9. GPIO/reset/logging tạo backlog: source có GPIO system và settle trong lúc stream; trace đo antenna interval. Không thêm auto reset của test.
10. Acquisition/scheduling độc lập storage: NOT VERIFIED; callback sequence guard và test A giúp tách.

## Kiểm thử thực hiện

Môi trường host x86_64 Linux, GCC 13.3.0, C++17; build storage Release -O3 -DNDEBUG -fno-fast-math -ffp-contract=off. Không dùng -march=native ở host test. HDF5 1.14.5, nlohmann_json 3.11.3. SQLite dùng header local và system runtime, không chứng nhận tổ hợp dependency của Pi. Xem evidence/environment.txt và compile_commands.json.

| Kiểm thử | Kết quả | Giới hạn |
|---|---|---|
| CMake storage Release | PASS | Hardware/DSP target tắt ở CMake host |
| CTest telemetry/storage core/startup alignment | PASS 3/3 | Fake ring helper không tái hiện scheduling Pi |
| DSP oracle + continuity + cancellation/deadline/overflow | PASS | Build riêng với FFTW float từ pyFFTW wheel; host only |
| Callback thực trong main | PASS 5 tình huống | Anonymous SHM, synthetic SDK declarations; không gọi thiết bị |
| Python maintenance | PASS 16/16 | Offline |
| Python protocol | PASS 7/7 | Offline |
| Pipeline HDF5/SQLite/PNG tự khởi động | PASS | Fixture 60 source records, 10 rows |
| Pipeline trì hoãn + double activation rejection | PASS | Xác minh aligned, phase_calibrated, calibration_id metadata và phi=0.25 |
| Main có/không storage syntax | PASS | **Synthetic header, không phải vendor SDK build/link** |
| Protected files + alignment_test + viewer | PASS byte comparison | Xem protected_check.json |
| Full build/link SDK trên Pi của bản vá | NOT RUN | Cần người dùng build |
| A/B/C/D, nhiều startup, nhiều roll, Drive thật | NOT RUN | Không truy cập/vận hành Pi |

Trong lúc viết test deferred, đã sửa đường dẫn/type assertion của fixture theo schema HDF5 thật (/metadata, calibration_id integer, không có dataset calibration_id mỗi row). Không thay schema sản phẩm để làm test pass.

## Điều kiện nghiệm thu còn thiếu

Build bản vá bằng SDK thực; xác minh bản đầu vào trên Pi; thu trace A/B/C/D; kiểm tra số mất mẫu và queue drops, nhiệt/throttling; lặp startup và chạy qua nhiều roll/upload. Chưa có phép đo thời gian trước/sau trên cùng Pi. Không được gọi bản vá này là đã sửa hoàn toàn overflow hoặc ổn định dài hạn.
