#pragma once
#include <array>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <ctime>

// Bounded in-memory evidence. No formatting/I/O/allocation in mark().
// Dump only after the observation loop exits, including exception unwinding.
class RuntimeTrace {
    struct Entry { const char* label; double wall_ms, cpu_ms; uint32_t a,b,oa,ob; };
    std::array<Entry,128> entries_{};
    size_t count_=0;
    std::chrono::steady_clock::time_point start_=std::chrono::steady_clock::now();
    double cpu_start_=cpu_ms();
    static double cpu_ms() noexcept {
        timespec t{};
        if(clock_gettime(CLOCK_THREAD_CPUTIME_ID,&t)) return -1;
        return t.tv_sec*1000.0+t.tv_nsec/1e6;
    }
public:
    template<class Ring> void mark(const char* label,const Ring& a,const Ring& b) {
        if(count_==entries_.size()) return;
        entries_[count_++]={label,
            std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-start_).count(),
            cpu_ms()-cpu_start_,a.available_read(),b.available_read(),a.get_overflow_count(),b.get_overflow_count()};
    }
    ~RuntimeTrace() {
        std::fprintf(stderr,"[TRACE] relative wall_ms; parent-thread cpu_ms; occupancy samples; overflow callbacks\n");
        for(size_t i=0;i<count_;++i){const auto& e=entries_[i];
            std::fprintf(stderr,"[TRACE] %s wall_ms=%.3f cpu_ms=%.3f rb=%u/%u ov=%u/%u\n",
                e.label,e.wall_ms,e.cpu_ms,e.a,e.b,e.oa,e.ob);}
    }
};
