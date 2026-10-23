#include "storage/storage_pipeline.hpp"
#include <chrono>
#include <iostream>
#include <thread>
using namespace solar::storage;
int main(int argc,char** argv){try{if(argc!=2 && argc!=3)return 2;auto c=load_config(argv[1]);set_observation_timezone(c);
    auto estimate_json=estimate(c,2048,1024,1953,1e7);std::cout<<estimate_json.dump()<<'\n';
    nlohmann::json m={{"session_id","def"},{"baseline_revision","92d6889d46a864d403d0f519aebadf15ca4c614b"},{"calibration_id",0},{"units","ADC-count^2 per input FFT bin (mean after frequency grouping)"},{"cross_product_convention","X1*conj(X2) * exp(-j*phi)"},{"grouping_policy","whole source integrations"},{"frequency_bin_bandwidth_hz",1e7/512}};
    AggregationConfig g{2048,512,2048,1024,1953,1e7,35e6,1,PartialPolicy::Emit};std::vector<double>a(2048,4),b(2048,9),re(2048,1.8),im(2048,2.4);
    {const bool deferred=argc==3;Pipeline p(c,g,m,std::vector<float>(2048,0),deferred);
        if(deferred){p.start_observation(true,true,std::vector<float>(2048,0.25f));
            bool rejected=false;try{p.start_observation(true,true,std::vector<float>(2048,0.25f));}catch(const std::logic_error&){rejected=true;}
            if(!rejected)throw std::runtime_error("double activation accepted");}
        std::uint64_t now=std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::system_clock::now().time_since_epoch()).count();
        for(std::uint64_t k=0;k<60;++k){if(p.try_submit(k,now+k*199987,a,b,re,im)!=PipelineSubmit::ACCEPTED)throw std::runtime_error("fixture submit failed");}
        p.stop();}
    std::cout<<"Pipeline shutdown returned; inspect ledger/status/HDF5 assertions in Python.\n";return 0;
}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}}
