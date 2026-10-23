# Storage v1 — hướng dẫn vận hành

Bản triển khai trên baseline `92d6889d46a864d403d0f519aebadf15ca4c614b`, chưa commit/push. Xem [REPORT.md](REPORT.md) trước khi nghiệm thu: storage portable đã build/test, ứng dụng hardware đầy đủ và Drive thật chưa chạy. Không coi đây là chứng nhận vận hành Pi 5.

## Phụ thuộc và build

Linux, C++17, CMake, FFTW3 single precision, SDRplay SDK của baseline, HDF5 C development, SQLite3 development, nlohmann/json >=3.10, Python3 có zoneinfo và tzdata. Python dùng numpy/h5py/matplotlib; các worker dùng rclone. Trên Raspberry Pi OS/Debian, cài các gói phát triển tương ứng (cần quyền quản trị):

```sh
sudo apt install build-essential cmake pkg-config libfftw3-dev libhdf5-dev libsqlite3-dev nlohmann-json3-dev python3-numpy python3-h5py python3-matplotlib tzdata rclone
cmake -S . -B build -DSOLAR_BUILD_STORAGE=ON
cmake --build build -j2
```

SDRplay SDK phải được cài theo hướng dẫn baseline. Lệnh cài/build trên Pi là hướng dẫn, chưa được chạy trên Pi trong phiên này. Nếu Python của hệ điều hành chưa đáp ứng `scripts/storage_requirements.txt`, dùng môi trường Python phù hợp; cả ứng dụng và timer phải tìm cùng interpreter/dependency. Timer sinh mặc định dùng `/usr/bin/python3`; kiểm tra dependency tại interpreter đó. Đường scripts được gắn vào binary lúc CMake configure: giữ source tại đường đã build hoặc configure/build lại sau khi di chuyển.

Build kiểm thử không cần SDR/FFTW:

```sh
cmake -S . -B build-storage -DSOLAR_BUILD_HARDWARE=OFF -DSOLAR_BUILD_DSP=OFF
cmake --build build-storage -j2
ctest --test-dir build-storage --output-on-failure
python3 -B tests/storage/test_maintenance.py
python3 -B tests/storage/run_pipeline_smoke.py --build-dir build-storage
python3 -B tests/test_protocol_v3.py
```

## Cấu hình và khởi động

`config/solar_storage.json` là cấu hình mẫu đầy đủ; schema đi cùng nó. Mọi đường tương đối tính từ thư mục JSON. Validator Python là contract chung, C++ gọi validator bằng argv rồi nạp kết quả chuẩn hóa. Không có mặc định ngầm khi thiếu trường; không hot reload. Kiểm tra và restart sau mỗi thay đổi.

```sh
python3 scripts/storage_config.py --config /opt/solar_correlator/config/solar_storage.json
/opt/solar_correlator/build/solar_correlator --config /opt/solar_correlator/config/solar_storage.json --check-config
/opt/solar_correlator/build/solar_correlator --config /opt/solar_correlator/config/solar_storage.json
```

Lệnh đầu chỉ validate, lệnh `--check-config` còn dự toán bằng geometry DSP thật, cả hai không gọi Drive/SDR. Không truyền `--config` giữ nhánh legacy, không lưu/probe. Có `--config` sai thì thoát lỗi, không fallback. CLI SDR cũ vẫn giữ nguyên. `storage.enabled=false` tắt writer; `quicklook.enabled=false` tắt PNG và được đóng băng trong snapshot từng file. Chạy offline có chủ đích: đặt cả `sync.enabled=false` và `startup_check.enabled=false`. Không tắt kiểm tra NTP trên máy quan sát chỉ để vượt lỗi cleanup.

Đổi `storage.save_freq_bins` thành 256/512/1024/2048 (ước số NFFT thực); đổi `save_interval_sec` không nhỏ hơn cadence nguồn. Khoảng lưu thực = ceil(target/cadence nguồn)*cadence nguồn. Mẫu 1s thực tế là 1.1999232s. Restart mở session/file mới, không hoàn lại quota cũ. Config hash và snapshot nằm trong metadata. Tham số khoa học/plot/PNG cap của file lấy snapshot; remote, timeout, lịch, retention và ownership vận hành lấy config hiện tại. Snapshot không chứa OAuth token.

## Drive và lịch nền

Trên máy người dùng, chạy `rclone config` để tự tạo remote; đặt tên/base dir/path rclone.conf vào JSON. Không đưa token vào dự án. Kiểm tra quyền đọc/ghi đúng thư mục đích. Mẫu dùng `gdrive:SolarInterferometer_Data/YYYY-MM-DD/`. Upload dùng `copyto`, xác minh bằng tải luồng và SHA-256 + số byte; tốn thêm băng thông đọc đầy đủ. Không có thao tác xóa dữ liệu quan sát trên remote. Probe là ngoại lệ duy nhất, trong namespace cấu hình.

Main lấy SingleInstanceLock, validate, recovery/cleanup/local check, rồi probe upload/readback trước HardwareGuard, USB/fork/SDR. Probe fail thì thoát khác 0. Thành công chỉ xác nhận thời điểm probe. Helper `startup` là nội bộ main; không chạy nó đồng thời với phiên main đang hoạt động.

Sinh service/timer từ JSON (chưa cài tự động):

```sh
python3 scripts/generate_storage_timers.py --config /opt/solar_correlator/config/solar_storage.json --output-dir /tmp/solar-storage-units
```

Xem các unit trước khi cài. Chạy service với cùng user có quyền thư mục data/state và rclone.conf; không cần root cho worker. Sync/cleanup có timer và flock riêng, không giữ khóa SQLite trong network. Khi thay interval trong JSON, sinh/cài lại timer. Chạy thủ công:

```sh
python3 scripts/storage_maintenance.py sync --config /opt/solar_correlator/config/solar_storage.json
python3 scripts/storage_maintenance.py cleanup --config /opt/solar_correlator/config/solar_storage.json
python3 scripts/read_storage_status.py --config /opt/solar_correlator/config/solar_storage.json
```

Ví dụ phần service quan sát (thay đường dẫn/user theo hệ thống, không cài nguyên mẫu):

```ini
[Service]
Type=simple
WorkingDirectory=/opt/solar_correlator
ExecStart=/opt/solar_correlator/build/solar_correlator --config /opt/solar_correlator/config/solar_storage.json
Restart=on-failure
```

Chọn RestartSec theo chính sách supervisor của trạm. Các cờ SDR legacy có thể thêm vào ExecStart. Binary chưa build với SDK trong môi trường bàn giao nên phải hoàn tất gate hardware trước vận hành.

## Quota, retention và phục hồi

Quota là byte logic sản phẩm + dự phòng phụ trợ, không phải tổng physical writes SD, filesystem block rounding hay OS journal/log. Mỗi HDF5 charge toàn bộ max_file_bytes trước khi tạo; PNG reserve cùng ngày quan sát, không hoàn lại sau xóa/thất bại. VFD chặn EOA/write/truncate vượt cap kể cả metadata. Nhiều restart/partial file sẽ dừng sớm hơn dự toán. Không tự giảm bins/cadence. Disk dùng hysteresis min/resume, queue hữu hạn; producer drop khi không thể nhận, không chờ IO.

Retention mẫu 5*86400s tính từ start mẫu đầu, không từ mtime. Có thể xóa cả chưa upload theo `delete_unuploaded_expired=true`; đây là mất dữ liệu có audit. Đặt false bảo vệ chưa verified nhưng không còn bảo đảm giới hạn tuổi. Mẫu cuối của file bị xóa sớm hơn tối đa độ dài file. Cleanup theo lượt + thời gian đọc/transfer giải phóng lock + latency filesystem; không đảm bảo xóa đúng giây hết hạn. Worker ghi số file busy và latency thực của lượt, chưa đo bound trên Pi. Clock jump/NTP không tin cậy làm pause xóa.

Không xóa ledger để “reset quota”. Marker data_dir gắn với ledger UUID/path; mất/move ledger thì startup từ chối. WRITING có tên final được kiểm schema/size rồi khôi phục READY, không suy ra upload VERIFIED. Temp/quarantine dùng tuổi manifest + grace và lock. Không tự xóa file lạ/không có ownership. Có lỗi ledger/version/cap phải điều tra và backup; không dựng DB mới để tiếp tục. DB có cap; lịch sử quota hiện chưa compact, có thể đầy sau vận hành dài và dừng an toàn.

Dừng main và hai timer trước migration. `python3 scripts/migrate_config.py old.json new.json` kiểm v1 và tạo bản canonical, không overwrite. `python3 scripts/migrate_ledger.py --config /absolute/config.json` lấy lock, kiểm v1 và tạo backup offline. Chưa có lịch sử schema cũ: tool từ chối version lạ, không giả vờ hỗ trợ chuyển v0/v2. Giữ config/ledger/data cùng nhau khi phục hồi; kiểm integrity và giữ quota đã charge. Không sửa finalized HDF5/PNG.

## Đọc và mở rộng

```python
import h5py, numpy as np
with h5py.File('finalized.h5', 'r') as f:
    hz = f['frequency_hz'][:]
    d = f['data']
    z = d['s12_real'][:] + 1j*d['s12_imag'][:]
    phase = np.where(np.abs(z)>0, np.angle(z), np.nan)
    time_us = d['timestamps_us'][:]
```

Dùng `scripts/solar_storage/reader.py` để kiểm version/shape và các hàm science để mask power/coherence đúng. Dataset lớn nên đọc chunk; không tái dựng bin gốc hay mean độ lớn nguồn từ visibility đã gộp. Tạo PNG một file qua `generate_quicklook.py --config ... --artifact-id ...`; lời gọi trực tiếp phải được supervisor giới hạn thời gian (sync worker đã làm việc này).

Thêm panel: cập nhật panels/science + config/schema + golden scientific tests, giữ byte/pixel cap. Thêm dataset: nâng data schema, reader/version gate, writer/record/aggregator, dự toán và test cross-language; không đổi nghĩa bốn mảng v1. Đổi backend: adapter transfer + identity/verification/error tests; không đổi DSP. Xem [ARCHITECTURE.md](ARCHITECTURE.md).
