#include "bounded_hdf5_vfd.hpp"
#if H5_VERSION_GE(1,14,0)
#include <H5FDdevelop.h>
#else
#include <H5FDpublic.h>
#endif
#include <algorithm>
#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <limits>
#include <new>
#include <stdexcept>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>
namespace solar::storage {
namespace {
struct File { H5FD_t base{}; int fd=-1; haddr_t limit=0,eoa=0,eof=0; dev_t dev{};ino_t ino{}; };
File* file(H5FD_t* f){return reinterpret_cast<File*>(f);}
const File* file(const H5FD_t* f){return reinterpret_cast<const File*>(f);}
bool within(const File* f,haddr_t addr,std::size_t n){return addr<=f->limit && n<=f->limit-addr;}
H5FD_t* open_file(const char* name,unsigned flags,hid_t fapl,haddr_t) {
    auto* cfg=static_cast<const std::uint64_t*>(H5Pget_driver_info(fapl));
    if(!cfg || !*cfg || *cfg>std::uint64_t(std::numeric_limits<off_t>::max()))return nullptr;
    auto* f=new(std::nothrow) File;if(!f)return nullptr;f->limit=*cfg;
    int of=O_CLOEXEC|O_NOFOLLOW|((flags&H5F_ACC_RDWR)?O_RDWR:O_RDONLY);
    if(flags&H5F_ACC_CREAT)of|=O_CREAT;
    if(flags&H5F_ACC_EXCL)of|=O_EXCL;
    if(flags&H5F_ACC_TRUNC)of|=O_TRUNC;
    f->fd=::open(name,of,0600);
    struct stat st{};
    if(f->fd<0 || fstat(f->fd,&st)!=0 || !S_ISREG(st.st_mode) || st.st_size<0 || std::uint64_t(st.st_size)>*cfg){if(f->fd>=0)::close(f->fd);delete f;return nullptr;}
    f->eof=st.st_size;f->dev=st.st_dev;f->ino=st.st_ino;return &f->base;
}
herr_t close_file(H5FD_t* p){auto* f=file(p);int rc=::close(f->fd);delete f;return rc<0?-1:0;}
int compare(const H5FD_t* a,const H5FD_t* b){auto* x=file(a);auto* y=file(b);if(x->dev!=y->dev)return x->dev<y->dev?-1:1;return x->ino==y->ino?0:(x->ino<y->ino?-1:1);}
herr_t query(const H5FD_t*,unsigned long* flags){*flags=H5FD_FEAT_AGGREGATE_METADATA|H5FD_FEAT_ACCUMULATE_METADATA|H5FD_FEAT_DATA_SIEVE|H5FD_FEAT_AGGREGATE_SMALLDATA|H5FD_FEAT_DEFAULT_VFD_COMPATIBLE;return 0;}
haddr_t get_eoa(const H5FD_t* p,H5FD_mem_t){return file(p)->eoa;}
herr_t set_eoa(H5FD_t* p,H5FD_mem_t,haddr_t a){auto* f=file(p);if(a>f->limit){errno=ENOSPC;return -1;}f->eoa=a;return 0;}
haddr_t get_eof(const H5FD_t* p,H5FD_mem_t){return std::max(file(p)->eof,file(p)->eoa);}
herr_t read_file(H5FD_t* p,H5FD_mem_t,hid_t,haddr_t a,size_t n,void* v){auto* f=file(p);if(!within(f,a,n))return -1;auto* b=static_cast<unsigned char*>(v);while(n){auto r=pread(f->fd,b,n,static_cast<off_t>(a));if(r<0&&errno==EINTR)continue;if(r<0)return -1;if(!r){std::memset(b,0,n);return 0;}b+=r;a+=r;n-=r;}return 0;}
herr_t write_file(H5FD_t* p,H5FD_mem_t,hid_t,haddr_t a,size_t n,const void* v){auto* f=file(p);if(!within(f,a,n)){errno=ENOSPC;return -1;}auto* b=static_cast<const unsigned char*>(v);while(n){auto r=pwrite(f->fd,b,n,static_cast<off_t>(a));if(r<0&&errno==EINTR)continue;if(r<=0)return -1;b+=r;a+=r;n-=r;f->eof=std::max(f->eof,a);}return 0;}
herr_t flush_file(H5FD_t* p,hid_t,hbool_t){return fsync(file(p)->fd)<0?-1:0;}
herr_t truncate_file(H5FD_t* p,hid_t,hbool_t){auto* f=file(p);if(f->eoa>f->limit)return -1;if(ftruncate(f->fd,static_cast<off_t>(f->eoa))<0)return -1;f->eof=f->eoa;return 0;}
herr_t lock_file(H5FD_t* p,hbool_t rw){return flock(file(p)->fd,(rw?LOCK_EX:LOCK_SH)|LOCK_NB)<0?-1:0;}
herr_t unlock_file(H5FD_t* p){return flock(file(p)->fd,LOCK_UN)<0?-1:0;}
}
hid_t bounded_hdf5_fapl(std::uint64_t bytes){
    // Called exclusively on the HDF5 owner thread; registration is process-local.
    static hid_t driver=-1;
    if(driver<0){H5FD_class_t c{};
#if H5_VERSION_GE(1,14,0)
        c.version=H5FD_CLASS_VERSION;c.value=static_cast<H5FD_class_value_t>(513);
#endif
        c.name="solar_bounded";c.maxaddr=std::numeric_limits<off_t>::max();c.fc_degree=H5F_CLOSE_STRONG;
        c.fapl_size=sizeof(std::uint64_t);c.open=open_file;c.close=close_file;c.cmp=compare;c.query=query;
        c.get_eoa=get_eoa;c.set_eoa=set_eoa;c.get_eof=get_eof;c.read=read_file;c.write=write_file;
        c.flush=flush_file;c.truncate=truncate_file;c.lock=lock_file;c.unlock=unlock_file;
        for(auto& m:c.fl_map)m=H5FD_MEM_DEFAULT;
        driver=H5FDregister(&c);if(driver<0)throw std::runtime_error("register bounded HDF5 driver");}
    hid_t p=H5Pcreate(H5P_FILE_ACCESS);
    if(p<0)throw std::runtime_error("HDF5 fapl");
    if(H5Pset_driver(p,driver,&bytes)<0){H5Pclose(p);throw std::runtime_error("configure bounded HDF5 driver");}
    return p;
}
}
