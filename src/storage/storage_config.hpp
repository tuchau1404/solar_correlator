#pragma once
#include <nlohmann/json.hpp>
#include <string>
#include <vector>
namespace solar::storage {
struct Config {nlohmann::json value;std::string hash,path;};
std::string run_helper(const std::vector<std::string>& args,double timeout_sec,std::size_t output_limit);
// Owns a separate maintenance process, never performs network work on DSP thread.
class AutoMaintenance {
public:
    ~AutoMaintenance();
    void start(const Config& c);
    void check();
    void stop() noexcept;
private:
    int pid_=-1;
};
Config load_config(const std::string& path);
void startup_check(const Config& c);
std::string observation_day(std::uint64_t utc_us);
void set_observation_timezone(const Config& c);
nlohmann::json estimate(const Config& c,std::uint64_t fft,std::uint64_t hop,std::uint64_t frames,double fs);
}
