#!/usr/bin/env python3
"""
visualize_slowstart.py — RUDP Congestion Control Visualizer
============================================================

Two modes
---------
  python visualize_slowstart.py          # simulation (no Docker required)
  python visualize_slowstart.py --live   # parse logs from running containers

Output: screenshots/wireshark_slowstart.png
        (also shown interactively unless --no-show is passed)

Simulation parameters mirror RUDP/rudp_socket.py exactly:
  INIT_CWND=1, INIT_SSTHRESH=32, WINDOW_CAP=32, DUP_ACK_THRESH=3
  Slow start: cwnd += 1 per ACK  (doubles each RTT)
  AIMD:       cwnd += 1/cwnd per ACK  (+1 MSS / RTT)
  RTO:        ssthresh = max(cwnd/2, 1), cwnd = 1
  Fast retransmit/recovery: ssthresh = max(cwnd/2, 1), cwnd = ssthresh
"""
from __future__ import annotations
import argparse
import os
import pathlib
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# ── Optional dependency check ─────────────────────────────────────────────────
try:
    import matplotlib
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    from matplotlib.ticker import MaxNLocator
except ImportError:
    sys.exit("pip install matplotlib")

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT       = pathlib.Path(__file__).parent
SHOTS_DIR  = ROOT / "screenshots"
OUT_FILE   = SHOTS_DIR / "wireshark_slowstart.png"
SHOTS_DIR.mkdir(exist_ok=True)

# ── RUDP constants (must match rudp_socket.py) ────────────────────────────────
INIT_CWND     = 1.0
INIT_SSTHRESH = 32.0
WINDOW_CAP    = 32.0
DUP_ACK_THRESH = 3

# ── Dark presentation theme (matches build_pptx.py palette) ──────────────────
BG        = "#0D1B2A"
GRID      = "#1E3A5F"
CWND_CLR  = "#00B4FF"
THRESH_CLR = "#FF6B35"
SS_FILL   = "#00B4FF22"
CA_FILL   = "#00FF8822"
RTO_CLR   = "#FF4444"
FR_CLR    = "#FFD700"
TEXT_CLR  = "#E8F4FD"
ANNO_CLR  = "#FFFFFF"


# ══════════════════════════════════════════════════════════════════════════════
#  SIMULATION ENGINE
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Event:
    kind: str          # "ack", "rto", "fr"
    rtt:  int          # x-axis position (RTT index)
    cwnd_before: float
    cwnd_after:  float
    ssthresh:    float
    label:       str = ""


@dataclass
class SimState:
    cwnd:          float = INIT_CWND
    ssthresh:      float = INIT_SSTHRESH
    dup_ack_count: int   = 0
    rtt:           int   = 0
    events:        List[Event] = field(default_factory=list)

    # time series
    rtt_series:     List[int]   = field(default_factory=list)
    cwnd_series:    List[float] = field(default_factory=list)
    thresh_series:  List[float] = field(default_factory=list)
    phase_series:   List[str]   = field(default_factory=list)   # "SS" or "CA"

    def record(self):
        self.rtt_series.append(self.rtt)
        self.cwnd_series.append(self.cwnd)
        self.thresh_series.append(self.ssthresh)
        self.phase_series.append("SS" if self.cwnd < self.ssthresh else "CA")

    def ack_rtt(self, n_pkts: Optional[int] = None):
        """One full RTT of ACKs received. n_pkts = cwnd (all in-flight ACKed)."""
        if n_pkts is None:
            n_pkts = int(self.cwnd)
        before = self.cwnd
        for _ in range(n_pkts):
            if self.cwnd < self.ssthresh:
                self.cwnd += 1.0
            else:
                self.cwnd += 1.0 / self.cwnd
        self.cwnd = min(self.cwnd, WINDOW_CAP)
        self.rtt += 1
        self.events.append(Event("ack", self.rtt, before, self.cwnd, self.ssthresh))
        self.record()

    def rto(self, label: str = "Timeout (packet loss)"):
        before = self.cwnd
        self.ssthresh = max(self.cwnd / 2.0, 1.0)
        self.cwnd     = INIT_CWND
        self.events.append(Event("rto", self.rtt, before, self.cwnd,
                                  self.ssthresh, label))
        self.record()

    def fast_retransmit(self, label: str = "Fast Retransmit (3× dup-ACK)"):
        before = self.cwnd
        self.ssthresh = max(self.cwnd / 2.0, 1.0)
        self.cwnd     = self.ssthresh
        self.events.append(Event("fr", self.rtt, before, self.cwnd,
                                  self.ssthresh, label))
        self.record()


def run_simulation() -> SimState:
    """
    Simulate a representative RUDP session:
      Phase 1  — Slow Start from cwnd=1 until ssthresh (32)  [RTTs 0-5]
      Phase 2  — AIMD congestion avoidance                    [RTTs 5-9]
      Phase 3  — Timeout (packet loss)                        [RTT 10]
      Phase 4  — Slow Start restart                           [RTTs 10-13]
      Phase 5  — AIMD again                                   [RTTs 13-17]
      Phase 6  — Fast Retransmit (3 dup-ACKs)                 [RTT 18]
      Phase 7  — Fast Recovery → AIMD                         [RTTs 18-22]
    """
    s = SimState()
    s.record()  # t=0 initial state

    # ── Phase 1: Slow start until ssthresh ───────────────────────────────────
    while s.cwnd < s.ssthresh and s.rtt < 6:
        s.ack_rtt()

    # ── Phase 2: AIMD for a few RTTs ─────────────────────────────────────────
    for _ in range(5):
        s.ack_rtt()

    # ── Phase 3: Timeout ──────────────────────────────────────────────────────
    s.rto()

    # ── Phase 4: Slow start again ─────────────────────────────────────────────
    while s.cwnd < s.ssthresh and s.rtt < 20:
        s.ack_rtt()

    # ── Phase 5: AIMD ─────────────────────────────────────────────────────────
    for _ in range(5):
        s.ack_rtt()

    # ── Phase 6: Fast retransmit ──────────────────────────────────────────────
    s.fast_retransmit()

    # ── Phase 7: Fast recovery → AIMD ────────────────────────────────────────
    for _ in range(5):
        s.ack_rtt()

    return s


# ══════════════════════════════════════════════════════════════════════════════
#  LIVE LOG PARSER
# ══════════════════════════════════════════════════════════════════════════════

_RE_NEW_ACK = re.compile(
    r"New ACK ×(\d+)\s+cwnd=([\d.]+)\s+ssthresh=([\d.]+)\s+phase=(\w+)")
_RE_RTO     = re.compile(r"RTO\s+cwnd→([\d.]+)\s+ssthresh→([\d.]+)")
_RE_FR      = re.compile(r"FR\s+cwnd→([\d.]+)\s+ssthresh→([\d.]+)")


def get_server_containers() -> List[str]:
    try:
        out = subprocess.check_output(
            ["docker", "ps", "--filter", "name=rudp", "--format", "{{.Names}}"],
            text=True, timeout=5)
        names = [l.strip() for l in out.splitlines() if l.strip()]
        if not names:
            # fallback: any container with exam in name
            out2 = subprocess.check_output(
                ["docker", "ps", "--filter", "name=exam", "--format", "{{.Names}}"],
                text=True, timeout=5)
            names = [l.strip() for l in out2.splitlines() if l.strip()]
        return names
    except Exception as e:
        print(f"[warn] docker ps failed: {e}")
        return []


def enable_slowsend(containers: List[str], delay_ms: int = 150) -> None:
    """Write /tmp/rudp_slowsend.json into each server container."""
    payload = f'{{"delay_ms": {delay_ms}}}'
    for cname in containers:
        try:
            subprocess.run(
                ["docker", "exec", cname,
                 "sh", "-c", f"echo '{payload}' > /tmp/rudp_slowsend.json"],
                check=True, timeout=5)
            print(f"  [slowsend] {cname}: delay_ms={delay_ms}")
        except Exception as e:
            print(f"  [warn] could not set slowsend on {cname}: {e}")


def disable_slowsend(containers: List[str]) -> None:
    for cname in containers:
        try:
            subprocess.run(
                ["docker", "exec", cname,
                 "sh", "-c", "rm -f /tmp/rudp_slowsend.json"],
                check=True, timeout=5)
        except Exception:
            pass


def collect_logs(containers: List[str], since_s: int = 60) -> str:
    logs = []
    for cname in containers:
        try:
            out = subprocess.check_output(
                ["docker", "logs", "--since", f"{since_s}s", cname],
                text=True, stderr=subprocess.STDOUT, timeout=10)
            logs.append(out)
        except Exception as e:
            print(f"  [warn] could not get logs from {cname}: {e}")
    return "\n".join(logs)


def parse_live_logs(raw: str) -> SimState:
    """
    Parse rudp_socket log lines into a SimState time series.
    Requires DEBUG logging in the container (set LOG_LEVEL=DEBUG env var
    or restart server with logging.basicConfig(level=logging.DEBUG)).
    Falls back to INFO-only events (RTO / FR) if DEBUG lines are absent.
    """
    s = SimState()
    s.record()

    for line in raw.splitlines():
        m = _RE_NEW_ACK.search(line)
        if m:
            n  = int(m.group(1))
            cw = float(m.group(2))
            th = float(m.group(3))
            ph = m.group(4)
            s.rtt += 1
            s.cwnd     = cw
            s.ssthresh = th
            s.events.append(Event("ack", s.rtt, 0, cw, th))
            s.record()
            continue

        m = _RE_RTO.search(line)
        if m:
            cw = float(m.group(1))
            th = float(m.group(2))
            s.rtt += 1
            before      = s.cwnd
            s.cwnd      = cw
            s.ssthresh  = th
            s.events.append(Event("rto", s.rtt, before, cw, th,
                                   "Timeout (packet loss)"))
            s.record()
            continue

        m = _RE_FR.search(line)
        if m:
            cw = float(m.group(1))
            th = float(m.group(2))
            s.rtt += 1
            before      = s.cwnd
            s.cwnd      = cw
            s.ssthresh  = th
            s.events.append(Event("fr", s.rtt, before, cw, th,
                                   "Fast Retransmit (3× dup-ACK)"))
            s.record()

    return s


# ══════════════════════════════════════════════════════════════════════════════
#  PLOT
# ══════════════════════════════════════════════════════════════════════════════

def _shade_phases(ax, rtt_series, phase_series, cwnd_series, thresh_series):
    """Fill SS / CA regions with translucent colour bands."""
    xs = rtt_series
    n  = len(xs)
    start = 0
    cur_phase = phase_series[0] if phase_series else "SS"
    for i in range(1, n + 1):
        ph = phase_series[i] if i < n else None
        if ph != cur_phase or i == n:
            x0, x1 = xs[start], xs[i - 1]
            clr = SS_FILL if cur_phase == "SS" else CA_FILL
            ax.axvspan(x0, x1, color=clr, zorder=0)
            if x1 > x0:
                mid = (x0 + x1) / 2
                lbl = "Slow Start" if cur_phase == "SS" else "Cong. Avoidance"
                ax.text(mid, ax.get_ylim()[1] * 0.92, lbl,
                        ha='center', va='top', fontsize=7,
                        color=CWND_CLR if cur_phase == "SS" else "#00FF88",
                        alpha=0.7, style='italic')
            if ph is not None:
                start = i
                cur_phase = ph


def plot(s: SimState, title_suffix: str = "", show: bool = True) -> None:
    matplotlib.rcParams.update({
        'figure.facecolor':  BG,
        'axes.facecolor':    BG,
        'axes.edgecolor':    GRID,
        'axes.labelcolor':   TEXT_CLR,
        'xtick.color':       TEXT_CLR,
        'ytick.color':       TEXT_CLR,
        'grid.color':        GRID,
        'text.color':        TEXT_CLR,
        'font.family':       'monospace',
    })

    xs  = s.rtt_series
    cws = s.cwnd_series
    ths = s.thresh_series

    fig, ax = plt.subplots(figsize=(13, 6))
    fig.patch.set_facecolor(BG)

    # ── Phase background ──────────────────────────────────────────────────────
    # We call _shade_phases after setting ylim
    ax.set_ylim(-0.5, WINDOW_CAP + 4)
    _shade_phases(ax, xs, s.phase_series, cws, ths)

    # ── ssthresh line ─────────────────────────────────────────────────────────
    ax.step(xs, ths, color=THRESH_CLR, linewidth=1.5,
            linestyle='--', where='post', label='ssthresh', zorder=3)

    # ── cwnd staircase ────────────────────────────────────────────────────────
    ax.step(xs, cws, color=CWND_CLR, linewidth=2.5,
            where='post', label='cwnd', zorder=4)
    ax.plot(xs, cws, 'o', color=CWND_CLR, markersize=4, zorder=5)

    # ── WINDOW_CAP ceiling ────────────────────────────────────────────────────
    ax.axhline(WINDOW_CAP, color="#888888", linewidth=1,
               linestyle=':', alpha=0.6, label=f'WINDOW_CAP={int(WINDOW_CAP)}')

    # ── Annotate events ───────────────────────────────────────────────────────
    for ev in s.events:
        if ev.kind == "rto":
            ax.axvline(ev.rtt, color=RTO_CLR, linewidth=1.5,
                       linestyle='-', alpha=0.8, zorder=3)
            ax.annotate(
                f"RTO\nssthresh→{ev.ssthresh:.0f}\ncwnd→{ev.cwnd_after:.0f}",
                xy=(ev.rtt, ev.cwnd_after),
                xytext=(ev.rtt + 0.4, min(ev.cwnd_before * 0.7, WINDOW_CAP - 4)),
                color=RTO_CLR,
                fontsize=8,
                arrowprops=dict(arrowstyle='->', color=RTO_CLR, lw=1.2),
                bbox=dict(boxstyle='round,pad=0.3', facecolor=BG,
                          edgecolor=RTO_CLR, alpha=0.9),
            )

        elif ev.kind == "fr":
            ax.axvline(ev.rtt, color=FR_CLR, linewidth=1.5,
                       linestyle='-', alpha=0.8, zorder=3)
            ax.annotate(
                f"Fast Retransmit\nssthresh→{ev.ssthresh:.0f}\ncwnd→{ev.cwnd_after:.0f}",
                xy=(ev.rtt, ev.cwnd_after),
                xytext=(ev.rtt + 0.4, min(ev.cwnd_before * 0.6, WINDOW_CAP - 4)),
                color=FR_CLR,
                fontsize=8,
                arrowprops=dict(arrowstyle='->', color=FR_CLR, lw=1.2),
                bbox=dict(boxstyle='round,pad=0.3', facecolor=BG,
                          edgecolor=FR_CLR, alpha=0.9),
            )

    # ── Labels ────────────────────────────────────────────────────────────────
    ax.set_xlabel("RTT (round-trip)", fontsize=11)
    ax.set_ylabel("Window size (packets)", fontsize=11)
    title = "RUDP Congestion Control — Slow Start → AIMD → Recovery"
    if title_suffix:
        title += f"  [{title_suffix}]"
    ax.set_title(title, fontsize=13, fontweight='bold', color=ANNO_CLR, pad=12)

    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.grid(True, linestyle='--', alpha=0.4)

    # ── Legend ────────────────────────────────────────────────────────────────
    patches = [
        mpatches.Patch(color=CWND_CLR,   label="cwnd (congestion window)"),
        mpatches.Patch(color=THRESH_CLR, label="ssthresh (slow-start threshold)"),
        mpatches.Patch(color=RTO_CLR,    label="RTO — timeout, cwnd→1"),
        mpatches.Patch(color=FR_CLR,     label="Fast Retransmit — cwnd→ssthresh"),
        mpatches.Patch(color=SS_FILL,    label="Slow Start phase"),
        mpatches.Patch(color=CA_FILL,    label="AIMD / Cong. Avoidance phase"),
    ]
    ax.legend(handles=patches, loc='upper right',
              facecolor="#0a1520", edgecolor=GRID,
              labelcolor=TEXT_CLR, fontsize=8)

    # ── Equation box ─────────────────────────────────────────────────────────
    eq = (
        "Slow Start:  cwnd += 1 / ACK\n"
        "AIMD:        cwnd += 1/cwnd / ACK\n"
        "RTO:         ssthresh = cwnd/2 ; cwnd = 1\n"
        "Fast Ret.:   ssthresh = cwnd/2 ; cwnd = ssthresh"
    )
    ax.text(0.01, 0.03, eq, transform=ax.transAxes,
            fontsize=7.5, verticalalignment='bottom',
            color="#AACCEE", family='monospace',
            bbox=dict(boxstyle='round', facecolor='#0a1520',
                      edgecolor=GRID, alpha=0.85))

    plt.tight_layout()
    fig.savefig(str(OUT_FILE), dpi=150, bbox_inches='tight',
                facecolor=BG)
    print(f"  [saved] {OUT_FILE}")

    if show:
        plt.show()
    plt.close()


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Visualize RUDP congestion control (slow start → AIMD)")
    parser.add_argument("--live",     action="store_true",
                        help="Collect logs from running Docker containers")
    parser.add_argument("--slowsend", type=int, default=150, metavar="MS",
                        help="inter-batch delay written to containers (default 150 ms)")
    parser.add_argument("--wait",     type=int, default=30, metavar="SEC",
                        help="seconds to wait for live traffic (default 30)")
    parser.add_argument("--no-show",  action="store_true",
                        help="Do not open interactive window")
    args = parser.parse_args()

    show = not args.no_show

    if args.live:
        print("=== LIVE MODE ===")
        containers = get_server_containers()
        if not containers:
            print("[warn] No running RUDP/exam containers found via 'docker ps'.")
            print("       Falling back to simulation.")
            s = run_simulation()
            plot(s, "simulation", show)
            return

        print(f"Found containers: {containers}")
        print(f"Enabling slowsend ({args.slowsend} ms) to make staircase visible...")
        enable_slowsend(containers, args.slowsend)

        print(f"Waiting {args.wait}s for traffic (run an exam session now)...")
        try:
            for remaining in range(args.wait, 0, -5):
                print(f"  {remaining}s remaining...", end="\r", flush=True)
                time.sleep(5)
        except KeyboardInterrupt:
            print("\n  (interrupted early)")

        print("\nCollecting logs...")
        raw = collect_logs(containers, since_s=args.wait + 10)

        print("Disabling slowsend...")
        disable_slowsend(containers)

        s = parse_live_logs(raw)
        if len(s.rtt_series) < 3:
            print("[warn] Not enough log data parsed (is DEBUG logging enabled?).")
            print("       To enable DEBUG: restart servers with LOG_LEVEL=DEBUG env var.")
            print("       Falling back to simulation.")
            s = run_simulation()
            title = "simulation (fallback — enable DEBUG logging for live data)"
        else:
            title = f"live — {len(s.rtt_series)} ACK events"

        plot(s, title, show)

    else:
        print("=== SIMULATION MODE ===")
        print("Simulating: Slow Start -> AIMD -> Timeout -> Recovery -> Fast Retransmit")
        s = run_simulation()

        # Print summary table
        print(f"\n{'RTT':>5}  {'cwnd':>8}  {'ssthresh':>10}  {'phase':>4}")
        print("-" * 36)
        for i, (r, c, t, ph) in enumerate(zip(
                s.rtt_series, s.cwnd_series, s.thresh_series, s.phase_series)):
            marker = ""
            for ev in s.events:
                if ev.rtt == r and ev.kind != "ack":
                    marker = f"  << {ev.kind.upper()}"
            print(f"{r:>5}  {c:>8.2f}  {t:>10.2f}  {ph:>4}{marker}")

        plot(s, "simulation", show)


if __name__ == "__main__":
    main()
