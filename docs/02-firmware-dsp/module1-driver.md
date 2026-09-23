# Module 1: Dual USB Stream Validation & Ingestion Driver

## 1. Executive Summary & Objectives

Module 1 establishes the physical and driver-level ingestion layer of the 2-element solar radio interferometer. The core objective is to verify that a Raspberry Pi 5 can reliably ingest concurrent, continuous high-throughput baseband data from two SDRplay RSPdx receivers over independent USB 3.0 links without buffer overflows, dropped packets, or kernel DMA stalls.

The primary validation milestones are:

* **High-Throughput Ingestion:** Sustain an aggregate data throughput of 80 MB/s (640 Mbps) across two USB 3.0 buses without packet loss (10.0 MSPS  $\times$ 4 bytes [int16 I/Q] $\times$ 2 channels).
  
* **Driver Stability & Low Overhead:** Maintain zero driver-level buffer resets (`reset = 0`) over continuous streaming runs in a headless Linux environment.
  
* **Physical Hardware Interface:** Validate direct USB 3.0 bus connectivity between the Raspberry Pi 5 RP1 southbridge controller and the two RSPdx receivers.

---

### 2.1. Physical Interconnect Topology
The physical hardware layer interfaces the Raspberry Pi 5 host directly with the two SDRplay RSPdx receiver units to validate concurrent, high-throughput baseband data ingestion across independent USB buses. The physical topology and signal routing are represented by the logical architecture diagram (Figure 2.1) and its physical laboratory benchtop implementation (Figure 2.2):

#### A. Logical Interconnect Architecture
![Physical Interconnect Topology](../assets/photos/module1_hardware_interconnect.svg)
*Figure 2.1: Logical bus topology connecting dual SDRplay RSPdx units directly to the Raspberry Pi 5 host controller over dedicated USB 3.0 links.*

#### B. Laboratory Benchtop Implementation
![Physical Benchtop Hardware Setup](../assets/photos/module1_bench_setup.jpg)
*Figure 2.2: Benchtop validation setup illustrating the physical arrangement of the Raspberry Pi 5 host and dual SDRplay RSPdx receivers during ingestion testing.*

---

## 3. Host Operating System & Environment Configuration

To prevent OS scheduler preemption and interrupt latencies from stalling kernel-level USB DMA transfers, the Raspberry Pi 5 host environment is configured as follows:

### 3.1. OS Configuration
To prevent kernel-level USB DMA streaming from stalling due to CPU scheduling delays, Dynamic Voltage and Frequency Scaling (DVFS) latency spikes, or graphical rendering interrupts, the host operating system is tuned as follows:

* **Operating System:** Raspberry Pi OS 64-bit Lite (Debian Bookworm) running the AArch64 kernel, leveraging native 64-bit registers and ARM NEON SIMD capabilities while minimizing background RAM and process overhead.
* **Headless Runlevel Target:** The Wayland/X11 display server is disabled by default to eliminate display interrupt latency and window manager context switching:
  ```bash
  sudo systemctl set-default multi-user.target
  sudo reboot
  ```
* **CPU Frequency Governor:** All four ARM Cortex-A76 cores are locked to `performance` mode at a constant 2.4 GHz, eliminating DVFS transition latencies that trigger DMA FIFO overruns and driver buffer resets during continuous 80 MB/s ingestion:
  ```bash
  # Force all CPU cores to maximum operating frequency (2.4 GHz)
  echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor

  # Verify locked clock frequency (returns 2400000000 Hz)
  vcgencmd measure_clock arm
  ```


### 3.2. Driver Service Setup
The SDRplay API v3 runtime daemon (`sdrplay.service`) manages the low-level hardware interface:
```bash
# Verify driver daemon health
sudo systemctl status sdrplay

# Restart driver service prior to hardware acquisition tests
sudo systemctl restart sdrplay
```
 
---

## 4. Software Architecture & Ingestion Implementation
>  **Design Retrospective: Multi-Threading Failure (`std::thread`)**
> An initial design attempt evaluated an in-process multi-threaded architecture using `std::thread` to handle each RSPdx device on dedicated thread loops. This approach failed due to internal constraints of the proprietary `libsdrplay_api.so` runtime:
>
> * **Single-Device Process Bound:** The API runtime relies on unexposed process-wide global state variables and static event callbacks, restricting each Unix process space (PID) to a single active hardware instance.
>
> * **Hardware Selection Conflict:** Calling `sdrplay_api_SelectDevice()` concurrently from secondary threads caused subsequent initialization calls to return `sdrplay_api_Fail`, while bypassing thread safety led to race conditions and memory segmentation faults.
> 
> Consequently, multi-threading was deprecated in favor of a multi-process architecture (`fork()`), providing strict virtual memory isolation for each receiver instance.

### 4.1. Multi-Process Driver Lifecycle Flow

![Multi-Process Driver Lifecycle Flow](../assets/photos/module1_code_diagram.svg)

#### Multi-Process Execution Walkthrough
The driver lifecycle executes across isolated Unix process spaces to prevent thread deadlocks and guarantee deterministic streaming:

1. **Hardware Discovery & Handle Release (Parent Process):**  
   The parent process queries the SDRplay API service, verifies that two RSPdx units are detected on the physical USB 3.0 buses, and captures their serial strings (`ser0`, `ser1`). The parent then calls `sdrplay_api_Close()`. Explicitly releasing the API instance before spawning children prevents cloned processes from inheriting locked file descriptors or corrupt driver handles.

2. **Process Address Space Isolation (`fork()`):**  
   The execution context branches into two dedicated child worker processes (`p1`, `p2`). Each child process operates in its own memory space and initializes its own isolated instance of the SDRplay API runtime, completely eliminating inter-thread memory corruption.

3. **Per-Device Configuration & Ingestion (Child Processes):**  
   Each child binds strictly to its designated serial number via `sdrplay_api_SelectDevice()`. Hardware registers are committed (35.0 MHz RF, 8.0 MHz IF filter, 10.0 MSPS sample rate, manual gain, and Port C input). The asynchronous streaming engine runs for a continuous 10-second window, tallying sample volumes and tracking buffer reset flags atomically before performing teardown and terminating via `_exit(0)`.

4. **Synchronization & Lifecycle Reaping (`waitpid`):**  
   The parent process blocks on `waitpid()` until both child processes finish execution. This enforces orderly shutdown, reaps exit statuses to prevent zombie processes, and confirms that both hardware links sustained ingestion simultaneously.

### 4.2. Configuration Parameters
Both receivers are configured with identical hardware register settings to ensure symmetric operation:

| Parameter | Configuration Value | Physical / Engineering Rationale |
| :--- | :--- | :--- |
| **Center Frequency ($f_{\text{RF}}$)** | `35000000.0` ($35.0\text{ MHz}$) | Centers reception over the 30.0 – 40.0 MHz solar observation band. |
| **Analog Filter Bandwidth** | `sdrplay_api_BW_8_000` ($8.0\text{ MHz}$) | Limits out-of-band aliasing prior to ADC digitisation. |
| **Sampling Rate ($f_s$)** | `10000000.0` ($10.0\text{ MSPS}$) | Provides $100\text{ ns}$ sample resolution and 10.0 MHz complex Nyquist bandwidth. |
| **Antenna Input Port** | `sdrplay_api_RspDx_ANTENNA_C` | Routes through the low-loss $50\ \Omega$ BNC Port C. |
| **Automatic Gain Control (AGC)** | `sdrplay_api_AGC_DISABLE` | Fixes analog/digital gains to avoid artificial amplitude modulation. |
| **IF Gain Reduction** | `30` ($30\text{ dB}$ nominal) | Sets IF variable gain amplifier across both channels. |


### 4.3. Key Code Implementations

> **Source Reference:** Full production driver implementation is available at [`tests/dual_stream_test.cpp`](https://github.com/tuchau1404/solar_correlator/blob/main/tests/dual_stream_test.cpp).

#### A. Lock-Free Atomic Callback
The driver delivers I/Q blocks asynchronously via kernel threads. To avoid DMA latency stalls, the callback uses lock-free atomic counters with relaxed memory order instead of mutex locks:

```cpp
void StreamCallback(short *xi, short *xq, sdrplay_api_StreamCbParamsT *params, 
                    unsigned int numSamples, unsigned int reset, void *cbContext) {
    if (reset) {
        reset_count.fetch_add(1, std::memory_order_relaxed);
    }
    total_samples.fetch_add(numSamples, std::memory_order_relaxed);
}
```

#### B. Isolated Process Execution
Each child worker binds exclusively to a specific hardware serial and executes in its own address space, bypassing thread contention in `libsdrplay_api.so`:

```cpp
void run_device_process(int dev_index, const std::string &target_serno) {
    sdrplay_api_Open();
    sdrplay_api_SelectDevice(&devs[selected_idx]); // Isolated binding

    // Commit 35 MHz, 10 MSPS, Port C
    chParams->tunerParams.rfFreq.rfHz = 35000000.0;
    deviceParams->devParams->fsFreq.fsHz = 10000000.0;
    deviceParams->devParams->rspDxParams.antennaSel = sdrplay_api_RspDx_ANTENNA_C;

    sdrplay_api_Init(devs[selected_idx].dev, &cbFns, nullptr);
    std::this_thread::sleep_for(std::chrono::seconds(10));
    _exit(0);
}
```

#### C. Process Orchestration & Reaping
The parent process enumerates the bus, explicitly closes its API instance, forks dedicated workers, and reaps exit statuses to prevent zombie processes:

```cpp
// Explicitly release API handle before fork to avoid contaminated descriptors
sdrplay_api_Close();

pid_t p1 = fork();
if (p1 == 0) run_device_process(0, ser0);

pid_t p2 = fork();
if (p2 == 0) run_device_process(1, ser1);

// Await completion of both parallel streams
waitpid(p1, &status, 0);
waitpid(p2, &status, 0);
```


## 5. Experimental Verification & Ingestion Benchmark

To validate the throughput, zero-packet-drop integrity, and driver isolation under high data rates, a 10-second simultaneous streaming benchmark was conducted on Raspberry Pi 5 using two RSPdx devices at 10.0 MSPS (80 MB/s aggregated raw I/Q throughput).

![Figure 5.1: Dual 10 MSPS Streaming Benchmark Terminal Output](../assets/photos/module1_result.jpg)
*Figure 5.1: Simultaneous dual-channel benchmark output verifying sustained 80 MB/s ingestion without runtime buffer drops*

### 5.1. Log Analysis & Performance Evaluation

* **Actual Sample Rate (9.98646 MSPS):** Both devices achieved $\approx 99.86\%$ of the nominal 10.0 MSPS target. The fractional deficit ($\approx 135,400$ samples out of 100 million) accounts for an initial handshake and USB DMA startup latency of approximately $13.5\text{ ms}$ during API initialization.
  
* **Buffer Stability & Zero Drop (Reset/Drop = 1):** The count of 1 corresponds strictly to the mandatory hardware flush flag dispatched on the very first USB packet during driver startup. Over the entire subsequent 10-second run, this counter remained unchanged, proving zero runtime buffer overflows and zero packet loss at 80 MB/s.
  
* **Channel Symmetry:** Identical metrics across both channels confirm that the multi-process (`fork()`) architecture completely decouples hardware driver instances across the dual USB 3.0 buses on Raspberry Pi 5.