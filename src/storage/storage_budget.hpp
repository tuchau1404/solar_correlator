#pragma once
#include "storage_config.hpp"
#include <sqlite3.h>
namespace solar::storage {
struct FileGrant {std::string id,path,day;std::uint64_t cap,png_cap;};
class Budget {
public:
    explicit Budget(const Config&);~Budget();
    FileGrant reserve(const std::string& id,const std::string& path,const std::string& day,std::uint64_t start,const std::string& session);
    void ready(const FileGrant&,std::uint64_t bytes);
    void quarantine(const FileGrant&);
    nlohmann::json snapshot(const std::string& day);
private:
    sqlite3* db_=nullptr;Config config_;
    void exec(const std::string& sql);
};
}
