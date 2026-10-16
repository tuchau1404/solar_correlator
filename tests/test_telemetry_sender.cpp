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
static unsigned version = 2, payload_type = 0;
static std::uint64_t shared_session = 0;
static std::uint64_t read64(const unsigned char* p) {
    std::uint64_t value = 0;
    for (unsigned i = 0; i < 8; ++i) value = (value << 8) | p[i];
    return value;
}
extern "C" ssize_t __wrap_sendto(int, const void* data, size_t size, int flags, const sockaddr*, socklen_t) {
    ++calls;
    assert(size == 1076 && (flags & MSG_DONTWAIT));
    const auto* p = static_cast<const unsigned char*>(data);
    assert(std::memcmp(p, "SOL2", 4) == 0);
    assert(p[4] == 0 && p[5] == version && p[7] == 52);
    assert(p[52] == 0x3f && p[53] == 0x80 && p[54] == 0 && p[55] == 0);
    assert(p[36] == 0 && p[37] == 0 && p[38] == payload_type && p[39] == 1);
    if (version == 3) {
        assert(read64(p + 8) == shared_session);
        assert(read64(p + 16) == 42 && read64(p + 24) == 1700000000000000ULL);
        assert(p[32] == 0 && p[33] == 0 && p[34] == 7 && p[35] == 161);
    }
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
    assert(sender.stats().would_block == 1 && sender.stats().no_buffers == 1);
    assert(sender.stats().too_large == 1 && sender.stats().interrupted == 1);
    assert(sender.stats().other == 2);
    spectrum[0] = std::numeric_limits<float>::quiet_NaN(); calls = 0;
    assert(!sender.send_spectrum(3, 4, spectrum).queued && calls == 0);
    assert(allocations == before);
    injected = 0; version = 3;
    shared_session = TelemetrySender::new_session();
    assert(shared_session && TelemetrySender::new_session() != shared_session);
    spectrum[0] = 1;
    for (payload_type = 1; payload_type <= 5; ++payload_type) {
        TelemetrySender typed("127.0.0.1", 9999, shared_session,
                              static_cast<TelemetrySender::Payload>(payload_type));
        const auto checkpoint = allocations.load();
        calls = 0;
        assert(typed.send_spectrum(42, 1700000000000000ULL, spectrum).queued);
        assert(calls == 8 && allocations == checkpoint);
    }
}
