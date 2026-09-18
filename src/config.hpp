#pragma once

#include <cstdint>
#include <string>
#include "sdrplay_api.h"

namespace SolarConfig {

namespace SDR {
    // Tần số và bộ lọc băng thông
    constexpr double RF_CENTER_FREQ_HZ                      = 35000000.0;           // 35.0 MHz (Phủ dải 30-40 MHz)
    constexpr sdrplay_api_Bw_MHzT IF_BW                     = sdrplay_api_BW_8_000; // 8.0 MHz Analog Filter[cite: 3]
    constexpr double SAMPLE_RATE_HZ                         = 10000000.0;           // 10.0 MSPS (100 ns/mẫu)[cite: 3]
    constexpr sdrplay_api_RspDx_AntennaSelectT ANTENNA_PORT = sdrplay_api_RspDx_ANTENNA_A; // Cổng C BNC[cite: 3]

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
namespace Correlator {
    constexpr uint32_t FFT_SIZE                        = 2048;                   // Phân giải ~4.88 kHz/bin
    constexpr uint32_t HOP_SIZE                        = FFT_SIZE / 2;           // 1024 mẫu (Gối đầu 50% Welch's method)
    constexpr double INTEGRATION_TIME_SEC              = 0.2;                    // 200 ms
    // Số khung tích phân (M = 10,000,000 * 0.2 / 1024 = 1953)
    constexpr uint32_t INTEGRATION_FRAMES              = static_cast<uint32_t>((SDR::SAMPLE_RATE_HZ * INTEGRATION_TIME_SEC) / HOP_SIZE);
    constexpr float DYNAMIC_RANGE_EPS                  = 1e-12f;                 // Chống log10(0)
}

namespace Network {
    const std::string UDP_DEST_IP                      = "192.168.1.2";        // IP Workstation
    constexpr uint16_t UDP_DEST_PORT                   = 9999;
    constexpr uint32_t TELEMETRY_MAGIC                 = 0x534F4C52;             // "SOLR"
}

} // namespace SolarConfig