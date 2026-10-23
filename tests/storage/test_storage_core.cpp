#include "storage/storage_aggregator.hpp"
#include "storage/storage_queue.hpp"
#include <cmath>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <thread>
using namespace solar::storage;
namespace {
void require(bool value,const char* message) {if(!value)throw std::runtime_error(message);}
void near(double x,double y,double tol,const char* message) {require(std::isfinite(x)&&std::abs(x-y)<=tol,message);}
template<class F>void rejects(F f){bool threw=false;try{f();}catch(const std::exception&){threw=true;}require(threw,"must reject invalid input");}
AggregationConfig config(std::size_t bins=8,std::size_t out=2) {return {bins,out,bins,bins/2,2,100,35000000,.16,PartialPolicy::Emit};}
SourceRecord record(std::uint64_t id,double theta=0) {
    SourceRecord r(8);r.frame_id=id;r.valid_fft_frames=2;r.sample_start=id*8;r.sample_end=r.sample_start+12;
    r.estimated_start_utc_us=1000000+id*80000;r.estimated_end_utc_us=r.estimated_start_utc_us+120000;
    r.backend_timestamp_us=r.estimated_end_utc_us+10000;
    for(std::size_t k=0;k<8;++k) {r.values[S11][k]=4;r.values[S22][k]=9;
        r.values[S12_REAL][k]=3*std::cos(theta);r.values[S12_IMAG][k]=3*std::sin(theta);}
    return r;
}
void science() {
    Aggregator a(config());
    require(a.push(record(0,3.13)).empty(),"first record waits");
    auto out=a.push(record(1,-3.13));require(out.size()==1,"window emitted");
    near(out[0].values[S12_REAL][0],3*std::cos(3.13),1e-6,"complex mean wrap real");
    near(out[0].values[S12_IMAG][0],0,1e-7,"complex mean wrap imaginary");
    near(out[0].covered_sec,.20,1e-12,"Welch union no double counting");
    near(out[0].cadence_sec,.16,1e-12,"cadence distinct from support");
    require(out[0].timestamp_us==1100000,"midpoint");
    auto d=derive_bin(4,9,0,3);near(d.phase_rad, std::acos(-1)/2,1e-12,"phase sign");
    near(d.coherence_magnitude,.5,1e-12,"gamma");near(d.coherence_squared,.25,1e-12,"gamma squared");
    require(std::isnan(derive_bin(1,1,0,0).phase_rad),"zero cross phase invalid");
    require(std::isnan(derive_bin(0,0,0,0).coherence_magnitude),"zero power coherence invalid");
    require(derive_bin(1,1,2,0).coherence_violation,"large violation not clipped");
    require(!derive_bin(1,1,1+1e-7,0).coherence_violation,"float rounding allowance");
    Aggregator cancel(config());cancel.push(record(0,0));auto c=cancel.push(record(1,std::acos(-1)));
    require(std::hypot(c[0].values[2][0],c[0].values[3][0])<1e-6,"rotating visibility cancels");
    Aggregator bad(config());auto r=record(0);r.values[1][1]=std::numeric_limits<double>::quiet_NaN();bad.push(r);
    auto b=bad.push(record(1));for(const auto& v:b[0].values)require(std::isnan(v[0])&&std::isfinite(v[1]),"common strict invalid group");
    require(b[0].quality&INVALID_BIN,"invalid flagged");
    std::cout<<"PASS phase wrap, coherence, zero, complex cancellation, NaN masks, Welch support\n";
}
void boundaries() {
    Aggregator a(config());a.push(record(0));auto x=a.push(record(2));
    require(x.size()==1&&(x[0].quality&DISCONTINUITY)&&(x[0].quality&PARTIAL_WINDOW),"gap splits window");
    auto last=a.finish();require(last&&last->frame_first==2,"shutdown partial");require(!a.finish(),"finish idempotent");
    Aggregator cal(config());cal.push(record(0));auto r=record(1);r.calibration_id=1;auto y=cal.push(r);
    require(y.size()==1&&(y[0].quality&CALIBRATION_BOUNDARY),"calibration split");
    Aggregator clock(config());clock.push(record(0));r=record(1);r.estimated_start_utc_us=1;r.estimated_end_utc_us=120001;
    auto z=clock.push(r);require(z.size()==1&&(z[0].quality&DISCONTINUITY),"backward UTC split");
    auto cfg=config();cfg.partial_policy=PartialPolicy::Discard;Aggregator discard(cfg);discard.push(record(0));
    require(!discard.finish()&&discard.discarded_partial_records()==1,"discard accounting");
    // Unequal source weights, contiguous supports: one FFT then two FFTs.
    Aggregator weighted(config());r=record(0);r.valid_fft_frames=1;r.sample_end=8;r.estimated_end_utc_us=1080000;
    for(auto& v:r.values) { for(auto& p:v) p=1; }
    weighted.push(r);
    r=record(1);r.sample_start=4;r.sample_end=16;r.estimated_start_utc_us=1040000;r.estimated_end_utc_us=1160000;
    for(auto& v:r.values) { for(auto& p:v) p=4; }
    weighted.push(r);auto w=weighted.finish();
    near(w->values[0][0],3,1e-7,"FFT-weighted average");near(w->covered_sec,.16,1e-12,"partial-source support");
    rejects([&]{weighted.push(r);});
    std::cout<<"PASS gaps, calibration, backward UTC, shutdown policies, unequal weights\n";
}
void mapping() {
    for(std::size_t bins:{2048,1024,512,256}) {
        AggregationConfig c{2048,bins,2048,1024,1953,1e7,35e6,1,PartialPolicy::Emit};Aggregator a(c);
        require(a.source_records_per_window()==6,"whole integrations for 1 second");
        near(a.effective_cadence_sec(),1.1999232,1e-12,"actual cadence");
        auto f=a.frequency_hz();double width=1e7/bins;
        near(f.front(),30e6+(2048.0/bins-1)*.5*1e7/2048,1e-6,"first RF bin center");
        for(std::size_t i=1;i<f.size();++i) near(f[i]-f[i-1],width,1e-6,"RF increasing without wrap");
    }
    auto c=config();c.output_bins=3;rejects([&]{Aggregator bad(c);});c=config();c.requested_interval_sec=.01;rejects([&]{Aggregator bad(c);});
    Aggregator a(config());auto r=record(0);r.sample_end=13;rejects([&]{a.push(r);});
    r=record(0);r.values[0].resize(1);rejects([&]{a.push(r);});
    std::cout<<"PASS RF mapping, whole-integration cadence, input validation\n";
}
void queue() {
    SourceQueue q(2,8);auto r=record(0);require(q.try_push(r)==SubmitResult::ACCEPTED,"queue accepted");
    r.values[0][0]=99;SourceRecord read(8);require(q.try_pop(read)&&read.values[0][0]==4,"queue owns a copy");
    require(q.try_push(r)==SubmitResult::ACCEPTED&&q.try_push(r)==SubmitResult::ACCEPTED,"capacity");
    require(q.try_push(r)==SubmitResult::QUEUE_FULL,"queue overflow drops newest");
    require(q.high_water()==2,"high water");
    require(q.try_pop(read)&&q.try_pop(read)&&!q.try_pop(read),"drain");
    SourceQueue threaded(7,8);std::atomic<bool> failed{false};
    std::thread consumer([&]{SourceRecord v(8);for(std::uint64_t i=0;i<100000;++i){while(!threaded.try_pop(v))std::this_thread::yield();if(v.frame_id!=i||v.values[0][0]!=double(i))failed=true;}});
    for(std::uint64_t i=0;i<100000;++i){r.frame_id=i;r.values[0][0]=double(i);while(threaded.try_push(r)==SubmitResult::QUEUE_FULL)std::this_thread::yield();}
    consumer.join();require(!failed,"SPSC ordering and ownership");
    std::cout<<"PASS preallocated queue copy/full/drain, 100000-record threaded transfer\n";
}
}
int main(){try{science();boundaries();mapping();queue();std::cout<<"ALL STORAGE CORE TESTS PASSED\n";return 0;}catch(const std::exception& e){std::cerr<<"FAIL "<<e.what()<<'\n';return 1;}}
