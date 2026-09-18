#include "time_alignment.hpp"
#include <cmath>
#include <iostream>
#include <algorithm>

TimeAligner::TimeAligner(uint32_t snapshot_size)
    : n_fft_(snapshot_size), temp_buf1_(snapshot_size), temp_buf2_(snapshot_size) {
    
    in_ch1_     = fftwf_alloc_complex(n_fft_);
    in_ch2_     = fftwf_alloc_complex(n_fft_);
    out_ch1_    = fftwf_alloc_complex(n_fft_);
    out_ch2_    = fftwf_alloc_complex(n_fft_);
    xcorr_freq_ = fftwf_alloc_complex(n_fft_);
    xcorr_time_ = fftwf_alloc_complex(n_fft_);

    plan_fwd_ch1_ = fftwf_plan_dft_1d(static_cast<int>(n_fft_), in_ch1_, out_ch1_, FFTW_FORWARD, FFTW_ESTIMATE);
    plan_fwd_ch2_ = fftwf_plan_dft_1d(static_cast<int>(n_fft_), in_ch2_, out_ch2_, FFTW_FORWARD, FFTW_ESTIMATE);
    plan_bwd_     = fftwf_plan_dft_1d(static_cast<int>(n_fft_), xcorr_freq_, xcorr_time_, FFTW_BACKWARD, FFTW_ESTIMATE);
}

TimeAligner::~TimeAligner() {
    if (plan_fwd_ch1_) fftwf_destroy_plan(plan_fwd_ch1_);
    if (plan_fwd_ch2_) fftwf_destroy_plan(plan_fwd_ch2_);
    if (plan_bwd_)     fftwf_destroy_plan(plan_bwd_);

    if (in_ch1_)     fftwf_free(in_ch1_);
    if (in_ch2_)     fftwf_free(in_ch2_);
    if (out_ch1_)    fftwf_free(out_ch1_);
    if (out_ch2_)    fftwf_free(out_ch2_);
    if (xcorr_freq_) fftwf_free(xcorr_freq_);
    if (xcorr_time_) fftwf_free(xcorr_time_);
}

AlignmentResult TimeAligner::measure_offset(const ComplexSample* ch1_data, const ComplexSample* ch2_data) {
    AlignmentResult result;

    // Trừ DC offset nội tại
    double sum_i1 = 0, sum_q1 = 0, sum_i2 = 0, sum_q2 = 0;
    for (uint32_t i = 0; i < n_fft_; ++i) {
        sum_i1 += ch1_data[i].i; sum_q1 += ch1_data[i].q;
        sum_i2 += ch2_data[i].i; sum_q2 += ch2_data[i].q;
    }
    const float mean_i1 = static_cast<float>(sum_i1 / n_fft_);
    const float mean_q1 = static_cast<float>(sum_q1 / n_fft_);
    const float mean_i2 = static_cast<float>(sum_i2 / n_fft_);
    const float mean_q2 = static_cast<float>(sum_q2 / n_fft_);

    for (uint32_t i = 0; i < n_fft_; ++i) {
        in_ch1_[i][0] = static_cast<float>(ch1_data[i].i) - mean_i1;
        in_ch1_[i][1] = static_cast<float>(ch1_data[i].q) - mean_q1;
        in_ch2_[i][0] = static_cast<float>(ch2_data[i].i) - mean_i2;
        in_ch2_[i][1] = static_cast<float>(ch2_data[i].q) - mean_q2;
    }

    fftwf_execute(plan_fwd_ch1_);
    fftwf_execute(plan_fwd_ch2_);

    // Tương quan phổ: S_xy = out_ch1 * conj(out_ch2)
    for (uint32_t k = 0; k < n_fft_; ++k) {
        const float xr = out_ch1_[k][0];
        const float xi = out_ch1_[k][1];
        const float yr = out_ch2_[k][0];
        const float yi = out_ch2_[k][1];

        xcorr_freq_[k][0] = xr * yr + xi * yi;
        xcorr_freq_[k][1] = xi * yr - xr * yi;
    }

    fftwf_execute(plan_bwd_);

    float max_mag = -1.0f;
    uint32_t peak_idx = 0;
    std::vector<float> magnitudes(n_fft_);

    for (uint32_t m = 0; m < n_fft_; ++m) {
        const float r = xcorr_time_[m][0];
        const float im = xcorr_time_[m][1];
        const float mag = std::sqrt(r * r + im * im);
        magnitudes[m] = mag;

        if (mag > max_mag) {
            max_mag = mag;
            peak_idx = m;
        }
    }

    int32_t lag = 0;
    if (peak_idx < (n_fft_ / 2)) {
        lag = static_cast<int32_t>(peak_idx);
    } else {
        lag = static_cast<int32_t>(peak_idx) - static_cast<int32_t>(n_fft_);
    }

    double noise_sum = 0.0;
    uint32_t noise_count = 0;
    const int32_t win = static_cast<int32_t>(SolarConfig::Alignment::PEAK_SEARCH_WINDOW);
    const int32_t p_idx = static_cast<int32_t>(peak_idx);

    for (uint32_t m = 0; m < n_fft_; ++m) {
        int32_t dist = std::abs(static_cast<int32_t>(m) - p_idx);
        if (dist > static_cast<int32_t>(n_fft_ / 2)) {
            dist = static_cast<int32_t>(n_fft_) - dist;
        }
        if (dist > win) {
            noise_sum += magnitudes[m];
            noise_count++;
        }
    }

    const float noise_floor = (noise_count > 0) ? static_cast<float>(noise_sum / noise_count) : 1e-6f;
    const float pnr_linear = max_mag / (noise_floor + 1e-6f);
    const float pnr_db = 20.0f * std::log10(pnr_linear);

    result.lag_samples    = lag;
    result.delay_microsec = (static_cast<double>(lag) / SolarConfig::SDR::SAMPLE_RATE_HZ) * 1e6;
    result.peak_mag       = max_mag;
    result.noise_floor    = noise_floor;
    result.pnr_db         = pnr_db;
    result.is_valid       = (pnr_db >= SolarConfig::Alignment::PNR_THRESHOLD_DB);

    return result;
}

AlignmentResult TimeAligner::measure_offset_from_buffers(const SharedMemoryRingBuffer& rb1, const SharedMemoryRingBuffer& rb2) {
    if (rb1.available_read() < n_fft_ || rb2.available_read() < n_fft_) {
        AlignmentResult res;
        res.is_valid = false;
        return res;
    }

    rb1.peek_from_tail(temp_buf1_.data(), n_fft_);
    rb2.peek_from_tail(temp_buf2_.data(), n_fft_);

    return measure_offset(temp_buf1_.data(), temp_buf2_.data());
}