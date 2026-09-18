#pragma once

#include <cstdint>
#include <vector>
#include <fftw3.h>
#include "config.hpp"
#include "ring_buffer.hpp"

struct AlignmentResult {
    int32_t lag_samples{0};      // >0: CH1 chạy trước CH2; <0: CH2 chạy trước CH1; =0: Khóa hoàn toàn
    double delay_microsec{0.0};  // Độ trễ thời gian (us)
    float peak_mag{0.0f};        // Biên độ đỉnh tương quan
    float noise_floor{0.0f};     // Mức sàn nhiễu
    float pnr_db{0.0f};          // Tỷ số Peak-to-Noise (dB)
    bool is_valid{false};        // Đạt ngưỡng PNR
};

class TimeAligner {
public:
    explicit TimeAligner(uint32_t snapshot_size = SolarConfig::Alignment::SNAPSHOT_SIZE);
    ~TimeAligner();

    TimeAligner(const TimeAligner&) = delete;
    TimeAligner& operator=(const TimeAligner&) = delete;

    AlignmentResult measure_offset(const ComplexSample* ch1_data, const ComplexSample* ch2_data);
    AlignmentResult measure_offset_from_buffers(const SharedMemoryRingBuffer& rb1, const SharedMemoryRingBuffer& rb2);

private:
    uint32_t n_fft_;
    fftwf_complex* in_ch1_{nullptr};
    fftwf_complex* in_ch2_{nullptr};
    fftwf_complex* out_ch1_{nullptr};
    fftwf_complex* out_ch2_{nullptr};
    fftwf_complex* xcorr_freq_{nullptr};
    fftwf_complex* xcorr_time_{nullptr};

    fftwf_plan plan_fwd_ch1_{nullptr};
    fftwf_plan plan_fwd_ch2_{nullptr};
    fftwf_plan plan_bwd_{nullptr};

    std::vector<ComplexSample> temp_buf1_;
    std::vector<ComplexSample> temp_buf2_;
};