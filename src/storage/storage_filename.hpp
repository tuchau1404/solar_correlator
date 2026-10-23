#pragma once
#include <cstdint>
#include <ctime>
#include <limits>
#include <stdexcept>
#include <string>

namespace solar::storage {
// TZ is set once by set_observation_timezone() before the worker starts.
// Keep the complete artifact ID: readable seconds alone can collide on restart.
// Full microsecond precision remains in HDF5 metadata/datasets and the ledger.
inline std::string observation_filename(std::uint64_t start_utc_us,
                                        const std::string& artifact_id) {
    const auto seconds = start_utc_us / 1000000;
    if (seconds > static_cast<std::uint64_t>(std::numeric_limits<std::time_t>::max()))
        throw std::out_of_range("observation timestamp exceeds time_t");
    const auto stamp = static_cast<std::time_t>(seconds);
    std::tm local{};
    if (!localtime_r(&stamp, &local))
        throw std::runtime_error("observation filename localtime failed");
    char text[64]{};
    if (!std::strftime(text, sizeof(text), "%Y-%m-%d_%H-%M-%S_UTC%z", &local))
        throw std::runtime_error("observation filename format failed");
    return std::string(text) + "_" + artifact_id + ".h5";
}
}
