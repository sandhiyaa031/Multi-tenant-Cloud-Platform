import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from pathlib import Path

# Paths
ROOT = Path("D:/college/PROJECTS-SEM 5/dbpilot/Multi-tenant-Cloud-Platform")
RESULTS_DIR = ROOT / "results" / "final"

# Read Comparison Data
df_comp = pd.read_csv(RESULTS_DIR / "comparison.csv")

# 1. Bar Chart: B0 vs B1 vs B2 SLO Violations
plt.figure(figsize=(8, 6))
sns.barplot(data=df_comp, x='mode', y='slo_viol_rate', palette=['#ff6b6b', '#feca57', '#48dbfb'])
plt.title("Interactive Query SLO Violations (> 2.0ms)")
plt.ylabel("Violation Rate (%)")
plt.xlabel("Control Policy")
plt.xticks(ticks=[0, 1, 2], labels=["B0 (Unbounded A=32)", "B1 (Static A=8)", "B2 (Adaptive)"])
for i, v in enumerate(df_comp['slo_viol_rate']):
    plt.text(i, v + 0.1, f"{v:.2f}%", ha='center', fontweight='bold')
plt.tight_layout()
plt.savefig(RESULTS_DIR / "slo_violations_chart.png")
plt.close()

# 2. Bar Chart: Analytical Throughput
plt.figure(figsize=(8, 6))
sns.barplot(data=df_comp, x='mode', y='ana_tps', palette=['#ff6b6b', '#feca57', '#48dbfb'])
plt.title("Analytical Throughput (Queries / Second)")
plt.ylabel("Throughput (TPS)")
plt.xlabel("Control Policy")
plt.xticks(ticks=[0, 1, 2], labels=["B0 (Unbounded A=32)", "B1 (Static A=8)", "B2 (Adaptive)"])
for i, v in enumerate(df_comp['ana_tps']):
    plt.text(i, v + 0.05, f"{v:.2f} TPS", ha='center', fontweight='bold')
plt.tight_layout()
plt.savefig(RESULTS_DIR / "analytical_tps_chart.png")
plt.close()

# 3. Line Chart: B2 Adaptive Controller Trace
b2_trace_path = RESULTS_DIR / "b2" / "b2_trace.csv"
if b2_trace_path.exists():
    df_trace = pd.read_csv(b2_trace_path)
    df_trace['seconds'] = df_trace['timestamp'] - df_trace['timestamp'].iloc[0]
    
    fig, ax1 = plt.subplots(figsize=(10, 5))
    
    color = 'tab:red'
    ax1.set_xlabel('Time Processed (Seconds)')
    ax1.set_ylabel('SLO Violation Rate (%)', color=color)
    ax1.plot(df_trace['seconds'], df_trace['violation_rate'], color=color, marker='o')
    ax1.tick_params(axis='y', labelcolor=color)
    ax1.axhline(y=1.5, color='gray', linestyle='--', label='Scale Down Limit (1.5%)')
    ax1.axhline(y=0.5, color='gray', linestyle=':', label='Scale Up Limit (0.5%)')
    
    ax2 = ax1.twinx()  
    color = 'tab:blue'
    ax2.set_ylabel('Target Analytical Concurrency (A)', color=color)  
    ax2.step(df_trace['seconds'], df_trace['target_concurrency'], color=color, where='post')
    ax2.tick_params(axis='y', labelcolor=color)
    ax2.set_ylim(0, 32)
    
    fig.tight_layout()  
    plt.title("B2 Adaptive Admission Controller Action vs SLO Violations")
    fig.legend(loc="upper left", bbox_to_anchor=(0.15, 0.85))
    plt.savefig(RESULTS_DIR / "b2_controller_trace.png")
    plt.close()

print("Charts successfully generated.")
