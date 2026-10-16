# Telemetry v3: phase và coherence đồng thời

Bản vá dựa trên commit `7b78ef472d416b0de6a721ef21aa35958f39a5c8` của
`https://github.com/tuchau1404/solar_correlator`, clone sạch ngày 16/10/2026.
Không có thay đổi cục bộ trong checkout lúc bắt đầu. Không push và không chạy phần cứng.

## Kết quả đọc code trước khi sửa

Đã đọc `src/main.cpp`, `config.hpp`, `network_config.hpp`, `telemetry_sender.hpp`,
`correlator.hpp/.cpp`, `viewer/telemetry_protocol.py`, `viewer_fx.py`, các test telemetry/viewer và CMake.

Correlator tính `X1*conj(X2)`, tích phân visibility phức, rồi áp dụng vector pha
`phi` theo thứ tự FFT chưa shift. Các đầu ra đã được fftshift qua `j=(k+N/2)%N`.
Main chỉ đổi auto-power tuyến tính sang dB; không fftshift lần nữa.

- `S12_dB = 10 log10(max(|S12|, 1e-12))`.
- `S11_dB`, `S22_dB` lấy từ auto-power đã chuẩn hóa ADC-count²/bin.
- Phase = `atan2(Im(S12), Re(S12))`, radian; bao gồm phép bù `phi` đang có.
- Coherence **amplitude** `gamma = |S12| / (sqrt(S11)*sqrt(S22))`, clamp 0–1.
  Không phải gamma². Power bằng 0 cho phase/coherence bằng 0 nhưng đó là ước lượng invalid.
- Tần số bin `j`: `30 + j*10/2048` MHz. Bin 1126 = 35.498046875 MHz.
- Công suất là dB re 1 ADC-count²/bin, không phải dBm hoặc SFU.
- Header v2 có 52 byte, flags luôn 1, mỗi đối tượng sender tạo một session riêng.
  Vì thế v2 không tự mô tả phase/coherence hoặc phân biệt S11/S22/S12.

Các file thuật toán/hardware bị giới hạn trong yêu cầu không được sửa.

## Hợp đồng SOL2 v3 cuối cùng

Tất cả integer và IEEE-754 float32 dùng big endian. Mỗi datagram có 52 byte header
và 256 float32, tổng **1076 byte UDP payload**; 8 datagram cho một phổ 2048 bin.
Không truyền ABI struct. Không thay nghĩa v2: backend main dùng version 3.

| Offset byte | Kiểu | Ý nghĩa |
|---|---|---|
| 0 | 4 byte | ASCII `SOL2` |
| 4 | uint16 | Version = 3 |
| 6 | uint16 | Header bytes = 52 |
| 8 | uint64 | Session ngẫu nhiên, khác 0; main tạo một lần và chia sẻ cả năm sender |
| 16 | uint64 | Frame ID, từ 0; cùng cycle dùng cùng ID; cycle bị bỏ để lại gap nếu tiếp tục phục hồi |
| 24 | uint64 | UTC Unix microseconds, lấy một lần **sau khi** cycle DSP hoàn tất; không phải hardware sample timestamp |
| 32 | uint32 | Integration frames = 1953 |
| 36 | uint32 | Flags = `1 | (payload_type << 8)`; bit 0 = fftshifted; mọi bit khác ngoài vùng type phải bằng 0 |
| 40 | uint16 | Total bins = 2048 |
| 42 | uint16 | Chunk index 0–7 |
| 44 | uint16 | Chunks per frame = 8 |
| 46 | uint16 | Bins in this chunk = 256 |
| 48 | uint32 | Bin offset = chunk index × 256 |
| 52 | 256 × float32 | Payload của chunk |

| Type | Flags | Cổng mặc định | Payload |
|---|---|---|---|
| 1 | 0x101 | 9999 | Cross-power magnitude S12, dB |
| 2 | 0x201 | 9998 | Auto-power S11, dB |
| 3 | 0x301 | 9997 | Auto-power S22, dB |
| 4 | 0x401 | 9996 | Cross-phase rad, −π đến +π |
| 5 | 0x501 | 9995 | Coherence amplitude gamma, 0–1 |

V3 hiện cố định N=2048, M=1953, hop=1024, fs=10 MHz và RF center=35 MHz.
Không có metadata tần số/cadence tùy biến trên wire. Khi thay cấu hình DSP phải cập nhật
hợp đồng/receiver trước. Metadata giữa các luồng phải khớp timestamp và integration count chính xác.

Main gửi cả phase và coherence từ cùng vector kết quả cycle; không tính lại DSP,
không log10 phase/coherence. Đường kiểm tra cycle/overflow/producer và fail-closed
vẫn nằm trước mọi send. Thay đổi ngoài CLI/send chỉ là tăng telemetry frame ID khi
cycle bị bỏ, để lịch sử không nối qua lần phục hồi chẩn đoán.

Sender dùng socket nonblocking, `MSG_DONTWAIT`, PMTU-discovery và không retry trong
đường send. Giữ thống kê `attempted`, `queued`, `failed`, `datagrams`, `invalid`,
`would_block`, `no_buffers`, `too_large`, `interrupted`, `other`, `last_errno`.
`queued` chỉ có nghĩa kernel nhận đủ 8 datagram, không có xác nhận delivery.

## Receiver, ghép frame và giới hạn bộ nhớ

`Receiver(..., expected_type=T)` chỉ nhận v3 đúng loại T; cổng chỉ định vị socket,
header mới là nguồn nhận diện loại dữ liệu. Sai type/version hiện lỗi trong status bar
và tăng counter, không chuyển sang cách diễn giải khác. Receiver không sửa số liệu invalid.

- Reassembly: tối đa 8 frame dở dang mỗi luồng, timeout 0.75 s theo monotonic clock.
  Chấp nhận chunk đảo thứ tự, duplicate giống nhau; duplicate khác nội dung bị loại/quarantine.
  V3 cho phép frame hoàn tất đảo thứ tự trong cửa sổ 8 frame; giữ 64 ID hoàn tất gần nhất.
- Mỗi receiver có queue 300 frame, tràn queue bỏ frame hiển thị cũ và tăng `display_skips`.
- Join cache: tối đa 32 key `(session, frame ID)`, grace period 0.35 s.
  S12 quyết định session đang hiển thị. Giữ tối đa 32 session cũ để loại gói trễ.
- UI nạp xen kẽ các queue theo timestamp/frame; không ghép các bản “latest” theo cổng.
  Có đủ luồng thì xuất ngay; hết grace thì xuất S12 và những luồng có metadata khớp.
  Luồng thiếu không chặn UI, không mượn coherence frame trước. Luồng không có S12 hết hạn bị bỏ.
- Một frame đã xuất không được AUX đến trễ sửa lại. Frame trễ hơn ID đã xuất bị bỏ.
  Khi quá giới hạn cache, frame cũ bị loại; gap thể hiện trong lịch sử.
- Session mới reset lịch sử, deep accumulator và mốc frame; không ghép khác session.
- Lịch sử S12/phase/coherence giới hạn 300 slot; time trace cũng 300 slot. Gap rất dài
  chỉ chèn tối đa 300 slot NaN. Bộ nhớ không tăng theo thời gian chạy.

Counter `frame_gaps` ở reassembler là số gap quan sát lúc hoàn tất frame tiến tới;
frame hoàn tất trễ có thể lấp gap nên đây không phải thống kê mất gói cuối cùng.
UDP không có authentication/retransmission; tùy chọn `--source-ip` lọc nguồn IP.

## Viewer CW và Solar

Mặc định CW, chọn 35.5 MHz và làm tròn về bin gần nhất. Không tự bám peak.
Click phổ hoặc nhập MHz; GUI hiện tần số yêu cầu, bin và tần số thực tế.
Đổi lựa chọn xóa time trace; waterfall toàn dải vẫn được giữ.

- Time trace: phase wrapped độ, −180° đến +180°, coherence cùng bin/frame.
  Trục thời gian dùng UTC timestamp nhận được, nhãn Vietnam UTC+07:00.
  Chèn NaN qua gap, phase invalid, bước wrap >180°, thời gian lùi hoặc gap thời gian
  lớn hơn 1.5 cadence danh định. Không unwrap và không làm mượt pha.
- Mask: cần phase và gamma khớp frame, hữu hạn, gamma >0 và gamma ≥ ngưỡng hiển thị.
  Gamma=0 luôn invalid kể cả ngưỡng đặt 0. Thiếu gamma thì phase trống.
- Waterfall pha và S12 dùng cùng bin, frame slot và nhãn UTC+7 lấy từ timestamp từng frame.
  Đây là trục **frame slot có nhãn thời gian**, không phải phép nội suy lên lưới UTC đều.
  Gap không có timestamp được để trống. Hai đầu bảng màu pha −180°/+180° cùng màu.
  NaN được render trong suốt, không biến thành pha 0°.
- Solar hiện thêm waterfall pha và waterfall S12 trung bình sâu. CW ưu tiên time trace.
  Có thể chuyển mode trong GUI. S11/S22 chỉ overlay nếu khớp observation hiện tại.
- Các colorbar công suất chỉnh được. Ngưỡng gamma mặc định 0.5 chỉ phục vụ hiển thị,
  không được dùng như chuẩn nghiệm thu solar burst.
- Deep S12 giữ phép trung bình miền công suất tuyến tính từ |S12| rồi đổi về dB.
  Không phải coherent integration visibility, không trung bình góc wrapped.

## Build và chạy

Trên Pi đã cài SDK SDRplay, FFTW3 single precision và pkg-config:

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DSOLAR_NATIVE=ON
cmake --build build -j4
```

Thay `192.168.1.2` bằng IPv4 máy viewer. Các lệnh sau là hướng dẫn, **chưa chạy trên Pi**.
Giữ quy trình alignment/calibration hiện hữu; chế độ GUI không thay chế độ hardware.

Backend cho cả CW và solar (mặc định năm cổng như bảng):

```bash
./build/solar_correlator --dest-ip 192.168.1.2 \
  --port-s12 9999 --port-s11 9998 --port-s22 9997 \
  --port-phase 9996 --port-coherence 9995
```

Viewer trên máy nhận:

```bash
python -m pip install -r viewer/requirements.txt
python viewer/viewer_fx.py --mode cw --frequency-mhz 35.5 --coherence-threshold 0.5
python viewer/viewer_fx.py --mode solar --frequency-mhz 35.5 --coherence-threshold 0.5
```

CW bỏ alignment chỉ trong quy trình chẩn đoán đã chủ động chọn: backend thêm
`--skip-align`; GUI vẫn `--mode cw`. Không dùng cờ đó như mặc định quan trắc.
Muốn phase calibration, dùng cờ `--cal-phase` đang có sau khi lựa chọn quy trình phù hợp;
backend vẫn cấm kết hợp `--skip-align` và `--cal-phase` như trước.

S11/S22/phase/coherence có thể tắt bằng port 0 ở **cả hai phía**.
S12 bắt buộc 1..65535; mọi cổng bật phải khác nhau. Nếu tắt coherence, S12 vẫn chạy,
nhưng phase bị mask hoàn toàn.

## Chuyển từ v2 / CLI cũ

`--port-aux` và `--aux-mode` bị từ chối bằng thông báo chuyển đổi rõ ràng ở cả main và
viewer FX. Dùng `--port-phase` và `--port-coherence`; cập nhật backend và viewer cùng lúc.
Không có auto-detect hoặc fallback v2 trong viewer FX mới.

Để tránh phá công cụ cũ, constructor C++ hai tham số `TelemetrySender(ip, port)` vẫn
phát **v2** flags=1. Các tool test/dual-waterfall cũ và `viewer.py`, `viewer_dual.py`
tiếp tục đường legacy này, được test riêng. Main mới gọi constructor có session/type nên phát v3.
Không gửi phase/coherence qua constructor legacy và không ghép các session v2 độc lập.

## File thay đổi

| File | Lý do |
|---|---|
| `src/main.cpp` | CLI hai cổng, session chung, gửi năm vector cùng cycle; frame gap khi cycle bị bỏ |
| `src/network_config.hpp` | Cổng phase/coherence mặc định |
| `src/telemetry_sender.hpp` | Typed v3 và session do main cấp, giữ constructor legacy v2 |
| `viewer/telemetry_protocol.py` | Decode type/version, cache ghép frame, restart và giới hạn bộ nhớ |
| `viewer/science_model.py` (mới) | Mô hình lịch sử, chọn bin, mask pha, ngắt wrap/gap |
| `viewer/viewer_fx.py` | GUI CW/Solar, time trace, waterfall pha, điều khiển mask và tần số |
| `tests/test_telemetry_sender.cpp` | Header v2/v3, đồng nhất năm loại, nonblocking/no-allocation, errno counters |
| `tests/test_protocol_v3.py` (mới) | Reassembly, type/version, loss, duplicate, timeout, restart, metadata, session isolation |
| `tests/test_science_viewer.py` (mới) | CW/burst, wrap/gap/bin, memory bounds, GUI offscreen/alpha/color/geometry/click |
| `tests/send_v3_fixture.cpp` (mới) | Sender năm luồng giả lập, không hardware |
| `tests/test_udp_v3.py` (mới) | C++ → UDP → Python → GUI, CW/burst/restart |
| `CMakeLists.txt` | Thêm executable fixture để kiểm thử tích hợp C++/Python; không thêm dependency |
| `docs/02-firmware-dsp/telemetry-v3-upgrade.md` | Hợp đồng, sử dụng, giới hạn và bàn giao |
| `docs/02-firmware-dsp/module4-telemetry.md`, `viewer-modes.md` | Dẫn tới hợp đồng mới, đánh dấu tài liệu cũ không dùng cho v3 |

`src/config.hpp` được đọc nhưng không cần sửa; `UDP_PORT_AUX` cũ còn để tương thích nguồn.
`viewer/requirements.txt` giữ nguyên.

## Kiểm thử có thể chạy lại không cần SDR

```bash
cmake -S . -B build -DSOLAR_BUILD_HARDWARE=OFF -DSOLAR_BUILD_DSP=OFF
cmake --build build -j2
ctest --test-dir build --output-on-failure
QT_QPA_PLATFORM=offscreen PYTHONDONTWRITEBYTECODE=1 \
  SENDER_BINARY=./build/test_udp_sender V3_SENDER_BINARY=./build/send_v3_fixture \
  python -m unittest discover -s tests -p 'test_*.py' -v
```

Kết quả thực chạy nằm trong `VALIDATION.txt` của gói bàn giao. Môi trường kiểm tra:
Linux x86_64, GCC 13.3.0, Python 3.12, NumPy 2.3.5, PyQt5 5.15.11,
Qt 5.15.19 và PyQtGraph 0.13.7. Build UDP-only bằng CMake; main còn được
biên dịch/liên kết riêng với SDK SDRplay 3.15 và thư viện FFTW có sẵn, chạy help/CLI sai
trước đường hardware. Compiler cảnh báo sẵn có trong `hardware_control.hpp` về bỏ qua
return value `system()`; file này không được sửa.

Các kiểm thử bao phủ 11 nhóm yêu cầu: năm payload cùng định danh; sai version/type;
đảo/mất/trùng chunk/timeout; restart frame 0 session mới; không ghép coherence n−1;
CW một bin với pha nhiễu ở bin khác; wrap/gap/đổi bin; thiếu coherence vẫn có S12;
NaN mask render alpha 0; burst dịch tần pha biết trước; cache/history có giới hạn.
Có kiểm tra backlog 80 frame và click chọn tần số. Đã xem ảnh GUI offscreen ở hai mode.

Chưa xác minh Windows, Pi 5/ARM, SDRplay streaming thật, mạng giữa Pi và PC,
hiệu năng dài hạn, path MTU thực tế, calibration vật lý hoặc quan trắc solar burst thật.
Chưa có tiêu chí chất lượng khoa học ngoài mask hiển thị, chưa hỗ trợ unwrap,
chưa có per-bin flags chất lượng hardware. Không thêm nhãn dBm/SFU.
