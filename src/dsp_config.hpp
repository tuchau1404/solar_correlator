#pragma once
#include <cstdint>
namespace SolarConfig {
namespace Sampling { constexpr double RATE_HZ = 10000000.0; }
namespace Correlator {
    constexpr uint32_t FFT_SIZE                        = 2048;                   // Phân giải ~4.88 kHz/bin
    constexpr uint32_t HOP_SIZE                        = FFT_SIZE / 2;           // 1024 mẫu (Gối đầu 50% Welch's method)
    constexpr double INTEGRATION_TIME_SEC              = 0.2;                    // 200 ms
    // Số khung tích phân (M = 10,000,000 * 0.2 / 1024 = 1953)
    constexpr uint32_t INTEGRATION_FRAMES              = static_cast<uint32_t>((Sampling::RATE_HZ * INTEGRATION_TIME_SEC) / HOP_SIZE);
    constexpr float DYNAMIC_RANGE_EPS                  = 1e-12f;                 // Chống log10(0)
}

}
