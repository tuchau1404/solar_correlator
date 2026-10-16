#pragma once
#include <cstdint>
namespace SolarConfig::Network {
inline constexpr char UDP_DEST_IP[] = "192.168.1.2";
inline constexpr std::uint16_t UDP_DEST_PORT = 9999;
inline constexpr std::uint16_t UDP_PORT_PHASE = 9996;
inline constexpr std::uint16_t UDP_PORT_COHERENCE = 9995;
inline constexpr std::uint32_t TELEMETRY_MAGIC = 0x534F4C32; // SOL2
}
