#include "startup_alignment.hpp"
#include <sstream>
#include <vector>
#include <iostream>
#include <algorithm>
struct Ring {
    unsigned tail=0,head=10000,flushes=0; bool short_skip=false;
    void flush(){tail=head;++flushes;}
    unsigned skip(unsigned n){if(short_skip)return 0;tail+=n;return n;}
};
struct Result { bool is_valid; float pnr_db; int lag_samples; };
static void require(bool v){if(!v)throw std::runtime_error("alignment assertion failed");}
int main(){
    for(int lag:{-17,0,19}){
        Ring a,b;StartupAlignment::Options o;o.snapshot_size=128;o.max_residual_samples=0;
        unsigned calls=0;std::vector<unsigned> tails;std::ostringstream log;
        StartupAlignment::acquire(a,b,o,[]{},[&]{tails.push_back(a.tail);return Result{true,40,calls++?0:lag};},log);
        require(calls==4 && a.flushes==1 && b.flushes==1);
        require(int(a.tail)-int(b.tail)==lag);
        for(unsigned i=1;i<tails.size();++i)require(tails[i]>=tails[i-1]+128);
        const auto delta=int(a.tail)-int(b.tail);a.skip(500);b.skip(500);require(int(a.tail)-int(b.tail)==delta);
    }
    // A one-sample residual remains valid in the unchanged test default, but
    // must never be labelled exact zero by main.
    for(unsigned tolerance:{0u,1u}){
        Ring a,b;StartupAlignment::Options o;o.snapshot_size=128;o.attempts=1;o.max_residual_samples=tolerance;
        unsigned calls=0;bool failed=false;std::ostringstream log;
        try{StartupAlignment::acquire(a,b,o,[]{},[&]{return Result{true,40,calls++?1:0};},log);}
        catch(const StartupAlignment::Failed&){failed=true;}
        require(failed==(tolerance==0));
    }
    for(int mode:{0,1,2,3}){
        Ring a,b;StartupAlignment::Options o;o.snapshot_size=128;o.attempts=2;
        unsigned calls=0;bool failed=false;std::ostringstream log;
        if(mode==1)a.short_skip=true;
        try{StartupAlignment::acquire(a,b,o,[&]{if(mode>=2)throw std::runtime_error(mode==2?"overflow":"timeout");},
            [&]{++calls;return Result{true,mode==0?5.f:40.f,1};},log);}
        catch(const std::runtime_error&){failed=true;}
        require(failed);if(mode==0)require(calls==2 && a.tail==b.tail);if(mode>=2)require(calls==0);
    }
    std::cout<<"PASS startup sign, fresh confirmations, exact-zero policy, weak retry, short skip, overflow/timeout propagation, paired handoff\n";
}
