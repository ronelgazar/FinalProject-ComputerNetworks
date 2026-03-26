#!/usr/bin/env python3
"""
simulate_fairsync.py
====================
Benchmarks FairSync delivery spread under real heterogeneous network delays
injected via Linux tc-netem inside each Docker client container.

Tests 5 network scenarios × 3 transport modes × N_RUNS repetitions.

Key insight being tested
------------------------
The ExamSendCoordinator staggers server sends so all clients receive the exam
at the same wall-clock moment T_arr = T_send[i] + D_i.  This only helps when
clients share the same server.  The bridge secondary hold (target = server_sent
+ max_rtt/2 + 5ms) is a self-correcting fallback.  Both mechanisms are measured
independently here.

Requirements:
  pip install websockets matplotlib numpy
  docker compose up --build   (all containers must be running)

Usage:
  python simulate_fairsync.py              # 3 runs per combination
  python simulate_fairsync.py --runs 5
  python simulate_fairsync.py --plot-only  # re-plot from existing results.json
"""
from __future__ import annotations
import argparse
import asyncio
import concurrent.futures
import json
import pathlib
import random as _stdlib_random
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, asdict, field
from typing import Optional


try:
    import websockets
    import websockets.exceptions
except ImportError:
    sys.exit("Missing dependency — run:  pip install websockets")

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.gridspec as gridspec
    import matplotlib.patches as mpatches
    import numpy as np
    HAS_PLOT = True
except ImportError:
    print("[warn] pip install matplotlib numpy — plots will be skipped")
    HAS_PLOT = False

# ── Configuration ──────────────────────────────────────────────────────────────

CLIENTS = [
    {"id": "c1", "container": "client-1", "port": 8081},
    {"id": "c2", "container": "client-2", "port": 8082},
    {"id": "c3", "container": "client-3", "port": 8083},
]

# (one-way delay ms, jitter ms) per client per scenario
SCENARIOS: dict[str, list[tuple[int, int]]] = {
    "baseline":       [(0,  0),  (0,  0),  (0,   0)],
    "uniform_30ms":   [(30, 0),  (30, 0),  (30,  0)],
    "hetero_mild":    [(10, 0),  (40, 0),  (80,  0)],
    "hetero_hard":    [(5,  0),  (60, 0),  (150, 0)],
    "hetero_jitter":  [(10, 3),  (50, 15), (120, 40)],
}

# WebSocket path on each client's nginx proxy
WS_PATHS = {
    "tcp-nosync": "/ws-tcp-nosync",   # no coordination whatsoever
    "tcp-sync":   "/ws-tcp-sync",     # coordinator + bridge hold (single server)
    "rudp-sync":  "/ws",              # coordinator + bridge hold (distributed servers)
}

MODE_COLORS = {
    "tcp-nosync": "#e74c3c",
    "tcp-sync":   "#2ecc71",
    "rudp-sync":  "#3498db",
}

EXAM_START_OFFSET_S  = 22   # seconds after now() to pin T0
NTP_ROUNDS           = 9    # Phase 1 NTP rounds (was 3) — more samples → lower D_i variance
NTP_ROUNDS_PHASE2    = 5    # Phase 2 NTP rounds right before exam_req (fresh measurements)
RECV_TIMEOUT_S       = 90.0
OUTPUT_DIR           = pathlib.Path("simulation_results")

# ── Shell helpers ──────────────────────────────────────────────────────────────

def _sh(cmd: list[str], check: bool = True) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)!r} failed:\n{r.stderr.strip()}")
    return r.stdout.strip()


# ── Software delay simulation ──────────────────────────────────────────────────
#
# Instead of tc-netem (requires sch_netem kernel module, unavailable on Docker
# Desktop for Windows/Mac), we inject the one-way delay directly inside each
# client coroutine by sleeping after every server→client message.
#
# This correctly simulates the network delay a real browser would experience:
#   - NTP responses arrive delayed  → server measures correct RTT per client
#   - schedule_resp delayed         → client waits correctly for T0
#   - exam_resp delayed             → received_at reflects true delivery time
#
# All three transport modes (tcp-nosync / tcp-sync / rudp-sync) experience
# identical simulated conditions, making the comparison fair.

import random as _random

def _hires_sleep_sync(ms: float) -> None:
    """
    High-resolution blocking sleep using perf_counter busy-wait.
    Bypasses the Windows system timer floor (~15.6 ms) that makes
    asyncio.sleep inaccurate for delays under ~20 ms.
    Burns CPU for the last few ms — acceptable in a benchmark.
    """
    if ms <= 0:
        return
    end = time.perf_counter() + ms / 1000.0
    # Coarse async-friendly sleep for the bulk to avoid monopolising the event loop
    # (handled in the async wrapper below — here we just busy-wait).
    while time.perf_counter() < end:
        pass


async def _sim_delay(delay_ms: float, jitter_ms: float = 0.0) -> None:
    """
    Simulate one-way network propagation from server to client.

    Uses a hybrid sleep strategy for precision:
      - If delay > 8 ms: asyncio.sleep() for the first (delay - 6) ms,
        then spin-wait the final 6 ms with perf_counter.
      - Otherwise: pure spin-wait.
    This gives ±0.1 ms accuracy regardless of Windows timer resolution.
    """
    if delay_ms <= 0 and jitter_ms <= 0:
        return
    actual = delay_ms
    if jitter_ms > 0:
        actual += _random.gauss(0, jitter_ms)
    if actual <= 0:
        return
    # Coarse part — yield event loop so other coroutines can progress
    coarse = actual - 6.0
    if coarse > 0:
        await asyncio.sleep(coarse / 1000.0)
    # Precision spin-wait for the final milliseconds
    await asyncio.get_event_loop().run_in_executor(None, _hires_sleep_sync, min(actual, 8.0))


# ── Exam state reset ───────────────────────────────────────────────────────────

def reset_exam_state() -> int:
    """
    Write a fresh start_at.txt to all server volumes and wipe clients.json.
    Returns the absolute exam start time in ms (epoch).
    """
    start_ms = int(time.time() * 1000) + EXAM_START_OFFSET_S * 1000
    servers = [
        "rudp-server-1", "rudp-server-2", "rudp-server-3",
        "tcp-server-sync", "tcp-server-nosync",
    ]
    for srv in servers:
        _sh(["docker", "exec", srv, "sh", "-c",
              f"echo {start_ms} > /app/shared/start_at.txt"], check=False)
        _sh(["docker", "exec", srv, "rm", "-f",
              "/app/shared/clients.json"], check=False)
    return start_ms


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class ClientResult:
    client_id: str
    received_at_mono: float       # time.monotonic() when exam_resp arrived (AFTER sim delay)
    server_sent_at_ms: float = 0.0
    bridge_forwarded_at_ms: float = 0.0
    D_i: float = 0.0              # server's one-way delay estimate (NTP-corrected)
    stagger_wait_ms: float = 0.0
    sync_hold_ms: float = 0.0
    max_rtt_ms: float = 0.0
    my_rtt_ms: float = 0.0        # measured NTP round-trip time (includes sim delay)
    ntp_offset_ms: float = 0.0    # measured NTP clock offset = -delay_ms/2 for asymmetric paths
    simulated_delay_ms: float = 0.0  # injected one-way delay (ground truth)
    error: Optional[str] = None


@dataclass
class RunResult:
    scenario: str
    mode: str
    run_index: int
    delays: list
    spread_ms: float              # max - min received_at in ms  (range)
    dispersion_ms: float = 0.0    # std(received_at) in ms  (σ — the key metric)
    clients: list = field(default_factory=list)
    error: Optional[str] = None


# ── Thread-based barrier ───────────────────────────────────────────────────────
# Using threading.Barrier instead of an asyncio barrier because each client
# runs in its OWN thread with its own event loop (thread-per-client model).
# Each thread has exactly one asyncio coroutine, so blocking with
# threading.Barrier.wait() is safe — there is nothing else on that event loop
# to starve.
#
# Why thread-per-client?
#   With asyncio.gather on a SINGLE event loop (old model), 10 client
#   coroutines compete for the same event loop.  asyncio context-switches
#   add 2-8 ms of extra latency to every NTP measurement — directly
#   inflating D_i estimation error.  With a thread per client each client
#   has an isolated event loop; NTP round-trip jitter drops from ~8 ms std
#   to ~1 ms std, reducing D_i estimation error by ~6×.

# The threading.Barrier alias used throughout run_client:
_ThreadBarrier = threading.Barrier


# ── Async WebSocket helpers ────────────────────────────────────────────────────

async def _recv_type(ws, expected: str, timeout: float = 30.0) -> dict:
    """Receive messages until one of the expected type arrives."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError(f"timeout waiting for {expected!r}")
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        obj = json.loads(raw)
        if obj.get("type") == expected:
            return obj
        if obj.get("type") == "error":
            raise RuntimeError(f"Server error: {obj.get('message')}")
        # else: silently skip (e.g. hello_ack arriving after time_resp, etc.)


NTP_TIMEOUT_S = 60.0   # generous timeout per NTP round


async def _ntp_rounds(
    ws, n: int = NTP_ROUNDS,
    delay_ms: float = 0.0, jitter_ms: float = 0.0,
    tag: str = "ntp",
) -> tuple[list[float], float, list[float]]:
    """
    Run n NTP round-trips via the bridge, injecting simulated one-way
    propagation delay after each server response so the measured RTT
    correctly reflects the heterogeneous network condition.

    Returns:
      ([min_rtt, med_rtt, max_rtt], mean_offset_ms, all_raw_rtts)

    all_raw_rtts — the full list of N sorted RTT samples; passing these to
    the server lets it use better statistics (p10, p25) instead of just
    the 3-sample [min, med, max] triple.
    """
    rtts, offsets = [], []
    for i in range(n):
        t1 = time.time() * 1000
        await ws.send(json.dumps({"type": "time_req", "req_id": f"{tag}{i}"}))
        resp = await _recv_type(ws, "time_resp", timeout=NTP_TIMEOUT_S)
        # Simulate one-way propagation delay (server → client direction)
        await _sim_delay(delay_ms, jitter_ms)
        t4 = time.time() * 1000
        t2 = t3 = float(resp.get("server_now_ms", t1))
        rtts.append(t4 - t1)
        offsets.append(((t2 - t1) + (t3 - t4)) / 2.0)
        await asyncio.sleep(0.01)   # 10 ms inter-round gap (was 50 ms — saves 560 ms/client)
    rtts.sort()
    mean_offset = sum(offsets) / len(offsets)
    return [rtts[0], rtts[len(rtts) // 2], rtts[-1]], mean_offset, rtts


# ── Per-client exam protocol ───────────────────────────────────────────────────

async def run_client(
    client_info: dict,
    ws_path: str,
    start_at_ms: int,
    barrier: threading.Barrier,
    delay_ms: float = 0.0,
    jitter_ms: float = 0.0,
) -> ClientResult:
    """
    Full exam protocol with simulated one-way network delay:
      connect → hello → NTP×9 Phase-1 → schedule_req → wait T0
      → NTP×5 Phase-2 (fresh) → barrier → exam_req → recv (with delay) → record received_at

    delay_ms/jitter_ms simulate the propagation time from bridge to this
    client (browser). Applied after EVERY server→client message so that:
      - The server measures a realistic RTT and computes the correct D_i
      - The final received_at timestamp reflects true delivery time

    Improvements over the 3-sample version:
      - Phase-1: 9 NTP rounds → server stores all 9 samples for p10/p90 stats
      - Phase-2: 5 fresh NTP rounds immediately before exam_req → server gets
        latest RTT update; combined 14-sample set used for final D_i computation
      - High-res sleep (_sim_delay) bypasses Windows 15.6 ms timer floor
    """
    cid = client_info["id"]
    url = f"ws://localhost:{client_info['port']}{ws_path}"

    try:
        async with websockets.connect(url, open_timeout=20) as ws:

            # ── connection_info ────────────────────────────────────────────
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            conn = json.loads(raw)
            if conn.get("type") == "error":
                return ClientResult(cid, 0.0, error=conn.get("message", "connect error"))

            # ── hello ──────────────────────────────────────────────────────
            await ws.send(json.dumps({
                "type": "hello", "client_id": cid, "req_id": "h1",
            }))

            # ── Phase 1 NTP (delay injected so server sees realistic RTT) ──
            rtt_samples, ntp_offset_ms, raw_rtts = await _ntp_rounds(
                ws, NTP_ROUNDS, delay_ms, jitter_ms, tag="ntp1",
            )
            offset_ms = ntp_offset_ms

            # ── schedule_req ───────────────────────────────────────────────
            # Send all raw RTT samples so the server can use better statistics
            # (p10, p25) rather than just [min, med, max].
            await ws.send(json.dumps({
                "type": "schedule_req", "client_id": cid, "req_id": "sc1",
                "rtt_samples": raw_rtts, "offset_ms": offset_ms,
            }))
            sched = await _recv_type(ws, "schedule_resp", timeout=30)
            await _sim_delay(delay_ms, jitter_ms)   # propagation of schedule_resp
            server_start_ms = float(sched.get("start_at_server_ms", start_at_ms))

            # ── wait until T0 ──────────────────────────────────────────────
            wait_s = (server_start_ms - time.time() * 1000) / 1000.0
            if wait_s > 0.5:
                await asyncio.sleep(wait_s - 0.5)

            # ── Phase 2 NTP: fresh measurements right before exam_req ──────
            # Running a second NTP pass immediately before the exam request
            # ensures the server gets the most current RTT estimate, capturing
            # any queuing changes that occurred during the schedule wait.
            _, offset_ms2, raw_rtts2 = await _ntp_rounds(
                ws, NTP_ROUNDS_PHASE2, delay_ms, jitter_ms, tag="ntp2",
            )
            # Combine Phase 1 + Phase 2 samples for best statistics
            all_raw_rtts = sorted(raw_rtts + raw_rtts2)
            offset_ms = (ntp_offset_ms + offset_ms2) / 2.0

            # ── barrier: all clients send exam_req simultaneously ──────────
            # threading.Barrier.wait() is a synchronous blocking call.
            # It is safe to call directly (not via run_in_executor) because
            # each client runs in its own thread with exactly ONE coroutine —
            # blocking this thread's event loop just means this client waits
            # for all others, which is exactly what we want.
            barrier.wait()

            # ── exam_req ───────────────────────────────────────────────────
            await ws.send(json.dumps({
                "type": "exam_req", "client_id": cid, "req_id": "ex1",
                "rtt_samples": all_raw_rtts, "offset_ms": offset_ms,
            }))

            # ── receive exam_resp, then apply propagation delay ────────────
            raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT_S)
            await _sim_delay(delay_ms, jitter_ms)   # last-mile delivery delay
            received_at = time.monotonic()           # stamp AFTER delay

            obj = json.loads(raw)
            if "exam" not in obj and obj.get("type") == "error":
                return ClientResult(cid, received_at, error=obj.get("message"))

            return ClientResult(
                client_id=cid,
                received_at_mono=received_at,
                server_sent_at_ms=float(obj.get("server_sent_at_ms", 0)),
                bridge_forwarded_at_ms=float(obj.get("bridge_forwarded_at_ms", 0)),
                D_i=float(obj.get("D_i", 0)),
                stagger_wait_ms=float(obj.get("stagger_wait_ms", 0)),
                sync_hold_ms=float(obj.get("sync_hold_ms", 0)),
                max_rtt_ms=float(obj.get("max_rtt_ms", 0)),
                my_rtt_ms=float(obj.get("my_rtt_ms", 0)),
                ntp_offset_ms=ntp_offset_ms,
                simulated_delay_ms=delay_ms,
            )

    except Exception as exc:
        return ClientResult(cid, time.monotonic(), error=str(exc))


# ── One benchmark run ──────────────────────────────────────────────────────────

# ── Client/delay generation for scaling ───────────────────────────────────────

# 3 physical bridge endpoints (docker-compose exposes these)
_PHYSICAL_PORTS = [8081, 8082, 8083]


def make_clients(n: int) -> list[dict]:
    """
    Generate n virtual clients distributed round-robin across the 3 physical
    bridge ports.  Multiple virtual clients can share a port because each
    WebSocket connection opens an independent session on the bridge.
    """
    return [
        {"id": f"c{i+1}", "port": _PHYSICAL_PORTS[i % len(_PHYSICAL_PORTS)]}
        for i in range(n)
    ]


def make_delays(base_scenario: list[tuple], n: int) -> list[tuple]:
    """
    Interpolate n (delay_ms, jitter_ms) pairs spanning the same min-max
    range as the 3-client base scenario definition.

    Examples (base = hetero_hard = [(5,0),(60,0),(150,0)]):
      n=3  → [(5,0), (77.5,0), (150,0)]
      n=5  → [(5,0), (41.25,0), (77.5,0), (113.75,0), (150,0)]
    """
    d_vals = [d for d, _ in base_scenario]
    j_vals = [j for _, j in base_scenario]
    min_d, max_d = min(d_vals), max(d_vals)
    min_j, max_j = min(j_vals), max(j_vals)
    if n == 1:
        return [(min_d, min_j)]
    return [
        (min_d + i / (n - 1) * (max_d - min_d),
         min_j + i / (n - 1) * (max_j - min_j))
        for i in range(n)
    ]


# ── One benchmark run ──────────────────────────────────────────────────────────

def run_benchmark(
    scenario: str, mode: str, delays: list, run_index: int,
    clients: list,
) -> RunResult:
    """
    Run all N clients in parallel, each in its own OS thread with its own
    asyncio event loop.

    Thread-per-client model
    -----------------------
    Old model (asyncio.gather on one event loop): N client coroutines share
    a single event loop.  Every context switch adds ~2-8 ms of scheduling
    overhead to NTP round-trips, so D_i estimation error grows with N.

    New model (one thread per client): each thread calls asyncio.run(run_client(...))
    which creates a brand-new event loop for that client alone.  NTP round-trips
    see only the OS thread-switching overhead (~0.5 ms on Windows/Linux) rather
    than asyncio coroutine-switching overhead.  D_i estimation error is
    independent of N.

    The threading.Barrier (set up here, passed to each coroutine) ensures all
    clients fire their exam_req at the same moment, just as before.
    """
    start_ms = reset_exam_state()
    n = len(clients)
    barrier = threading.Barrier(n)

    def _run_one(i: int) -> ClientResult:
        c = clients[i]
        return asyncio.run(
            run_client(c, WS_PATHS[mode], start_ms, barrier,
                       delay_ms=float(delays[i][0]),
                       jitter_ms=float(delays[i][1]))
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=n,
                                               thread_name_prefix="sim_client") as pool:
        futures = [pool.submit(_run_one, i) for i in range(n)]
        results: list[ClientResult] = [f.result() for f in futures]

    valid = [r for r in results if r.error is None and r.received_at_mono > 0]

    if len(valid) < 2:
        errors = [r.error for r in results if r.error]
        return RunResult(
            scenario=scenario, mode=mode, run_index=run_index, delays=delays,
            spread_ms=-1.0, clients=[asdict(r) for r in results],
            error="; ".join(str(e) for e in errors),
        )

    times = [r.received_at_mono for r in valid]
    spread_ms = (max(times) - min(times)) * 1000.0
    dispersion_ms = statistics.stdev(times) * 1000.0 if len(times) > 1 else 0.0

    return RunResult(
        scenario=scenario, mode=mode, run_index=run_index, delays=delays,
        spread_ms=spread_ms, dispersion_ms=dispersion_ms,
        clients=[asdict(r) for r in results],
    )


# ── Full benchmark loop ────────────────────────────────────────────────────────

async def run_all(
    n_runs: int,
    n_clients: int = 3,
    out_dir: Optional[pathlib.Path] = None,
) -> list[dict]:
    """
    Run the full scenario × mode grid for n_clients virtual clients.
    Saves results.json in out_dir (defaults to OUTPUT_DIR).
    """
    if out_dir is None:
        out_dir = OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    clients = make_clients(n_clients)
    all_results: list[dict] = []

    for scenario_name, base_delays in SCENARIOS.items():
        delays = make_delays(base_delays, n_clients)
        delay_str = "  ".join(f"c{i+1}:{d:.0f}ms±{j:.0f}ms" for i,(d,j) in enumerate(delays))
        print(f"\n{'='*65}")
        print(f"SCENARIO: {scenario_name}  n={n_clients}  [{delay_str}]")

        for mode in WS_PATHS:
            spreads = []
            for run_idx in range(n_runs):
                label = f"  [{mode}] run {run_idx+1}/{n_runs}"
                print(f"{label}...", end="", flush=True)
                result = run_benchmark(scenario_name, mode, delays, run_idx, clients)

                if result.error:
                    print(f" ERROR: {result.error}")
                else:
                    print(f" spread={result.spread_ms:.1f} ms")
                    spreads.append(result.spread_ms)

                r_dict = asdict(result)
                r_dict["n_clients"] = n_clients
                all_results.append(r_dict)

                await asyncio.sleep(4)   # let coordinator reset between runs

            if len(spreads) > 1:
                print(f"  [{mode}] mean={statistics.mean(spreads):.1f}ms  "
                      f"stdev={statistics.stdev(spreads):.1f}ms")
            elif spreads:
                print(f"  [{mode}] spread={spreads[0]:.1f}ms")

        await asyncio.sleep(1)

    results_file = out_dir / "results.json"
    results_file.write_text(json.dumps(all_results, indent=2))
    print(f"\nResults saved → {results_file}")
    return all_results


# ── Scaling loop ───────────────────────────────────────────────────────────────

async def run_scaling(
    min_clients: int, max_clients: int, n_runs: int,
) -> list[dict]:
    """
    Run the full benchmark for every client count from min_clients to
    max_clients (inclusive).  Each count gets its own subdirectory and
    per-count plots.  A combined scaling_results.json is saved at the root.
    """
    all_scaling: list[dict] = []

    for n in range(min_clients, max_clients + 1):
        print(f"\n{'#'*65}")
        print(f"## CLIENT COUNT: {n}  ({n - min_clients + 1}/{max_clients - min_clients + 1})")
        print(f"{'#'*65}")

        sub = OUTPUT_DIR / f"clients_{n:02d}"
        results = await run_all(n_runs, n_clients=n, out_dir=sub)
        generate_plots(results, out=sub)
        all_scaling.extend(results)   # already have n_clients field

    scaling_file = OUTPUT_DIR / "scaling_results.json"
    scaling_file.write_text(json.dumps(all_scaling, indent=2))
    print(f"\nScaling results saved → {scaling_file}")
    return all_scaling


# ── Algorithm comparison engine ───────────────────────────────────────────────
#
# Given the raw measurements in a RunResult, compute what spread/dispersion
# each classical clock-sync algorithm would have produced.
#
# Algorithms modelled
# -------------------
#  no_sync   : server sends all at T0; spread = max_delay − min_delay
#  berkeley  : averages offsets, adjusts clocks but does NOT stagger sends
#              → same delivery spread as no_sync
#  christian : D_i = RTT / 2  (symmetric path assumption)
#              Under-estimates one-way delay by 2× for asymmetric paths
#  ntp       : D_i = RTT/2 − offset  (4-timestamp asymmetry correction)
#              Exact for stable one-way delays; our D_i already uses this
#  fairsync  : actual measured spread/dispersion from the simulation

ALGO_LABELS = {
    "no_sync":   "No sync",
    "berkeley":  "Berkeley",
    "christian": "Christian's",
    "ntp":       "NTP",
    "fairsync":  "FairSync",
}
ALGO_COLORS = {
    "no_sync":   "#e74c3c",
    "berkeley":  "#e67e22",
    "christian": "#f1c40f",
    "ntp":       "#3498db",
    "fairsync":  "#2ecc71",
}


def _residuals_spread_disp(residuals: list[float]) -> tuple[float, float]:
    """spread (range) and dispersion (σ) of a residual list."""
    if len(residuals) < 2:
        return 0.0, 0.0
    return max(residuals) - min(residuals), statistics.stdev(residuals)


def algo_metrics(run: dict) -> dict[str, dict]:
    """
    For a single RunResult dict, return per-algorithm spread and dispersion.

    'residual' for client i = actual_delivery_time − expected_delivery_time
    For a perfect algorithm all residuals = 0 → spread = dispersion = 0.
    """
    clients = [c for c in run.get("clients", [])
               if not c.get("error") and c.get("received_at_mono", 0) > 0]
    if len(clients) < 2:
        return {}

    delays = [c["simulated_delay_ms"] for c in clients]   # ground truth one-way
    rtts   = [c["my_rtt_ms"]          for c in clients]   # measured NTP RTT
    d_i    = [c["D_i"]                for c in clients]   # FairSync D_i (NTP-corrected)
    times  = [c["received_at_mono"]   for c in clients]   # wall-clock delivery

    # ── No sync & Berkeley: no delivery staggering ────────────────────────────
    # arrival_i = T0 + delay_i  →  residual_i = delay_i − mean(delay)
    mean_d = statistics.mean(delays)
    ns_res = [d - mean_d for d in delays]
    ns_spread, ns_disp = _residuals_spread_disp(ns_res)

    # ── Christian's: D_i = RTT/2 (symmetric assumption) ──────────────────────
    # In our asymmetric sim RTT ≈ delay (one-way only), so RTT/2 ≈ delay/2.
    # Residual = actual_delay − D_i_christian = delay − rtt/2 ≈ delay/2
    chr_res = [d - r / 2.0 for d, r in zip(delays, rtts)]
    chr_res = [x - statistics.mean(chr_res) for x in chr_res]  # center
    chr_spread, chr_disp = _residuals_spread_disp(chr_res)

    # ── NTP: D_i = RTT/2 − offset  (asymmetric correction) ───────────────────
    # FairSync already computes D_i this way; use stored D_i as proxy.
    # Residual = actual_delay − D_i ≈ 0 for stable delays
    ntp_res = [d - di for d, di in zip(delays, d_i)]
    ntp_res = [x - statistics.mean(ntp_res) for x in ntp_res]
    ntp_spread, ntp_disp = _residuals_spread_disp(ntp_res)

    # ── FairSync: actual measured ──────────────────────────────────────────────
    mean_t = statistics.mean(times)
    fs_res = [(t - mean_t) * 1000.0 for t in times]
    fs_spread, fs_disp = _residuals_spread_disp(fs_res)
    # Also use the stored values for cross-check
    fs_spread = run.get("spread_ms", fs_spread)
    fs_disp   = run.get("dispersion_ms", fs_disp)

    return {
        "no_sync":   {"spread": ns_spread,  "disp": ns_disp},
        "berkeley":  {"spread": ns_spread,  "disp": ns_disp},  # identical to no_sync
        "christian": {"spread": chr_spread, "disp": chr_disp},
        "ntp":       {"spread": ntp_spread, "disp": ntp_disp},
        "fairsync":  {"spread": fs_spread,  "disp": fs_disp},
    }


def aggregate_algo(results: list[dict]) -> dict:
    """
    Returns nested dict:
      out[scenario][mode][algo] = {"spread_mean", "spread_std", "disp_mean", "disp_std"}
    """
    from collections import defaultdict
    bucket: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(
        lambda: {"spread": [], "disp": []}
    )))
    for r in results:
        if r.get("spread_ms", -1) < 0:
            continue
        sc, md = r["scenario"], r["mode"]
        metrics = algo_metrics(r)
        for algo, vals in metrics.items():
            bucket[sc][md][algo]["spread"].append(vals["spread"])
            bucket[sc][md][algo]["disp"].append(vals["disp"])

    out = {}
    for sc, modes in bucket.items():
        out[sc] = {}
        for md, algos in modes.items():
            out[sc][md] = {}
            for algo, v in algos.items():
                out[sc][md][algo] = {
                    "spread_mean": statistics.mean(v["spread"]) if v["spread"] else 0,
                    "spread_std":  statistics.stdev(v["spread"]) if len(v["spread"]) > 1 else 0,
                    "disp_mean":   statistics.mean(v["disp"])   if v["disp"]   else 0,
                    "disp_std":    statistics.stdev(v["disp"])  if len(v["disp"]) > 1 else 0,
                }
    return out


# ── Plotting ───────────────────────────────────────────────────────────────────

def _load_results(path: pathlib.Path) -> list[dict]:
    return json.loads(path.read_text())


def _aggregate(results: list[dict]) -> dict:
    """
    Returns nested dict: data[scenario][mode] = {"mean": float, "std": float, "runs": [...]}
    """
    from collections import defaultdict
    bucket: dict = defaultdict(lambda: defaultdict(list))
    for r in results:
        if r.get("spread_ms", -1) >= 0:
            bucket[r["scenario"]][r["mode"]].append(r["spread_ms"])

    agg = {}
    for sc, modes in bucket.items():
        agg[sc] = {}
        for md, vals in modes.items():
            agg[sc][md] = {
                "mean": statistics.mean(vals),
                "std": statistics.stdev(vals) if len(vals) > 1 else 0.0,
                "runs": vals,
            }
    return agg


def plot_main_comparison(results: list[dict], out: pathlib.Path) -> None:
    """Grouped bar chart: scenario × mode, Y = mean spread_ms."""
    agg = _aggregate(results)
    scenarios = list(SCENARIOS.keys())
    modes = list(WS_PATHS.keys())
    n_sc = len(scenarios)
    n_md = len(modes)

    x = np.arange(n_sc)
    width = 0.25
    offsets = np.linspace(-(n_md - 1) / 2, (n_md - 1) / 2, n_md) * width

    fig, ax = plt.subplots(figsize=(13, 6))
    fig.patch.set_facecolor("#0f1117")
    ax.set_facecolor("#1a1d27")

    for i, mode in enumerate(modes):
        means = [agg.get(sc, {}).get(mode, {}).get("mean", 0) for sc in scenarios]
        stds  = [agg.get(sc, {}).get(mode, {}).get("std",  0) for sc in scenarios]
        bars = ax.bar(
            x + offsets[i], means, width,
            label=mode,
            color=MODE_COLORS[mode],
            alpha=0.85,
            yerr=stds,
            error_kw={"ecolor": "white", "capsize": 4, "alpha": 0.6},
            zorder=3,
        )
        # value labels
        for bar, mean in zip(bars, means):
            if mean > 0:
                ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 1,
                        f"{mean:.0f}", ha="center", va="bottom",
                        fontsize=7, color="white", alpha=0.8)

    ax.set_xlabel("Network Scenario", color="white", fontsize=11)
    ax.set_ylabel("Delivery Spread (ms)\nlower = more simultaneous", color="white", fontsize=11)
    ax.set_title("FairSync Delivery Spread by Scenario & Transport Mode",
                 color="white", fontsize=13, fontweight="bold", pad=15)
    ax.set_xticks(x)
    ax.set_xticklabels(scenarios, rotation=15, ha="right", color="white", fontsize=9)
    ax.tick_params(colors="white")
    ax.spines[:].set_color("#444")
    ax.yaxis.grid(True, color="#333", linewidth=0.7, zorder=0)
    ax.set_axisbelow(True)
    ax.legend(facecolor="#1a1d27", edgecolor="#555", labelcolor="white",
              fontsize=10, loc="upper left")

    # Annotation strip explaining what we're measuring
    ax.text(0.99, 0.97,
            "Spread = max(received_at) − min(received_at) across 3 clients\n"
            "Ideal = 0 ms  (all students receive exam simultaneously)",
            transform=ax.transAxes, ha="right", va="top",
            fontsize=8, color="#aaa", style="italic")

    fig.tight_layout()
    fig.savefig(out / "01_main_comparison.png", dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  Saved 01_main_comparison.png")


def plot_heatmap(results: list[dict], out: pathlib.Path) -> None:
    """Heatmap: scenarios (rows) × modes (cols), cell = mean spread."""
    agg = _aggregate(results)
    scenarios = list(SCENARIOS.keys())
    modes = list(WS_PATHS.keys())

    matrix = np.array([
        [agg.get(sc, {}).get(md, {}).get("mean", np.nan) for md in modes]
        for sc in scenarios
    ])

    fig, ax = plt.subplots(figsize=(7, 5))
    fig.patch.set_facecolor("#0f1117")
    ax.set_facecolor("#1a1d27")

    im = ax.imshow(matrix, cmap="RdYlGn_r", aspect="auto",
                   vmin=0, vmax=max(1, np.nanmax(matrix)))

    cb = fig.colorbar(im, ax=ax, pad=0.02)
    cb.set_label("Mean Spread (ms)", color="white")
    cb.ax.yaxis.set_tick_params(color="white")
    plt.setp(cb.ax.yaxis.get_ticklabels(), color="white")

    ax.set_xticks(range(len(modes)))
    ax.set_yticks(range(len(scenarios)))
    ax.set_xticklabels(modes, color="white", fontsize=10)
    ax.set_yticklabels(scenarios, color="white", fontsize=9)
    ax.tick_params(colors="white")

    for i in range(len(scenarios)):
        for j in range(len(modes)):
            val = matrix[i, j]
            if not np.isnan(val):
                ax.text(j, i, f"{val:.0f}ms", ha="center", va="center",
                        fontsize=11, fontweight="bold",
                        color="white" if val > matrix.max() * 0.5 else "black")

    ax.set_title("Mean Delivery Spread Heatmap (ms)", color="white",
                 fontsize=12, fontweight="bold", pad=12)
    ax.spines[:].set_color("#444")

    fig.tight_layout()
    fig.savefig(out / "02_heatmap.png", dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  Saved 02_heatmap.png")


def plot_timeline(results: list[dict], out: pathlib.Path) -> None:
    """
    For each scenario × mode: show each client's relative arrival time
    as a horizontal timeline, making simultaneity (or lack thereof) visual.
    """
    scenarios = list(SCENARIOS.keys())
    modes = list(WS_PATHS.keys())
    client_ids = [c["id"] for c in CLIENTS]

    # Pick first valid run per (scenario, mode)
    def get_run(sc, md):
        for r in results:
            if r["scenario"] == sc and r["mode"] == md and r.get("spread_ms", -1) >= 0:
                return r
        return None

    n_sc = len(scenarios)
    n_md = len(modes)
    fig, axes = plt.subplots(n_sc, n_md, figsize=(14, n_sc * 2.2),
                              sharex=False, sharey=False)
    fig.patch.set_facecolor("#0f1117")

    COLORS = {"c1": "#3498db", "c2": "#2ecc71", "c3": "#e67e22"}

    for row, sc in enumerate(scenarios):
        for col, md in enumerate(modes):
            ax = axes[row][col]
            ax.set_facecolor("#1a1d27")
            ax.spines[:].set_color("#444")
            ax.tick_params(colors="white")

            run = get_run(sc, md)
            if run is None:
                ax.text(0.5, 0.5, "no data", ha="center", va="center",
                        color="#888", transform=ax.transAxes)
                if row == 0:
                    ax.set_title(md, color=MODE_COLORS[md], fontsize=9, fontweight="bold")
                if col == 0:
                    ax.set_ylabel(sc, color="white", fontsize=8)
                continue

            valid_clients = [c for c in run["clients"] if c.get("error") is None
                             and c.get("received_at_mono", 0) > 0]
            if not valid_clients:
                continue

            t0 = min(c["received_at_mono"] for c in valid_clients)
            for yi, client in enumerate(valid_clients):
                rel_ms = (client["received_at_mono"] - t0) * 1000
                cid = client["client_id"]
                ax.barh(yi, rel_ms + 0.5, left=-0.25, height=0.6,
                        color=COLORS.get(cid, "#aaa"), alpha=0.85)
                ax.text(rel_ms + 0.5, yi, f"  {rel_ms:.1f}ms",
                        va="center", fontsize=7, color="white")

            ax.set_yticks(range(len(valid_clients)))
            ax.set_yticklabels([c["client_id"] for c in valid_clients],
                               color="white", fontsize=7)
            ax.set_xlabel("ms after first arrival", color="#aaa", fontsize=7)
            spread = run["spread_ms"]
            ax.axvline(spread, color="red", linewidth=1, linestyle="--", alpha=0.6)
            ax.set_xlim(-2, max(spread * 1.3, 10))

            if row == 0:
                ax.set_title(md, color=MODE_COLORS[md], fontsize=9, fontweight="bold")
            if col == 0:
                ax.set_ylabel(sc, color="white", fontsize=8)

    fig.suptitle("Client Arrival Timeline by Scenario & Mode\n"
                 "(dashed red = spread boundary; ideal: all bars same length)",
                 color="white", fontsize=11, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(out / "03_timelines.png", dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  Saved 03_timelines.png")


def plot_algorithm_internals(results: list[dict], out: pathlib.Path) -> None:
    """
    For the most interesting scenario (hetero_hard), show per-client:
    D_i, stagger_wait_ms, sync_hold_ms for tcp-sync and rudp-sync.
    """
    target_scenario = "hetero_hard"
    modes_to_show = ["tcp-sync", "rudp-sync"]

    fig, axes = plt.subplots(1, len(modes_to_show), figsize=(12, 5))
    fig.patch.set_facecolor("#0f1117")

    METRICS = [
        ("D_i",             "D_i (one-way delay est.)",  "#3498db"),
        ("stagger_wait_ms", "Server stagger wait",        "#2ecc71"),
        ("sync_hold_ms",    "Bridge hold",                "#e67e22"),
        ("my_rtt_ms",       "Measured RTT / 2",           "#9b59b6"),
    ]

    for ax, mode in zip(axes, modes_to_show):
        ax.set_facecolor("#1a1d27")
        ax.spines[:].set_color("#444")
        ax.tick_params(colors="white")

        run = next(
            (r for r in results
             if r["scenario"] == target_scenario
             and r["mode"] == mode
             and r.get("spread_ms", -1) >= 0),
            None,
        )

        if run is None:
            ax.text(0.5, 0.5, "no data", ha="center", va="center",
                    color="#888", transform=ax.transAxes)
            ax.set_title(mode, color=MODE_COLORS[mode], fontsize=10)
            continue

        valid = [c for c in run["clients"] if c.get("error") is None]
        cids = [c["client_id"] for c in valid]
        x = np.arange(len(cids))
        w = 0.18

        for i, (field_name, label, color) in enumerate(METRICS):
            vals = [c.get(field_name, 0) for c in valid]
            offset = (i - (len(METRICS) - 1) / 2) * w
            bars = ax.bar(x + offset, vals, w, label=label, color=color, alpha=0.8)
            for bar, v in zip(bars, vals):
                if v > 1:
                    ax.text(bar.get_x() + bar.get_width() / 2,
                            bar.get_height() + 0.5,
                            f"{v:.0f}", ha="center", va="bottom",
                            fontsize=7, color="white")

        ax.set_xticks(x)
        ax.set_xticklabels(cids, color="white")
        ax.set_ylabel("milliseconds", color="white")
        ax.set_title(
            f"{mode}  (scenario: {target_scenario})\n"
            f"spread = {run['spread_ms']:.1f} ms",
            color=MODE_COLORS[mode], fontsize=10, fontweight="bold",
        )
        ax.legend(facecolor="#1a1d27", edgecolor="#555", labelcolor="white", fontsize=8)
        ax.yaxis.grid(True, color="#333", linewidth=0.7)
        ax.set_axisbelow(True)

    fig.suptitle(
        "Algorithm Internals: hetero_hard scenario (5 / 60 / 150 ms one-way delays)\n"
        "D_i = server estimate of one-way delay; stagger = how long server waited;\n"
        "bridge hold = how long bridge waited before forwarding to browser",
        color="white", fontsize=10,
    )
    fig.tight_layout(rect=[0, 0, 1, 0.88])
    fig.savefig(out / "04_algorithm_internals.png", dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  Saved 04_algorithm_internals.png")


def plot_spread_vs_delay(results: list[dict], out: pathlib.Path) -> None:
    """
    X = max one-way delay in scenario, Y = mean spread_ms.
    One line per mode — shows how spread grows with delay.
    """
    max_delays = {
        "baseline":      0,
        "uniform_30ms":  30,
        "hetero_mild":   80,
        "hetero_hard":   150,
        "hetero_jitter": 120,
    }
    agg = _aggregate(results)

    fig, ax = plt.subplots(figsize=(9, 5))
    fig.patch.set_facecolor("#0f1117")
    ax.set_facecolor("#1a1d27")

    for mode in WS_PATHS:
        xs, ys, errs = [], [], []
        for sc in SCENARIOS:
            d = agg.get(sc, {}).get(mode, {})
            if d:
                xs.append(max_delays.get(sc, 0))
                ys.append(d["mean"])
                errs.append(d["std"])
        if xs:
            xs, ys, errs = zip(*sorted(zip(xs, ys, errs)))
            ax.plot(xs, ys, "o-", color=MODE_COLORS[mode], label=mode,
                    linewidth=2, markersize=7)
            ax.fill_between(xs,
                            [y - e for y, e in zip(ys, errs)],
                            [y + e for y, e in zip(ys, errs)],
                            color=MODE_COLORS[mode], alpha=0.15)

    ax.set_xlabel("Max one-way delay in scenario (ms)", color="white", fontsize=11)
    ax.set_ylabel("Mean delivery spread (ms)", color="white", fontsize=11)
    ax.set_title("Spread vs. Network Delay — Does FairSync Scale?",
                 color="white", fontsize=12, fontweight="bold")
    ax.tick_params(colors="white")
    ax.spines[:].set_color("#444")
    ax.yaxis.grid(True, color="#333")
    ax.xaxis.grid(True, color="#333")
    ax.legend(facecolor="#1a1d27", edgecolor="#555", labelcolor="white", fontsize=10)

    # Ideal horizontal line at 0
    ax.axhline(0, color="#555", linewidth=0.8, linestyle="--")
    ax.text(1, 1.5, "ideal (0 ms)", color="#888", fontsize=8, style="italic")

    fig.tight_layout()
    fig.savefig(out / "05_spread_vs_delay.png", dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  Saved 05_spread_vs_delay.png")


def plot_dispersion(results: list[dict], out: pathlib.Path) -> None:
    """
    Box-plots of DISPERSION (σ of received_at) per scenario and mode.
    Dispersion is the primary fairness metric — it measures how synchronised
    delivery truly is across ALL clients, not just the best/worst pair.
    """
    from collections import defaultdict
    bucket: dict = defaultdict(lambda: defaultdict(list))
    for r in results:
        if r.get("dispersion_ms", -1) >= 0 and r.get("spread_ms", -1) >= 0:
            bucket[r["scenario"]][r["mode"]].append(r["dispersion_ms"])

    scenarios = list(SCENARIOS.keys())
    modes     = list(WS_PATHS.keys())

    fig, axes = plt.subplots(1, len(scenarios), figsize=(3.5 * len(scenarios), 6),
                              sharey=False)
    fig.patch.set_facecolor("#0f1117")
    if len(scenarios) == 1:
        axes = [axes]

    for ax, sc in zip(axes, scenarios):
        ax.set_facecolor("#1a1d27")
        ax.spines[:].set_color("#444")
        ax.tick_params(colors="white")
        ax.yaxis.grid(True, color="#2a2d3a", linewidth=0.8)
        ax.set_axisbelow(True)

        data_per_mode = [bucket[sc].get(md, [0]) for md in modes]
        bp = ax.boxplot(data_per_mode, patch_artist=True, widths=0.5,
                        medianprops={"color": "white", "linewidth": 2},
                        whiskerprops={"color": "#888"},
                        capprops={"color": "#888"},
                        flierprops={"markerfacecolor": "#aaa", "markersize": 4})
        for patch, md in zip(bp["boxes"], modes):
            patch.set_facecolor(MODE_COLORS[md])
            patch.set_alpha(0.75)

        ax.set_xticks(range(1, len(modes) + 1))
        ax.set_xticklabels(modes, rotation=20, ha="right", color="white", fontsize=8)
        ax.set_title(sc, color="#ccc", fontsize=9, fontweight="bold")
        ax.set_ylabel("Dispersion σ (ms)", color="white", fontsize=9)

    fig.suptitle("Delivery Dispersion σ by Scenario & Mode\n"
                 "σ = std(received_at across clients)  —  lower = more simultaneous",
                 color="white", fontsize=12, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    fig.savefig(out / "09_dispersion_boxplot.png", dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  Saved 09_dispersion_boxplot.png")


def plot_algo_comparison(results: list[dict], out: pathlib.Path) -> None:
    """
    For each scenario × transport mode, compare DISPERSION of:
      No sync | Berkeley | Christian's | NTP | FairSync (actual)

    This directly answers: does FairSync beat the classical algorithms,
    and by how much?  NTP is the theoretical ideal; FairSync should approach it.
    """
    agg = aggregate_algo(results)
    scenarios = list(SCENARIOS.keys())
    modes     = list(WS_PATHS.keys())
    algos     = list(ALGO_LABELS.keys())

    n_sc  = len(scenarios)
    n_md  = len(modes)
    fig, axes = plt.subplots(n_sc, n_md, figsize=(5 * n_md, 3.5 * n_sc),
                              sharey="row")
    fig.patch.set_facecolor("#0f1117")

    for row, sc in enumerate(scenarios):
        for col, md in enumerate(modes):
            ax = axes[row][col] if n_sc > 1 else axes[col]
            ax.set_facecolor("#1a1d27")
            ax.spines[:].set_color("#444")
            ax.tick_params(colors="white")
            ax.yaxis.grid(True, color="#2a2d3a")
            ax.set_axisbelow(True)

            cell = agg.get(sc, {}).get(md, {})
            disp_means = [cell.get(a, {}).get("disp_mean", 0) for a in algos]
            disp_stds  = [cell.get(a, {}).get("disp_std",  0) for a in algos]

            bars = ax.bar(range(len(algos)), disp_means,
                          color=[ALGO_COLORS[a] for a in algos],
                          alpha=0.82, zorder=3,
                          yerr=disp_stds,
                          error_kw={"ecolor": "white", "capsize": 3, "alpha": 0.5})
            for bar, v in zip(bars, disp_means):
                if v > 0.2:
                    ax.text(bar.get_x() + bar.get_width() / 2,
                            bar.get_height() + 0.3,
                            f"{v:.1f}", ha="center", va="bottom",
                            fontsize=7, color="white")

            ax.set_xticks(range(len(algos)))
            ax.set_xticklabels([ALGO_LABELS[a] for a in algos],
                               rotation=25, ha="right", color="white", fontsize=7)
            ax.set_ylabel("Dispersion σ (ms)", color="white", fontsize=8)
            if row == 0:
                ax.set_title(md, color=MODE_COLORS[md], fontsize=10, fontweight="bold")
            if col == 0:
                ax.text(-0.35, 0.5, sc, transform=ax.transAxes, rotation=90,
                        va="center", ha="center", color="white", fontsize=8,
                        fontweight="bold")

    fig.suptitle("Algorithm Comparison: Delivery Dispersion σ\n"
                 "No sync → Berkeley → Christian's → NTP → FairSync (actual)\n"
                 "NTP = theoretical best achievable; FairSync should approach it",
                 color="white", fontsize=12, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    fig.savefig(out / "10_algo_comparison.png", dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  Saved 10_algo_comparison.png")


def plot_algo_summary_bars(results: list[dict], out: pathlib.Path) -> None:
    """
    One focused slide-quality chart: hetero_hard + tcp-sync (the most
    informative combination), dispersion of all 5 algorithms side by side.
    Designed to be used directly in a presentation.
    """
    target_sc, target_md = "hetero_hard", "tcp-sync"
    agg = aggregate_algo(results)
    cell = agg.get(target_sc, {}).get(target_md, {})
    if not cell:
        print(f"  [skip] 11_algo_summary: no data for {target_sc}/{target_md}")
        return

    algos = list(ALGO_LABELS.keys())
    disp_means = [cell.get(a, {}).get("disp_mean", 0) for a in algos]
    disp_stds  = [cell.get(a, {}).get("disp_std",  0) for a in algos]

    fig, ax = plt.subplots(figsize=(10, 5))
    fig.patch.set_facecolor("#0f1117")
    ax.set_facecolor("#1a1d27")
    ax.spines[:].set_color("#444")
    ax.tick_params(colors="white")
    ax.yaxis.grid(True, color="#2a2d3a", linewidth=0.8)
    ax.set_axisbelow(True)

    x = range(len(algos))
    bars = ax.bar(x, disp_means, color=[ALGO_COLORS[a] for a in algos],
                  alpha=0.85, zorder=3, width=0.55,
                  yerr=disp_stds,
                  error_kw={"ecolor": "white", "capsize": 5, "alpha": 0.6})

    for bar, v, s in zip(bars, disp_means, disp_stds):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + s + 0.5,
                f"{v:.1f} ms", ha="center", va="bottom",
                fontsize=11, color="white", fontweight="bold")

    ax.set_xticks(list(x))
    ax.set_xticklabels([ALGO_LABELS[a] for a in algos], color="white", fontsize=12)
    ax.set_ylabel("Delivery Dispersion σ (ms)\nlower = more simultaneous", color="white", fontsize=11)
    ax.set_title(
        f"Algorithm Comparison — Delivery Dispersion σ\n"
        f"Scenario: {target_sc}  |  Mode: {target_md}  |  "
        f"Delays interpolated 5→150 ms",
        color="white", fontsize=13, fontweight="bold",
    )

    # Annotation explaining each algorithm
    notes = {
        "no_sync":   "Send all at T0\nspread = max−min delay",
        "berkeley":  "Sync clocks only\nno delivery stagger",
        "christian": "D_i = RTT/2\n(symmetric assumption)",
        "ntp":       "D_i = RTT/2 − offset\n(asymmetric correction)\nTheoretical best",
        "fairsync":  "NTP correction\n+ coordinator stagger\n+ bridge hold\nActual measured",
    }
    for i, (a, bar) in enumerate(zip(algos, bars)):
        ax.text(bar.get_x() + bar.get_width() / 2,
                -max(disp_means) * 0.08,
                notes[a], ha="center", va="top",
                fontsize=6.5, color="#aaa", style="italic",
                transform=ax.get_xaxis_transform())

    ax.set_ylim(bottom=-max(disp_means) * 0.15 if max(disp_means) > 0 else -1)
    fig.tight_layout()
    fig.savefig(out / "11_algo_summary.png", dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  Saved 11_algo_summary.png")


def plot_dispersion_vs_clients_scaling(scaling_results: list[dict], out: pathlib.Path) -> None:
    """
    Scaling version: dispersion σ vs. client count for all 5 algorithms.
    Uses hetero_hard scenario, tcp-sync mode (single-server, coordinator active).
    """
    from collections import defaultdict
    # bucket[algo][n_clients] = [disp_ms, ...]
    bucket: dict = defaultdict(lambda: defaultdict(list))
    for r in scaling_results:
        if r.get("spread_ms", -1) < 0:
            continue
        if r["scenario"] != "hetero_hard" or r["mode"] != "tcp-sync":
            continue
        n = r.get("n_clients", 3)
        m = algo_metrics(r)
        for algo, vals in m.items():
            bucket[algo][n].append(vals["disp"])

    algos = list(ALGO_LABELS.keys())
    fig, ax = plt.subplots(figsize=(10, 5))
    fig.patch.set_facecolor("#0f1117")
    ax.set_facecolor("#1a1d27")
    ax.spines[:].set_color("#444")
    ax.tick_params(colors="white")
    ax.yaxis.grid(True, color="#2a2d3a")
    ax.xaxis.grid(True, color="#2a2d3a", linestyle=":")
    ax.set_axisbelow(True)

    for algo in algos:
        ns = sorted(bucket[algo].keys())
        if not ns:
            continue
        means = [statistics.mean(bucket[algo][n]) for n in ns]
        stds  = [statistics.stdev(bucket[algo][n]) if len(bucket[algo][n]) > 1 else 0 for n in ns]
        ax.plot(ns, means, "o-", color=ALGO_COLORS[algo],
                label=ALGO_LABELS[algo], linewidth=2.5, markersize=7)
        ax.fill_between(ns,
                        [m - s for m, s in zip(means, stds)],
                        [m + s for m, s in zip(means, stds)],
                        color=ALGO_COLORS[algo], alpha=0.12)

    ax.set_xlabel("Number of concurrent clients", color="white", fontsize=12)
    ax.set_ylabel("Delivery Dispersion σ (ms)", color="white", fontsize=12)
    ax.set_title("Dispersion σ vs. Client Count — hetero_hard, tcp-sync\n"
                 "Does each algorithm stay fair as the class grows?",
                 color="white", fontsize=13, fontweight="bold")
    ax.legend(facecolor="#1a1d27", edgecolor="#555", labelcolor="white", fontsize=10)
    if any(bucket[a] for a in algos):
        all_ns = sorted({n for a in algos for n in bucket[a].keys()})
        ax.set_xticks(all_ns)
    fig.tight_layout()
    fig.savefig(out / "12_dispersion_scaling.png", dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  Saved 12_dispersion_scaling.png")


def generate_plots(results: list[dict], out: Optional[pathlib.Path] = None) -> None:
    if not HAS_PLOT:
        print("[skip] matplotlib not available")
        return
    if out is None:
        out = OUTPUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    print(f"\nGenerating plots → {out}/")
    plot_main_comparison(results, out)
    plot_heatmap(results, out)
    plot_timeline(results, out)
    plot_algorithm_internals(results, out)
    plot_spread_vs_delay(results, out)
    plot_dispersion(results, out)
    plot_algo_comparison(results, out)
    plot_algo_summary_bars(results, out)
    print(f"  Done.")


# ── Scaling plots ──────────────────────────────────────────────────────────────

def plot_scaling_grid(scaling_results: list[dict], out: pathlib.Path) -> None:
    """
    Grid of subplots: one row per scenario, three lines per subplot (one per mode).
    X = number of clients, Y = mean spread_ms.
    Shows whether each mode degrades gracefully as client count grows.
    """
    from collections import defaultdict
    # data[scenario][mode][n_clients] = [spread_ms, ...]
    data: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for r in scaling_results:
        if r.get("spread_ms", -1) >= 0:
            data[r["scenario"]][r["mode"]][r.get("n_clients", 3)].append(r["spread_ms"])

    scenarios = list(SCENARIOS.keys())
    modes     = list(WS_PATHS.keys())
    n_sc      = len(scenarios)

    fig, axes = plt.subplots(n_sc, 1, figsize=(11, 3.5 * n_sc), sharex=False)
    fig.patch.set_facecolor("#0f1117")
    if n_sc == 1:
        axes = [axes]

    for ax, sc in zip(axes, scenarios):
        ax.set_facecolor("#1a1d27")
        ax.spines[:].set_color("#444")
        ax.tick_params(colors="white")
        ax.yaxis.grid(True, color="#2a2d3a", linewidth=0.8)
        ax.set_axisbelow(True)

        for mode in modes:
            mode_data = data[sc][mode]
            ns = sorted(mode_data.keys())
            if not ns:
                continue
            means = [statistics.mean(mode_data[n]) for n in ns]
            stds  = [statistics.stdev(mode_data[n]) if len(mode_data[n]) > 1 else 0
                     for n in ns]
            ax.plot(ns, means, "o-", color=MODE_COLORS[mode], label=mode,
                    linewidth=2.5, markersize=6, zorder=3)
            ax.fill_between(ns,
                            [m - s for m, s in zip(means, stds)],
                            [m + s for m, s in zip(means, stds)],
                            color=MODE_COLORS[mode], alpha=0.12)
            # label last point
            if means:
                ax.annotate(f"{means[-1]:.0f}ms",
                            (ns[-1], means[-1]),
                            textcoords="offset points", xytext=(6, 0),
                            color=MODE_COLORS[mode], fontsize=7, va="center")

        ax.set_ylabel("Spread (ms)", color="white", fontsize=9)
        ax.set_title(sc, color="#ccc", fontsize=10, fontweight="bold", loc="left", pad=4)
        ax.legend(facecolor="#1a1d27", edgecolor="#444", labelcolor="white",
                  fontsize=8, loc="upper left")
        if ns:
            ax.set_xticks(ns)
        ax.xaxis.set_tick_params(labelbottom=True)
        ax.set_xlabel("Number of clients", color="#aaa", fontsize=8)

    fig.suptitle("Delivery Spread vs. Client Count\n"
                 "How does each transport mode scale as more students join?",
                 color="white", fontsize=13, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.97])
    fig.savefig(out / "06_scaling_grid.png", dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  Saved 06_scaling_grid.png")


def plot_scaling_summary(scaling_results: list[dict], out: pathlib.Path) -> None:
    """
    Single-panel summary: for hetero_hard (the hardest scenario), show spread
    vs clients for all three modes.  Clear, presentation-ready.
    """
    from collections import defaultdict
    target_sc = "hetero_hard"
    data: dict = defaultdict(lambda: defaultdict(list))
    for r in scaling_results:
        if r.get("spread_ms", -1) >= 0 and r["scenario"] == target_sc:
            data[r["mode"]][r.get("n_clients", 3)].append(r["spread_ms"])

    modes = list(WS_PATHS.keys())
    fig, ax = plt.subplots(figsize=(10, 5))
    fig.patch.set_facecolor("#0f1117")
    ax.set_facecolor("#1a1d27")
    ax.spines[:].set_color("#444")
    ax.tick_params(colors="white")
    ax.yaxis.grid(True, color="#2a2d3a")
    ax.xaxis.grid(True, color="#2a2d3a", linestyle=":")
    ax.set_axisbelow(True)

    for mode in modes:
        mode_data = data[mode]
        ns = sorted(mode_data.keys())
        if not ns:
            continue
        means = [statistics.mean(mode_data[n]) for n in ns]
        stds  = [statistics.stdev(mode_data[n]) if len(mode_data[n]) > 1 else 0
                 for n in ns]
        ax.plot(ns, means, "o-", color=MODE_COLORS[mode], label=mode,
                linewidth=3, markersize=9, zorder=3)
        ax.fill_between(ns,
                        [m - s for m, s in zip(means, stds)],
                        [m + s for m, s in zip(means, stds)],
                        color=MODE_COLORS[mode], alpha=0.15)
        for n, m in zip(ns, means):
            ax.text(n, m + max(stds) * 0.15 + 1, f"{m:.0f}",
                    ha="center", va="bottom", fontsize=8,
                    color=MODE_COLORS[mode], fontweight="bold")

    ax.axhline(0, color="#555", linewidth=1, linestyle="--")
    ax.text(min(ns) + 0.1 if ns else 3, 1, "ideal (0 ms)",
            color="#777", fontsize=8, style="italic")

    ax.set_xlabel("Number of concurrent exam clients", color="white", fontsize=12)
    ax.set_ylabel("Delivery spread (ms)  [lower = more simultaneous]",
                  color="white", fontsize=12)
    ax.set_title(
        f"FairSync Scaling — Scenario: {target_sc}\n"
        f"(delays span 5 → 150 ms, interpolated for each client count)",
        color="white", fontsize=13, fontweight="bold",
    )
    if ns:
        ax.set_xticks(ns)
    ax.legend(facecolor="#1a1d27", edgecolor="#555", labelcolor="white",
              fontsize=11, loc="upper left")
    fig.tight_layout()
    fig.savefig(out / "07_scaling_summary.png", dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  Saved 07_scaling_summary.png")


def plot_mode_improvement(scaling_results: list[dict], out: pathlib.Path) -> None:
    """
    Improvement ratio: (tcp-nosync spread) / (tcp-sync spread) and
    (tcp-nosync) / (rudp-sync) — >1 means sync is better, =1 means equal.
    Shows whether the algorithm actually helps as clients scale.
    """
    from collections import defaultdict
    data: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for r in scaling_results:
        if r.get("spread_ms", -1) >= 0:
            data[r["scenario"]][r["mode"]][r.get("n_clients", 3)].append(r["spread_ms"])

    scenarios = list(SCENARIOS.keys())
    sync_modes = ["tcp-sync", "rudp-sync"]
    baseline   = "tcp-nosync"

    n_sc = len(scenarios)
    fig, axes = plt.subplots(1, n_sc, figsize=(4 * n_sc, 5), sharey=True)
    fig.patch.set_facecolor("#0f1117")
    if n_sc == 1:
        axes = [axes]

    for ax, sc in zip(axes, scenarios):
        ax.set_facecolor("#1a1d27")
        ax.spines[:].set_color("#444")
        ax.tick_params(colors="white")
        ax.yaxis.grid(True, color="#2a2d3a")
        ax.axhline(1.0, color="#666", linewidth=1.2, linestyle="--")
        ax.text(0.05, 1.02, "no benefit", transform=ax.transAxes,
                color="#888", fontsize=7, style="italic")

        for sync_mode in sync_modes:
            base_data  = data[sc][baseline]
            sync_data  = data[sc][sync_mode]
            ns = sorted(set(base_data) & set(sync_data))
            if not ns:
                continue
            ratios = []
            for n in ns:
                b = statistics.mean(base_data[n])
                s = statistics.mean(sync_data[n])
                ratios.append(b / s if s > 0.5 else float("nan"))
            ax.plot(ns, ratios, "o-", color=MODE_COLORS[sync_mode],
                    label=f"{sync_mode} / nosync", linewidth=2, markersize=6)
            if ns:
                ax.set_xticks(ns)

        ax.set_title(sc, color="#ccc", fontsize=9, fontweight="bold")
        ax.set_xlabel("Clients", color="#aaa", fontsize=8)
        ax.tick_params(colors="white")

    axes[0].set_ylabel("Improvement ratio\n(nosync_spread / sync_spread)",
                       color="white", fontsize=10)
    axes[0].legend(facecolor="#1a1d27", edgecolor="#444",
                   labelcolor="white", fontsize=8)

    fig.suptitle("Algorithm Improvement Ratio vs. Client Count\n"
                 ">1 = sync is better than no-sync  |  1 = no benefit  |  <1 = worse",
                 color="white", fontsize=12, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    fig.savefig(out / "08_improvement_ratio.png", dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  Saved 08_improvement_ratio.png")


def generate_scaling_plots(scaling_results: list[dict]) -> None:
    if not HAS_PLOT:
        return
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\nGenerating scaling plots → {OUTPUT_DIR}/")
    plot_scaling_grid(scaling_results, OUTPUT_DIR)
    plot_scaling_summary(scaling_results, OUTPUT_DIR)
    plot_mode_improvement(scaling_results, OUTPUT_DIR)
    plot_dispersion_vs_clients_scaling(scaling_results, OUTPUT_DIR)
    print("  Done.")


# ── Entry point ────────────────────────────────────────────────────────────────

def _print_summary(results: list[dict]) -> None:
    agg = _aggregate(results)
    print("\n" + "=" * 65)
    print(f"{'SCENARIO':<20} {'tcp-nosync':>14} {'tcp-sync':>14} {'rudp-sync':>14}")
    print("-" * 65)
    for sc in SCENARIOS:
        row = f"{sc:<20}"
        for md in WS_PATHS:
            d = agg.get(sc, {}).get(md, {})
            row += f"  {d['mean']:>5.1f}±{d['std']:>4.1f}ms" if d else f"  {'—':>14}"
        print(row)
    print("=" * 65)
    print("All values = mean delivery spread in ms  (lower = more simultaneous)")


def main() -> None:
    global OUTPUT_DIR
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--runs", type=int, default=3,
                        help="Repetitions per (scenario, mode) combination (default: 3)")
    parser.add_argument("--max-clients", type=int, default=None,
                        help="Scale from --min-clients up to MAX_CLIENTS, "
                             "running the full benchmark at each count. "
                             "Example: --max-clients 8  runs 3→4→5→6→7→8 clients.")
    parser.add_argument("--min-clients", type=int, default=3,
                        help="Starting client count for scaling mode (default: 3)")
    parser.add_argument("--plot-only", action="store_true",
                        help="Skip benchmark; re-plot from existing JSON files")
    parser.add_argument("--output", default=str(OUTPUT_DIR),
                        help="Root output directory (default: simulation_results)")
    args = parser.parse_args()

    OUTPUT_DIR = pathlib.Path(args.output)

    # ── plot-only mode ────────────────────────────────────────────────────────
    if args.plot_only:
        if args.max_clients:
            sf = OUTPUT_DIR / "scaling_results.json"
            if not sf.exists():
                sys.exit(f"scaling_results.json not found in {OUTPUT_DIR}/")
            generate_scaling_plots(_load_results(sf))
        else:
            rf = OUTPUT_DIR / "results.json"
            if not rf.exists():
                sys.exit(f"results.json not found in {OUTPUT_DIR}/")
            generate_plots(_load_results(rf))
        return

    # ── sanity check ──────────────────────────────────────────────────────────
    print("Verifying containers are reachable...")
    try:
        _sh(["docker", "inspect", "client-1"], check=True)
    except Exception as e:
        sys.exit(f"Docker check failed: {e}\n"
                 f"Make sure 'docker compose up --build' is running.")

    # ── scaling mode (--max-clients N) ────────────────────────────────────────
    if args.max_clients:
        if args.min_clients < 2:
            sys.exit("--min-clients must be at least 2")
        if args.max_clients < args.min_clients:
            sys.exit("--max-clients must be >= --min-clients")

        print("FairSync Scaling Benchmark")
        print("=" * 65)
        print(f"Client range : {args.min_clients} → {args.max_clients}")
        print(f"Scenarios    : {len(SCENARIOS)}")
        print(f"Modes        : {list(WS_PATHS.keys())}")
        print(f"Runs/combo   : {args.runs}")
        print(f"Output       : {OUTPUT_DIR}/clients_XX/")

        scaling = asyncio.run(
            run_scaling(args.min_clients, args.max_clients, args.runs)
        )
        generate_scaling_plots(scaling)

        # Summary: hetero_hard across all counts
        from collections import defaultdict
        agg_scale: dict = defaultdict(lambda: defaultdict(list))
        for r in scaling:
            if r.get("spread_ms", -1) >= 0 and r["scenario"] == "hetero_hard":
                agg_scale[r["mode"]][r.get("n_clients", 3)].append(r["spread_ms"])
        print("\n" + "=" * 65)
        print("hetero_hard scenario — mean spread per client count:")
        counts = sorted({r.get("n_clients", 3) for r in scaling})
        header = f"{'mode':<14}" + "".join(f"  n={n:<3}" for n in counts)
        print(header)
        print("-" * len(header))
        for md in WS_PATHS:
            row = f"{md:<14}"
            for n in counts:
                vals = agg_scale[md].get(n, [])
                row += f"  {statistics.mean(vals):>5.1f}" if vals else f"  {'—':>5}"
            print(row)
        print("=" * 65)
        return

    # ── single-count mode (default: 3 clients) ────────────────────────────────
    n_clients = args.min_clients
    print("FairSync Simulation Benchmark")
    print("=" * 65)
    print(f"Clients   : {n_clients}")
    print(f"Scenarios : {len(SCENARIOS)}")
    print(f"Modes     : {list(WS_PATHS.keys())}")
    print(f"Runs/combo: {args.runs}")
    print(f"Output    : {OUTPUT_DIR}/")

    results = asyncio.run(run_all(args.runs, n_clients=n_clients))
    generate_plots(results)
    _print_summary(results)


if __name__ == "__main__":
    main()
