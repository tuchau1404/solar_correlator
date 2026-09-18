import socket
import struct
import numpy as np
import pyqtgraph as pg
from PyQt5 import QtWidgets, QtCore, QtGui

# ============================================================================
# CẤU HÌNH THÔNG SỐ TRUYỀN NHẬN & HIỂN THỊ
# ============================================================================
UDP_IP = "0.0.0.0"          # Đón nhận từ mọi card mạng PC
UDP_PORT = 9999             # Khớp với SolarConfig::Network::UDP_DEST_PORT
FFT_SIZE = 2048             # Số bin tần số
FREQ_START_MHZ = 30.0       # Tần số bắt đầu
FREQ_STOP_MHZ = 40.0        # Tần số kết thúc
WATERFALL_HISTORY = 300     # Số dòng lịch sử thời gian

HEADER_FORMAT = "<4s Q Q I I"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
EXPECTED_PACKET_SIZE = HEADER_SIZE + (FFT_SIZE * 4)  # 8220 bytes

# Khởi tạo Socket UDP Non-blocking với bộ đệm 2 MB
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * 1024 * 1024)
sock.bind((UDP_IP, UDP_PORT))
sock.setblocking(False)

# Khởi tạo GUI
app = QtWidgets.QApplication([])
win = pg.GraphicsLayoutWidget(show=True, title="Solar Correlator - Real-time Waterfall Viewer")
win.resize(1100, 750)

freq_axis = np.linspace(FREQ_START_MHZ, FREQ_STOP_MHZ, FFT_SIZE)
waterfall_data = np.full((WATERFALL_HISTORY, FFT_SIZE), -60.0, dtype=np.float32)

# 1. ĐỒ THỊ PHỔ TỨC THỜI (NỬA TRÊN)
p1 = win.addPlot(title="Phổ công suất tương quan tức thời S12 (30.0 - 40.0 MHz)")
p1.setLabel('bottom', "Tần số", units='MHz')
p1.setLabel('left', "Công suất", units='dB')
p1.getAxis('bottom').enableAutoSIPrefix(False)
p1.setXRange(FREQ_START_MHZ, FREQ_STOP_MHZ, padding=0)
p1.setYRange(-70.0, 5.0)
p1.showGrid(x=True, y=True, alpha=0.3)
curve = p1.plot(pen=pg.mkPen(color='y', width=1.5))

win.nextRow()

# 2. THÁC NƯỚC PHỔ CUỘN (NỬA DƯỚI)
p2 = win.addPlot(title="Thác nước phổ Waterfall (30.0 - 40.0 MHz)")
p2.setLabel('bottom', "Tần số", units='MHz')
p2.setLabel('left', "Khung thời gian trôi (Frames)")
p2.getAxis('bottom').enableAutoSIPrefix(False)
p2.setXRange(FREQ_START_MHZ, FREQ_STOP_MHZ, padding=0)
p2.setYRange(0, WATERFALL_HISTORY, padding=0)

img = pg.ImageItem()
p2.addItem(img)

# --- SỬA LỖI TỌA ĐỘ BẰNG MA TRẬN BIẾN ĐỔI CHUẨN ---
# Co giãn 2048 pixel ảnh thành dải tần đúng 10.0 MHz (từ 30.0 đến 40.0)
tr = QtGui.QTransform()
tr.translate(FREQ_START_MHZ, 0)
tr.scale((FREQ_STOP_MHZ - FREQ_START_MHZ) / FFT_SIZE, 1.0)
img.setTransform(tr)

# Thiết lập bảng màu Inferno và ngưỡng hiển thị
colormap = pg.colormap.get('inferno')
img.setColorMap(colormap)
img.setLevels([-65.0, -5.0])

# VÒNG LẶP CẬP NHẬT DỮ LIỆU
def update():
    global waterfall_data
    latest_spectrum = None
    latest_frame_id = 0

    try:
        while True:
            data, _ = sock.recvfrom(65535)
            if len(data) == EXPECTED_PACKET_SIZE:
                magic, frame_id, ts_us, m_frames, flags = struct.unpack_from(HEADER_FORMAT, data, 0)
                if magic.decode('ascii', errors='ignore') == "SOLR":
                    spectrum = np.frombuffer(data[HEADER_SIZE:], dtype=np.float32)
                    
                    waterfall_data = np.roll(waterfall_data, -1, axis=0)
                    waterfall_data[-1, :] = spectrum
                    
                    latest_spectrum = spectrum
                    latest_frame_id = frame_id
    except BlockingIOError:
        pass

    if latest_spectrum is not None:
        curve.setData(freq_axis, latest_spectrum)
        # Nạp dữ liệu dạng chuyển vị để trục X là tần số (2048 bin), trục Y là thời gian (300 dòng)
        img.setImage(waterfall_data.T, autoLevels=False)
        
        peak_idx = int(np.argmax(latest_spectrum))
        peak_db = float(latest_spectrum[peak_idx])
        peak_freq = FREQ_START_MHZ + peak_idx * ((FREQ_STOP_MHZ - FREQ_START_MHZ) / FFT_SIZE)
        p1.setTitle(f"Phổ S12 tức thời | Khung #{latest_frame_id} | "
                    f"Đỉnh: {peak_db:5.1f} dB @ {peak_freq:6.3f} MHz")

timer = QtCore.QTimer()
timer.timeout.connect(update)
timer.start(25)

if __name__ == '__main__':
    app.exec_()