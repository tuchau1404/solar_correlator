#include "storage/hdf5_writer.hpp"
#include "storage/storage_budget.hpp"
#include <filesystem>
#include <fstream>
#include <iostream>
#include <random>
#include <stdexcept>
using namespace solar::storage;namespace fs=std::filesystem;
void require(bool b,const char* m){if(!b)throw std::runtime_error(m);}
int main(int argc,char** argv){try{
    require(argc==3,"usage: test_storage_io config output-dir");auto c=load_config(argv[1]);set_observation_timezone(c);auto dir=fs::path(argv[2]);fs::create_directories(dir);
    H5Eset_auto2(H5E_DEFAULT,nullptr,nullptr);
    std::vector<double> freq(512);for(std::size_t k=0;k<freq.size();++k)freq[k]=30e6+(k*4+1.5)*1e7/2048;
    std::vector<float> phi(2048,0);
    nlohmann::json m={{"data_schema_version",1},{"units","ADC-count^2 per input FFT bin (mean after frequency grouping)"},{"cross_product_convention","X1*conj(X2) * exp(-j*phi)"},{"effective_config_json",c.value.dump()},{"config_hash",c.hash},{"observation_day","2026-10-17"},{"calibration_id",0},{"artifact_id","abc_0"},{"grouping_policy","synthetic known values; FFT-weighted linear mean"},{"frequency_bin_bandwidth_hz",1e7/512},{"baseline_revision","92d6889d46a864d403d0f519aebadf15ca4c614b"}};
    OutputRecord r;for(auto& v:r.values)v.resize(512);for(std::size_t k=0;k<512;++k){r.values[0][k]=4;r.values[1][k]=9;r.values[2][k]=1.8;r.values[3][k]=2.4;}r.valid_fft_frames=1953*6;r.span_sec=1.2000256;r.covered_sec=r.span_sec;r.cadence_sec=1.1999232;r.quality=UTC_ESTIMATED;
    auto start=std::uint64_t(1792198800000000ULL);std::vector<OutputRecord> rows;
    for(unsigned i=0;i<750;++i){r.frame_first=i*6;r.frame_last=i*6+5;r.start_utc_us=start+std::uint64_t(i*1.1999232e6);r.end_utc_us=r.start_utc_us+1200026;r.timestamp_us=r.start_utc_us+600013;rows.push_back(r);}
    auto path=dir/"golden.h5";{Hdf5Writer writer(path.string(),8500000,m,freq,phi,16,"none",0);writer.append(rows);writer.close();require(writer.rows()==750,"row count");}
    require(fs::file_size(path)<=8500000,"file cap respected");
    bool failed=false;try{Hdf5Writer tiny((dir/"too_small.h5.tmp").string(),1024,m,freq,phi,16,"none",0);tiny.append(rows);tiny.close();}catch(const std::exception&){failed=true;}
    require(failed,"insufficient HDF5 cap must fail");require(fs::file_size(dir/"too_small.h5.tmp")<=1024,"no pre-quota overflow");
    Budget b(c);auto grant=b.reserve("abc_0",(fs::path(c.value.at("storage").at("data_dir").get<std::string>())/"golden.h5").string(),"2026-10-17",start,"abc");b.ready(grant,fs::file_size(path));
    bool quota=false;unsigned accepted=1;for(unsigned i=1;i<200;++i){try{b.reserve("abc_"+std::to_string(i),(dir/("quota_"+std::to_string(i)+".h5")).string(),"2026-10-17",start,"abc");++accepted;}catch(const std::exception& e){if(std::string(e.what()).find("PAUSED_QUOTA")!=std::string::npos){quota=true;break;}throw;}}
    require(quota,"quota must stop");std::cout<<"PASS bounded HDF5, golden 750 rows, C++/Python ledger, conservative cap stop; accepted_files="<<accepted<<" golden_bytes="<<fs::file_size(path)<<"\n";return 0;
}catch(const std::exception& e){std::cerr<<"FAIL "<<e.what()<<"\n";return 1;}}
