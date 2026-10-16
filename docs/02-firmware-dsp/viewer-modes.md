> **SOL2 v3 update (2026-10-16):** Main và viewer FX hiện dùng năm luồng typed v3.
> Xem [hợp đồng, lệnh chạy và kiểm thử mới](telemetry-v3-upgrade.md).
> Phần nội dung cũ bên dưới không phải hợp đồng hoặc hướng dẫn chạy v3.

# Solar Correlator Visualization Modes & Execution Guide

This document details the telemetry architecture, operational display modes, plot configurations, and execution commands for the real-time visualization suite of the **Decametric Solar Radio Interferometer (30.0 – 40.0 MHz)**.

---

## 1. Telemetry Architecture Overview

The visualization clients run on the **Host Workstation / PC** and ingest binary UDP datagrams transmitted from the **Raspberry Pi 5 Edge DSP Backend** across the local network:
* **Operating Frequency Band:** 30.0 MHz to 40.0 MHz ($f_{\text{center}} = 35.0\text{ MHz}$, $\text{Bandwidth} = 10.0\text{ MHz}$).
* **Frequency Channels:** 2048 bins (channel resolution $\Delta f \approx 4.8828\text{ kHz}$).
* **Nominal Cadence:** $200\text{ ms} \pm 2\text{ ms}$ per frame ($5\text{ FPS}$, corresponding to $M = 1953$ to $2441$ integrated FFT frames).
* **Datagram Payload:** 8,220 bytes total (28-byte packed header with `SOLR` identifier, monotonic Frame ID, microsecond timestamp, followed by 2048 IEEE 754 `float32` decibel values).
* **Power Calibration Unit:** $\text{dB re 1 ADC-count}^2/\text{bin}$ (nominal dynamic range $10.0\text{ dB}$ to $95.0\text{ dB}$).
* **Time System:** All GUI title bars convert raw microsecond UTC timestamps into **Vietnam Time (ICT / UTC+7, format: `HH:MM:SS.mmm`)**.

---

## 2. Comprehensive Visualization Modes

The project provides three distinct visualization scripts tailored for specific observational, diagnostic, and calibration workflows:

### Mode 1: Standard Science Observation (`viewer/viewer.py`)
* **Primary Target:** Routine solar transit observations, coronal mass ejection tracking, and scientific solar radio burst monitoring (Solar Type II and Type III sweeps).
* **Backend Pipeline:** `solar_correlator` daemon executing Module 2 (hardware time-alignment lock at $k=0$) followed by Module 3 (FX Correlator).
* **Network Channels:** Listens on a single UDP socket (`0.0.0.0:9999`).
* **Display Layout (2 Stacked Panels):**
  1. **Top Panel (1D Spectrum):** Real-time cross-power spectrum $|S_{12}(f)|$ plotted in bright yellow (`#ffdc32`) across 30.0 – 40.0 MHz. Displays dynamic peak tracking (dB amplitude and MHz center frequency) and frame metadata in the header.
  2. **Bottom Panel (2D Waterfall):** Rolling 300-frame time-frequency spectrogram (~60 seconds history at $200\text{ ms/slot}$) using the high-contrast `inferno` colormap. Includes an interactive `ColorBarItem` / `HistogramLUTItem` on the right for mouse-dragged dynamic range adjustments (default $20.0\text{ dB}$ to $85.0\text{ dB}$).

### Mode 2: Hardware Diagnostic & RF Bench Split-View (`viewer/viewer_dual.py`)
* **Primary Target:** Hardware validation, bench testing with RF splitters, coaxial cable matching, and real-time gain calibration between the two RSPdx receivers without running cross-correlation or Module 2 time alignment.
* **Backend Pipeline:** `tests/dual_waterfall_test.cpp` streaming unaligned raw auto-powers $S_{11}(f) = |X_1(f)|^2$ and $S_{22}(f) = |X_2(f)|^2$ concurrently.
* **Network Channels:** Dual independent UDP sockets to isolate kernel socket buffers:
  * Port `9998`: SDR Channel 1 Auto-Power ($S_{11}$)
  * Port `9999`: SDR Channel 2 Auto-Power ($S_{22}$)
* **Display Layout (4-Plot Symmetric Split-View):**
  * **Left Column (SDR 1 / $S_{11}$):**
    * Top: 1D Auto-Power Spectrum $|S_{11}|$ ($10 - 95\text{ dB}$, yellow curve `#ffdc32`).
    * Bottom: 2D Rolling Waterfall (300 slots) with dedicated interactive `ColorBarItem`.
  * **Right Column (SDR 2 / $S_{22}$):**
    * Top: 1D Auto-Power Spectrum $|S_{22}|$ ($10 - 95\text{ dB}$, light cyan curve `#38bdf8`).
    * Bottom: 2D Rolling Waterfall (300 slots) with dedicated interactive `ColorBarItem`.
  * **Status Bar:** Real-time channel delivery metrics (`complete`, `frame_gaps`, `late`, `invalid`) and instantaneous gain balance $\Delta\text{Peak} = P_{11} - P_{22}$ in dB.

### Mode 3: Advanced Hybrid & Multi-Scale Waterfall (`viewer/viewer_fx.py`)
* **Primary Target:** Advanced astronomical research combining fast transient detection with deep sensitivity integration and complex visibility phase tracking.
* **Backend Pipeline:** Full FX Correlator backend outputting cross-power, optional auto-power overlays, and interferometric phase / coherence.
* **Network Channels:** Configurable multi-port reception (1 to 4 UDP ports):
  * Primary Port (`--port-s12`, default `9999`): Cross-power spectrum $|S_{12}|$
  * Auxiliary Port 1 (`--port-s11`, optional): SDR 1 auto-power $S_{11}$
  * Auxiliary Port 2 (`--port-s22`, optional): SDR 2 auto-power $S_{22}$
  * Auxiliary Port 3 (`--port-aux`, optional): Visibility Phase $\Delta\phi$ or Coherence $\gamma_{12}$
* **Display Layout (Up to 4 Plot Widgets / 6 Data Streams):**
  1. **Row 0, Col 0 (1D Multi-Curve Overlay Spectrum):**
     * Fast $|S_{12}|$ ($200\text{ ms}$, yellow curve `#ffdc32`) for rapid transient monitoring.
     * Deep Integrated $\langle |S_{12}| \rangle$ ($N$-frame linear average, orange curve `#f97316`) for noise-suppressed spectral baseline verification.
     * Optional background overlays of $S_{11}$ (cyan `#38bdf8`) and $S_{22}$ (red `#f87171`).
  2. **Row 0, Col 1 (1D Auxiliary Diagnostic - Enabled via `--port-aux`):**
     * Interferometric Visibility Phase $\Delta\phi \in [-\pi, +\pi]\text{ rad}$ (purple curve `#a78bfa`), or
     * Complex Degree of Coherence $\gamma_{12} \in [0.0, 1.0]$ (emerald curve `#34d399`).
  3. **Row 1, Col 0 (Fast 2D Waterfall):**
     * Single-frame resolution ($200\text{ ms/slot}$, 300 history slots) for detecting micro-bursts and rapid frequency drift rates.
  4. **Row 2, Col 0 (Deep Integrated 2D Waterfall):**
     * Multi-frame linear power integration (default: 5 frames $\approx 1.0\text{ s/slot}$, 300 history slots). Provides $\approx +3.5\text{ dB}$ additional noise floor suppression to extract faint, long-duration solar events (Type II/IV shock waves).

---

## 3. Terminal Execution Commands

### Prerequisites on Host Workstation
Ensure your Python environment contains the required dependencies:
```bash
pip install numpy PyQt5 pyqtgraph
```
*Windows Firewall Configuration (run in elevated PowerShell if packets are dropped):*
```powershell
New-NetFirewallRule -DisplayName "Solar UDP" -Direction Inbound -LocalPort 9996-9999 -Protocol UDP -Action Allow
```

---

### Command Set 1: Running Mode 1 (`viewer.py`)
Listen on the default UDP port (`9999`) across all network interfaces:
```bash
python viewer/viewer.py
```
Specify a custom bind address or port:
```bash
python viewer/viewer.py --bind 0.0.0.0 --port 9999
```
Filter incoming datagrams to accept only packets from the Raspberry Pi 5 IP:
```bash
python viewer/viewer.py --port 9999 --source-ip 192.168.1.15
```

---

### Command Set 2: Running Mode 2 (`viewer_dual.py`)
Run the dual-channel receiver using the audited default ports (`9998` for Ch1, `9999` for Ch2):
```bash
python viewer/viewer_dual.py
```
Explicitly customize UDP ports and bind address:
```bash
python viewer/viewer_dual.py --bind 0.0.0.0 --port1 9998 --port2 9999
```
Lock to a specific transmitter IP:
```bash
python viewer/viewer_dual.py --port1 9998 --port2 9999 --source-ip 192.168.1.15
```

---

### Command Set 3: Running Mode 3 (`viewer_fx.py`)

#### A. Standard Dual-Scale Waterfall (Single Port 9999)
Runs the fast $200\text{ ms}$ waterfall and the deep $1.0\text{ s}$ integrated waterfall simultaneously:
```bash
python viewer/viewer_fx.py --port 9999
```

#### B. Deep Integration Depth Adjustment
Set deep integration to 10 frames ($2.0\text{ s}$ per slot, $+5.0\text{ dB}$ noise floor suppression):
```bash
python viewer/viewer_fx.py --port 9999 --deep-frames 10
```

#### C. Cross-Power Overlay with Auto-Power Spectrograms ($S_{11}$ & $S_{22}$)
Overlay both receiver auto-power curves on top of the cross-power spectrum:
```bash
python viewer/viewer_fx.py --port-s12 9999 --port-s11 9998 --port-s22 9997
```

#### D. Full Hybrid Scientific Mode with Phase Tracking
Enables cross-power, dual auto-powers, and the 1D Interferometric Phase plot ($\Delta\phi$):
```bash
python viewer/viewer_fx.py --port-s12 9999 --port-s11 9998 --port-s22 9997 --port-aux 9996 --aux-mode phase
```

#### E. Full Hybrid Scientific Mode with Coherence Tracking
Enables cross-power, dual auto-powers, and the Degree of Coherence plot ($\gamma_{12}$):
```bash
python viewer/viewer_fx.py --port-s12 9999 --port-s11 9998 --port-s22 9997 --port-aux 9996 --aux-mode coherence
```

---

## 4. Summary Matrix of Visualization Scripts

| Feature / Metric | `viewer/viewer.py` | `viewer/viewer_dual.py` | `viewer/viewer_fx.py` |
| :--- | :--- | :--- | :--- |
| **Operational Role** | Standard Science Run | Hardware QA & RF Calibration | Advanced Hybrid Multi-Scale |
| **UDP Ports Used** | 1 (`9999`) | 2 (`9998`, `9999`) | 1 to 4 (`9996`–`9999`) |
| **Total Plot Widgets** | 2 | 4 (2x2 Split) | 3 to 4 |
| **Cross-Power $|S_{12}|$** | Yes (Fast 200 ms) | No | Yes (Fast + Deep Integration) |
| **Auto-Powers ($S_{11}, S_{22}$)** | No | Yes (Isolated Channels) | Yes (Overlay Mode) |
| **Interferometric Phase $\Delta\phi$** | No | No | Yes (via `--port-aux` mode) |
| **Coherence $\gamma_{12}$** | No | No | Yes (via `--port-aux` mode) |
| **Dual-Scale Waterfalls** | No (Single 200 ms) | No (Dual 200 ms) | Yes ($200\text{ ms}$ & $1.0\text{ s}$ Deep) |
| **Zero-Roll Ring Buffering** | Yes (`axisOrder='row-major'`) | Yes (`axisOrder='row-major'`) | Yes (`axisOrder='row-major'`) |
| **Timestamp Formatting** | Vietnam Time (UTC+7) | Vietnam Time (UTC+7) | Vietnam Time (UTC+7) |

---

## 5. Backend Companion Execution (Raspberry Pi 5)

To stream real-time data to these GUI viewers, execute the corresponding backend target on the Raspberry Pi 5:

1. **For `viewer.py` or `viewer_fx.py` (Live Observation):**
   ```bash
   cd ~/solar_correlator
   cmake --build build -j4
   ./build/solar_correlator
   ```
2. **For `viewer_dual.py` (Hardware Diagnostic & Calibration):**
   ```bash
   cd ~/solar_correlator
   cmake --build build --target dual_waterfall_test -j4
   ./build/dual_waterfall_test
   ```