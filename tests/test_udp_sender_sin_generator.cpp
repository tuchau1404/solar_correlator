#include <iostream>
#include <vector>
#include <chrono>
#include <thread>
#include <cmath>
#include "../src/telemetry_sender.hpp"

int main() {
    std::cout << "[Test 1] Đang phát gói tin UDP giả lập về: " 
              << SolarConfig::Network::UDP_DEST_IP << ":" 
              << SolarConfig::Network::UDP_DEST_PORT << "\n";

    TelemetrySender sender(SolarConfig::Network::UDP_DEST_IP, SolarConfig::Network::UDP_DEST_PORT);
    std::vector<float> fake_spectrum(SolarConfig::Correlator::FFT_SIZE, -60.0f); // Sàn nhiễu -60 dB

    uint64_t frame_id = 0;
    while (true) {
        // Tạo một đỉnh sóng di chuyển qua lại để kiểm tra đồ thị trên PC
        int peak_bin = 500 + static_cast<int>(300 * std::sin(frame_id * 0.1));
        std::fill(fake_spectrum.begin(), fake_spectrum.end(), -60.0f);
        fake_spectrum[peak_bin] = -10.0f; // Đỉnh sóng -10 dB

        auto now = std::chrono::system_clock::now();
        uint64_t ts_us = std::chrono::duration_cast<std::chrono::microseconds>(now.time_since_epoch()).count();

        sender.send_spectrum(frame_id, ts_us, fake_spectrum);
        std::cout << "-> Đã bắn khung #" << frame_id << " (Peak tại bin " << peak_bin << ")\n";

        frame_id++;
        std::this_thread::sleep_for(std::chrono::milliseconds(200)); // Nhịp 5 Hz chuẩn
    }
    return 0;
}