# Bản vá FXViewer — Live/H5

Bản vá dựa trên đúng source gửi trong hội thoại (viewer_fx(3).py và các helper đi kèm), ngày 23/10/2026. Không sửa backend hoặc giao thức mạng.

## Cài đặt

1. Sao lưu thư mục viewer hiện tại.
2. Giải nén gói. Chép các file `.py` và `requirements.txt` trong thư mục `viewer_patch` vào cùng thư mục chứa viewer hiện tại. Giữ tên chuẩn **viewer_fx.py**, **science_model.py**, **telemetry_protocol.py**, không dùng hậu tố `(1)/(3)`.
3. File mới **h5_raster.py phải nằm cạnh viewer_h5.py**.
4. Cài dependency bằng đúng Python dùng chạy viewer:

```bash
python -m pip install -r requirements.txt
```

Chạy Live:

```bash
python viewer_fx.py
```

Mở H5 trong cùng giao diện:

```bash
python viewer_fx.py --source h5 --h5 "file_1.h5" "file_2.h5"
```

Hoặc `python viewer_fx.py --offline`, sau đó Open H5 để chọn nhiều file. Khi khởi động H5, chưa tạo Receiver; chỉ khi chuyển sang Live mới khởi tạo receiver.

## Các file

| File | Trạng thái / nội dung |
|---|---|
| viewer_fx.py | Vá Deep continuity/count, tăng chiều cao tối thiểu để đọc trục |
| viewer_h5.py | Axis an toàn với plot ẩn, token Cancel, Pause snapshot/Deep visibility, tooltip, raster H5 |
| offline_h5.py | Validity coherence trước aggregation, bounds và miền timestamp, thêm điểm hủy khi index |
| requirements.txt | Bổ sung h5py |
| h5_raster.py | Mới: vẽ RGBA theo đúng cạnh thời gian mỗi hàng, NaN alpha=0 |
| h5_offline.py | Launcher tương thích chuyển sang FXViewer --source h5 |
| viewer_ui.py | Kèm nguyên bản để đủ helper |
| science_model.py | Kèm nguyên bản, chuẩn hóa tên file |
| telemetry_protocol.py | Kèm nguyên bản, không thay implementation |

Không cần thay viewer.py, viewer_dual.py hoặc read_udp.py. File `viewer_changes.patch` là unified diff để review với bản gốc tên chuẩn; có thể dùng các file hoàn chỉnh thay vì apply diff.

## Thay đổi gắn với audit

- F01: thay axis không gọi nhánh setAxisItems giả định axis còn ở scene; kiểm thử CW/Solar/Focus cả hai chiều.
- F02: thêm h5py>=3.10,<4.
- F03: dùng validity coherence của từng bin trước tích lũy. Bin sai lớn bị loại, các bin hợp lệ khác còn được giữ. ROI tiếp tục yêu cầu common valid mask.
- F04: lưu/khôi phục curve và text snapshot khi Live đang Pause; mở lại threshold/marker trong H5; khôi phục Deep checkbox/curve.
- F05: Cancel vô hiệu generation; worker không emit ready khi đã bị yêu cầu hủy.
- F06: Deep Live bỏ nhóm dang dở khi timestamp không liên tục và tính tổng M thực tế. **Giữ mean magnitude đều theo observation**, không tự đổi thành FFT-weighted hoặc coherent average. Ngưỡng continuity dùng cadence theo M với hệ số 1.5, cùng hằng số hop/sample-rate mà viewer hiện dùng. Backend không thay đổi.
- F07: tick H5 một dòng giờ, nhãn colorbar ngắn, mỗi plot có chiều cao tối thiểu. Overview nhiều plot có thể phải cuộn dọc; Focus vẫn dùng vùng lớn. Cửa sổ hẹp có thể cần cuộn ngang toolbar. Chưa xác nhận mọi DPI Windows.
- F08: từ chối frame bounds đảo, weight không dương và UTC vượt miền date editor; formatter axis có fallback.
- F09: tooltip cards cập nhật theo H5; hover trace ghi rõ raw và chất lượng mask.
- F10: entrypoint offline cũ dùng cùng FXViewer. Các tùy chọn CLI riêng của OfflineWindow cũ không được giữ; dùng `viewer_fx.py --help`.
- F11: bỏ PColorMeshItem khỏi H5. Raster không dùng NaN làm index LUT; ô mask có alpha=0, không thay thành measurement 0. Mỗi segment riêng, mỗi row dùng time edges thật; không nội suy qua gap.

## Ngữ nghĩa khoa học cần giữ

- H5 Fast/Deep dùng FFT-weighted complex average trong từng segment rồi mới tính magnitude/phase/coherence. Spectrum S12 dùng mean magnitude. Live Deep vẫn là mean magnitude đều theo observation, không coherent integration.
- Với hai H5 thật đã gửi: 1502 record, 512 bin; Fast tự gộp 2 record/row, Deep tối đa 5 record/row. Bản vá không thay sampling của file.
- Không hiệu chuẩn pha lần hai, không zero pha, không fringe-stop; đơn vị không đổi sang dBm/SFU.
- UTC trong hai H5 là estimated, metadata nói không có hardware UTC anchor.

## Kiểm thử đã chạy

- **23 test PASS** trên bản vá. Các test hồi quy hiện kiểm tra hành vi mong muốn sau vá, không còn coi tái hiện lỗi cũ là PASS.
- Hai H5 thật: 1502×512, worker thật, ngưỡng coherence **0.1**, 3 raster, 63112 ô phase mask có alpha=0. Render Overview, Fast và Fringe; không exception Qt trong probe. Chuyển H5 về Live từ Focus không cần bật mọi plot để tránh lỗi.
- Snapshot Pause có dữ liệu được giữ qua H5; Cancel muộn bị bỏ; ba chế độ plot và các Focus dùng cùng PlotItem.
- CLI `--source h5` / `--offline`: 0 Receiver constructor calls, 0 Qt exceptions trong startup probe.
- SHA256 hai H5 trước/sau bằng nhau. telemetry_protocol.py, science_model.py, viewer_ui.py giữ byte nội dung gốc.
- Môi trường: Linux offscreen, Python 3.12.14, numpy 2.3.5, PyQt5 5.15.11 / Qt 5.15.19, pyqtgraph 0.13.7, h5py 3.16.0.
- Chưa kiểm thử GUI Windows, DPI/mouse thật, UDP trực tiếp từ Pi hoặc chạy bền 24/7. Không coi bản vá này là nghiệm thu toàn hệ thống.

Thư mục `validation` chứa screenshot và log. Peak RSS của probe thật là 252040 KiB (~246 MiB); bao gồm cả dữ liệu so sánh độc lập và ảnh, không phải benchmark RAM thuần viewer.

Muốn chạy lại test: cài thêm pytest; đặt thư mục `upload` chứa đúng hai H5 người dùng đã gửi cạnh thư mục `viewer_patch`, rồi chạy:

```bash
python -m pytest viewer_patch/tests -q
python viewer_patch/tests/real_h5_check.py
```

Không cần chép tests/validation vào thư mục chương trình khi chỉ muốn chạy viewer.
