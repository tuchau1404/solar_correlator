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
#include "correlator.hpp"
#include "telemetry_sender.hpp"

namespace {

// ============================================================================
// 1. BIẾN TÍN HIỆU TOÀN CỤC (ASYNC-SIGNAL-SAFE)
// ============================================================================
std::atomic<bool> g_running{true};
volatile sig_atomic_t g_last_signal = 0;

std::atomic<bool> g_child_running{true};
std::atomic<uint32_t> g_child_stream_resets{0};

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
    uint16_t port_aux        = SolarConfig::Network::UDP_PORT_AUX;
    std::string aux_mode     = "coherence";
    bool skip_align          = false;
    bool cal_phase           = false;
    int max_align_retries    = 5;
    std::string serial_ch1   = "";
    std::string serial_ch2   = "";
    bool show_help           = false;
};

void print_usage(const char* prog_name) {
    std::cout
        << "Sử dụng: " << prog_name << " [TÙY CHỌN]\n\n"
        << "Chương trình điều phối trung tâm Giao thoa kế Vô tuyến Mặt Trời (30.0 - 40.0 MHz)\n"
        << "Chuỗi xử lý: Dual SDRplay RSPdx -> POSIX SHM & Time Alignment -> Welch FX Correlator -> Multi-Port SOL2 v2 UDP\n\n"
        << "Tùy chọn dòng lệnh:\n"
        << "  --dest-ip <ip>            Địa chỉ IPv4 nhận Telemetry UDP (mặc định: " << SolarConfig::Network::UDP_DEST_IP << ")\n"
        << "  --port-s12 <1..65535>     Cổng UDP chính phát phổ |S12| dB (mặc định: " << SolarConfig::Network::UDP_PORT_S12 << ")\n"
        << "  --port-s11 <0..65535>     Cổng UDP phát phổ tự thân S11 dB; 0 để tắt (mặc định: " << SolarConfig::Network::UDP_PORT_S11 << ")\n"
        << "  --port-s22 <0..65535>     Cổng UDP phát phổ tự thân S22 dB; 0 để tắt (mặc định: " << SolarConfig::Network::UDP_PORT_S22 << ")\n"
        << "  --port-aux <0..65535>     Cổng UDP phát kênh phụ trợ; 0 để tắt (mặc định: " << SolarConfig::Network::UDP_PORT_AUX << ")\n"
        << "  --aux-mode <mode>         Chế độ kênh phụ trợ: 'coherence' hoặc 'phase' (mặc định: coherence)\n"
        << "  --skip-align, --no-align  Bỏ qua căn chỉnh trễ mẫu Module 2 (chế độ chẩn đoán CW / tải giả 50 Ohm)\n"
        << "  --cal-phase               Chụp và bù pha thiết bị tĩnh (phi_rad) ngay sau khi khóa mẫu k = 0\n"
        << "  --max-align-retries <n>   Số lần thử căn chỉnh tối đa ở Module 2 (mặc định: 5)\n"
        << "  --serial-ch1 <ser>        Chỉ định cứng Serial Number RSPdx cho Kênh 1\n"
        << "  --serial-ch2 <ser>        Chỉ định cứng Serial Number RSPdx cho Kênh 2\n"
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
        } else if (arg == "--port-aux") {
            const char* v = require_next(arg);
            if (!v) return false;
            if (!parse_port_value(v, true, opts.port_aux)) {
                err_msg = "Giá trị --port-aux không hợp lệ (bắt buộc 0 hoặc 1..65535): " + std::string(v);
                return false;
            }
        } else if (arg == "--aux-mode") {
            const char* v = require_next(arg);
            if (!v) return false;
            opts.aux_mode = v;
            if (opts.aux_mode != "coherence" && opts.aux_mode != "phase") {
                err_msg = "Giá trị --aux-mode không hợp lệ (chỉ chấp nhận 'coherence' hoặc 'phase'): " + opts.aux_mode;
                return false;
            }
        } else if (arg == "--skip-align" || arg == "--no-align") {
            opts.skip_align = true;
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
    if (opts.port_aux > 0) active_ports.emplace_back("--port-aux", opts.port_aux);

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
        fd_ = ::open(lock_path_.c_str(), O_CREAT | O_RDWR, 0666);
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
        (void)::ftruncate(fd_, 0);
        const std::string pid_str = std::to_string(::getpid()) + "\n";
        (void)::write(fd_, pid_str.data(), pid_str.size());
        acquired_ = true;
        return true;
    }

    ~SingleInstanceLock() {
        release();
    }

    void release() noexcept {
        if (fd_ >= 0) {
            if (acquired_) {
                (void)::unlink(lock_path_.c_str());
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
    const std::string& shm_name, uint32_t capacity, bool is_producer) {
    if constexpr (std::is_constructible_v<SharedMemoryRingBuffer, const std::string&, uint32_t, bool>) {
        return std::make_unique<SharedMemoryRingBuffer>(shm_name, capacity, is_producer);
    } else if constexpr (std::is_constructible_v<SharedMemoryRingBuffer, const std::string&, bool>) {
        (void)capacity;
        return std::make_unique<SharedMemoryRingBuffer>(shm_name, is_producer);
    } else {
        static_assert(std::is_constructible_v<SharedMemoryRingBuffer, const std::string&, uint32_t, bool>,
                      "Không tìm thấy constructor tương thích cho SharedMemoryRingBuffer");
        return nullptr;
    }
}

template <typename T, typename = void>
struct has_align_buffers_3 : std::false_type {};
template <typename T>
struct has_align_buffers_3<T, std::void_t<decltype(
    std::declval<T&>().align_buffers(
        std::declval<SharedMemoryRingBuffer&>(),
        std::declval<SharedMemoryRingBuffer&>(),
        std::declval<AlignmentResult&>()))>> : std::true_type {};

template <typename T, typename = void>
struct has_align_buffers_2 : std::false_type {};
template <typename T>
struct has_align_buffers_2<T, std::void_t<decltype(
    std::declval<T&>().align_buffers(
        std::declval<SharedMemoryRingBuffer&>(),
        std::declval<SharedMemoryRingBuffer&>()))>> : std::true_type {};

template <typename AlignerT>
bool execute_buffer_alignment(AlignerT& aligner,
                              SharedMemoryRingBuffer& rb1,
                              SharedMemoryRingBuffer& rb2,
                              AlignmentResult& out_initial_res) {
    if constexpr (has_align_buffers_3<AlignerT>::value) {
        using RetT = decltype(aligner.align_buffers(rb1, rb2, out_initial_res));
        if constexpr (std::is_convertible_v<RetT, bool>) {
            return static_cast<bool>(aligner.align_buffers(rb1, rb2, out_initial_res));
        } else {
            aligner.align_buffers(rb1, rb2, out_initial_res);
            return out_initial_res.is_valid;
        }
    } else if constexpr (has_align_buffers_2<AlignerT>::value) {
        using RetT = decltype(aligner.align_buffers(rb1, rb2));
        if constexpr (std::is_same_v<RetT, AlignmentResult>) {
            out_initial_res = aligner.align_buffers(rb1, rb2);
            return out_initial_res.is_valid;
        } else {
            out_initial_res = aligner.measure_offset_from_buffers(rb1, rb2);
            if (!out_initial_res.is_valid) return false;
            return static_cast<bool>(aligner.align_buffers(rb1, rb2));
        }
    } else {
        out_initial_res = aligner.measure_offset_from_buffers(rb1, rb2);
        if (!out_initial_res.is_valid) {
            return false;
        }
        if (out_initial_res.lag_samples > 0) {
            rb1.skip(static_cast<uint32_t>(out_initial_res.lag_samples));
        } else if (out_initial_res.lag_samples < 0) {
            rb2.skip(static_cast<uint32_t>(-out_initial_res.lag_samples));
        }
        return true;
    }
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

template <typename T, typename = void> struct has_complete : std::false_type {};
template <typename T> struct has_complete<T, std::void_t<decltype(std::declval<const T&>().complete)>> : std::true_type {};

template <typename T, typename = void> struct has_ok : std::false_type {};
template <typename T> struct has_ok<T, std::void_t<decltype(std::declval<const T&>().ok)>> : std::true_type {};

template <typename T, typename = void> struct has_success : std::false_type {};
template <typename T> struct has_success<T, std::void_t<decltype(std::declval<const T&>().success)>> : std::true_type {};

template <typename T, typename = void> struct has_chunks_sent : std::false_type {};
template <typename T> struct has_chunks_sent<T, std::void_t<decltype(std::declval<const T&>().chunks_sent)>> : std::true_type {};

template <typename T, typename = void> struct has_chunks_total : std::false_type {};
template <typename T> struct has_chunks_total<T, std::void_t<decltype(std::declval<const T&>().chunks_total)>> : std::true_type {};

template <typename T, typename = void> struct has_total_chunks : std::false_type {};
template <typename T> struct has_total_chunks<T, std::void_t<decltype(std::declval<const T&>().total_chunks)>> : std::true_type {};

template <typename T, typename = void> struct has_last_errno : std::false_type {};
template <typename T> struct has_last_errno<T, std::void_t<decltype(std::declval<const T&>().last_errno)>> : std::true_type {};

template <typename T, typename = void> struct has_error_count : std::false_type {};
template <typename T> struct has_error_count<T, std::void_t<decltype(std::declval<const T&>().error_count)>> : std::true_type {};

template <typename T, typename = void> struct has_chunks_failed : std::false_type {};
template <typename T> struct has_chunks_failed<T, std::void_t<decltype(std::declval<const T&>().chunks_failed)>> : std::true_type {};

template <typename T, typename = void> struct has_failed_chunks : std::false_type {};
template <typename T> struct has_failed_chunks<T, std::void_t<decltype(std::declval<const T&>().failed_chunks)>> : std::true_type {};

template <typename T, typename = void> struct has_would_block : std::false_type {};
template <typename T> struct has_would_block<T, std::void_t<decltype(std::declval<const T&>().would_block)>> : std::true_type {};

template <typename ResT>
void accumulate_telemetry_result(const ResT& res, PortTelemetryStats& st) noexcept {
    constexpr size_t kDefaultSol2Chunks = 8;
    size_t sent = kDefaultSol2Chunks;
    size_t expected = kDefaultSol2Chunks;

    if constexpr (has_chunks_sent<ResT>::value) {
        sent = static_cast<size_t>(res.chunks_sent);
        st.chunks_sent_total += sent;
    }
    if constexpr (has_chunks_total<ResT>::value) {
        expected = static_cast<size_t>(res.chunks_total);
    } else if constexpr (has_total_chunks<ResT>::value) {
        expected = static_cast<size_t>(res.total_chunks);
    }

    bool is_complete = false;
    if constexpr (has_complete<ResT>::value) {
        is_complete = static_cast<bool>(res.complete);
    } else if constexpr (has_ok<ResT>::value) {
        is_complete = static_cast<bool>(res.ok);
    } else if constexpr (has_success<ResT>::value) {
        is_complete = static_cast<bool>(res.success);
    } else if constexpr (has_chunks_sent<ResT>::value) {
        is_complete = (sent == expected && expected > 0);
    } else {
        is_complete = true;
    }

    bool had_err = false;
    if constexpr (has_last_errno<ResT>::value) {
        if (res.last_errno != 0) had_err = true;
    }
    if constexpr (has_error_count<ResT>::value) {
        if (res.error_count > 0) had_err = true;
    }
    if constexpr (has_chunks_failed<ResT>::value) {
        if (res.chunks_failed > 0) had_err = true;
    }
    if constexpr (has_failed_chunks<ResT>::value) {
        if (res.failed_chunks > 0) had_err = true;
    }
    if constexpr (has_would_block<ResT>::value) {
        if (res.would_block > 0 && !is_complete) had_err = true;
    }

    if (is_complete) {
        st.frames_complete++;
    } else {
        if (sent > 0 && sent < expected) {
            st.frames_partial++;
        } else if (!had_err) {
            st.frames_partial++;
        }
        if (had_err || sent == 0) {
            st.send_errors++;
        }
    }
}

template <typename SenderT, typename = void>
struct has_send_spectrum_ts : std::false_type {};
template <typename SenderT>
struct has_send_spectrum_ts<SenderT, std::void_t<decltype(
    std::declval<SenderT&>().send_spectrum(std::declval<const std::vector<float>&>(), uint64_t{0}))>> : std::true_type {};

template <typename SenderT, typename = void>
struct has_send_frame_ts : std::false_type {};
template <typename SenderT>
struct has_send_frame_ts<SenderT, std::void_t<decltype(
    std::declval<SenderT&>().send_frame(std::declval<const std::vector<float>&>(), uint64_t{0}))>> : std::true_type {};

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
void sdr_stream_callback(short* xi,
                         short* xq,
                         [[maybe_unused]] sdrplay_api_StreamCbParamsT* params,
                         unsigned int numSamples,
                         unsigned int reset,
                         void* cbContext) {
    auto* rb = static_cast<SharedMemoryRingBuffer*>(cbContext);
    if (rb == nullptr || numSamples == 0) {
        return;
    }
    if (reset != 0) {
        g_child_stream_resets.fetch_add(1, std::memory_order_relaxed);
    }
    if (!rb->is_stream_started()) {
        return;
    }
    (void)rb->write(xi, xq, static_cast<uint32_t>(numSamples));
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

    err = sdrplay_api_Init(devs[selected_idx].dev, &cb_fns, producer_rb.get());
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
    std::cout << "[Network] Cấu hình SOL2 v2 UDP Telemetry (Đích: " << cli.dest_ip << "):\n";
    std::cout << "  + S12 (Cross-Power dB) : Port " << cli.port_s12 << " [ENABLED]\n";
    std::cout << "  + S11 (Auto-Power 1 dB): " << (cli.port_s11 > 0 ? ("Port " + std::to_string(cli.port_s11)) : "DISABLED (0)") << "\n";
    std::cout << "  + S22 (Auto-Power 2 dB): " << (cli.port_s22 > 0 ? ("Port " + std::to_string(cli.port_s22)) : "DISABLED (0)") << "\n";
    std::cout << "  + AUX (" << std::setw(9) << std::left << cli.aux_mode << ")      : "
              << (cli.port_aux > 0 ? ("Port " + std::to_string(cli.port_aux)) : "DISABLED (0)") << "\n";

    ChildProcessGuard children;
    children.pid1 = ::fork();
    if (children.pid1 < 0) {
        std::cerr << "[LỖI HỆ THỐNG] Lỗi fork() tiến trình con Kênh 1: " << std::strerror(errno) << "\n";
        return 1;
    }
    if (children.pid1 == 0) {
        instance_lock.release();
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
        instance_lock.release();
        run_sdr_producer_process(2, ser_ch2, SolarConfig::SHM::CH2_NAME);
    }

    std::optional<HardwareGuard> hw_guard;
    if (!cli.skip_align) {
        hw_guard.emplace(); // Gọi HardwareGuard() mặc định: bật Cal + Noise Source 15V
    }
    int exit_code = 0;

    try {
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
            std::cout << "[Supervisor] Cả 2 kênh RSPdx đã sẵn sàng. Kích hoạt luồng I/Q đồng thời và chờ 250ms ổn định DMA...\n";
            rb1.set_start_stream(true);
            rb2.set_start_stream(true);

            const auto warmup_end = std::chrono::steady_clock::now() + std::chrono::milliseconds(250);
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
                std::cout << "\n[STATE: ALIGNING] Bắt đầu căn chỉnh độ trễ mẫu khởi động trên nguồn tạp âm chuẩn (N = "
                          << SolarConfig::Alignment::SNAPSHOT_SIZE
                          << ", ngưỡng PNR >= " << SolarConfig::Alignment::PNR_THRESHOLD_DB
                          << " dB, tối đa " << cli.max_align_retries << " lần thử)...\n";

                TimeAligner aligner(SolarConfig::Alignment::SNAPSHOT_SIZE);
                bool locked = false;

                for (int attempt = 1; attempt <= cli.max_align_retries && g_running.load(std::memory_order_relaxed); ++attempt) {
                    rb1.flush();
                    rb2.flush();

                    if (!wait_for_samples(SolarConfig::Alignment::SNAPSHOT_SIZE, 3000)) {
                        if (!g_running.load(std::memory_order_relaxed)) break;
                        std::cerr << "  [Thử " << attempt << "/" << cli.max_align_retries
                                  << "] Quá hạn chờ nạp " << SolarConfig::Alignment::SNAPSHOT_SIZE << " mẫu.\n";
                        continue;
                    }

                    AlignmentResult initial_res {};
                    const uint32_t av1_before_align = rb1.available_read();
                    const uint32_t av2_before_align = rb2.available_read();

                    const bool align_ok = api_bridge::execute_buffer_alignment(
                        aligner, rb1, rb2, initial_res);

                    std::cout << std::fixed << std::setprecision(2)
                              << "  [Thử " << attempt << "/" << cli.max_align_retries << "] "
                              << "Đo ban đầu: k_offset = " << initial_res.lag_samples << " mẫu ("
                              << initial_res.delay_microsec << " us), PNR = " << initial_res.pnr_db << " dB -> ";

                    if (!align_ok || !initial_res.is_valid) {
                        std::cout << "CHƯA ĐẠT NGƯỠNG PNR!\n";
                        continue;
                    }

                    if (rb1.available_read() == av1_before_align &&
                        rb2.available_read() == av2_before_align &&
                        initial_res.lag_samples != 0) {
                        if (initial_res.lag_samples > 0) {
                            rb1.skip(static_cast<uint32_t>(initial_res.lag_samples));
                        } else {
                            rb2.skip(static_cast<uint32_t>(-initial_res.lag_samples));
                        }
                    }

                    std::this_thread::sleep_for(std::chrono::milliseconds(10));
                    if (!wait_for_samples(SolarConfig::Alignment::SNAPSHOT_SIZE, 3000)) {
                        if (!g_running.load(std::memory_order_relaxed)) break;
                        std::cout << "Lỗi chờ mẫu hậu kiểm!\n";
                        continue;
                    }

                    AlignmentResult verify_res = aligner.measure_offset_from_buffers(rb1, rb2);
                    std::cout << "Hậu kiểm: k = " << verify_res.lag_samples << " mẫu, PNR = "
                              << verify_res.pnr_db << " dB ";

                    if (verify_res.is_valid && std::abs(verify_res.lag_samples) <= 1) {
                        std::cout << "=> [LOCKED k=" << verify_res.lag_samples << "]\n";
                        locked = true;
                        is_sample_aligned = true;
                        break;
                    } else {
                        std::cout << "=> [CHƯA KHÓA ĐỈNH]\n";
                    }
                }

                if (!g_running.load(std::memory_order_relaxed)) {
                    state = SystemState::STOPPING;
                } else if (!locked) {
                    std::cerr << "[FATAL][FAIL-CLOSED] Không thể khóa đồng bộ mẫu (k = 0) sau "
                              << cli.max_align_retries
                              << " lần thử! Ngắt nguồn 15V Noise Source và dừng hệ thống an toàn.\n";
                    if (hw_guard.has_value()) {
                        hw_guard->turn_off();
                    }
                    state = SystemState::STOPPING;
                    exit_code = 1;
                }
            }
        }

        if (state != SystemState::STOPPING) {
            FXCorrelator::Options fx_opts {};
            fx_opts.fft_size = SolarConfig::Correlator::FFT_SIZE;
            fx_opts.frames   = SolarConfig::Correlator::INTEGRATION_FRAMES;
            fx_opts.running  = &g_running;

            FXCorrelator fx(fx_opts);

            const size_t n_bins = SolarConfig::Correlator::FFT_SIZE;
            std::vector<float> phi_rad(n_bins, 0.0f);
            std::vector<float> s12_db(n_bins, 0.0f);
            std::vector<float> phase_rad(n_bins, 0.0f);
            std::vector<float> coherence(n_bins, 0.0f);
            std::vector<float> s11_db(n_bins, 0.0f);
            std::vector<float> s22_db(n_bins, 0.0f);

            if (cli.cal_phase) {
                state = SystemState::CALIBRATING;
                std::cout << "\n[STATE: CALIBRATING] Đang chụp đáp tuyến pha thiết bị tĩnh (phi_rad) khi Noise Source đang bật...\n";

                if (!api_bridge::invoke_prime_buffers(fx, rb1, rb2)) {
                    throw std::runtime_error("Lỗi prime_buffers() trước khi chạy chu kỳ --cal-phase.");
                }

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
                            phi_rad[k] = -phase_rad[k];
                            locked_phase_bins++;
                        } else {
                            phi_rad[k] = 0.0f;
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

            // NGẮT NGUỒN TẠP ÂM 15V VÀ GẠT RF SWITCH VỀ 2 ANTEN NGOÀI TRỜI TRƯỚC KHI QUAN SÁT
            if (state != SystemState::STOPPING && hw_guard.has_value() && hw_guard->is_active()) {
                const uint32_t ov1_pre_sw = rb1.get_overflow_count();
                const uint32_t ov2_pre_sw = rb2.get_overflow_count();

                hw_guard->turn_off();

                const uint32_t common_avail = std::min(rb1.available_read(), rb2.available_read());
                if (common_avail > 0) {
                    (void)rb1.skip(common_avail);
                    (void)rb2.skip(common_avail);
                }

                if (rb1.get_overflow_count() != ov1_pre_sw || rb2.get_overflow_count() != ov2_pre_sw) {
                    throw std::runtime_error(
                        "[FAIL-CLOSED] Xảy ra tràn bộ đệm SHM trong lúc chuyển mạch RF Switch về Anten!");
                }
            }

            if (state != SystemState::STOPPING) {
                if (!api_bridge::invoke_prime_buffers(fx, rb1, rb2)) {
                    if (g_running.load(std::memory_order_relaxed)) {
                        throw std::runtime_error("Lỗi prime_buffers() khởi tạo cửa sổ trượt 50% Welch.");
                    }
                    state = SystemState::STOPPING;
                }
            }

            TelemetrySender sender_s12(cli.dest_ip, cli.port_s12);
            std::optional<TelemetrySender> sender_s11;
            std::optional<TelemetrySender> sender_s22;
            std::optional<TelemetrySender> sender_aux;

            if (cli.port_s11 > 0) sender_s11.emplace(cli.dest_ip, cli.port_s11);
            if (cli.port_s22 > 0) sender_s22.emplace(cli.dest_ip, cli.port_s22);
            if (cli.port_aux > 0) sender_aux.emplace(cli.dest_ip, cli.port_aux);

            api_bridge::PortTelemetryStats stats_s12 {};
            api_bridge::PortTelemetryStats stats_s11 {};
            api_bridge::PortTelemetryStats stats_s22 {};
            api_bridge::PortTelemetryStats stats_aux {};

            if (state != SystemState::STOPPING) {
                state = SystemState::RUNNING;
                std::cout << "\n[STATE: " << state_to_string(state, is_sample_aligned, is_phase_calibrated)
                          << "] Bắt đầu vòng lặp tích phân Welch FX Correlator (~200 ms/frame)...\n";
                std::cout << "--------------------------------------------------------------------------------\n";

                uint64_t valid_frames_count = 0;
                int diag_recovery_count = 0;
                constexpr int kMaxDiagRecoveries = 5;
                auto last_report_time = std::chrono::steady_clock::now();

                while (g_running.load(std::memory_order_relaxed) && state == SystemState::RUNNING) {
                    std::string child_err;
                    if (!children.check_children_alive(child_err)) {
                        std::cerr << "\n[FATAL][SUPERVISOR] " << child_err << "\n";
                        state = SystemState::STOPPING;
                        exit_code = 1;
                        break;
                    }

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

                    if (!g_running.load(std::memory_order_relaxed)) {
                        state = SystemState::STOPPING;
                        break;
                    }

                    const uint32_t ov1_after = rb1.get_overflow_count();
                    const uint32_t ov2_after = rb2.get_overflow_count();
                    const bool overflow_occurred = (ov1_after > ov1_before) || (ov2_after > ov2_before);

                    if (!cycle_ok || overflow_occurred) {
                        state = SystemState::RECOVERING;
                        std::cerr << "\n[STATE: RECOVERING] Phát hiện gián đoạn chu kỳ tích phân "
                                  << "(cycle_ok=" << cycle_ok
                                  << ", ov_ch1=" << ov1_before << "->" << ov1_after
                                  << ", ov_ch2=" << ov2_before << "->" << ov2_after
                                  << (cycle_err_msg.empty() ? "" : (", err=" + cycle_err_msg)) << ").\n";

                        if (is_phase_calibrated) {
                            std::cerr << "[FATAL][FAIL-CLOSED] Tràn bộ đệm làm mất mốc đồng bộ mẫu, "
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

                    convert_linear_auto_power_to_db(fx.auto_power1(), s11_db);
                    convert_linear_auto_power_to_db(fx.auto_power2(), s22_db);

                    const uint64_t timestamp_us = current_utc_timestamp_us();

                    api_bridge::dispatch_udp_spectrum(sender_s12, valid_frames_count, timestamp_us, s12_db, stats_s12);
                    if (sender_s11.has_value()) {
                        api_bridge::dispatch_udp_spectrum(*sender_s11, valid_frames_count, timestamp_us, s11_db, stats_s11);
                    }
                    if (sender_s22.has_value()) {
                        api_bridge::dispatch_udp_spectrum(*sender_s22, valid_frames_count, timestamp_us, s22_db, stats_s22);
                    }
                    if (sender_aux.has_value()) {
                        const std::vector<float>& aux_vec =
                            (cli.aux_mode == "phase") ? phase_rad : coherence;
                        api_bridge::dispatch_udp_spectrum(*sender_aux, valid_frames_count, timestamp_us, aux_vec, stats_aux);
                    }

                    valid_frames_count++;

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
                                  << "Frame #" << std::setw(5) << valid_frames_count << " | "
                                  << std::fixed << std::setprecision(2)
                                  << "|S12|_max=" << peak_s12_val << " dB @" << std::setprecision(3) << peak_freq_mhz << " MHz | "
                                  << std::setprecision(1)
                                  << "S11=" << peak_s11_val << " dB, S22=" << peak_s22_val << " dB | "
                                  << std::setprecision(3) << "Coh_max=" << max_coh_val << " | "
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
                        if (sender_aux.has_value()) {
                            std::cout << " aux:" << stats_aux.frames_complete << "/" << stats_aux.send_errors;
                        }
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

    children.terminate_and_reap_all();
    SharedMemoryRingBuffer::unlink_shm(SolarConfig::SHM::CH1_NAME);
    SharedMemoryRingBuffer::unlink_shm(SolarConfig::SHM::CH2_NAME);
    instance_lock.release();

    std::cout << "[Supervisor] Hoàn tất đóng hệ thống an toàn (exit_code = " << exit_code << ").\n";
    return exit_code;
}
