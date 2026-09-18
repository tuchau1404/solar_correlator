#include <iostream>
#include <vector>
#include <chrono>
#include <thread>
#include <cmath>
#include <algorithm>
#include <iomanip>
#include "../src/telemetry_sender.hpp"

int main() {
    std::cout << "[Test 1 - Dynamic dB] Đang phát gói tin giả lập biến thiên dB về: " 
              << SolarConfig::Network::UDP_DEST_IP << ":" 
              << SolarConfig::Network::UDP_DEST_PORT << "\n";

    TelemetrySender sender(SolarConfig::Network::UDP_DEST_IP, SolarConfig::Network::UDP_DEST_PORT);
    std::vector<float> fake_spectrum(SolarConfig::Correlator::FFT_SIZE, -60.0f);

    uint64_t frame_id = 0;
    while (true) {
        // 1. Giả lập tần số quét trôi qua lại quanh vùng trung tâm (bin 700 -> 1300)
        int peak_bin = 1024 + static_cast<int>(300 * std::sin(frame_id * 0.05));

        // 2. Giả lập công suất dB thay đổi tuần hoàn từ -55 dB đến -5 dB
        // Chu kỳ biến thiên biên độ: ~80 khung hình (~16 giây)
        float peak_db = -30.0f + 25.0f * std::sin(frame_id * 0.08f);

        // 3. Reset mảng về sàn nhiễu -60 dB (kèm dao động ngẫu nhiên nhẹ 0.5 dB)
        for (size_t i = 0; i < fake_spectrum.size(); ++i) {
            float noise_jitter = (std::rand() % 100) / 200.0f; // Nhiễu 0 -> 0.5 dB
            fake_spectrum[i] = -60.0f + noise_jitter;
        }

        // 4. Tạo búp sóng có độ rộng thực tế (lan tỏa +/- 3 bin xung quanh đỉnh)
        for (int offset = -3; offset <= 3; ++offset) {
            int bin = peak_bin + offset;
            if (bin >= 0 && bin < static_cast<int>(SolarConfig::Correlator::FFT_SIZE)) {
                float attenuation = std::abs(offset) * 6.0f; // Suy hao 6 dB mỗi bin xa đỉnh
                fake_spectrum[bin] = std::max(-60.0f, peak_db - attenuation);
            }
        }

        // 5. Gửi gói tin qua UDP
        auto now = std::chrono::system_clock::now();
        uint64_t ts_us = std::chrono::duration_cast<std::chrono::microseconds>(now.time_since_epoch()).count();
        sender.send_spectrum(frame_id, ts_us, fake_spectrum);

        // In log kiểm tra giá trị dB phát ra
        float freq_mhz = 30.0f + peak_bin * (10.0f / SolarConfig::Correlator::FFT_SIZE);
        std::cout << "-> Khung #" << std::setw(5) << frame_id 
                  << " | Đỉnh: " << std::setw(5) << std::fixed << std::setprecision(1) << peak_db << " dB"
                  << " @ " << std::setw(6) << std::setprecision(3) << freq_mhz << " MHz\n";

        frame_id++;
        std::this_thread::sleep_for(std::chrono::milliseconds(100)); // Chu kỳ 5 Hz chuẩn
    }
    return 0;
}