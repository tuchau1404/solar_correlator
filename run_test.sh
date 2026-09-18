#!/bin/bash
set -e

echo "[1/3] Biên dịch mã nguồn..."
cmake --build build -j4

echo "[2/3] Dọn dẹp SHM cũ, khởi động lại SDRplay và chờ 4s..."
sudo rm -f /dev/shm/shm_sdr_ch*
sudo systemctl restart sdrplay
sleep 4

echo "[3/3] Thực thi bài kiểm tra Module 2..."
./build/alignment_test
# for i in {1..5}; do echo "=== Lần $i ===" && ./run_test.sh; done