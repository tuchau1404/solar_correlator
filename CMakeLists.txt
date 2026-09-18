cmake_minimum_required(VERSION 3.16)
project(solar_correlator CXX)

set(CMAKE_CXX_STANDARD 17)
# Cờ biên dịch kích hoạt tối đa sức mạnh ARM NEON / Fast-Math
set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -O3 -march=native -Wall -Wextra -ffast-math")

find_library(SDRPLAY_LIB sdrplay_api REQUIRED)
find_package(PkgConfig REQUIRED)
pkg_check_modules(FFTW3 REQUIRED fftw3f)

include_directories(src ${FFTW3_INCLUDE_DIRS})

# Target 1: Công cụ Test Module 2 (Giữ lại để chẩn đoán hệ thống)
if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/tests/alignment_test.cpp")
    add_executable(alignment_test 
        tests/alignment_test.cpp 
        src/time_alignment.cpp
    )
    target_link_libraries(alignment_test ${SDRPLAY_LIB} ${FFTW3_LIBRARIES} pthread rt m)
endif()

# Target 2: CHƯƠNG TRÌNH THỰC THI CHÍNH CỦA HỆ THỐNG
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
add_executable(test_udp_sender tests/test_udp_sender.cpp)
target_link_libraries(test_udp_sender pthread rt m)
add_executable(test_dsp_math tests/test_dsp_math.cpp src/correlator.cpp)
target_link_libraries(test_dsp_math ${FFTW3_LIBRARIES} pthread rt m)