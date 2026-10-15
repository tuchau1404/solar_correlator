// Linux-only standalone auto-power diagnostic. No TimeAligner.
#include <algorithm>
#include <array>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <iomanip>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#include <fcntl.h>
#include <poll.h>
#include <sys/prctl.h>
#include <sys/wait.h>
#include <unistd.h>
#include <sdrplay_api.h>
#include "config.hpp"
#include "ring_buffer.hpp"
#include "correlator.hpp"
#include "telemetry_sender.hpp"

namespace {
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
std::atomic<bool> running{true};
static_assert(std::atomic<bool>::is_always_lock_free);
void stop_signal(int) noexcept { running.store(false, std::memory_order_relaxed); }
void signals() {
    struct sigaction action{};
    action.sa_handler = stop_signal;
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGINT, &action, nullptr) || sigaction(SIGTERM, &action, nullptr))
        throw std::runtime_error("sigaction failed");
}
void require(sdrplay_api_ErrT err, const char* operation) {
    if (err != sdrplay_api_Success)
        throw std::runtime_error(std::string(operation) + ": " + sdrplay_api_GetErrorString(err));
}
// A dead parent cannot leave a producer running. Kernel SIGKILL does not run
// SDK cleanup; stale SHM is deliberately NOT silently replaced on next start.
void parent_guard(pid_t parent) {
    if (prctl(PR_SET_PDEATHSIG, SIGKILL) != 0 || getppid() != parent) _exit(125);
}
struct Child {
    pid_t pid{-1};
    int status{};
    bool poll() {
        if (pid <= 0) return true;
        pid_t result;
        do { result = waitpid(pid, &status, WNOHANG); } while (result < 0 && errno == EINTR);
        if (result == pid || (result < 0 && errno == ECHILD)) { pid = -1; return true; }
        if (result < 0) throw std::runtime_error("waitpid failed");
        return false;
    }
    void check() { if (poll()) throw std::runtime_error("SDR subprocess exited"); }
    void stop() noexcept {
        if (pid <= 0) return;
        try {
            if (poll()) return;
            if (pid > 0) kill(pid, SIGTERM);
            auto deadline = Clock::now() + 1500ms;
            while (!poll() && Clock::now() < deadline) std::this_thread::sleep_for(10ms);
            if (pid <= 0) return;
            kill(pid, SIGKILL);
            deadline = Clock::now() + 1500ms;
            while (!poll() && Clock::now() < deadline) std::this_thread::sleep_for(10ms);
            if (pid > 0) std::cerr << "Child " << pid << " not reaped after SIGKILL; kernel/driver may be blocked\n";
        } catch (...) { std::cerr << "Child cleanup failed\n"; }
    }
    ~Child() { stop(); }
};
struct Api {
    bool opened{}, locked{}, selected{}, initialized{};
    sdrplay_api_DeviceT device{};
    void open() { require(sdrplay_api_Open(), "Open"); opened = true; }
    void lock() { require(sdrplay_api_LockDeviceApi(), "LockDeviceApi"); locked = true; }
    void unlock() { require(sdrplay_api_UnlockDeviceApi(), "UnlockDeviceApi"); locked = false; }
    void close() noexcept {
        bool failed = false;
        // Exit without destroying the callback context if Uninit cannot confirm
        // callbacks have stopped. Parent supervises SDK calls that hang.
        if (initialized) {
            const auto e = sdrplay_api_Uninit(device.dev);
            if (e != sdrplay_api_Success) {
                std::cerr << "Uninit failed: " << sdrplay_api_GetErrorString(e) << '\n';
                _exit(3);
            }
            initialized = false;
        }
        if (selected) {
            const auto e = sdrplay_api_ReleaseDevice(&device);
            if (e != sdrplay_api_Success) { std::cerr << "ReleaseDevice failed\n"; failed = true; }
            selected = false;
        }
        if (locked) {
            if (sdrplay_api_UnlockDeviceApi() != sdrplay_api_Success) failed = true;
            locked = false;
        }
        if (opened) {
            if (sdrplay_api_Close() != sdrplay_api_Success) { std::cerr << "Close failed\n"; failed = true; }
            opened = false;
        }
        if (failed) _exit(3);
    }
    ~Api() { close(); }
};
using Serials = std::array<std::array<char, 128>, 2>;
Serials discover() {
    int pipefd[2];
    if (pipe2(pipefd, O_CLOEXEC | O_NONBLOCK) != 0) throw std::runtime_error("pipe2 failed");
    Child child;
    const pid_t parent = getpid();
    std::cout.flush(); std::cerr.flush();
    child.pid = fork();
    if (child.pid < 0) { close(pipefd[0]); close(pipefd[1]); throw std::runtime_error("discovery fork failed"); }
    if (child.pid == 0) {
        parent_guard(parent);
        close(pipefd[0]);
        try {
            Serials result{};
            {
                Api api;
                api.open(); api.lock();
                sdrplay_api_DeviceT devices[SDRPLAY_MAX_DEVICES]{};
                unsigned count{};
                require(sdrplay_api_GetDevices(devices, &count, SDRPLAY_MAX_DEVICES), "GetDevices");
                std::vector<std::string> serials;
                for (unsigned i = 0; i < count; ++i)
                    if (devices[i].hwVer == SDRPLAY_RSPdx_ID) serials.emplace_back(devices[i].SerNo);
                std::sort(serials.begin(), serials.end());
                if (serials.size() != 2 || serials[0] == serials[1])
                    throw std::runtime_error("Exactly two RSPdx devices required (RSPdxR2 is not selected)");
                for (std::size_t i = 0; i < result.size(); ++i) {
                    if (serials[i].size() >= result[i].size()) throw std::runtime_error("Serial too long");
                    std::memcpy(result[i].data(), serials[i].c_str(), serials[i].size() + 1);
                }
                api.unlock();
            }
            const auto n = write(pipefd[1], result.data(), sizeof(result));
            _exit(n == static_cast<ssize_t>(sizeof(result)) ? 0 : 1);
        } catch (const std::exception& e) { std::cerr << "Discovery: " << e.what() << '\n'; _exit(1); }
    }
    close(pipefd[1]);
    Serials result{};
    std::size_t received{};
    const auto deadline = Clock::now() + 10s;
    try {
        while (running.load() && Clock::now() < deadline) {
            const auto n = read(pipefd[0], reinterpret_cast<char*>(result.data()) + received,
                                sizeof(result) - received);
            if (n > 0) received += static_cast<std::size_t>(n);
            else if (n < 0 && errno != EAGAIN && errno != EINTR) throw std::runtime_error("discovery pipe read failed");
            if (child.poll()) {
                // The child can exit between read(EAGAIN) and waitpid.
                if (received < sizeof(result)) {
                    const auto remaining = read(pipefd[0], reinterpret_cast<char*>(result.data()) + received,
                                                sizeof(result) - received);
                    if (remaining > 0) received += static_cast<std::size_t>(remaining);
                }
                if (received != sizeof(result) || !WIFEXITED(child.status) || WEXITSTATUS(child.status) != 0)
                    throw std::runtime_error("Discovery failed");
                close(pipefd[0]);
                return result;
            }
            std::this_thread::sleep_for(10ms);
        }
        throw std::runtime_error("Discovery cancelled or timed out");
    } catch (...) { close(pipefd[0]); throw; }
}
struct CallbackContext {
    SharedMemoryRingBuffer& rb;
    sdrplay_api_DeviceT* device{nullptr};
    std::atomic<bool> fatal{false};
    std::atomic<bool> overload{false};
    std::atomic<bool> overload_ack{false};
    bool have_sample{false}; // stream callback thread only
    std::uint32_t expected_sample{};
};
void stream(short* i, short* q, sdrplay_api_StreamCbParamsT* params, unsigned n,
            unsigned reset, void* opaque) noexcept {
    auto& ctx = *static_cast<CallbackContext*>(opaque);
    if (!ctx.rb.is_stream_started() || n == 0) return;
    if (reset || !params) { ctx.fatal.store(true); return; }
    if (ctx.have_sample && params->firstSampleNum != ctx.expected_sample) {
        ctx.fatal.store(true); return;
    }
    ctx.expected_sample = params->firstSampleNum + n;
    ctx.have_sample = true;
    try { if (n) ctx.rb.write(i, q, n); }
    catch (...) { ctx.fatal.store(true); }
}
void event(sdrplay_api_EventT id, sdrplay_api_TunerSelectT tuner,
           sdrplay_api_EventParamsT* params, void* opaque) noexcept {
    auto& ctx = *static_cast<CallbackContext*>(opaque);
    if (id == sdrplay_api_DeviceRemoved) ctx.fatal.store(true);
    if (id == sdrplay_api_PowerOverloadChange && params) {
        ctx.overload.store(params->powerOverloadParams.powerOverloadChangeType == sdrplay_api_Overload_Detected);
        // SDRplay's reference callback acknowledges each overload notification.
        if (!ctx.device || sdrplay_api_Update(ctx.device->dev, tuner,
                sdrplay_api_Update_Ctrl_OverloadMsgAck, sdrplay_api_Update_Ext1_None)
                != sdrplay_api_Success) ctx.fatal.store(true);
        ctx.overload_ack.store(true);
    }
}
int producer(const std::string& serial, const std::string& name) {
    try {
        SharedMemoryRingBuffer rb(name, SolarConfig::SHM::BUFFER_CAPACITY, false);
        CallbackContext ctx{rb};
        Api api; // destroyed before ctx and rb
        api.open(); api.lock();
        sdrplay_api_DeviceT devices[SDRPLAY_MAX_DEVICES]{};
        unsigned count{};
        require(sdrplay_api_GetDevices(devices, &count, SDRPLAY_MAX_DEVICES), "GetDevices");
        bool found = false;
        for (unsigned i = 0; i < count; ++i) {
            if (serial == devices[i].SerNo && devices[i].hwVer == SDRPLAY_RSPdx_ID) {
                api.device = devices[i]; found = true; break;
            }
        }
        if (!found) throw std::runtime_error("Selected serial disappeared");
        require(sdrplay_api_SelectDevice(&api.device), "SelectDevice");
        api.selected = true;
        api.unlock();
        sdrplay_api_DeviceParamsT* params = nullptr;
        require(sdrplay_api_GetDeviceParams(api.device.dev, &params), "GetDeviceParams");
        if (!params || !params->devParams || !params->rxChannelA) throw std::runtime_error("Null device parameters");
        auto& ch = *params->rxChannelA;
        ch.tunerParams.rfFreq.rfHz = SolarConfig::SDR::RF_CENTER_FREQ_HZ;
        ch.tunerParams.bwType = SolarConfig::SDR::IF_BW;
        ch.tunerParams.ifType = sdrplay_api_IF_Zero;
        ch.ctrlParams.decimation.enable = 0;
        ch.ctrlParams.agc.enable = SolarConfig::SDR::AGC_MODE;
        ch.tunerParams.gain.LNAstate = SolarConfig::SDR::LNA_STATE;
        ch.tunerParams.gain.gRdB = SolarConfig::SDR::GAIN_GRDB;
        params->devParams->fsFreq.fsHz = SolarConfig::SDR::SAMPLE_RATE_HZ;
        params->devParams->rspDxParams.antennaSel = SolarConfig::SDR::ANTENNA_PORT;
        ctx.device = &api.device;
        sdrplay_api_CallbackFnsT callbacks{};
        callbacks.StreamACbFn = stream;
        callbacks.EventCbFn = event;
        require(sdrplay_api_Init(api.device.dev, &callbacks, &ctx), "Init");
        api.initialized = true;
        rb.set_ready(true);
        std::cerr << "Streaming serial " << serial << " via " << name << '\n';
        while (running.load() && !ctx.fatal.load()) {
            if (ctx.overload_ack.exchange(false)) {
                std::cerr << serial << (ctx.overload.load() ? " OVERLOAD\n" : " overload cleared\n");
            }
            std::this_thread::sleep_for(10ms);
        }
        rb.set_ready(false);
        rb.set_start_stream(false);
        return ctx.fatal.load() ? 2 : 0;
    } catch (const std::exception& e) { std::cerr << "Producer: " << e.what() << '\n'; return 1; }
}
// Own only SHM objects created by this run. Existing names cause an explicit
// failure, rather than unlinking a live acquisition's buffers.
struct ShmOwner {
    std::unique_ptr<SharedMemoryRingBuffer> one, two;
    ~ShmOwner() {
        if (one) SharedMemoryRingBuffer::unlink_shm(SolarConfig::SHM::CH1_NAME);
        if (two) SharedMemoryRingBuffer::unlink_shm(SolarConfig::SHM::CH2_NAME);
    }
};
std::uint16_t port(const char* text) {
    errno = 0;
    char* end{};
    const long value = std::strtol(text, &end, 10);
    if (errno || end == text || *end || value < 1 || value > 65535) throw std::invalid_argument("Invalid port");
    return static_cast<std::uint16_t>(value);
}
bool dispatch(TelemetrySender& sender, std::uint64_t id, std::uint64_t stamp,
              const std::vector<float>& db) {
    const auto result = sender.send_spectrum(id, stamp, db);
    return result.queued && result.datagrams == TelemetrySender::chunks && result.error == 0;
}
}

int main(int argc, char** argv) {
    try {
        signals();
        if (argc > 4) throw std::invalid_argument("Usage: dual_waterfall_test [IPv4] [port1] [port2]");
        const std::string ip = argc > 1 ? argv[1] : SolarConfig::Network::UDP_DEST_IP;
        const auto port1 = argc > 2 ? port(argv[2]) : std::uint16_t{9998};
        const auto port2 = argc > 3 ? port(argv[3]) : std::uint16_t{9999};
        if (port1 == port2) throw std::invalid_argument("Ports must differ");
        // Parent never opens SDRplay or starts threads before these forks.
        const auto serials = discover();
        if (!running.load()) return 0;
        ShmOwner shm;
        shm.one = std::make_unique<SharedMemoryRingBuffer>(SolarConfig::SHM::CH1_NAME, SolarConfig::SHM::BUFFER_CAPACITY, true);
        shm.two = std::make_unique<SharedMemoryRingBuffer>(SolarConfig::SHM::CH2_NAME, SolarConfig::SHM::BUFFER_CAPACITY, true);
        Child child1, child2; // destroyed before SHM
        const pid_t parent = getpid();
        child1.pid = fork();
        if (child1.pid < 0) throw std::runtime_error("fork ch1 failed");
        if (child1.pid == 0) { parent_guard(parent); _exit(producer(serials[0].data(), SolarConfig::SHM::CH1_NAME)); }
        child2.pid = fork();
        if (child2.pid < 0) throw std::runtime_error("fork ch2 failed");
        if (child2.pid == 0) { parent_guard(parent); _exit(producer(serials[1].data(), SolarConfig::SHM::CH2_NAME)); }
        auto& rb1 = *shm.one;
        auto& rb2 = *shm.two;
        const auto deadline = Clock::now() + 10s;
        while (running.load() && (!rb1.is_ready() || !rb2.is_ready())) {
            child1.check(); child2.check();
            if (Clock::now() >= deadline) throw std::runtime_error("SDR readiness timeout");
            std::this_thread::sleep_for(10ms);
        }
        if (!running.load()) return 0;
        constexpr auto n = SolarConfig::Correlator::FFT_SIZE;
        FXCorrelator::Options options;
        options.running = &running;
        options.timeout = 1000ms;
        // Plan FFTs and allocate all storage while callback writes are gated off.
        FXCorrelator fx(options);
        TelemetrySender sender1(ip, port1), sender2(ip, port2);
        std::vector<float> phi(n), cross(n), phase(n), coherence(n), db1(n), db2(n);
        rb1.set_start_stream(true); rb2.set_start_stream(true);
        bool primed = false;
        unsigned consecutive_failures = 0;
        unsigned total_failures = 0;
        std::uint64_t id = 0, ok1 = 0, ok2 = 0;
        while (running.load()) {
            child1.check(); child2.check();
            if (!rb1.is_ready() || !rb2.is_ready()) throw std::runtime_error("SDR lost readiness");
            try {
                if (!primed) {
                    rb1.flush(); rb2.flush();
                    fx.prime_buffers(rb1, rb2);
                    primed = true;
                }
                fx.process_integration_cycle(rb1, rb2, phi, cross, phase, coherence);
            } catch (const std::exception& e) {
                if (!running.load()) break;
                primed = false;
                ++id; // invalid integration leaves an explicit display gap
                child1.check(); child2.check();
                if (++consecutive_failures >= 3 || ++total_failures >= 10) throw;
                std::cerr << "DSP recovery " << consecutive_failures << ": " << e.what() << '\n';
                std::this_thread::sleep_for(20ms);
                continue;
            }
            consecutive_failures = 0;
            for (std::size_t k = 0; k < n; ++k) {
                db1[k] = static_cast<float>(10 * std::log10(std::max(fx.auto_power1()[k], 1e-12)));
                db2[k] = static_cast<float>(10 * std::log10(std::max(fx.auto_power2()[k], 1e-12)));
            }
            const auto us = std::chrono::duration_cast<std::chrono::microseconds>(
                std::chrono::system_clock::now().time_since_epoch()).count();
            if (us < 0) throw std::runtime_error("System UTC clock precedes epoch");
            ++id;
            ok1 += dispatch(sender1, id, static_cast<std::uint64_t>(us), db1);
            ok2 += dispatch(sender2, id, static_cast<std::uint64_t>(us), db2);
            if (id % 25 == 0) {
                const auto peak1 = std::max_element(db1.begin(), db1.end());
                const auto peak2 = std::max_element(db2.begin(), db2.end());
                const double start = (SolarConfig::SDR::RF_CENTER_FREQ_HZ - SolarConfig::SDR::SAMPLE_RATE_HZ / 2) / 1e6;
                const double step = SolarConfig::SDR::SAMPLE_RATE_HZ / (1e6 * n);
                std::cout << std::fixed << std::setprecision(3) << "Frame " << id
                    << " peaks " << *peak1 << " dB @ " << start + (peak1 - db1.begin()) * step
                    << " MHz / " << *peak2 << " dB @ " << start + (peak2 - db2.begin()) * step
                    << " MHz; queued " << ok1 << '/' << ok2
                    << "; UDP failed " << sender1.stats().failed << '/' << sender2.stats().failed
                    << "; errno " << sender1.stats().last_errno << '/' << sender2.stats().last_errno
                    << "; overflow events " << rb1.get_overflow_count() << '/' << rb2.get_overflow_count() << '\n';
            }
        }
        rb1.set_start_stream(false); rb2.set_start_stream(false);
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "Fatal: " << e.what() << '\n';
        return running.load() ? 1 : 0;
    }
}
