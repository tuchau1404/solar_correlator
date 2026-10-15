#pragma once
#include <array>
#include <cerrno>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <string>
#include <system_error>
#include <vector>
#include <arpa/inet.h>
#include <sys/random.h>
#include <sys/socket.h>
#include <unistd.h>
#include "dsp_config.hpp"
#include "network_config.hpp"

// SOL2 v2: all integers and IEEE-754 binary32 payload words are big-endian.
// No ABI struct is placed on the wire. One instance has one sending thread.
class TelemetrySender {
public:
    static constexpr std::size_t bins = SolarConfig::Correlator::FFT_SIZE;
    static constexpr std::size_t chunk_bins = 256;
    static constexpr std::size_t header_bytes = 52;
    static constexpr std::size_t chunks = bins / chunk_bins;
    static constexpr std::size_t packet_bytes = header_bytes + chunk_bins * 4;
    static_assert(bins == 2048 && bins % chunk_bins == 0);
    static_assert(sizeof(float) == 4 && std::numeric_limits<float>::is_iec559);
    struct Stats {
        std::uint64_t attempted{}, queued{}, failed{}, datagrams{}, invalid{};
        std::uint64_t would_block{}, no_buffers{}, too_large{}, interrupted{}, other{};
        int last_errno{};
    };
    struct Result { bool queued; unsigned datagrams; int error; };

    explicit TelemetrySender(const std::string& ip, std::uint16_t port) {
        dest_.sin_family = AF_INET;
        dest_.sin_port = htons(port);
        if (port == 0 || inet_pton(AF_INET, ip.c_str(), &dest_.sin_addr) != 1)
            throw std::invalid_argument("Invalid destination IPv4/port");
        // Initialization only. A fresh session distinguishes sender restarts.
        auto* p = reinterpret_cast<unsigned char*>(&session_);
        std::size_t done = 0;
        while (done < sizeof(session_)) {
            const auto n = getrandom(p + done, sizeof(session_) - done, 0);
            if (n < 0 && errno == EINTR) continue;
            if (n <= 0) throw std::system_error(n < 0 ? errno : EIO, std::generic_category(), "getrandom");
            done += static_cast<std::size_t>(n);
        }
        if (!session_) session_ = 1;
        sock_ = socket(AF_INET, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
        if (sock_ < 0) throw std::system_error(errno, std::generic_category(), "socket");
        try {
            int requested = 256 * 1024;
            if (setsockopt(sock_, SOL_SOCKET, SO_SNDBUF, &requested, sizeof(requested)) < 0)
                throw std::system_error(errno, std::generic_category(), "SO_SNDBUF");
            // Fail explicitly on an unexpectedly small path MTU, rather than fragment.
            int mode = IP_PMTUDISC_DO;
            if (setsockopt(sock_, IPPROTO_IP, IP_MTU_DISCOVER, &mode, sizeof(mode)) < 0)
                throw std::system_error(errno, std::generic_category(), "IP_MTU_DISCOVER");
            socklen_t len = sizeof(actual_sndbuf_);
            if (getsockopt(sock_, SOL_SOCKET, SO_SNDBUF, &actual_sndbuf_, &len) < 0)
                throw std::system_error(errno, std::generic_category(), "getsockopt");
        } catch (...) { close(sock_); sock_ = -1; throw; }
    }
    ~TelemetrySender() { if (sock_ >= 0) close(sock_); }
    TelemetrySender(const TelemetrySender&) = delete;
    TelemetrySender& operator=(const TelemetrySender&) = delete;
    TelemetrySender(TelemetrySender&&) = delete;
    TelemetrySender& operator=(TelemetrySender&&) = delete;
    const Stats& stats() const noexcept { return stats_; }
    int send_buffer_bytes() const noexcept { return actual_sndbuf_; }

    // No userspace allocation, logging, sleep or retry in this path.
    // queued=true means kernel acceptance, not receiver delivery.
    Result send_spectrum(std::uint64_t id, std::uint64_t utc_us,
                         const std::vector<float>& values) noexcept {
        ++stats_.attempted;
        if (values.size() != bins) return invalid();
        for (float x : values) if (!std::isfinite(x)) return invalid();
        put(0, SolarConfig::Network::TELEMETRY_MAGIC, 4);
        put(4, 2, 2); put(6, header_bytes, 2);
        put(8, session_, 8); put(16, id, 8); put(24, utc_us, 8);
        put(32, SolarConfig::Correlator::INTEGRATION_FRAMES, 4);
        put(36, 1, 4); // 10log10(|S12| / (1 ADC-count^2/bin)), fftshifted
        put(40, bins, 2); put(44, chunks, 2); put(46, chunk_bins, 2);
        unsigned sent = 0;
        for (std::size_t c = 0; c < chunks; ++c) {
            put(42, c, 2); put(48, c * chunk_bins, 4);
            for (std::size_t i = 0; i < chunk_bins; ++i) {
                std::uint32_t bits;
                std::memcpy(&bits, &values[c * chunk_bins + i], 4);
                put(header_bytes + i * 4, bits, 4);
            }
            auto n = sendto(sock_, packet_.data(), packet_.size(), MSG_DONTWAIT,
                           reinterpret_cast<const sockaddr*>(&dest_), sizeof(dest_));
            if (n != static_cast<ssize_t>(packet_.size())) {
                const int e = n < 0 ? errno : EIO;
                ++stats_.failed; stats_.last_errno = e;
                if (e == EAGAIN || e == EWOULDBLOCK) ++stats_.would_block;
                else if (e == ENOBUFS) ++stats_.no_buffers;
                else if (e == EMSGSIZE) ++stats_.too_large;
                else if (e == EINTR) ++stats_.interrupted;
                else ++stats_.other;
                return {false, sent, e}; // Abandon remainder of this frame.
            }
            ++sent; ++stats_.datagrams;
        }
        ++stats_.queued;
        return {true, sent, 0};
    }
private:
    Result invalid() noexcept {
        ++stats_.invalid; ++stats_.failed; stats_.last_errno = EINVAL;
        return {false, 0, EINVAL};
    }
    void put(std::size_t offset, std::uint64_t value, unsigned width) noexcept {
        for (unsigned i = 0; i < width; ++i)
            packet_[offset + i] = static_cast<std::uint8_t>(value >> (8 * (width - 1 - i)));
    }
    int sock_{-1}, actual_sndbuf_{};
    sockaddr_in dest_{};
    std::uint64_t session_{};
    std::array<std::uint8_t, packet_bytes> packet_{};
    Stats stats_{};
};
