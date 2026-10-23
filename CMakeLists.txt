cmake_minimum_required(VERSION 3.16)
project(solar_correlator LANGUAGES C CXX)

option(SOLAR_BUILD_DSP "Build FFTW DSP tests" ON)
option(SOLAR_BUILD_HARDWARE "Build SDRplay acquisition application" ON)
option(SOLAR_NATIVE "Optimize for the build host (not cross-compilation)" OFF)

find_package(Threads REQUIRED)

add_library(solar_options INTERFACE)
target_compile_features(solar_options INTERFACE cxx_std_17)
set(CMAKE_CXX_EXTENSIONS OFF)
target_include_directories(solar_options INTERFACE ${CMAKE_CURRENT_SOURCE_DIR}/src)

if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
    target_compile_options(solar_options INTERFACE -Wall -Wextra -Wpedantic
        -fno-fast-math -ffp-contract=off $<$<CONFIG:Release>:-O3>)
    if(SOLAR_NATIVE)
        target_compile_options(solar_options INTERFACE -march=native)
    endif()
endif()

add_executable(test_udp_sender tests/test_udp_sender.cpp)
target_link_libraries(test_udp_sender PRIVATE solar_options Threads::Threads)

if(SOLAR_BUILD_DSP OR SOLAR_BUILD_HARDWARE)
    find_package(PkgConfig REQUIRED)
    pkg_check_modules(FFTW3 REQUIRED IMPORTED_TARGET fftw3f)
endif()

if(SOLAR_BUILD_DSP AND EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/tests/test_dsp_math.cpp")
    add_executable(test_dsp_math tests/test_dsp_math.cpp src/correlator.cpp)
    target_link_libraries(test_dsp_math PRIVATE solar_options PkgConfig::FFTW3 Threads::Threads rt m)
endif()

if(SOLAR_BUILD_HARDWARE)
    find_library(SDRPLAY_LIB NAMES sdrplay_api)
    find_path(SDRPLAY_INCLUDE_DIR NAMES sdrplay_api.h)
    if(NOT SDRPLAY_LIB OR NOT SDRPLAY_INCLUDE_DIR)
        message(FATAL_ERROR "SDRplay SDK missing. For UDP only use -DSOLAR_BUILD_HARDWARE=OFF -DSOLAR_BUILD_DSP=OFF")
    endif()
    target_include_directories(solar_options INTERFACE ${SDRPLAY_INCLUDE_DIR})

    # Tự động quét và build các bài kiểm tra phần cứng độc lập nếu file tồn tại
    foreach(test IN ITEMS single_dev_test dual_stream_test alignment_test)
        if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/tests/${test}.cpp")
            add_executable(${test} tests/${test}.cpp)
            if(test STREQUAL "alignment_test")
                target_sources(${test} PRIVATE src/time_alignment.cpp)
            endif()
            target_link_libraries(${test} PRIVATE solar_options ${SDRPLAY_LIB} PkgConfig::FFTW3 Threads::Threads rt m)
        endif()
    endforeach()

    # Tool chẩn đoán phần cứng độc lập: Dual SDR Live Waterfall (Auto-Power S11 & S22)
    if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/tests/dual_waterfall_test.cpp")
        add_executable(dual_waterfall_test tests/dual_waterfall_test.cpp src/correlator.cpp)
        target_link_libraries(dual_waterfall_test PRIVATE solar_options ${SDRPLAY_LIB} PkgConfig::FFTW3 Threads::Threads rt m)
    endif()

    add_executable(solar_correlator src/main.cpp src/time_alignment.cpp src/correlator.cpp)
    target_link_libraries(solar_correlator PRIVATE solar_options ${SDRPLAY_LIB} PkgConfig::FFTW3 Threads::Threads rt m)
endif()

include(CTest)
if(BUILD_TESTING AND CMAKE_SYSTEM_NAME STREQUAL "Linux" AND CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
    add_executable(test_telemetry_sender tests/test_telemetry_sender.cpp)
    target_link_libraries(test_telemetry_sender PRIVATE solar_options Threads::Threads)
    target_compile_options(test_telemetry_sender PRIVATE -UNDEBUG)
    target_link_options(test_telemetry_sender PRIVATE -Wl,--wrap=sendto)
    add_test(NAME telemetry_sender_contract COMMAND test_telemetry_sender)
endif()
# Dependency-free scientific storage core; runtime archiving requires --config.
add_library(solar_storage_core src/storage/storage_aggregator.cpp)
target_link_libraries(solar_storage_core PUBLIC solar_options)
if(BUILD_TESTING)
    add_executable(test_storage_core tests/storage/test_storage_core.cpp)
    target_link_libraries(test_storage_core PRIVATE solar_storage_core Threads::Threads)
    add_test(NAME storage_core_contract COMMAND test_storage_core)
endif()

option(SOLAR_BUILD_STORAGE "Build HDF5 storage integration" ON)
if(SOLAR_BUILD_STORAGE)
    find_package(HDF5 REQUIRED COMPONENTS C)
    find_package(SQLite3 REQUIRED)
    find_package(nlohmann_json 3.10 REQUIRED)
    add_library(solar_storage
        src/storage/storage_config.cpp src/storage/storage_budget.cpp
        src/storage/bounded_hdf5_vfd.cpp src/storage/hdf5_writer.cpp
        src/storage/storage_pipeline.cpp)
    target_include_directories(solar_storage PUBLIC ${HDF5_INCLUDE_DIRS})
    target_link_libraries(solar_storage PUBLIC solar_storage_core ${HDF5_LIBRARIES}
        SQLite::SQLite3 nlohmann_json::nlohmann_json Threads::Threads)
    target_compile_definitions(solar_storage PRIVATE
        SOLAR_STORAGE_SCRIPTS="${CMAKE_CURRENT_SOURCE_DIR}/scripts")
    execute_process(COMMAND git rev-parse HEAD WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
        OUTPUT_VARIABLE SOLAR_REVISION OUTPUT_STRIP_TRAILING_WHITESPACE)
    execute_process(COMMAND git status --porcelain WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
        OUTPUT_VARIABLE SOLAR_DIRTY OUTPUT_STRIP_TRAILING_WHITESPACE)
    if(NOT SOLAR_REVISION)
        file(STRINGS "${CMAKE_CURRENT_SOURCE_DIR}/SOURCE_REVISION" SOLAR_REVISION LIMIT_COUNT 1)
        set(SOLAR_DIRTY "source-archive")
    endif()
    if(SOLAR_DIRTY)
        set(SOLAR_PATCH_STATE "uncommitted")
    else()
        set(SOLAR_PATCH_STATE "clean")
    endif()
    if(TARGET solar_correlator)
        target_link_libraries(solar_correlator PRIVATE solar_storage)
        target_compile_definitions(solar_correlator PRIVATE SOLAR_STORAGE_ENABLED
            SOLAR_BASELINE_REVISION="${SOLAR_REVISION}" SOLAR_PATCH_STATE="${SOLAR_PATCH_STATE}")
    endif()
endif()

if(SOLAR_BUILD_STORAGE AND BUILD_TESTING)
    add_executable(test_storage_io tests/storage/test_storage_io.cpp)
    target_link_libraries(test_storage_io PRIVATE solar_storage)
endif()

if(SOLAR_BUILD_STORAGE AND BUILD_TESTING)
    add_executable(test_storage_pipeline tests/storage/test_pipeline.cpp)
    target_link_libraries(test_storage_pipeline PRIVATE solar_storage)
endif()

if(SOLAR_BUILD_STORAGE AND BUILD_TESTING)
    add_executable(measure_storage_day tests/storage/measure_day.cpp)
    target_link_libraries(measure_storage_day PRIVATE solar_storage)
endif()

if(BUILD_TESTING)
    add_executable(test_startup_alignment tests/test_startup_alignment.cpp)
    target_link_libraries(test_startup_alignment PRIVATE solar_options)
    add_test(NAME startup_alignment_contract COMMAND test_startup_alignment)
endif()
if(BUILD_TESTING AND SOLAR_BUILD_HARDWARE AND CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
    # The included application entry point is never executed; discard it at link.
    add_executable(test_stream_continuity tests/test_stream_continuity.cpp)
    target_link_libraries(test_stream_continuity PRIVATE solar_options Threads::Threads rt)
    target_compile_options(test_stream_continuity PRIVATE -ffunction-sections -fdata-sections)
    target_link_options(test_stream_continuity PRIVATE -Wl,--gc-sections)
    target_link_libraries(test_stream_continuity PRIVATE PkgConfig::FFTW3 ${SDRPLAY_LIB})
    add_test(NAME stream_continuity_contract COMMAND test_stream_continuity)
endif()
