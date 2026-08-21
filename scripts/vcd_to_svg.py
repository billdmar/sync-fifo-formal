#!/usr/bin/env python3
# =============================================================================
# vcd_to_svg.py — Render a styled digital-timing-diagram SVG from a Verilator
#                 VCD, showing a real fill -> full -> drain -> empty window of
#                 the sync_fifo simulation.
#
# Intellectual honesty: this is a faithful hand-parser of the VCD produced by
# `make sim`. Every plotted edge and bus value is taken verbatim from the trace.
# Nothing is idealized or hand-drawn. We only *select a time window* (the
# contiguous reset -> fill-to-full -> drain-to-empty sequence) so the diagram is
# legible; we do not alter the recorded values within it.
#
# No third-party dependencies (stdlib only) — keeps the artifact reproducible
# in CI and in a clean venv.
#
# Usage:
#   python3 scripts/vcd_to_svg.py \
#       [--vcd docs/waveforms/sim_waves.vcd] \
#       [--out docs/waveforms/sim_waves.svg] \
#       [--start <ps>] [--end <ps>]   # auto-detected if omitted
# =============================================================================

import argparse
import sys

# Signals to render, in vertical order. (vcd_name, label, kind)
#   kind: "bit" = single-bit, "bus" = multi-bit value (hex/dec label).
SIGNALS = [
    ("clk",          "clk",     "bit"),
    ("rst_n",        "rst_n",   "bit"),
    ("wr_en",        "wr_en",   "bit"),
    ("wr_data",      "wr_data", "bus"),
    ("rd_en",        "rd_en",   "bit"),
    ("rd_data",      "rd_data", "bus"),
    ("full",         "full",    "bit"),
    ("empty",        "empty",   "bit"),
    ("count",        "count",   "bus"),
]


def parse_vcd(path):
    """Parse VCD. Return (id->name map, list of (time, {name: value}) snapshots).

    Values: single-bit stay as '0'/'1'/'x'/'z' strings; buses become ints
    (or the raw string if non-binary). Only the first (TOP-level) declaration of
    each signal name is kept, so we get the DUT-port view, not internal scopes.
    """
    code2name = {}
    seen_names = set()
    in_defs = True
    snapshots = []
    cur = {}
    t = 0
    started = False

    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            if in_defs:
                if line.startswith("$var"):
                    # $var wire <width> <code> <name> [range] $end
                    parts = line.split()
                    code = parts[3]
                    name = parts[4]
                    if name not in seen_names:
                        code2name[code] = name
                        seen_names.add(name)
                elif line.startswith("$enddefinitions"):
                    in_defs = False
                continue

            if line[0] == "#":
                if started:
                    snapshots.append((t, dict(cur)))
                t = int(line[1:])
                started = True
                continue

            c = line[0]
            if c in "01xz":
                code = line[1:]
                name = code2name.get(code)
                if name is not None:
                    cur[name] = c
            elif c in "bB":
                val, code = line[1:].split()
                name = code2name.get(code)
                if name is not None:
                    try:
                        cur[name] = int(val, 2)
                    except ValueError:
                        cur[name] = val
            # 'r' (real) not used here.

    if started:
        snapshots.append((t, dict(cur)))
    return code2name, snapshots


def value_at(snapshots, name, time):
    """Last known value of `name` at or before `time`."""
    val = None
    for t, snap in snapshots:
        if t > time:
            break
        if name in snap and snap[name] is not None:
            val = snap[name]
    return val


def auto_window(snapshots):
    """Find a contiguous reset -> fill-to-full -> drain-to-empty window.

    We scan for the first index where rst_n is high and count climbs 0->DEPTH
    (full=1) and then descends back to 0 (empty=1) without an intervening reset.
    Returns (start_ps, end_ps). Falls back to the whole trace if not found.
    """
    # Build a count timeline (time, count, full, empty, rst_n, wr_en, rd_en).
    timeline = []
    for t, snap in snapshots:
        timeline.append((
            t,
            snap.get("count"),
            snap.get("full"),
            snap.get("empty"),
            snap.get("rst_n"),
            snap.get("wr_en"),
            snap.get("rd_en"),
        ))

    n = len(timeline)
    for i in range(n):
        t, cnt, full, empty, rst, wr, rd = timeline[i]
        # Anchor on an empty/idle state (count==0, empty asserted). The reset for
        # the directed fill/drain scenario may still be active here; we allow
        # rst_n to be released (0→1) during the prologue, then require a strictly
        # monotonic, write-driven fill to full followed by a strictly monotonic,
        # READ-driven drain to empty. The read-driven requirement is what
        # distinguishes a real drain from a reset-clear (where count snaps to 0
        # with rd_en low) and from the noisy randomized region (count oscillates).
        if cnt == 0 and empty == "1":
            saw_full = False
            saw_fill_start = False
            prev_cnt = 0
            for j in range(i, n):
                tj, cj, fj, ej, rj, wj, rdj = timeline[j]
                if cj is None:
                    continue
                if rj == "0" and saw_fill_start:
                    break  # a reset mid-sequence is not a real drain — reject
                if not saw_fill_start:
                    if cj == 0:
                        continue          # still in reset / idle prologue
                    saw_fill_start = True  # fill has begun
                if not saw_full:
                    if cj < prev_cnt:      # fill must be monotonic up
                        break
                    if fj == "1":
                        saw_full = True
                else:
                    if cj > prev_cnt:      # drain must be monotonic down
                        break
                    if cj < prev_cnt and rdj != "1":
                        break              # count fell without a read => not a real drain
                    if ej == "1":
                        # Clean, read-driven fill→full→drain→empty window found.
                        return max(0, t - 4), tj + 6
                prev_cnt = cj
            # else keep scanning from the next anchor.
    # Fallback: whole trace.
    return snapshots[0][0], snapshots[-1][0]


def collect_edges(snapshots, name, start, end):
    """Return list of (time, value) change points within [start, end],
    seeded with the value at `start`."""
    pts = [(start, value_at(snapshots, name, start))]
    last = pts[0][1]
    for t, snap in snapshots:
        if t <= start or t > end:
            continue
        if name in snap and snap[name] is not None and snap[name] != last:
            pts.append((t, snap[name]))
            last = snap[name]
    return pts


def render_svg(snapshots, start, end, out_path):
    # ---- Layout constants -------------------------------------------------
    LEFT = 110          # label gutter width
    RIGHT_PAD = 24
    TOP = 64            # space for title
    ROW_H = 46          # vertical pitch per signal row
    WAVE_H = 26         # height of a wave within its row
    BIT_HI_OFFSET = 4   # high level offset from row top
    PX_PER_PS = 14.0    # horizontal scale

    span = end - start
    width = int(LEFT + span * PX_PER_PS + RIGHT_PAD)
    height = int(TOP + len(SIGNALS) * ROW_H + 28)

    # Palette (dark theme, crisp).
    BG = "#0d1117"
    GRID = "#21262d"
    LABEL = "#c9d1d9"
    BIT = "#39d353"        # green for logic transitions
    BUS = "#58a6ff"        # blue for bus shapes
    BUS_TXT = "#e6edf3"
    TITLE = "#e6edf3"
    SUB = "#8b949e"
    EDGEHL = "#f78166"     # highlight color for full/empty markers

    def x_of(t):
        return LEFT + (t - start) * PX_PER_PS

    parts = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" '
        f'font-family="SFMono-Regular,Consolas,Menlo,monospace">'
    )
    parts.append(f'<rect width="{width}" height="{height}" fill="{BG}"/>')

    # Title
    parts.append(
        f'<text x="{LEFT}" y="26" fill="{TITLE}" font-size="18" '
        f'font-weight="700">sync_fifo &#8212; fill &#8594; full &#8594; drain &#8594; empty (DEPTH=8)</text>'
    )
    parts.append(
        f'<text x="{LEFT}" y="46" fill="{SUB}" font-size="12">'
        f'Real Verilator trace (docs/waveforms/sim_waves.vcd), window {start}–{end} ps · '
        f'1 cycle = 2 ps</text>'
    )

    # Light vertical grid lines every 2 ps (one clock period).
    tg = start - (start % 2)
    while tg <= end:
        gx = x_of(tg)
        parts.append(
            f'<line x1="{gx:.1f}" y1="{TOP}" x2="{gx:.1f}" '
            f'y2="{TOP + len(SIGNALS)*ROW_H:.1f}" stroke="{GRID}" stroke-width="1"/>'
        )
        tg += 2

    # ---- Per-signal waves -------------------------------------------------
    for idx, (name, label, kind) in enumerate(SIGNALS):
        row_top = TOP + idx * ROW_H
        y_hi = row_top + BIT_HI_OFFSET
        y_lo = y_hi + WAVE_H
        mid = (y_hi + y_lo) / 2

        # Label
        parts.append(
            f'<text x="{LEFT - 12}" y="{mid + 4:.1f}" fill="{LABEL}" '
            f'font-size="13" text-anchor="end">{label}</text>'
        )

        pts = collect_edges(snapshots, name, start, end)

        if kind == "bit":
            # Build a stepped polyline of the digital signal.
            d = []
            prev_y = None
            for k, (t, v) in enumerate(pts):
                x = x_of(t)
                lvl = y_hi if v == "1" else y_lo
                if prev_y is None:
                    d.append(f'M{x:.1f},{lvl:.1f}')
                else:
                    # vertical transition then horizontal hold
                    d.append(f'L{x:.1f},{prev_y:.1f} L{x:.1f},{lvl:.1f}')
                prev_y = lvl
            # extend to window end
            d.append(f'L{x_of(end):.1f},{prev_y:.1f}')
            color = EDGEHL if name in ("full", "empty") else BIT
            parts.append(
                f'<path d="{"".join(d)}" fill="none" stroke="{color}" '
                f'stroke-width="2"/>'
            )
        else:
            # Bus: draw a "stretched hexagon" segment per stable value with the
            # value printed in the middle. 0 is shown as a flat low line for
            # rd_data/wr_data readability is fine; we always print the number.
            seg_pts = pts + [(end, pts[-1][1])]
            for k in range(len(seg_pts) - 1):
                t0, v0 = seg_pts[k]
                t1, _ = seg_pts[k + 1]
                if t1 <= t0:
                    continue
                x0 = x_of(t0)
                x1 = x_of(t1)
                slope = min(3.0, (x1 - x0) / 2.0)
                # hexagon outline
                parts.append(
                    f'<path d="M{x0:.1f},{mid:.1f} '
                    f'L{x0+slope:.1f},{y_hi:.1f} '
                    f'L{x1-slope:.1f},{y_hi:.1f} '
                    f'L{x1:.1f},{mid:.1f} '
                    f'L{x1-slope:.1f},{y_lo:.1f} '
                    f'L{x0+slope:.1f},{y_lo:.1f} Z" '
                    f'fill="none" stroke="{BUS}" stroke-width="1.6"/>'
                )
                # value text
                if isinstance(v0, int):
                    txt = f'{v0:#x}' if name in ("wr_data", "rd_data") else str(v0)
                else:
                    txt = str(v0)
                cx = (x0 + x1) / 2
                if (x1 - x0) > 12:
                    parts.append(
                        f'<text x="{cx:.1f}" y="{mid + 4:.1f}" fill="{BUS_TXT}" '
                        f'font-size="11" text-anchor="middle">{txt}</text>'
                    )

    # ---- Annotate the full / empty milestones -----------------------------
    # Mark the clock time where full first rises and where empty first rises.
    def first_rise_time(name):
        prev = value_at(snapshots, name, start)
        for t, snap in snapshots:
            if t <= start or t > end:
                continue
            if name in snap and snap[name] == "1" and prev != "1":
                return t
            if name in snap:
                prev = snap[name]
        return None

    bottom = TOP + len(SIGNALS) * ROW_H
    for marker_name, text in (("full", "FULL"), ("empty", "EMPTY")):
        mt = first_rise_time(marker_name)
        if mt is not None:
            mx = x_of(mt)
            parts.append(
                f'<line x1="{mx:.1f}" y1="{TOP}" x2="{mx:.1f}" y2="{bottom}" '
                f'stroke="{EDGEHL}" stroke-width="1" stroke-dasharray="3 3" '
                f'opacity="0.7"/>'
            )
            parts.append(
                f'<text x="{mx:.1f}" y="{bottom + 18}" fill="{EDGEHL}" '
                f'font-size="11" font-weight="700" text-anchor="middle">{text}</text>'
            )

    parts.append("</svg>")

    with open(out_path, "w") as f:
        f.write("\n".join(parts))
    return width, height


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--vcd", default="docs/waveforms/sim_waves.vcd")
    ap.add_argument("--out", default="docs/waveforms/sim_waves.svg")
    ap.add_argument("--start", type=int, default=None)
    ap.add_argument("--end", type=int, default=None)
    args = ap.parse_args()

    _, snapshots = parse_vcd(args.vcd)
    if not snapshots:
        print(f"ERROR: no samples parsed from {args.vcd}", file=sys.stderr)
        return 1

    if args.start is not None and args.end is not None:
        start, end = args.start, args.end
    else:
        start, end = auto_window(snapshots)

    w, h = render_svg(snapshots, start, end, args.out)
    print(f"Wrote {args.out}  ({w}x{h} px)  window {start}-{end} ps  "
          f"from {len(snapshots)} VCD samples")
    return 0


if __name__ == "__main__":
    sys.exit(main())
