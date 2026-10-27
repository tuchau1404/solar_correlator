#include <algorithm>
#include <arpa/inet.h>
#include <array>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstdint>
#include <cstdlib>
#include <cstdio>
#include <cstring>
#include <exception>
#include <fcntl.h>
#include <iomanip>
#include <iostream>
#include <limits>
#include <memory>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <thread>
#include <type_traits>
#include <unistd.h>
#include <vector>

#include "sdrplay_api.h"
#include "config.hpp"
#include "hardware_control.hpp"
#include "ring_buffer.hpp"
#include "time_alignment.hpp"
#include "startup_alignment.hpp"
#include "runtime_trace.hpp"
#include "correlator.hpp"
#include "telemetry_sender.hpp"
#ifdef SOLAR_STORAGE_ENABLED
#include "storage/storage_pipeline.hpp"
#endif

namespace {

// ============================================================================
// 1. BIẾN TÍN HIỆU TOÀN CỤC (ASYNC-SIGNAL-SAFE)
// ============================================================================
std::atomic<bool> g_running{true};
volatile sig_atomic_t g_last_signal = 0;

std::atomic<bool> g_child_running{true};

extern "C" void parent_signal_handler(int sig) noexcept {
    g_last_signal = sig;
    g_running.store(false, std::memory_order_relaxed);
}

extern "C" void child_signal_handler(int) noexcept {
    g_child_running.store(false, std::memory_order_relaxed);
}

bool install_signal_handlers(void (*handler)(int) noexcept) noexcept {
    struct sigaction sa {};
    sa.sa_handler = handler;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0;

    if (sigaction(SIGINT, &sa, nullptr) != 0) return false;
    if (sigaction(SIGTERM, &sa, nullptr) != 0) return false;

    struct sigaction sa_ign {};
    sa_ign.sa_handler = SIG_IGN;
    sigemptyset(&sa_ign.sa_mask);
    sa_ign.sa_flags = 0;
    (void)sigaction(SIGPIPE, &sa_ign, nullptr);
    return true;
}

// ============================================================================
// 2. MÁY TRẠNG THÁI HỆ THỐNG & CẤU HÌNH THAM SỐ CLI
// ============================================================================
enum class SystemState {
    STARTUP,
    ALIGNING,
    UNALIGNED,
    CALIBRATING,
    RUNNING,
    RECOVERING,
    STOPPING
};

const char* state_to_string(SystemState st, bool aligned_lock, bool phase_calibrated) noexcept {
    switch (st) {
        case SystemState::STARTUP:     return "STARTUP";
        case SystemState::ALIGNING:    return "ALIGNING";
        case SystemState::UNALIGNED:   return "UNALIGNED";
        case SystemState::CALIBRATING: return "CALIBRATING";
        case SystemState::RUNNING:
            if (!aligned_lock) return "UNALIGNED";
            return phase_calibrated ? "ALIGNED+CAL" : "ALIGNED";
        case SystemState::RECOVERING:  return "RECOVERING";
        case SystemState::STOPPING:    return "STOPPING";
    }
    return "UNKNOWN";
}

struct CliOptions {
    std::string dest_ip      = SolarConfig::Network::UDP_DEST_IP;
    uint16_t port_s12        = SolarConfig::Network::UDP_PORT_S12;
    uint16_t port_s11        = SolarConfig::Network::UDP_PORT_S11;
    uint16_t port_s22        = SolarConfig::Network::UDP_PORT_S22;
    uint16_t port_phase = SolarConfig::Network::UDP_PORT_PHASE;
    uint16_t port_coherence = SolarConfig::Network::UDP_PORT_COHERENCE;
    bool skip_align          = false;
    bool cal_phase           = false;
    std::string observation_source = "antenna";
    int max_align_retries    = 8;
    std::string serial_ch1   = "";
    std::string serial_ch2   = "";
    bool show_help           = false;
    std::string storage_config;
    bool check_config = false;
};

void print_usage(const char* prog_name) {
    std::cout
        << "Sử dụng: " << prog_name << " [TÙY CHỌN]\n\n"
        << "Chương trình điều phối trung tâm Giao thoa kế Vô tuyến Mặt Trời (30.0 - 40.0 MHz)\n"
        << "Chuỗi xử lý: Dual SDRplay RSPdx -> POSIX SHM & Time Alignment -> Welch FX Correlator -> Multi-Port SOL2 v3 UDP\n\n"
        << "Tùy chọn dòng lệnh:\n"
        << "  --dest-ip <ip>            Địa chỉ IPv4 nhận Telemetry UDP (mặc định: " << SolarConfig::Network::UDP_DEST_IP << ")\n"
        << "  --port-s12 <1..65535>     Cổng UDP chính phát phổ |S12| dB (mặc định: " << SolarConfig::Network::UDP_PORT_S12 << ")\n"
        << "  --port-s11 <0..65535>     Cổng UDP phát phổ tự thân S11 dB; 0 để tắt (mặc định: " << SolarConfig::Network::UDP_PORT_S11 << ")\n"
        << "  --port-s22 <0..65535>     Cổng UDP phát phổ tự thân S22 dB; 0 để tắt (mặc định: " << SolarConfig::Network::UDP_PORT_S22 << ")\n"
        << "  --port-phase <0..65535>   Phase rad, default 9996; 0 disables\n"
        << "  --port-coherence <0..65535> Coherence amplitude gamma, default 9995; 0 disables\n"
        << "  --skip-align, --no-align  Bỏ qua căn chỉnh trễ mẫu Module 2 (chế độ chẩn đoán CW / tải giả 50 Ohm)\n"
        << "  --input-source <antenna|noise>  Observation source (default: antenna); selected after alignment/calibration\n"
        << "  --cal-phase               Chụp và bù pha thiết bị tĩnh (phi_rad) ngay sau khi khóa mẫu k = 0\n"
        << "  --max-align-retries <n>   Số lần thử căn chỉnh tối đa ở Module 2 (mặc định: 8)\n"
        << "  --serial-ch1 <ser>        Chỉ định cứng Serial Number RSPdx cho Kênh 1\n"
        << "  --serial-ch2 <ser>        Chỉ định cứng Serial Number RSPdx cho Kênh 2\n"
        << "  --config <path>          Enable configured storage/startup pipeline\n"
        << "  --check-config           Validate config and estimate offline, without SDR/Drive\n"
        << "  -h, --help                Hiển thị hướng dẫn này và thoát\n";
}

bool parse_port_value(const std::string& str, bool allow_zero, uint16_t& out_port) {
    if (str.empty()) return false;
    char* endptr = nullptr;
    errno = 0;
    long val = std::strtol(str.c_str(), &endptr, 10);
    if (errno != 0 || endptr == str.c_str() || *endptr != '\0') {
        return false;
    }
    const long min_val = allow_zero ? 0L : 1L;
    if (val < min_val || val > 65535L) {
        return false;
    }
    out_port = static_cast<uint16_t>(val);
    return true;
}

bool parse_positive_int(const std::string& str, int& out_val) {
    if (str.empty()) return false;
    char* endptr = nullptr;
    errno = 0;
    long val = std::strtol(str.c_str(), &endptr, 10);
    if (errno != 0 || endptr == str.c_str() || *endptr != '\0' || val < 1L || val > 1000L) {
        return false;
    }
    out_val = static_cast<int>(val);
    return true;
}

bool parse_and_validate_cli(int argc, char* argv[], CliOptions& opts, std::string& err_msg) {
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        auto require_next = [&](const std::string& flag) -> const char* {
            if (i + 1 >= argc) {
                err_msg = "Thiếu giá trị cho tham số " + flag;
                return nullptr;
            }
            return argv[++i];
        };

        if (arg == "-h" || arg == "--help") {
            opts.show_help = true;
            return true;
        } else if (arg == "--config") {
            const char* v = require_next(arg); if (!v) return false;
            opts.storage_config = v;
        } else if (arg == "--check-config") {
            opts.check_config = true;
        } else if (arg == "--dest-ip") {
            const char* v = require_next(arg);
            if (!v) return false;
            opts.dest_ip = v;
        } else if (arg == "--port-s12") {
            const char* v = require_next(arg);
            if (!v) return false;
            if (!parse_port_value(v, false, opts.port_s12)) {
                err_msg = "Giá trị --port-s12 không hợp lệ (bắt buộc trong khoảng 1..65535): " + std::string(v);
                return false;
            }
        } else if (arg == "--port-s11") {
            const char* v = require_next(arg);
            if (!v) return false;
            if (!parse_port_value(v, true, opts.port_s11)) {
                err_msg = "Giá trị --port-s11 không hợp lệ (bắt buộc 0 hoặc 1..65535): " + std::string(v);
                return false;
            }
        } else if (arg == "--port-s22") {
            const char* v = require_next(arg);
            if (!v) return false;
            if (!parse_port_value(v, true, opts.port_s22)) {
                err_msg = "Giá trị --port-s22 không hợp lệ (bắt buộc 0 hoặc 1..65535): " + std::string(v);
                return false;
            }
        } else if (arg == "--port-phase" || arg == "--port-coherence") {
            const char* v = require_next(arg);
            if (!v) return false;
            auto& port = arg == "--port-phase" ? opts.port_phase : opts.port_coherence;
            if (!parse_port_value(v, true, port)) {
                err_msg = "Invalid port (0..65535): " + arg;
                return false;
            }
        } else if (arg == "--port-aux" || arg == "--aux-mode") {
            err_msg = "SOL2 v3: replace --port-aux/--aux-mode with --port-phase and --port-coherence";
            return false;
        } else if (arg == "--skip-align" || arg == "--no-align") {
            opts.skip_align = true;
        } else if (arg == "--input-source") {
            const char* v = require_next(arg);
            if (!v) return false;
            opts.observation_source = v;
            if (opts.observation_source != "antenna" && opts.observation_source != "noise") {
                err_msg = "--input-source must be antenna or noise";
                return false;
            }
        } else if (arg == "--cal-phase") {
            opts.cal_phase = true;
        } else if (arg == "--max-align-retries") {
            const char* v = require_next(arg);
            if (!v) return false;
            if (!parse_positive_int(v, opts.max_align_retries)) {
                err_msg = "Giá trị --max-align-retries phải là số nguyên dương (1..1000): " + std::string(v);
                return false;
            }
        } else if (arg == "--serial-ch1") {
            const char* v = require_next(arg);
            if (!v) return false;
            opts.serial_ch1 = v;
        } else if (arg == "--serial-ch2") {
            const char* v = require_next(arg);
            if (!v) return false;
            opts.serial_ch2 = v;
        } else {
            err_msg = "Tham số dòng lệnh không xác định: " + arg;
            return false;
        }
    }

    if (opts.check_config && opts.storage_config.empty()) {
        err_msg = "--check-config requires --config <path>"; return false;
    }
#ifndef SOLAR_STORAGE_ENABLED
    if (!opts.storage_config.empty()) { err_msg = "Rebuild with SOLAR_BUILD_STORAGE=ON"; return false; }
#endif
    struct in_addr ipv4_addr {};
    if (inet_pton(AF_INET, opts.dest_ip.c_str(), &ipv4_addr) != 1) {
        err_msg = "Địa chỉ IPv4 đích không hợp lệ (--dest-ip): " + opts.dest_ip;
        return false;
    }

    if (opts.skip_align && opts.cal_phase) {
        err_msg = "Không thể bật đồng thời --skip-align (--no-align) và --cal-phase (hiệu chuẩn pha đòi hỏi đã khóa mẫu k = 0).";
        return false;
    }

    std::vector<std::pair<std::string, uint16_t>> active_ports;
    active_ports.emplace_back("--port-s12", opts.port_s12);
    if (opts.port_s11 > 0) active_ports.emplace_back("--port-s11", opts.port_s11);
    if (opts.port_s22 > 0) active_ports.emplace_back("--port-s22", opts.port_s22);
    if (opts.port_phase > 0) active_ports.emplace_back("--port-phase", opts.port_phase);
    if (opts.port_coherence > 0) active_ports.emplace_back("--port-coherence", opts.port_coherence);

    for (size_t i = 0; i < active_ports.size(); ++i) {
        for (size_t j = i + 1; j < active_ports.size(); ++j) {
            if (active_ports[i].second == active_ports[j].second) {
                err_msg = "Xung đột cổng UDP: " + active_ports[i].first + " và " +
                          active_ports[j].first + " cùng trỏ vào cổng " +
                          std::to_string(active_ports[i].second);
                return false;
            }
        }
    }

    const bool has_s1 = !opts.serial_ch1.empty();
    const bool has_s2 = !opts.serial_ch2.empty();
    if (has_s1 != has_s2) {
        err_msg = "Khi chỉ định Serial Number thủ công, bắt buộc phải truyền cả --serial-ch1 và --serial-ch2.";
        return false;
    }
    if (has_s1 && opts.serial_ch1 == opts.serial_ch2) {
        err_msg = "Serial Number của Kênh 1 và Kênh 2 phải khác nhau (--serial-ch1 != --serial-ch2).";
        return false;
    }

    return true;
}

// ============================================================================
// 3. KHÓA PHIÊN DUY NHẤT (SINGLE-INSTANCE FILE LOCK RAII)
// ============================================================================
class SingleInstanceLock {
public:
    explicit SingleInstanceLock(const std::string& lock_path) : lock_path_(lock_path) {}

    SingleInstanceLock(const SingleInstanceLock&) = delete;
    SingleInstanceLock& operator=(const SingleInstanceLock&) = delete;

    bool acquire(std::string& err_msg) {
        fd_ = ::open(lock_path_.c_str(), O_CREAT | O_RDWR | O_CLOEXEC, 0666);
        if (fd_ < 0) {
            err_msg = "Không thể mở lock file " + lock_path_ + ": " + std::strerror(errno);
            return false;
        }
        if (::flock(fd_, LOCK_EX | LOCK_NB) != 0) {
            const int e = errno;
            ::close(fd_);
            fd_ = -1;
            err_msg = "Đã có một phiên solar_correlator hoặc bài test phần cứng khác đang giữ khóa (" +
                      lock_path_ + ": " + std::strerror(e) + "). Từ chối khởi chạy để bảo vệ SHM!";
            return false;
        }
        acquired_ = true;
        const std::string pid_str = std::to_string(::getpid()) + "\n";
        if (::ftruncate(fd_, 0) != 0 ||
            ::write(fd_, pid_str.data(), pid_str.size()) != static_cast<ssize_t>(pid_str.size())) {
            err_msg = "Unable to record lock owner PID";
            release();
            return false;
        }
        return true;
    }

    ~SingleInstanceLock() {
        release();
    }

    void close_in_child() noexcept {
        if (fd_ >= 0) ::close(fd_);
        fd_ = -1;
        acquired_ = false;
    }

    void release() noexcept {
        if (fd_ >= 0) {
            if (acquired_) {
                // Keep the inode: unlink would permit a second independent lock.
                (void)::flock(fd_, LOCK_UN);
            }
            ::close(fd_);
            fd_ = -1;
            acquired_ = false;
        }
    }

private:
    std::string lock_path_;
    int fd_{-1};
    bool acquired_{false};
};

// ============================================================================
// 4. BỘ CẦU NỐI TƯƠNG THÍCH CHỮ KÝ API (C++17 ZERO-OVERHEAD TRAITS)
// ============================================================================
namespace api_bridge {

inline std::unique_ptr<SharedMemoryRingBuffer> create_ring_buffer(
    const std::string& name, uint32_t capacity, bool producer) {
    return std::make_unique<SharedMemoryRingBuffer>(name, capacity, producer);
}

template <typename CorrelatorT>
bool invoke_prime_buffers(CorrelatorT& fx, SharedMemoryRingBuffer& rb1, SharedMemoryRingBuffer& rb2) {
    using RetT = decltype(fx.prime_buffers(rb1, rb2));
    if constexpr (std::is_convertible_v<RetT, bool>) {
        return static_cast<bool>(fx.prime_buffers(rb1, rb2));
    } else {
        fx.prime_buffers(rb1, rb2);
        return g_running.load(std::memory_order_relaxed);
    }
}

template <typename CorrelatorT>
bool invoke_integration_cycle(CorrelatorT& fx,
                              SharedMemoryRingBuffer& rb1,
                              SharedMemoryRingBuffer& rb2,
                              const std::vector<float>& phi_rad,
                              std::vector<float>& s12_db,
                              std::vector<float>& phase_rad,
                              std::vector<float>& coherence) {
    using RetT = decltype(fx.process_integration_cycle(rb1, rb2, phi_rad, s12_db, phase_rad, coherence));
    if constexpr (std::is_convertible_v<RetT, bool>) {
        return static_cast<bool>(
            fx.process_integration_cycle(rb1, rb2, phi_rad, s12_db, phase_rad, coherence));
    } else {
        fx.process_integration_cycle(rb1, rb2, phi_rad, s12_db, phase_rad, coherence);
        return g_running.load(std::memory_order_relaxed);
    }
}

struct PortTelemetryStats {
    uint64_t frames_complete{0};
    uint64_t frames_partial{0};
    uint64_t send_errors{0};
    uint64_t chunks_sent_total{0};
};

inline void accumulate_telemetry_result(const TelemetrySender::Result& res,
                                        PortTelemetryStats& st) noexcept {
    st.chunks_sent_total += res.datagrams;
    if (res.queued) ++st.frames_complete;
    else {
        ++st.send_errors;
        if (res.datagrams > 0) ++st.frames_partial;
    }
}

template <typename SenderT>
void dispatch_udp_spectrum(SenderT& sender,
                           uint64_t frame_id,
                           uint64_t timestamp_us,
                           const std::vector<float>& payload,
                           PortTelemetryStats& stats) noexcept {
    auto res = sender.send_spectrum(frame_id, timestamp_us, payload);
    accumulate_telemetry_result(res, stats);
}

} // namespace api_bridge

// ============================================================================
// 5. TIẾN TRÌNH CON THU NHẬN MẪU SDRPLAY RSPDX (MODULE 1 PRODUCER)
// ============================================================================
struct StreamContext {
    SharedMemoryRingBuffer* ring;
    uint32_t expected_sample{0};
    bool have_expected{false};
};
void sdr_stream_callback(short* xi, short* xq,
                         sdrplay_api_StreamCbParamsT* params,
                         unsigned int numSamples, unsigned int reset, void* cbContext) {
    auto* ctx = static_cast<StreamContext*>(cbContext);
    if (!ctx || !ctx->ring || !g_child_running.load(std::memory_order_relaxed)) return;
    auto& rb = *ctx->ring;
    if (!rb.is_stream_started()) return;
    auto fail = [&] { rb.set_ready(false); g_child_running.store(false, std::memory_order_relaxed); };
    if (reset || !params) { fail(); return; }
    if (!numSamples) return;
    if (ctx->have_expected && params->firstSampleNum != ctx->expected_sample) { fail(); return; }
    ctx->expected_sample = static_cast<uint32_t>(params->firstSampleNum + numSamples);
    ctx->have_expected = true;
    try { if (rb.write(xi, xq, numSamples) != numSamples) fail(); }
    catch (...) { fail(); }
}

void sdr_event_callback([[maybe_unused]] sdrplay_api_EventT eventId,
                        [[maybe_unused]] sdrplay_api_TunerSelectT tuner,
                        [[maybe_unused]] sdrplay_api_EventParamsT* params,
                        [[maybe_unused]] void* cbContext) {}

[[noreturn]] void run_sdr_producer_process(int channel_number,
                                           const std::string& target_serial,
                                           const std::string& shm_name) {
    (void)install_signal_handlers(child_signal_handler);

    std::unique_ptr<SharedMemoryRingBuffer> producer_rb;
    try {
        producer_rb = api_bridge::create_ring_buffer(
            shm_name, SolarConfig::SHM::BUFFER_CAPACITY, true);
    } catch (const std::exception& e) {
        std::cerr << "[Producer Ch" << channel_number << "] Lỗi khởi tạo SHM ("
                  << shm_name << "): " << e.what() << "\n";
        _exit(1);
    }

    sdrplay_api_ErrT err = sdrplay_api_Open();
    if (err != sdrplay_api_Success) {
        std::cerr << "[Producer Ch" << channel_number << "] Lỗi sdrplay_api_Open: "
                  << sdrplay_api_GetErrorString(err) << "\n";
        _exit(1);
    }

    (void)sdrplay_api_LockDeviceApi();

    sdrplay_api_DeviceT devs[SDRPLAY_MAX_DEVICES];
    unsigned int num_devs = 0;
    err = sdrplay_api_GetDevices(devs, &num_devs, SDRPLAY_MAX_DEVICES);
    if (err != sdrplay_api_Success || num_devs == 0) {
        std::cerr << "[Producer Ch" << channel_number << "] Lỗi sdrplay_api_GetDevices: "
                  << sdrplay_api_GetErrorString(err) << "\n";
        (void)sdrplay_api_UnlockDeviceApi();
        (void)sdrplay_api_Close();
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
        std::cerr << "[Producer Ch" << channel_number << "] Không tìm thấy RSPdx Serial: "
                  << target_serial << "\n";
        (void)sdrplay_api_UnlockDeviceApi();
        (void)sdrplay_api_Close();
        _exit(1);
    }

    devs[selected_idx].tuner = sdrplay_api_Tuner_A;
    err = sdrplay_api_SelectDevice(&devs[selected_idx]);
    (void)sdrplay_api_UnlockDeviceApi();

    if (err != sdrplay_api_Success) {
        std::cerr << "[Producer Ch" << channel_number << "] Lỗi sdrplay_api_SelectDevice ("
                  << target_serial << "): " << sdrplay_api_GetErrorString(err) << "\n";
        (void)sdrplay_api_Close();
        _exit(1);
    }

    sdrplay_api_DeviceParamsT* device_params = nullptr;
    err = sdrplay_api_GetDeviceParams(devs[selected_idx].dev, &device_params);
    if (err != sdrplay_api_Success || device_params == nullptr ||
        device_params->rxChannelA == nullptr || device_params->devParams == nullptr) {
        std::cerr << "[Producer Ch" << channel_number << "] Lỗi sdrplay_api_GetDeviceParams: "
                  << sdrplay_api_GetErrorString(err) << "\n";
        (void)sdrplay_api_ReleaseDevice(&devs[selected_idx]);
        (void)sdrplay_api_Close();
        _exit(1);
    }

    auto* ch_params = device_params->rxChannelA;
    ch_params->tunerParams.rfFreq.rfHz   = SolarConfig::SDR::RF_CENTER_FREQ_HZ;
    ch_params->tunerParams.bwType        = SolarConfig::SDR::IF_BW;
    ch_params->tunerParams.ifType        = sdrplay_api_IF_Zero;
    ch_params->ctrlParams.agc.enable     = sdrplay_api_AGC_DISABLE;
    ch_params->tunerParams.gain.gRdB     = SolarConfig::SDR::GAIN_GRDB;
    ch_params->tunerParams.gain.LNAstate = static_cast<unsigned char>(SolarConfig::SDR::LNA_STATE);

    device_params->devParams->fsFreq.fsHz                  = SolarConfig::SDR::SAMPLE_RATE_HZ;
    device_params->devParams->rspDxParams.antennaSel       = SolarConfig::SDR::ANTENNA_PORT;
    device_params->devParams->rspDxParams.rfNotchEnable    = 0;
    device_params->devParams->rspDxParams.rfDabNotchEnable = 0;

    sdrplay_api_CallbackFnsT cb_fns {};
    cb_fns.StreamACbFn = sdr_stream_callback;
    cb_fns.EventCbFn   = sdr_event_callback;

    StreamContext stream_context{producer_rb.get()};
    err = sdrplay_api_Init(devs[selected_idx].dev, &cb_fns, &stream_context);
    if (err != sdrplay_api_Success) {
        std::cerr << "[Producer Ch" << channel_number << "] Lỗi sdrplay_api_Init ("
                  << target_serial << "): " << sdrplay_api_GetErrorString(err) << "\n";
        (void)sdrplay_api_ReleaseDevice(&devs[selected_idx]);
        (void)sdrplay_api_Close();
        _exit(1);
    }

    producer_rb->set_ready(true);

    while (g_child_running.load(std::memory_order_relaxed)) {
        std::this_thread::sleep_for(std::chrono::milliseconds(25));
    }

    (void)sdrplay_api_Uninit(devs[selected_idx].dev);
    (void)sdrplay_api_ReleaseDevice(&devs[selected_idx]);
    (void)sdrplay_api_Close();
    _exit(0);
}

// ============================================================================
// 6. QUẢN LÝ TIẾN TRÌNH CON & DỌN DẸP TÀI NGUYÊN (RAII SUPERVISOR)
// ============================================================================
struct ChildProcessGuard {
    pid_t pid1{-1};
    pid_t pid2{-1};

    bool check_children_alive(std::string& err_msg) noexcept {
        if (pid1 > 0) {
            int st = 0;
            pid_t r = ::waitpid(pid1, &st, WNOHANG);
            if (r == pid1) {
                pid1 = -1;
                err_msg = "Tiến trình Producer Kênh 1 đã dừng bất ngờ (exit/signal status = " +
                          std::to_string(st) + ").";
                return false;
            }
        }
        if (pid2 > 0) {
            int st = 0;
            pid_t r = ::waitpid(pid2, &st, WNOHANG);
            if (r == pid2) {
                pid2 = -1;
                err_msg = "Tiến trình Producer Kênh 2 đã dừng bất ngờ (exit/signal status = " +
                          std::to_string(st) + ").";
                return false;
            }
        }
        return true;
    }

    void terminate_and_reap_all() noexcept {
        if (pid1 > 0) (void)::kill(pid1, SIGTERM);
        if (pid2 > 0) (void)::kill(pid2, SIGTERM);

        const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(3000);
        while ((pid1 > 0 || pid2 > 0) && std::chrono::steady_clock::now() < deadline) {
            if (pid1 > 0) {
                int st = 0;
                if (::waitpid(pid1, &st, WNOHANG) == pid1) pid1 = -1;
            }
            if (pid2 > 0) {
                int st = 0;
                if (::waitpid(pid2, &st, WNOHANG) == pid2) pid2 = -1;
            }
            if (pid1 <= 0 && pid2 <= 0) break;
            std::this_thread::sleep_for(std::chrono::milliseconds(25));
        }

        if (pid1 > 0) {
            (void)::kill(pid1, SIGKILL);
            int st = 0;
            (void)::waitpid(pid1, &st, 0);
            pid1 = -1;
        }
        if (pid2 > 0) {
            (void)::kill(pid2, SIGKILL);
            int st = 0;
            (void)::waitpid(pid2, &st, 0);
            pid2 = -1;
        }
    }

    ~ChildProcessGuard() {
        terminate_and_reap_all();
    }
};

// ============================================================================
// 7. HÀM TIỆN ÍCH CHUYỂN ĐỔN ĐƠN VỊ & QUÉT ĐỈNH TELEMETRY
// ============================================================================
void convert_linear_auto_power_to_db(const std::vector<double>& linear_val,
                                     std::vector<float>& out_db) noexcept {
    const size_t n = std::min(linear_val.size(), out_db.size());
    for (size_t k = 0; k < n; ++k) {
        const double v = linear_val[k];
        const double clamped = (std::isfinite(v) && v >= 0.0) ? std::max(v, 1e-12) : 1e-12;
        out_db[k] = static_cast<float>(10.0 * std::log10(clamped));
    }
}

bool set_gpio_level(int pin, bool high) noexcept {
    char command[64];
    const int n = std::snprintf(command, sizeof(command), "pinctrl set %d op %s",
                                pin, high ? "dh" : "dl");
    return n > 0 && static_cast<size_t>(n) < sizeof(command) &&
           std::system(command) == 0;
}

bool force_antenna_off() noexcept {
    // Attempt BOTH outputs even if the first command fails.
    const bool noise_ok = set_gpio_level(SolarConfig::Hardware::PIN_NOISE_SOURCE, false);
    const bool switch_ok = set_gpio_level(SolarConfig::Hardware::PIN_RF_SWITCH, false);
    return noise_ok && switch_ok;
}

// Same GPIO polarity as HardwareGuard: HIGH selects Cal/Splitter and noise ON.
// Every failure attempts to return both outputs to the existing safe state.
bool select_observation_source(bool noise) noexcept {
    if (!noise) return force_antenna_off();
    if (!set_gpio_level(SolarConfig::Hardware::PIN_RF_SWITCH, true) ||
        !set_gpio_level(SolarConfig::Hardware::PIN_NOISE_SOURCE, true)) {
        (void)force_antenna_off();
        return false;
    }
    return true;
}

struct SafeGpioOnExit {
    ~SafeGpioOnExit() {
        if (!force_antenna_off())
            std::cerr << "[GPIO ERROR] Unable to command safe GPIO levels.\n";
    }
};

uint64_t current_utc_timestamp_us() noexcept {
    const auto now = std::chrono::system_clock::now();
    return static_cast<uint64_t>(
        std::chrono::duration_cast<std::chrono::microseconds>(now.time_since_epoch()).count());
}

double bin_to_freq_mhz(size_t k, size_t n_fft) noexcept {
    const double df_hz = SolarConfig::SDR::SAMPLE_RATE_HZ / static_cast<double>(n_fft);
    const double half_n = static_cast<double>(n_fft) / 2.0;
    const double freq_hz = SolarConfig::SDR::RF_CENTER_FREQ_HZ + (static_cast<double>(k) - half_n) * df_hz;
    return freq_hz * 1e-6;
}

} // namespace

// ============================================================================
// 8. CHƯƠNG TRÌNH ĐIỀU PHỐI TRUNG TÂM (MAIN ENTRY POINT)
// ============================================================================
int main(int argc, char* argv[]) {
    CliOptions cli;
    std::string cli_err;
    if (!parse_and_validate_cli(argc, argv, cli, cli_err)) {
        std::cerr << "[LỖI THAM SỐ CLI] " << cli_err << "\n\n";
        print_usage(argv[0]);
        return 1;
    }
    if (cli.show_help) {
        print_usage(argv[0]);
        return 0;
    }

#ifdef SOLAR_STORAGE_ENABLED
    std::optional<solar::storage::Config> storage_config;
    if (cli.check_config) {
        try {
            const auto config = solar::storage::load_config(cli.storage_config);
            std::cout << nlohmann::json{{"effective_config",config.value},{"config_hash",config.hash},
                {"estimate",solar::storage::estimate(config,SolarConfig::Correlator::FFT_SIZE,
                    SolarConfig::Correlator::HOP_SIZE,SolarConfig::Correlator::INTEGRATION_FRAMES,
                    SolarConfig::SDR::SAMPLE_RATE_HZ)}}.dump(2) << "\n";
            return 0;
        } catch (const std::exception& e) { std::cerr << "[CONFIG] " << e.what() << "\n"; return 2; }
    }
#endif
    if (!install_signal_handlers(parent_signal_handler)) {
        std::cerr << "[LỖI HỆ THỐNG] Không thể cài đặt trình xử lý tín hiệu POSIX (sigaction).\n";
        return 1;
    }

    SystemState state = SystemState::STARTUP;
    bool is_sample_aligned = false;
    bool is_phase_calibrated = false;

    std::cout << "================================================================================\n";
    std::cout << " SOLAR_CORRELATOR - HỆ THỐNG GIAO THOA KẾ VÔ TUYẾN MẶT TRỜI (30.0 - 40.0 MHz)\n";
    std::cout << "================================================================================\n";
    std::cout << "[STATE: " << state_to_string(state, is_sample_aligned, is_phase_calibrated) << "]\n";

    SingleInstanceLock instance_lock("/tmp/solar_correlator.lock");
    std::string lock_err;
    if (!instance_lock.acquire(lock_err)) {
        std::cerr << "[LỖI KHÓA PHIÊN] " << lock_err << "\n";
        return 1;
    }

#ifdef SOLAR_STORAGE_ENABLED
    if (!cli.storage_config.empty()) {
        try {
            storage_config = solar::storage::load_config(cli.storage_config);
            std::cout << solar::storage::estimate(*storage_config,SolarConfig::Correlator::FFT_SIZE,
                SolarConfig::Correlator::HOP_SIZE,SolarConfig::Correlator::INTEGRATION_FRAMES,
                SolarConfig::SDR::SAMPLE_RATE_HZ).dump(2) << "\n";
            solar::storage::set_observation_timezone(*storage_config);
            solar::storage::startup_check(*storage_config);
        } catch (const std::exception& e) { std::cerr << "[STORAGE STARTUP] " << e.what() << "\n"; return 2; }
    }
#endif
    SafeGpioOnExit safe_gpio;
    if (!force_antenna_off()) {
        std::cerr << "[FATAL] Unable to command Noise OFF / Antenna.\n";
        return 1;
    }

    SharedMemoryRingBuffer::unlink_shm(SolarConfig::SHM::CH1_NAME);
    SharedMemoryRingBuffer::unlink_shm(SolarConfig::SHM::CH2_NAME);

    sdrplay_api_ErrT sdr_err = sdrplay_api_Open();
    if (sdr_err != sdrplay_api_Success) {
        std::cerr << "[LỖI SDRPLAY] Không thể kết nối SDRplay API Service: "
                  << sdrplay_api_GetErrorString(sdr_err) << "\n";
        return 1;
    }

    float api_ver = 0.0f;
    if (sdrplay_api_ApiVersion(&api_ver) == sdrplay_api_Success &&
        std::abs(api_ver - SDRPLAY_API_VERSION) > 0.01f) {
        std::cerr << "[CẢNH BÁO] Phiên bản SDRplay API DLL (" << api_ver
                  << ") khác với header biên dịch (" << SDRPLAY_API_VERSION << ").\n";
    }

    (void)sdrplay_api_LockDeviceApi();
    sdrplay_api_DeviceT devs[SDRPLAY_MAX_DEVICES];
    unsigned int num_devs = 0;
    sdr_err = sdrplay_api_GetDevices(devs, &num_devs, SDRPLAY_MAX_DEVICES);
    (void)sdrplay_api_UnlockDeviceApi();

    if (sdr_err != sdrplay_api_Success) {
        std::cerr << "[LỖI SDRPLAY] Lỗi sdrplay_api_GetDevices: "
                  << sdrplay_api_GetErrorString(sdr_err) << "\n";
        (void)sdrplay_api_Close();
        return 1;
    }

    std::vector<std::string> discovered_serials;
    for (unsigned int i = 0; i < num_devs; ++i) {
        discovered_serials.emplace_back(devs[i].SerNo);
    }
    (void)sdrplay_api_Close();

    std::string ser_ch1;
    std::string ser_ch2;

    if (!cli.serial_ch1.empty() && !cli.serial_ch2.empty()) {
        const bool found1 = std::find(discovered_serials.begin(), discovered_serials.end(), cli.serial_ch1) != discovered_serials.end();
        const bool found2 = std::find(discovered_serials.begin(), discovered_serials.end(), cli.serial_ch2) != discovered_serials.end();
        if (!found1 || !found2) {
            std::cerr << "[LỖI PHẦN CỨNG] Không tìm thấy RSPdx khớp Serial chỉ định ("
                      << "Ch1=" << cli.serial_ch1 << " [" << (found1 ? "OK" : "MISSING") << "], "
                      << "Ch2=" << cli.serial_ch2 << " [" << (found2 ? "OK" : "MISSING") << "]).\n";
            return 1;
        }
        ser_ch1 = cli.serial_ch1;
        ser_ch2 = cli.serial_ch2;
    } else {
        if (discovered_serials.size() < 2) {
            std::cerr << "[LỖI PHẦN CỨNG] Cần tối thiểu 2 thiết bị SDRplay RSPdx (hiện tìm thấy: "
                      << discovered_serials.size() << ").\n";
            return 1;
        }
        if (discovered_serials.size() > 2) {
            std::cerr << "[LỖI PHẦN CỨNG] Phát hiện " << discovered_serials.size()
                      << " thiết bị SDRplay (> 2). Vui lòng chỉ định rõ qua --serial-ch1 và --serial-ch2!\n";
            return 1;
        }
        std::sort(discovered_serials.begin(), discovered_serials.end());
        ser_ch1 = discovered_serials[0];
        ser_ch2 = discovered_serials[1];
    }

    std::cout << "[Hardware] Ánh xạ thiết bị thu SDRplay RSPdx (REFin 24 MHz, LO = 35.0 MHz, fs = 10.0 MSPS):\n";
    std::cout << "  + Kênh 1 (CH1 -> " << SolarConfig::SHM::CH1_NAME << ") <-> Serial: " << ser_ch1 << "\n";
    std::cout << "  + Kênh 2 (CH2 -> " << SolarConfig::SHM::CH2_NAME << ") <-> Serial: " << ser_ch2 << "\n";
    std::cout << "[Network] Cấu hình SOL2 v3 UDP Telemetry (Đích: " << cli.dest_ip << "):\n";
    std::cout << "  + S12 (Cross-Power dB) : Port " << cli.port_s12 << " [ENABLED]\n";
    std::cout << "  + S11 (Auto-Power 1 dB): " << (cli.port_s11 > 0 ? ("Port " + std::to_string(cli.port_s11)) : "DISABLED (0)") << "\n";
    std::cout << "  + S22 (Auto-Power 2 dB): " << (cli.port_s22 > 0 ? ("Port " + std::to_string(cli.port_s22)) : "DISABLED (0)") << "\n";
    std::cout << "  + Phase rad: " << cli.port_phase << " | Coherence gamma: " << cli.port_coherence << " (0=disabled)\n";

    ChildProcessGuard children;
    children.pid1 = ::fork();
    if (children.pid1 < 0) {
        std::cerr << "[LỖI HỆ THỐNG] Lỗi fork() tiến trình con Kênh 1: " << std::strerror(errno) << "\n";
        return 1;
    }
    if (children.pid1 == 0) {
        instance_lock.close_in_child();
        run_sdr_producer_process(1, ser_ch1, SolarConfig::SHM::CH1_NAME);
    }

    children.pid2 = ::fork();
    if (children.pid2 < 0) {
        std::cerr << "[LỖI HỆ THỐNG] Lỗi fork() tiến trình con Kênh 2: " << std::strerror(errno) << "\n";
        children.terminate_and_reap_all();
        SharedMemoryRingBuffer::unlink_shm(SolarConfig::SHM::CH1_NAME);
        return 1;
    }
    if (children.pid2 == 0) {
        instance_lock.close_in_child();
        run_sdr_producer_process(2, ser_ch2, SolarConfig::SHM::CH2_NAME);
    }

#ifdef SOLAR_STORAGE_ENABLED
    solar::storage::AutoMaintenance auto_maintenance;
#endif
    std::optional<HardwareGuard> hw_guard;
    int exit_code = 0;

    try {
        if (!cli.skip_align) {
            hw_guard.emplace();
            // The immutable guard ignores system() status; reassert and check.
            if (!set_gpio_level(SolarConfig::Hardware::PIN_RF_SWITCH, true) ||
                !set_gpio_level(SolarConfig::Hardware::PIN_NOISE_SOURCE, true))
                throw std::runtime_error("GPIO calibration command failed");
            std::this_thread::sleep_for(std::chrono::milliseconds(
                SolarConfig::Hardware::WARMUP_DELAY_MS));
        }
        std::unique_ptr<SharedMemoryRingBuffer> rb1_ptr;
        std::unique_ptr<SharedMemoryRingBuffer> rb2_ptr;

        const auto shm_open_deadline = std::chrono::steady_clock::now() + std::chrono::seconds(10);
        while (g_running.load(std::memory_order_relaxed)) {
            std::string child_err;
            if (!children.check_children_alive(child_err)) {
                throw std::runtime_error(child_err);
            }
            try {
                if (!rb1_ptr) {
                    rb1_ptr = api_bridge::create_ring_buffer(
                        SolarConfig::SHM::CH1_NAME, SolarConfig::SHM::BUFFER_CAPACITY, false);
                }
                if (!rb2_ptr) {
                    rb2_ptr = api_bridge::create_ring_buffer(
                        SolarConfig::SHM::CH2_NAME, SolarConfig::SHM::BUFFER_CAPACITY, false);
                }
                if (rb1_ptr && rb2_ptr) break;
            } catch (const std::exception&) {
                if (std::chrono::steady_clock::now() >= shm_open_deadline) {
                    throw std::runtime_error("Quá thời gian chờ (10s) mở vùng nhớ POSIX SHM từ Producer.");
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(50));
            }
        }

        if (!rb1_ptr || !rb2_ptr) {
            throw std::runtime_error("Bị hủy trong khi chờ khởi tạo vùng nhớ chia sẻ SHM.");
        }

        SharedMemoryRingBuffer& rb1 = *rb1_ptr;
        SharedMemoryRingBuffer& rb2 = *rb2_ptr;

        RuntimeTrace trace;
        auto mark = [&](const char* label) { trace.mark(label, rb1, rb2); };
        mark("preallocate.begin");

        // Perform FFTW_MEASURE before enabling writes to the ring buffers.
        FXCorrelator::Options fx_opts {};
        fx_opts.fft_size = SolarConfig::Correlator::FFT_SIZE;
        fx_opts.frames = SolarConfig::Correlator::INTEGRATION_FRAMES;
        fx_opts.running = &g_running;
        FXCorrelator fx(fx_opts);
        std::optional<TimeAligner> aligner;
        if (!cli.skip_align) aligner.emplace(SolarConfig::Alignment::SNAPSHOT_SIZE);
        const size_t n_bins = SolarConfig::Correlator::FFT_SIZE;
        std::vector<float> phi_rad(n_bins, 0.0f);
        std::vector<float> s12_db(n_bins, 0.0f);
        std::vector<float> phase_rad(n_bins, 0.0f);
        std::vector<float> coherence(n_bins, 0.0f);
        std::vector<float> s11_db(n_bins, 0.0f);
        std::vector<float> s22_db(n_bins, 0.0f);
        const auto telemetry_session = TelemetrySender::new_session();
        using Payload = TelemetrySender::Payload;
        TelemetrySender sender_s12(cli.dest_ip, cli.port_s12, telemetry_session, Payload::S12);
        std::optional<TelemetrySender> sender_s11;
        std::optional<TelemetrySender> sender_s22;
        std::optional<TelemetrySender> sender_phase, sender_coherence;

        if (cli.port_s11 > 0) sender_s11.emplace(cli.dest_ip, cli.port_s11, telemetry_session, Payload::S11);
        if (cli.port_s22 > 0) sender_s22.emplace(cli.dest_ip, cli.port_s22, telemetry_session, Payload::S22);
        if (cli.port_phase > 0) sender_phase.emplace(cli.dest_ip, cli.port_phase, telemetry_session, Payload::Phase);
        if (cli.port_coherence > 0) sender_coherence.emplace(cli.dest_ip, cli.port_coherence, telemetry_session, Payload::Coherence);

#ifdef SOLAR_STORAGE_ENABLED
        std::unique_ptr<solar::storage::Pipeline> storage;
        if (storage_config && storage_config->value.at("storage").at("enabled").get<bool>()) {
            const auto& cfg = storage_config->value.at("storage");
            solar::storage::AggregationConfig geometry{n_bins,cfg.at("save_freq_bins"),
                n_bins,SolarConfig::Correlator::HOP_SIZE,SolarConfig::Correlator::INTEGRATION_FRAMES,
                SolarConfig::SDR::SAMPLE_RATE_HZ,SolarConfig::SDR::RF_CENTER_FREQ_HZ,
                cfg.at("save_interval_sec"),cfg.at("partial_window_policy")=="emit" ?
                    solar::storage::PartialPolicy::Emit : solar::storage::PartialPolicy::Discard};
            nlohmann::json metadata={
                {"session_id",std::to_string(telemetry_session)},
                {"observation_source",cli.observation_source},
                {"baseline_revision",SOLAR_BASELINE_REVISION},{"patch_state",SOLAR_PATCH_STATE},
                {"center_freq_hz",SolarConfig::SDR::RF_CENTER_FREQ_HZ},
                {"sample_rate_hz",SolarConfig::SDR::SAMPLE_RATE_HZ},{"input_fft_bins",n_bins},
                {"output_bins",cfg.at("save_freq_bins")},{"window","symmetric Hann N-1; actual float window energy"},
                {"window_energy",fx.window_energy()},{"hop_samples",SolarConfig::Correlator::HOP_SIZE},
                {"source_integration_frames",SolarConfig::Correlator::INTEGRATION_FRAMES},
                {"normalization","sum(raw DFT cross)/(M*N*sum(w^2)); ADC-count units restored"},
                {"cross_product_convention","X1*conj(X2) * exp(-j*phi)"},
                {"units","ADC-count^2 per input FFT bin (mean after frequency grouping)"},
                {"db_definition","10*log10(linear/1 ADC-count^2); not dBm or calibrated flux"},
                {"grouping_policy","whole contiguous integrations to reach target; linear frequency mean; common strict invalid mask"},
                {"frequency_bin_bandwidth_hz",SolarConfig::SDR::SAMPLE_RATE_HZ/cfg.at("save_freq_bins").get<double>()},
                {"timestamp_semantics","estimated sample support from first processing-wall-clock end anchor; original backend timestamps retained"},
                {"utc_uncertainty","unknown USB/SHM/processing latency; no hardware UTC anchor"},
                {"calibration_id",is_phase_calibrated?1:0},{"phase_calibrated",is_phase_calibrated},
                {"aligned",is_sample_aligned},{"k_offset_samples",is_sample_aligned?nlohmann::json(0):nlohmann::json(nullptr)},
                {"quality_flag_definitions","1 UTC_ESTIMATED;2 INVALID_BIN;4 PARTIAL;8 DISCONTINUITY;16 CALIBRATION_BOUNDARY;32 COHERENCE_VIOLATION"}};
            storage=std::make_unique<solar::storage::Pipeline>(*storage_config,geometry,metadata,phi_rad,true);
        }
#endif

        mark("preallocate.end");


        std::cout << "[Supervisor] Đang chờ cả 2 bộ thu RSPdx hoàn tất khởi tạo phần cứng...\n";
        const auto hw_ready_deadline = std::chrono::steady_clock::now() + std::chrono::seconds(15);
        while (g_running.load(std::memory_order_relaxed) && (!rb1.is_ready() || !rb2.is_ready())) {
            std::string child_err;
            if (!children.check_children_alive(child_err)) {
                throw std::runtime_error(child_err);
            }
            if (std::chrono::steady_clock::now() >= hw_ready_deadline) {
                throw std::runtime_error("Quá thời gian chờ (15s) phần cứng RSPdx sẵn sàng (is_ready == false).");
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(25));
        }

        if (!g_running.load(std::memory_order_relaxed)) {
            state = SystemState::STOPPING;
        }

        if (state != SystemState::STOPPING) {
            std::cout << "[Supervisor] Cả 2 kênh RSPdx đã sẵn sàng. Kích hoạt luồng I/Q đồng thời và chờ 200ms ổn định DMA...\n";
            mark("stream_gate.begin");
            rb1.set_start_stream(true);
            rb2.set_start_stream(true);
            mark("stream_gate.end");

            const auto warmup_end = std::chrono::steady_clock::now() + std::chrono::milliseconds(200);
            while (g_running.load(std::memory_order_relaxed) &&
                   std::chrono::steady_clock::now() < warmup_end) {
                std::string child_err;
                if (!children.check_children_alive(child_err)) {
                    throw std::runtime_error(child_err);
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(25));
            }

            rb1.flush();
            rb2.flush();
        }

        auto wait_for_samples = [&](uint32_t required_samples, int timeout_ms) -> bool {
            const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
            while (g_running.load(std::memory_order_relaxed)) {
                std::string child_err;
                if (!children.check_children_alive(child_err)) {
                    throw std::runtime_error(child_err);
                }
                if (rb1.available_read() >= required_samples &&
                    rb2.available_read() >= required_samples) {
                    return true;
                }
                if (std::chrono::steady_clock::now() >= deadline) {
                    return false;
                }
                std::this_thread::sleep_for(std::chrono::microseconds(500));
            }
            return false;
        };

        uint32_t aligned_drops1 = 0;
        uint32_t aligned_drops2 = 0;
        auto check_alignment_health = [&]() {
            std::string err;
            if (!children.check_children_alive(err) || !rb1.is_ready() || !rb2.is_ready()) {
                mark("health.producer_failure");
                throw std::runtime_error("[ALIGNMENT_LOST] Producer stopped/reset: " + err);
            }
            if (!cli.skip_align &&
                (rb1.get_overflow_count() != aligned_drops1 ||
                 rb2.get_overflow_count() != aligned_drops2)) {
                mark("health.overflow");
                throw std::runtime_error("[ALIGNMENT_LOST] Sample drop since stream gate");
            }
        };
        auto skip_pair = [&](uint32_t count) {
            if (!wait_for_samples(count, 3000))
                throw std::runtime_error("Paired discard timeout/cancellation");
            if (rb1.skip(count) != count || rb2.skip(count) != count)
                throw std::runtime_error("Short paired discard");
        };

        if (state != SystemState::STOPPING) {
            if (cli.skip_align) {
                state = SystemState::UNALIGNED;
                is_sample_aligned = false;
                std::cout << "\n================================================================================\n";
                std::cout << " [STATE: UNALIGNED - DIAGNOSTIC MODE]\n";
                std::cout << " Cảnh báo: Đã bỏ qua Time Alignment theo cờ --skip-align/--no-align.\n";
                std::cout << " GPIO 26 (Noise Source) = LOW, GPIO 6 (RF Switch) = LOW.\n";
                std::cout << "================================================================================\n\n";
            } else {
                state = SystemState::ALIGNING;
                StartupAlignment::Options options;
                options.snapshot_size = SolarConfig::Alignment::SNAPSHOT_SIZE;
                options.attempts = static_cast<unsigned>(cli.max_align_retries);
                options.max_residual_samples = 0; // Main metadata promises exact integer k=0.
                auto wait_snapshot = [&] {
                    check_alignment_health();
                    if (!wait_for_samples(options.snapshot_size, 3000))
                        throw std::runtime_error("alignment snapshot timeout/cancellation");
                    check_alignment_health();
                };
                mark("alignment.begin");
                StartupAlignment::acquire(rb1, rb2, options, wait_snapshot,
                    [&] {
                        mark("snapshot.begin");
                        const auto result = aligner->measure_offset_from_buffers(rb1, rb2);
                        mark("snapshot.end");
                        return result;
                    }, std::cout);
                is_sample_aligned = true;
                check_alignment_health();
                mark("alignment.locked");

            }
        }

        if (state != SystemState::STOPPING) {
            check_alignment_health();


            if (cli.cal_phase) {
                state = SystemState::CALIBRATING;
                std::cout << "\n[STATE: CALIBRATING] Đang chụp đáp tuyến pha thiết bị tĩnh (phi_rad) khi Noise Source đang bật...\n";

                check_alignment_health();
                mark("prime.begin");
                if (!api_bridge::invoke_prime_buffers(fx, rb1, rb2)) {
                    throw std::runtime_error("Lỗi prime_buffers() trước khi chạy chu kỳ --cal-phase.");
                }

                check_alignment_health();
                const uint32_t ov1_before = rb1.get_overflow_count();
                const uint32_t ov2_before = rb2.get_overflow_count();

                const bool cal_cycle_ok = api_bridge::invoke_integration_cycle(
                    fx, rb1, rb2, phi_rad, s12_db, phase_rad, coherence);

                const uint32_t ov1_after = rb1.get_overflow_count();
                const uint32_t ov2_after = rb2.get_overflow_count();

                if (!g_running.load(std::memory_order_relaxed)) {
                    state = SystemState::STOPPING;
                } else if (!cal_cycle_ok || ov1_after != ov1_before || ov2_after != ov2_before) {
                    throw std::runtime_error(
                        "[FAIL-CLOSED] Phát sinh tràn bộ đệm (overflow) hoặc gián đoạn mẫu trong chu kỳ --cal-phase!");
                } else {
                    constexpr float kMinCalCoherence = 0.10f;
                    size_t locked_phase_bins = 0;
                    double mean_coherence = 0.0;

                    for (size_t k = 0; k < n_bins; ++k) {
                        if (!std::isfinite(phase_rad[k]) || !std::isfinite(coherence[k])) {
                            throw std::runtime_error(
                                "[FAIL-CLOSED] Giá trị phase_rad hoặc coherence không hữu hạn (NaN/Inf) khi hiệu chuẩn pha!");
                        }
                        mean_coherence += static_cast<double>(coherence[k]);
                        if (coherence[k] >= kMinCalCoherence) {
                            // Output k is shifted; phi is indexed in natural FFT order.
                            phi_rad[(k + n_bins / 2) % n_bins] = phase_rad[k];
                            locked_phase_bins++;
                        } else {
                            phi_rad[(k + n_bins / 2) % n_bins] = 0.0f;
                        }
                    }
                    mean_coherence /= static_cast<double>(n_bins);

                    if (locked_phase_bins == 0) {
                        throw std::runtime_error(
                            "[FAIL-CLOSED] Không có bin tần số nào đạt ngưỡng Coherence >= 0.10 trong chế độ --cal-phase!");
                    }

                    is_phase_calibrated = true;
                    std::cout << "[STATE: CALIBRATING -> DONE] Đã khóa bù pha tĩnh thành công cho "
                              << locked_phase_bins << "/" << n_bins
                              << " bins (Coherence trung bình = " << std::fixed << std::setprecision(3)
                              << mean_coherence << ").\n";
                }
            }

#ifdef SOLAR_STORAGE_ENABLED
            if (storage && state != SystemState::STOPPING) {
                mark("storage.activate.begin");
                storage->start_observation(is_sample_aligned, is_phase_calibrated, phi_rad);
                mark("storage.activate.end");
                auto_maintenance.start(*storage_config);
                check_alignment_health();
            }
#endif
            // Select a fixed source for this observation/session. Do not mix
            // startup calibration/switch transients into saved observations.
            if (state != SystemState::STOPPING &&
                (cli.observation_source == "noise" ||
                 (hw_guard.has_value() && hw_guard->is_active()))) {
                const uint32_t ov1_pre_sw = rb1.get_overflow_count();
                const uint32_t ov2_pre_sw = rb2.get_overflow_count();

                check_alignment_health();
                const bool observe_noise = cli.observation_source == "noise";
                mark("observation_source.begin");
                if (!observe_noise && hw_guard.has_value()) hw_guard->turn_off();
                if (!select_observation_source(observe_noise))
                    throw std::runtime_error("GPIO observation source command failed");
                const int settle_ms = observe_noise
                    ? std::max(SolarConfig::Hardware::WARMUP_DELAY_MS,
                               SolarConfig::Hardware::SWITCH_SETTLE_MS)
                    : SolarConfig::Hardware::SWITCH_SETTLE_MS;
                std::this_thread::sleep_for(std::chrono::milliseconds(settle_ms));
                check_alignment_health();

                // Discard every sample already committed on EITHER channel.
                // Wait for the lagging producer, then advance both tails equally.
                // This is a SHM watermark, not a bound on USB/SDR in-flight data.
                const uint32_t boundary = std::max(rb1.available_read(), rb2.available_read());
                skip_pair(boundary);
                check_alignment_health();
                mark("observation_source.paired_boundary");

                if (rb1.get_overflow_count() != ov1_pre_sw || rb2.get_overflow_count() != ov2_pre_sw) {
                    throw std::runtime_error(
                        "[FAIL-CLOSED] SHM overflow while selecting observation source");
                }
                std::cout << "[INPUT] Observation source=" << cli.observation_source
                          << " (RF switch=" << (observe_noise ? "HIGH" : "LOW")
                          << ", noise=" << (observe_noise ? "ON" : "OFF") << ")\n";
            }

            if (state != SystemState::STOPPING) {
                check_alignment_health();
                mark("prime.begin");
                if (!api_bridge::invoke_prime_buffers(fx, rb1, rb2)) {
                    if (g_running.load(std::memory_order_relaxed)) {
                        throw std::runtime_error("Lỗi prime_buffers() khởi tạo cửa sổ trượt 50% Welch.");
                    }
                    state = SystemState::STOPPING;
                }
            }

            mark("prime.end");
            check_alignment_health();
            api_bridge::PortTelemetryStats stats_s12 {};
            api_bridge::PortTelemetryStats stats_s11 {};
            api_bridge::PortTelemetryStats stats_s22 {};
            api_bridge::PortTelemetryStats stats_phase {}, stats_coherence {};

            if (state != SystemState::STOPPING) {
                state = SystemState::RUNNING;
                std::cout << "\n[STATE: " << state_to_string(state, is_sample_aligned, is_phase_calibrated)
                          << "] Bắt đầu vòng lặp tích phân Welch FX Correlator (~200 ms/frame)...\n";
                std::cout << "--------------------------------------------------------------------------------\n";

                uint64_t telemetry_frame_id = 0;
                int diag_recovery_count = 0;
                constexpr int kMaxDiagRecoveries = 5;
                auto last_report_time = std::chrono::steady_clock::now();

                while (g_running.load(std::memory_order_relaxed) && state == SystemState::RUNNING) {
#ifdef SOLAR_STORAGE_ENABLED
                    auto_maintenance.check();
#endif
                    std::string child_err;
                    if (!children.check_children_alive(child_err)) {
                        std::cerr << "\n[FATAL][SUPERVISOR] " << child_err << "\n";
                        state = SystemState::STOPPING;
                        exit_code = 1;
                        break;
                    }

                    check_alignment_health();
                    if (telemetry_frame_id < 8) mark("integration.begin");
                    const uint32_t ov1_before = rb1.get_overflow_count();
                    const uint32_t ov2_before = rb2.get_overflow_count();

                    bool cycle_ok = false;
                    std::string cycle_err_msg;
                    try {
                        cycle_ok = api_bridge::invoke_integration_cycle(
                            fx, rb1, rb2, phi_rad, s12_db, phase_rad, coherence);
                    } catch (const std::exception& ex) {
                        cycle_ok = false;
                        cycle_err_msg = ex.what();
                    }

                    if (telemetry_frame_id < 8 || !cycle_ok) mark("integration.end");
                    if (!g_running.load(std::memory_order_relaxed)) {
                        state = SystemState::STOPPING;
                        break;
                    }

                    const uint32_t ov1_after = rb1.get_overflow_count();
                    const uint32_t ov2_after = rb2.get_overflow_count();
                    const bool overflow_occurred = (ov1_after != ov1_before) || (ov2_after != ov2_before);

                    if (!cycle_ok || overflow_occurred) {
                        ++telemetry_frame_id; // visible gap after a discarded integration
                        state = SystemState::RECOVERING;
                        std::cerr << "\n[STATE: RECOVERING] Phát hiện gián đoạn chu kỳ tích phân "
                                  << "(cycle_ok=" << cycle_ok
                                  << ", ov_ch1=" << ov1_before << "->" << ov1_after
                                  << ", ov_ch2=" << ov2_before << "->" << ov2_after
                                  << (cycle_err_msg.empty() ? "" : (", err=" + cycle_err_msg)) << ").\n";

                        if (is_phase_calibrated) {
                            std::cerr << "[FATAL][ALIGNMENT_LOST][FAIL-CLOSED] Tràn bộ đệm làm mất mốc đồng bộ mẫu, "
                                      << "khiến vector hiệu chuẩn pha tĩnh (--cal-phase) không còn hợp lệ. "
                                      << "Dừng hệ thống để bảo vệ dữ liệu!\n";
                            state = SystemState::STOPPING;
                            exit_code = 1;
                            break;
                        } else if (cli.skip_align) {
                            diag_recovery_count++;
                            if (diag_recovery_count > kMaxDiagRecoveries) {
                                std::cerr << "[FATAL][FAIL-CLOSED] Vượt quá số lần phục hồi tối đa ("
                                          << kMaxDiagRecoveries << ") ở chế độ UNALIGNED.\n";
                                state = SystemState::STOPPING;
                                exit_code = 1;
                                break;
                            }
                            std::cerr << "[RECOVERY][UNALIGNED] Thực hiện flush() + prime_buffers() (lần "
                                      << diag_recovery_count << "/" << kMaxDiagRecoveries << ")...\n";
                            rb1.flush();
                            rb2.flush();
                            mark("prime.begin");
                            if (!api_bridge::invoke_prime_buffers(fx, rb1, rb2)) {
                                state = SystemState::STOPPING;
                                exit_code = 1;
                                break;
                            }
                            state = SystemState::RUNNING;
                            continue;
                        } else {
                            std::cerr << "[FATAL][ALIGNMENT_LOST] Tràn bộ đệm làm mất khóa đồng bộ mẫu k = 0 "
                                      << "trong chế độ quan trắc ALIGNED. Dừng hệ thống Fail-Closed!\n";
                            state = SystemState::STOPPING;
                            exit_code = 1;
                            break;
                        }
                    }

                    check_alignment_health();
                    convert_linear_auto_power_to_db(fx.auto_power1(), s11_db);
                    convert_linear_auto_power_to_db(fx.auto_power2(), s22_db);

                    const uint64_t timestamp_us = current_utc_timestamp_us();

                    api_bridge::dispatch_udp_spectrum(sender_s12, telemetry_frame_id, timestamp_us, s12_db, stats_s12);
                    if (sender_s11.has_value()) {
                        api_bridge::dispatch_udp_spectrum(*sender_s11, telemetry_frame_id, timestamp_us, s11_db, stats_s11);
                    }
                    if (sender_s22.has_value()) {
                        api_bridge::dispatch_udp_spectrum(*sender_s22, telemetry_frame_id, timestamp_us, s22_db, stats_s22);
                    }
                    if (sender_phase) api_bridge::dispatch_udp_spectrum(*sender_phase, telemetry_frame_id, timestamp_us, phase_rad, stats_phase);
                    if (sender_coherence) api_bridge::dispatch_udp_spectrum(*sender_coherence, telemetry_frame_id, timestamp_us, coherence, stats_coherence);

#ifdef SOLAR_STORAGE_ENABLED
                    if (storage) storage->try_submit(telemetry_frame_id,timestamp_us,
                        fx.auto_power1(),fx.auto_power2(),fx.cross_real(),fx.cross_imag());
#endif
                    telemetry_frame_id++;

                    const auto now_steady = std::chrono::steady_clock::now();
                    if (std::chrono::duration<double>(now_steady - last_report_time).count() >= 5.0) {
                        size_t peak_s12_bin = 0;
                        float peak_s12_val  = -std::numeric_limits<float>::infinity();
                        float peak_s11_val  = -std::numeric_limits<float>::infinity();
                        float peak_s22_val  = -std::numeric_limits<float>::infinity();
                        float max_coh_val   = 0.0f;

                        for (size_t k = 0; k < n_bins; ++k) {
                            if (s12_db[k] > peak_s12_val) {
                                peak_s12_val = s12_db[k];
                                peak_s12_bin = k;
                            }
                            if (s11_db[k] > peak_s11_val) peak_s11_val = s11_db[k];
                            if (s22_db[k] > peak_s22_val) peak_s22_val = s22_db[k];
                            if (coherence[k] > max_coh_val) max_coh_val = coherence[k];
                        }

                        const double peak_freq_mhz = bin_to_freq_mhz(peak_s12_bin, n_bins);
                        const double fill_ch1_pct = (100.0 * static_cast<double>(rb1.available_read())) /
                                                    static_cast<double>(SolarConfig::SHM::BUFFER_CAPACITY);
                        const double fill_ch2_pct = (100.0 * static_cast<double>(rb2.available_read())) /
                                                    static_cast<double>(SolarConfig::SHM::BUFFER_CAPACITY);

                        std::cout << "[" << state_to_string(state, is_sample_aligned, is_phase_calibrated) << "] "
                                  << "Frame #" << std::setw(5) << (telemetry_frame_id - 1) << " | "
                                  << std::fixed << std::setprecision(2)
                                  << "|S12|_max=" << peak_s12_val << " dB @" << std::setprecision(3) << peak_freq_mhz << " MHz | "
                                  << std::setprecision(1)
                                  << "S11=" << peak_s11_val << " dB, S22=" << peak_s22_val << " dB | "
                                  << std::setprecision(3) << "Coh_max=" << max_coh_val
                                  << " Phase@S12peak=" << phase_rad[peak_s12_bin] << " rad | "
                                  << std::setprecision(1)
                                  << "RB=" << fill_ch1_pct << "%/" << fill_ch2_pct << "% "
                                  << "(Drop=" << rb1.get_overflow_count() << "/" << rb2.get_overflow_count() << ") | "
                                  << "UDP[s12:" << stats_s12.frames_complete << "/" << stats_s12.send_errors;

                        if (sender_s11.has_value()) {
                            std::cout << " s11:" << stats_s11.frames_complete << "/" << stats_s11.send_errors;
                        }
                        if (sender_s22.has_value()) {
                            std::cout << " s22:" << stats_s22.frames_complete << "/" << stats_s22.send_errors;
                        }
                        if (sender_phase) std::cout << " phase:" << stats_phase.frames_complete << "/" << stats_phase.send_errors;
                        if (sender_coherence) std::cout << " coherence:" << stats_coherence.frames_complete << "/" << stats_coherence.send_errors;
                        std::cout << "]\n";

                        last_report_time = now_steady;
                    }
                }
            }
        }
    } catch (const std::exception& e) {
        std::cerr << "\n[EXCEPTION][FAIL-CLOSED] " << e.what() << "\n";
        exit_code = 1;
    }

    state = SystemState::STOPPING;
    g_running.store(false, std::memory_order_relaxed);

    if (g_last_signal != 0) {
        std::cout << "\n[STATE: STOPPING] Nhận tín hiệu dừng (sig=" << g_last_signal
                  << "). Đang ngắt GPIO phần cứng, thu hồi tiến trình con và giải phóng SHM...\n";
    } else {
        std::cout << "\n[STATE: STOPPING] Đang ngắt GPIO phần cứng, thu hồi tiến trình con và giải phóng SHM...\n";
    }

    if (hw_guard.has_value()) {
        hw_guard->turn_off();
    }

    // Noise observation also works without HardwareGuard (--skip-align).
    if (!force_antenna_off()) {
        std::cerr << "[GPIO ERROR] Unable to command Noise OFF / Antenna at shutdown.\n";
        exit_code = 1;
    }
    children.terminate_and_reap_all();
#ifdef SOLAR_STORAGE_ENABLED
    auto_maintenance.stop();
#endif
    SharedMemoryRingBuffer::unlink_shm(SolarConfig::SHM::CH1_NAME);
    SharedMemoryRingBuffer::unlink_shm(SolarConfig::SHM::CH2_NAME);
    // Keep the lock until safe_gpio and all other guards have been destroyed.

    std::cout << "[Supervisor] Hoàn tất đóng hệ thống an toàn (exit_code = " << exit_code << ").\n";
    return exit_code;
}
