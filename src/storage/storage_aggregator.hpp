#pragma once
#include "storage_record.hpp"
#include <optional>

namespace solar::storage {
enum class PartialPolicy { Emit, Discard };
struct AggregationConfig {
    std::size_t input_bins, output_bins;
    std::uint64_t fft_size, hop_samples, source_frames;
    double sample_rate_hz, center_frequency_hz, requested_interval_sec;
    PartialPolicy partial_policy;
};
// Single worker ownership. No clock/filesystem/network calls; never stores a
// borrowed reference. push may emit the old partial and a complete new window.
class Aggregator {
public:
    explicit Aggregator(AggregationConfig config);
    std::vector<OutputRecord> push(const SourceRecord& record);
    std::optional<OutputRecord> finish();
    std::vector<double> frequency_hz() const;
    double effective_cadence_sec() const;
    std::uint64_t source_records_per_window() const { return target_records_; }
    std::uint64_t discarded_partial_records() const { return discarded_; }
private:
    void validate(const SourceRecord&) const;
    std::optional<OutputRecord> flush(std::uint32_t reason);
    AggregationConfig config_;
    std::uint64_t target_records_, count_ = 0, discarded_ = 0, coverage_samples_ = 0;
    std::array<std::vector<double>, COMPONENTS> sums_;
    std::vector<unsigned char> valid_;
    OutputRecord pending_;
    bool have_previous_ = false;
    std::uint64_t previous_frame_ = 0, previous_segment_ = 0, previous_calibration_ = 0;
    std::uint64_t previous_sample_end_ = 0, previous_start_utc_ = 0, previous_end_utc_ = 0;
};
} // namespace solar::storage
