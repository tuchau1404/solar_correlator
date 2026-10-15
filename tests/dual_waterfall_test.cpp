/**
 * @file dual_waterfall_test.cpp
 * @brief Công cụ chẩn đoán phần cứng độc lập: Dual SDR Live Waterfall Spectrogram.
 *        Thu dữ liệu 10 MSPS từ 2 máy thu SDRplay RSPdx, tính phổ tự thân S11 & S22
 *        bằng FXCorrelator (Welch 50% overlap, M = 1953) và phát qua 2 cổng UDP SOL2.
 *        Tuyệt đối không sử dụng Module 2 (TimeAligner).
 */

#include <iostream>
#include <iomanip>
#include <string>
#include <vector>
#include <atomic>
#include <chrono>
#include <thread>
#include <cmath>
#include <csignal>
#include <cstdlib>
#include <algorithm>
#include <stdexcept>
#include <type_traits>
#include <unistd.h>
#include <sys/wait.h>

#include <sdrplay_api.h>
#include "config.hpp"
#include "ring_buffer.hpp"
#include "correlator.hpp"
#include "telemetry_sender.hpp"

namespace {

// Cờ điều khiển vòng đời toàn cục cho cả tiến trình cha và tiến trình con
std::atomic<bool> g_running{true};

void signal_handler(int) noexcept {
    g_running.store(false, std::memory_order_relaxed);
}

void install_signal_handlers() {
    struct sigaction sa{};
    sa.sa_handler = signal_handler;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0;
    sigaction(SIGINT, &sa, nullptr);
    sigaction(SIGTERM, &sa, nullptr);
}

// SFINAE helper tương thích với các kiểu chữ ký hàm send_spectrum của TelemetrySender
template <typename S, typename = void>
struct has_send_4args : std::false_type {};

template <typename S>
struct has_send_4args<S, std::void_t<decltype(std::declval<S&>().send_spectrum(
    uint64_t{}, uint64_t{}, std::declval<const std::vector<float>&>(), uint32_t{}))>> : std::true_type {};

template <typename S, typename = void>
struct has_send_3args : std::false_type {};

template <typename S>
struct has_send_3args<S, std::void_t<decltype(std::declval<S&>().send_spectrum(
    uint64_t{}, uint64_t{}, std::declval<const std::vector<float>&>()))>> : std::true_type {};

template <typename Sender>
bool dispatch_telemetry(Sender& sender,
                        uint64_t frame_id,
                        uint64_t timestamp_us,
                        const std::vector<float>& spectrum_db,
                        uint32_t integration_frames) {
    if constexpr (has_send_4args<Sender>::value) {
        sender.send_spectrum(frame_id, timestamp_us, spectrum_db, integration_frames);
    } else if constexpr (has_send_3args<Sender>::value) {
        sender.send_spectrum(frame_id, timestamp_us, spectrum_db);
    } else {
        sender.send_spectrum(spectrum_db, frame_id, timestamp_us, integration_frames);
    }
    return true;
}

// Callback nhận luồng I/Q từ SDRplay Service API trong tiến trình con
void sdr_stream_callback(short* xi,
                         short* xq,
                         [[maybe_unused]] sdrplay_api_StreamCbParamsT* params,
                         unsigned int numSamples,
                         [[maybe_unused]] unsigned int reset,
                         void* cbContext) {
    auto* rb = static_cast<SharedMemoryRingBuffer*>(cbContext);
    if (rb == nullptr || numSamples == 0) {
        return;
    }
    if (!rb->is_stream_started()) {
        return;
    }
    rb->write(xi, xq, numSamples);
}

void sdr_event_callback([[maybe_unused]] sdrplay_api_EventT eventId,
                        [[maybe_unused]] sdrplay_api_TunerSelectT tuner,
                        [[maybe_unused]] sdrplay_api_EventParamsT* params,
                        [[maybe_unused]] void* cbContext) {
}

// Tiến trình con điều khiển độc lập từng máy thu RSPdx (Producer)
[[noreturn]] void run_sdr_producer(int ch_index,
                                   const std::string& target_serial,
                                   const std::string& shm_name) {
    install_signal_handlers();

    try {
        SharedMemoryRingBuffer producer_rb(shm_name, SolarConfig::SHM::BUFFER_CAPACITY, true);

        sdrplay_api_ErrT err = sdrplay_api_Open();
        if (err != sdrplay_api_Success) {
            std::cerr << "[SDR Ch" << ch_index << "] Lỗi sdrplay_api_Open: "
                      << sdrplay_api_GetErrorString(err) << "\n";
            _exit(1);
        }

        sdrplay_api_DeviceT devs[SDRPLAY_MAX_DEVICES];
        unsigned int num_devs = 0;
        err = sdrplay_api_GetDevices(devs, &num_devs, SDRPLAY_MAX_DEVICES);
        if (err != sdrplay_api_Success || num_devs == 0) {
            std::cerr << "[SDR Ch" << ch_index << "] Lỗi sdrplay_api_GetDevices.\n";
            sdrplay_api_Close();
            _exit(1);
        }

        int selected_idx = -1;
        for (unsigned int i = 0; i < num_devs; ++i) {
            if (target_serial == devs[i].SerNo) {
                selected_idx = static_cast<int>(i);
                break;
            }
        }

        if (selected_idx < 0) {
            std::cerr << "[SDR Ch" << ch_index << "] Không tìm thấy thiết bị có Serial: "
                      << target_serial << "\n";
            sdrplay_api_Close();
            _exit(1);
        }

        err = sdrplay_api_SelectDevice(&devs[selected_idx]);
        if (err != sdrplay_api_Success) {
            std::cerr << "[SDR Ch" << ch_index << "] Lỗi sdrplay_api_SelectDevice: "
                      << sdrplay_api_GetErrorString(err) << "\n";
            sdrplay_api_Close();
            _exit(1);
        }

        sdrplay_api_DeviceParamsT* device_params = nullptr;
        err = sdrplay_api_GetDeviceParams(devs[selected_idx].dev, &device_params);
        if (err != sdrplay_api_Success || device_params == nullptr) {
            std::cerr << "[SDR Ch" << ch_index << "] Lỗi sdrplay_api_GetDeviceParams: "
                      << sdrplay_api_GetErrorString(err) << "\n";
            sdrplay_api_ReleaseDevice(&devs[selected_idx]);
            sdrplay_api_Close();
            _exit(1);
        }

        // Cấu hình thông số RF & IF cho kênh thu A
        auto* ch_params = device_params->rxChannelA;
        ch_params->tunerParams.rfFreq.rfHz = 35.0e6;                     // 35.0 MHz
        ch_params->tunerParams.bwType      = sdrplay_api_BW_8_000;       // IF BW 8.0 MHz
        ch_params->ctrlParams.agc.enable   = sdrplay_api_AGC_DISABLE;    // Tắt AGC
        ch_params->tunerParams.gain.gRdB   = 30;                         // IF Gain Reduction 30 dB
        ch_params->tunerParams.gain.LNAstate = 4;                        // LNA State 4

        // Cấu hình Sample Rate 10.0 MSPS và cổng Anten C (BNC) cho RSPdx
        device_params->devParams->fsFreq.fsHz            = 10.0e6;
        device_params->devParams->rspDxParams.antennaSel = sdrplay_api_RspDx_ANTENNA_C;

        sdrplay_api_CallbackFnsT cb_fns{};
        cb_fns.StreamACbFn = sdr_stream_callback;
        cb_fns.EventCbFn   = sdr_event_callback;

        err = sdrplay_api_Init(devs[selected_idx].dev, &cb_fns, &producer_rb);
        if (err != sdrplay_api_Success) {
            std::cerr << "[SDR Ch" << ch_index << "] Lỗi sdrplay_api_Init: "
                      << sdrplay_api_GetErrorString(err) << "\n";
            sdrplay_api_ReleaseDevice(&devs[selected_idx]);
            sdrplay_api_Close();
            _exit(1);
        }

        producer_rb.set_ready(true);

        while (g_running.load(std::memory_order_relaxed)) {
            std::this_thread::sleep_for(std::chrono::milliseconds(25));
        }

        sdrplay_api_Uninit(devs[selected_idx].dev);
        sdrplay_api_ReleaseDevice(&devs[selected_idx]);
        sdrplay_api_Close();
        _exit(0);
    } catch (const std::exception& e) {
        std::cerr << "[SDR Ch" << ch_index << "] Ngoại lệ: " << e.what() << "\n";
        _exit(1);
    }
}

uint16_t parse_port(const char* arg, const char* name) {
    char* endptr = nullptr;
    long val = std::strtol(arg, &endptr, 10);
    if (endptr == arg || *endptr != '\0' || val <= 0 || val > 65535) {
        throw std::invalid_argument(std::string("Cổng UDP không hợp lệ cho ") + name + ": " + arg);
    }
    return static_cast<uint16_t>(val);
}

} // namespace

int main(int argc, char* argv[]) {
    install_signal_handlers();

    std::string dest_ip = SolarConfig::Network::UDP_DEST_IP;
    uint16_t port_ch1 = 9998;
    uint16_t port_ch2 = 9999;

    try {
        if (argc >= 2) {
            dest_ip = argv[1];
        }
        if (argc >= 3) {
            port_ch1 = parse_port(argv[2], "port_ch1");
        }
        if (argc >= 4) {
            port_ch2 = parse_port(argv[3], "port_ch2");
        }
        if (port_ch1 == port_ch2) {
            std::cerr << "[Lỗi] port_ch1 và port_ch2 bắt buộc phải khác nhau!\n";
            return 1;
        }
    } catch (const std::exception& e) {
        std::cerr << "[Lỗi CLI] " << e.what() << "\n"
                  << "Cách dùng: " << argv[0] << " [dest_ip] [port_ch1] [port_ch2]\n";
        return 1;
    }

    std::cout << "========================================================================\n"
              << " SOLAR CORRELATOR - DUAL SDR LIVE WATERFALL DIAGNOSTIC (AUTO-POWER)\n"
              << "========================================================================\n"
              << " + Đích UDP Telemetry : " << dest_ip
              << " (Ch1 Port: " << port_ch1 << " | Ch2 Port: " << port_ch2 << ")\n"
              << " + Cấu hình F-Engine  : N = " << SolarConfig::Correlator::FFT_SIZE
              << " bins, M = " << SolarConfig::Correlator::INTEGRATION_FRAMES
              << " frames (~200 ms, Welch 50%)\n"
              << " + Chế độ đo          : S11 & S22 độc lập (Bỏ qua TimeAligner Module 2)\n"
              << "========================================================================\n";

    // 1. Quét bus USB lấy Serial của 2 thiết bị RSPdx trước khi fork()
    sdrplay_api_ErrT err = sdrplay_api_Open();
    if (err != sdrplay_api_Success) {
        std::cerr << "[Lỗi] Không thể mở SDRplay Service API: "
                  << sdrplay_api_GetErrorString(err) << "\n";
        return 1;
    }

    sdrplay_api_DeviceT devs[SDRPLAY_MAX_DEVICES];
    unsigned int num_devs = 0;
    err = sdrplay_api_GetDevices(devs, &num_devs, SDRPLAY_MAX_DEVICES);
    if (err != sdrplay_api_Success || num_devs < 2) {
        std::cerr << "[Lỗi] Yêu cầu tối thiểu 2 máy thu RSPdx! Số thiết bị tìm thấy: "
                  << num_devs << "\n";
        sdrplay_api_Close();
        return 1;
    }

    const std::string ser0 = devs[0].SerNo;
    const std::string ser1 = devs[1].SerNo;
    sdrplay_api_Close(); // Đóng API ở tiến trình cha trước khi fork()

    std::cout << "[Hardware] Phát hiện 2 máy thu SDRplay RSPdx:\n"
              << "  -> Kênh 1 (S11, UDP " << port_ch1 << ") : Serial " << ser0 << "\n"
              << "  -> Kênh 2 (S22, UDP " << port_ch2 << ") : Serial " << ser1 << "\n";

    // 2. Dọn sạch bộ nhớ chia sẻ cũ trước khi khởi tạo
    SharedMemoryRingBuffer::unlink_shm(SolarConfig::SHM::CH1_NAME);
    SharedMemoryRingBuffer::unlink_shm(SolarConfig::SHM::CH2_NAME);

    // 3. Fork 2 tiến trình con thu mẫu độc lập
    pid_t pid1 = fork();
    if (pid1 < 0) {
        std::cerr << "[Lỗi] fork() thất bại cho Kênh 1.\n";
        return 1;
    }
    if (pid1 == 0) {
        run_sdr_producer(1, ser0, SolarConfig::SHM::CH1_NAME);
    }

    pid_t pid2 = fork();
    if (pid2 < 0) {
        std::cerr << "[Lỗi] fork() thất bại cho Kênh 2.\n";
        kill(pid1, SIGTERM);
        waitpid(pid1, nullptr, 0);
        SharedMemoryRingBuffer::unlink_shm(SolarConfig::SHM::CH1_NAME);
        return 1;
    }
    if (pid2 == 0) {
        run_sdr_producer(2, ser1, SolarConfig::SHM::CH2_NAME);
    }

    int exit_code = 0;

    // 4. Tiến trình cha: DSP F-Engine & Dual-Port SOL2 Telemetry
    try {
        const auto wait_deadline = std::chrono::steady_clock::now() + std::chrono::seconds(10);

        std::unique_ptr<SharedMemoryRingBuffer> rb1_ptr;
        std::unique_ptr<SharedMemoryRingBuffer> rb2_ptr;

        // Chờ tiến trình con tạo xong vùng nhớ SHM
        while (g_running.load(std::memory_order_relaxed)) {
            try {
                if (!rb1_ptr) {
                    rb1_ptr = std::make_unique<SharedMemoryRingBuffer>(
                        SolarConfig::SHM::CH1_NAME, SolarConfig::SHM::BUFFER_CAPACITY, false);
                }
                if (!rb2_ptr) {
                    rb2_ptr = std::make_unique<SharedMemoryRingBuffer>(
                        SolarConfig::SHM::CH2_NAME, SolarConfig::SHM::BUFFER_CAPACITY, false);
                }
                if (rb1_ptr && rb2_ptr) {
                    break;
                }
            } catch (const std::exception&) {
                if (std::chrono::steady_clock::now() >= wait_deadline) {
                    throw std::runtime_error("Quá thời gian chờ mở vùng nhớ SHM từ tiến trình con.");
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(20));
            }
        }

        SharedMemoryRingBuffer& rb1 = *rb1_ptr;
        SharedMemoryRingBuffer& rb2 = *rb2_ptr;

        std::cout << "[Parent DSP] Đang chờ cả 2 RSPdx hoàn tất sdrplay_api_Init()...\n";
        while (g_running.load(std::memory_order_relaxed) && (!rb1.is_ready() || !rb2.is_ready())) {
            int status = 0;
            if (waitpid(pid1, &status, WNOHANG) > 0 || waitpid(pid2, &status, WNOHANG) > 0) {
                throw std::runtime_error("Một tiến trình con SDRplay đã dừng đột ngột khi khởi tạo.");
            }
            if (std::chrono::steady_clock::now() >= wait_deadline) {
                throw std::runtime_error("Quá thời gian chờ (Timeout) phần cứng SDRplay sẵn sàng.");
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(25));
        }

        if (g_running.load(std::memory_order_relaxed)) {
            // Bật đồng thời cổng đẩy mẫu trên cả 2 kênh
            rb1.set_start_stream(true);
            rb2.set_start_stream(true);

            // Chờ 250 ms cho luồng DMA ổn định rồi xả sạch dữ liệu quá độ ban đầu
            std::this_thread::sleep_for(std::chrono::milliseconds(250));
            rb1.flush();
            rb2.flush();

            const uint32_t fft_size = SolarConfig::Correlator::FFT_SIZE;
            const uint32_t int_frames = SolarConfig::Correlator::INTEGRATION_FRAMES;

            FXCorrelator::Options fx_opts{};
            fx_opts.fft_size = fft_size;
            fx_opts.frames   = int_frames;
            fx_opts.running  = &g_running;

            FXCorrelator fx(fx_opts);
            TelemetrySender sender_ch1(dest_ip, port_ch1);
            TelemetrySender sender_ch2(dest_ip, port_ch2);

            std::vector<float> zero_phi(fft_size, 0.0f);
            std::vector<float> dummy_db(fft_size, 0.0f);
            std::vector<float> dummy_phase(fft_size, 0.0f);
            std::vector<float> dummy_coh(fft_size, 0.0f);

            std::vector<float> s11_db(fft_size, 0.0f);
            std::vector<float> s22_db(fft_size, 0.0f);

            std::cout << "[Parent DSP] Đang nạp gối đầu bước đầu (prime_buffers)...\n";
            fx.prime_buffers(rb1, rb2);
            std::cout << "[Parent DSP] Bắt đầu phát phổ S11 & S22 thời gian thực (Nhấn Ctrl+C để dừng).\n\n";

            uint64_t frame_id = 0;
            uint64_t udp_ok_ch1 = 0;
            uint64_t udp_ok_ch2 = 0;

            while (g_running.load(std::memory_order_relaxed)) {
                try {
                    fx.process_integration_cycle(rb1, rb2, zero_phi, dummy_db, dummy_phase, dummy_coh);
                } catch (const std::exception& e) {
                    if (!g_running.load(std::memory_order_relaxed)) {
                        break;
                    }
                    std::cerr << "\n[Cảnh báo DSP] " << e.what()
                              << " -> Đang thực hiện flush() và prime_buffers() để tự phục hồi...\n";
                    rb1.flush();
                    rb2.flush();
                    fx.prime_buffers(rb1, rb2);
                    continue;
                }

                const std::vector<double>& p1 = fx.auto_power1();
                const std::vector<double>& p2 = fx.auto_power2();

                for (uint32_t k = 0; k < fft_size; ++k) {
                    s11_db[k] = static_cast<float>(10.0 * std::log10(std::max(p1[k], 1e-12)));
                    s22_db[k] = static_cast<float>(10.0 * std::log10(std::max(p2[k], 1e-12)));
                }

                const auto now_sys = std::chrono::system_clock::now();
                const uint64_t timestamp_us = static_cast<uint64_t>(
                    std::chrono::duration_cast<std::chrono::microseconds>(
                        now_sys.time_since_epoch()).count());

                ++frame_id;

                if (dispatch_telemetry(sender_ch1, frame_id, timestamp_us, s11_db, int_frames)) {
                    ++udp_ok_ch1;
                }
                if (dispatch_telemetry(sender_ch2, frame_id, timestamp_us, s22_db, int_frames)) {
                    ++udp_ok_ch2;
                }

                // Định kỳ mỗi 25 khung (~5 giây) in dòng trạng thái tóm tắt
                if (frame_id % 25 == 0) {
                    auto it1 = std::max_element(s11_db.begin(), s11_db.end());
                    auto it2 = std::max_element(s22_db.begin(), s22_db.end());
                    const int idx1 = static_cast<int>(std::distance(s11_db.begin(), it1));
                    const int idx2 = static_cast<int>(std::distance(s22_db.begin(), it2));

                    const double freq1_mhz = 30.0 + static_cast<double>(idx1) * (10.0 / fft_size);
                    const double freq2_mhz = 30.0 + static_cast<double>(idx2) * (10.0 / fft_size);

                    const uint32_t buf1_k = rb1.available_read() / 1024;
                    const uint32_t buf2_k = rb2.available_read() / 1024;
                    const uint32_t drop1  = rb1.get_overflow_count();
                    const uint32_t drop2  = rb2.get_overflow_count();

                    std::cout << std::fixed
                              << "[Frame #" << std::setw(5) << frame_id << "] "
                              << "Ch1 Peak: " << std::setw(5) << std::setprecision(1) << *it1
                              << " dB @ " << std::setprecision(3) << freq1_mhz << " MHz | "
                              << "Ch2 Peak: " << std::setw(5) << std::setprecision(1) << *it2
                              << " dB @ " << std::setprecision(3) << freq2_mhz << " MHz | "
                              << "Buf: " << buf1_k << "k/" << buf2_k << "k | "
                              << "Drops: " << drop1 << "/" << drop2 << " | "
                              << "UDP Ok: " << udp_ok_ch1 << "/" << udp_ok_ch2 << "\n";
                }
            }
        }
    } catch (const std::exception& e) {
        std::cerr << "\n[Lỗi Hệ thống] " << e.what() << "\n";
        exit_code = 1;
    }

    // 5. Dọn dẹp tiến trình con và vùng nhớ chia sẻ POSIX SHM
    std::cout << "\n[Cleanup] Đang dừng 2 tiến trình SDRplay và thu hồi bộ nhớ SHM...\n";
    kill(pid1, SIGTERM);
    kill(pid2, SIGTERM);

    int status1 = 0;
    int status2 = 0;
    waitpid(pid1, &status1, 0);
    waitpid(pid2, &status2, 0);

    SharedMemoryRingBuffer::unlink_shm(SolarConfig::SHM::CH1_NAME);
    SharedMemoryRingBuffer::unlink_shm(SolarConfig::SHM::CH2_NAME);

    std::cout << "[Cleanup] Đã kết thúc an toàn.\n";
    return exit_code;
}