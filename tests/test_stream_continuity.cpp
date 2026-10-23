// Exercises the actual main callback with local, anonymous SHM; no SDR/GPIO calls.
#define main solar_main_not_run
#include "../src/main.cpp"
#undef main
int main() {
    for(int scenario=0;scenario<5;++scenario){
        const auto name="/solar_callback_audit_"+std::to_string(getpid())+"_"+std::to_string(scenario);
        SharedMemoryRingBuffer rb(name,8,true);SharedMemoryRingBuffer::unlink_shm(name);
        rb.set_ready(true);rb.set_start_stream(true);g_child_running=true;
        StreamContext context{&rb};short x[9]{},q[9]{};
        sdrplay_api_StreamCbParamsT params{};
        params.firstSampleNum=scenario==0?UINT32_MAX-1:100;
        sdr_stream_callback(x,q,&params,2,0,&context);
        params.firstSampleNum=scenario==0?0:102;
        if(scenario==1)++params.firstSampleNum;
        sdr_stream_callback(x,q,scenario==3?nullptr:&params,scenario==4?9:2,scenario==2?1:0,&context);
        const bool healthy=scenario==0;
        if(rb.is_ready()!=healthy || g_child_running.load()!=healthy || rb.available_read()!=(healthy?4u:2u))return 1;
        if(rb.get_overflow_count()!=(scenario==4?1u:0u))return 2;
    }
    std::cout<<"PASS callback sample-number wrap, gap, reset, null params, full-ring fail-closed\n";
}
