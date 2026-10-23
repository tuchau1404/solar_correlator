#pragma once
#include "storage_record.hpp"
#include <hdf5.h>
#include <nlohmann/json.hpp>
#include <map>
#include <string>
namespace solar::storage {
class Hdf5Writer {
public:
    Hdf5Writer(const std::string& temp_path,std::uint64_t reserved_bytes,
               const nlohmann::json& metadata,const std::vector<double>& frequency,
               const std::vector<float>& calibration_phi,std::size_t chunk_rows,
               const std::string& compression,unsigned compression_level);
    ~Hdf5Writer();
    Hdf5Writer(const Hdf5Writer&)=delete;Hdf5Writer& operator=(const Hdf5Writer&)=delete;
    void append(const std::vector<OutputRecord>& records);
    void flush();
    // Only closes. Publishing/rename belongs to lifecycle owner and ledger.
    void close();
    std::uint64_t rows() const{return rows_;}
private:
    void cleanup() noexcept;
    hid_t file_=-1;
    std::map<std::string,hid_t> datasets_;
    std::size_t bins_=0;
    std::uint64_t rows_=0;
    bool failed_=false;
};
}
