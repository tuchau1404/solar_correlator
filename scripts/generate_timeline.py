import matplotlib.pyplot as plt
import matplotlib.patches as patches
from pathlib import Path

# Cấu hình thông số vật lý & hình học
SAMPLE_RATE_HZ = 10_000_000      # 10 MSPS
SAMPLE_DURATION_NS = 100          # Ts = 100 ns mỗi mẫu
K_OFFSET = 3                      # Số mẫu lệch minh họa (k_offset = 3 mẫu -> 300 ns)
NUM_SAMPLES_RX1 = 6               # Số khối mẫu hiển thị trên Kênh 1
NUM_SAMPLES_RX2 = 4               # Số khối mẫu hiển thị trên Kênh 2

# Thiết lập Canvas đồ họa chuẩn vector
fig, ax = plt.subplots(figsize=(10.5, 4.2), dpi=300)

# ============================================================================
# 1. KHỐI TÍN HIỆU RECEIVER 1 (USB LEADER - XUẤT PHÁT TẠI T = 0)
# ============================================================================
y_rx1 = 2.1
block_height = 0.55

for i in range(NUM_SAMPLES_RX1):
    t_start = i * SAMPLE_DURATION_NS
    rect = patches.Rectangle(
        (t_start, y_rx1), SAMPLE_DURATION_NS, block_height,
        linewidth=1.8, edgecolor='#1E40AF', facecolor='#DBEAFE', zorder=3
    )
    ax.add_patch(rect)
    ax.text(
        t_start + SAMPLE_DURATION_NS / 2, y_rx1 + block_height / 2, f"Sample {i}",
        color='#1E40AF', ha='center', va='center', fontweight='bold', fontsize=9.5
    )

ax.text((NUM_SAMPLES_RX1 + 0.3) * SAMPLE_DURATION_NS, y_rx1 + block_height / 2, "...", 
        color='#64748B', ha='center', va='center', fontweight='bold', fontsize=16)

# ============================================================================
# 2. KHỐI TÍN HIỆU RECEIVER 2 (DELAYED STREAM - BỊ TRỄ K_OFFSET)
# ============================================================================
y_rx2 = 0.7

# Vùng trễ khởi động phần cứng / USB (Khoảng chờ vô định hình)
skew_duration = K_OFFSET * SAMPLE_DURATION_NS
skew_rect = patches.Rectangle(
    (0, y_rx2), skew_duration, block_height,
    linewidth=1.8, edgecolor='#DC2626', facecolor='#FEE2E2', linestyle='--', zorder=3
)
ax.add_patch(skew_rect)
ax.text(
    skew_duration / 2, y_rx2 + block_height / 2, 
    "Hardware / USB\nStartup Latency",
    color='#991B1B', ha='center', va='center', fontstyle='italic', fontweight='semibold', fontsize=8.5
)

# Các mẫu dải cơ sở bắt đầu thu sau khi trễ
for i in range(NUM_SAMPLES_RX2):
    t_start = skew_duration + (i * SAMPLE_DURATION_NS)
    rect = patches.Rectangle(
        (t_start, y_rx2), SAMPLE_DURATION_NS, block_height,
        linewidth=1.8, edgecolor='#15803D', facecolor='#DCFCE7', zorder=3
    )
    ax.add_patch(rect)
    ax.text(
        t_start + SAMPLE_DURATION_NS / 2, y_rx2 + block_height / 2, f"Sample {i}",
        color='#15803D', ha='center', va='center', fontweight='bold', fontsize=9.5
    )

ax.text(skew_duration + (NUM_SAMPLES_RX2 + 0.3) * SAMPLE_DURATION_NS, y_rx2 + block_height / 2, "...", 
        color='#64748B', ha='center', va='center', fontweight='bold', fontsize=16)

# ============================================================================
# 3. ĐƯỜNG GIÓNG TỌA ĐỘ VÀ MŨI TÊN ĐO K_OFFSET
# ============================================================================
t_rx1_origin = 0
t_rx2_start = skew_duration

# Gióng đứng nét đứt
ax.plot([t_rx1_origin, t_rx1_origin], [0.35, y_rx1], color='#94A3B8', linestyle=':', linewidth=1.4, zorder=2)
ax.plot([t_rx2_start, t_rx2_start], [0.35, y_rx1], color='#DC2626', linestyle=':', linewidth=1.5, zorder=2)

# Mũi tên đo kích thước 2 chiều (Dimension Arrow)
arrow_y = 1.6
ax.annotate(
    '', xy=(t_rx1_origin, arrow_y), xytext=(t_rx2_start, arrow_y),
    arrowprops=dict(arrowstyle='<->', color='#DC2626', lw=2.0)
)
ax.text(
    t_rx2_start / 2, arrow_y + 0.12, 
    f"$k_{{\\mathrm{{offset}}}} = {K_OFFSET}$ samples  ($\\Delta t = {K_OFFSET * SAMPLE_DURATION_NS}$ ns)",
    color='#DC2626', ha='center', va='bottom', fontweight='bold', fontsize=10.5
)

# ============================================================================
# 4. ĐỊNH DẠNG TRỤC TỌA ĐỘ VÀ NHÃN KỸ THUẬT
# ============================================================================
ax.text(-25, y_rx1 + block_height / 2, "Receiver 1 Timeline\n(USB Leader / Early)", 
        ha='right', va='center', fontweight='bold', color='#1E293B', fontsize=10)
ax.text(-25, y_rx2 + block_height / 2, "Receiver 2 Timeline\n(Delayed Stream)", 
        ha='right', va='center', fontweight='bold', color='#1E293B', fontsize=10)

ax.set_xlim(-160, 780)
ax.set_ylim(0.1, 3.1)
ax.set_xlabel("Continuous Timeline $t$ (Nanoseconds / ns)  —  $T_s = 100$ ns per sample ($f_s = 10.0$ MSPS)", 
              fontsize=10.5, labelpad=10, fontweight='semibold', color='#0F172A')

ax.set_xticks(range(0, 701, 100))
ax.set_yticks([])

# Ẩn viền bao quanh đồ thị, chỉ giữ lại trục hoành
ax.spines['top'].set_visible(False)
ax.spines['right'].set_visible(False)
ax.spines['left'].set_visible(False)
ax.spines['bottom'].set_color('#334155')
ax.spines['bottom'].set_linewidth(1.5)

# Lưới gióng vạch thời gian
ax.grid(axis='x', linestyle='--', alpha=0.45, color='#94A3B8')

plt.tight_layout()

# ============================================================================
# 5. XUẤT FILE VECTOR SVG
# ============================================================================
# Tự động xuất ra thư mục hiện tại và thư mục docs/assets/ nếu có
output_svg = "timeline_skew.svg"
plt.savefig(output_svg, format="svg", bbox_inches='tight')

assets_path = Path("docs/assets")
if assets_path.exists():
    plt.savefig(assets_path / output_svg, format="svg", bbox_inches='tight')
    print(f"[OK] Đã lưu file: docs/assets/{output_svg}")

print(f"[OK] Đã tạo thành công file vector: {output_svg}")
plt.close()