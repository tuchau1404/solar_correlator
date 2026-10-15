#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
==============================================================================
Solar Correlator - Real-Time Spectrum & Waterfall Visualization Client
Host Platform: Workstation / Laptop (Windows, Linux, macOS)
Backend: Raspberry Pi 5 (FX Correlator via UDP Telemetry)
==============================================================================
"""

import socket
import struct
import time
import numpy as np
import pyqtgraph as pg
from PyQt5 import QtWidgets, QtCore, QtGui

# ============================================================================
# CẤU HÌNH TRUYỀN NHẬN VÀ THAM SỐ GIAO THOA
# ============================================================================
UDP_IP = "0.0.0.0"          # Lắng nghe trên mọi giao diện mạng của PC
UDP_PORT = 9999             # Khớp với SolarConfig::Network::UDP_DEST_PORT
FFT_SIZE = 2048             # Số bin tần số F-Engine
FREQ_START_MHZ = 30.0       # Tần số biên dưới
FREQ_STOP_MHZ = 40.0        # Tần số biên trên
WATERFALL_HISTORY = 300     # Số dòng lịch sử lưu trữ (tương đương 60 giây ở tau = 200 ms)

# Cấu trúc Header nhị phân 28 bytes (#pragma pack(1)):
# Magic "SOLR" (4s), FrameID (Q: uint64), Timestamp (Q: uint64),
# IntegrationFrames (I: uint32), PayloadFlags (I: uint32)
HEADER_FORMAT = "<4s Q Q I I"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)  # 28 bytes
EXPECTED_PACKET_SIZE = HEADER_SIZE + (FFT_SIZE * 4)  # 8220 bytes

# ============================================================================
# THANG ĐO NĂNG LƯỢNG ĐỒNG BỘ CHUẨN TOÁN ASTRA (ADC-count^2/bin)
# ============================================================================
SPECTRUM_Y_MIN = 10.0       # Sàn hiển thị phổ tức thời (dB)
SPECTRUM_Y_MAX = 95.0       # Đỉnh hiển thị phổ tức thời (dB)
WATERFALL_LEVEL_MIN = 20.0  # Mức sàn thác nước (Màu tím/đen)
WATERFALL_LEVEL_MAX = 85.0  # Mức đỉnh thác nước (Màu vàng rực/trắng)

# ============================================================================
# KHỞI TẠO SOCKET UDP NON-BLOCKING (BỘ ĐỆM 2 MB)
# ============================================================================
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
# Tăng bộ đệm socket lên 2 MB để chống tràn hàng đợi khi hệ điều hành bận
try:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * 1024 * 1024)
except Exception as e:
    print(f"[CẢNH BÁO] Không thể đặt SO_RCVBUF: {e}")

sock.bind((UDP_IP, UDP_PORT))
sock.setblocking(False)

# ============================================================================
# KHỞI TẠO GIAO DIỆN ĐỒ HỌA PYQTGRAPH
# ============================================================================
app = QtWidgets.QApplication([])
# Tối ưu hóa rendering đồ họa nền OpenGL/GPU
pg.setConfigOptions(antialias=True, imageAxisOrder='row-major')

win = pg.GraphicsLayoutWidget(show=True, title="Solar Correlator - Real-time Waterfall Viewer")
win.resize(1150, 800)

freq_axis = np.linspace(FREQ_START_MHZ, FREQ_STOP_MHZ, FFT_SIZE, endpoint=False)
waterfall_data = np.full((WATERFALL_HISTORY, FFT_SIZE), WATERFALL_LEVEL_MIN, dtype=np.float32)

# ----------------------------------------------------------------------------
# 1. ĐỒ THỊ PHỔ CÔNG SUẤT TỨC THỜI S12 (NỬA TRÊN)
# ----------------------------------------------------------------------------
p1 = win.addPlot(row=0, col=0, title="Phổ công suất tương quan |S₁₂| (30.0 – 40.0 MHz)")
p1.setLabel('bottom', "Tần số", units='MHz')
p1.setLabel('left', "Công suất phổ", units='dB')
p1.getAxis('bottom').enableAutoSIPrefix(False)  # Khóa tiền tố SI tự động để không hiện kMHz
p1.setXRange(FREQ_START_MHZ, FREQ_STOP_MHZ, padding=0)
p1.setYRange(SPECTRUM_Y_MIN, SPECTRUM_Y_MAX)
p1.showGrid(x=True, y=True, alpha=0.3)
curve = p1.plot(pen=pg.mkPen(color=(255, 220, 50), width=1.5))

# ----------------------------------------------------------------------------
# 2. THÁC NƯỚC PHỔ CUỘN WATERFALL (NỬA DƯỚI)
# ----------------------------------------------------------------------------
p2 = win.addPlot(row=1, col=0, title="Thác nước quan trắc thời gian thực (60s gần nhất)")
p2.setLabel('bottom', "Tần số", units='MHz')
p2.setLabel('left', "Lịch sử khung thời gian (Frames)")
p2.getAxis('bottom').enableAutoSIPrefix(False)
p2.setXRange(FREQ_START_MHZ, FREQ_STOP_MHZ, padding=0)
p2.setYRange(0, WATERFALL_HISTORY, padding=0)

img = pg.ImageItem()
p2.addItem(img)

# Căn tọa độ hình học 2D: Ánh xạ 2048 bin điểm ảnh phủ đúng dải 30.0 - 40.0 MHz
tr = QtGui.QTransform()
tr.translate(FREQ_START_MHZ, 0)
tr.scale((FREQ_STOP_MHZ - FREQ_START_MHZ) / FFT_SIZE, 1.0)
img.setTransform(tr)

# Cấu hình Bảng màu Inferno độ tương phản cao cho thiên văn
colormap = pg.colormap.get('inferno')
img.setColorMap(colormap)
img.setLevels([WATERFALL_LEVEL_MIN, WATERFALL_LEVEL_MAX])

# ----------------------------------------------------------------------------
# 3. COLORBAR TƯƠNG TÁC (HISTOGRAM / DẢI MÀU BÊN PHẢI)
# ----------------------------------------------------------------------------
hist = pg.HistogramLUTItem()
hist.setImageItem(img)
hist.gradient.setColorMap(colormap)
hist.setLevels(WATERFALL_LEVEL_MIN, WATERFALL_LEVEL_MAX)
win.addItem(hist, row=1, col=1)

# ============================================================================
# VÒNG LẶP HÚT DỮ LIỆU SOCKET & CẬP NHẬT ĐỒ HỌA
# ============================================================================
last_frame_count = 0
dropped_packets_detected = 0

def update_gui():
    global waterfall_data, last_frame_count, dropped_packets_detected
    latest_spectrum = None
    latest_frame_id = 0
    latest_m_frames = 0
    packets_read = 0

    # Hút cạn hàng đợi UDP để triệt tiêu độ trễ hiển thị
    try:
        while True:
            data, _ = sock.recvfrom(65535)
            if len(data) == EXPECTED_PACKET_SIZE:
                magic, frame_id, ts_us, m_frames, flags = struct.unpack_from(HEADER_FORMAT, data, 0)
                if magic == b"SOLR":
                    packets_read += 1
                    
                    # Phát hiện rớt gói mạng dựa trên Frame ID đơn điệu
                    if last_frame_count > 0 and frame_id > (last_frame_count + 1):
                        dropped_packets_detected += (frame_id - last_frame_count - 1)
                    last_frame_count = frame_id

                    # Giải nén mảng phổ 2048 số thực Float32
                    spectrum = np.frombuffer(data[HEADER_SIZE:], dtype=np.float32)

                    # Cuộn dữ liệu thác nước: đẩy dòng cũ lên trên, nạp dòng mới vào đáy
                    waterfall_data = np.roll(waterfall_data, -1, axis=0)
                    waterfall_data[-1, :] = spectrum

                    latest_spectrum = spectrum
                    latest_frame_id = frame_id
                    latest_m_frames = m_frames
    except BlockingIOError:
        pass  # Đã đọc sạch toàn bộ gói tin đang chờ trong socket buffer

    # Cập nhật đồ họa khi có khung dữ liệu mới
    if latest_spectrum is not None:
        # Cập nhật đường phổ 1D
        curve.setData(freq_axis, latest_spectrum)

        # Cập nhật ảnh thác nước 2D (chuyển vị để trục X là tần số, trục Y là thời gian)
        img.setImage(waterfall_data.T, autoLevels=False)

        # Dò đỉnh phổ tức thời
        peak_idx = int(np.argmax(latest_spectrum))
        peak_db = float(latest_spectrum[peak_idx])
        peak_freq = FREQ_START_MHZ + peak_idx * ((FREQ_STOP_MHZ - FREQ_START_MHZ) / FFT_SIZE)

        # Cập nhật thanh tiêu đề giám sát trạng thái
        drop_info = f" | Rớt gói: {dropped_packets_detected}" if dropped_packets_detected > 0 else ""
        p1.setTitle(f"Phổ S₁₂ tức thời | Khung #{latest_frame_id} (M={latest_m_frames}) | "
                    f"Đỉnh: {peak_db:5.1f} dB @ {peak_freq:6.3f} MHz{drop_info}")

# Kích hoạt Timer quét giao diện mỗi 25 ms (40 FPS, vượt tốc độ 5 Hz của Pi 5)
timer = QtCore.QTimer()
timer.timeout.connect(update_gui)
timer.start(25)

# ============================================================================
# ĐIỂM VÀO CHƯƠNG TRÌNH
# ============================================================================
if __name__ == '__main__':
    print("=" * 70)
    print(" SOLAR CORRELATOR - WORKSTATION REAL-TIME VIEWER")
    print(f" Lắng nghe UDP tại: {UDP_IP}:{UDP_PORT}")
    print(f" Dải tần quan trắc : {FREQ_START_MHZ} – {FREQ_STOP_MHZ} MHz ({FFT_SIZE} bins)")
    print(f" Ngưỡng hiển thị phổ: {SPECTRUM_Y_MIN} đến {SPECTRUM_Y_MAX} dB (ADC-count²/bin)")
    print("=" * 70)
    app.exec_()