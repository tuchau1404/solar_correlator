#include <iostream>
#include <iomanip>
#include <atomic>
#include <chrono>
#include <thread>
#include <vector>
#include <csignal>
#include <unistd.h>
#include <sys/wait.h>
#include "sdrplay_api.h"
#include "config.hpp"
#include "ring_buffer.hpp"
#include "time_alignment.hpp"
#include "correlator.hpp"
#include "telemetry_sender.hpp"

static std::atomic<bool> g_system_running{true};

static void signal_handler(int) {
    g_system_running.store(false);
}

// ============================================================================
// HÀM CHẠY TIẾN TRÌNH CON ĐỂ GHI SDR (Tái sử dụng chuẩn từ Module 2)
// ============================================================================
static void ChildStreamCallback(short *xi, short *xq, sdrplay_api_StreamCbParamsT*, 
                                unsigned int numSamples, unsigned int, void *cbContext) {
    auto* ring_buffer = static_cast<SharedMemoryRingBuffer*>(cbContext);
    if (ring_buffer && ring_buffer->is_stream_started()) {
        ring_buffer->write(xi, xq, numSamples);
    }
}
static void ChildDummyEventCallback(sdrplay_api_EventT, sdrplay_api_TunerSelectT, sdrplay_api_EventParamsT*, void*) {}

static void run_sdr_producer(int dev_idx, const std::string& target_serno, const std::string& shm_name) {
    std::signal(SIGTERM, signal_handler);
    std::signal(SIGINT, signal_handler);

    SharedMemoryRingBuffer producer_rb(shm_name, SolarConfig::SHM::BUFFER_CAPACITY, true);
    sdrplay_api_Open();
    sdrplay_api_DeviceT devs[SDRPLAY_MAX_DEVICES];
    unsigned int numDevs = 0;
    sdrplay_api_GetDevices(devs, &numDevs, SDRPLAY_MAX_DEVICES);

    int selected_idx = -1;
    for (unsigned int i = 0; i < numDevs; ++i) {
        if (target_serno == devs[i].SerNo) { selected_idx = i; break; }
    }
    
    sdrplay_api_SelectDevice(&devs[selected_idx]);
    sdrplay_api_DeviceParamsT *deviceParams = nullptr;
    sdrplay_api_GetDeviceParams(devs[selected_idx].dev, &deviceParams);

    auto *chParams = deviceParams->rxChannelA;
    chParams->tunerParams.rfFreq.rfHz = SolarConfig::SDR::RF_CENTER_FREQ_HZ;
    chParams->tunerParams.bwType      = SolarConfig::SDR::IF_BW;
    chParams->ctrlParams.agc.enable   = sdrplay_api_AGC_DISABLE;
    chParams->tunerParams.gain.gRdB   = SolarConfig::SDR::GAIN_GRDB;
    deviceParams->devParams->fsFreq.fsHz            = SolarConfig::SDR::SAMPLE_RATE_HZ;
    deviceParams->devParams->rspDxParams.antennaSel = SolarConfig::SDR::ANTENNA_PORT;

    sdrplay_api_CallbackFnsT cbFns{};
    cbFns.StreamACbFn = ChildStreamCallback;
    cbFns.StreamBCbFn = nullptr;
    cbFns.EventCbFn   = ChildDummyEventCallback;
    sdrplay_api_Init(devs[selected_idx].dev, &cbFns, &producer_rb);
    producer_rb.set_ready(true);

    while (g_system_running.load()) std::this_thread::sleep_for(std::chrono::milliseconds(50));

    sdrplay_api_Uninit(devs[selected_idx].dev);
    sdrplay_api_ReleaseDevice(&devs[selected_idx]);
    sdrplay_api_Close();
    _exit(0);
}

// ============================================================================
// HỆ THỐNG ĐIỀU PHỐI CHÍNH (MASTER DSP)
// ============================================================================
int main() {
    std::cout << "===================================================================\n";
    std::cout << " SOLAR INTERFEROMETER - MODULE 3: FX CORRELATOR ENGINE & TELEMETRY\n";
    std::cout << "===================================================================\n";

    std::signal(SIGTERM, signal_handler);
    std::signal(SIGINT, signal_handler);

    sdrplay_api_Open();
    sdrplay_api_DeviceT devs[SDRPLAY_MAX_DEVICES];
    unsigned int numDevs = 0;
    sdrplay_api_GetDevices(devs, &numDevs, SDRPLAY_MAX_DEVICES);
    if (numDevs < 2) {
        std::cerr << "[Lỗi] Cần 2 thiết bị RSPdx. Tìm thấy: " << numDevs << "\n";
        return -1;
    }
    std::string ser0 = devs[0].SerNo, ser1 = devs[1].SerNo;
    sdrplay_api_Close();

    SharedMemoryRingBuffer::unlink_shm(SolarConfig::SHM::CH1_NAME);
    SharedMemoryRingBuffer::unlink_shm(SolarConfig::SHM::CH2_NAME);

    // Kích hoạt phần cứng (Module 2)
    pid_t pid1 = fork(); if (pid1 == 0) run_sdr_producer(0, ser0, SolarConfig::SHM::CH1_NAME);
    pid_t pid2 = fork(); if (pid2 == 0) run_sdr_producer(1, ser1, SolarConfig::SHM::CH2_NAME);

    try {
        SharedMemoryRingBuffer rb1(SolarConfig::SHM::CH1_NAME, SolarConfig::SHM::BUFFER_CAPACITY, false);
        SharedMemoryRingBuffer rb2(SolarConfig::SHM::CH2_NAME, SolarConfig::SHM::BUFFER_CAPACITY, false);
        TimeAligner aligner(SolarConfig::Alignment::SNAPSHOT_SIZE);

        while ((!rb1.is_ready() || !rb2.is_ready()) && g_system_running.load()) {
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
        }

        std::cout << "[DSP] Phần cứng sẵn sàng. Bật stream và đợi mốc T0 (500ms)...\n";
        rb1.set_start_stream(true);
        rb2.set_start_stream(true);
        std::this_thread::sleep_for(std::chrono::milliseconds(500));

        // BƯỚC 1: ĐỒNG BỘ MODULE 2
        rb1.flush(); rb2.flush();
        while (rb1.available_read() < SolarConfig::Alignment::SNAPSHOT_SIZE || 
               rb2.available_read() < SolarConfig::Alignment::SNAPSHOT_SIZE) {
            std::this_thread::sleep_for(std::chrono::microseconds(500));
        }

        AlignmentResult align_res = aligner.measure_offset_from_buffers(rb1, rb2);
        std::cout << "[Module 2] Tìm thấy k_offset = " << align_res.lag_samples << " mẫu. Đang bù trễ...\n";
        
        if (align_res.lag_samples > 0) rb1.skip(align_res.lag_samples);
        else if (align_res.lag_samples < 0) rb2.skip(-align_res.lag_samples);
        
        align_res = aligner.measure_offset_from_buffers(rb1, rb2);
        if (std::abs(align_res.lag_samples) <= 1) {
            std::cout << "\033[32m[PASS] Đồng bộ cứng mẫu k = 0 hoàn tất!\033[0m\n";
        }

        // BƯỚC 2: CHUYỂN GIAO SANG MODULE 3 (FX CORRELATOR)
        FXCorrelator correlator;
        TelemetrySender sender(SolarConfig::Network::UDP_DEST_IP, SolarConfig::Network::UDP_DEST_PORT);
        correlator.prime_buffers(rb1, rb2);
        
        std::vector<float> spectrum_db(SolarConfig::Correlator::FFT_SIZE);
        std::vector<float> phase_rad(SolarConfig::Correlator::FFT_SIZE);
        std::vector<float> coherence(SolarConfig::Correlator::FFT_SIZE);
        std::vector<float> fringe_phase(SolarConfig::Correlator::FFT_SIZE, 0.0f); // Default Phase Array 0

        uint64_t frame_id = 0;
        auto last_log = std::chrono::steady_clock::now();
        
        std::cout << "\n>>> [MODULE 3] KHỞI CHẠY BỘ XỬ LÝ GIAO THOA THỜI GIAN THỰC (50% Overlap)...\n";
        std::cout << "----------------------------------------------------------------------------------\n";
        std::cout << " Frame ID | Chu kỳ (ms) | Buffer Ch1/Ch2 (KB) | Drop | Peak dB | Tần số Đỉnh \n";
        std::cout << "----------------------------------------------------------------------------------\n";

        while (g_system_running.load()) {
            auto cycle_start = std::chrono::steady_clock::now();

            // Xử lý Toán học (Hot Loop DSP)
            correlator.process_integration_cycle(rb1, rb2, fringe_phase, spectrum_db, phase_rad, coherence);
            
            auto cycle_end = std::chrono::steady_clock::now();
            uint64_t ts_us = std::chrono::duration_cast<std::chrono::microseconds>(cycle_end.time_since_epoch()).count();

            // Bắn dữ liệu Telemetry (Non-blocking)
            sender.send_spectrum(frame_id, ts_us, spectrum_db);
            
            // In terminal mỗi 5 giây
            std::chrono::duration<double> elapsed_log = cycle_end - last_log;
            if (elapsed_log.count() >= 5.0) {
                double cycle_ms = std::chrono::duration<double, std::milli>(cycle_end - cycle_start).count();
                
                auto max_it = std::max_element(spectrum_db.begin(), spectrum_db.end());
                float peak_db = *max_it;
                int peak_idx = std::distance(spectrum_db.begin(), max_it);
                float peak_freq_mhz = 35.0f + (peak_idx - 1024) * (10.0f / 2048.0f);

                std::cout << " #" << std::setw(7) << frame_id << " | "
                          << std::setw(8) << std::fixed << std::setprecision(1) << cycle_ms << " ms | "
                          << std::setw(6) << (rb1.available_read() / 1024) << "K/" 
                          << std::setw(5) << (rb2.available_read() / 1024) << "K | "
                          << std::setw(4) << rb1.get_overflow_count() << " | "
                          << std::setw(6) << peak_db << " | " 
                          << std::setw(6) << peak_freq_mhz << " MHz\n";
                last_log = cycle_end;
            }
            frame_id++;
        }

    } catch(const std::exception& e) {
        std::cerr << "[Exception Lỗi] " << e.what() << "\n";
    }

    // Dọn dẹp tài nguyên
    std::cout << "\n[Hệ thống] Đang tắt an toàn và thu hồi tài nguyên...\n";
    kill(pid1, SIGTERM);
    kill(pid2, SIGTERM);
    int status;
    waitpid(pid1, &status, 0);
    waitpid(pid2, &status, 0);

    SharedMemoryRingBuffer::unlink_shm(SolarConfig::SHM::CH1_NAME);
    SharedMemoryRingBuffer::unlink_shm(SolarConfig::SHM::CH2_NAME);
    return 0;
}