#include "storage/hdf5_writer.hpp"
#include "storage/storage_budget.hpp"
#include <cmath>
#include <filesystem>
#include <iostream>
#include <random>
using namespace solar::storage;namespace fs=std::filesystem;
int main(int argc,char** argv){try{if(argc!=2)return 2;auto c=load_config(argv[1]);set_observation_timezone(c);Budget budget(c);
    auto root=fs::path(c.value.at("storage").at("data_dir").get<std::string>());std::vector<double> freq(512);for(std::size_t k=0;k<512;++k)freq[k]=30e6+(k*4+1.5)*1e7/2048;std::vector<float> phi(2048,0);
    const std::uint64_t start=1792170000000000ULL;const double interval=1.1999232;const std::uint64_t total=std::ceil(86400/interval);std::uint64_t bytes=0,files=0;
    std::mt19937 rng(123);std::uniform_real_distribution<double> noise(0,1);
    for(std::uint64_t first=0;first<total;first+=750){auto aid="aaa_"+std::to_string(files);auto path=root/(aid+".h5");auto stamp=start+std::uint64_t(first*interval*1e6);auto grant=budget.reserve(aid,path.string(),"2026-10-17",stamp,"aaa");
        nlohmann::json m={{"data_schema_version",1},{"artifact_id",aid},{"session_id","aaa"},{"baseline_revision","92d6889d46a864d403d0f519aebadf15ca4c614b"},{"patch_state","synthetic experiment"},{"units","ADC-count^2 per input FFT bin (mean after frequency grouping)"},{"cross_product_convention","X1*conj(X2) * exp(-j*phi)"},{"effective_config_json",c.value.dump()},{"config_hash",c.hash},{"observation_day","2026-10-17"},{"calibration_id",0},{"grouping_policy","synthetic already-grouped fixture; six source integrations per output"},{"frequency_bin_bandwidth_hz",1e7/512},{"quicklook_enabled",true},{"input_fft_bins",2048},{"output_bins",512},{"source_integration_frames",1953},{"requested_save_interval_sec",1.0},{"effective_save_interval_sec",interval},{"timestamp_semantics","synthetic known UTC"}};
        Hdf5Writer w(path.string()+".tmp",grant.cap,m,freq,phi,16,"none",0);std::vector<OutputRecord> batch;
        for(auto i=first;i<std::min(first+750,total);++i){OutputRecord r;for(auto& v:r.values)v.resize(512);for(std::size_t f=0;f<512;++f){double a=4+noise(rng),b=9+noise(rng),mag=.5*std::sqrt(a*b),theta=.02*i+.01*f;r.values[0][f]=a;r.values[1][f]=b;r.values[2][f]=mag*std::cos(theta);r.values[3][f]=mag*std::sin(theta);}
            r.frame_first=i*6;r.frame_last=i*6+5;r.valid_fft_frames=11718;r.sample_start=i*11718*1024;r.sample_end=r.sample_start+11718*1024+1024;r.start_utc_us=start+std::uint64_t(i*interval*1e6);r.end_utc_us=r.start_utc_us+1200026;r.timestamp_us=r.start_utc_us+600013;r.span_sec=1.2000256;r.covered_sec=r.span_sec;r.cadence_sec=interval;batch.push_back(std::move(r));if(batch.size()==16){w.append(batch);batch.clear();}}
        if(!batch.empty())w.append(batch);w.close();fs::rename(path.string()+".tmp",path);auto size=fs::file_size(path);budget.ready(grant,size);bytes+=size;++files;
    }
    std::cout<<nlohmann::json{{"rows",total},{"files",files},{"hdf5_logical_bytes",bytes},{"quota",budget.snapshot("2026-10-17")},{"scope","24h equivalent synthetic grouped outputs, native writer and ledger; no SDR timing"}}.dump(2)<<'\n';return 0;
}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}}
