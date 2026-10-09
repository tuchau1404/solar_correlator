# Next plan
> Hệ thống giao thoa kế vô tuyến đồng pha 2 kênh thời gian thực dải Decametric trên nền tảng Raspberry Pi 5 và SDRplay RSPdx phục vụ quan sát bùng nổ bức xạ Mặt Trời (Type II, Type III)[cite: 1, 4].

---

## 📌 1. Tổng quan Kiến trúc Hệ thống

Hệ thống được thiết kế theo mô hình phân tách hai tầng: **Edge DSP Backend (Raspberry Pi 5)** chịu trách nhiệm thu gom và xử lý số hiệu năng cao, kết hợp **Host GUI Frontend (Máy tính cá nhân)** để hiển thị phổ và thác nước thời gian thực[cite: 1, 4].

```
       [ Anten 1 ]                 [ Anten 2 ]
            │                           │
            ▼                           ▼
     [ RSPdx SDR #1 ]            [ RSPdx SDR #2 ]
     (REFin 24 MHz) ◄─────┬─────► (REFin 24 MHz) [Khóa phần cứng 24 MHz]
            │             │             │
            └─────────┐   │   ┌─────────┘
              (USB 3.0)   │   (USB 3.0)
                      ▼   ▼   ▼
        ┌────────────────────────────────────────────────────────┐
        │       RASPBERRY PI 5 (Edge DSP - Headless CLI)         │
        │                                                        │
        │  [Module 1] Thu 2x 10 MSPS -> POSIX SHM (80 MB/s)      │
        │  [Module 2] Flush Barrier & Căn chỉnh trễ mẫu (k = 0)  │
        │  [Module 3] FX Correlator (FFTW3 NEON, τ = 200 ms)     │
        │             ├── Phổ tự tương quan S11, S22             │
        │             └── Phổ giao thoa chéo S12                 │
        │  [Lưu trữ]  Ghi file HDF5 cuốn chiếu (15 phút/file)    │
        │  [Cloud]    rclone background sync -> Google Drive     │
        └──────────────────────────┬─────────────────────────────┘
                                   │
              [Direct LAN Cat6 STP / Local Phone Hotspot]
                      (Gói tin UDP Flat Binary)
                                   │
                                   ▼
        ┌────────────────────────────────────────────────────────┐
        │            WORKSTATION / LAPTOP (GUI Viewer)           │
        │                                                        │
        │  [Module 4] viewer.py (PyQtGraph)                      │
        │             ├── Đồ thị 3 phổ tức thời (S11, S22, S12)  │
        │             └── Thác nước Waterfall dải 30 - 40 MHz    │
        └────────────────────────────────────────────────────────┘
```
[cite: 1, 4]

---

## 📍 2. Hiện trạng Dự án & Lộ trình Thực hiện (Status & Roadmap)

### Phase 1: Lab Benchtop Instrumentation (ĐÃ HOÀN THÀNH)
* [x] **Khóa pha phần cứng (Hardware Phase-Locking):** Cấp nguồn xung chuẩn 24 MHz ngoài qua cổng `REFin` cho cả hai thiết bị RSPdx, triệt tiêu hoàn toàn hiện tượng trôi mẫu tương đối giữa 2 ADC[cite: 1, 2, 4].
* [x] **Thu nhận kép 10 MSPS (Module 1):** Kiến trúc đa tiến trình (`fork()`) với bộ đệm vòng không khóa (Lock-free SPSC Ring Buffer) trên POSIX Shared Memory (`/dev/shm`), duy trì thông lượng $80\text{ MB/s}$ không rớt gói (`reset_count = 0`)[cite: 1, 4].
* [x] **Căn chỉnh trễ mẫu khởi động (Module 2):** Đo tương quan chéo miền thời gian nhanh qua FFTW3 IFFT, xác định độ trễ khởi động USB $k_{\text{offset}}$ và tự động xả mẫu bù trễ về mốc $k = 0$ tuyệt đối ($\vert{}k\vert{} \le 1\text{ mẫu} \approx 100\text{ ns}$, $\text{PNR} \ge 20\text{ dB}$), giữ khóa ổn định liên tục qua bài test 60 giây[cite: 2, 4].

---

### Phase 2: Core DSP & Field Infrastructure (KẾ HOẠCH HIỆN TẠI)

#### A. Module 3: FX Correlator Engine & Tính toán 3 Phổ
* **F-Engine (Phân rã phổ):**
  * Chia dòng mẫu I/Q thành các khối $N = 2048\text{ điểm}$ ($\Delta f \approx 4.88\text{ kHz/bin}$)[cite: 1, 4].
  * Nhân cửa sổ Hanning chống rò rỉ phổ[cite: 1, 4].
  * Biến đổi Fourier thuận 1D qua `fftw3f` tối ưu tập lệnh vector ARM NEON trên Pi 5[cite: 1, 4].
* **X-Engine (Nhân tương quan & Tích lũy Welch):**
  * Tính phổ giao thoa chéo: $S_{12}(f) = X_1(f) \cdot X_2^*(f)$[cite: 1, 4].
  * **Tính thêm 2 phổ tự tương quan để chẩn đoán kênh:**
    * $S_{11}(f) = \vert{}X_1(f)\vert{}^2$ (Giám sát mức tín hiệu SDR 1)[cite: 1, 4]
    * $S_{22}(f) = \vert{}X_2(f)\vert{}^2$ (Giám sát mức tín hiệu SDR 2)[cite: 1, 4]
  * Tích phân thời gian $\tau = 200\text{ ms}$ ($M \approx 1953\text{ khung}$), nén lưu lượng dữ liệu từ $80\text{ MB/s}$ thô xuống còn $\approx 120\text{ KB/s}$[cite: 1, 4].

#### B. Module 4: Giao thức Mạng & Host GUI (PyQtGraph)
* **Cấu trúc gói tin UDP Telemetry (Non-blocking):**
  * Header 32 bytes (Magic "SOLR", Frame ID, Timestamp UTC micro-giây)[cite: 1, 4].
  * Payload chứa $3 \times 2048$ số thực float32: $[S_{11}, S_{22}, S_{12}]$ quy đổi sang thang đo dB[cite: 1, 4].
* **Viewer trên Laptop (`viewer.py`):**
  * Vẽ đồng thời 3 đường phổ tức thời: SDR 1 (Xanh lam), SDR 2 (Cam), Giao thoa chéo (Đỏ)[cite: 1, 4].
  * Biểu đồ thác nước Waterfall cuộn thời gian thực dải $30.0 - 40.0\text{ MHz}$ ở tốc độ 5 FPS[cite: 1, 4].

---

### Phase 3: Outdoor Science Deployment (KẾ HOẠCH TƯƠNG LAI)
* [ ] **Lắp đặt mảng ăng-ten:** Dựng cặp ăng-ten Dipole theo trục Đông - Tây (Baseline 10 – 15 m) tối ưu cho dải tần 30.0 – 40.0 MHz[cite: 1, 4].
* [ ] **Chuyển mạch hiệu chuẩn tự động:** Tích hợp công tắc SPDT RF Switch và nguồn tạp âm Avalanche qua chân GPIO để hiệu chuẩn pha vi sai tĩnh $\Delta\phi_0(f)$ tại chỗ[cite: 1, 4].
* [ ] **Quan trắc quá cảnh Mặt Trời:** Ghi nhận vân giao thoa (Fringes) và đối chiếu sự kiện bùng nổ vô tuyến với mạng lưới e-CALLISTO/vệ tinh GOES[cite: 1, 4].

---

## 🌐 3. Chiến lược Mạng & Debug Hiện trường (KHÔNG DÙNG TAILSCALE)

### Lý do kỹ thuật loại bỏ Tailscale và Web Dashboard:
1. **Không dùng Tailscale:** Header mã hóa của WireGuard làm giảm MTU, băm nhỏ gói tin UDP 8–24 KB gây rớt khung Waterfall khi qua Internet; đồng thời tiềm ẩn nguy cơ nghẽn relay DERP và ngốn thêm CPU trên Pi 5[cite: 1].
2. **Không dùng Web Dashboard:** Render thác nước 2048 cột ở tần số 5 Hz trên trình duyệt web gây lag và tốn tài nguyên; dùng `viewer.py` (PyQtGraph) tận dụng trực tiếp GPU máy trạm, giúp CPU Pi 5 hoàn toàn giải phóng cho tác vụ DSP[cite: 1, 4].

### Phương án kết nối tại trạm đo:

| Kịch bản | Phương thức kết nối | Đặc tính kỹ thuật |
| :--- | :--- | :--- |
| **Debug trực tiếp tại bãi đo (Khuyên dùng)** | **Cáp mạng LAN Cat6 STP cắm trực tiếp (PC $\leftrightarrow$ Pi 5)**[cite: 1, 4] | • Tự nhận IP Link-local (`169.254.x.x`)[cite: 1]<br>• SSH: `ssh radi02@raspberry1.local`[cite: 1]<br>• Rớt gói UDP bằng $0\%$, độ trễ $< 1\text{ ms}$[cite: 1]<br>• Cáp bọc giáp STP triệt tiêu hoàn toàn nhiễu RFI vào dải 35 MHz[cite: 1, 4] |
| **Kết nối không dây ngoài bãi** | **Wi-Fi Hotspot nội bộ từ điện thoại / Laptop**[cite: 1] | • Pi 5 và PC cùng kết nối vào Hotspot cá nhân[cite: 1]<br>• Không bị chặn port 22 (SSH), không bị AP Isolation[cite: 1]<br>• Hoạt động độc lập không phụ thuộc sóng 4G/Internet |

### Quản lý phiên làm việc ngoài bãi:
* Thực thi tiến trình trong **`tmux`** để tránh trường hợp đứt kết nối mạng làm tắt đột ngột tiến trình C++[cite: 2]:
  ```bash
  tmux new -s solar_run
  ./build/solar_correlator
  # Nhấn Ctrl + B, rồi nhấn D để tách phiên chạy ngầm
  ```
 [cite: 2]

---

## 💾 4. Giải pháp Lưu trữ & Đồng bộ Đám mây (Storage Pipeline)

### Nguyên tắc an toàn lưu trữ:
* **TUYỆT ĐỐI KHÔNG ghi dữ liệu thô I/Q ($80\text{ MB/s}$) vào thẻ MicroSD:** Chỉ lưu mảng phổ đã tích phân ($\approx 40 - 65\text{ KB/s}$)[cite: 1, 4].
* **Độ bền thẻ nhớ:** Dùng thẻ MicroSD High Endurance[cite: 4]. Tốc độ sinh dữ liệu $\approx 235\text{ MB/giờ} \approx 2.8\text{ GB/ngày}$ (cho 12 giờ đo), thẻ 32–64 GB đủ đệm an toàn từ 5 đến 15 ngày[cite: 4].

### Cơ chế chia file cuốn chiếu & Tải lên Google Drive:
1. **File Rolling 15 phút (C++ Engine):**
   * Phần mềm tự động đóng file cũ và tạo file mới sau mỗi 15 phút theo định dạng: `solar_vis_YYYYMMDD_HHMMSS.h5`[cite: 4].
   * Kích thước mỗi file: $\approx 35 - 50\text{ MB}$ (an toàn khi mất nguồn đột ngột, chỉ mất tối đa 15 phút hiện tại)[cite: 1, 4].
   * Cấu trúc HDF5 chuẩn:
     * `/data/visibilities_real`: Ma trận phần thực $\text{Re}(S_{12})$[cite: 1, 4]
     * `/data/visibilities_imag`: Ma trận phần ảo $\text{Im}(S_{12})$[cite: 1, 4]
     * `/data/auto_power_ch1`: Phổ tự tương quan $S_{11}$[cite: 1, 4]
     * `/data/auto_power_ch2`: Phổ tự tương quan $S_{22}$[cite: 1, 4]
     * `/data/timestamps`: Mốc thời gian chuẩn UTC (micro-giây)[cite: 1, 4]
2. **Đồng bộ tự động qua `rclone` (Non-blocking):**
   * Tác vụ đẩy lên Drive chạy độc lập qua tiến trình riêng, không gọi trực tiếp trong mã C++[cite: 1, 4].
   * File script `sync_drive.sh`:
     ```bash
     #!/bin/bash
     /usr/bin/rclone copy /home/radi02/solar_data/ gdrive:SolarInterferometer_Data/ \
         --include "*.h5" \
         --min-age 1m \
         --transfers 1 \
         --log-file /home/radi02/solar_data/rclone.log
     ```
    [cite: 1]
   * Lập lịch tự động bằng `crontab -e`:
     ```cron
     */15 * * * * /bin/bash /home/radi02/sync_drive.sh
     ```
    [cite: 1]
3. **Chính sách dọn dẹp xoay vòng (Circular Purge):**
   * Tự động xóa các file cục bộ cũ hơn 5 ngày trên thẻ nhớ MicroSD lúc 02:00 AM hàng ngày để thẻ không bao giờ bị đầy[cite: 1, 4].

---

## 📚 5. Bản đồ Liên kết Tài liệu (Documentation Map)

Cấu trúc chi tiết nằm trong thư mục `docs/` được xuất bản qua GitHub Pages[cite: 1]:

* **Phần cứng & RF Front-End (`docs/01-hardware/`):**
  * [Thiết kế Ăng-ten & Cáp RF Port C BNC](docs/01-hardware/rf-frontend.md)[cite: 1]
  * [Khóa pha đồng bộ 24 MHz REFin](docs/01-hardware/clock-sync.md)[cite: 1, 2]
  * [Mạch nguồn tạp âm Avalanche & SPDT Switch](docs/01-hardware/rf-switching.md)[cite: 1, 2]
  * [Kết nối Raspberry Pi 5 & Cổng USB 3.0](docs/01-hardware/host-interconnect.md)[cite: 1]
* **Firmware & DSP Engine (`docs/02-firmware-dsp/`):**
  * [Kiến trúc Đa tiến trình & POSIX SHM Ring Buffer](docs/02-firmware-dsp/architecture.md)[cite: 1, 2]
  * [Module 1: Driver Ingestion 10 MSPS kép](docs/02-firmware-dsp/module1-driver.md)[cite: 1]
  * [Module 2: Khóa bù trễ mẫu thời gian thực k=0](docs/02-firmware-dsp/module2-time-align.md)[cite: 1, 2]
  * [Module 3: Lõi FX Correlator & Welch Integration](docs/02-firmware-dsp/module3-fx-correlator.md)[cite: 1, 4]
  * [Module 4: Giao thức UDP Telemetry & PyQtGraph Viewer](docs/02-firmware-dsp/module4-telemetry.md)[cite: 1, 4]
* **Đo kiểm & Nhật ký gỡ lỗi (`docs/03-calibration-tests/`):**
  * [Kiểm chuẩn Bàn đo Zero-Baseline trong Lab](docs/03-calibration-tests/lab-bench-test.md)[cite: 1, 2]
  * [Nhật ký Gỡ lỗi Thực nghiệm (Debug Chronicles)](docs/03-calibration-tests/debug-chronicles.md)[cite: 1, 2]

---

## 🛠️ 6. Hướng dẫn Vận hành Nhanh (Quick Start)

### 1. Khởi chạy trên Raspberry Pi 5 (Backend Edge DSP)
```bash
# Biên dịch hệ thống
cmake --build build -j4

# Chạy kiểm tra căn chỉnh trễ mẫu Module 2
./build/alignment_test

# Khởi chạy luồng đo chính trong tmux
tmux new -s solar_daemon
./build/solar_correlator
```
[cite: 1, 2, 4]

### 2. Khởi chạy trên Máy tính Giám sát (Host PC GUI)
```bash
# Cài đặt thư viện phụ thuộc
pip install pyqtgraph PyQt5 numpy

# Khởi chạy giao diện hiển thị 3 phổ và Waterfall
python viewer/viewer.py
```
[cite: 1, 4]