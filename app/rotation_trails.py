"""Display recorded rotation paths without estimating coordinates or trading dates."""
from __future__ import annotations

import hashlib
import html
import json
import math

import pandas as pd
import plotly.graph_objects as go

# Slot identity is shared between themes; shape/dash also distinguish the eight paths.
PALETTES = {
    "light": ("#0077BB", "#C05A14", "#008577", "#CC3311", "#AA4499", "#5544AA", "#886600", "#555555"),
    "dark": ("#56B4E9", "#E69F00", "#44C9AA", "#F09079", "#CC79A7", "#A3A6FF", "#F0E442", "#CBD5E1"),
}
SYMBOLS = ("circle", "square", "triangle-up", "diamond", "triangle-down", "pentagon", "hexagon", "star")
DASHES = ("solid", "dash", "dot", "dashdot", "solid", "dash", "dot", "dashdot")
TIME_SCALES = {
    "light": ((0, "#9CA3AF"), (0.5, "#6366B9"), (1, "#312080")),
    "dark": ((0, "#64748B"), (0.5, "#A5A7EF"), (1, "#E4D8FF")),
}


def trail_styles(keys, theme="light"):
    """Stable for a given identity set regardless of rank/selection order (max eight).

    Hash gives each identity a preferred slot. Sorted collision resolution keeps
    selected paths distinct. Changing the selection can change a colliding slot.
    """
    identities = sorted(set(keys))
    if len(identities) > 8:
        raise ValueError("이동 경로는 최대 8개까지 표시할 수 있습니다.")
    occupied, result = set(), {}
    for key in identities:
        raw = json.dumps(key, ensure_ascii=False, separators=(",", ":")).encode()
        slot = int.from_bytes(hashlib.sha256(raw).digest()[:4], "big") % 8
        while slot in occupied:
            slot = (slot + 1) % 8
        occupied.add(slot)
        result[key] = dict(color=PALETTES[theme][slot], symbol=SYMBOLS[slot], dash=DASHES[slot])
    return result


def effective_trail_mode(mode, selected_count):
    if mode not in ("simple", "sector", "time"):
        raise ValueError("알 수 없는 궤적 표시 방식입니다.")
    return "sector" if mode == "time" and selected_count > 2 else mode


def add_rotation_trails(fig, focused, trails, label_col, sizes, theme, mode, x_range, y_range, height):
    """Add at most two traces per path, with no halo or endpoint rings.

    Time mode colors only observed markers; it does not split/interpolate lines.
    Arrow visibility uses a conservative 160px inner plot width for narrow screens.
    """
    mode = effective_trail_mode(mode, len(focused))
    styles = trail_styles(focused["_key"], theme["mode"])
    neutral = "#CBD5E1" if theme["mode"] == "dark" else "#475569"
    path_count, arrows = 0, 0
    if trails.empty:
        return dict(rotation_path_count=0, trail_mode=mode, direction_count=0)
    # Shared date slots: equal observed dates have equal time colors even if a
    # sector is missing an observation. T labels still count each sector's records.
    dates = pd.to_datetime(trails["date"], errors="coerce").dropna().drop_duplicates().sort_values().tolist()
    date_slots = {date: index for index, date in enumerate(dates)}
    for idx, row in focused.iterrows():
        # Defensive display normalization; the source history builder remains unchanged.
        tail = trails[trails["_key"].map(lambda key: key == row["_key"])].copy()
        tail["date"] = pd.to_datetime(tail["date"], errors="coerce")
        tail = tail.dropna(subset=["date", "median_rs", "rs_delta_4w"]).sort_values("date", kind="stable")
        tail = tail.drop_duplicates("date", keep="last").tail(20)
        if len(tail) < 2 or len(tail[["median_rs", "rs_delta_4w"]].drop_duplicates()) < 2:
            continue
        path_count += 1
        style = dict(styles[row["_key"]], dash="solid", symbol="circle")
        if mode == "simple":
            style["color"] = neutral
        label = html.escape(str(row[label_col]))
        # Always show the full identity in the legend for duplicate sector names.
        identity = " · ".join(html.escape(str(p)) for p in row["_key"] if p)
        group = json.dumps(row["_key"], ensure_ascii=False)
        steps = [f"T-{len(tail)-i-1}" if i < len(tail)-1 else "T0" for i in range(len(tail))]
        # At most four historical labels plus T0; every observation remains in hover.
        label_stride = max(1, math.ceil((len(tail)-1)/4))
        step_labels = [step if i % label_stride == 0 and i < len(tail)-1 else ""
                       for i, step in enumerate(steps)]
        delta_x, delta_y = tail["median_rs"].diff(), tail["rs_delta_4w"].diff()
        custom = [[date.strftime("%Y-%m-%d"), step,
                   "확인 불가" if pd.isna(dx) else f"{dx:+.2f}",
                   "확인 불가" if pd.isna(dy) else f"{dy:+.2f}"]
                  for date, step, dx, dy in zip(tail["date"], steps, delta_x, delta_y)]
        base = dict(x=tail["median_rs"].tolist(), y=tail["rs_delta_4w"].tolist(), legendgroup=group)
        marker = dict(size=3, symbol="circle", line=dict(width=0))
        if mode == "time":
            marker.update(color=[date_slots[date] for date in tail["date"]], cmin=0, cmax=max(1, len(dates)-1),
                          colorscale=TIME_SCALES[theme["mode"]], showscale=False)
        else:
            marker.update(color=style["color"])
        fig.add_trace(go.Scatter(
            **base, mode="lines+markers+text" if mode == "time" else "lines+markers",
            name=identity, line=dict(color=style["color"], width=1.4, dash="solid"),
            marker=marker, opacity=.40, customdata=custom,
            text=step_labels if mode == "time" else None, textposition="top center",
            textfont=dict(size=9, color=theme["text"]),
            meta=dict(kind="rotation_path", identity=list(row["_key"])), showlegend=mode != "simple",
            hovertemplate=(f"<b>{label}</b><br>분석일: %{{customdata[0]}} · %{{customdata[1]}}"
                           "<br>중앙 RS: %{x:.1f} · 4주Δ: %{y:+.1f}"
                           "<br>이전 관측 대비: RS %{customdata[2]}점 · 4주Δ %{customdata[3]}점"
                           "<br>스텝은 표시 관측 순서이며 거래일 간격이 아닙니다.<extra></extra>")))
        end, before = tail.iloc[-1], tail.iloc[-2]
        radius = float(sizes.loc[idx]) / 2
        # Use the immediate previous observation: a repeated latest point has no new direction.
        dx = float(end["median_rs"] - before["median_rs"])
        dy = float(end["rs_delta_4w"] - before["rs_delta_4w"])
        recent = tail.tail(3)
        if len(recent[["median_rs", "rs_delta_4w"]].drop_duplicates()) > 1:
            recent_marker = dict(marker, size=4, symbol="circle")
            if mode == "time":
                recent_marker["color"] = [date_slots[date] for date in recent["date"]]
            fig.add_trace(go.Scatter(
                x=recent["median_rs"].tolist(), y=recent["rs_delta_4w"].tolist(),
                mode="lines+markers", marker=recent_marker,
                line=dict(color=style["color"], width=2.2, dash="solid"),
                opacity=1, hoverinfo="skip", showlegend=False, legendgroup=group,
                meta=dict(kind="rotation_recent", identity=list(row["_key"]))))
            standoff = radius + 5  # Outside the single bubble outline.
            distance = math.hypot(dx * 160 / (x_range[1]-x_range[0]),
                                  dy * max(250, height-260) / (y_range[1]-y_range[0]))
            if distance > standoff + 14:
                arrows += 1
                fig.add_annotation(name="rotation_direction", x=end["median_rs"], y=end["rs_delta_4w"],
                                   ax=before["median_rs"], ay=before["rs_delta_4w"],
                                   xref="x", yref="y", axref="x", ayref="y", text="", showarrow=True,
                                   arrowhead=2, arrowsize=.8, arrowwidth=1.2,
                                   arrowcolor=style["color"], standoff=standoff)
    return dict(rotation_path_count=path_count, trail_mode=mode, direction_count=arrows)

