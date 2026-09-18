#include <iostream>
#include <vector>
#include <cmath>
#include <atomic>
#include <thread>
#include <chrono>
#include <algorithm>
#include <iomanip>
#include "config.hpp"
#include "ring_buffer.hpp"
#include "correlator.hpp"

static std::atomic<bool> g_feeder_running{true};

// Luồng độc lập bơm dữ liệu sóng sin 1.0 MHz liên tục vào 2 bộ đệm (giả lập 2 kênh SDR)
void feeder_worker(SharedMemoryRingBuffer* rb1, SharedMemoryRingBuffer* rb2) {
    const uint32_t CHUNK = 2048;
    std::vector<int16_t> xi(CHUNK), xq(CHUNK);
    uint64_t sample_idx = 0;

    while (g_feeder_running.load(std::memory_order_relaxed)) {
        // Nếu bộ đệm còn khoảng trống, tiếp tục bơm mẫu
        if (rb1->available_read() < (SolarConfig::SHM::BUFFER_CAPACITY / 2)) {
            for (uint32_t n = 0; n < CHUNK; ++n) {
                // Sóng sin 1.0 MHz trên tốc độ lấy mẫu 10 MSPS (chu kỳ 10 mẫu)
                double phase = 2.0 * M_PI * 1000000.0 * (sample_idx + n) / SolarConfig::SDR::SAMPLE_RATE_HZ;
                xi[n] = static_cast<int16_t>(10000.0 * std::cos(phase));
                xq[n] = static_cast<int16_t>(10000.0 * std::sin(phase));
            }
            sample_idx += CHUNK;

            // Bơm cùng một tín hiệu đồng pha vào cả 2 kênh
            rb1->write(xi.data(), xq.data(), CHUNK);
            rb2->write(xi.data(), xq.data(), CHUNK);
        } else {
            std::this_thread::yield();
        }
    }
}

int main() {
    std::cout << ">>> [TEST 2] KIỂM TRA THUẬT TOÁN FX CORRELATOR & WELCH METHOD <<<\n";

    SharedMemoryRingBuffer::unlink_shm("/shm_test_ch1");
    SharedMemoryRingBuffer::unlink_shm("/shm_test_ch2");

    SharedMemoryRingBuffer rb1("/shm_test_ch1", SolarConfig::SHM::BUFFER_CAPACITY, true);
    SharedMemoryRingBuffer rb2("/shm_test_ch2", SolarConfig::SHM::BUFFER_CAPACITY, true);

    // 1. Khởi chạy luồng bơm dữ liệu nhân tạo
    std::thread feeder(feeder_worker, &rb1, &rb2);

    // 2. Khởi tạo FX Correlator Engine
    FXCorrelator correlator;
    correlator.prime_buffers(rb1, rb2);

    std::vector<float> spec_db(SolarConfig::Correlator::FFT_SIZE);
    std::vector<float> phase_rad(SolarConfig::Correlator::FFT_SIZE);
    std::vector<float> coh(SolarConfig::Correlator::FFT_SIZE);
    std::vector<float> fringe(SolarConfig::Correlator::FFT_SIZE, 0.0f);

    std::cout << "[DSP] Đang xử lý tích phân trọn vẹn " 
              << SolarConfig::Correlator::INTEGRATION_FRAMES << " khung FFT (Welch 50% Overlap)...\n";

    auto t1 = std::chrono::steady_clock::now();
    
    // 3. Thực thi tính toán toàn bộ 1953 khung
    correlator.process_integration_cycle(rb1, rb2, fringe, spec_db, phase_rad, coh);

    auto t2 = std::chrono::steady_clock::now();
    double calc_ms = std::chrono::duration<double, std::milli>(t2 - t1).count();

    // 4. Dừng luồng bơm
    g_feeder_running.store(false);
    feeder.join();

    SharedMemoryRingBuffer::unlink_shm("/shm_test_ch1");
    SharedMemoryRingBuffer::unlink_shm("/shm_test_ch2");

    // 5. Thẩm định kết quả toán học
    auto max_it = std::max_element(spec_db.begin(), spec_db.end());
    int peak_bin = std::distance(spec_db.begin(), max_it);
    float peak_coh = coh[peak_bin];
    float peak_phase = phase_rad[peak_bin];

    // Tần số tương ứng: bin 1024 = 35.0 MHz -> bin ~1229 tương ứng 36.0 MHz (35.0 + 1.0 MHz)
    float peak_freq = 30.0f + peak_bin * (10.0f / SolarConfig::Correlator::FFT_SIZE);

    std::cout << "\n--- KẾT QUẢ KIỂM TRA TOÁN HỌC DSP ---\n";
    std::cout << " + Thời gian tính 1953 khung  : " << std::fixed << std::setprecision(2) << calc_ms << " ms\n";
    std::cout << " + Vị trí đỉnh phổ (Peak Bin) : " << peak_bin << " (" << peak_freq << " MHz)\n";
    std::cout << " + Công suất cực đại          : " << *max_it << " dB\n";
    std::cout << " + Độ lệch pha tại đỉnh       : " << peak_phase << " rad (Kỳ vọng: 0.0 rad)\n";
    std::cout << " + Độ kết hợp (Coherence)     : " << std::setprecision(5) << peak_coh << " (Kỳ vọng: 1.00000)\n";

    if (peak_coh > 0.98f && std::abs(peak_phase) < 0.05f) {
        std::cout << "\n\033[32m[PASS] TOÁN HỌC FX CORRELATOR, FFTW3 NEON & PHƯƠNG PHÁP WELCH CHÍNH XÁC HOÀN TOÀN!\033[0m\n";
        return 0;
    } else {
        std::cout << "\n\033[31m[FAIL] SAI LỆCH TOÁN HỌC TRONG PHÉP TÍNH TƯƠNG QUAN CHÉO!\033[0m\n";
        return -1;
    }
}