#include "correlator.hpp"
#include "dsp_config.hpp"
#include <algorithm>
#include <array>
#include <cmath>
#include <complex>
#include <exception>
#include <iostream>
#include <numeric>
#include <random>
#include <stdexcept>
#include <thread>
#include <unistd.h>

namespace {
constexpr long double pi=3.141592653589793238462643383279502884L;
void require(bool ok,const char* message) { if(!ok) throw std::runtime_error(message); }
void near(double got,double want,double tol,const char* message) {
    if(!std::isfinite(got) || std::abs(got-want)>tol) {
        std::cerr<<message<<": got "<<got<<", expected "<<want<<", tol "<<tol<<'\n';
        throw std::runtime_error(message);
    }
}
struct Rings {
    std::string name1,name2;
    SharedMemoryRingBuffer a,b;
    static std::string name() {
        static unsigned id=0;
        return "/fx_audit_"+std::to_string(getpid())+"_"+std::to_string(id++);
    }
    // Anonymous immediately after creation: crash/abort cannot leave named SHM.
    // Constructor helper unlinks each successful object before constructing next.
    Rings():name1(name()),name2(name()),a(name1,8192,true),b(create_second(),8192,true) {
        SharedMemoryRingBuffer::unlink_shm(name2);
    }
    std::string create_second() {
        SharedMemoryRingBuffer::unlink_shm(name1);
        return name2;
    }
};
// Both channels advance together, retry without changing sequence number.
// The stop flag carries no payload; relaxed atomic is sufficient. join publishes errors.
struct Feeder {
    std::atomic<bool> running{true};
    std::exception_ptr error;
    std::thread worker;
    Feeder(Rings& r,const std::vector<ComplexSample>& x,const std::vector<ComplexSample>& y)
      :worker([&,this]{try {
        const auto end=std::chrono::steady_clock::now()+std::chrono::seconds(20);
        size_t pos=0;
        std::array<int16_t,257> xi{},xq{},yi{},yq{};
        while(pos<x.size() && running.load(std::memory_order_relaxed)) {
            if(std::chrono::steady_clock::now()>end) throw std::runtime_error("feeder timeout");
            const uint32_t count=uint32_t(std::min<size_t>(257,x.size()-pos));
            if(r.a.available_write()<count || r.b.available_write()<count) {
                std::this_thread::sleep_for(std::chrono::microseconds(20)); continue;
            }
            for(uint32_t j=0;j<count;++j) {
                xi[j]=x[pos+j].i;xq[j]=x[pos+j].q;
                yi[j]=y[pos+j].i;yq[j]=y[pos+j].q;
            }
            require(r.a.write(xi.data(),xq.data(),count)==count,"channel 1 write");
            require(r.b.write(yi.data(),yq.data(),count)==count,"channel 2 write");
            pos+=count;
        }
      } catch(...) {error=std::current_exception();}}) {}
    void finish() { if(worker.joinable()) worker.join(); if(error) std::rethrow_exception(error); }
    ~Feeder(){running.store(false,std::memory_order_relaxed);if(worker.joinable())worker.join();}
};
struct Result {std::vector<float> db,phase,coh;explicit Result(size_t n):db(n),phase(n),coh(n){} };
void run(FXCorrelator& fx,Rings& r,const std::vector<float>& phi,Result& o) {
    fx.process_integration_cycle(r.a,r.b,phi,o.db,o.phase,o.coh);
    for(size_t k=0;k<o.db.size();++k) {
        require(std::isfinite(o.db[k])&&std::isfinite(o.phase[k])&&std::isfinite(o.coh[k]),"finite outputs");
        require(o.coh[k]>=0&&o.coh[k]<=1,"coherence bounds");
    }
}
struct Reference {double p1,p2,db,phase,coh;};
// Independent long-double direct DFT, ideal symmetric Hann, raw integer samples.
Reference reference(const std::vector<ComplexSample>& x,const std::vector<ComplexSample>& y,
 uint32_t n,uint32_t frames,uint32_t k,size_t start,double phi) {
    std::complex<long double> cross=0;
    long double a=0,b=0,s2=0;
    std::vector<std::complex<long double>> basis(n);
    for(uint32_t j=0;j<n;++j) {
        const long double w=.5L*(1-std::cos(2*pi*j/(n-1)));
        s2+=w*w;basis[j]=w*std::polar(1.L,-2*pi*k*j/n);
    }
    for(uint32_t f=0;f<frames;++f) {
        std::complex<long double> u=0,v=0;
        for(uint32_t j=0;j<n;++j) {
            size_t ix=start+size_t(f)*(n/2)+j;
            u+=std::complex<long double>(x[ix].i,x[ix].q)*basis[j];
            v+=std::complex<long double>(y[ix].i,y[ix].q)*basis[j];
        }
        cross+=u*std::conj(v);a+=std::norm(u);b+=std::norm(v);
    }
    cross*=std::polar(1.L,-(long double)phi);
    const long double scale=1/(frames*(long double)n*s2);
    const auto mag=std::abs(cross)*scale;
    return {double(a*scale),double(b*scale),double(10*std::log10(std::max(mag,1e-12L))),
        mag>0?double(std::arg(cross)):0,a>0&&b>0?double(std::abs(cross)/std::sqrt(a*b)):0};
}
void tone(uint32_t frames,double amplitude,double theta,bool correct,bool negative=false) {
    constexpr uint32_t n=2048;
    const size_t count=(size_t(frames)+1)*(n/2);
    std::vector<ComplexSample> x(count),y(count);
    std::array<ComplexSample,10> lut{};
    for(unsigned j=0;j<10;++j) {
        const double angle=double(2*pi)*j/10*(negative?-1:1);
        lut[j]={int16_t(std::lround(amplitude*std::cos(angle))),int16_t(std::lround(amplitude*std::sin(angle)))};
    }
    // theta=pi/2 uses exact integer rotation; no oscillator discontinuities.
    for(size_t j=0;j<count;++j) {x[j]=lut[j%10];y[j]=theta==0?x[j]:(theta>0?ComplexSample{int16_t(-x[j].q),x[j].i}:ComplexSample{x[j].q,int16_t(-x[j].i)});}
    Rings r;FXCorrelator fx({n,frames,std::chrono::seconds(20),nullptr});
    Feeder feeder(r,x,y);fx.prime_buffers(r.a,r.b);
    std::vector<float> phi(n,correct?float(-theta):0.f); Result o(n);run(fx,r,phi,o);feeder.finish();
    const uint32_t k=negative?n-205:205, shifted=(k+n/2)%n;
    require(size_t(std::max_element(o.db.begin(),o.db.end())-o.db.begin())==shifted,"peak bin");
    const auto ref=reference(x,y,n,frames,k,0,phi[k]);
    near(o.db[shifted],ref.db,2e-4,"peak dB");near(o.phase[shifted],ref.phase,2e-6,"phase sign");
    near(o.coh[shifted],1,2e-6,"coherence one");
    require(r.a.available_read()==0 && r.b.available_read()==0,"exact sample count");
    std::cout<<"PASS tone M="<<frames<<" A="<<amplitude<<" theta="<<theta
             <<" corrected="<<correct<<" peak="<<shifted<<" dB="<<o.db[shifted]<<'\n';
}
void random_reference() {
    constexpr uint32_t n=64,m=23,cycles=2;
    std::vector<ComplexSample> x((cycles*m+1)*n/2),y(x.size());
    std::mt19937 rng(12345);std::uniform_int_distribution<int> dist(-30000,30000);
    for(size_t j=0;j<x.size();++j) {x[j]={int16_t(dist(rng)),int16_t(dist(rng))};y[j]={int16_t(dist(rng)),int16_t(dist(rng))};}
    Rings r;FXCorrelator fx({n,m,std::chrono::seconds(5),nullptr});Feeder feeder(r,x,y);fx.prime_buffers(r.a,r.b);
    std::vector<float> phi(n);for(unsigned k=0;k<n;++k)phi[k]=float(.03*k-.7);
    Result o(n);
    for(unsigned cycle=0;cycle<cycles;++cycle) {
        run(fx,r,phi,o);long double weighted=0,s2=0;
        for(unsigned j=0;j<n;++j) {
            long double w=.5L*(1-std::cos(2*pi*j/(n-1)));s2+=w*w;
            for(unsigned f=0;f<m;++f) {auto v=x[(cycle*m+f)*n/2+j];weighted+=(static_cast<long double>(v.i)*v.i+static_cast<long double>(v.q)*v.q)*w*w;}
        }
        near(std::accumulate(fx.auto_power1().begin(),fx.auto_power1().end(),0.),double(weighted/(m*s2)),double(weighted/(m*s2))*5e-7,"Parseval");
        for(unsigned k=0;k<n;++k) {
            auto ref=reference(x,y,n,m,k,cycle*m*n/2,phi[k]);unsigned s=(k+n/2)%n;
            near(fx.auto_power1()[s],ref.p1,ref.p1*1e-6,"auto power oracle");
            near(o.db[s],ref.db,3e-4,"random cross dB");near(o.phase[s],ref.phase,2e-5,"random phase");
            near(o.coh[s],ref.coh,2e-6,"random coherence");
        }
    }
    feeder.finish();require(r.a.available_read()==0&&r.b.available_read()==0,"cycle continuity sample count");
    std::cout<<"PASS random long-double DFT, Parseval, two-cycle continuity\n";
}
template<class F>void throws(F f,const char* msg){bool caught=false;try{f();}catch(const std::exception&){caught=true;}require(caught,msg);}
void edges() {
    constexpr unsigned n=64,m=5;std::vector<ComplexSample> zero((m+1)*n/2),full=zero;
    for(auto& v:full)v={-32768,-32768};
    for(int mode=0;mode<3;++mode) {
        Rings r;FXCorrelator fx({n,m,std::chrono::seconds(2),nullptr});
        const auto& x=mode==2?full:zero;const auto& y=mode?full:zero;
        Feeder feeder(r,x,y);fx.prime_buffers(r.a,r.b);Result o(n);std::vector<float> phi(n);
        run(fx,r,phi,o);feeder.finish();
        if(mode<2) for(unsigned k=0;k<n;++k){near(o.db[k],-120,0,"zero dB");near(o.coh[k],0,0,"zero coherence");near(o.phase[k],0,0,"zero phase");}
        else {require(size_t(std::max_element(o.db.begin(),o.db.end())-o.db.begin())==n/2,"DC bin");near(o.coh[n/2],1,2e-6,"int16 minimum coherence");near(std::accumulate(fx.auto_power1().begin(),fx.auto_power1().end(),0.),2147483648.,1000,"full-scale power");}
    }
    Rings r;std::atomic<bool> running{true};FXCorrelator fx({n,m,std::chrono::milliseconds(30),&running});Result o(n);std::vector<float> phi(n);
    throws([&]{run(fx,r,phi,o);},"unprimed rejection");
    throws([&]{fx.prime_buffers(r.a,r.b);},"prime starvation deadline");
    std::array<int16_t,n> a{};r.a.write(a.data(),a.data(),n);r.b.write(a.data(),a.data(),n);fx.prime_buffers(r.a,r.b);
    throws([&]{fx.process_integration_cycle(r.a,r.b,phi,o.db,o.db,o.coh);},"alias rejection");
    auto bad=phi;bad[0]=std::numeric_limits<float>::quiet_NaN();throws([&]{run(fx,r,bad,o);},"NaN phase rejected");
    Result wrong(n-1);throws([&]{run(fx,r,phi,wrong);},"size rejection");
    throws([&]{run(fx,r,phi,o);},"mid-cycle starvation deadline");
    running.store(false);throws([&]{fx.prime_buffers(r.a,r.b);},"cancel prime");running.store(true);
    r.a.write(a.data(),a.data(),n);r.b.write(a.data(),a.data(),n);fx.prime_buffers(r.a,r.b);
    std::vector<int16_t> huge(8192);require(r.a.write(huge.data(),huge.data(),8192)==0,"deliberate overflow");
    throws([&]{run(fx,r,phi,o);},"drop rejection");
    std::cout<<"PASS zeros, one silent channel, full int16, invalid inputs, timeouts, cancellation, overflow\n";
}
}
int main(){try {
    tone(SolarConfig::Correlator::INTEGRATION_FRAMES,10000,0,true);
    tone(31,32767,double(pi/2),false);tone(31,32767,double(pi/2),true);
    tone(31,10000,-double(pi/2),false);tone(31,10000,-double(pi/2),true);
    tone(31,10000,0,true,true);random_reference();edges();
    std::cout<<"ALL DSP TESTS PASSED (host architecture only; no ARM timing certification)\n";return 0;
}catch(const std::exception& e){std::cerr<<"FAIL: "<<e.what()<<'\n';return 1;}}
