#include <iostream>
#include <iomanip>
#include <atomic>
#include <chrono>
#include <thread>
#include <vector>
#include <csignal>
#include <algorithm>
#include <cmath>
#include <unistd.h>
#include <sys/wait.h>
#include "sdrplay_api.h"
#include "config.hpp"
#include "ring_buffer.hpp"
#include "time_alignment.hpp"

static std::atomic<bool> g_child_running{true};

static void child_sig_handler(int) {
    g_child_running.store(false);
}

// Callback thu thập mẫu I/Q từ phần cứng SDR cho từng thiết bị
static void ChildStreamCallback(short *xi, short *xq, 
                                [[maybe_unused]] sdrplay_api_StreamCbParamsT *params, 
                                unsigned int numSamples, 
                                [[maybe_unused]] unsigned int reset, 
                                void *cbContext) {
    auto* ring_buffer = static_cast<SharedMemoryRingBuffer*>(cbContext);
    if (ring_buffer == nullptr || numSamples == 0) return;

    if (!ring_buffer->is_stream_started()) {
        return;
    }

    ring_buffer->write(xi, xq, numSamples);
}

static void ChildDummyEventCallback([[maybe_unused]] sdrplay_api_EventT eventId, 
                                    [[maybe_unused]] sdrplay_api_TunerSelectT tuner, 
                                    [[maybe_unused]] sdrplay_api_EventParamsT *params, 
                                    [[maybe_unused]] void *cbContext) {}

// Hàm thu thập dữ liệu độc lập chạy trên tiến trình con (Producer)
static void run_sdr_producer(int dev_idx, const std::string& target_serno, const std::string& shm_name) {
    std::signal(SIGTERM, child_sig_handler);
    std::signal(SIGINT, child_sig_handler);

    SharedMemoryRingBuffer producer_rb(shm_name, SolarConfig::SHM::BUFFER_CAPACITY, true);

    sdrplay_api_ErrT err = sdrplay_api_Open();
    if (err != sdrplay_api_Success) {
        std::cerr << "[Tiến trình " << dev_idx << "] Lỗi Open API: " << sdrplay_api_GetErrorString(err) << "\n";
        _exit(1);
    }

    sdrplay_api_DeviceT devs[SDRPLAY_MAX_DEVICES];
    unsigned int numDevs = 0;
    sdrplay_api_GetDevices(devs, &numDevs, SDRPLAY_MAX_DEVICES);

    int selected_idx = -1;
    for (unsigned int i = 0; i < numDevs; ++i) {
        if (target_serno == devs[i].SerNo) {
            selected_idx = static_cast<int>(i);
            break;
        }
    }

    if (selected_idx == -1) {
        std::cerr << "[Tiến trình " << dev_idx << "] Không tìm thấy Serial: " << target_serno << "\n";
        sdrplay_api_Close();
        _exit(1);
    }

    err = sdrplay_api_SelectDevice(&devs[selected_idx]);
    if (err != sdrplay_api_Success) {
        std::cerr << "[Tiến trình " << dev_idx << "] Lỗi SelectDevice: " << sdrplay_api_GetErrorString(err) << "\n";
        sdrplay_api_Close();
        _exit(1);
    }

    sdrplay_api_DeviceParamsT *deviceParams = nullptr;
    sdrplay_api_GetDeviceParams(devs[selected_idx].dev, &deviceParams);

    auto *chParams = deviceParams->rxChannelA;
    chParams->tunerParams.rfFreq.rfHz   = SolarConfig::SDR::RF_CENTER_FREQ_HZ;
    chParams->tunerParams.bwType        = SolarConfig::SDR::IF_BW;
    
    // Nạp cấu hình Gain từ file config
    chParams->ctrlParams.agc.enable     = SolarConfig::SDR::AGC_MODE;
    chParams->tunerParams.gain.LNAstate = SolarConfig::SDR::LNA_STATE;
    chParams->tunerParams.gain.gRdB     = SolarConfig::SDR::GAIN_GRDB;

    // Cấu hình phần cứng chung
    deviceParams->devParams->fsFreq.fsHz            = SolarConfig::SDR::SAMPLE_RATE_HZ;
    deviceParams->devParams->rspDxParams.antennaSel = SolarConfig::SDR::ANTENNA_PORT;

    sdrplay_api_CallbackFnsT cbFns{};
    cbFns.StreamACbFn = ChildStreamCallback;
    cbFns.EventCbFn   = ChildDummyEventCallback;

    err = sdrplay_api_Init(devs[selected_idx].dev, &cbFns, &producer_rb);
    if (err != sdrplay_api_Success) {
        std::cerr << "[Tiến trình " << dev_idx << "] Lỗi Init: " << sdrplay_api_GetErrorString(err) << "\n";
        sdrplay_api_ReleaseDevice(&devs[selected_idx]);
        sdrplay_api_Close();
        _exit(1);
    }

    producer_rb.set_ready(true);

    while (g_child_running.load(std::memory_order_relaxed)) {
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }

    sdrplay_api_Uninit(devs[selected_idx].dev);
    sdrplay_api_ReleaseDevice(&devs[selected_idx]);
    sdrplay_api_Close();
    _exit(0);
}

int main() {
    std::cout << "===================================================================\n";
    std::cout << " SOLAR INTERFEROMETER - MODULE 2: POSIX SHM & TIME ALIGNMENT TEST\n";
    std::cout << "===================================================================\n";

    if (sdrplay_api_Open() != sdrplay_api_Success) {
        std::cerr << "[Lỗi] Không thể kết nối SDRplay Service API!\n";
        return -1;
    }

    sdrplay_api_DeviceT devs[SDRPLAY_MAX_DEVICES];
    unsigned int numDevs = 0;
    sdrplay_api_GetDevices(devs, &numDevs, SDRPLAY_MAX_DEVICES);

    if (numDevs < 2) {
        std::cerr << "[Lỗi] Cần tối thiểu 2 thiết bị RSPdx! Tìm thấy: " << numDevs << "\n";
        sdrplay_api_Close();
        return -1;
    }

    std::string ser0 = devs[0].SerNo;
    std::string ser1 = devs[1].SerNo;
    std::cout << "[Hardware] Phát hiện 2 thiết bị RSPdx:\n";
    std::cout << "  - Ch 1 Serial: " << ser0 << "\n";
    std::cout << "  - Ch 2 Serial: " << ser1 << "\n";
    sdrplay_api_Close();

    SharedMemoryRingBuffer::unlink_shm(SolarConfig::SHM::CH1_NAME);
    SharedMemoryRingBuffer::unlink_shm(SolarConfig::SHM::CH2_NAME);

    pid_t pid1 = fork();
    if (pid1 == 0) {
        run_sdr_producer(0, ser0, SolarConfig::SHM::CH1_NAME);
    }

    pid_t pid2 = fork();
    if (pid2 == 0) {
        run_sdr_producer(1, ser1, SolarConfig::SHM::CH2_NAME);
    }

    std::cout << "\n[DSP Engine] Đang đợi cả 2 phần cứng RSPdx hoàn tất khởi tạo Init...\n";

    try {
        SharedMemoryRingBuffer rb1(SolarConfig::SHM::CH1_NAME, SolarConfig::SHM::BUFFER_CAPACITY, false);
        SharedMemoryRingBuffer rb2(SolarConfig::SHM::CH2_NAME, SolarConfig::SHM::BUFFER_CAPACITY, false);
        TimeAligner aligner(SolarConfig::Alignment::SNAPSHOT_SIZE);

        int wait_count = 0;
        while ((!rb1.is_ready() || !rb2.is_ready()) && wait_count < 100) {
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
            wait_count++;
        }

        if (!rb1.is_ready() || !rb2.is_ready()) {
            throw std::runtime_error("Quá thời gian chờ khởi tạo phần cứng!");
        }

        std::cout << "[DSP Engine] Cả 2 phần cứng đã sẵn sàng. Bật cổng stream đồng thời!\n";
        rb1.set_start_stream(true);
        rb2.set_start_stream(true);

        // Chống tràn bộ đệm: Chỉ chờ 200ms để ổn định luồng (2 triệu mẫu < 4.19M dung lượng SHM)
        std::cout << "[DSP Engine] Đang chờ luồng USB và bộ lọc DC nội bộ ổn định (200ms)...\n";
        std::this_thread::sleep_for(std::chrono::milliseconds(200));

        // ===================================================================
        // BƯỚC 0: FLUSH ĐỒNG THỜI ĐỂ THIẾT LẬP MỐC T0 THỜI GIAN THỰC
        // ===================================================================
        std::cout << "\n>>> [BƯỚC 0] THIẾT LẬP MỐC ĐỒNG BỘ THỜI GIAN THỰC T0 (FLUSH BARRIER)...\n";
        rb1.flush();
        rb2.flush();

        // Chờ cả 2 bộ đệm nạp đủ SNAPSHOT_SIZE mẫu từ đúng mốc T0
        while (rb1.available_read() < SolarConfig::Alignment::SNAPSHOT_SIZE ||
               rb2.available_read() < SolarConfig::Alignment::SNAPSHOT_SIZE) {
            std::this_thread::sleep_for(std::chrono::microseconds(500));
        }

        // ===================================================================
        // BƯỚC 1: ĐO ĐỘ LỆCH MẪU KHỞI ĐỘNG BAN ĐẦU
        // ===================================================================
        std::cout << "\n>>> [BƯỚC 1] ĐO ĐỘ LỆCH MẪU KHỞI ĐỘNG BAN ĐẦU (N = " 
                  << SolarConfig::Alignment::SNAPSHOT_SIZE << ")...\n";
        AlignmentResult initial_res = aligner.measure_offset_from_buffers(rb1, rb2);

        std::cout << std::fixed << std::setprecision(3);
        std::cout << "  + Đỉnh tương quan ban đầu : k_offset = " << initial_res.lag_samples << " mẫu\n";
        std::cout << "  + Độ trễ thời gian         : Delta_t  = " << initial_res.delay_microsec << " us\n";
        std::cout << "  + Peak-to-Noise Ratio     : PNR      = " << initial_res.pnr_db << " dB\n";

        // ===================================================================
        // BƯỚC 2: CĂN CHỈNH XẢ MẪU BÙ TRỄ (SAMPLE DROPPING)
        // ===================================================================
        std::cout << "\n>>> [BƯỚC 2] THỰC HIỆN CĂN CHỈNH XẢ MẪU BÙ TRỄ...\n";
        if (initial_res.is_valid) {
            if (initial_res.lag_samples > 0) {
                rb1.skip(static_cast<uint32_t>(initial_res.lag_samples));
                std::cout << "  -> Đã xả đúng " << initial_res.lag_samples << " mẫu ở KÊNH 1.\n";
            } else if (initial_res.lag_samples < 0) {
                rb2.skip(static_cast<uint32_t>(-initial_res.lag_samples));
                std::cout << "  -> Đã xả đúng " << -initial_res.lag_samples << " mẫu ở KÊNH 2.\n";
            } else {
                std::cout << "  -> Hai kênh đã trùng khớp từ đầu (k = 0).\n";
            }

            // CHỐNG RACE CONDITION: Chờ cả 2 bộ đệm nạp đủ lại mẫu sau khi xả trước khi sang Bước 3
            while (rb1.available_read() < SolarConfig::Alignment::SNAPSHOT_SIZE ||
                   rb2.available_read() < SolarConfig::Alignment::SNAPSHOT_SIZE) {
                std::this_thread::sleep_for(std::chrono::microseconds(500));
            }
        } else {
            std::cerr << "  [Cảnh báo] Không thể căn chỉnh do PNR dưới ngưỡng cho phép!\n";
        }

        // ===================================================================
        // BƯỚC 3: XÁC NHẬN ĐỒNG BỘ SAU CĂN CHỈNH
        // ===================================================================
        std::cout << "\n>>> [BƯỚC 3] XÁC NHẬN ĐỒNG BỘ SAU CĂN CHỈNH...\n";
        AlignmentResult verify_res = aligner.measure_offset_from_buffers(rb1, rb2);
        std::cout << "  + Tọa độ đỉnh mới  : k = " << verify_res.lag_samples << " mẫu (" 
                  << verify_res.delay_microsec << " us)\n";
        std::cout << "  + PNR sau căn chỉnh : " << verify_res.pnr_db << " dB\n";

        bool pass_step3 = (std::abs(verify_res.lag_samples) <= 1 && verify_res.is_valid);
        if (pass_step3) {
            std::cout << "  => TRẠNG THÁI: \033[32m[PASS] ĐỒNG BỘ MẪU HOÀN TOÀN (SAMPLE-ALIGNED)\033[0m\n";
        } else {
            std::cout << "  => TRẠNG THÁI: \033[31m[FAIL] Chưa khóa được đỉnh chính xác!\033[0m\n";
        }

        // ===================================================================
        // BƯỚC 4: VÒNG LẶP TEST KHÓA PHA ỔN ĐỊNH VÀ TIÊU THỤ ĐỒNG BỘ (60 GIÂY)
        // ===================================================================
        std::cout << "\n>>> [BƯỚC 4] BẮT ĐẦU TEST KHÓA PHA ỔN ĐỊNH TRONG " << SolarConfig::Test::DURATION_SEC << " GIÂY...\n";
        std::cout << "------------------------------------------------------------------------------------------------------\n";
        std::cout << " Thời gian | Round | Đỉnh k (mẫu) | Độ trễ (us) |   PNR (dB)   | Peak Mag | Noise Flr | Buffer Ch1/Ch2 | Drop Ch1/Ch2 | Trạng thái\n";
        std::cout << "------------------------------------------------------------------------------------------------------\n";

        std::vector<ComplexSample> drain_buf1(SolarConfig::Test::DRAIN_CHUNK_SIZE);
        std::vector<ComplexSample> drain_buf2(SolarConfig::Test::DRAIN_CHUNK_SIZE);
        auto start_time = std::chrono::steady_clock::now();
        auto last_check = start_time;
        int check_round = 1;
        int failed_rounds = 0;

        while (true) {
            auto now = std::chrono::steady_clock::now();
            double elapsed_total = std::chrono::duration<double>(now - start_time).count();
            if (elapsed_total >= SolarConfig::Test::DURATION_SEC) {
                break;
            }

            uint32_t av1 = rb1.available_read();
            uint32_t av2 = rb2.available_read();

            if (std::chrono::duration<double>(now - last_check).count() >= SolarConfig::Test::CHECK_INTERVAL_SEC) {
                if (av1 >= SolarConfig::Alignment::SNAPSHOT_SIZE && av2 >= SolarConfig::Alignment::SNAPSHOT_SIZE) {
                    AlignmentResult check_res = aligner.measure_offset_from_buffers(rb1, rb2);
                    
                    std::cout << "  [T+" << std::setw(2) << std::setfill(' ') << static_cast<int>(elapsed_total) << "s]   | "
                              << "#" << std::setw(2) << std::setfill('0') << check_round++ << "  | "
                              << std::setw(6) << std::setfill(' ') << check_res.lag_samples << "      | "
                              << std::setw(7) << std::fixed << std::setprecision(3) << check_res.delay_microsec << "   | "
                              << std::setw(6) << std::fixed << std::setprecision(2) << check_res.pnr_db << " dB   | "
                              << std::setw(8) << std::scientific << std::setprecision(1) << check_res.peak_mag << " | "
                              << std::setw(8) << std::scientific << std::setprecision(1) << check_res.noise_floor << "  | "
                              << std::dec << std::setw(5) << (av1 / 1024) << "k/" << std::setw(5) << (av2 / 1024) << "k  | "
                              << std::setw(3) << rb1.get_overflow_count() << "/" << std::setw(3) << rb2.get_overflow_count() << "    | ";

                    if (std::abs(check_res.lag_samples) <= 1 && check_res.is_valid) {
                        std::cout << "\033[32m[LOCKED]\033[0m\n";
                    } else if (!check_res.is_valid) {
                        std::cout << "\033[33m[LOW SNR]\033[0m\n";
                    } else {
                        std::cout << "\033[31m[DRIFTED " << check_res.lag_samples << "smp]\033[0m\n";
                        failed_rounds++;
                    }
                    last_check = now;
                }
            }

            // Tiêu thụ đồng đều lượng mẫu vượt mức snapshot ở cả 2 kênh để duy trì mốc k = 0
            uint32_t common_avail = std::min(av1, av2);
            if (common_avail > SolarConfig::Alignment::SNAPSHOT_SIZE) {
                uint32_t to_read = std::min(common_avail - SolarConfig::Alignment::SNAPSHOT_SIZE, 
                                            static_cast<uint32_t>(SolarConfig::Test::DRAIN_CHUNK_SIZE));
                rb1.read(drain_buf1.data(), to_read);
                rb2.read(drain_buf2.data(), to_read);
            } else {
                std::this_thread::sleep_for(std::chrono::microseconds(500));
            }
        }
        std::cout << "------------------------------------------------------------------------------------------------------\n";

        std::cout << "\n===================================================================\n";
        if (pass_step3 && failed_rounds == 0) {
            std::cout << " \033[32mKẾT LUẬN: BÀI KIỂM TRA MODULE 2 ĐẠT CHUẨN XUẤT SẮC (PASS)\033[0m\n";
            std::cout << " - Hai luồng I/Q đã khóa đồng bộ mẫu (|k| <= 1 sample ~ 100 ns).\n";
            std::cout << " - Xung REFin 24 MHz giữ ổn định 100% trong suốt 60 giây (Zero Drift).\n";
        } else {
            std::cout << " \033[31mKẾT LUẬN: BÀI KIỂM TRA MODULE 2 CHƯA ĐẠT (FAIL)\033[0m\n";
        }
        std::cout << "===================================================================\n";

    } catch (const std::exception& e) {
        std::cerr << "[Exception] " << e.what() << "\n";
    }

    kill(pid1, SIGTERM);
    kill(pid2, SIGTERM);

    int status;
    waitpid(pid1, &status, 0);
    waitpid(pid2, &status, 0);

    SharedMemoryRingBuffer::unlink_shm(SolarConfig::SHM::CH1_NAME);
    SharedMemoryRingBuffer::unlink_shm(SolarConfig::SHM::CH2_NAME);

    return 0;
}