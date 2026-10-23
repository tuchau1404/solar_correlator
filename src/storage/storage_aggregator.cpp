#include "storage_aggregator.hpp"
#include <algorithm>
#include <cmath>
#include <limits>
#include <stdexcept>

namespace solar::storage {
namespace {
constexpr double nan = std::numeric_limits<double>::quiet_NaN();
constexpr double rounding_tolerance = 32 * std::numeric_limits<float>::epsilon();
std::uint64_t add(std::uint64_t a, std::uint64_t b) {
    if (b > std::numeric_limits<std::uint64_t>::max() - a)
        throw std::overflow_error("storage sample/weight counter overflow");
    return a + b;
}
}
DerivedBin derive_bin(double a, double b, double re, double im) {
    DerivedBin d{nan, nan, nan, nan, false};
    if (!std::isfinite(a) || !std::isfinite(b) || !std::isfinite(re) ||
        !std::isfinite(im) || a < 0 || b < 0) return d;
    d.magnitude = std::hypot(re, im);
    if (!std::isfinite(d.magnitude)) return {nan,nan,nan,nan,true};
    if (d.magnitude > 0) d.phase_rad = std::atan2(im, re);
    if (a > 0 && b > 0) {
        const double gamma = (d.magnitude / std::sqrt(a)) / std::sqrt(b);
        if (!std::isfinite(gamma) || gamma > 1 + rounding_tolerance) {
            d.coherence_violation = true;
            d.phase_rad = nan;
        } else {
            d.coherence_magnitude = std::min(gamma, 1.0);
            d.coherence_squared = d.coherence_magnitude*d.coherence_magnitude;
        }
    } else if (d.magnitude > 0) {
        d.coherence_violation = true;
        d.phase_rad = nan;
    }
    return d;
}
Aggregator::Aggregator(AggregationConfig c) : config_(c) {
    if (c.input_bins < 4 || c.fft_size != c.input_bins ||
        c.output_bins == 0 || c.input_bins % c.output_bins != 0 ||
        c.hop_samples == 0 || c.hop_samples > c.fft_size || c.source_frames == 0 ||
        !std::isfinite(c.sample_rate_hz) || c.sample_rate_hz <= 0 ||
        !std::isfinite(c.center_frequency_hz) || c.center_frequency_hz <= 0 ||
        !std::isfinite(c.requested_interval_sec) || c.requested_interval_sec <= 0)
        throw std::invalid_argument("invalid storage aggregation geometry");
    if (c.source_frames > (std::numeric_limits<std::uint64_t>::max()-c.fft_size)/c.hop_samples)
        throw std::invalid_argument("source geometry overflows sample counter");
    const long double cadence = static_cast<long double>(c.source_frames)*c.hop_samples/c.sample_rate_hz;
    if (c.requested_interval_sec < cadence*(1-1e-12L))
        throw std::invalid_argument("save interval shorter than source cadence");
    const long double n = std::ceil(c.requested_interval_sec/cadence - 1e-12L);
    // Bound counters exactly; input double need not represent UINT64_MAX.
    if (n < 1 || n > static_cast<long double>(std::numeric_limits<std::uint64_t>::max()/c.source_frames/c.hop_samples))
        throw std::invalid_argument("save interval overflows sample counters");
    target_records_ = static_cast<std::uint64_t>(n);
    for (auto& s : sums_) s.resize(c.output_bins);
    valid_.resize(c.output_bins);
}
void Aggregator::validate(const SourceRecord& r) const {
    for (const auto& x : r.values)
        if (x.size() != config_.input_bins) throw std::invalid_argument("source bin shape mismatch");
    if (r.valid_fft_frames == 0 || r.valid_fft_frames > config_.source_frames ||
        r.sample_end <= r.sample_start || r.estimated_end_utc_us < r.estimated_start_utc_us)
        throw std::invalid_argument("invalid source support/weight/time");
    const auto expected = (r.valid_fft_frames-1)*config_.hop_samples + config_.fft_size;
    if (r.sample_end-r.sample_start != expected)
        throw std::invalid_argument("support must describe contiguous valid Welch frames");
    // Missing frames inside a source record require a richer support contract.
    // A caller must split them into contiguous records, never invent coverage.
}
std::vector<OutputRecord> Aggregator::push(const SourceRecord& r) {
    validate(r);
    std::vector<OutputRecord> result;
    const bool same_segment = have_previous_ && r.segment_id == previous_segment_;
    if (same_segment && r.frame_id <= previous_frame_)
        throw std::invalid_argument("duplicate or out-of-order source frame");
    const auto overlap = config_.fft_size-config_.hop_samples;
    const bool boundary = have_previous_ && r.calibration_id != previous_calibration_;
    const bool gap = have_previous_ && (!same_segment ||
        previous_frame_ == std::numeric_limits<std::uint64_t>::max() ||
        r.frame_id != previous_frame_+1 || r.sample_start != previous_sample_end_-overlap ||
        r.estimated_start_utc_us < previous_start_utc_ || r.estimated_end_utc_us < previous_end_utc_);
    const std::uint32_t transition=(boundary?CALIBRATION_BOUNDARY:0u)|(gap?DISCONTINUITY:0u);
    if (count_ && transition) {
        auto o=flush(transition); if(o) result.push_back(std::move(*o));
    }
    if (!count_) {
        pending_ = OutputRecord{};
        pending_.frame_first=r.frame_id; pending_.segment_id=r.segment_id;
        pending_.calibration_id=r.calibration_id; pending_.sample_start=r.sample_start;
        pending_.start_utc_us=r.estimated_start_utc_us;
        pending_.backend_timestamp_first_us=r.backend_timestamp_us;
        for (auto& s:sums_) std::fill(s.begin(),s.end(),0.0);
        std::fill(valid_.begin(),valid_.end(),1);
        coverage_samples_=r.sample_end-r.sample_start;
    } else {
        coverage_samples_=add(coverage_samples_,r.sample_end-pending_.sample_end);
    }
    pending_.frame_last=r.frame_id; pending_.sample_end=r.sample_end;
    pending_.end_utc_us=r.estimated_end_utc_us;
    pending_.backend_timestamp_last_us=r.backend_timestamp_us;
    pending_.valid_fft_frames=add(pending_.valid_fft_frames,r.valid_fft_frames);
    pending_.quality |= r.quality | transition;
    have_previous_=true; previous_frame_=r.frame_id; previous_segment_=r.segment_id;
    previous_calibration_=r.calibration_id; previous_sample_end_=r.sample_end;
    previous_start_utc_=r.estimated_start_utc_us; previous_end_utc_=r.estimated_end_utc_us;
    const std::size_t group=config_.input_bins/config_.output_bins;
    const double weight=static_cast<double>(r.valid_fft_frames);
    for(std::size_t f=0; f<config_.output_bins; ++f) {
        if (!valid_[f]) continue;
        for(std::size_t j=f*group; j<(f+1)*group; ++j) {
            bool ok=r.values[S11][j]>=0 && r.values[S22][j]>=0;
            for(std::size_t k=0;k<COMPONENTS;++k) ok &= std::isfinite(r.values[k][j]);
            if (!ok) {valid_[f]=0;break;}
            for(std::size_t k=0;k<COMPONENTS;++k) {
                sums_[k][f] += r.values[k][j]*weight/static_cast<double>(group);
                if (!std::isfinite(sums_[k][f])) valid_[f]=0;
            }
        }
    }
    ++count_;
    // Weight-based cadence handles partial contiguous source integrations too.
    if (pending_.valid_fft_frames >= target_records_*config_.source_frames) {
        auto o=flush(0); if(o) result.push_back(std::move(*o));
    }
    return result;
}
std::optional<OutputRecord> Aggregator::flush(std::uint32_t reason) {
    if (!count_) return std::nullopt;
    const bool partial=pending_.valid_fft_frames<target_records_*config_.source_frames;
    if (partial && config_.partial_policy==PartialPolicy::Discard) {
        discarded_=add(discarded_,count_);count_=0;return std::nullopt;
    }
    pending_.quality |= reason|(partial?PARTIAL_WINDOW:0u);
    pending_.timestamp_us=pending_.start_utc_us+(pending_.end_utc_us-pending_.start_utc_us)/2;
    pending_.span_sec=static_cast<double>(pending_.sample_end-pending_.sample_start)/config_.sample_rate_hz;
    pending_.covered_sec=static_cast<double>(coverage_samples_)/config_.sample_rate_hz;
    pending_.cadence_sec=static_cast<double>(pending_.valid_fft_frames)*config_.hop_samples/config_.sample_rate_hz;
    for(auto& v:pending_.values) v.resize(config_.output_bins);
    for(std::size_t f=0;f<config_.output_bins;++f) {
        std::array<double,COMPONENTS> v{};
        for(std::size_t k=0;k<COMPONENTS;++k) {
            v[k]=sums_[k][f]/static_cast<double>(pending_.valid_fft_frames);
            if (!std::isfinite(v[k]) || std::abs(v[k])>std::numeric_limits<float>::max()) valid_[f]=0;
        }
        if (!valid_[f]) pending_.quality |= INVALID_BIN;
        else if(derive_bin(v[S11],v[S22],v[S12_REAL],v[S12_IMAG]).coherence_violation)
            pending_.quality |= COHERENCE_VIOLATION;
        for(std::size_t k=0;k<COMPONENTS;++k)
            pending_.values[k][f]=valid_[f]?static_cast<float>(v[k]):static_cast<float>(nan);
    }
    count_=0;
    return std::move(pending_);
}
std::optional<OutputRecord> Aggregator::finish() {return flush(0);}
double Aggregator::effective_cadence_sec() const {
    return static_cast<double>(target_records_)*config_.source_frames*config_.hop_samples/config_.sample_rate_hz;
}
std::vector<double> Aggregator::frequency_hz() const {
    const std::size_t group=config_.input_bins/config_.output_bins;
    const double df=config_.sample_rate_hz/static_cast<double>(config_.input_bins);
    std::vector<double> out(config_.output_bins);
    for(std::size_t f=0;f<out.size();++f)
        out[f]=config_.center_frequency_hz+
            (static_cast<double>(f*group)+(static_cast<double>(group)-1)/2-static_cast<double>(config_.input_bins)/2)*df;
    return out;
}
} // namespace solar::storage
