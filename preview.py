import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import matplotlib.font_manager as fm
from solve import solve
from shift_core import MASTER, STATE_SYMBOL, EVE, NIGHT, DAY, OFF, LEAVE, OFFSITE, GAI, DAYNIGHT

fp = fm.FontProperties(fname="/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf")
COL = {NIGHT: "#1F4E78", EVE: "#E8A33D", DAY: "#E2EFDA", OFF: "#D9D9D9",
       LEAVE: "#FFF2CC", OFFSITE: "#DDEBF7", GAI: "#D6BFA8", DAYNIGHT: "#8DB4E2"}
TXTW = {NIGHT, DAYNIGHT}

r = solve("/home/claude/希望届_2026年08月.xlsx", {11}, 45)
A = r["assign"]; days = r["data"]["days"]; dow = r["data"]["dow"]; names = r["names"]; lvl = r["lvl"]
staff = {s["name"]: s for s in r["data"]["staff"]}

nR, nC = len(names), len(days)
fig, ax = plt.subplots(figsize=(nC * 0.34 + 1.6, nR * 0.34 + 1.4))
for i, n in enumerate(names):
    y = nR - 1 - i
    ax.text(-0.6, y + 0.5, f"{n} Lv{lvl[n]}", ha="right", va="center", fontsize=7, fontproperties=fp)
    for j, d in enumerate(days):
        st = A.get((n, d), OFF); sym = STATE_SYMBOL[st]
        ax.add_patch(Rectangle((j, y), 1, 1, facecolor=COL[st], edgecolor="white", lw=0.5))
        fixed = d in staff[n]["cells"]
        ax.text(j + 0.5, y + 0.5, sym, ha="center", va="center", fontsize=6.5,
                color="white" if st in TXTW else "black",
                fontweight="bold" if fixed else "normal", fontproperties=fp)
for j, d in enumerate(days):
    c = "#C00000" if dow[d] == "日" or d in (11,) else ("#1F4E78" if dow[d] == "土" else "black")
    ax.text(j + 0.5, nR + 0.15, f"{d}\n{dow[d]}", ha="center", va="bottom", fontsize=6,
            color=c, fontproperties=fp)
ax.set_xlim(-4, nC); ax.set_ylim(0, nR + 1)
ax.axis("off")
ax.set_title("2026年8月 勤務表（第1段階・自動生成）  ●深夜 ▲準夜 ー日勤 ×休 年休 出出張 / 太字=希望反映",
             fontsize=8.5, fontproperties=fp, pad=10)
plt.tight_layout()
plt.savefig("/home/claude/勤務表_プレビュー.png", dpi=170, bbox_inches="tight")
print("saved preview")
