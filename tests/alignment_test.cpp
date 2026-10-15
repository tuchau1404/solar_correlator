#include <iostream>
#include <cstdlib>
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
#include "hardware_control.hpp"
#include "startup_alignment.hpp"

static_assert(std::atomic<bool>::is_always_lock_free, "signal flags must be lock-free");

static std::atomic<bool> g_child_running{true};
static std::atomic<bool> g_parent_running{true};

static void child_sig_handler(int) {
    g_child_running.store(false);
}

static void parent_sig_handler(int) {
    g_parent_running.store(false);
}

struct StreamContext {
    SharedMemoryRingBuffer* ring;
    uint32_t expected_sample{0};
    bool have_expected{false}; // Sole serialized producer callback owns these.
};

static void ChildStreamCallback(short *xi, short *xq,
                                sdrplay_api_StreamCbParamsT *params,
                                unsigned int numSamples,
                                unsigned int reset,
                                void *cbContext) {
    auto* ctx = static_cast<StreamContext*>(cbContext);
    if (!ctx || !ctx->ring) return;
    auto* ring_buffer = ctx->ring;
    if (!g_child_running.load(std::memory_order_relaxed)) return;

    if (!ring_buffer->is_stream_started()) {
        return;
    }

    // Fail closed on loss: ring positions cannot encode missing hardware time.
    auto fail_stream = [&] {
        ring_buffer->set_ready(false);
        g_child_running.store(false, std::memory_order_relaxed);
    };
    if (reset || !params) { fail_stream(); return; }
    if (numSamples == 0) return;
    if (ctx->have_expected && params->firstSampleNum != ctx->expected_sample) {
        fail_stream();
        return;
    }
    ctx->expected_sample = static_cast<uint32_t>(params->firstSampleNum + numSamples);
    ctx->have_expected = true;
    try {
        if (ring_buffer->write(xi, xq, numSamples) != numSamples)
            fail_stream();
    } catch (...) {
        fail_stream();
    }
}

static void ChildDummyEventCallback([[maybe_unused]] sdrplay_api_EventT eventId,
                                    [[maybe_unused]] sdrplay_api_TunerSelectT tuner,
                                    [[maybe_unused]] sdrplay_api_EventParamsT *params,
                                    [[maybe_unused]] void *cbContext) {}

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
    if (sdrplay_api_GetDevices(devs, &numDevs, SDRPLAY_MAX_DEVICES) != sdrplay_api_Success) {
        sdrplay_api_Close();
        throw std::runtime_error("GetDevices failed");
    }

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
    if (sdrplay_api_GetDeviceParams(devs[selected_idx].dev, &deviceParams) != sdrplay_api_Success ||
        !deviceParams || !deviceParams->rxChannelA || !deviceParams->devParams) {
        sdrplay_api_ReleaseDevice(&devs[selected_idx]);
        sdrplay_api_Close();
        throw std::runtime_error("GetDeviceParams failed");
    }

    auto *chParams = deviceParams->rxChannelA;
    chParams->tunerParams.rfFreq.rfHz   = SolarConfig::SDR::RF_CENTER_FREQ_HZ;
    chParams->tunerParams.bwType        = SolarConfig::SDR::IF_BW;
    chParams->ctrlParams.agc.enable     = SolarConfig::SDR::AGC_MODE;
    chParams->tunerParams.gain.LNAstate = SolarConfig::SDR::LNA_STATE;
    chParams->tunerParams.gain.gRdB     = SolarConfig::SDR::GAIN_GRDB;

    deviceParams->devParams->fsFreq.fsHz            = SolarConfig::SDR::SAMPLE_RATE_HZ;
    deviceParams->devParams->rspDxParams.antennaSel = SolarConfig::SDR::ANTENNA_PORT;

    sdrplay_api_CallbackFnsT cbFns{};
    cbFns.StreamACbFn = ChildStreamCallback;
    cbFns.EventCbFn   = ChildDummyEventCallback;

    StreamContext context{&producer_rb};
    err = sdrplay_api_Init(devs[selected_idx].dev, &cbFns, &context);
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

    producer_rb.set_ready(false);
    sdrplay_api_Uninit(devs[selected_idx].dev);
    sdrplay_api_ReleaseDevice(&devs[selected_idx]);
    sdrplay_api_Close();
    _exit(0);
}

static void auto_reset_hardware() {
    std::cout << "[INIT] Đang tự động reset USB và khởi động lại sdrplay service...\n";

    // Dọn file SHM cũ
    std::system("rm -f /dev/shm/shm_sdr_ch*");

    // Reset USB các thiết bị có ID 1df7
    std::system("for dev in $(lsusb -d 1df7: | awk '{print $2\"/\"substr($4,1,3)}'); do sudo usbreset \"$dev\"; done");

    // Restart dịch vụ sdrplay daemon
    std::system("sudo systemctl restart sdrplay");

    // Chờ 2.5 giây cho firmware và mạch PLL ổn định
    std::this_thread::sleep_for(std::chrono::milliseconds(2500));
    std::cout << "[INIT] Phần cứng đã sẵn sàng!\n";
}

int main() {
    auto_reset_hardware();
    std::cout << "===================================================================\n";
    std::cout << " SOLAR INTERFEROMETER - MODULE 2: POSIX SHM & TIME ALIGNMENT TEST\n";
    std::cout << "===================================================================\n";

    if (sdrplay_api_Open() != sdrplay_api_Success) {
        std::cerr << "[Lỗi] Không thể kết nối SDRplay Service API!\n";
        return -1;
    }

    sdrplay_api_DeviceT devs[SDRPLAY_MAX_DEVICES];
    unsigned int numDevs = 0;
    if (sdrplay_api_GetDevices(devs, &numDevs, SDRPLAY_MAX_DEVICES) != sdrplay_api_Success) {
        sdrplay_api_Close();
        throw std::runtime_error("GetDevices failed");
    }

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

    const std::string ch1_name = SolarConfig::SHM::CH1_NAME + "_audit_" + std::to_string(getpid());
    const std::string ch2_name = SolarConfig::SHM::CH2_NAME + "_audit_" + std::to_string(getpid());
    SharedMemoryRingBuffer::unlink_shm(ch1_name);
    SharedMemoryRingBuffer::unlink_shm(ch2_name);

    std::signal(SIGTERM, parent_sig_handler);
    std::signal(SIGINT, parent_sig_handler);

    int exit_code = 1;
    pid_t pid1 = fork();
    if (pid1 == 0) {
        try { run_sdr_producer(0, ser0, ch1_name); } catch (...) { _exit(1); }
    }

    if (pid1 < 0) { std::cerr << "fork CH1 failed\n"; return 1; }

    pid_t pid2 = fork();
    if (pid2 == 0) {
        try { run_sdr_producer(1, ser1, ch2_name); } catch (...) { _exit(1); }
    }

    if (pid2 < 0) {
        kill(pid1, SIGTERM);
        while (waitpid(pid1, nullptr, 0) < 0 && errno == EINTR) {}
        SharedMemoryRingBuffer::unlink_shm(ch1_name);
        return 1;
    }

    // Tiến trình cha bật GPIO 6 và 26 theo cấu hình SolarConfig::Hardware
    HardwareGuard hw_guard;

    std::cout << "\n[DSP Engine] Đang đợi cả 2 phần cứng RSPdx hoàn tất khởi tạo Init...\n";

    try {
        SharedMemoryRingBuffer rb1(ch1_name, SolarConfig::SHM::BUFFER_CAPACITY, false);
        SharedMemoryRingBuffer rb2(ch2_name, SolarConfig::SHM::BUFFER_CAPACITY, false);
        TimeAligner aligner(SolarConfig::Alignment::SNAPSHOT_SIZE);

        int wait_count = 0;
        while ((!rb1.is_ready() || !rb2.is_ready()) && wait_count < 100 && g_parent_running.load(std::memory_order_relaxed)) {
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
            wait_count++;
        }

        if (!g_parent_running.load(std::memory_order_relaxed) || !rb1.is_ready() || !rb2.is_ready()) {
            throw std::runtime_error("Quá thời gian chờ khởi tạo phần cứng hoặc nhận lệnh ngắt!");
        }

        std::cout << "[DSP Engine] Cả 2 phần cứng đã sẵn sàng. Bật cổng stream đồng thời!\n";
        auto require_healthy = [&] {
            if (!rb1.is_ready() || !rb2.is_ready() ||
                rb1.get_overflow_count() != 0 || rb2.get_overflow_count() != 0)
                throw std::runtime_error("producer stopped or stream discontinuity detected");
        };
        auto wait_snapshot = [&] {
            if (!g_parent_running.load(std::memory_order_relaxed))
                throw std::runtime_error("alignment interrupted");
            const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(3);
            while (rb1.available_read() < SolarConfig::Alignment::SNAPSHOT_SIZE ||
                   rb2.available_read() < SolarConfig::Alignment::SNAPSHOT_SIZE) {
                require_healthy();
                if (!g_parent_running.load(std::memory_order_relaxed) ||
                    std::chrono::steady_clock::now() >= deadline)
                    throw std::runtime_error("snapshot wait interrupted or timed out");
                std::this_thread::sleep_for(std::chrono::microseconds(500));
            }
            require_healthy();
        };
        rb1.set_start_stream(true);
        rb2.set_start_stream(true);

        std::cout << "[DSP Engine] Đang chờ luồng USB và bộ lọc DC nội bộ ổn định (200ms)...\n";
        std::this_thread::sleep_for(std::chrono::milliseconds(200));

        StartupAlignment::Options options;
        options.snapshot_size = SolarConfig::Alignment::SNAPSHOT_SIZE;
        StartupAlignment::acquire(rb1, rb2, options, wait_snapshot,
            [&] { return aligner.measure_offset_from_buffers(rb1, rb2); }, std::cout);

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
        auto last_drain = start_time;
        uint64_t drained_samples = 0;
        int check_round = 1;
        int failed_rounds = 0;
        constexpr int required_rounds = 11; // t=5,10,...,55 for the 60 s test
        static_assert(SolarConfig::Test::DURATION_SEC == 60.0 && SolarConfig::Test::CHECK_INTERVAL_SEC == 5.0);

        while (g_parent_running.load(std::memory_order_relaxed)) {
            require_healthy();
            auto now = std::chrono::steady_clock::now();
            if (now - last_drain > std::chrono::seconds(3))
                throw std::runtime_error("stream made no consumer progress for 3 seconds");
            double elapsed_total = std::chrono::duration<double>(now - start_time).count();
            if (elapsed_total >= SolarConfig::Test::DURATION_SEC) {
                break;
            }

            uint32_t av1 = rb1.available_read();
            uint32_t av2 = rb2.available_read();

            if (elapsed_total >= check_round * SolarConfig::Test::CHECK_INTERVAL_SEC + 1.0)
                throw std::runtime_error("missed measurement deadline");
            if (elapsed_total >= check_round * SolarConfig::Test::CHECK_INTERVAL_SEC) {
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
                        failed_rounds++;
                        std::cout << "\033[33m[LOW SNR]\033[0m\n";
                    } else {
                        std::cout << "\033[31m[DRIFTED " << check_res.lag_samples << "smp]\033[0m\n";
                        failed_rounds++;
                    }
                }
            }

            uint32_t common_avail = std::min(av1, av2);
            if (common_avail > SolarConfig::Alignment::SNAPSHOT_SIZE) {
                uint32_t to_read = std::min(common_avail - SolarConfig::Alignment::SNAPSHOT_SIZE,
                                            static_cast<uint32_t>(SolarConfig::Test::DRAIN_CHUNK_SIZE));
                if (rb1.read(drain_buf1.data(), to_read) != to_read ||
                    rb2.read(drain_buf2.data(), to_read) != to_read)
                    throw std::runtime_error("unequal paired drain");
                drained_samples += to_read;
                last_drain = std::chrono::steady_clock::now();
            } else {
                std::this_thread::sleep_for(std::chrono::microseconds(500));
            }
        }
        std::cout << "------------------------------------------------------------------------------------------------------\n";

        std::cout << "\n===================================================================\n";
        require_healthy();
        // Include a final observation at/after 60 s; elapsed time is checked too.
        wait_snapshot();
        const auto final_res = aligner.measure_offset_from_buffers(rb1, rb2);
        if (!final_res.is_valid || std::abs(final_res.lag_samples) > 1) failed_rounds++;
        require_healthy();
        std::cout << "Paired samples drained: " << drained_samples << "\n";
        const bool full_duration = std::chrono::duration<double>(std::chrono::steady_clock::now() - start_time).count()
                                   >= SolarConfig::Test::DURATION_SEC;
        if (failed_rounds == 0 &&
            check_round - 1 >= required_rounds && full_duration && g_parent_running.load(std::memory_order_relaxed)) {
            exit_code = 0;
            std::cout << " \033[32mKẾT LUẬN: BÀI KIỂM TRA MODULE 2 ĐẠT CHUẨN XUẤT SẮC (PASS)\033[0m\n";
            std::cout << " - Hai luồng I/Q đã khóa đồng bộ mẫu (|k| <= 1 sample ~ 100 ns).\n";
            std::cout << " - Các snapshot kiểm tra đạt tiêu chí integer-lag; không đo trực tiếp độ ổn định REFin.\n";
        } else {
            std::cout << " \033[31mKẾT LUẬN: BÀI KIỂM TRA MODULE 2 CHƯA ĐẠT (FAIL)\033[0m\n";
        }
        std::cout << "===================================================================\n";

    } catch (const StartupAlignment::Failed& e) {
        std::cerr << "[AUTO-ALIGN] " << e.what() << "; cleaning up before automatic restart.\n";
        exit_code = 75; // Launcher retries only alignment exhaustion, not stability failures.
    } catch (const std::exception& e) {
        std::cerr << "[Exception] " << e.what() << "\n";
    }

    hw_guard.turn_off();

    kill(pid1, SIGTERM);
    kill(pid2, SIGTERM);

    int status;
    while (waitpid(pid1, &status, 0) < 0 && errno == EINTR) {}
    while (waitpid(pid2, &status, 0) < 0 && errno == EINTR) {}

    SharedMemoryRingBuffer::unlink_shm(ch1_name);
    SharedMemoryRingBuffer::unlink_shm(ch2_name);

    return g_parent_running.load(std::memory_order_relaxed) ? exit_code : 130;
}
