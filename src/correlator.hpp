#pragma once
#include <atomic>
#include <chrono>
#include <cstdint>
#include <vector>
#include <fftw3.h>
#include "ring_buffer.hpp"

// Single DSP-owner object. Serialize FFTW plan creation/destruction with all other
// FFTW users in the process. Separate objects may execute independent plans.
class FXCorrelator {
public:
    struct Options {
        uint32_t fft_size = 2048;
        uint32_t frames = 1953;
        std::chrono::milliseconds timeout{5000}; // whole prime/cycle deadline
        const std::atomic<bool>* running = nullptr; // false requests cancellation
    };
    FXCorrelator();
    explicit FXCorrelator(Options options);
    ~FXCorrelator();
    FXCorrelator(const FXCorrelator&) = delete;
    FXCorrelator& operator=(const FXCorrelator&) = delete;
    void prime_buffers(SharedMemoryRingBuffer&, SharedMemoryRingBuffer&);
    // phi: UNSHIFTED FFT order, predicted arg(X1*conj(X2)), constant in cycle.
    // Output: shifted order; 10log10(|cross power| / 1 ADC-count^2), power/bin.
    // Sum of linear auto powers equals window-weighted mean-square power.
    // Zero power: -120 dB display floor, phase/coherence = 0 (invalid estimate).
    // A timeout/drop/cancel invalidates state: resynchronize streams then re-prime.
    void process_integration_cycle(SharedMemoryRingBuffer&, SharedMemoryRingBuffer&,
        const std::vector<float>& phi, std::vector<float>& db,
        std::vector<float>& phase, std::vector<float>& coherence);
    const std::vector<double>& auto_power1() const { return power1_; }
    const std::vector<double>& auto_power2() const { return power2_; }
    // Same shifted order, normalization and phase-only correction as auto_power.
    // Read only after a successful cycle, on the DSP-owner thread; copy before
    // the next cycle. A failed cycle does not publish a valid new snapshot.
    const std::vector<double>& cross_real() const { return cross_real_; }
    const std::vector<double>& cross_imag() const { return cross_imag_; }
    double window_energy() const { return s2_; }
private:
    using Clock = std::chrono::steady_clock;
    void cleanup() noexcept;
    void check(SharedMemoryRingBuffer&, SharedMemoryRingBuffer&, Clock::time_point);
    void read_pair(SharedMemoryRingBuffer&, SharedMemoryRingBuffer&,
                   uint32_t offset, Clock::time_point);
    Options options_;
    uint32_t n_, hop_, old_offset_ = 0;
    uint32_t drops1_ = 0, drops2_ = 0;
    bool primed_ = false;
    double s2_ = 0;
    std::vector<ComplexSample> samples1_, samples2_;
    std::vector<float> window_;
    std::vector<double> re_, im_, a1_, a2_, power1_, power2_, cross_real_, cross_imag_;
    fftwf_complex *in1_ = nullptr, *in2_ = nullptr;
    fftwf_complex *out1_ = nullptr, *out2_ = nullptr;
    fftwf_plan plan1_ = nullptr, plan2_ = nullptr;
};
