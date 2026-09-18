#include <iostream>
#include <atomic>
#include <chrono>
#include <thread>
#include <cstring>
#include <unistd.h>
#include <sys/wait.h>
#include "sdrplay_api.h"

std::atomic<uint64_t> total_samples(0);
std::atomic<uint32_t> reset_count(0);

// Callback nhan luong I/Q
void StreamCallback(short *xi, short *xq, sdrplay_api_StreamCbParamsT *params, 
                    unsigned int numSamples, unsigned int reset, void *cbContext) {
    if (reset) reset_count.fetch_add(1, std::memory_order_relaxed);
    total_samples.fetch_add(numSamples, std::memory_order_relaxed);
}

void DummyEventCallback(sdrplay_api_EventT eventId, sdrplay_api_TunerSelectT tuner, 
                        sdrplay_api_EventParamsT *params, void *cbContext) {}

// Ham chay doc lap cho tung thiet bi tren 1 tien trinh rieng
void run_device_process(int dev_index, const std::string &target_serno) {
    sdrplay_api_ErrT err = sdrplay_api_Open();
    if (err != sdrplay_api_Success) {
        std::cerr << "[Tien trinh " << dev_index << "] Loi Open API: " << sdrplay_api_GetErrorString(err) << "\n";
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
        std::cerr << "[Tien trinh " << dev_index << "] Khong tim thay Serial " << target_serno << "\n";
        sdrplay_api_Close();
        _exit(1);
    }

    err = sdrplay_api_SelectDevice(&devs[selected_idx]);
    if (err != sdrplay_api_Success) {
        std::cerr << "[Tien trinh " << dev_index << "] Loi SelectDevice: " << sdrplay_api_GetErrorString(err) << "\n";
        sdrplay_api_Close();
        _exit(1);
    }

    sdrplay_api_DeviceParamsT *deviceParams = nullptr;
    sdrplay_api_GetDeviceParams(devs[selected_idx].dev, &deviceParams);

    // Cau hinh Tuner 35 MHz, loc 8 MHz
    auto *chParams = deviceParams->rxChannelA;
    chParams->tunerParams.rfFreq.rfHz = 35000000.0;                 // 35 MHz
    chParams->tunerParams.bwType = sdrplay_api_BW_8_000;            // 8 MHz BW
    chParams->ctrlParams.agc.enable = sdrplay_api_AGC_DISABLE;      // Tat AGC[cite: 1]
    chParams->tunerParams.gain.gRdB = 30;                           // Manual Gain 30 dB[cite: 1]

    // Cau hinh 10 MSPS, Cong C BNC
    deviceParams->devParams->fsFreq.fsHz = 10000000.0;              // 10 MSPS[cite: 1]
    deviceParams->devParams->rspDxParams.antennaSel = sdrplay_api_RspDx_ANTENNA_C; // Cong C BNC[cite: 1]

    sdrplay_api_CallbackFnsT cbFns{};
    cbFns.StreamACbFn = StreamCallback;
    cbFns.EventCbFn = DummyEventCallback;

    err = sdrplay_api_Init(devs[selected_idx].dev, &cbFns, nullptr);
    if (err != sdrplay_api_Success) {
        std::cerr << "[Tien trinh " << dev_index << "] Loi Init: " << sdrplay_api_GetErrorString(err) << "\n";
        sdrplay_api_ReleaseDevice(&devs[selected_idx]);
        sdrplay_api_Close();
        _exit(1);
    }

    // Stream trong 10 giay
    std::this_thread::sleep_for(std::chrono::seconds(10));

    double actual_rate = (total_samples.load() / 10.0) / 1e6;
    std::cout << "-> Ket qua Thiet bi [" << dev_index << "] (Serial " << target_serno << "):\n"
              << "   + Toc do lay mau: " << actual_rate << " MSPS\n"
              << "   + So lan Buffer Reset/Drop: " << reset_count.load() << "\n";

    sdrplay_api_Uninit(devs[selected_idx].dev);
    sdrplay_api_ReleaseDevice(&devs[selected_idx]);
    sdrplay_api_Close();
    _exit(0);
}

int main() {
    // 1. Quet danh sach Serial cua ca 2 thiet bi truoc khi Fork
    if (sdrplay_api_Open() != sdrplay_api_Success) {
        std::cerr << "Loi: Khong the ket noi API Service!\n";
        return -1;
    }

    sdrplay_api_DeviceT devs[SDRPLAY_MAX_DEVICES];
    unsigned int numDevs = 0;
    sdrplay_api_GetDevices(devs, &numDevs, SDRPLAY_MAX_DEVICES);

    if (numDevs < 2) {
        std::cerr << "Loi: Can 2 thiet bi RSPdx! Tim thay: " << numDevs << "\n";
        sdrplay_api_Close();
        return -1;
    }

    std::string ser0 = devs[0].SerNo;
    std::string ser1 = devs[1].SerNo;
    std::cout << "Phat hien 2 thiet bi: " << ser0 << " va " << ser1 << "\n";
    sdrplay_api_Close(); // Dong lai de tien trinh con mo ket noi rieng

    std::cout << ">>> Bat dau chay thu nghiem dong thoi 2 luong 10 MSPS (80 MB/s) trong 10s...\n\n";

    // 2. Tao 2 tien trinh con chay song song
    pid_t p1 = fork();
    if (p1 == 0) {
        run_device_process(0, ser0);
    }

    pid_t p2 = fork();
    if (p2 == 0) {
        run_device_process(1, ser1);
    }

    // 3. Cho ca 2 tien trinh hoan tat
    int status;
    waitpid(p1, &status, 0);
    waitpid(p2, &status, 0);

    std::cout << "\n>>> Hoan tat kiem tra 2 thiet bi dong thoi.\n";
    return 0;
}