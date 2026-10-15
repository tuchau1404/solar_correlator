#pragma once
#include <cmath>
#include <cstdint>
#include <ostream>
#include <stdexcept>

namespace StartupAlignment {
struct Options {
    uint32_t snapshot_size = 131072;
    unsigned attempts = 8;
    unsigned confirmations = 3;
    float min_pnr_db = 20.0f; // Proposed calibration threshold; tune with hardware data.
};
class Failed : public std::runtime_error {
public:
    Failed() : std::runtime_error("startup alignment exhausted retries") {}
};

// Single consumer. wait_ready checks cancellation, continuity, and a deadline.
// measure copies a snapshot and computes correlation without consuming it.
// Fresh confirmations advance BOTH tails by N, preserving their relative offset.
template<class Ring, class Wait, class Measure>
void acquire(Ring& a, Ring& b, const Options& o,
             Wait wait_ready, Measure measure, std::ostream& log) {
    if (!o.snapshot_size || !o.attempts || !o.confirmations ||
        !std::isfinite(o.min_pnr_db))
        throw std::invalid_argument("invalid startup alignment options");
    const auto confident = [&](const auto& r) {
        return r.is_valid && std::isfinite(r.pnr_db) && r.pnr_db >= o.min_pnr_db &&
               std::abs(int64_t(r.lag_samples)) < int64_t(o.snapshot_size / 2);
    };
    for (unsigned attempt = 1; attempt <= o.attempts; ++attempt) {
        // Discard old trials. This does NOT establish hardware T0.
        a.flush(); b.flush();
        wait_ready();
        const auto candidate = measure();
        log << "[AUTO-ALIGN " << attempt << '/' << o.attempts << "] lag="
            << candidate.lag_samples << " PNR=" << candidate.pnr_db << " dB\n";
        if (!confident(candidate)) {
            log << "[AUTO-ALIGN] Weak/invalid candidate: no correction; retry fresh capture.\n";
            continue;
        }
        const int64_t lag = candidate.lag_samples;
        if (lag > 0 && a.skip(static_cast<uint32_t>(lag)) != static_cast<uint32_t>(lag))
            throw std::runtime_error("short startup skip CH1");
        if (lag < 0 && b.skip(static_cast<uint32_t>(-lag)) != static_cast<uint32_t>(-lag))
            throw std::runtime_error("short startup skip CH2");

        unsigned good = 0;
        for (; good < o.confirmations; ++good) {
            // Skip at least a complete window BEFORE each verification, so
            // verification never reuses the samples used to estimate the lag.
            wait_ready();
            if (a.skip(o.snapshot_size) != o.snapshot_size ||
                b.skip(o.snapshot_size) != o.snapshot_size)
                throw std::runtime_error("short paired startup advance");
            wait_ready();
            const auto verify = measure();
            log << "[AUTO-ALIGN VERIFY " << good + 1 << '/' << o.confirmations
                << "] lag=" << verify.lag_samples << " PNR=" << verify.pnr_db << " dB\n";
            if (!confident(verify) || std::abs(int64_t(verify.lag_samples)) > 1)
                break;
        }
        if (good == o.confirmations) {
            wait_ready(); // Recheck health after the final FFT.
            log << "[AUTO-ALIGN] LOCKED: starting stability test.\n";
            return;
        }
        log << "[AUTO-ALIGN] Verification failed: retry fresh capture.\n";
    }
    throw Failed{};
}
} // namespace StartupAlignment
