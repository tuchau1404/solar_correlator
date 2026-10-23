#pragma once
#include "storage_aggregator.hpp"
#include "storage_config.hpp"
#include "storage_queue.hpp"
#include <atomic>
#include <condition_variable>
#include <mutex>
#include <thread>
namespace solar::storage {
enum class PipelineState { RUNNING, DISABLED, PAUSED_QUOTA, PAUSED_DISK, STOPPING, ERROR };
enum class PipelineSubmit { ACCEPTED, QUEUE_FULL, DISABLED, PAUSED_QUOTA, PAUSED_DISK, STOPPING, ERROR, INVALID_RECORD };
class Pipeline {
public:
    Pipeline(Config config,AggregationConfig geometry,nlohmann::json metadata,std::vector<float> calibration,bool defer_start=false);
    // Owner thread only, before any submissions; worker has not started yet.
    void start_observation(bool aligned,bool phase_calibrated,const std::vector<float>& calibration);
    ~Pipeline();
    Pipeline(const Pipeline&)=delete;Pipeline& operator=(const Pipeline&)=delete;
    PipelineSubmit try_submit(std::uint64_t frame,std::uint64_t backend_utc,const std::vector<double>& a,const std::vector<double>& b,const std::vector<double>& re,const std::vector<double>& im) noexcept;
    void stop();
private:
    void worker() noexcept;
    Config config_;AggregationConfig geometry_;nlohmann::json metadata_;std::vector<float> calibration_;
    SourceQueue queue_;SourceRecord producer_;
    std::atomic<PipelineState> state_{PipelineState::RUNNING};
    std::atomic<bool> stopping_{false};std::atomic<std::uint64_t> dropped_{0};
    std::condition_variable wake_;std::mutex wait_mutex_;std::thread thread_;
    bool anchored_=false;std::uint64_t last_frame_=0,anchor_=0,sample_=0,segment_=0;
};
}
