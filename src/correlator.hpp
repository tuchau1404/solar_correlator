#pragma once

#include <vector>
#include <cstdint>
#include <fftw3.h>
#include "config.hpp"
#include "ring_buffer.hpp"

class FXCorrelator {
public:
    FXCorrelator();
    ~FXCorrelator();

    // Vô hiệu hóa copy
    FXCorrelator(const FXCorrelator&) = delete;
    FXCorrelator& operator=(const FXCorrelator&) = delete;

    // Nạp trước nửa bộ đệm để bắt đầu cơ chế cửa sổ trượt
    void prime_buffers(SharedMemoryRingBuffer& rb1, SharedMemoryRingBuffer& rb2);

    // Xử lý 1 chu kỳ tích phân trọn vẹn (M = 1953 khung)
    // Các mảng out_ db, phase, coherence phải được cấp phát trước với kích thước N = 2048
    void process_integration_cycle(SharedMemoryRingBuffer& rb1, 
                                   SharedMemoryRingBuffer& rb2,
                                   const std::vector<float>& fringe_phase_rad,
                                   std::vector<float>& out_spectrum_db,
                                   std::vector<float>& out_phase_rad,
                                   std::vector<float>& out_coherence);

private:
    const uint32_t n_fft_;
    const uint32_t hop_size_;
    const uint32_t m_frames_;
    float window_power_norm_;

    // Bộ đệm trượt trên L1/L2 Cache
    std::vector<ComplexSample> slide_buf1_;
    std::vector<ComplexSample> slide_buf2_;
    std::vector<float> window_; // Mảng Hanning

    // Vùng nhớ ARM NEON SIMD cho FFTW3
    fftwf_complex* in1_{nullptr};
    fftwf_complex* in2_{nullptr};
    fftwf_complex* out1_{nullptr};
    fftwf_complex* out2_{nullptr};
    fftwf_plan plan1_{nullptr};
    fftwf_plan plan2_{nullptr};

    // Vector Accumulators (Chuẩn Welch)
    std::vector<float> accum_s12_real_;
    std::vector<float> accum_s12_imag_;
    std::vector<float> accum_s11_;
    std::vector<float> accum_s22_;
};