#include "correlator.hpp"
#include <cmath>
#include <cstring>
#include <algorithm>

FXCorrelator::FXCorrelator()
    : n_fft_(SolarConfig::Correlator::FFT_SIZE),
      hop_size_(SolarConfig::Correlator::HOP_SIZE),
      m_frames_(SolarConfig::Correlator::INTEGRATION_FRAMES),
      slide_buf1_(n_fft_), slide_buf2_(n_fft_), window_(n_fft_),
      accum_s12_real_(n_fft_, 0.0f), accum_s12_imag_(n_fft_, 0.0f),
      accum_s11_(n_fft_, 0.0f), accum_s22_(n_fft_, 0.0f) 
{
    // 1. Tính trước mảng cửa sổ Hanning và hệ số chuẩn hóa năng lượng (S2)
    float s2 = 0.0f;
    for (uint32_t n = 0; n < n_fft_; ++n) {
        window_[n] = 0.5f * (1.0f - std::cos(2.0f * static_cast<float>(M_PI) * n / (n_fft_ - 1)));
        s2 += window_[n] * window_[n];
    }
    window_power_norm_ = s2;

    // 2. Cấp phát bộ nhớ căn chỉnh chuẩn SIMD 64-byte cho FFTW3
    in1_  = fftwf_alloc_complex(n_fft_);
    in2_  = fftwf_alloc_complex(n_fft_);
    out1_ = fftwf_alloc_complex(n_fft_);
    out2_ = fftwf_alloc_complex(n_fft_);

    // 3. Khởi tạo Kế hoạch (Plans) với cờ FFTW_MEASURE tận dụng tối đa ARM NEON
    plan1_ = fftwf_plan_dft_1d(n_fft_, in1_, out1_, FFTW_FORWARD, FFTW_MEASURE);
    plan2_ = fftwf_plan_dft_1d(n_fft_, in2_, out2_, FFTW_FORWARD, FFTW_MEASURE);
}

FXCorrelator::~FXCorrelator() {
    if (plan1_) fftwf_destroy_plan(plan1_);
    if (plan2_) fftwf_destroy_plan(plan2_);
    if (in1_) fftwf_free(in1_);
    if (in2_) fftwf_free(in2_);
    if (out1_) fftwf_free(out1_);
    if (out2_) fftwf_free(out2_);
}

void FXCorrelator::prime_buffers(SharedMemoryRingBuffer& rb1, SharedMemoryRingBuffer& rb2) {
    // Chờ Ring Buffer tích lũy đủ số mẫu khởi tạo
    while (rb1.available_read() < hop_size_ || rb2.available_read() < hop_size_) {
        // Block nhẹ để đợi
    }
    // Nạp mồi vào nửa sau của bộ đệm trượt
    rb1.read(&slide_buf1_[hop_size_], hop_size_);
    rb2.read(&slide_buf2_[hop_size_], hop_size_);
}

void FXCorrelator::process_integration_cycle(SharedMemoryRingBuffer& rb1, 
                                             SharedMemoryRingBuffer& rb2,
                                             const std::vector<float>& fringe_phase_rad,
                                             std::vector<float>& out_spectrum_db,
                                             std::vector<float>& out_phase_rad,
                                             std::vector<float>& out_coherence) 
{
    // Reset Vector Accumulators
    std::fill(accum_s12_real_.begin(), accum_s12_real_.end(), 0.0f);
    std::fill(accum_s12_imag_.begin(), accum_s12_imag_.end(), 0.0f);
    std::fill(accum_s11_.begin(), accum_s11_.end(), 0.0f);
    std::fill(accum_s22_.begin(), accum_s22_.end(), 0.0f);

    for (uint32_t frame = 0; frame < m_frames_; ++frame) {
        // [CƠ CHẾ SLIDING WINDOW] - Trượt 1024 mẫu cũ lên đầu
        std::memcpy(&slide_buf1_[0], &slide_buf1_[hop_size_], hop_size_ * sizeof(ComplexSample));
        std::memcpy(&slide_buf2_[0], &slide_buf2_[hop_size_], hop_size_ * sizeof(ComplexSample));

        // Rút 1024 mẫu MỚI từ Ring Buffers vào nửa sau
        while (rb1.available_read() < hop_size_ || rb2.available_read() < hop_size_) {
            // Chờ spin-lock nhẹ - Dữ liệu thực tế 1024 mẫu tốn ~102 us để đến nơi
        }
        rb1.read(&slide_buf1_[hop_size_], hop_size_);
        rb2.read(&slide_buf2_[hop_size_], hop_size_);

        // F-ENGINE: Nhân cửa sổ Hanning & ép kiểu float
        for (uint32_t n = 0; n < n_fft_; ++n) {
            float w = window_[n];
            in1_[n][0] = static_cast<float>(slide_buf1_[n].i) * w;
            in1_[n][1] = static_cast<float>(slide_buf1_[n].q) * w;
            
            in2_[n][0] = static_cast<float>(slide_buf2_[n].i) * w;
            in2_[n][1] = static_cast<float>(slide_buf2_[n].q) * w;
        }

        // Thực thi phân rã phổ
        fftwf_execute(plan1_);
        fftwf_execute(plan2_);

        // X-ENGINE: Nhân giao thoa, bù trễ vi mô & cộng dồn
        for (uint32_t m = 0; m < n_fft_; ++m) {
            float r1 = out1_[m][0], i1 = out1_[m][1];
            float r2 = out2_[m][0], i2 = out2_[m][1];

            // Phổ tương quan thô: S12_raw = X1 * conj(X2)
            float s12_raw_re = r1 * r2 + i1 * i2;
            float s12_raw_im = i1 * r2 - r1 * i2;

            // Xoay pha (Fringe Stopping): S12_corr = S12_raw * exp(-j * Phi)
            float phi = fringe_phase_rad[m];
            float cos_phi = std::cos(phi);
            float sin_phi = std::sin(phi);

            float s12_corr_re = s12_raw_re * cos_phi + s12_raw_im * sin_phi;
            float s12_corr_im = s12_raw_im * cos_phi - s12_raw_re * sin_phi;

            // Phổ tự tương quan
            float s11 = r1 * r1 + i1 * i1;
            float s22 = r2 * r2 + i2 * i2;

            // Accumulate
            accum_s12_real_[m] += s12_corr_re;
            accum_s12_imag_[m] += s12_corr_im;
            accum_s11_[m] += s11;
            accum_s22_[m] += s22;
        }
    }

    // POST-PROCESSING: Chuẩn hóa, FFT Shift, Decibel
    float norm_factor = 1.0f / (m_frames_ * window_power_norm_);
    float eps = SolarConfig::Correlator::DYNAMIC_RANGE_EPS;

    for (uint32_t m = 0; m < n_fft_; ++m) {
        // Dịch FFT đưa bin trung tâm (DC 35.0 MHz) ra giữa trục đồ thị (bin 1024)
        uint32_t shifted_idx = (m + n_fft_ / 2) % n_fft_;

        float avg_s12_re = accum_s12_real_[m] * norm_factor;
        float avg_s12_im = accum_s12_imag_[m] * norm_factor;
        float avg_s11    = accum_s11_[m] * norm_factor;
        float avg_s22    = accum_s22_[m] * norm_factor;

        float mag_s12 = std::sqrt(avg_s12_re * avg_s12_re + avg_s12_im * avg_s12_im);
        
        // P_dB: Chuyển đổi mức hiển thị logarit
        out_spectrum_db[shifted_idx] = 10.0f * std::log10(mag_s12 + eps);
        
        // Góc pha bù trừ (Radians)
        out_phase_rad[shifted_idx] = std::atan2(avg_s12_im, avg_s12_re);
        
        // Độ kết hợp chuẩn hóa (Normalized Coherence)
        float denominator = std::sqrt(avg_s11 * avg_s22);
        out_coherence[shifted_idx] = (denominator > eps) ? (mag_s12 / denominator) : 0.0f;
    }
}