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
#include "config.hpp"

#pragma pack(push, 1)
struct ComplexSample {
    int16_t i;
    int16_t q;
};
#pragma pack(pop)

struct alignas(64) RingBufferControlBlock {
    alignas(64) std::atomic<uint32_t> head{0};
    alignas(64) std::atomic<uint32_t> tail{0};
    alignas(64) std::atomic<uint32_t> overflow_cnt{0};
    alignas(64) std::atomic<bool> is_ready{false};
    alignas(64) std::atomic<bool> start_stream{false};
    uint32_t capacity{0};
    uint32_t mask{0};
};

class SharedMemoryRingBuffer {
public:
    SharedMemoryRingBuffer(const std::string& shm_name, uint32_t capacity, bool is_producer)
        : shm_name_(shm_name), capacity_(capacity), is_producer_(is_producer) {
        
        if ((capacity & (capacity - 1)) != 0) {
            throw std::invalid_argument("[SHM RingBuffer] Dung lượng bắt buộc phải là lũy thừa của 2!");
        }
        mask_ = capacity - 1;
        total_shm_size_ = sizeof(RingBufferControlBlock) + (static_cast<size_t>(capacity_) * sizeof(ComplexSample));

        if (is_producer_) {
            shm_fd_ = shm_open(shm_name_.c_str(), O_CREAT | O_RDWR | O_TRUNC, 0666);
            if (shm_fd_ < 0) {
                throw std::runtime_error("[SHM RingBuffer] Lỗi shm_open Producer: " + shm_name_);
            }
            if (ftruncate(shm_fd_, total_shm_size_) != 0) {
                close(shm_fd_);
                throw std::runtime_error("[SHM RingBuffer] Lỗi ftruncate: " + shm_name_);
            }
        } else {
            // Consumer: Thử kết nối lại nhiều lần chờ Producer tạo xong file SHM
            int retry_count = 50;
            while (retry_count > 0) {
                shm_fd_ = shm_open(shm_name_.c_str(), O_RDWR, 0666);
                if (shm_fd_ >= 0) {
                    break;
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(50));
                retry_count--;
            }

            if (shm_fd_ < 0) {
                throw std::runtime_error("[SHM RingBuffer] Lỗi shm_open Consumer: " + shm_name_ + " (Hết thời gian chờ Producer)");
            }
        }

        void* mapped_ptr = mmap(nullptr, total_shm_size_, PROT_READ | PROT_WRITE, MAP_SHARED, shm_fd_, 0);
        if (mapped_ptr == MAP_FAILED) {
            close(shm_fd_);
            throw std::runtime_error("[SHM RingBuffer] Lỗi mmap: " + shm_name_);
        }

        ctrl_ = reinterpret_cast<RingBufferControlBlock*>(mapped_ptr);
        data_buffer_ = reinterpret_cast<ComplexSample*>(
            reinterpret_cast<uint8_t*>(mapped_ptr) + sizeof(RingBufferControlBlock)
        );

        if (is_producer_) {
            ctrl_->head.store(0, std::memory_order_relaxed);
            ctrl_->tail.store(0, std::memory_order_relaxed);
            ctrl_->overflow_cnt.store(0, std::memory_order_relaxed);
            ctrl_->is_ready.store(false, std::memory_order_relaxed);
            ctrl_->start_stream.store(false, std::memory_order_relaxed);
            ctrl_->capacity = capacity_;
            ctrl_->mask = mask_;
        }
    }

    ~SharedMemoryRingBuffer() {
        if (ctrl_ != nullptr) {
            munmap(ctrl_, total_shm_size_);
            ctrl_ = nullptr;
        }
        if (shm_fd_ >= 0) {
            close(shm_fd_);
            shm_fd_ = -1;
        }
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

        const uint32_t free_slots = capacity_ - (current_head - current_tail);
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
        const uint32_t to_read = std::min(num_samples, available);
        if (to_read == 0) return 0;

        for (uint32_t n = 0; n < to_read; ++n) {
            const uint32_t idx = (current_tail + n) & mask_;
            dest[n] = data_buffer_[idx];
        }

        ctrl_->tail.store(current_tail + to_read, std::memory_order_release);
        return to_read;
    }

    uint32_t peek_from_tail(ComplexSample* dest, uint32_t num_samples) const {
        const uint32_t current_tail = ctrl_->tail.load(std::memory_order_relaxed);
        const uint32_t current_head = ctrl_->head.load(std::memory_order_acquire);

        const uint32_t available = current_head - current_tail;
        if (available < num_samples) {
            return 0;
        }

        for (uint32_t n = 0; n < num_samples; ++n) {
            const uint32_t idx = (current_tail + n) & mask_;
            dest[n] = data_buffer_[idx];
        }
        return num_samples;
    }

    uint32_t skip(uint32_t num_samples) {
        const uint32_t current_tail = ctrl_->tail.load(std::memory_order_relaxed);
        const uint32_t current_head = ctrl_->head.load(std::memory_order_acquire);

        const uint32_t available = current_head - current_tail;
        const uint32_t to_skip = std::min(num_samples, available);

        ctrl_->tail.store(current_tail + to_skip, std::memory_order_release);
        return to_skip;
    }

    void flush() {
        const uint32_t current_head = ctrl_->head.load(std::memory_order_acquire);
        ctrl_->tail.store(current_head, std::memory_order_release);
    }

    uint32_t available_read() const {
        const uint32_t current_head = ctrl_->head.load(std::memory_order_acquire);
        const uint32_t current_tail = ctrl_->tail.load(std::memory_order_relaxed);
        return (current_head - current_tail);
    }

    uint32_t get_overflow_count() const {
        return ctrl_->overflow_cnt.load(std::memory_order_relaxed);
    }

private:
    std::string shm_name_;
    uint32_t capacity_{0};
    uint32_t mask_{0};
    size_t total_shm_size_{0};
    bool is_producer_{false};
    int shm_fd_{-1};
    RingBufferControlBlock* ctrl_{nullptr};
    ComplexSample* data_buffer_{nullptr};
};