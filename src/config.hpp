#pragma once

#include <cstdint>
#include <string>
#include "sdrplay_api.h"
#include "dsp_config.hpp"
#include "network_config.hpp"

namespace SolarConfig {

namespace SDR {
    // Tần số và bộ lọc băng thông
    constexpr double RF_CENTER_FREQ_HZ                      = 35000000.0;           // 35.0 MHz (Phủ dải 30-40 MHz)
    constexpr sdrplay_api_Bw_MHzT IF_BW                     = sdrplay_api_BW_8_000; // 8.0 MHz Analog Filter
    constexpr double SAMPLE_RATE_HZ                         = Sampling::RATE_HZ;           // 10.0 MSPS (100 ns/mẫu)
    constexpr sdrplay_api_RspDx_AntennaSelectT ANTENNA_PORT = sdrplay_api_RspDx_ANTENNA_A; // Cổng A BNC

    // =========================================================================
    // CẤU HÌNH ĐỘ LỢI (GAIN CONTROL) - KHÓA CỨNG ĐỐI XỨNG CẢ 2 KÊNH
    // =========================================================================
    // Tắt hoàn toàn AGC để tránh méo pha và giữ cố định biên độ
    constexpr sdrplay_api_AgcControlT AGC_MODE              = sdrplay_api_AGC_DISABLE;

    // RF Gain (Tầng LNA đầu vào):
    // Dải 12-50 MHz hỗ trợ State 0 đến 19:
    // State 0 = 0 dB GR (Max Gain) | State 4 = 12 dB GR | State 5 = 15 dB GR
    constexpr unsigned char LNA_STATE                       = 8; 

    // IF Gain (Tầng trung tần):
    // Suy hao trung tần đặt mức 30 dB (Manual IF Gain)
    constexpr int GAIN_GRDB                                 = 30;
}

namespace SHM {
    const std::string CH1_NAME                         = "/shm_sdr_ch1";
    const std::string CH2_NAME                         = "/shm_sdr_ch2";
    constexpr uint32_t BUFFER_CAPACITY                 = 1 << 22;                // 4,194,304 mẫu
}

namespace Alignment {
    constexpr uint32_t SNAPSHOT_SIZE                   = 131072;
    constexpr float PNR_THRESHOLD_DB                   = 10.0f;
    constexpr uint32_t PEAK_SEARCH_WINDOW              = 16;
}

namespace Test {
    constexpr double DURATION_SEC                      = 60.0;
    constexpr double CHECK_INTERVAL_SEC                = 5.0;
    constexpr uint32_t DRAIN_CHUNK_SIZE                = 16384;
}

// BỔ SUNG MODULE 3 & 4



namespace Hardware {
        constexpr int PIN_RF_SWITCH       = 6;   // GPIO 6: RF SPDT Switch
        constexpr int PIN_NOISE_SOURCE    = 26;  // GPIO 26: Nguồn 15V Noise Source
        constexpr int WARMUP_DELAY_MS     = 200; // Thời gian chờ ổn định DC bias (ms)
        constexpr int SWITCH_SETTLE_MS    = 100; // Thời gian trễ gạt tiếp điểm RF (ms)
    }

} // namespace SolarConfig