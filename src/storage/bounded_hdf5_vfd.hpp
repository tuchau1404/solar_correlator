#pragma once
#include <hdf5.h>
#include <cstdint>
namespace solar::storage {
// A pre-reserved logical file limit is enforced BEFORE EOA, pwrite and truncate.
// The caller owns quota reservation; this driver never creates quota itself.
hid_t bounded_hdf5_fapl(std::uint64_t reserved_bytes);
}
