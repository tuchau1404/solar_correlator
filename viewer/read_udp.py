import socket
import struct
import numpy as np
import datetime

UDP_IP = "0.0.0.0"      # Lắng nghe trên mọi card mạng của PC
UDP_PORT = 9999         # Khớp với SolarConfig::Network::UDP_DEST_PORT trên Pi 5

# Cấu trúc Header: 4s (magic), Q (uint64 frame_id), Q (uint64 timestamp_us),
#                  I (uint32 integration_frames), I (uint32 payload_flags)
HEADER_FORMAT = "<4s Q Q I I"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)  # 28 bytes
EXPECTED_PAYLOAD_SAMPLES = 2048
EXPECTED_PACKET_SIZE = HEADER_SIZE + EXPECTED_PAYLOAD_SAMPLES * 4  # 8220 bytes

def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * 1024 * 1024) # Đệm 2 M
    sock.bind((UDP_IP, UDP_PORT))
    
    print(f"[UDP Receiver] Đang chờ gói tin từ Pi 5 tại cổng {UDP_PORT}...")
    
    last_frame_id = None
    
    while True:
        data, addr = sock.recvfrom(65535)
        
        # 1. Kiểm tra kích thước gói
        if len(data) != EXPECTED_PACKET_SIZE:
            print(f"[Cảnh báo] Kích thước gói sai lệch: {len(data)} bytes (kỳ vọng {EXPECTED_PACKET_SIZE})")
            continue
            
        # 2. Giải mã Header
        magic, frame_id, ts_us, m_frames, flags = struct.unpack_from(HEADER_FORMAT, data, 0)
        magic_str = magic.decode("ascii", errors="ignore")
        
        if magic_str != "SOLR":
            print(f"[Lỗi] Sai Magic Header: {magic_str}")
            continue
            
        # 3. Kiểm tra rớt gói (Drop packet)
        drop_warn = ""
        if last_frame_id is not None and frame_id != last_frame_id + 1:
            dropped = frame_id - last_frame_id - 1
            drop_warn = f" | \033[31m[RỚT {dropped} GÓI]\033[0m"
        last_frame_id = frame_id
        
        # 4. Giải mã Payload sang mảng float32 NumPy
        spectrum_db = np.frombuffer(data[HEADER_SIZE:], dtype=np.float32)
        
        # 5. Phân tích tham số phổ
        peak_idx = int(np.argmax(spectrum_db))
        peak_db = float(spectrum_db[peak_idx])
        min_db = float(np.min(spectrum_db))
        
        # Tần số tương ứng: bin 0 -> 30 MHz, bin 1024 -> 35 MHz, bin 2047 -> ~40 MHz
        peak_freq_mhz = 30.0 + peak_idx * (10.0 / 2048.0)
        
        # Chuyển đổi timestamp UTC
        dt_utc = datetime.datetime.fromtimestamp(ts_us / 1e6, tz=datetime.timezone.utc)
        time_str = dt_utc.strftime("%H:%M:%S.%f")[:-3]
        
        print(f"[{time_str}] Khung #{frame_id:<6} | M={m_frames} | "
              f"Đỉnh: {peak_db:6.1f} dB @ {peak_freq_mhz:6.3f} MHz (Sàn: {min_db:5.1f} dB){drop_warn}")

if __name__ == "__main__":
    main()