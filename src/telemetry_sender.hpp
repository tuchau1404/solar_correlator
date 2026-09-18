#pragma once

#include <vector>
#include <string>
#include <cstdint>
#include <cstring>
#include <stdexcept>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <fcntl.h>
#include "config.hpp"

#pragma pack(push, 1)
struct TelemetryHeader {
    uint32_t magic;              // "SOLR" (0x534F4C52)
    uint64_t frame_id;           // ID Khung liên tục
    uint64_t timestamp_us;       // Dấu thời gian UTC thực
    uint32_t integration_frames; // M = 1953
    uint32_t payload_flags;      // Bitmask định danh
};
#pragma pack(pop)

class TelemetrySender {
public:
    TelemetrySender(const std::string& dest_ip, uint16_t dest_port) {
        sock_ = socket(AF_INET, SOCK_DGRAM, 0);
        if (sock_ < 0) {
            throw std::runtime_error("[Telemetry] Không thể khởi tạo UDP Socket");
        }

        // Bật cờ Non-blocking (O_NONBLOCK) để bảo vệ luồng chính
        int flags = fcntl(sock_, F_GETFL, 0);
        fcntl(sock_, F_SETFL, flags | O_NONBLOCK);

        dest_addr_.sin_family = AF_INET;
        dest_addr_.sin_port = htons(dest_port);
        if (inet_pton(AF_INET, dest_ip.c_str(), &dest_addr_.sin_addr) <= 0) {
            throw std::runtime_error("[Telemetry] Địa chỉ IP đích không hợp lệ");
        }
    }

    ~TelemetrySender() {
        if (sock_ >= 0) close(sock_);
    }

    void send_spectrum(uint64_t frame_id, uint64_t timestamp_us, const std::vector<float>& spectrum_db) {
        // Cấu trúc gói 8224 Bytes: Header 32B + Payload 8192B
        std::vector<uint8_t> packet(sizeof(TelemetryHeader) + spectrum_db.size() * sizeof(float));
        
        TelemetryHeader* header = reinterpret_cast<TelemetryHeader*>(packet.data());
        header->magic = htonl(SolarConfig::Network::TELEMETRY_MAGIC); // Đổi byte order cho mạng
        header->frame_id = frame_id; 
        header->timestamp_us = timestamp_us;
        header->integration_frames = SolarConfig::Correlator::INTEGRATION_FRAMES;
        header->payload_flags = 0x01; // Bit 0: Preview dB Mode

        // Chép payload phổ dB vào gói
        float* payload = reinterpret_cast<float*>(packet.data() + sizeof(TelemetryHeader));
        std::memcpy(payload, spectrum_db.data(), spectrum_db.size() * sizeof(float));

        // Phát UDP non-blocking - fire and forget
        sendto(sock_, packet.data(), packet.size(), 0, 
               reinterpret_cast<struct sockaddr*>(&dest_addr_), sizeof(dest_addr_));
    }

private:
    int sock_{-1};
    struct sockaddr_in dest_addr_{};
};