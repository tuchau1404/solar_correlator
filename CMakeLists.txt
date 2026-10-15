cmake_minimum_required(VERSION 3.16)
project(solar_correlator CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# ----------------------------------------------------------------------
# CỜ BIÊN DỊCH TỐI ƯU CORTEX-A76 & CHUẨN SỐ HỌC IEEE 754
# Tuyệt đối KHÔNG dùng -ffast-math để đảm bảo an toàn số học cho correlator.cpp
# ----------------------------------------------------------------------
set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -O3 -march=native -Wall -Wextra -fno-fast-math -ffp-contract=off")

# ----------------------------------------------------------------------
# TÌM KIẾM THƯ VIỆN HỆ THỐNG
# ----------------------------------------------------------------------
find_library(SDRPLAY_LIB sdrplay_api REQUIRED)
find_package(PkgConfig REQUIRED)
pkg_check_modules(FFTW3 REQUIRED fftw3f)
find_package(Threads REQUIRED)

include_directories(
    ${CMAKE_CURRENT_SOURCE_DIR}/src
    ${FFTW3_INCLUDE_DIRS}
)

# ----------------------------------------------------------------------
# 1. CÁC BÀI KIỂM TRA PHẦN CỨNG & SANITY CHECKS
# ----------------------------------------------------------------------
# Test 2 luồng USB SDR song song (Module 1)
if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/tests/dual_stream_test.cpp")
    add_executable(dual_stream_test tests/dual_stream_test.cpp)
    target_link_libraries(dual_stream_test ${SDRPLAY_LIB} Threads::Threads rt m)
endif()

# Test căn chỉnh mốc trễ k=0 / SHM (Module 2)
if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/tests/alignment_test.cpp")
    add_executable(alignment_test 
        tests/alignment_test.cpp 
        src/time_alignment.cpp
    )
    target_link_libraries(alignment_test ${SDRPLAY_LIB} ${FFTW3_LIBRARIES} Threads::Threads rt m)
endif()

# Test độc lập truyền nhận mạng UDP (Module 3/4)
if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/tests/test_udp_sender.cpp")
    add_executable(test_udp_sender tests/test_udp_sender.cpp)
    target_link_libraries(test_udp_sender Threads::Threads rt m)
endif()

# Test độc lập thuật toán DSP / Welch FFTW3 (Module 3)
if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/tests/test_dsp_math.cpp")
    add_executable(test_dsp_math 
        tests/test_dsp_math.cpp 
        src/correlator.cpp
    )
    target_link_libraries(test_dsp_math ${FFTW3_LIBRARIES} Threads::Threads rt m)
endif()

# ----------------------------------------------------------------------
# 2. CHƯƠNG TRÌNH THỰC THI CHÍNH CỦA TOÀN BỘ HỆ THỐNG
# ----------------------------------------------------------------------
add_executable(solar_correlator 
    src/main.cpp 
    src/time_alignment.cpp 
    src/correlator.cpp
)
target_link_libraries(solar_correlator 
    ${SDRPLAY_LIB} 
    ${FFTW3_LIBRARIES} 
    Threads::Threads 
    rt 
    m
)