#include "correlator.hpp"
#include "dsp_config.hpp"
#include <algorithm>
#include <cmath>
#include <limits>
#include <stdexcept>
#include <thread>
#if defined(__aarch64__) && !defined(SOLAR_DISABLE_NEON)
#include <arm_neon.h>
#endif
#if defined(__FAST_MATH__) || (defined(__FINITE_MATH_ONLY__) && __FINITE_MATH_ONLY__ > 0)
#error "FXCorrelator requires finite/NaN-aware arithmetic; disable fast-math"
#endif
namespace {
constexpr double pi = 3.141592653589793238462643383279502884;
constexpr float input_scale = 1.0f / 32768.0f;
constexpr double adc_scale2 = 32768.0 * 32768.0;
// Loop iterations are independent; compiler can vectorize conversion/windowing.
void window_half(const ComplexSample* src, const float* w,
                 fftwf_complex* dst, uint32_t length) {
    for (uint32_t j = 0; j < length; ++j) {
        const float weight = w[j] * input_scale;
        dst[j][0] = float(src[j].i) * weight;
        dst[j][1] = float(src[j].q) * weight;
    }
}
void accumulate(const fftwf_complex* x, const fftwf_complex* y,
                double* re, double* im, double* a, double* b, uint32_t n) {
    uint32_t k = 0;
#if defined(__aarch64__) && !defined(SOLAR_DISABLE_NEON)
    for (; k + 1 < n; k += 2) {
        // Two complex bins, deinterleave FP32 then widen BEFORE multiplying.
        const float32x2x2_t xx = vld2_f32(&x[k][0]);
        const float32x2x2_t yy = vld2_f32(&y[k][0]);
        const float64x2_t r = vcvt_f64_f32(xx.val[0]), i = vcvt_f64_f32(xx.val[1]);
        const float64x2_t s = vcvt_f64_f32(yy.val[0]), t = vcvt_f64_f32(yy.val[1]);
        vst1q_f64(re+k, vaddq_f64(vld1q_f64(re+k), vaddq_f64(vmulq_f64(r,s), vmulq_f64(i,t))));
        vst1q_f64(im+k, vaddq_f64(vld1q_f64(im+k), vsubq_f64(vmulq_f64(i,s), vmulq_f64(r,t))));
        vst1q_f64(a+k, vaddq_f64(vld1q_f64(a+k), vaddq_f64(vmulq_f64(r,r), vmulq_f64(i,i))));
        vst1q_f64(b+k, vaddq_f64(vld1q_f64(b+k), vaddq_f64(vmulq_f64(s,s), vmulq_f64(t,t))));
    }
#endif
    for (; k < n; ++k) {
        const double r=x[k][0], i=x[k][1], s=y[k][0], t=y[k][1];
        re[k] += r*s + i*t;
        im[k] += i*s - r*t;
        a[k] += r*r + i*i;
        b[k] += s*s + t*t;
    }
}
}
FXCorrelator::FXCorrelator() : FXCorrelator(Options{
    SolarConfig::Correlator::FFT_SIZE, SolarConfig::Correlator::INTEGRATION_FRAMES,
    std::chrono::milliseconds(5000), nullptr}) {}
FXCorrelator::FXCorrelator(Options o) : options_(o), n_(o.fft_size), hop_(n_/2) {
    if (n_ < 4 || (n_ & (n_-1)) || n_ > uint32_t(std::numeric_limits<int>::max()) ||
        o.frames == 0 || o.timeout.count() <= 0)
        throw std::invalid_argument("invalid FFT size, frame count or deadline");
    samples1_.resize(n_); samples2_.resize(n_); window_.resize(n_);
    re_.resize(n_); im_.resize(n_); a1_.resize(n_); a2_.resize(n_);
    power1_.resize(n_); power2_.resize(n_);
    for (uint32_t j=0; j<n_; ++j) {
        window_[j] = float(0.5*(1.0-std::cos(2*pi*j/(n_-1))));
        s2_ += double(window_[j])*window_[j]; // normalize actual stored window
    }
    try {
        in1_=fftwf_alloc_complex(n_); in2_=fftwf_alloc_complex(n_);
        out1_=fftwf_alloc_complex(n_); out2_=fftwf_alloc_complex(n_);
        if (!in1_ || !in2_ || !out1_ || !out2_) throw std::bad_alloc();
        plan1_=fftwf_plan_dft_1d(int(n_),in1_,out1_,FFTW_FORWARD,FFTW_MEASURE);
        plan2_=fftwf_plan_dft_1d(int(n_),in2_,out2_,FFTW_FORWARD,FFTW_MEASURE);
        if (!plan1_ || !plan2_) throw std::runtime_error("FFTW planning failed");
    } catch (...) { cleanup(); throw; }
}
void FXCorrelator::cleanup() noexcept {
    if(plan1_) fftwf_destroy_plan(plan1_);
    if(plan2_) fftwf_destroy_plan(plan2_);
    fftwf_free(in1_); fftwf_free(in2_); fftwf_free(out1_); fftwf_free(out2_);
}
FXCorrelator::~FXCorrelator() { cleanup(); }
void FXCorrelator::check(SharedMemoryRingBuffer& r1, SharedMemoryRingBuffer& r2,
                         Clock::time_point deadline) {
    if ((options_.running && !options_.running->load(std::memory_order_relaxed)) ||
        Clock::now() >= deadline || r1.get_overflow_count()!=drops1_ ||
        r2.get_overflow_count()!=drops2_) {
        primed_=false;
        throw std::runtime_error("DSP cancelled, deadline expired, or sample drop; resync/re-prime required");
    }
}
void FXCorrelator::read_pair(SharedMemoryRingBuffer& r1, SharedMemoryRingBuffer& r2,
                            uint32_t offset, Clock::time_point deadline) {
    for (;;) {
        check(r1,r2,deadline);
        if(r1.available_read()>=hop_ && r2.available_read()>=hop_) break;
        std::this_thread::sleep_for(std::chrono::microseconds(20));
    }
    if(r1.read(samples1_.data()+offset,hop_)!=hop_ ||
       r2.read(samples2_.data()+offset,hop_)!=hop_)
        throw std::runtime_error("SPSC consumer ownership violated");
}
void FXCorrelator::prime_buffers(SharedMemoryRingBuffer& r1, SharedMemoryRingBuffer& r2) {
    primed_=false;
    drops1_=r1.get_overflow_count(); drops2_=r2.get_overflow_count();
    old_offset_=0;
    read_pair(r1,r2,0,Clock::now()+options_.timeout);
    primed_=true;
}
void FXCorrelator::process_integration_cycle(SharedMemoryRingBuffer& r1,
    SharedMemoryRingBuffer& r2, const std::vector<float>& phi,
    std::vector<float>& db, std::vector<float>& phase, std::vector<float>& coh) {
    if (!primed_) throw std::logic_error("prime buffers before processing");
    if(phi.size()!=n_ || db.size()!=n_ || phase.size()!=n_ || coh.size()!=n_ ||
       &db==&phase || &db==&coh || &phase==&coh || &phi==&db || &phi==&phase || &phi==&coh)
        throw std::invalid_argument("FFT vectors must have N elements and be distinct");
    for(float v:phi) if(!std::isfinite(v)) throw std::invalid_argument("nonfinite fringe phase");
    std::fill(re_.begin(),re_.end(),0); std::fill(im_.begin(),im_.end(),0);
    std::fill(a1_.begin(),a1_.end(),0); std::fill(a2_.begin(),a2_.end(),0);
    const auto deadline=Clock::now()+options_.timeout;
    try {
        for(uint32_t frame=0;frame<options_.frames;++frame) {
            const uint32_t fresh=hop_-old_offset_;
            read_pair(r1,r2,fresh,deadline);
            window_half(samples1_.data()+old_offset_,window_.data(),in1_,hop_);
            window_half(samples2_.data()+old_offset_,window_.data(),in2_,hop_);
            window_half(samples1_.data()+fresh,window_.data()+hop_,in1_+hop_,hop_);
            window_half(samples2_.data()+fresh,window_.data()+hop_,in2_+hop_,hop_);
            old_offset_=fresh;
            fftwf_execute(plan1_); fftwf_execute(plan2_);
            accumulate(out1_,out2_,re_.data(),im_.data(),a1_.data(),a2_.data(),n_);
        }
        check(r1,r2,deadline);
        const double norm=adc_scale2/(double(options_.frames)*n_*s2_);
        // Constant phase rotation commutes with summation. No trig in hot loop.
        for(uint32_t k=0;k<n_;++k) {
            const uint32_t j=(k+n_/2)%n_;
            const double c=std::cos(double(phi[k])), s=std::sin(double(phi[k]));
            const double real=(re_[k]*c+im_[k]*s)*norm;
            const double imag=(im_[k]*c-re_[k]*s)*norm;
            const double mag=std::hypot(real,imag);
            const double p1=a1_[k]*norm, p2=a2_[k]*norm;
            const double denom=std::sqrt(p1)*std::sqrt(p2);
            if(!std::isfinite(mag) || !std::isfinite(denom) || p1<0 || p2<0)
                throw std::runtime_error("nonfinite/invalid DSP output");
            const double gamma=denom>0 ? mag/denom : 0;
            if(!std::isfinite(gamma) || gamma>1.0+1e-9)
                throw std::runtime_error("Cauchy-Schwarz violation");
            power1_[j]=p1; power2_[j]=p2;
            db[j]=float(10*std::log10(std::max(mag,1e-12)));
            phase[j]=mag>0 ? float(std::atan2(imag,real)) : 0;
            coh[j]=float(std::clamp(gamma,0.0,1.0));
        }
        check(r1,r2,deadline);
    } catch (...) { primed_=false; throw; }
}
