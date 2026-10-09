#pragma once

#include <cstdlib>
#include <string>
#include <chrono>
#include <thread>
#include <iostream>
#include "config.hpp"

class HardwareGuard {
private:
    bool is_active_{false};

public:
    HardwareGuard() {
        start_calibration();
    }

    ~HardwareGuard() {
        turn_off();
    }

    HardwareGuard(const HardwareGuard&) = delete;
    HardwareGuard& operator=(const HardwareGuard&) = delete;

    void start_calibration() {
        if (!is_active_) {
            std::cout << "\n[Hardware Control] Kích hoạt chế độ Hiệu chuẩn (CALIBRATION):\n";
            std::cout << "  -> Kích GPIO " << SolarConfig::Hardware::PIN_RF_SWITCH 
                      << " = HIGH: Gạt RF Switch sang cổng Cal/Splitter...\n";
            std::system(("pinctrl set " + std::to_string(SolarConfig::Hardware::PIN_RF_SWITCH) + " op dh").c_str());

            std::cout << "  -> Kích GPIO " << SolarConfig::Hardware::PIN_NOISE_SOURCE 
                      << " = HIGH: Bật nguồn 15V Noise Source...\n";
            std::system(("pinctrl set " + std::to_string(SolarConfig::Hardware::PIN_NOISE_SOURCE) + " op dh").c_str());

            std::cout << "  -> Chờ phần cứng ổn định (" << SolarConfig::Hardware::WARMUP_DELAY_MS << " ms)...\n";
            std::this_thread::sleep_for(std::chrono::milliseconds(SolarConfig::Hardware::WARMUP_DELAY_MS));
            is_active_ = true;
        }
    }

    void turn_off() {
        if (is_active_) {
            std::cout << "\n[Hardware Control] Ngắt phần cứng về trạng thái an toàn (Antenna / OFF):\n";
            std::cout << "  -> Tắt GPIO " << SolarConfig::Hardware::PIN_NOISE_SOURCE 
                      << " = LOW: Ngắt nguồn 15V Noise Source trước...\n";
            std::system(("pinctrl set " + std::to_string(SolarConfig::Hardware::PIN_NOISE_SOURCE) + " op dl").c_str());

            std::cout << "  -> Tắt GPIO " << SolarConfig::Hardware::PIN_RF_SWITCH 
                      << " = LOW: Gạt RF Switch về 2 Anten ngoài trời...\n";
            std::system(("pinctrl set " + std::to_string(SolarConfig::Hardware::PIN_RF_SWITCH) + " op dl").c_str());
            is_active_ = false;
        }
    }

    bool is_active() const {
        return is_active_;
    }
};