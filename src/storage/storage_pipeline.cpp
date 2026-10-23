#include "storage_pipeline.hpp"
#include "storage_filename.hpp"
#include "storage_budget.hpp"
#include "hdf5_writer.hpp"
#include <chrono>
#include <filesystem>
#include <fstream>
#include <fcntl.h>
#include <sys/file.h>
#include <sys/statvfs.h>
#include <sys/syscall.h>
#include <unistd.h>
namespace solar::storage {
namespace {
using Clock=std::chrono::steady_clock;namespace fs=std::filesystem;
std::uint64_t utc(){return std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::system_clock::now().time_since_epoch()).count();}
std::uint64_t free_bytes(const std::string& path){struct statvfs s{};if(statvfs(path.c_str(),&s))throw std::runtime_error("statvfs");return std::uint64_t(s.f_bavail)*s.f_frsize;}
void publish(const std::string& tmp,const std::string& final){
    // Linux renameat2 is atomic and refuses an existing destination.
    if(syscall(SYS_renameat2,AT_FDCWD,tmp.c_str(),AT_FDCWD,final.c_str(),1))throw std::runtime_error("publish rename failed");
    int fd=open(fs::path(final).parent_path().c_str(),O_RDONLY|O_DIRECTORY|O_CLOEXEC);if(fd<0)throw std::runtime_error("open artifact directory");int rc=fsync(fd);close(fd);if(rc)throw std::runtime_error("fsync artifact directory");
}
const char* name(PipelineState s){switch(s){case PipelineState::RUNNING:return "RUNNING";case PipelineState::DISABLED:return "DISABLED";case PipelineState::PAUSED_QUOTA:return "PAUSED_QUOTA";case PipelineState::PAUSED_DISK:return "PAUSED_DISK";case PipelineState::STOPPING:return "STOPPING";default:return "ERROR";}}
}
Pipeline::Pipeline(Config c,AggregationConfig g,nlohmann::json m,std::vector<float> phi,bool defer_start):config_(std::move(c)),geometry_(g),metadata_(std::move(m)),calibration_(std::move(phi)),queue_(config_.value.at("storage").at("queue_capacity_records"),g.input_bins),producer_(g.input_bins){producer_.calibration_id=metadata_.at("calibration_id").get<std::uint64_t>();if(!config_.value.at("storage").at("enabled").get<bool>()){state_=PipelineState::DISABLED;return;}if(defer_start){state_=PipelineState::DISABLED;return;}thread_=std::thread(&Pipeline::worker,this);}
void Pipeline::start_observation(bool aligned,bool phase_calibrated,const std::vector<float>& phi) {
    if(thread_.joinable() || stopping_ || !config_.value.at("storage").at("enabled").get<bool>())
        throw std::logic_error("storage activation requires a deferred enabled pipeline");
    if(phi.size()!=calibration_.size()) throw std::invalid_argument("calibration size");
    std::copy(phi.begin(),phi.end(),calibration_.begin());
    metadata_["aligned"]=aligned;
    metadata_["k_offset_samples"]=aligned?nlohmann::json(0):nlohmann::json(nullptr);
    metadata_["phase_calibrated"]=phase_calibrated;
    metadata_["calibration_id"]=phase_calibrated?1:0;
    producer_.calibration_id=phase_calibrated?1:0;
    state_=PipelineState::RUNNING;
    thread_=std::thread(&Pipeline::worker,this);
}
Pipeline::~Pipeline(){stop();}
void Pipeline::stop(){stopping_=true;wake_.notify_one();if(thread_.joinable())thread_.join();}
PipelineSubmit Pipeline::try_submit(std::uint64_t frame,std::uint64_t stamp,const std::vector<double>& a,const std::vector<double>& b,const std::vector<double>& re,const std::vector<double>& im) noexcept {
    if(stopping_){dropped_++;return PipelineSubmit::STOPPING;}
    auto state=state_.load(std::memory_order_relaxed);
    if(state!=PipelineState::RUNNING){dropped_++;switch(state){
        case PipelineState::DISABLED:return PipelineSubmit::DISABLED;
        case PipelineState::PAUSED_QUOTA:return PipelineSubmit::PAUSED_QUOTA;
        case PipelineState::PAUSED_DISK:return PipelineSubmit::PAUSED_DISK;
        case PipelineState::STOPPING:return PipelineSubmit::STOPPING;
        default:return PipelineSubmit::ERROR;}}

    if(a.size()!=geometry_.input_bins||b.size()!=a.size()||re.size()!=a.size()||im.size()!=a.size()){dropped_++;return PipelineSubmit::INVALID_RECORD;}
    const auto span=(geometry_.source_frames-1)*geometry_.hop_samples+geometry_.fft_size;
    if(!anchored_||frame!=last_frame_+1){++segment_;sample_=0;const auto duration=std::uint64_t(span/geometry_.sample_rate_hz*1e6);anchor_=stamp>duration?stamp-duration:0;anchored_=true;}
    producer_.frame_id=frame;producer_.segment_id=segment_;
    producer_.sample_start=sample_;producer_.sample_end=sample_+span;producer_.valid_fft_frames=geometry_.source_frames;
    producer_.backend_timestamp_us=stamp;producer_.estimated_start_utc_us=anchor_+std::uint64_t(sample_/geometry_.sample_rate_hz*1e6);producer_.estimated_end_utc_us=anchor_+std::uint64_t((sample_+span)/geometry_.sample_rate_hz*1e6);producer_.quality=UTC_ESTIMATED;
    const std::vector<double>* src[]={&a,&b,&re,&im};for(std::size_t k=0;k<COMPONENTS;++k)std::copy(src[k]->begin(),src[k]->end(),producer_.values[k].begin());
    last_frame_=frame;sample_+=geometry_.source_frames*geometry_.hop_samples;
    if(queue_.try_push(producer_)!=SubmitResult::ACCEPTED){dropped_++;return PipelineSubmit::QUEUE_FULL;}
    wake_.notify_one();
    return PipelineSubmit::ACCEPTED;
}
void Pipeline::worker() noexcept {
    const auto& c=config_.value;const auto& s=c.at("storage");const auto data=s.at("data_dir").get<std::string>();
    std::unique_ptr<Budget> budget_ptr;
    std::unique_ptr<Hdf5Writer> writer;std::optional<FileGrant> grant;int owner=-1;
    std::uint64_t counter=0,output_dropped=0;std::string error,paused_day;
    auto last_status=Clock::time_point::min(),last_flush=Clock::now(),drain_deadline=Clock::time_point::max();
    auto release=[&]{if(owner>=0){close(owner);owner=-1;}};
    auto status=[&]{auto now=Clock::now();if(last_status!=Clock::time_point::min()&&std::chrono::duration<double>(now-last_status).count()<c.at("status").at("interval_sec").get<double>()&&!stopping_)return;
        nlohmann::json j={{"status_schema_version",1},{"timestamp_utc_us",utc()},{"session",metadata_.at("session_id")},{"baseline_revision",metadata_.at("baseline_revision")},{"storage",name(state_)},{"source_dropped_records",dropped_.load()},{"output_dropped_records",output_dropped},{"queue_depth",queue_.depth()},{"queue_high_water",queue_.high_water()},{"error",error.substr(0,1024)},{"free_bytes",free_bytes(data)},{"workers","See per-worker status files; this snapshot does not certify maintenance health."}};
        if(budget_ptr)j["quota"]=budget_ptr->snapshot(observation_day(utc()));
        auto text=j.dump();if(text.size()>c.at("status").at("max_bytes").get<std::size_t>())throw std::runtime_error("status size cap");auto path=c.at("status").at("path").get<std::string>();auto tmp=path+".tmp";
        {std::ofstream f(tmp,std::ios::binary|std::ios::trunc);f<<text;f.flush();if(!f)throw std::runtime_error("status write");}fs::rename(tmp,path);last_status=now;};
    try{
        budget_ptr=std::make_unique<Budget>(config_);auto& budget=*budget_ptr;Aggregator aggregator(geometry_);SourceRecord incoming(geometry_.input_bins);
        std::vector<OutputRecord> batch;batch.reserve(s.at("append_batch_records"));std::uint64_t first_stamp=0;
        auto close_file=[&]{if(!writer)return;if(!batch.empty()){writer->append(batch);batch.clear();}writer->close();writer.reset();auto tmp=grant->path+".tmp";publish(tmp,grant->path);budget.ready(*grant,fs::file_size(grant->path));release();grant.reset();};
        auto quarantine=[&]{writer.reset();batch.clear();if(grant){budget.quarantine(*grant);grant.reset();}release();};
        auto accept=[&](OutputRecord record){
            auto day=observation_day(record.timestamp_us);
            if(writer&&(day!=grant->day||record.start_utc_us-first_stamp>=std::uint64_t(s.at("roll_minutes").get<double>()*60e6)))close_file();
            if(free_bytes(data)<c.at("budget").at("min_free_bytes").get<std::uint64_t>()){close_file();state_=PipelineState::PAUSED_DISK;++output_dropped;return;}
            if(!writer){
                if(free_bytes(data)<c.at("budget").at("min_free_bytes").get<std::uint64_t>()+s.at("max_file_bytes").get<std::uint64_t>()){state_=PipelineState::PAUSED_DISK;++output_dropped;return;}
                auto id=metadata_.at("session_id").get<std::string>()+"_"+std::to_string(counter++);auto path=(fs::path(data)/(observation_filename(record.start_utc_us,id))).string();
                auto lockpath=fs::path(c.at("ledger").at("path").get<std::string>()).parent_path()/"artifact_locks"/(id+".lock");
                fs::create_directories(lockpath.parent_path());owner=open(lockpath.c_str(),O_CREAT|O_RDWR|O_CLOEXEC|O_NOFOLLOW,0600);if(owner<0||flock(owner,LOCK_EX|LOCK_NB))throw std::runtime_error("artifact ownership lock");
                grant=budget.reserve(id,path,day,record.start_utc_us,metadata_.at("session_id"));
                auto meta=metadata_;meta["observation_day"]=day;meta["artifact_id"]=id;meta["data_schema_version"]=1;meta["effective_config_json"]=config_.value.dump();meta["config_hash"]=config_.hash;meta["quicklook_enabled"]=c.at("quicklook").at("enabled");meta["source_dropped_records_at_open"]=dropped_.load();meta["output_dropped_records_at_open"]=output_dropped;meta["effective_save_interval_sec"]=aggregator.effective_cadence_sec();meta["requested_save_interval_sec"]=geometry_.requested_interval_sec;
                writer=std::make_unique<Hdf5Writer>(path+".tmp",grant->cap,meta,aggregator.frequency_hz(),calibration_,s.at("chunk_rows"),s.at("compression"),s.at("compression_level"));first_stamp=record.start_utc_us;
            }
            batch.push_back(std::move(record));if(batch.size()>=s.at("append_batch_records").get<std::size_t>()){writer->append(batch);batch.clear();}
        };
        unsigned io_failures=0;auto retry_after=Clock::now();
        auto failed=[&](const std::exception& e){error=e.what();quarantine();aggregator=Aggregator(geometry_);++output_dropped;
                    if(error.find("PAUSED_QUOTA")!=std::string::npos){state_=PipelineState::PAUSED_QUOTA;paused_day=observation_day(utc());}
                    else{state_=PipelineState::ERROR;++io_failures;auto delay=std::min(s.at("io_backoff_max_sec").get<double>(),s.at("io_backoff_initial_sec").get<double>()*std::pow(2.0,std::min(io_failures-1,30u)));retry_after=Clock::now()+std::chrono::duration_cast<Clock::duration>(std::chrono::duration<double>(delay));}
                
        };
        for(;;){
            if(stopping_&&drain_deadline==Clock::time_point::max())drain_deadline=Clock::now()+std::chrono::duration_cast<Clock::duration>(std::chrono::duration<double>(s.at("shutdown_drain_timeout_sec").get<double>()));
            if(stopping_&&Clock::now()>=drain_deadline){while(queue_.try_pop(incoming))dropped_++;break;}
            if(state_==PipelineState::PAUSED_DISK&&free_bytes(data)>=c.at("budget").at("resume_free_bytes").get<std::uint64_t>())state_=PipelineState::RUNNING;
            if(state_==PipelineState::PAUSED_QUOTA&&observation_day(utc())!=paused_day)state_=PipelineState::RUNNING;
            if(state_==PipelineState::ERROR&&io_failures<=s.at("io_retry_limit").get<unsigned>()&&Clock::now()>=retry_after)state_=PipelineState::RUNNING;
            bool got=queue_.try_pop(incoming);
            if(got){if(state_!=PipelineState::RUNNING){dropped_++;continue;}
                try{for(auto& out:aggregator.push(incoming))accept(std::move(out));}
                catch(const std::exception& e){failed(e);}
            }else if(stopping_)break;
            try{if(writer&&std::chrono::duration<double>(Clock::now()-last_flush).count()>=s.at("flush_interval_sec").get<double>()){if(!batch.empty()){writer->append(batch);batch.clear();}writer->flush();last_flush=Clock::now();}}catch(const std::exception& e){failed(e);}
            status();if(!got){std::unique_lock<std::mutex> l(wait_mutex_);wake_.wait_for(l,std::chrono::duration<double>(s.at("resume_poll_sec").get<double>()));}
        }
        if(state_==PipelineState::RUNNING){auto partial=aggregator.finish();if(partial)accept(std::move(*partial));close_file();}else quarantine();
        state_=PipelineState::STOPPING;status();
    }catch(const std::exception& e){error=e.what();state_=PipelineState::ERROR;writer.reset();release();try{status();}catch(...){}}
}
}
