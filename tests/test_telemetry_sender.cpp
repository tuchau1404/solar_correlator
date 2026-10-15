#include <atomic>
#include <cassert>
#include <cstdlib>
#include <new>
#include "telemetry_sender.hpp"
static std::atomic<unsigned> allocations{0};
void* operator new(std::size_t n) { ++allocations; if (void* p = std::malloc(n)) return p; throw std::bad_alloc(); }
void operator delete(void* p) noexcept { std::free(p); }
void operator delete(void* p, std::size_t) noexcept { std::free(p); }
static int injected = 0;
static unsigned calls = 0;
extern "C" ssize_t __wrap_sendto(int, const void* data, size_t size, int flags, const sockaddr*, socklen_t) {
    ++calls;
    assert(size == 1076 && (flags & MSG_DONTWAIT));
    const auto* p = static_cast<const unsigned char*>(data);
    assert(std::memcmp(p, "SOL2", 4) == 0);
    assert(p[4] == 0 && p[5] == 2 && p[7] == 52);
    assert(p[52] == 0x3f && p[53] == 0x80 && p[54] == 0 && p[55] == 0);
    if (injected == -1) return 1;
    if (injected) { errno = injected; return -1; }
    return static_cast<ssize_t>(size);
}
int main() {
    TelemetrySender sender("127.0.0.1", 9999);
    std::vector<float> spectrum(2048, 1.0f);
    unsigned before = allocations.load();
    assert(sender.send_spectrum(1, 2, spectrum).queued);
    assert(calls == 8 && allocations == before);
    for (int error : {EAGAIN, ENOBUFS, EMSGSIZE, EINTR, EIO, -1}) {
        injected = error; calls = 0;
        auto r = sender.send_spectrum(2, 3, spectrum);
        assert(!r.queued && r.datagrams == 0 && calls == 1);
        assert(r.error == (error == -1 ? EIO : error));
    }
    spectrum[0] = std::numeric_limits<float>::quiet_NaN(); calls = 0;
    assert(!sender.send_spectrum(3, 4, spectrum).queued && calls == 0);
    assert(allocations == before);
}
