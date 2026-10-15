#include <chrono>
#include <cmath>
#include <csignal>
#include <iostream>
#include <thread>
#include <vector>
#include "telemetry_sender.hpp"
namespace {
volatile std::sig_atomic_t running = 1;
extern "C" void stop(int) { running = 0; }
}
int main(int argc, char** argv) {
    try {
        const std::string ip = argc > 1 ? argv[1] : SolarConfig::Network::UDP_DEST_IP;
        unsigned long port = SolarConfig::Network::UDP_DEST_PORT;
        if (argc > 2) {
            std::size_t used = 0;
            port = std::stoul(argv[2], &used);
            if (used != std::string(argv[2]).size() || port == 0 || port > 65535)
                throw std::invalid_argument("port must be 1..65535");
        }
        const auto limit = argc > 3 ? std::stoull(argv[3]) : 0;
        struct sigaction action {};
        action.sa_handler = stop;
        sigemptyset(&action.sa_mask);
        if (sigaction(SIGINT, &action, nullptr) || sigaction(SIGTERM, &action, nullptr))
            throw std::system_error(errno, std::generic_category(), "sigaction");
        TelemetrySender sender(ip, static_cast<std::uint16_t>(port));
        std::vector<float> spectrum(TelemetrySender::bins);
        using Clock = std::chrono::steady_clock;
        constexpr auto period = std::chrono::milliseconds(200);
        const auto origin = Clock::now();
        auto deadline = origin;
        std::uint64_t id = 0, skipped = 0, sent = 0;
        std::cout << "SOL2 -> " << ip << ':' << port << " SO_SNDBUF="
                  << sender.send_buffer_bytes() << '\n';
        while (running && (!limit || sent < limit)) {
            std::this_thread::sleep_until(deadline);
            if (!running) break;
            auto now = Clock::now();
            if (now - deadline >= period) {
                auto missed = (now - deadline) / period;
                id += missed; skipped += missed; deadline += period * missed;
            }
            const double t = std::chrono::duration<double>(now - origin).count();
            const int peak = 1024 + static_cast<int>(300 * std::sin(t * 0.25));
            const double peak_db = 70 + 8 * std::sin(t * 0.4);
            for (std::size_t i = 0; i < spectrum.size(); ++i) {
                // Deterministic display fixture; not a statistical Welch/ADC simulation.
                const double floor_db = 25 + 3 * std::sin(i * 0.173 + t);
                const double delta = static_cast<double>(i) - peak;
                const double power = std::pow(10.0, floor_db / 10.0)
                    + std::pow(10.0, peak_db / 10.0) * std::exp(-delta * delta / 2.0);
                spectrum[i] = static_cast<float>(10 * std::log10(power));
            }
            const auto utc = std::chrono::duration_cast<std::chrono::microseconds>(
                std::chrono::system_clock::now().time_since_epoch()).count();
            sender.send_spectrum(id, static_cast<std::uint64_t>(utc), spectrum);
            if (sent % 25 == 0)
                std::cout << "frame=" << id << " peak=" << spectrum[peak]
                          << " queued=" << sender.stats().queued << " failed=" << sender.stats().failed
                          << " errno=" << sender.stats().last_errno << " skipped=" << skipped << '\n';
            ++id; ++sent; deadline += period;
        }
        std::cout << "Exit: queued=" << sender.stats().queued << " failed=" << sender.stats().failed << '\n';
        return sender.stats().failed ? 2 : 0;
    } catch (const std::exception& e) { std::cerr << e.what() << '\n'; return 1; }
}
