#include "hdf5_writer.hpp"
#include "bounded_hdf5_vfd.hpp"
#include <stdexcept>
namespace solar::storage {
namespace {
void ok(herr_t rc,const char* msg){if(rc<0)throw std::runtime_error(msg);}
hid_t id(hid_t rc,const char* msg){if(rc<0)throw std::runtime_error(msg);return rc;}
struct Handle{hid_t value;herr_t(*closer)(hid_t);~Handle(){if(value>=0)closer(value);}operator hid_t()const{return value;}};
void attr(hid_t group,const std::string& key,const nlohmann::json& value){
    Handle space{id(H5Screate(H5S_SCALAR),"attribute space"),H5Sclose};
    if(value.is_number_unsigned()||value.is_number_integer()){
        auto v=value.get<std::int64_t>();Handle a{id(H5Acreate2(group,key.c_str(),H5T_STD_I64LE,space,H5P_DEFAULT,H5P_DEFAULT),"integer attribute"),H5Aclose};ok(H5Awrite(a,H5T_NATIVE_INT64,&v),"attribute write");
    }else if(value.is_number_float()){
        auto v=value.get<double>();Handle a{id(H5Acreate2(group,key.c_str(),H5T_IEEE_F64LE,space,H5P_DEFAULT,H5P_DEFAULT),"float attribute"),H5Aclose};ok(H5Awrite(a,H5T_NATIVE_DOUBLE,&v),"attribute write");
    }else{
        auto text=value.is_string()?value.get<std::string>():value.dump();Handle type{id(H5Tcopy(H5T_C_S1),"string type"),H5Tclose};ok(H5Tset_size(type,text.size()+1),"string length");
        Handle a{id(H5Acreate2(group,key.c_str(),type,space,H5P_DEFAULT,H5P_DEFAULT),"string attribute"),H5Aclose};ok(H5Awrite(a,type,text.c_str()),"attribute write");
    }
}
void vector_dataset(hid_t file,const char* path,hid_t disk,hid_t memory,const void* values,hsize_t n){Handle s{id(H5Screate_simple(1,&n,nullptr),"vector space"),H5Sclose};Handle d{id(H5Dcreate2(file,path,disk,s,H5P_DEFAULT,H5P_DEFAULT,H5P_DEFAULT),"vector dataset"),H5Dclose};ok(H5Dwrite(d,memory,H5S_ALL,H5S_ALL,H5P_DEFAULT,values),"vector write");}
void put_row(hid_t ds,hid_t type,const void* value,hsize_t row,hsize_t bins,bool matrix){
    hsize_t dims[]={row+1,bins};ok(H5Dset_extent(ds,dims),"extend dataset");Handle space{id(H5Dget_space(ds),"dataset space"),H5Sclose};hsize_t start[]={row,0},count[]={1,bins};
    ok(H5Sselect_hyperslab(space,H5S_SELECT_SET,start,nullptr,count,nullptr),"select row");Handle mem{id(H5Screate_simple(matrix?2:1,count,nullptr),"row memory"),H5Sclose};ok(H5Dwrite(ds,type,mem,space,H5P_DEFAULT,value),"write row");
}
}
Hdf5Writer::Hdf5Writer(const std::string& path,std::uint64_t cap,const nlohmann::json& metadata,const std::vector<double>& frequency,const std::vector<float>& phi,std::size_t chunk,const std::string& compression,unsigned level):bins_(frequency.size()){
    if(!bins_||!chunk)throw std::invalid_argument("empty HDF5 axes/chunks");
    try{
        Handle fapl{bounded_hdf5_fapl(cap),H5Pclose};file_=id(H5Fcreate(path.c_str(),H5F_ACC_EXCL,H5P_DEFAULT,fapl),"create capped HDF5 temp");
        Handle group{id(H5Gcreate2(file_,"/metadata",H5P_DEFAULT,H5P_DEFAULT,H5P_DEFAULT),"metadata group"),H5Gclose};
        for(auto it=metadata.begin();it!=metadata.end();++it)attr(group,it.key(),it.value());
        vector_dataset(file_,"/frequency_hz",H5T_IEEE_F64LE,H5T_NATIVE_DOUBLE,frequency.data(),frequency.size());
        vector_dataset(file_,"/calibration_phi_rad_unshifted",H5T_IEEE_F32LE,H5T_NATIVE_FLOAT,phi.data(),phi.size());
        Handle data{id(H5Gcreate2(file_,"/data",H5P_DEFAULT,H5P_DEFAULT,H5P_DEFAULT),"data group"),H5Gclose};
        auto create=[&](const std::string& name,hid_t type,bool matrix){
            hsize_t dims[]={0,bins_},max[]={H5S_UNLIMITED,bins_},chunks[]={chunk,bins_};
            Handle space{id(H5Screate_simple(matrix?2:1,dims,max),"extendable space"),H5Sclose};Handle dcpl{id(H5Pcreate(H5P_DATASET_CREATE),"dataset properties"),H5Pclose};
            ok(H5Pset_chunk(dcpl,matrix?2:1,chunks),"chunk configuration");
            if(compression=="gzip")ok(H5Pset_deflate(dcpl,level),"gzip unavailable/configuration error");else if(compression!="none")throw std::invalid_argument("unsupported compression");
            datasets_[name]=id(H5Dcreate2(data,name.c_str(),type,space,H5P_DEFAULT,dcpl,H5P_DEFAULT),"create data field");
        };
        for(const char* n:{"s11_linear","s22_linear","s12_real","s12_imag"})create(n,H5T_IEEE_F32LE,true);
        for(const char* n:{"start_utc_us","end_utc_us","timestamps_us","frame_ids_first","frame_ids_last","valid_fft_frames","sample_start","sample_end","backend_timestamp_first_us","backend_timestamp_last_us"})create(n,H5T_STD_U64LE,false);
        for(const char* n:{"integration_duration_sec","coverage_sec","cadence_sec"})create(n,H5T_IEEE_F64LE,false);
        create("quality_flags",H5T_STD_U32LE,false);
    }catch(...){failed_=true;cleanup();throw;}
}
void Hdf5Writer::append(const std::vector<OutputRecord>& records){
    if(file_<0||failed_)throw std::logic_error("writer not writable");
    try{for(const auto& r:records){
        for(const auto& v:r.values)if(v.size()!=bins_)throw std::invalid_argument("HDF5 record shape");
        const char* arrays[]={"s11_linear","s22_linear","s12_real","s12_imag"};for(std::size_t k=0;k<COMPONENTS;++k)put_row(datasets_.at(arrays[k]),H5T_NATIVE_FLOAT,r.values[k].data(),rows_,bins_,true);
        auto u=[&](const char* name,std::uint64_t v){put_row(datasets_.at(name),H5T_NATIVE_UINT64,&v,rows_,bins_,false);};
        auto d=[&](const char* name,double v){put_row(datasets_.at(name),H5T_NATIVE_DOUBLE,&v,rows_,bins_,false);};
        u("start_utc_us",r.start_utc_us);u("end_utc_us",r.end_utc_us);u("timestamps_us",r.timestamp_us);u("frame_ids_first",r.frame_first);u("frame_ids_last",r.frame_last);u("valid_fft_frames",r.valid_fft_frames);u("sample_start",r.sample_start);u("sample_end",r.sample_end);u("backend_timestamp_first_us",r.backend_timestamp_first_us);u("backend_timestamp_last_us",r.backend_timestamp_last_us);
        d("integration_duration_sec",r.span_sec);d("coverage_sec",r.covered_sec);d("cadence_sec",r.cadence_sec);put_row(datasets_.at("quality_flags"),H5T_NATIVE_UINT32,&r.quality,rows_,bins_,false);++rows_;
    }}catch(...){failed_=true;throw;}
}
void Hdf5Writer::flush(){if(file_<0||failed_)throw std::logic_error("writer failed/closed");try{ok(H5Fflush(file_,H5F_SCOPE_GLOBAL),"HDF5 flush");}catch(...){failed_=true;throw;}}
void Hdf5Writer::close(){
    if(file_<0)return;
    if(failed_)throw std::logic_error("failed HDF5 cannot finalize");
    flush();bool error=false;
    for(auto& kv:datasets_){if(H5Dclose(kv.second)<0)error=true;kv.second=-1;}
    datasets_.clear();auto f=file_;file_=-1;if(H5Fclose(f)<0)error=true;
    if(error){failed_=true;throw std::runtime_error("HDF5 close failed; quarantine required");}
}
void Hdf5Writer::cleanup() noexcept{for(auto& kv:datasets_)if(kv.second>=0)H5Dclose(kv.second);datasets_.clear();if(file_>=0)H5Fclose(file_);file_=-1;}
Hdf5Writer::~Hdf5Writer(){cleanup();}
}
