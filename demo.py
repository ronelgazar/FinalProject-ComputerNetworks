#!/usr/bin/env python3
"""
demo.py — Full automated showcase + test for the Exam Network Simulation.

Brings up Docker Compose, waits for all services, then runs five phases:

  [1] DNS Hierarchy     - iterative resolution trace root->TLD->auth->resolver
  [2] RUDP Servers      - verifies all 3 exam-server instances are healthy
  [3] Multi-Client Sim  - N concurrent WS clients run the full exam protocol
  [4] Admin Stats       - live client table from the admin API
  [5] Sync Report       - timing proof: all clients received exam at same ms

Usage
-----
    cd FinalProject-ComputerNetworks
    pip install websockets
    python3 demo.py                   # build images, start, run demo
    python3 demo.py --skip-build      # use cached images (faster)
    python3 demo.py --no-docker       # services already running
    python3 demo.py --clients 2       # use 2 clients instead of 3
    python3 demo.py --offset 30       # 30-second exam start offset (default 15)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import string
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ── Force UTF-8 output on Windows (PowerShell / cmd / MSYS) ──────────────────
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── dependency check ──────────────────────────────────────────────────────────
try:
    import websockets
except ImportError:
    print("\n  [!] Missing dependency.  Run:  pip install websockets\n")
    sys.exit(1)

# ── ANSI colours (Windows-safe via ANSI escape -- works in Windows Terminal) ──
BOLD = "\033[1m"
B    = "\033[1;34m"   # blue
G    = "\033[1;32m"   # green
Y    = "\033[1;33m"   # yellow
R    = "\033[1;31m"   # red
M    = "\033[1;35m"   # magenta
C    = "\033[1;36m"   # cyan
W    = "\033[1;37m"   # white bold
DIM  = "\033[2m"
RST  = "\033[0m"

# Enable ANSI on Windows
if sys.platform == "win32":
    os.system("")

# ── Config ────────────────────────────────────────────────────────────────────
ADMIN_URL     = "http://localhost:9999"
CLIENT_PORTS  = [8081, 8082, 8083]
NTP_ROUNDS    = 12
PROJECT_ROOT  = os.path.dirname(os.path.abspath(__file__))

# ── Pretty-print helpers ──────────────────────────────────────────────────────
def hdr(n: int, text: str):
    print(f"\n{W}{'─' * 64}{RST}")
    print(f"{W}  [{n}] {text}{RST}")
    print(f"{W}{'─' * 64}{RST}")

def ok(msg: str):    print(f"  {G}✓{RST}  {msg}")
def err(msg: str):   print(f"  {R}✗{RST}  {msg}")
def info(msg: str):  print(f"  {C}·{RST}  {msg}")
def warn(msg: str):  print(f"  {Y}!{RST}  {msg}")
def kv(label: str, val: str, vc: str = W):
    print(f"     {DIM}{label:<32}{RST}{vc}{val}{RST}")

def _bar(frac: float, width: int = 30, color: str = G) -> str:
    filled = int(frac * width)
    return f"{color}{'█' * filled}{DIM}{'░' * (width - filled)}{RST}"

# ── SimpleBarrier (asyncio.Barrier requires Python 3.11) ─────────────────────
class SimpleBarrier:
    """Drop-in asyncio.Barrier replacement for Python 3.9+."""
    def __init__(self, n: int):
        self._n     = n
        self._count = 0
        self._event = asyncio.Event()

    async def wait(self):
        self._count += 1
        if self._count >= self._n:
            self._event.set()
        await self._event.wait()

# ── Result struct ─────────────────────────────────────────────────────────────
@dataclass
class ClientResult:
    port:             int
    client_id:        str   = ""
    conn_info:        Optional[dict] = None    # from bridge connection_info
    ntp_samples:      list  = field(default_factory=list)
    best_rtt_ms:      float = 0.0
    clock_offset_ms:  float = 0.0
    schedule:         Optional[dict] = None
    deliver_delay_ms: int   = 0
    max_rtt_ms:       float = 0.0
    my_rtt_ms:        float = 0.0
    relay_candidate:  Optional[str] = None
    exam_req_at:      float = 0.0   # time.monotonic()
    exam_resp_at:     float = 0.0   # time.monotonic()
    submitted:        bool  = False
    error:            Optional[str] = None

# ── Docker helpers ────────────────────────────────────────────────────────────
def _sh(cmd: list, check: bool = True, capture: bool = False,
        extra_env: dict = None) -> subprocess.CompletedProcess:
    env = {**os.environ, **(extra_env or {})}
    return subprocess.run(cmd, env=env, check=check,
                          capture_output=capture, text=True,
                          cwd=PROJECT_ROOT)

def docker_up(skip_build: bool):
    hdr(0, "Docker Compose — starting services")
    cmd = ["docker", "compose", "up", "-d", "--remove-orphans"]
    if not skip_build:
        cmd.append("--build")
    info(f"$ {' '.join(cmd)}")
    try:
        _sh(cmd, check=False)
        ok("docker compose up completed")
    except FileNotFoundError:
        err("'docker' command not found — is Docker Desktop running?")
        sys.exit(1)

def _inject_start_time(offset_sec: int):
    """
    Pre-write /app/shared/start_at.txt via docker exec so all 3 RUDP server
    instances return the same start time without waiting 120 s.
    """
    start_ms = int((time.time() + offset_sec) * 1000)
    py_cmd = (
        f"import pathlib; "
        f"p=pathlib.Path('/app/shared'); p.mkdir(parents=True,exist_ok=True); "
        f"p.joinpath('start_at.txt').write_text('{start_ms}'); "
        f"print('start_at_ms={start_ms}')"
    )
    try:
        r = _sh(["docker", "exec", "rudp-server-1", "python3", "-c", py_cmd],
                check=False, capture=True)
        if r.returncode == 0:
            t = time.strftime("%H:%M:%S", time.localtime(start_ms / 1000))
            ms = start_ms % 1000
            ok(f"Exam start pinned to {G}{t}.{ms:03d}{RST}  (+{offset_sec}s from now)")
        else:
            warn(f"Could not inject start time: {r.stderr.strip()[:80]}")
    except Exception as e:
        warn(f"inject_start_time failed: {e}")

def _wait_for(url: str, label: str, timeout: int = 120) -> bool:
    info(f"Waiting for {label}…")
    deadline = time.monotonic() + timeout
    dots = 0
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            print()
            ok(f"{label} is ready")
            return True
        except Exception:
            sys.stdout.write(".")
            sys.stdout.flush()
            dots += 1
            time.sleep(2)
    print()
    err(f"{label} not ready after {timeout}s")
    return False

def _dump_logs(container: str, lines: int = 20):
    try:
        r = _sh(["docker", "logs", container, "--tail", str(lines)],
                check=False, capture=True)
        output = (r.stdout + r.stderr).strip()
        if output:
            print(f"\n  {Y}--- docker logs {container} (last {lines} lines) ---{RST}")
            for line in output.splitlines()[-lines:]:
                print(f"  {DIM}{line}{RST}")
    except Exception:
        pass

def wait_for_services(ports: list, offset_sec: int) -> list[int]:
    if not _wait_for(f"{ADMIN_URL}/api/status", "admin API", timeout=120):
        _dump_logs("admin")
        sys.exit(1)
    # Inject exam start time as soon as rudp-server-1 is up
    _inject_start_time(offset_sec)
    # Wait for each client nginx
    ready = []
    for port in ports:
        if _wait_for(f"http://localhost:{port}/", f"client :{port}", timeout=90):
            ready.append(port)
        else:
            warn(f"Skipping :{port} — container not ready")
    return ready

# ── Admin API ─────────────────────────────────────────────────────────────────
def api(path: str) -> Any:
    try:
        with urllib.request.urlopen(f"{ADMIN_URL}{path}", timeout=8) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

# ── [1] DNS test ──────────────────────────────────────────────────────────────
def phase_dns():
    hdr(1, "DNS Hierarchy — iterative resolution of server.exam.lan")

    data = api("/api/dns/trace")

    if "hops" in data:
        hops     = data.get("hops", [])
        resolved = data.get("resolved", [])

        print(f"\n  {W}Iterative resolution trace:{RST}\n")
        for i, hop in enumerate(hops):
            role   = hop.get("role", "?")
            server = hop.get("server", "?")
            answer = hop.get("answer") or hop.get("referral") or "—"
            rtt    = hop.get("rtt_ms", "?")
            arrow  = f"{G}✓ ANSWER{RST}" if i == len(hops) - 1 else f"{DIM}→ referral{RST}"
            print(f"     {DIM}{i+1}.{RST}  {C}{role:<12}{RST}  {server:<16}  "
                  f"{DIM}rtt={rtt}ms{RST}  {arrow}")
            print(f"          {W}{answer}{RST}\n")

        if resolved:
            ok(f"server.exam.lan → {G}{' , '.join(resolved)}{RST}  "
               f"({len(resolved)} A records — DNS round-robin load balancing)")
        else:
            warn("No A records returned")
    else:
        info(f"DNS trace raw: {json.dumps(data)[:200]}")

    # Confirm from inside the Docker network via docker exec
    try:
        r = _sh(
            ["docker", "exec", "dns-resolver", "python3", "-c",
             "import socket; r=socket.getaddrinfo('server.exam.lan',9000,"
             "socket.AF_INET,socket.SOCK_DGRAM); "
             "print('IPs:', list(dict.fromkeys(x[4][0] for x in r)))"],
            check=False, capture=True
        )
        if r.returncode == 0:
            ok(f"getaddrinfo from resolver container: {r.stdout.strip()}")
        else:
            info(f"docker exec check skipped ({r.stderr.strip()[:50]})")
    except Exception:
        pass

# ── [2] RUDP server health ────────────────────────────────────────────────────
def phase_rudp_health():
    hdr(2, "RUDP Exam Servers — health & shared state")

    for container in ["rudp-server-1", "rudp-server-2", "rudp-server-3"]:
        try:
            r = _sh(["docker", "inspect", "--format",
                     "{{.State.Status}} {{.State.StartedAt}}",
                     container], check=False, capture=True)
            status, *rest = r.stdout.strip().split()
            started = rest[0][:19].replace("T", " ") if rest else "?"
            color  = G if status == "running" else Y
            ok(f"{container:<18}  {color}{status}{RST}  started {DIM}{started}{RST}")
        except Exception as e:
            err(f"{container}  inspect failed: {e}")

    # Shared start_at.txt
    try:
        r = _sh(["docker", "exec", "rudp-server-1",
                 "cat", "/app/shared/start_at.txt"],
                check=False, capture=True)
        if r.returncode == 0 and r.stdout.strip():
            ms = int(r.stdout.strip())
            t  = time.strftime("%H:%M:%S", time.localtime(ms / 1000))
            remaining = (ms - time.time() * 1000) / 1000
            ok(f"start_at.txt = {G}{t}.{ms%1000:03d}{RST}  "
               f"({remaining:.1f}s from now)")

            # Confirm all three servers agree
            agreed = True
            for srv in ["rudp-server-2", "rudp-server-3"]:
                r2 = _sh(["docker", "exec", srv,
                           "cat", "/app/shared/start_at.txt"],
                          check=False, capture=True)
                if r2.stdout.strip() != r.stdout.strip():
                    warn(f"{srv} has a different start_at! ({r2.stdout.strip()})")
                    agreed = False
            if agreed:
                ok(f"All 3 RUDP instances agree on start_at_ms — "
                   f"{G}shared volume working{RST}")
        else:
            info("start_at.txt not written yet")
    except Exception:
        pass

    # clients.json
    try:
        r = _sh(["docker", "exec", "rudp-server-1",
                 "cat", "/app/shared/clients.json"],
                check=False, capture=True)
        if r.returncode == 0 and r.stdout.strip():
            clients = json.loads(r.stdout)
            info(f"clients.json: {len(clients)} records so far")
        else:
            info("clients.json: empty (no clients connected yet)")
    except Exception:
        pass

# ── [3] WebSocket client simulation ──────────────────────────────────────────
def _rid():
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=8))

async def _recv_until(ws, msg_type: str, req_id: str,
                      timeout: float = 15.0) -> dict:
    """Receive messages, discarding unsolicited events, until type+req_id match."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError(f"Timed out waiting for {msg_type}")
        raw  = await asyncio.wait_for(ws.recv(), timeout=remaining)
        msg  = json.loads(raw)
        if msg.get("type") == msg_type and msg.get("req_id") == req_id:
            return msg

async def simulate_client(port: int,
                           result: ClientResult,
                           sched_barrier: SimpleBarrier,
                           exam_barrier: SimpleBarrier):
    """
    Simulates one browser tab through the full exam protocol:
      connect → hello → NTP sync → schedule → wait for T₀
      → exam_req → exam_begin → save answers → submit
    """
    url       = f"ws://localhost:{port}/ws"
    client_id = f"demo-{port}-{_rid()}"
    result.client_id = client_id

    try:
        async with websockets.connect(url, open_timeout=15,
                                      ping_interval=None) as ws:

            # ── connection_info (unsolicited first message from bridge) ───────
            raw  = await asyncio.wait_for(ws.recv(), timeout=10)
            msg  = json.loads(raw)
            if msg.get("type") == "connection_info":
                result.conn_info = msg
            else:
                result.conn_info = None   # not yet available

            # ── hello → bridge injects rtt_ms ─────────────────────────────────
            await ws.send(json.dumps({"type": "hello", "client_id": client_id}))

            # ── NTP clock sync ────────────────────────────────────────────────
            for _ in range(NTP_ROUNDS):
                req_id = _rid()
                t0     = time.time() * 1000
                await ws.send(json.dumps({
                    "type": "time_req", "req_id": req_id, "client_t0_ms": t0,
                }))
                resp   = await _recv_until(ws, "time_resp", req_id, timeout=5)
                t1     = time.time() * 1000
                rtt    = t1 - t0
                offset = resp["server_now_ms"] - (t0 + rtt / 2)
                result.ntp_samples.append({"rtt": rtt, "offset": offset})
                await asyncio.sleep(0.12)

            best = min(result.ntp_samples, key=lambda s: s["rtt"])
            result.best_rtt_ms    = best["rtt"]
            result.clock_offset_ms = best["offset"]

            # ── schedule_req ──────────────────────────────────────────────────
            req_id = _rid()
            await ws.send(json.dumps({"type": "schedule_req", "req_id": req_id}))
            sched  = await _recv_until(ws, "schedule_resp", req_id, timeout=10)

            result.schedule         = sched
            result.deliver_delay_ms = sched.get("deliver_delay_ms", 0)
            result.max_rtt_ms       = sched.get("max_rtt_ms", 0.0)
            result.my_rtt_ms        = sched.get("my_rtt_ms", 0.0)
            result.relay_candidate  = sched.get("relay_candidate")

            # ── wait until all clients have their schedule, then sync ─────────
            await sched_barrier.wait()

            # ── wait for T₀ using clock-corrected local time ──────────────────
            start_at_ms   = sched["start_at_server_ms"]
            local_start   = start_at_ms - result.clock_offset_ms
            wait_sec      = (local_start - time.time() * 1000) / 1000
            if wait_sec > 0:
                await asyncio.sleep(wait_sec)

            # ── exam_req — critical synchronized point ────────────────────────
            req_id = _rid()
            result.exam_req_at = time.monotonic()
            await ws.send(json.dumps({
                "type": "exam_req", "req_id": req_id,
                "exam_id": sched["exam_id"],
            }))
            exam_resp = await _recv_until(ws, "exam_resp", req_id, timeout=30)
            result.exam_resp_at = time.monotonic()   # ← bridge delay already applied

            exam = exam_resp["exam"]

            # ── barrier: all clients record exam_resp_at before we continue ───
            await exam_barrier.wait()

            # ── exam_begin ────────────────────────────────────────────────────
            req_id = _rid()
            await ws.send(json.dumps({
                "type": "exam_begin", "req_id": req_id,
                "exam_id": exam["exam_id"],
                "opened_at_ms": result.exam_resp_at * 1000,
            }))
            try:
                await asyncio.wait_for(ws.recv(), timeout=4)
            except asyncio.TimeoutError:
                pass

            # ── fill answers ──────────────────────────────────────────────────
            answers = []
            for q in exam["questions"]:
                if q["type"] == "mcq" and q.get("options"):
                    answers.append({"qid": q["id"], "type": "mcq",
                                    "optionId": q["options"][0]["id"]})
                else:
                    answers.append({"qid": q["id"], "type": "text",
                                    "text": f"Demo answer from client :{port}"})

            # ── answers_save ──────────────────────────────────────────────────
            req_id = _rid()
            await ws.send(json.dumps({
                "type": "answers_save", "req_id": req_id,
                "exam_id": exam["exam_id"], "answers": answers,
            }))
            try:
                await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                pass

            # ── submit ────────────────────────────────────────────────────────
            req_id = _rid()
            await ws.send(json.dumps({
                "type": "submit_req", "req_id": req_id,
                "exam_id": exam["exam_id"], "answers": answers,
            }))
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=6)
                result.submitted = json.loads(raw).get("ok", False)
            except asyncio.TimeoutError:
                result.submitted = False

    except Exception as e:
        result.error = str(e)

async def phase_clients(ports: list[int], offset_sec: int) -> list[ClientResult]:
    hdr(3, f"Multi-Client Simulation — {len(ports)} concurrent clients")
    n       = len(ports)
    results = [ClientResult(port=p) for p in ports]

    sched_barrier = SimpleBarrier(n)
    exam_barrier  = SimpleBarrier(n)

    info(f"Ports:   {ports}")
    info(f"NTP:     {NTP_ROUNDS} rounds per client")
    info(f"Exam T₀: ≈{offset_sec}s from script start")
    info(f"All clients launched concurrently via asyncio.gather\n")

    tasks = [
        asyncio.create_task(
            simulate_client(ports[i], results[i], sched_barrier, exam_barrier)
        )
        for i in range(n)
    ]

    t_start = time.monotonic()
    while not all(t.done() for t in tasks):
        elapsed = time.monotonic() - t_start
        done    = sum(1 for r in results if r.exam_resp_at > 0 or r.error)
        ntp_max = max((len(r.ntp_samples) for r in results), default=0)
        frac    = ntp_max / NTP_ROUNDS
        bar     = _bar(frac, width=20)
        sys.stdout.write(
            f"\r  {C}·{RST}  {elapsed:5.1f}s  "
            f"NTP {bar}  {ntp_max}/{NTP_ROUNDS}  "
            f"clients done: {done}/{n}  "
        )
        sys.stdout.flush()
        await asyncio.sleep(0.4)

    sys.stdout.write("\r" + " " * 72 + "\r")
    await asyncio.gather(*tasks, return_exceptions=True)

    for r in results:
        if r.error:
            err(f":{r.port}  {r.error[:70]}")
        elif r.submitted:
            ok(f":{r.port}  {r.client_id}  submitted={G}✓{RST}  "
               f"ntp_best={r.best_rtt_ms:.1f}ms  offset={r.clock_offset_ms:+.1f}ms")
        else:
            warn(f":{r.port}  {r.client_id}  not submitted yet")

    return results

# ── [4] Admin stats ───────────────────────────────────────────────────────────
def phase_admin():
    hdr(4, "Admin Panel — Live Stats")

    status = api("/api/status")
    if "error" not in status:
        kv("Total clients",   str(status.get("total_clients", "?")), G)
        kv("Submitted",       str(status.get("submitted_count", "?")), G)
        kv("Pending",         str(status.get("pending_count", "?")), Y)
        kv("Exam start",      status.get("start_at", "not set"), C)
    else:
        warn(f"Status unavailable: {status['error']}")

    clients = api("/api/clients")
    if isinstance(clients, list) and clients:
        print(f"\n  {W}{'client_id':<26} {'RTT':>7} {'submitted':>10} "
              f"{'answers':>8} {'connected_at':>14}{RST}")
        print(f"  {'─' * 70}")
        for c in clients[:10]:
            cid  = c.get("client_id", "?")[:24]
            rtt  = f"{c.get('rtt_ms', '?')} ms"
            sub  = (G + "  ✓" + RST) if c.get("submitted") else (R + "  ✗" + RST)
            ans  = str(c.get("answers_count", "?"))
            cat  = c.get("connected_at", 0)
            cat_s = time.strftime("%H:%M:%S", time.localtime(cat / 1000)) if cat else "?"
            print(f"  {cid:<26} {rtt:>7} {sub:>10} {ans:>8} {cat_s:>14}")
    else:
        info("No client records (or admin unavailable)")

# ── [5] Sync timing report ────────────────────────────────────────────────────
def phase_report(results: list[ClientResult]):
    hdr(5, "Synchronized Delivery — Timing Proof")

    valid = [r for r in results if r.exam_resp_at > 0 and not r.error]
    if not valid:
        err("No successful clients — cannot compute sync stats")
        for r in results:
            if r.error:
                err(f"  :{r.port}  {r.error}")
        return

    t_ref      = min(r.exam_resp_at for r in valid)
    t_max      = max(r.exam_resp_at for r in valid)
    spread_ms  = (t_max - t_ref) * 1000

    # ── per-client table ──────────────────────────────────────────────────────
    print(f"\n  {W}{'Port':>6}  {'client_id':<22}  {'my_rtt':>7}  {'max_rtt':>8}  "
          f"{'delay':>7}  {'exam_resp Δ':>13}  {'sub':>4}{RST}")
    print(f"  {'─' * 78}")

    for r in results:
        if r.error:
            print(f"  {r.port:>6}  {R}ERROR — {r.error[:50]}{RST}")
            continue

        delta_ms  = (r.exam_resp_at - t_ref) * 1000
        col       = G if delta_ms < 10 else Y if delta_ms < 50 else R
        delta_str = f"+{delta_ms:.2f} ms"
        sub_str   = (G + "✓" + RST) if r.submitted else (R + "✗" + RST)
        h_rtt     = r.conn_info.get("handshake_rtt_ms", "?") if r.conn_info else "?"

        print(f"  {r.port:>6}  {r.client_id[:22]:<22}  "
              f"{str(h_rtt):>6}ms  {r.max_rtt_ms:>7.1f}ms  "
              f"{r.deliver_delay_ms:>6}ms  "
              f"{col}{delta_str:>13}{RST}  {sub_str}")

    # ── DNS resolution (from first client) ────────────────────────────────────
    print()
    ci = next((r.conn_info for r in valid if r.conn_info), None)
    if ci:
        print(f"  {W}DNS Resolution (client :{valid[0].port}):{RST}")
        kv("  Resolver",    ci.get("dns_resolver", "?"))
        kv("  Hostname",    ci.get("dns_hostname", "?"))
        kv("  Chosen IP",   ci.get("resolved_ip", "?"),  G)
        kv("  All A recs",  ", ".join(ci.get("all_ips", [])), C)
        h = ci.get("handshake_rtt_ms")
        if h is not None:
            kv("  Handshake RTT", f"{h:.1f} ms", M)

    # ── NTP sync quality ──────────────────────────────────────────────────────
    print(f"\n  {W}Clock Sync (NTP-style, {NTP_ROUNDS} samples per client):{RST}")
    for r in valid:
        if not r.ntp_samples:
            continue
        rtts = [s["rtt"] for s in r.ntp_samples]
        print(f"     :{r.port}  "
              f"best={r.best_rtt_ms:.1f}ms  "
              f"offset={r.clock_offset_ms:+.1f}ms  "
              f"min/avg/max = {min(rtts):.1f}/{sum(rtts)/len(rtts):.1f}/{max(rtts):.1f} ms")

    # ── sync formula explanation ──────────────────────────────────────────────
    if len(valid) > 1:
        print(f"\n  {W}Delivery synchronization formula:{RST}")
        print(f"     deliver_delay = (max_rtt − my_rtt) / 2 + buffer(30ms)")
        for r in valid:
            expected = (r.max_rtt_ms - r.my_rtt_ms) / 2 + 30
            print(f"     :{r.port}  "
                  f"({r.max_rtt_ms:.1f} − {r.my_rtt_ms:.1f}) / 2 + 30 "
                  f"= {expected:.1f}ms  →  bridge held {r.deliver_delay_ms}ms")

    # ── relay candidate ───────────────────────────────────────────────────────
    relay = next((r.relay_candidate for r in valid if r.relay_candidate), None)
    if relay:
        print(f"\n  {W}Relay candidate:{RST} {M}{relay}{RST}  "
              f"{DIM}(lowest RTT — identified as best relay node){RST}")

    # ── final verdict ─────────────────────────────────────────────────────────
    print(f"\n  {'─' * 64}")
    col = G if spread_ms < 30 else Y if spread_ms < 100 else R
    print(f"  {W}Spread across all clients:{RST}  {col}{spread_ms:.2f} ms{RST}")

    if spread_ms < 30:
        verdict = f"{G}SYNCHRONIZED{RST} — all clients within 30 ms jitter buffer ✓"
    elif spread_ms < 100:
        verdict = f"{Y}NEAR-SYNC{RST} — within 100 ms (acceptable)"
    else:
        verdict = f"{R}OUT OF SYNC{RST} — spread exceeds 100 ms"

    print(f"  {W}Verdict:{RST}  {verdict}")
    print(f"  {'─' * 64}")

# ── Summary banner ────────────────────────────────────────────────────────────
def print_banner():
    print(f"""
{W}╔══════════════════════════════════════════════════════════════╗
║          Exam Network Simulation — Full Demo Script          ║
║                                                              ║
║  DHCP · DNS hierarchy · RUDP · NTP sync · Admin panel       ║
║  Per-packet synchronized delivery · DNS round-robin LB      ║
╚══════════════════════════════════════════════════════════════╝{RST}
""")

def print_footer(ports: list[int]):
    print(f"\n{G}{'═' * 64}{RST}")
    print(f"{G}  Demo complete!{RST}")
    print(f"  Admin panel : {C}{ADMIN_URL}{RST}")
    for p in ports:
        print(f"  Browser     : {C}http://localhost:{p}/{RST}")
    print(f"{G}{'═' * 64}{RST}\n")

# ── Entrypoint ────────────────────────────────────────────────────────────────
async def async_main(args):
    if not args.no_docker:
        docker_up(skip_build=args.skip_build)
        ports = wait_for_services(CLIENT_PORTS[:args.clients], args.offset)
    else:
        # Assume already running; inject start time anyway
        _inject_start_time(args.offset)
        ports = CLIENT_PORTS[:args.clients]
        info(f"Using ports {ports} (--no-docker)")

    if not ports:
        err("No client ports available — aborting")
        sys.exit(1)

    phase_dns()
    phase_rudp_health()
    results = await phase_clients(ports, args.offset)
    phase_admin()
    phase_report(results)
    print_footer(ports)


def main():
    ap = argparse.ArgumentParser(
        description="Exam Network full demo + test",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--skip-build", action="store_true",
                    help="Skip docker compose --build (use cached images)")
    ap.add_argument("--no-docker",  action="store_true",
                    help="Assume services already running — skip docker compose")
    ap.add_argument("--clients",    type=int, default=3, choices=[1, 2, 3],
                    help="Number of concurrent clients to simulate (default: 3)")
    ap.add_argument("--offset",     type=int, default=15,
                    help="Seconds until exam start (default: 15)")
    args = ap.parse_args()

    print_banner()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
