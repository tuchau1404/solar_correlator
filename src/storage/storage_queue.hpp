#pragma once
#include "storage_record.hpp"
#include <atomic>
#include <algorithm>
#include <limits>
#include <stdexcept>

namespace solar::storage {
enum class SubmitResult { ACCEPTED, QUEUE_FULL, INVALID_RECORD };
// Exactly one producer and one consumer. All slots are allocated at construction.
// Producer source may be reused on return. Consumer output must be pre-sized.
// No allocations, OS locks, logs or database calls in either operation.
class SourceQueue {
public:
    SourceQueue(std::size_t capacity, std::size_t bins) : bins_(bins) {
        if (!capacity || !bins || capacity==std::numeric_limits<std::size_t>::max())
            throw std::invalid_argument("queue dimensions must be positive and bounded");
        slots_.reserve(capacity+1);
        for(std::size_t i=0;i<=capacity;++i) slots_.emplace_back(bins);
    }
    SubmitResult try_push(const SourceRecord& record) noexcept {
        for(const auto& v:record.values) if(v.size()!=bins_) return SubmitResult::INVALID_RECORD;
        const auto head=head_.load(std::memory_order_relaxed), next=(head+1)%slots_.size();
        const auto tail=tail_.load(std::memory_order_acquire);
        if(next==tail) return SubmitResult::QUEUE_FULL;
        copy(record,slots_[head]);
        head_.store(next,std::memory_order_release);
        const auto depth=(next+slots_.size()-tail)%slots_.size();
        if(depth>high_water_.load(std::memory_order_relaxed)) high_water_.store(depth,std::memory_order_relaxed);
        return SubmitResult::ACCEPTED;
    }
    bool try_pop(SourceRecord& record) noexcept {
        for(const auto& v:record.values) if(v.size()!=bins_) return false;
        const auto tail=tail_.load(std::memory_order_relaxed);
        if(tail==head_.load(std::memory_order_acquire)) return false;
        copy(slots_[tail],record);
        tail_.store((tail+1)%slots_.size(),std::memory_order_release);
        return true;
    }
    std::size_t capacity() const { return slots_.size()-1; }
    std::size_t depth() const { const auto h=head_.load(std::memory_order_acquire); const auto t=tail_.load(std::memory_order_acquire); return (h+slots_.size()-t)%slots_.size(); }
    std::size_t high_water() const { return high_water_.load(std::memory_order_relaxed); }
    std::size_t allocated_payload_bytes() const { return slots_.size()*bins_*COMPONENTS*sizeof(double); }
private:
    static void copy(const SourceRecord& a,SourceRecord& b) noexcept {
        for(std::size_t k=0;k<COMPONENTS;++k) std::copy(a.values[k].begin(),a.values[k].end(),b.values[k].begin());
        b.frame_id=a.frame_id; b.segment_id=a.segment_id; b.calibration_id=a.calibration_id;
        b.sample_start=a.sample_start;b.sample_end=a.sample_end;b.valid_fft_frames=a.valid_fft_frames;
        b.backend_timestamp_us=a.backend_timestamp_us;
        b.estimated_start_utc_us=a.estimated_start_utc_us;b.estimated_end_utc_us=a.estimated_end_utc_us;
        b.quality=a.quality;
    }
    std::size_t bins_;
    std::vector<SourceRecord> slots_;
    alignas(64) std::atomic<std::size_t> head_{0};
    alignas(64) std::atomic<std::size_t> tail_{0};
    std::atomic<std::size_t> high_water_{0};
};
} // namespace solar::storage
