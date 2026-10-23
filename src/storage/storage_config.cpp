#include "storage_config.hpp"
#include <cerrno>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstdlib>
#include <ctime>
#include <cstdio>
#include <thread>
#include <fcntl.h>
#include <poll.h>
#include <spawn.h>
#include <stdexcept>
#include <sys/wait.h>
#include <unistd.h>
extern char** environ;
#ifndef SOLAR_STORAGE_SCRIPTS
#error "SOLAR_STORAGE_SCRIPTS must name the installed helper directory"
#endif
namespace solar::storage {
std::string run_helper(const std::vector<std::string>& argv,double timeout,std::size_t limit){
    int pipes[2];if(pipe2(pipes,O_CLOEXEC))throw std::runtime_error("helper pipe");
    fcntl(pipes[0],F_SETFL,O_NONBLOCK);
    posix_spawn_file_actions_t actions;posix_spawn_file_actions_init(&actions);posix_spawn_file_actions_adddup2(&actions,pipes[1],STDOUT_FILENO);posix_spawn_file_actions_adddup2(&actions,pipes[1],STDERR_FILENO);posix_spawn_file_actions_addclose(&actions,pipes[0]);
    std::vector<char*> args;for(const auto& s:argv)args.push_back(const_cast<char*>(s.c_str()));args.push_back(nullptr);
    pid_t pid=-1;int rc=posix_spawnp(&pid,args[0],&actions,nullptr,args.data(),environ);posix_spawn_file_actions_destroy(&actions);close(pipes[1]);
    if(rc){close(pipes[0]);throw std::runtime_error("cannot launch helper");}
    auto deadline=std::chrono::steady_clock::now()+std::chrono::duration<double>(timeout);std::string out;bool done=false;int status=0;
    try{while(!done){
        char buf[4096];ssize_t n;
        while((n=read(pipes[0],buf,sizeof(buf)))>0){if(out.size()+std::size_t(n)>limit)throw std::runtime_error("helper output limit");out.append(buf,n);}
        auto w=waitpid(pid,&status,WNOHANG);if(w==pid)done=true;else if(w<0&&errno!=EINTR)throw std::runtime_error("helper wait");
        if(!done){if(std::chrono::steady_clock::now()>=deadline)throw std::runtime_error("helper timeout");pollfd f{pipes[0],POLLIN,0};poll(&f,1,50);}
    }
    // Drain bytes produced immediately before exit.
    char buf[4096];ssize_t n;while((n=read(pipes[0],buf,sizeof(buf)))>0){if(out.size()+std::size_t(n)>limit)throw std::runtime_error("helper output limit");out.append(buf,n);}
    close(pipes[0]);if(!WIFEXITED(status)||WEXITSTATUS(status)!=0)throw std::runtime_error("helper failed: "+out);return out;
    }catch(...){if(!done){kill(pid,SIGKILL);while(waitpid(pid,&status,0)<0&&errno==EINTR){}}close(pipes[0]);throw;}
}

AutoMaintenance::~AutoMaintenance(){stop();}
void AutoMaintenance::start(const Config& c){
    if(pid_>0)throw std::runtime_error("automatic maintenance already started");
    std::vector<std::string> values={"python3","-B",std::string(SOLAR_STORAGE_SCRIPTS)+"/storage_auto.py",
        "--config",c.path,"--parent-pid",std::to_string(getpid())};
    std::vector<char*> args;for(auto& v:values)args.push_back(v.data());args.push_back(nullptr);
    posix_spawnattr_t attr;int rc=posix_spawnattr_init(&attr);
    if(rc)throw std::runtime_error("automatic maintenance spawn attributes");
    rc=posix_spawnattr_setflags(&attr,POSIX_SPAWN_SETPGROUP);
    if(!rc)rc=posix_spawnattr_setpgroup(&attr,0);
    pid_t child=-1;
    if(!rc)rc=posix_spawnp(&child,args[0],nullptr,&attr,args.data(),environ);
    posix_spawnattr_destroy(&attr);
    if(rc)throw std::runtime_error("cannot launch automatic maintenance");
    pid_=child;
}
void AutoMaintenance::check(){
    if(pid_<0)return;
    int status=0;auto r=waitpid(pid_,&status,WNOHANG);
    if(r==pid_){
        // Remove remaining descendants in the worker process group on crash.
        kill(-pid_,SIGKILL);pid_=-1;
        throw std::runtime_error("automatic maintenance exited unexpectedly; inspect AUTO logs");
    }
    if(r<0&&errno!=EINTR)throw std::runtime_error("automatic maintenance wait failed");
}
void AutoMaintenance::stop() noexcept{
    if(pid_<0)return;
    kill(-pid_,SIGTERM);
    auto until=std::chrono::steady_clock::now()+std::chrono::seconds(5);
    int status=0;
    while(std::chrono::steady_clock::now()<until){
        auto r=waitpid(pid_,&status,WNOHANG);
        if(r==pid_ || (r<0&&errno==ECHILD)){
            kill(-pid_,SIGKILL);pid_=-1;return;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(25));
    }
    kill(-pid_,SIGKILL);
    while(waitpid(pid_,&status,0)<0&&errno==EINTR){}
    pid_=-1;
}

Config load_config(const std::string& path){
    // Bootstrap parser is offline and capped as a defensive execution limit,
    // not a storage retry or operational cadence default.
    auto envelope=nlohmann::json::parse(run_helper({"python3","-B",std::string(SOLAR_STORAGE_SCRIPTS)+"/storage_config.py","--config",path},60,1024*1024));
    return {envelope.at("config"),envelope.at("config_hash").get<std::string>(),path};
}
void set_observation_timezone(const Config& c){const auto tz=c.value.at("time").at("observation_timezone").get<std::string>();if(setenv("TZ",tz.c_str(),1))throw std::runtime_error("set timezone");tzset();}
std::string observation_day(std::uint64_t us){time_t sec=static_cast<time_t>(us/1000000);tm t{};if(!localtime_r(&sec,&t))throw std::runtime_error("observation date");char b[32];if(!strftime(b,sizeof(b),"%Y-%m-%d",&t))throw std::runtime_error("observation date format");return b;}
void startup_check(const Config& c){const auto& p=c.value.at("startup_check");double bound=p.at("max_attempts").get<double>()*(2*p.at("timeout_sec").get<double>()+p.at("backoff_max_sec").get<double>())+c.value.at("retention").at("reader_timeout_sec").get<double>();run_helper({"python3","-B",std::string(SOLAR_STORAGE_SCRIPTS)+"/storage_maintenance.py","startup","--config",c.path},bound,c.value.at("status").at("max_bytes"));}
nlohmann::json estimate(const Config& c,std::uint64_t n,std::uint64_t hop,std::uint64_t frames,double fs){
    auto bins=c.value.at("storage").at("save_freq_bins").get<std::uint64_t>();double requested=c.value.at("storage").at("save_interval_sec");
    if(!bins||n%bins)throw std::invalid_argument("save_freq_bins must divide actual input FFT bins");
    double cadence=double(frames)*hop/fs;if(requested<cadence)throw std::invalid_argument("save interval below source cadence");
    auto cycles=std::ceil(requested/cadence-1e-12);double effective=cycles*cadence;
    double roll=c.value.at("storage").at("roll_minutes").get<double>()*60;
    auto rows_per_file=std::max(1.0,std::floor(roll/effective));auto files=std::ceil(std::ceil(86400/effective)/rows_per_file);
    double hcap=c.value.at("storage").at("max_file_bytes"),pcap=c.value.at("quicklook").at("enabled").get<bool>()?c.value.at("quicklook").at("max_png_bytes").get<double>():0;
    double aux=2*c.value.at("ledger").at("max_bytes").get<double>()+c.value.at("budget").at("metadata_headroom_bytes").get<double>();
    auto total=files*(hcap+pcap)+aux;
    return {{"source_cadence_sec",cadence},{"source_support_sec",(double(frames-1)*hop+n)/fs},{"effective_save_cadence_sec",effective},{"input_records_per_window",cycles},{"frequency_bin_spacing_hz",fs/bins},{"base_arrays_bytes_per_day",std::ceil(86400/effective)*bins*4*sizeof(float)},{"reserved_cap_bytes_nominal_day",total},{"nominal_files_per_day",files},{"queue_payload_bytes",(c.value.at("storage").at("queue_capacity_records").get<std::uint64_t>()+1)*n*4*sizeof(double)},{"fits_nominal_daily_cap",total<=c.value.at("budget").at("daily_limit_bytes").get<double>()},{"note","Partial windows/restarts consume full conservative file caps; may stop earlier. Physical SD writes and OS logs excluded."}};
}
}
