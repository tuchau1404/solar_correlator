# Build và đo trên Pi — không ghi đè cây nguồn/config cũ

Gói chứa đầy đủ source, nhưng là bản vá ứng viên chưa chạy thiết bị thật. Giải nén thành `/home/radi02/solar_correlator_audit_v2/solar_correlator`. Không chép đè thư mục cũ hoặc config/data/state. Các lệnh dưới đây để bạn chủ động chạy; audit chưa vận hành Pi.

## 1. Xác minh source đang chạy trước đây

```bash
cd /home/radi02/solar_correlator_storage_v1/solar_correlator
sha256sum src/main.cpp tests/alignment_test.cpp src/startup_alignment.hpp
```

So sánh với REPORT.md. Nếu khác, giữ các sửa đổi Pi và gửi ba file cùng helper/module đã đổi trước khi áp dụng bản vá vào bản vận hành. Gói này lấy ZIP của chat, không tự trộn code Pi chưa được cung cấp.

## 2. Build vào thư mục mới

```bash
cd /home/radi02/solar_correlator_audit_v2/solar_correlator
cmake -S . -B build-release -DCMAKE_BUILD_TYPE=Release -DSOLAR_NATIVE=ON -DSOLAR_BUILD_STORAGE=ON -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
cmake --build build-release -j2
ctest --test-dir build-release --output-on-failure
./build-release/test_dsp_math
python3 -B -m unittest discover -s tests/storage -p test_maintenance.py
python3 -B -m unittest discover -s tests -p test_protocol_v3.py
python3 -B tests/storage/run_pipeline_smoke.py --build-dir build-release
python3 -B tests/storage/run_pipeline_smoke.py --build-dir build-release --deferred
mkdir -p audit-logs
uname -a > audit-logs/environment.txt
c++ --version >> audit-logs/environment.txt
sha256sum build-release/solar_correlator >> audit-logs/environment.txt
cp build-release/compile_commands.json audit-logs/compile_commands.json
```

CTest mới không tự chạy alignment_test phần cứng, USB reset hoặc GPIO. Không dùng alignment_test cùng lúc với main: bài test gốc không giữ khóa ứng dụng và có reset phạm vi rộng.

## 3. Giữ config vận hành

Dùng đường dẫn tuyệt đối tới config đã hoạt động, không dùng file mẫu của gói mới thay thế rclone config đang có:

```bash
./build-release/solar_correlator --config /home/radi02/solar_correlator_storage_v1/solar_correlator/config/solar_storage.json --check-config
```

Tạo bản B cùng thư mục config để giữ cách resolve relative paths; lệnh từ chối ghi đè nếu file đã tồn tại:

```bash
python3 - <<'PY'
import json,pathlib
p=pathlib.Path('/home/radi02/solar_correlator_storage_v1/solar_correlator/config/solar_storage.json')
c=json.loads(p.read_text());c['storage']['enabled']=False
with p.with_name('solar_storage_audit_off.json').open('x') as f:
    json.dump(c,f,indent=2)
PY
```

B chỉ đổi storage.enabled; startup probe/cleanup giữ chính sách config gốc. C/D sử dụng quota/ledger hiện tại khi tạo dữ liệu mới, không reset ledger để né quota. Không xóa dữ liệu để làm benchmark. Nếu muốn dữ liệu audit riêng, cần thiết kế ngân sách chung trước; không tự cấp lại daily cap bằng ledger mới.

## 4. A/B/C/D: chạy lần lượt, cùng serial và binary

Kiểm tra maintenance hiện có bằng `systemctl --user list-timers --all`. Ghi tên/trạng thái các unit thực tế. A/B/C cần maintenance dừng; D bật lại đúng các unit đã dùng với config tương ứng. Không chạy hai phiên SDR hoặc test phần cứng đồng thời. Không tự đổi affinity/governor/Fs để so sánh.

A — legacy:

```bash
./build-release/solar_correlator --serial-ch1 223802EC48 --serial-ch2 223802EE48 > audit-logs/A.log 2>&1
```

B — config, storage tắt, chính sách probe giữ nguyên:

```bash
./build-release/solar_correlator --serial-ch1 223802EC48 --serial-ch2 223802EE48 --config /home/radi02/solar_correlator_storage_v1/solar_correlator/config/solar_storage_audit_off.json > audit-logs/B.log 2>&1
```

C — storage bật, maintenance dừng:

```bash
./build-release/solar_correlator --serial-ch1 223802EC48 --serial-ch2 223802EE48 --config /home/radi02/solar_correlator_storage_v1/solar_correlator/config/solar_storage.json > audit-logs/C.log 2>&1
```

D — cùng lệnh C sau khi bật lại maintenance đã xác minh, đổi output thành `audit-logs/D.log`.

Dừng bằng Ctrl+C để dump trace và cleanup. Log chuyển hướng không hiện live; xem bằng terminal SSH khác `tail -f audit-logs/C.log`. Đợi ứng dụng thoát hoàn toàn trước phép thử kế tiếp. Không dùng kill -9 để lấy kết quả test.

Từ SSH khác trong mỗi lần chạy:

```bash
vmstat 1 60 > audit-logs/vmstat-C.txt
vcgencmd measure_temp
vcgencmd get_throttled
free -h
python3 scripts/read_storage_status.py --config /home/radi02/solar_correlator_storage_v1/solar_correlator/config/solar_storage.json
```

Đổi nhãn theo A/B/C/D; lưu kết quả temp/throttled/status trước và sau. Nếu có pidstat, lấy số CPU/RAM theo PID cha và hai producer. Trace CPU là thread cha, không đại diện tổng tải Pi.

## 5. Đọc trace và gửi kết quả

- `preallocate.*` phải nằm trước `stream_gate.*`, occupancy trước gate phải 0.
- `snapshot.*`, `alignment.locked`: theo dõi occupancy tăng trong tính tương quan.
- `storage.activate.*`: thời gian cập nhật metadata và tạo thread, **không bao gồm hết I/O khởi tạo bên worker**.
- `antenna.*`: gồm GPIO, settle và paired boundary.
- `prime.*`, `integration.*`: lấy chênh wall/cpu và occupancy; cycle cadence danh định 0.1999872s.
- Nếu occupancy tăng liên tục dù không còn constructor sau prime: điều tra throughput/scheduling. Không chỉ tăng timeout/buffer.
- `health.*`, overflow>0, sequence gap hoặc reset vẫn phải dừng, không xuất chu kỳ lỗi như aligned.

Cần ít nhất nhiều lần startup mỗi cấu hình và C/D qua nhiều roll 15 phút, kiểm tra HDF5/PNG/upload thực. Gửi log, compile_commands, status và thời lượng; không gửi token/rclone config bí mật. Các phép thử hiện tại trên host không thay thế bước này.
