cmake_minimum_required(VERSION 3.16)
project(solar_correlator CXX)

set(CMAKE_CXX_STANDARD 17)
# Cờ biên dịch kích hoạt tối đa sức mạnh ARM NEON / Fast-Math
set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -O3 -march=native -Wall -Wextra -ffast-math")

find_library(SDRPLAY_LIB sdrplay_api REQUIRED)
find_package(PkgConfig REQUIRED)
pkg_check_modules(FFTW3 REQUIRED fftw3f)

include_directories(src ${FFTW3_INCLUDE_DIRS})

# ----------------------------------------------------------------------
# 1. Các bài kiểm tra phần cứng & Sanity Checks
# ----------------------------------------------------------------------
# Test 2 luồng USB SDR song song (Module 1)
if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/tests/dual_stream_test.cpp")
    add_executable(dual_stream_test tests/dual_stream_test.cpp)
    target_link_libraries(dual_stream_test ${SDRPLAY_LIB} pthread rt m)
endif()

# Test căn chỉnh mốc trễ k=0 / SHM (Module 2)
if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/tests/alignment_test.cpp")
    add_executable(alignment_test 
        tests/alignment_test.cpp 
        src/time_alignment.cpp
    )
    target_link_libraries(alignment_test ${SDRPLAY_LIB} ${FFTW3_LIBRARIES} pthread rt m)
endif()

# Test độc lập truyền nhận mạng UDP (Module 3/4)
add_executable(test_udp_sender tests/test_udp_sender.cpp)
target_link_libraries(test_udp_sender pthread rt m)

# Test độc lập thuật toán DSP / Welch FFTW3 (Module 3)
add_executable(test_dsp_math tests/test_dsp_math.cpp src/correlator.cpp)
target_link_libraries(test_dsp_math ${FFTW3_LIBRARIES} pthread rt m)

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
    pthread 
    rt 
    m
)