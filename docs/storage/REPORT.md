# Báo cáo triển khai và kiểm thử — 2026-10-17

## Phạm vi kết luận

Đã triển khai source tích hợp, build storage C++ trên host Linux và kiểm thử synthetic/offline. Đây là bản để review/nghiệm thu tiếp, **chưa đủ bằng chứng nghiệm thu toàn bộ prompt trên Pi 5/SDRplay/Drive thật**. Không deploy, không push/merge, không upload dữ liệu quan sát thật. FAIL test còn tồn tại: không ghi nhận trong những test đã chạy; NOT RUN không được tính là PASS.

## Provenance

Repo https://github.com/tuchau1404/solar_correlator; remote HEAD thực là `refs/heads/main`. Baseline duy nhất `92d6889d46a864d403d0f519aebadf15ca4c614b`, commit time `2026-10-17T02:24:20Z`, fetch hoàn tất ghi nhận `2026-10-17T04:03:58Z` vào checkout riêng. Trước bàn giao đã `git ls-remote --symref ... HEAD` lần nữa: vẫn cùng SHA/main; không trộn revision mới. “Mới nhất” chỉ phạm vi HEAD main tại các lần kiểm tra, không mọi nhánh. Bản vá chưa commit; SOURCE_REVISION dành archive không có .git; runtime metadata phân biệt baseline và patch state.

Attachment duy nhất là `prompt_main_google_drive_v1.txt`, là yêu cầu chứ không phải source. Không có source attachment để diff với remote, không phát hiện local source edits cần lựa chọn baseline. Không có AGENTS.md được tìm thấy trong checkout. Dependency SDK không được cung cấp; không tái tạo SDK.

| File/module | Baseline path | Nguồn / revision | Khác biệt / quyết định |
|---|---|---|---|
| Main | src/main.cpp | remote, SHA trên | Giữ lifecycle/CLI; thêm --config/check, preflight và nhánh submit |
| Correlator | src/correlator.hpp/.cpp | remote, SHA trên | Thêm storage Re/Im double và getters sau cùng normalization/calibration |
| CMake | CMakeLists.txt | remote, SHA trên | HDF5/SQLite/json/storage targets; archive revision fallback |
| DSP tests | tests/test_dsp_math.cpp | remote, SHA trên | Thêm đối chiếu Re/Im với long-double DFT |
| Hardware/SHM/alignment/telemetry | 5 file bảo vệ dưới đây | remote, SHA trên | Không thay đổi byte |
| Viewer/parser/science | viewer/ | remote, SHA trên | Không thay đổi tracked tree |
| Storage modules | src/storage/ | mới, dựa baseline trên | Record, queue, aggregator, config, budget, VFD, writer, pipeline |
| Config/ledger | config/ | mới | JSON mẫu/schema và SQL version1 |
| Maintenance/quicklook | scripts/ | mới | Validator, startup, upload, retention, reader/science/panels, status, migration/timers |
| Synthetic tests | tests/storage/ | mới | Contracts, native IO/pipeline, maintenance, day simulation |
| Yêu cầu | prompt_main_google_drive_v1.txt | attachment | Dùng làm đặc tả, không ghi đè source |

SHA-256 file bảo vệ (đã so trực tiếp với git object baseline):

| Path | SHA-256 | Kết quả |
|---|---|---|
| src/hardware_control.hpp | 77fa7349a420ef71c254c0885408d432ff0fea2aa36721c4809ab042deab4901 | PASS |
| src/ring_buffer.hpp | 5ecc6be1efc9462dc828e4d334de4f2261efc6c8f942ce41ff796a51db458270 | PASS |
| src/time_alignment.hpp | 23103b5cd8a3dfe6d8b5067fb1b9848ddcadf61d26127b3ec4ee031b2aae7281 | PASS |
| src/time_alignment.cpp | 31393ca70440ffac5fcf1eeea9ba615a67500370d9dfa9f99ee490a59d52986d | PASS |
| src/telemetry_sender.hpp | c269fe1a115323843eefeda9073302f1eeadd48999d63f53ab1a240dbaebda6e | PASS |

## Định nghĩa xác minh từ source

| Mục | Thực tế baseline / quyết định |
|---|---|
| DSP geometry | FFT 2048, hop1024, M1953, Fs10MSPS |
| Cadence nguồn | 1953*1024/1e7 = 0.1999872s |
| Support nguồn | ((1953-1)*1024+2048)/1e7 = 0.2000896s; overlap không cộng đôi exposure |
| Window | Symmetric Hann denominator N-1; normalization dùng sum(window²) thực |
| Normalization | mean cross/raw ADC DFT /(M*N*sum(window²)); input /32768 rồi restore 32768²; không phải dBm/PSD hiệu chuẩn |
| Cross | X1*conj(X2), nhân exp(-j*phi); lấy Re/Im sau phase correction |
| Frequency | fftshift RF tăng dần; gộp mean contiguous, không wrap |
| Coherence | Magnitude gamma, không phải gamma²; sau gộp là estimator mới |
| dB | 10log10 linear ADC-count², không flux/dBm |
| Timestamp | system_clock sau xử lý; UTC sample suy từ processing anchor, uncertainty unknown |
| Telemetry | SOL2 v3, S12 magnitude9999/S11 9998/S22 9997/phase9996/coherence9995; khác dự kiến cũ gộp phase/coherence |
| API main | dispatch đủ5args, optional HardwareGuard chỉ emplace !skip_align, SingleInstanceLock, 2 child guards/forks, SHM overflow fail-closed giữ nguyên |
| Storage mặc định | ceil(1/.1999872)=6 cycles, cadence1.1999232s, 512bins spacing19531.25Hz |

Không thể tái tạo raw bins hoặc mean(|S12 nguồn|) sau gộp. Strict invalid-group mask mất toàn group nếu một thành phần invalid; chủ ý tránh weights theo bin lớn. HDF metadata lưu calibration vector unshifted, conventions, geometry, snapshot/hash/revision. Main không hỗ trợ thay calibration live trong phiên baseline.

## Dung lượng

Mô phỏng **tương đương 24 giờ**, không chạy SDR/real-time24h: 72,005 grouped rows, native HDF5 writer/ledger, 97 file và render đủ97PNG, compression none. Dữ liệu synthetic random với phase quay; các config khoa học/caps mặc định; sync/startup/NTP off, disk floor giảm cho fixture host. Không dùng hệ số nén đoán.

| Hạng mục | Byte logic đo / charge |
|---|---:|
| HDF5 thực, gồm metadata/scalars/chunks | 605,780,392 |
| PNG thực | 90,674,623 |
| Aux live fixture tại thời điểm đo | 131,273 |
| Tổng thực live fixture | 696,586,288 |
| HDF5 charge bảo thủ 97*8,500,000 | 824,500,000 |
| PNG charge 97*1,000,000 | 97,000,000 |
| Aux charge | 32,777,216 |
| Tổng charge | 954,277,216 |
| Daily limit | 1,000,000,000 |

Base arrays estimate589,864,960 không được dùng thay tổng pipeline. Aux đo là snapshot fixture, không ước lượng toàn OS; marker/log sau các sửa cuối có thể thay đổi vài byte, không chạy lại toàn24h sau bản sửa retry/PNG disk check. Full cap guard và final smoke đã chạy sau sửa. Ngày rollover97file gồm partial; nhiều restart có thể hết quota sớm. Quota dự phòng còn45,722,784 nhưng shares áp độc lập.

Năm ngày charge danh định khoảng4.77GB, thực synthetic khoảng3.48GB; đây không phải upper bound disk5GB: cửa sổ trượt có thể giao6ngày, writer/reader/temp/audit và filesystem overhead. Dự toán bảo thủ ít nhất6*1GB logical reservations + headroom disk floor500MB, thêm filesystem/OS theo đo trên thiết bị; không chứng minh giới hạn peak physical. Queue payload khoảng9,895,936bytes (151slots*4*2048*8), chưa gồm metadata/allocator/DSP/HDF/renderer RAM.

## Ma trận yêu cầu → cấu hình → module → bằng chứng

| Yêu cầu | Config keys | Module/test | Trạng thái và phạm vi |
|---|---|---|---|
| Schema/unknown/type/finite/crosschecks | config_version, toàn nhóm | config.py; test_maintenance | PASS invalid cases; C++ dùng cùng validator |
| Config thiếu/sai dừng trước SDR, legacy CLI | --config/--check-config | main.cpp | Source review; runtime hardware CLI NOT RUN |
| Complex averaging/weights/NaN/zero/±pi | save_interval_sec,save_freq_bins,partial_window_policy | test_storage_core | PASS, bins2048/1024/512/256 |
| Gap/calibration/backward UTC/partial | time.*, partial_window_policy | test_storage_core | PASS core; live calibration change không có API |
| SPSC ownership/full/concurrent | queue_capacity_records | test_storage_core | PASS100000 transfers; host, không Pi latency benchmark |
| Linear getters giữ DSP | không thêm DSP config | test_dsp_math | PASS tone, random longdoubleDFT, Parseval, edge/error |
| UDP tương thích | legacy source config | telemetry_sender_contract + test_protocol_v3 | PASS CTest và7Python tests |
| HDF schema/chunks/cap | chunk_rows,compression,max_file_bytes | test_storage_io | PASS750rows,6,307,456B golden;1KBcap bị chặn |
| Native writer→ledger→PNG | append_batch_records,quicklook.* | run_pipeline_smoke.py | PASS60source→10rows,weights11718,Re1.8/Im2.4 |
| Quota cross-language/restart/delete không refund | budget.*,ledger.* | native IO + maintenance | PASS reservation/readback/quota/retention; oldday refused |
| Hai process tranh PNG reservation | max_png_bytes | test_maintenance | PASS chỉ một charge |
| Recovery publication/tombstone/generation | stale_temp_grace_sec,ledger.* | test_maintenance | PASS finalname-before-READY, DELETE_PENDING replay, stale job |
| Version/migration gate | config_version,ledger version | test_maintenance + tools | PASS unknownledger rejected; migration restore exercise NOT RUN |
| Probe pass/fail | startup_check.* | Fake adapter tests | PASS mock; realauth/timeout/token/SDRsequence runtime NOT RUN |
| Identity remote | sync.remote_*,rclone_config_path | transfer.identity tests | PASS target change/access token refresh/new refresh token |
| Upload digest/size/retry | sync.verification_policy,timeout/backoff | transfer+maintenance | Implemented; full adapter fault matrix/realDrive NOT RUN |
| Retention trước/đúng tuổi/configdays | retention.days,delete_unuploaded_expired | test_maintenance | PASS trước/đúng5ngày, thaydays, giữunverified option |
| Busy reader/audit/no resurrection | reader_timeout_sec,audit/tombstone caps | test_maintenance | PASS lock skip/stale generation; cancel hung kernel NOT RUN |
| Clock jump/symlink guard | time.*,data_dir | test_maintenance | PASS synthetic forward jump/symlink; realNTP outage NOT RUN |
| Full day total bytes | budget.*, defaultscience | measure_day.cpp + measure_pngs.py | PASS equivalentday,97HDF+97PNG, bytes bảng trên |
| Status freshness | status.* | read_storage_status.py | Implemented; dedicated freshness regression NOT RUN |
| Diskfull/flush/close/rename/powerloss | min/resume_free_bytes,io_retry_* | VFD/pipeline | Hardcap PASS; OS fault injection NOT RUN |
| Pi5 CPU/RAM/IO/queue/retention lag | các giới hạn config | hardware benchmark | NOT RUN |
| File bảo vệ/viewer | không áp dụng | git byte/hash comparison | PASS |

C++ storage build PASS toàn targets, CTest2/2, Python maintenance16/16, protocol7/7, DSP oracle PASS. Native final smoke PASS sau thay retry flush và PNG disk floor. Full hardware executable **NOT RUN** vì thiếu SDRplay SDK/device; không suy từ portable library rằng main đã build.

Môi trường host: g++13.3, CMake4.4.4, HDF5 source1.14.5, nlohmann/json3.11.3, Python3.12, numpy2.5.4/h5py3.16/matplotlib3.11, FFTW float từ pyFFTW wheel. SQLite link runtime hệ thống, header local từ rusqlite; chỉ dùng API ổn định, version runtime được ghi thêm trong evidence.json. Không có apt privileges; dependency build trong scratch, không bundle binary/SDK. HDF51.10 compatibility branch, gzip filter, ARM ABI **NOT RUN**.

## Giới hạn / gate còn mở

- Chưa full build/link/run main với SDK, chưa kiểm CLI/probe-before-SDR bằng thiết bị, chưa benchmark deadline/CPU/RAM Pi. Phải chạy trước nghiệm thu.
- Chưa exhaustive crash/powerloss/ENOSPC/fsync/rename faults; crash model có targeted tests nhưng không chứng minh mọi interleaving.
- Chưa real rclone/Drive hoặc complete mock subprocess error classification matrix; max attempts lưu theo job (có thể cạn vĩnh viễn, không reset mỗi phiên). Recovery retry cần operator policy, không hứa tự phục hồi mọi lỗi.
- Cleanup đợi bounded managed reader bằng lock, nhưng không tự kill process tùy ý/direct renderer hoặc kernel IO treo. Sync child có timeout; lời gọi renderer thủ công phải supervise. Không chứng minh tuổi giữ upper bound tuyệt đối.
- UTC sample là estimate; producer không tự đổi anchor khi wall-clock nhảy liên tục mà giữ sample cadence. Original timestamps lưu lại; cleanup clock guard riêng. Không có hardware UTC uncertainty calibration.
- Status phân thành writer/worker snapshots, không đầy đủ structured error codes/session/revision mọi worker như đặc tả lý tưởng. C++ lỗi chính ở status, Python có rotating logging.
- Quota history chưa compaction lâu dài; DB tới cap sẽ fail/pause, không reset. Aux là conservative reservation, chưa OS physical accounting. Unknown files/symlink không tự dọn.
- Remote probe orphan record chưa gắn account identity như upload job; chỉ namespace/session/path, nên đổi tài khoản cần kiểm tra probe cũ thủ công trong đúng tài khoản. Không xóa remote quan sát.
- Calibration live ngoài phạm vi API baseline; future change cần mở file mới. Config migration hiện chỉ canonical/backup v1, không chuyển version chưa tồn tại.

Các mục NOT RUN/giới hạn trên là blocker cho tuyên bố “đạt toàn bộ yêu cầu/production-ready”, không được che bằng số test PASS. Source được giao đầy đủ để tiếp tục kiểm thử trên môi trường đích.
