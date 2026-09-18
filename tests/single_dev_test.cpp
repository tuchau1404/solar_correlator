#include <iostream>
#include <atomic>
#include <chrono>
#include <thread>
#include "sdrplay_api.h"

std::atomic<uint64_t> total_samples(0);
std::atomic<uint32_t> reset_count(0);

void StreamCallback(short *xi, short *xq, sdrplay_api_StreamCbParamsT *params, 
                    unsigned int numSamples, unsigned int reset, void *cbContext) {
    if (reset) {
        reset_count.fetch_add(1, std::memory_order_relaxed);
        std::cerr << "[Canh bao] Buffer Reset / Dropped packet!\n";
    }
    total_samples.fetch_add(numSamples, std::memory_order_relaxed);
}

void DummyEventCallback(sdrplay_api_EventT eventId, sdrplay_api_TunerSelectT tuner, 
                        sdrplay_api_EventParamsT *params, void *cbContext) {}

int main() {
    if (sdrplay_api_Open() != sdrplay_api_Success) {
        std::cerr << "Loi: Khong the ket noi sdrplay_api service!\n";
        return -1;
    }

    sdrplay_api_DeviceT devs[SDRPLAY_MAX_DEVICES];
    unsigned int numDevs = 0;
    sdrplay_api_GetDevices(devs, &numDevs, SDRPLAY_MAX_DEVICES);

    if (numDevs == 0) {
        std::cerr << "Loi: Khong tim thay thiet bi RSPdx nao!\n";
        sdrplay_api_Close();
        return -1;
    }

    std::cout << "Tim thay " << numDevs << " thiet bi. Dang test thiet bi [0]: " << devs[0].SerNo << "\n";
    sdrplay_api_SelectDevice(&devs[0]);

    sdrplay_api_DeviceParamsT *deviceParams = nullptr;
    sdrplay_api_GetDeviceParams(devs[0].dev, &deviceParams);

    // 1. Cau hinh kenh thu RF/IF
    auto *chParams = deviceParams->rxChannelA;
    chParams->tunerParams.rfFreq.rfHz = 35000000.0;                 // 35 MHz[cite: 1]
    chParams->tunerParams.bwType = sdrplay_api_BW_8_000;            // 8 MHz Analog Filter (Max cho RSPdx)
    chParams->ctrlParams.agc.enable = sdrplay_api_AGC_DISABLE;      // Tat IF AGC[cite: 1]
    chParams->tunerParams.gain.gRdB = 30;                           // Manual Gain 30 dB[cite: 1]

    // 2. Cau hinh phan cung thiet bi & Cong Anten C
    deviceParams->devParams->fsFreq.fsHz = 10000000.0;              // 10 MSPS[cite: 1]
    deviceParams->devParams->rspDxParams.antennaSel = sdrplay_api_RspDx_ANTENNA_C; // Cong C BNC[cite: 1]

    sdrplay_api_CallbackFnsT cbFns{};
    cbFns.StreamACbFn = StreamCallback;
    cbFns.EventCbFn = DummyEventCallback;

    if (sdrplay_api_Init(devs[0].dev, &cbFns, nullptr) != sdrplay_api_Success) {
        std::cerr << "Loi: Khong the Init thiet bi!\n";
        sdrplay_api_ReleaseDevice(&devs[0]);
        sdrplay_api_Close();
        return -1;
    }

    std::cout << "Dang thu thap du lieu 10 MSPS trong 5 giay...\n";
    std::this_thread::sleep_for(std::chrono::seconds(5));

    std::cout << "\n--- KET QUA TEST ---\n";
    std::cout << "Tong so mau thu duoc: " << total_samples.load() << "\n";
    std::cout << "Toc do lay mau thuc te: " << (total_samples.load() / 5.0) / 1e6 << " MSPS\n";
    std::cout << "So lan drop/reset bo dem: " << reset_count.load() << "\n";

    sdrplay_api_Uninit(devs[0].dev);
    sdrplay_api_ReleaseDevice(&devs[0]);
    sdrplay_api_Close();
    return 0;
}