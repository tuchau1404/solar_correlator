#include "storage_budget.hpp"
#include <chrono>
#include <filesystem>
#include <stdexcept>
namespace solar::storage {
namespace {
struct Statement{sqlite3_stmt* p=nullptr;Statement(sqlite3* db,const char* s){if(sqlite3_prepare_v2(db,s,-1,&p,nullptr)!=SQLITE_OK)throw std::runtime_error(sqlite3_errmsg(db));}~Statement(){sqlite3_finalize(p);}void text(int i,const std::string& v){if(sqlite3_bind_text(p,i,v.c_str(),-1,SQLITE_TRANSIENT)!=SQLITE_OK)throw std::runtime_error("ledger text bind");}void number(int i,std::uint64_t v){if(v>INT64_MAX||sqlite3_bind_int64(p,i,static_cast<sqlite3_int64>(v))!=SQLITE_OK)throw std::runtime_error("ledger integer bind");}int step(){return sqlite3_step(p);}};
std::uint64_t utc(){return std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::system_clock::now().time_since_epoch()).count();}
}
void Budget::exec(const std::string& s){char* e=nullptr;int r=sqlite3_exec(db_,s.c_str(),nullptr,nullptr,&e);std::string msg=e?e:"ledger error";sqlite3_free(e);if(r!=SQLITE_OK)throw std::runtime_error(msg);}
Budget::Budget(const Config& c):config_(c){
    auto path=c.value.at("ledger").at("path").get<std::string>();
    if(std::filesystem::file_size(path)>c.value.at("ledger").at("max_bytes").get<std::uint64_t>())throw std::runtime_error("existing ledger exceeds configured cap");
    if(sqlite3_open_v2(path.c_str(),&db_,SQLITE_OPEN_READWRITE|SQLITE_OPEN_NOMUTEX,nullptr)!=SQLITE_OK){if(db_)sqlite3_close(db_);db_=nullptr;throw std::runtime_error("open existing ledger");}
    try{sqlite3_busy_timeout(db_,int(c.value.at("ledger").at("busy_timeout_sec").get<double>()*1000));exec("PRAGMA journal_mode=DELETE");exec("PRAGMA synchronous=FULL");Statement v(db_,"PRAGMA user_version");if(v.step()!=SQLITE_ROW||sqlite3_column_int(v.p,0)!=1)throw std::runtime_error("unsupported ledger version");
        Statement p(db_,"PRAGMA page_size");if(p.step()!=SQLITE_ROW)throw std::runtime_error("ledger page size");auto pages=c.value.at("ledger").at("max_bytes").get<std::uint64_t>()/sqlite3_column_int(p.p,0);exec("PRAGMA max_page_count="+std::to_string(pages));
    }catch(...){sqlite3_close(db_);db_=nullptr;throw;}
}
Budget::~Budget(){if(db_)sqlite3_close(db_);}
FileGrant Budget::reserve(const std::string& id,const std::string& path,const std::string& day,std::uint64_t start,const std::string& session){
    const auto& b=config_.value.at("budget");const auto& q=config_.value.at("quicklook");
    const auto cap=config_.value.at("storage").at("max_file_bytes").get<std::uint64_t>();const auto png=q.at("enabled").get<bool>()?q.at("max_png_bytes").get<std::uint64_t>():0;
    exec("BEGIN IMMEDIATE");try{
        Statement row(db_,"SELECT hdf5_committed,png_reserved,png_committed,aux_committed FROM quota_day WHERE day=?");row.text(1,day);auto rc=row.step();std::uint64_t h=0,p=0,a=0;
        if(rc==SQLITE_ROW){h=sqlite3_column_int64(row.p,0);p=sqlite3_column_int64(row.p,1)+sqlite3_column_int64(row.p,2);a=sqlite3_column_int64(row.p,3);}
        else if(rc==SQLITE_DONE){Statement hi(db_,"SELECT value FROM meta WHERE key='day_highwater'");if(hi.step()==SQLITE_ROW&&day<reinterpret_cast<const char*>(sqlite3_column_text(hi.p,0)))throw std::runtime_error("PAUSED_QUOTA: date moved backwards");
            a=2*config_.value.at("ledger").at("max_bytes").get<std::uint64_t>()+b.at("metadata_headroom_bytes").get<std::uint64_t>();
            Statement ins(db_,"INSERT INTO quota_day(day,aux_committed) VALUES(?,?)");ins.text(1,day);ins.number(2,a);if(ins.step()!=SQLITE_DONE)throw std::runtime_error(sqlite3_errmsg(db_));
            Statement high(db_,"INSERT INTO meta VALUES('day_highwater',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value");high.text(1,day);if(high.step()!=SQLITE_DONE)throw std::runtime_error(sqlite3_errmsg(db_));
        }else throw std::runtime_error(sqlite3_errmsg(db_));
        auto limit=b.at("daily_limit_bytes").get<std::uint64_t>();
        if(h+cap>std::uint64_t(limit*b.at("hdf5_share").get<double>())||p+png>std::uint64_t(limit*b.at("png_share").get<double>())||h+p+a+cap+png>limit)throw std::runtime_error("PAUSED_QUOTA: conservative cap exhausted");
        Statement add(db_,"UPDATE quota_day SET hdf5_committed=hdf5_committed+?,png_reserved=png_reserved+? WHERE day=?");add.number(1,cap);add.number(2,png);add.text(3,day);if(add.step()!=SQLITE_DONE)throw std::runtime_error(sqlite3_errmsg(db_));
        Statement artifact(db_,"INSERT INTO artifact(id,path,day,start_utc_us,created_utc_us,cap,png_cap,state,kind,quicklook_enabled,config_hash,session) VALUES(?,?,?,?,?,?,?,'WRITING','h5',?,?,?)");artifact.text(1,id);artifact.text(2,path);artifact.text(3,day);artifact.number(4,start);artifact.number(5,utc());artifact.number(6,cap);artifact.number(7,png);artifact.number(8,png>0);artifact.text(9,config_.hash);artifact.text(10,session);if(artifact.step()!=SQLITE_DONE)throw std::runtime_error(sqlite3_errmsg(db_));
        exec("COMMIT");return {id,path,day,cap,png};
    }catch(...){exec("ROLLBACK");throw;}
}
void Budget::ready(const FileGrant& f,std::uint64_t bytes){if(bytes>f.cap)throw std::runtime_error("file exceeds reserved cap");Statement s(db_,"UPDATE artifact SET state='READY',size=? WHERE id=? AND state='WRITING'");s.number(1,bytes);s.text(2,f.id);if(s.step()!=SQLITE_DONE||sqlite3_changes(db_)!=1)throw std::runtime_error("finalize transition failed");}
void Budget::quarantine(const FileGrant& f){Statement s(db_,"UPDATE artifact SET state='QUARANTINED',generation=generation+1 WHERE id=? AND state='WRITING'");s.text(1,f.id);if(s.step()!=SQLITE_DONE)throw std::runtime_error("quarantine transition failed");}
nlohmann::json Budget::snapshot(const std::string& day){
    Statement s(db_,"SELECT hdf5_committed,png_reserved,png_committed,aux_committed FROM quota_day WHERE day=?");s.text(1,day);
    nlohmann::json j={{"day",day}};if(s.step()==SQLITE_ROW){j["hdf5_committed"]=sqlite3_column_int64(s.p,0);j["png_reserved"]=sqlite3_column_int64(s.p,1);j["png_committed"]=sqlite3_column_int64(s.p,2);j["aux_committed"]=sqlite3_column_int64(s.p,3);}return j;
}

}
