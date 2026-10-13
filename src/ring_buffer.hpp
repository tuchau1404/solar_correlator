#pragma once

#include <cstdint>
#include <string>
#include <atomic>
#include <cstring>
#include <algorithm>
#include <stdexcept>
#include <chrono>
#include <thread>
#include <sys/mman.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
#include <cstddef>
#include <limits>
#include <new>
#include <type_traits>
#include <cerrno>
#include <sys/file.h>

struct ComplexSample {
    int16_t i;
    int16_t q;
};

struct alignas(64) RingBufferControlBlock {
    alignas(64) std::atomic<uint32_t> head{0};
    alignas(64) std::atomic<uint32_t> tail{0};
    alignas(64) std::atomic<uint32_t> overflow_cnt{0};
    alignas(64) std::atomic<bool> is_ready{false};
    alignas(64) std::atomic<bool> start_stream{false};
    alignas(64) uint32_t magic{0};
    uint32_t abi_version{1};
    uint32_t capacity{0};
    uint32_t mask{0};
};

static_assert(sizeof(ComplexSample) == 4);
static_assert(std::is_trivially_copyable_v<ComplexSample>);
static_assert(std::is_trivially_copyable_v<RingBufferControlBlock>);
static_assert(std::is_standard_layout_v<RingBufferControlBlock>);
static_assert(std::atomic<uint32_t>::is_always_lock_free);
static_assert(std::atomic<bool>::is_always_lock_free);
static_assert(offsetof(RingBufferControlBlock, head) == 0);
static_assert(offsetof(RingBufferControlBlock, tail) == 64);
static_assert(offsetof(RingBufferControlBlock, overflow_cnt) == 128);
static_assert(offsetof(RingBufferControlBlock, is_ready) == 192);
static_assert(offsetof(RingBufferControlBlock, start_stream) == 256);
static_assert(offsetof(RingBufferControlBlock, magic) == 320);
static_assert(sizeof(RingBufferControlBlock) == 384);

// Linux/GCC or Clang, same ABI in both processes, coherent normal MAP_SHARED
// memory, address-free hardware atomics. Not a portable ISO C++ IPC abstraction.
// Exactly ONE producer; ONE consumer owns ALL read/peek/skip/flush/available
// calls. No other thread may advance tail during a peek. Caller buffers must
// contain n elements and must not alias the mapping. No concurrent teardown.
// Create/attach may block; sample transfer methods contain no OS locks.

class SharedMemoryRingBuffer {
public:
    SharedMemoryRingBuffer(const std::string& shm_name, uint32_t capacity, bool is_producer)
        : shm_name_(shm_name), capacity_(capacity), is_producer_(is_producer) {
        
        if (capacity == 0 || (capacity & (capacity - 1)) != 0 ||
            capacity > (uint32_t{1} << 31)) {
            throw std::invalid_argument("capacity must be a power of two in [1, 2^31]");
        }
        if (capacity > (std::numeric_limits<size_t>::max() - sizeof(RingBufferControlBlock)) /
                           sizeof(ComplexSample))
            throw std::length_error("SHM size overflow");
        mask_ = capacity - 1;
        total_shm_size_ = sizeof(RingBufferControlBlock) + size_t(capacity) * sizeof(ComplexSample);
        if (total_shm_size_ > static_cast<uintmax_t>(std::numeric_limits<off_t>::max()))
            throw std::length_error("SHM size exceeds off_t");

        bool created = false;
        try {
            const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(5);
            if (is_producer_) {
                shm_fd_ = shm_open(shm_name_.c_str(), O_CREAT | O_EXCL | O_RDWR, 0600);
                if (shm_fd_ < 0) throw std::runtime_error("create SHM failed (existing object is not replaced)");
                created = true;
                lock_until(deadline);
                if (ftruncate(shm_fd_, static_cast<off_t>(total_shm_size_)) != 0)
                    throw std::runtime_error("ftruncate failed");
                map_storage();
                ctrl_ = ::new (static_cast<void*>(ctrl_)) RingBufferControlBlock{};
                // C++17 non-allocating placement array new, trivial sample type.
                // Value initialization also touches the payload before streaming.
                data_buffer_ = ::new (static_cast<void*>(data_buffer_)) ComplexSample[capacity_]{};
                ctrl_->capacity = capacity_;
                ctrl_->mask = mask_;
                // Commit marker: attach reads it through pread while holding flock,
                // before accessing any atomic object. is_ready is hardware readiness.
                ctrl_->magic = kMagic;
                if (flock(shm_fd_, LOCK_UN) != 0) throw std::runtime_error("unlock failed");
            } else {
                for (;;) {
                    if (shm_fd_ < 0) {
                        shm_fd_ = shm_open(shm_name_.c_str(), O_RDWR, 0600);
                        if (shm_fd_ < 0 && errno != ENOENT && errno != EINTR)
                            throw std::runtime_error("attach shm_open failed");
                    }
                    if (shm_fd_ >= 0) {
                        lock_until(deadline);
                        struct stat st{};
                        if (fstat(shm_fd_, &st) != 0) throw std::runtime_error("fstat failed");
                        if (st.st_size != 0 && st.st_size != static_cast<off_t>(total_shm_size_))
                            throw std::runtime_error("SHM size/ABI mismatch");
                        uint32_t magic = 0;
                        if (st.st_size != 0) {
                            const auto got = pread(shm_fd_, &magic, sizeof(magic),
                                                   offsetof(RingBufferControlBlock, magic));
                            if (got != sizeof(magic)) throw std::runtime_error("SHM header read failed");
                        }
                        if (magic == kMagic) {
                            map_storage();
                            if (ctrl_->abi_version != 1 || ctrl_->capacity != capacity_ || ctrl_->mask != mask_)
                                throw std::runtime_error("SHM metadata mismatch");
                            if (flock(shm_fd_, LOCK_UN) != 0) throw std::runtime_error("unlock failed");
                            break;
                        }
                        if (flock(shm_fd_, LOCK_UN) != 0) throw std::runtime_error("unlock failed");
                    }
                    if (std::chrono::steady_clock::now() >= deadline)
                        throw std::runtime_error("SHM initialization timeout");
                    std::this_thread::sleep_for(std::chrono::milliseconds(5));
                }
            }
        } catch (...) {
            release_mapping();
            if (created) shm_unlink(shm_name_.c_str());
            throw;
        }
    }

    SharedMemoryRingBuffer(const SharedMemoryRingBuffer&) = delete;
    SharedMemoryRingBuffer& operator=(const SharedMemoryRingBuffer&) = delete;
    SharedMemoryRingBuffer(SharedMemoryRingBuffer&&) = delete;
    SharedMemoryRingBuffer& operator=(SharedMemoryRingBuffer&&) = delete;

    ~SharedMemoryRingBuffer() {
        release_mapping();
    }

    static void unlink_shm(const std::string& name) {
        shm_unlink(name.c_str());
    }

    void set_ready(bool ready) {
        ctrl_->is_ready.store(ready, std::memory_order_release);
    }

    bool is_ready() const {
        return ctrl_->is_ready.load(std::memory_order_acquire);
    }

    void set_start_stream(bool start) {
        ctrl_->start_stream.store(start, std::memory_order_release);
    }

    bool is_stream_started() const {
        return ctrl_->start_stream.load(std::memory_order_acquire);
    }

    uint32_t write(const int16_t* xi, const int16_t* xq, uint32_t num_samples) {
        const uint32_t current_head = ctrl_->head.load(std::memory_order_relaxed);
        const uint32_t current_tail = ctrl_->tail.load(std::memory_order_acquire);

        if (num_samples == 0) return 0;
        if (!xi || !xq) throw std::invalid_argument("null input");
        const uint32_t used = current_head - current_tail;
        if (used > capacity_) throw std::runtime_error("SPSC ownership/invariant violated");
        const uint32_t free_slots = capacity_ - used;
        if (num_samples > free_slots) {
            ctrl_->overflow_cnt.fetch_add(1, std::memory_order_relaxed);
            return 0;
        }

        for (uint32_t n = 0; n < num_samples; ++n) {
            const uint32_t idx = (current_head + n) & mask_;
            data_buffer_[idx].i = xi[n];
            data_buffer_[idx].q = xq[n];
        }

        ctrl_->head.store(current_head + num_samples, std::memory_order_release);
        return num_samples;
    }

    uint32_t read(ComplexSample* dest, uint32_t num_samples) {
        const uint32_t current_tail = ctrl_->tail.load(std::memory_order_relaxed);
        const uint32_t current_head = ctrl_->head.load(std::memory_order_acquire);

        const uint32_t available = current_head - current_tail;
        if (available > capacity_) throw std::runtime_error("SPSC ownership/invariant violated");
        const uint32_t to_read = std::min(num_samples, available);
        if (to_read == 0) return 0;

        copy_from_ring(dest, current_tail, to_read);

        ctrl_->tail.store(current_tail + to_read, std::memory_order_release);
        return to_read;
    }

    uint32_t peek_from_tail(ComplexSample* dest, uint32_t num_samples) const {
        const uint32_t current_tail = ctrl_->tail.load(std::memory_order_relaxed);
        const uint32_t current_head = ctrl_->head.load(std::memory_order_acquire);

        const uint32_t available = current_head - current_tail;
        if (available > capacity_) throw std::runtime_error("SPSC ownership/invariant violated");
        if (available < num_samples) {
            return 0;
        }

        copy_from_ring(dest, current_tail, num_samples);
        return num_samples;
    }

    uint32_t skip(uint32_t num_samples) {
        const uint32_t current_tail = ctrl_->tail.load(std::memory_order_relaxed);
        const uint32_t current_head = ctrl_->head.load(std::memory_order_acquire);

        const uint32_t available = current_head - current_tail;
        if (available > capacity_) throw std::runtime_error("SPSC ownership/invariant violated");
        const uint32_t to_skip = std::min(num_samples, available);

        ctrl_->tail.store(current_tail + to_skip, std::memory_order_release);
        return to_skip;
    }

    // Consumer-only discard of committed samples, NOT a hardware T0 barrier.
    void flush() {
        const uint32_t current_head = ctrl_->head.load(std::memory_order_acquire);
        ctrl_->tail.store(current_head, std::memory_order_release);
    }

    uint32_t available_read() const {
        const uint32_t current_head = ctrl_->head.load(std::memory_order_acquire);
        const uint32_t current_tail = ctrl_->tail.load(std::memory_order_relaxed);
        const uint32_t available = current_head - current_tail;
        if (available > capacity_) throw std::runtime_error("SPSC ownership/invariant violated");
        return available;
    }

    uint32_t get_overflow_count() const {
        return ctrl_->overflow_cnt.load(std::memory_order_relaxed);
    }

private:
    static constexpr uint32_t kMagic = 0x53425231;

    void lock_until(std::chrono::steady_clock::time_point deadline) {
        while (flock(shm_fd_, LOCK_EX | LOCK_NB) != 0) {
            if (errno != EWOULDBLOCK && errno != EINTR)
                throw std::runtime_error("SHM bootstrap lock failed");
            if (std::chrono::steady_clock::now() >= deadline)
                throw std::runtime_error("SHM bootstrap lock timeout");
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
    }

    void map_storage() {
        void* p = mmap(nullptr, total_shm_size_, PROT_READ | PROT_WRITE, MAP_SHARED, shm_fd_, 0);
        if (p == MAP_FAILED) throw std::runtime_error("mmap failed");
        ctrl_ = static_cast<RingBufferControlBlock*>(p);
        data_buffer_ = reinterpret_cast<ComplexSample*>(
            static_cast<unsigned char*>(p) + sizeof(RingBufferControlBlock));
    }

    void release_mapping() noexcept {
        if (ctrl_) { munmap(ctrl_, total_shm_size_); ctrl_ = nullptr; }
        data_buffer_ = nullptr;
        if (shm_fd_ >= 0) { close(shm_fd_); shm_fd_ = -1; }
    }

    void copy_from_ring(ComplexSample* dest, uint32_t tail, uint32_t n) const {
        if (n == 0) return;
        if (!dest) throw std::invalid_argument("null destination");
        const uint32_t index = tail & mask_;
        const uint32_t first = std::min(n, capacity_ - index);
        std::memcpy(dest, data_buffer_ + index, size_t(first) * sizeof(ComplexSample));
        if (n != first)
            std::memcpy(dest + first, data_buffer_, size_t(n - first) * sizeof(ComplexSample));
    }

    std::string shm_name_;
    uint32_t capacity_{0};
    uint32_t mask_{0};
    size_t total_shm_size_{0};
    bool is_producer_{false};
    int shm_fd_{-1};
    RingBufferControlBlock* ctrl_{nullptr};
    ComplexSample* data_buffer_{nullptr};
};
