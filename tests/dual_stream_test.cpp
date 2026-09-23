#include <iostream>
#include <iomanip>
#include <atomic>
#include <chrono>
#include <thread>
#include <cstring>
#include <unistd.h>
#include <sys/wait.h>
#include "sdrplay_api.h"

std::atomic<uint64_t> total_samples(0);
std::atomic<uint32_t> reset_count(0);

// Callback to receive I/Q stream
void StreamCallback(short *, short *, sdrplay_api_StreamCbParamsT *, 
                    unsigned int numSamples, unsigned int reset, void *) {
    if (reset) {
        reset_count.fetch_add(1, std::memory_order_relaxed);
    }
    total_samples.fetch_add(numSamples, std::memory_order_relaxed);
}

void DummyEventCallback(sdrplay_api_EventT, sdrplay_api_TunerSelectT, 
                        sdrplay_api_EventParamsT *, void *) {}

// Independent routine for each device running in an isolated process
void run_device_process(int dev_index, const std::string &target_serno) {
    sdrplay_api_ErrT err = sdrplay_api_Open();
    if (err != sdrplay_api_Success) {
        std::cerr << "[Process " << dev_index << "] Open API Error: " << sdrplay_api_GetErrorString(err) << "\n";
        _exit(1);
    }

    sdrplay_api_DeviceT devs[SDRPLAY_MAX_DEVICES];
    unsigned int numDevs = 0;
    sdrplay_api_GetDevices(devs, &numDevs, SDRPLAY_MAX_DEVICES);

    int selected_idx = -1;
    for (unsigned int i = 0; i < numDevs; i++) {
        if (target_serno == devs[i].SerNo) {
            selected_idx = i;
            break;
        }
    }

    if (selected_idx == -1) {
        std::cerr << "[Process " << dev_index << "] Device with Serial " << target_serno << " not found\n";
        sdrplay_api_Close();
        _exit(1);
    }

    err = sdrplay_api_SelectDevice(&devs[selected_idx]);
    if (err != sdrplay_api_Success) {
        std::cerr << "[Process " << dev_index << "] SelectDevice Error: " << sdrplay_api_GetErrorString(err) << "\n";
        sdrplay_api_Close();
        _exit(1);
    }

    sdrplay_api_DeviceParamsT *deviceParams = nullptr;
    sdrplay_api_GetDeviceParams(devs[selected_idx].dev, &deviceParams);

    // RF Tuner configuration: 35 MHz Center, 8 MHz Analog Filter
    auto *chParams = deviceParams->rxChannelA;
    chParams->tunerParams.rfFreq.rfHz = 35000000.0;                 // 35 MHz
    chParams->tunerParams.bwType = sdrplay_api_BW_8_000;            // 8 MHz BW
    chParams->ctrlParams.agc.enable = sdrplay_api_AGC_DISABLE;      // Disable AGC
    chParams->tunerParams.gain.gRdB = 30;                           // Manual Gain 30 dB

    // Hardware parameters: 10 MSPS, Antenna Port C (BNC)
    deviceParams->devParams->fsFreq.fsHz = 10000000.0;              // 10 MSPS
    deviceParams->devParams->rspDxParams.antennaSel = sdrplay_api_RspDx_ANTENNA_C;

    sdrplay_api_CallbackFnsT cbFns{};
    cbFns.StreamACbFn = StreamCallback;
    cbFns.EventCbFn = DummyEventCallback;

    err = sdrplay_api_Init(devs[selected_idx].dev, &cbFns, nullptr);
    if (err != sdrplay_api_Success) {
        std::cerr << "[Process " << dev_index << "] Init Error: " << sdrplay_api_GetErrorString(err) << "\n";
        sdrplay_api_ReleaseDevice(&devs[selected_idx]);
        sdrplay_api_Close();
        _exit(1);
    }

    // Stream acquisition for 10 seconds
    std::this_thread::sleep_for(std::chrono::seconds(10));

    uint64_t total = total_samples.load(std::memory_order_relaxed);
    double actual_rate = (static_cast<double>(total) / 10.0) / 1e6;

    std::cout << "-> Device [" << dev_index << "] Results (Serial " << target_serno << "):\n"
              << "   + Total samples acquired : " << total << "\n"
              << "   + Actual sample rate     : " << std::fixed << std::setprecision(5) << actual_rate << " MSPS\n"
              << "   + Buffer Reset/Drop count: " << reset_count.load(std::memory_order_relaxed) << "\n";

    sdrplay_api_Uninit(devs[selected_idx].dev);
    sdrplay_api_ReleaseDevice(&devs[selected_idx]);
    sdrplay_api_Close();
    _exit(0);
}

int main() {
    // 1. Scan serial numbers of both devices prior to fork
    if (sdrplay_api_Open() != sdrplay_api_Success) {
        std::cerr << "[Error] Failed to connect to SDRplay API service!\n";
        return -1;
    }

    sdrplay_api_DeviceT devs[SDRPLAY_MAX_DEVICES];
    unsigned int numDevs = 0;
    sdrplay_api_GetDevices(devs, &numDevs, SDRPLAY_MAX_DEVICES);

    if (numDevs < 2) {
        std::cerr << "[Error] At least 2 RSPdx devices required! Found: " << numDevs << "\n";
        sdrplay_api_Close();
        return -1;
    }

    std::string ser0 = devs[0].SerNo;
    std::string ser1 = devs[1].SerNo;
    std::cout << "[Hardware] Detected 2 devices: " << ser0 << " and " << ser1 << "\n";
    sdrplay_api_Close(); // Close API handle before spawning child processes

    std::cout << ">>> Starting simultaneous dual-stream test: 10 MSPS (80 MB/s) for 10s...\n\n";

    // 2. Fork into two parallel worker processes
    pid_t p1 = fork();
    if (p1 == 0) {
        run_device_process(0, ser0);
    }

    pid_t p2 = fork();
    if (p2 == 0) {
        run_device_process(1, ser1);
    }

    // 3. Await completion of both child processes
    int status;
    waitpid(p1, &status, 0);
    waitpid(p2, &status, 0);

    std::cout << "\n>>> Simultaneous dual-device test completed successfully.\n";
    return 0;
}