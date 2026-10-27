"""Chart the out-of-sample test: strategy vs. SPY, $1,500 start.

    python -m schwab_trader.plot_results
"""
from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from .config import RESULTS_DIR

SURFACE, INK, INK_2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
# column -> (color, label, line style); palette slots 1-3, validated as a set
SERIES = {
    "strategy": ("#2a78d6", "12-1 momentum, top 2", "-"),
    "spy": ("#eb6834", "S&P 500 (SPY)", "-"),
    "preregistered": ("#1baf7a", "Pre-registered pick", (0, (4, 2))),
}


def plot_equity(path=RESULTS_DIR / "robust_equity.csv", out=RESULTS_DIR / "equity_curve.png", title=None):
    eq = pd.read_csv(path, parse_dates=[0], index_col=0)
    fig, ax = plt.subplots(figsize=(10, 5.2), dpi=150)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    for col, (color, name, style) in SERIES.items():
        if col not in eq:
            continue
        ax.plot(eq.index, eq[col], color=color, linewidth=2, linestyle=style, label=name, solid_capstyle="round")
        ax.annotate(f"{name}  ${eq[col].iloc[-1]:,.0f}", (eq.index[-1], eq[col].iloc[-1]),
                    xytext=(6, 0), textcoords="offset points", va="center", fontsize=9, color=INK)
    ax.axhline(eq["strategy"].iloc[0], color=INK_2, linewidth=0.8, linestyle=(0, (2, 3)))
    ax.yaxis.set_major_formatter(matplotlib.ticker.StrMethodFormatter("${x:,.0f}"))
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=9, length=0)
    ax.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=INK)
    ax.set_title(title or "Last 5 years: $1,500, weekly rebalance, whole shares, after slippage", loc="left",
                 fontsize=12, color=INK, pad=12)
    fig.text(0.125, 0.015, f"150%/yr target would be ${eq['target_150pct'].iloc[-1]:,.0f} (off this scale). "
             "Price returns: dividends excluded on every line; idle cash earns 0%.", fontsize=8, color=INK_2)
    fig.subplots_adjust(right=0.76, bottom=0.12)
    fig.savefig(out, facecolor=SURFACE)
    plt.close(fig)
    return out


if __name__ == "__main__":
    print(plot_equity())
