#pragma once
#include <array>
#include <cstddef>
#include <cstdint>
#include <vector>

namespace solar::storage {
// The order is a schema contract. Values are mean power per INPUT FFT bin,
// ADC-count^2, after the same phase-only calibration for the cross product.
enum Component : std::size_t { S11, S22, S12_REAL, S12_IMAG, COMPONENTS };
enum Quality : std::uint32_t {
    UTC_ESTIMATED = 1u << 0, INVALID_BIN = 1u << 1,
    PARTIAL_WINDOW = 1u << 2, DISCONTINUITY = 1u << 3,
    CALIBRATION_BOUNDARY = 1u << 4, COHERENCE_VIOLATION = 1u << 5
};
struct SourceRecord {
    explicit SourceRecord(std::size_t bins = 0) { for (auto& x : values) x.resize(bins); }
    std::array<std::vector<double>, COMPONENTS> values;
    std::uint64_t frame_id = 0, segment_id = 0, calibration_id = 0;
    // Half-open sample support, relative to segment. These are NOT UTC times.
    std::uint64_t sample_start = 0, sample_end = 0, valid_fft_frames = 0;
    // Original backend processing-wall-clock timestamp retained separately.
    std::uint64_t backend_timestamp_us = 0;
    // Caller-supplied UTC estimate + quality; no hidden system-clock reads.
    std::uint64_t estimated_start_utc_us = 0, estimated_end_utc_us = 0;
    std::uint32_t quality = UTC_ESTIMATED;
};
struct OutputRecord {
    std::array<std::vector<float>, COMPONENTS> values;
    std::uint64_t frame_first = 0, frame_last = 0, segment_id = 0, calibration_id = 0;
    std::uint64_t sample_start = 0, sample_end = 0, valid_fft_frames = 0;
    std::uint64_t start_utc_us = 0, end_utc_us = 0, timestamp_us = 0;
    std::uint64_t backend_timestamp_first_us = 0, backend_timestamp_last_us = 0;
    // Unique support union, not valid_fft_frames*N/Fs. Not independent samples.
    double span_sec = 0, covered_sec = 0, cadence_sec = 0;
    std::uint32_t quality = 0;
};
struct DerivedBin {
    double magnitude, phase_rad, coherence_magnitude, coherence_squared;
    bool coherence_violation;
};
DerivedBin derive_bin(double s11, double s22, double real, double imag);
} // namespace solar::storage
