#!/usr/bin/env python3
"""
run_scenarios.py — Automated pcap scenario generator for exam-net.

Prerequisites:
  docker compose up --build -d       # stack must be running
  pip install websockets             # on the host

Usage:
  python run_scenarios.py                    # run all 10 scenarios
  python run_scenarios.py --scenario 1       # run only scenario 1
  python run_scenarios.py --scenario 2 3     # run specific scenarios
  python run_scenarios.py --list             # list scenarios

Output:
  captures/
    01_single_full_flow/          dhcp-server.pcap  dns-*.pcap  rudp-server-1.pcap  client-1.pcap
    02_three_clients_sync/        rudp-server-1.pcap  client-{1,2,3}.pcap
    03_netem_resilience/          rudp-server-1.pcap  client-{1,2}.pcap
    04_disconnect/                rudp-server-1.pcap  client-{1,2}.pcap
    05_tcp_nosync/                tcp-server-nosync.pcap  client-{1,2}.pcap
    06_tcp_sync/                  tcp-server-sync.pcap    client-{1,2}.pcap
    07_recursive_dns/             dns-resolver.pcap  dns-root.pcap  dns-tld.pcap  dns-auth.pcap
    08_rudp_sliding_window/       rudp-server-1.pcap  client-1.pcap
    09_tcp_nosync_large/          tcp-server-nosync.pcap  client-1.pcap
    10_tcp_sync_large/            tcp-server-sync.pcap    client-1.pcap
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Force UTF-8 output on Windows (avoids cp1252 errors for ✓ ✗ → characters)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Paths & constants ────────────────────────────────────────────────────────
ROOT         = Path(__file__).resolve().parent
CAPTURES_DIR = ROOT / "captures"
SIM_SCRIPT   = ROOT / "scenarios" / "client_sim.py"
PCAP_IMG     = "exam-pcap-sidecar"       # built once from ./capture/Dockerfile

# Host-side WebSocket URLs (docker-compose exposes 8081/8082/8083 → port 80 in containers)
WS = {
    "c1_rudp":     "ws://localhost:8081/ws",
    "c2_rudp":     "ws://localhost:8082/ws",
    "c3_rudp":     "ws://localhost:8083/ws",
    "c1_tcp_sync": "ws://localhost:8081/ws-tcp-sync",
    "c2_tcp_sync": "ws://localhost:8082/ws-tcp-sync",
    "c1_tcp_no":   "ws://localhost:8081/ws-tcp-nosync",
    "c2_tcp_no":   "ws://localhost:8082/ws-tcp-nosync",
}

# All servers share the exam-shared volume → write start_at.txt once on rudp-server-1
SHARED_SERVER = "rudp-server-1"

# ── Low-level helpers ────────────────────────────────────────────────────────

def _sh(cmd: list[str], check: bool = False,
        timeout: int = 60, quiet: bool = True) -> subprocess.CompletedProcess:
    kw: dict = {"capture_output": True, "text": True, "timeout": timeout,
                "encoding": "utf-8", "errors": "replace"}
    r = subprocess.run(cmd, **kw)
    if check and r.returncode != 0:
        raise RuntimeError(f"Command {cmd} failed:\n{r.stderr.strip()}")
    return r


def _build_pcap_image() -> None:
    print("  [build] Building pcap sidecar image …")
    _sh(["docker", "build", "-t", PCAP_IMG, str(ROOT / "capture")], check=True)
    print(f"  [build] {PCAP_IMG} ready")


def _start_capture(container: str, label: str) -> str:
    """Start a tcpdump sidecar sharing container's network namespace.

    The pcap is written into the sidecar's own /tmp so no host bind-mount is
    needed.  Docker Desktop for Windows has a long-standing bug where bind-
    mounting paths that contain spaces causes 'mkdir .../c: file exists'.
    Writing to /tmp and later extracting via 'docker cp' avoids that entirely.
    """
    cap_name = f"pcap-{label}"
    _sh(["docker", "rm", "-f", cap_name])           # remove stale if any
    _sh([
        "docker", "run", "-d",
        "--name", cap_name,
        f"--net=container:{container}",
        "--cap-add=NET_ADMIN", "--cap-add=NET_RAW",
        PCAP_IMG, f"/tmp/{label}.pcap",
    ], check=True)
    return cap_name


def _stop_captures(sidecars: list[str], out_dir: Path) -> None:
    """Stop sidecars, copy pcaps to out_dir via 'docker cp', then remove."""
    for name in sidecars:
        _sh(["docker", "stop", "-t", "3", name])
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in sidecars:
        label = name.removeprefix("pcap-")
        r = _sh(["docker", "cp",
                 f"{name}:/tmp/{label}.pcap",
                 str(out_dir / f"{label}.pcap")])
        if r.returncode == 0:
            print(f"  [capture] ✓ {label}.pcap")
        else:
            print(f"  [capture] ✗ {label}.pcap — {r.stderr.strip()[:120]}")
        _sh(["docker", "rm", "-f", name])
    print(f"  [capture] {len(sidecars)} file(s) saved → {out_dir.name}/")


def _start_captures(containers: list[str]) -> list[str]:
    """Start one capture sidecar per container; return sidecar names."""
    sidecars = []
    for c in containers:
        name = _start_capture(c, c)
        sidecars.append(name)
        print(f"  [capture] ▶ {c}")
    time.sleep(1)   # let sidecars settle before generating traffic
    return sidecars


# ── Exam-state helpers ───────────────────────────────────────────────────────

def _set_exam_start(offset_sec: int = 20) -> None:
    """
    Write start_at.txt on the shared volume so all servers use the same
    near-future timestamp.  offset_sec controls how many seconds from now.
    """
    ts = int(time.time() * 1000) + offset_sec * 1000
    _sh(["docker", "exec", SHARED_SERVER, "sh", "-c",
         f"mkdir -p /app/shared && echo {ts} > /app/shared/start_at.txt"],
        check=True)
    print(f"  [exam]    start_at = now + {offset_sec}s  (ts={ts})")


def _clear_exam_start() -> None:
    _sh(["docker", "exec", SHARED_SERVER, "rm", "-f",
         "/app/shared/start_at.txt"])
    print("  [exam]    start_at.txt cleared")


def _set_netem(container: str, delay_ms: int = 0,
               jitter_ms: int = 0, loss_pct: float = 0.0) -> None:
    cfg = json.dumps({"delay_ms": delay_ms,
                      "jitter_ms": jitter_ms,
                      "loss_pct": loss_pct})
    _sh(["docker", "exec", container, "sh", "-c",
         f"echo '{cfg}' > /tmp/netem_delay.json"])
    print(f"  [netem]   {container}: delay={delay_ms}ms "
          f"jitter={jitter_ms}ms loss={loss_pct}%")


def _clear_netem(container: str) -> None:
    _sh(["docker", "exec", container, "rm", "-f", "/tmp/netem_delay.json"])
    print(f"  [netem]   {container}: cleared")


def _set_tc_netem(container: str, delay_ms: int) -> None:
    """Add kernel-level netem delay on eth0 inside container (replaces existing)."""
    _sh(["docker", "exec", container,
         "tc", "qdisc", "del", "dev", "eth0", "root"])   # harmless if absent
    _sh(["docker", "exec", container,
         "tc", "qdisc", "add", "dev", "eth0", "root",
         "netem", "delay", f"{delay_ms}ms"], check=True)
    print(f"  [tc]      {container}: eth0 kernel delay={delay_ms}ms")


def _clear_tc_netem(container: str) -> None:
    """Remove kernel netem qdisc from eth0 inside container."""
    _sh(["docker", "exec", container,
         "tc", "qdisc", "del", "dev", "eth0", "root"])
    print(f"  [tc]      {container}: eth0 kernel netem cleared")


def _wait_ready(container: str, timeout: int = 60) -> None:
    """
    Poll until container State.Running == true.
    Fails fast if the container has already exited (crash detected).
    Dumps container logs on any failure for easier diagnosis.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _sh(["docker", "inspect", "-f",
                 "{{.State.Running}} {{.State.Status}} {{.State.ExitCode}}",
                 container])
        parts = r.stdout.strip().split()
        if len(parts) >= 1 and parts[0] == "true":
            return
        # Fast-fail if container already exited with an error
        if len(parts) >= 2 and parts[1] == "exited":
            exit_code = parts[2] if len(parts) >= 3 else "?"
            logs = _sh(["docker", "logs", "--tail", "40", container])
            print(f"\n  [debug] {container} exited (code={exit_code}). Logs:")
            for line in (logs.stdout + logs.stderr).splitlines()[-30:]:
                print(f"          {line}")
            raise RuntimeError(
                f"{container} crashed at startup (ExitCode={exit_code})")
        time.sleep(1)
    # Timeout — dump whatever logs exist
    logs = _sh(["docker", "logs", "--tail", "40", container])
    print(f"\n  [debug] {container} logs (last 30 lines):")
    for line in (logs.stdout + logs.stderr).splitlines()[-30:]:
        print(f"          {line}")
    raise TimeoutError(f"{container} not ready after {timeout}s")


def _wait_ws_port(host_port: int, timeout: int = 60) -> None:
    """
    Wait until TCP port host_port on localhost accepts connections.
    Used to confirm nginx is up inside a client container before the
    simulator tries to open a WebSocket.
    """
    import socket as _sock
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with _sock.create_connection(("localhost", host_port), timeout=1):
                return
        except OSError:
            time.sleep(1)
    raise TimeoutError(f"localhost:{host_port} not accepting TCP after {timeout}s")


def _clear_dhcp_leases() -> None:
    """
    Remove stale DHCP leases so recreated containers always get a clean offer.
    With fixed CONTAINER_MAC env vars, the server will re-issue the same IPs.
    """
    _sh(["docker", "exec", "dhcp-server", "rm", "-f", "/app/leases.json"])
    print("  [dhcp]    leases cleared")


def _set_dns_mode(mode: str) -> None:
    """Switch dns-resolver between 'iterative' and 'recursive' at runtime."""
    payload = json.dumps({"mode": mode})
    _sh(["docker", "exec", "dns-resolver", "sh", "-c",
         f"echo '{payload}' > /tmp/dns_mode.json"], check=True)
    print(f"  [dns]     resolver mode → {mode}")


def _set_root_recursive(enabled: bool) -> None:
    """Enable/disable recursive-capable mode on the root server at runtime."""
    if enabled:
        payload = json.dumps({"enabled": True})
        _sh(["docker", "exec", "dns-root", "sh", "-c",
             f"echo '{payload}' > /tmp/dns_recursive.json"], check=True)
        print("  [dns]     root recursive-capable → ON")
    else:
        _sh(["docker", "exec", "dns-root", "rm", "-f",
             "/tmp/dns_recursive.json"])
        print("  [dns]     root recursive-capable → OFF")


def _clear_resolver_cache() -> None:
    """
    Restart dns-resolver to wipe its in-process TTL cache.
    /tmp/dns_mode.json persists through restart (writable container layer),
    so call _set_dns_mode BEFORE this if you need a specific mode active.
    """
    _sh(["docker", "compose", "restart", "dns-resolver"],
        check=True, timeout=30)
    _wait_ready("dns-resolver", timeout=30)
    time.sleep(1)
    print("  [dns]     resolver restarted — in-memory cache cleared")


def _recreate_client(service: str) -> None:
    """
    Force-recreate a client container so it boots with a fresh network
    namespace.  Clears DHCP leases first so the server issues a clean offer.
    """
    print(f"  [action]  Recreating {service} (fresh network namespace) …")
    _clear_dhcp_leases()
    _sh(["docker", "compose", "up", "-d",
         "--force-recreate", "--no-deps", service], check=True, timeout=120)


def _ensure_client_running(service: str, host_port: int) -> None:
    """
    Make sure the client container is running and nginx is accepting
    connections.  If the container is absent or exited, recreate it.
    """
    r = _sh(["docker", "inspect", "-f",
             "{{.State.Running}} {{.State.Status}}", service])
    parts = r.stdout.strip().split()
    is_running = len(parts) >= 1 and parts[0] == "true"
    if not is_running:
        status = parts[1] if len(parts) >= 2 else "missing"
        print(f"  [action]  {service} is {status} — recreating …")
        _recreate_client(service)
        _wait_ready(service, timeout=90)
        time.sleep(2)
    _wait_ws_port(host_port, timeout=60)


# ── Simulator runner ─────────────────────────────────────────────────────────

def _run_sim(ws_url: str, client_id: str | None = None,
             disconnect_after: str | None = None,
             think_ms: int = 0) -> dict:
    """Run client_sim.py on the host; return parsed JSON result."""
    cid = client_id or f"sim-{int(time.time()) % 10000}"
    cmd = [sys.executable, str(SIM_SCRIPT),
           "--ws", ws_url, "--id", cid]
    if disconnect_after:
        cmd += ["--disconnect-after", disconnect_after]
    if think_ms:
        cmd += ["--think-ms", str(think_ms)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                       encoding="utf-8", errors="replace")
    # Last non-empty line is the JSON result
    for line in reversed(r.stdout.strip().splitlines()):
        try:
            result = json.loads(line)
            # Surface verbose sim output on error so we can diagnose
            if result.get("status") in ("error", "parse_error"):
                sim_log = r.stdout.strip()
                if sim_log:
                    print(f"\n  [sim-log] {cid}:\n" +
                          "\n".join(f"    {l}" for l in sim_log.splitlines()[-20:]))
                if r.stderr.strip():
                    print(f"  [sim-err] {cid}: {r.stderr.strip()[:400]}")
            return result
        except json.JSONDecodeError:
            continue
    # Could not parse any line — show raw output
    print(f"\n  [sim-log] {cid} (no JSON found):\n  stdout: {r.stdout[-400:]}\n  stderr: {r.stderr[-400:]}")
    return {"client_id": cid, "status": "parse_error",
            "stdout": r.stdout[-500:], "stderr": r.stderr[-500:]}


def _run_parallel(*args_list: tuple) -> list[dict]:
    """Run multiple _run_sim calls in parallel."""
    with ThreadPoolExecutor(max_workers=len(args_list)) as ex:
        futures = [ex.submit(_run_sim, *a) for a in args_list]
    return [f.result() for f in futures]


# ── Scenario helpers ─────────────────────────────────────────────────────────

def _print_results(label: str, results: list[dict]) -> None:
    fwd_times = [r.get("bridge_forwarded_at_ms", 0) for r in results]
    fwd_valid  = [t for t in fwd_times if t]
    for r in results:
        hold  = r.get("sync_hold_ms", "–")
        delta = r.get("sync_delta_ms", "–")
        status = r.get("status", "?")
        line = (f"  {r['client_id']:<22}  status={status:<12}"
                f"  hold={str(hold)+'ms':<10}"
                f"  Δ={str(delta)+'ms':<10}")
        if status == "error":
            line += f"  ← {r.get('error', '?')}"
        print(line)
    if len(fwd_valid) > 1:
        spread = max(fwd_valid) - min(fwd_valid)
        print(f"  {'→ spread':<22}  {spread:.1f} ms "
              f"({'✓ OK' if spread < 10 else '⚠ WIDE'})")


def _wireshark_tip(out_dir: Path, tips: dict[str, str]) -> None:
    lines = [
        f"# Wireshark filters for {out_dir.name}",
        f"# Open: wireshark {out_dir}\\*.pcap",
        "",
    ]
    for label, filt in tips.items():
        lines.append(f"#  {label}")
        lines.append(f"#    {filt}")
    (out_dir / "FILTERS.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n  Wireshark filters saved to {out_dir / 'FILTERS.txt'}")
    for label, filt in tips.items():
        print(f"    {label:<42}  {filt}")


# ════════════════════════════════════════════════════════════════════════════
# Scenario 1 — Single client full flow
# ════════════════════════════════════════════════════════════════════════════

def scenario_01_single_full_flow() -> None:
    """
    Demonstrates everything in one capture set:
      • DHCP  D→O→R→A  — captured on dhcp-server (always running);
                          client-1 is stopped then started so DHCP fires fresh.
      • DNS   iterative chain: resolver → root → TLD → auth (first query)
      • DNS   recursive reply: resolver cache hit (second query, no root/tld/auth)
      • RUDP  three-way handshake (SYN / SYN+ACK / ACK)
      • Exam  full protocol: NTP sync → schedule → exam_req → submit

    Capture ordering is critical:
      1. Start sidecars on STATIC containers (dhcp-server, dns-*, rudp-server-1)
         — these never restart, so sidecars stay valid.
      2. Stop client-1 (ensure it is fully down).
      3. Start client-1  →  entrypoint runs DHCP  →  nginx + ws_bridge start.
         DHCP traffic is captured by dhcp-server sidecar (already running).
      4. Once client-1 is up, attach its own sidecar for RUDP/WS traffic.
      5. Run simulator  →  triggers DNS resolution + full exam flow.
    """
    out = CAPTURES_DIR / "01_single_full_flow"

    # Step 1 — start sidecars on static (never-restarted) containers
    static_sidecars = _start_captures([
        "dhcp-server",
        "dns-root", "dns-tld", "dns-auth", "dns-resolver",
        "rudp-server-1",
    ])

    # Step 2+3 — force-recreate client-1 (fresh namespace → DHCP fires clean)
    # Using stop+start reuses the same network namespace; 'ip addr add' then
    # fails with "address already exists".  --force-recreate avoids that.
    _recreate_client("client-1")

    # Step 4 — wait for container to be running, then attach its sidecar
    _wait_ready("client-1", timeout=60)
    time.sleep(2)   # let the namespace settle before the sidecar attaches
    client_sidecars = _start_captures(["client-1"])

    # Wait until nginx is accepting connections on port 8081 (host)
    # This means entrypoint DHCP is done and supervisord has started nginx.
    print("  [action]  Waiting for nginx on port 8081 …")
    _wait_ws_port(8081, timeout=60)

    # Set exam start in 18 s (shared volume → all servers see it)
    _set_exam_start(18)

    # Step 5 — full exam flow; first WS connection triggers DNS resolution
    print("  [sim]     Running full exam flow …")
    result = _run_sim(WS["c1_rudp"], "student-alpha")
    _print_results("single client", [result])

    # ── DNS round 2: cached recursive response ─────────────────────────────
    # A second dig query hits the resolver's cache → only dns-resolver.pcap
    # shows traffic; root / tld / auth pcaps stay silent.
    print("  [dns]     Second DNS query (cache hit — only resolver.pcap active) …")
    net_r = _sh(["docker", "network", "ls", "--filter", "name=exam-net",
                 "--format", "{{.Name}}"])
    net = next((n.strip() for n in net_r.stdout.splitlines() if "exam" in n), None)
    if net:
        _sh(["docker", "run", "--rm", f"--network={net}", "alpine",
             "sh", "-c",
             "apk add -q bind-tools 2>/dev/null; "
             "dig @10.99.0.2 server.exam.lan A +short +time=3"],
            timeout=30)
    time.sleep(2)

    _stop_captures(static_sidecars + client_sidecars, out)

    _wireshark_tip(out, {
        "DHCP exchange":                 "bootp",
        "DNS iterative chain (1st query)":
            "dns  [open dns-root/tld/auth/resolver.pcap together]",
        "DNS cached reply  (2nd query)": "dns  [only dns-resolver.pcap has traffic]",
        "RUDP handshake":                "udp.port == 9000 && (udp[14] & 0x03 != 0)",
        "RUDP data + ACKs":              "udp.port == 9000",
        "NTP sync rounds":               "udp.port == 9000  [look for short DATA exchanges early]",
        "Full exam flow (combined)":     "udp.port == 9000  [client-1.pcap + rudp-server-1.pcap]",
    })


# ════════════════════════════════════════════════════════════════════════════
# Scenario 2 — Three clients RUDP + sync
# ════════════════════════════════════════════════════════════════════════════

def scenario_02_three_clients_sync() -> None:
    """
    3 clients connect simultaneously via RUDP + ExamSendCoordinator.
    Shows:
      • ExamSendCoordinator staggered send (different send times per client)
      • All 3 bridge_forwarded_at_ms within ~10 ms of each other
      • sync_hold_ms compensates per-client latency
    """
    out = CAPTURES_DIR / "02_three_clients_sync"

    _ensure_client_running("client-1", 8081)
    _ensure_client_running("client-2", 8082)
    _ensure_client_running("client-3", 8083)

    _clear_exam_start()
    _set_exam_start(22)

    sidecars = _start_captures([
        "rudp-server-1", "rudp-server-2", "rudp-server-3",
        "client-1", "client-2", "client-3",
    ])

    print("  [sim]     Launching 3 simultaneous clients …")
    results = _run_parallel(
        (WS["c1_rudp"], "student-1"),
        (WS["c2_rudp"], "student-2"),
        (WS["c3_rudp"], "student-3"),
    )
    _print_results("3-client sync", results)

    _stop_captures(sidecars, out)

    _wireshark_tip(out, {
        "RUDP traffic all servers":    "udp.port == 9000",
        "Client-1 receive time":       "ip.addr == (client-1 IP) && udp.port == 9000",
        "Staggered send on server":    "rudp-server-*.pcap  [look at timestamps of large DATA bursts]",
        "Sync spread (compare times)": "udp.port == 9000  [merge all, sort by time]",
    })


# ════════════════════════════════════════════════════════════════════════════
# Scenario 3 — Netem resilience (2 clients, 1 with artificial delay)
# ════════════════════════════════════════════════════════════════════════════

def scenario_03_netem_resilience() -> None:
    """
    client-1: normal
    client-2: netem delay=150 ms + jitter=40 ms

    Self-correcting hold:
      • client-2's bridge detects larger transit time
      • hold_ms shrinks automatically → both clients hit target_ms
      • spread stays < 15 ms despite 150 ms extra delay on client-2
    """
    out = CAPTURES_DIR / "03_netem_resilience"

    _ensure_client_running("client-1", 8081)
    _ensure_client_running("client-2", 8082)

    _clear_exam_start()
    _set_exam_start(22)
    _set_netem("client-2", delay_ms=150, jitter_ms=40)

    sidecars = _start_captures([
        "rudp-server-1",
        "client-1", "client-2",
    ])

    print("  [sim]     2 clients: client-1 normal, client-2 has 150ms+40ms jitter …")
    results = _run_parallel(
        (WS["c1_rudp"], "student-normal"),
        (WS["c2_rudp"], "student-delayed"),
    )
    _print_results("netem resilience", results)

    _clear_netem("client-2")
    _stop_captures(sidecars, out)

    _wireshark_tip(out, {
        "client-2 extra latency":         "udp.port == 9000  [client-2.pcap: larger inter-packet gaps]",
        "Bridge hold absorbs the delay":   "udp.port == 9000  [compare hold_ms in exam_resp JSON payload]",
        "Spread despite bad connection":   "merge client-1.pcap + client-2.pcap, filter udp.port==9000",
        "RUDP retransmits on client-2":    "udp.port == 9000  [duplicate seq numbers in client-2.pcap]",
    })


# ════════════════════════════════════════════════════════════════════════════
# Scenario 4 — Mid-exam disconnect
# ════════════════════════════════════════════════════════════════════════════

def scenario_04_disconnect() -> None:
    """
    client-1 disconnects abruptly after answers_save (simulates browser close).
    client-2 completes normally.

    Shows:
      • RUDP FIN sequence (or timeout → RST if bridge tears down first)
      • Server-side RudpReset / RudpTimeout handling
      • Partial answers already saved to disk (autosave worked)
      • client-2 unaffected — completes and submits
    """
    out = CAPTURES_DIR / "04_disconnect"

    _ensure_client_running("client-1", 8081)
    _ensure_client_running("client-2", 8082)

    _clear_exam_start()
    _set_exam_start(18)

    sidecars = _start_captures([
        "rudp-server-1",
        "client-1", "client-2",
    ])

    print("  [sim]     client-1 will drop after answers_save; client-2 completes …")
    results = _run_parallel(
        (WS["c1_rudp"], "student-dropout",  "answers_save"),
        (WS["c2_rudp"], "student-finisher", None),
    )
    _print_results("disconnect", results)

    _stop_captures(sidecars, out)

    _wireshark_tip(out, {
        "RUDP FIN / teardown (client-1)": "udp.port == 9000 && (udp[14] & 0x04 != 0)",
        "Normal completion (client-2)":   "udp.port == 9000  [client-2.pcap]",
        "All RUDP traffic":               "udp.port == 9000",
    })


# ════════════════════════════════════════════════════════════════════════════
# Scenario 5 — TCP + no sync (baseline)
# ════════════════════════════════════════════════════════════════════════════

def scenario_05_tcp_nosync() -> None:
    """
    2 clients via TCP, SYNC_ENABLED=0.
    Shows:
      • Newline-delimited JSON over plain TCP (port 9001)
      • No server_sent_at_ms / sync_hold_ms in exam_resp
      • Each client receives exam at different wall-clock times (no coordination)
      • Spread can be tens or hundreds of ms — unpredictable
    """
    out = CAPTURES_DIR / "05_tcp_nosync"

    _ensure_client_running("client-1", 8081)
    _ensure_client_running("client-2", 8082)

    # tcp-server-nosync shares exam-shared volume → this write affects it too
    _clear_exam_start()
    _set_exam_start(18)

    sidecars = _start_captures([
        "tcp-server-nosync",
        "client-1", "client-2",
    ])

    print("  [sim]     2 clients via TCP no-sync …")
    results = _run_parallel(
        (WS["c1_tcp_no"], "student-nosync-1"),
        (WS["c2_tcp_no"], "student-nosync-2"),
    )
    _print_results("tcp no-sync", results)

    _stop_captures(sidecars, out)

    _wireshark_tip(out, {
        "TCP stream (NL-JSON)":            "tcp.port == 9001",
        "No sync fields in exam_resp":     "tcp.port == 9001  [look for absence of server_sent_at_ms]",
        "Uncoordinated receive times":     "merge client-1.pcap + client-2.pcap, tcp.port==9001",
        "Compare spread vs scenario 02":   "bridge_forwarded_at_ms difference between clients",
    })


# ════════════════════════════════════════════════════════════════════════════
# Scenario 6 — TCP + sync
# ════════════════════════════════════════════════════════════════════════════

def scenario_06_tcp_sync() -> None:
    """
    2 clients via TCP, SYNC_ENABLED=1 (same ExamSendCoordinator as RUDP).
    Shows:
      • TCP transport with full synchronization
      • server_sent_at_ms + bridge hold present (identical logic to RUDP)
      • Spread comparable to RUDP scenario (~10 ms)
      • Demonstrates that sync is transport-agnostic
    """
    out = CAPTURES_DIR / "06_tcp_sync"

    _ensure_client_running("client-1", 8081)
    _ensure_client_running("client-2", 8082)

    _clear_exam_start()
    _set_exam_start(22)

    sidecars = _start_captures([
        "tcp-server-sync",
        "client-1", "client-2",
    ])

    print("  [sim]     2 clients via TCP+sync …")
    results = _run_parallel(
        (WS["c1_tcp_sync"], "student-tcp-1"),
        (WS["c2_tcp_sync"], "student-tcp-2"),
    )
    _print_results("tcp sync", results)

    _stop_captures(sidecars, out)

    _wireshark_tip(out, {
        "TCP NL-JSON stream":            "tcp.port == 9001",
        "server_sent_at_ms in payload":  "tcp.port == 9001  [follow TCP stream, grep server_sent_at_ms]",
        "Bridge hold in payload":        "tcp.port == 9001  [grep sync_hold_ms in client-1/2.pcap]",
        "Spread vs TCP no-sync (05)":    "compare bridge_forwarded_at_ms: scenario 05 vs 06",
        "Spread vs RUDP sync (02)":      "compare bridge_forwarded_at_ms: scenario 02 vs 06",
    })


# ════════════════════════════════════════════════════════════════════════════
# Scenario 7 — Pure recursive DNS resolution
# ════════════════════════════════════════════════════════════════════════════

def scenario_07_recursive_dns() -> None:
    """
    Captures a full recursive DNS exchange in isolation.

    In iterative mode (scenario 01) the resolver walks root→TLD→auth itself,
    producing 3 separate query/response pairs visible in the pcaps.

    In recursive mode (this scenario):
      • resolver sets RD=1 and asks root to do the walking
      • root queries TLD internally, then auth internally, and returns
        the final A record directly to the resolver
      • dns-resolver.pcap shows ONE query/response pair (client↔resolver
        + resolver↔root)
      • dns-root.pcap shows the sub-queries root made to TLD and auth

    Setup:
      1.  Enable recursive mode on resolver  (/tmp/dns_mode.json)
      2.  Enable RECURSIVE_CAPABLE on root   (/tmp/dns_recursive.json)
      3.  Restart resolver to flush in-memory TTL cache (mode file persists)
      4.  Start captures on resolver + root  (TLD + auth included for contrast)
      5.  Run a single dig query with RD=1
      6.  Stop captures, restore iterative mode, disable root recursive flag
    """
    out = CAPTURES_DIR / "07_recursive_dns"

    # Step 1+2 — switch both servers to recursive mode before cache flush
    _set_dns_mode("recursive")
    _set_root_recursive(True)

    # Step 3 — flush the resolver's in-memory cache (mode file survives restart)
    _clear_resolver_cache()

    # Locate the Docker network so we can spin up an alpine dig container on it
    net_r = _sh(["docker", "network", "ls", "--filter", "name=exam-net",
                 "--format", "{{.Name}}"])
    net = next((n.strip() for n in net_r.stdout.splitlines() if "exam" in n), None)
    if not net:
        print("  [warn]    exam-net network not found — aborting recursive DNS scenario")
        _set_dns_mode("iterative")
        _set_root_recursive(False)
        return

    # Step 4 — start sidecars (resolver + root carry the interesting traffic;
    #           TLD + auth included so the absence of traffic proves root handled
    #           the sub-queries internally on its own interface)
    sidecars = _start_captures(["dns-resolver", "dns-root", "dns-tld", "dns-auth"])

    # Step 5 — single recursive dig query (dig sends RD=1 by default)
    print("  [dns]     Sending recursive query for server.exam.lan …")
    r = _sh(["docker", "run", "--rm", f"--network={net}", "alpine",
             "sh", "-c",
             "apk add -q bind-tools 2>/dev/null; "
             "dig @10.99.0.2 server.exam.lan A +recurse +time=5 +tries=1"],
            timeout=30)
    for line in r.stdout.strip().splitlines():
        print(f"  [dig]     {line}")
    time.sleep(1)

    # Step 6 — collect pcaps and restore iterative mode
    _stop_captures(sidecars, out)
    _set_dns_mode("iterative")
    _set_root_recursive(False)

    _wireshark_tip(out, {
        "Client query + resolver response (1 pair)":
            "dns && ip.addr == 10.99.0.2  [dns-resolver.pcap]",
        "Resolver → root RD=1 sub-query":
            "dns.flags.recdesired == 1  [dns-resolver.pcap]",
        "Root internal sub-queries to TLD + auth":
            "dns  [dns-root.pcap — root's own outgoing queries]",
        "TLD + auth were NOT contacted by resolver":
            "dns  [dns-tld.pcap / dns-auth.pcap should be silent or minimal]",
        "RA=1 in resolver's reply to client":
            "dns.flags.recavail == 1  [dns-resolver.pcap]",
        "Compare vs iterative chain (scenario 01)":
            "open 01_single_full_flow/dns-*.pcap alongside — more hops, same answer",
    })


# ════════════════════════════════════════════════════════════════════════════
# Scenario 8 — RUDP sliding window: large payload + software slow-send
# ════════════════════════════════════════════════════════════════════════════

def scenario_08_rudp_sliding_window() -> None:
    """
    Makes the RUDP sliding window and exam-question payload directly visible
    in Wireshark by combining two effects:

    A) Large exam JSON (bigjson.json ≈ 34 KB → ~20 KB zlib-compressed)
       exam_resp is split into ~17 RUDP DATA chunks (MSS = 1200 B).
       In Wireshark: click any DATA packet, open the Data tab — the
       compressed JSON bytes at payload offset 14 hold the exam questions.
       Use "Follow UDP Stream" to reassemble all chunks in one view.

    B) Software slow-send delay (50 ms per window-fill batch) injected into
       rudp_socket.send() via /tmp/rudp_slowsend.json on the server.
       Because Docker Desktop's LinuxKit kernel ships without the sch_netem
       module, kernel tc-netem is not available; this file-based mechanism
       achieves the same visual effect without kernel-module dependencies.
       RUDP slow-start staircase (cwnd doubles each ACK round):
         batch 1: cwnd=1  → 1  chunk, pause 50 ms
         batch 2: cwnd=2  → 2  chunks, pause 50 ms
         batch 3: cwnd=4  → 4  chunks, pause 50 ms
         batch 4: cwnd=8  → 8  chunks, pause 50 ms
         batch 5: cwnd=16 → remaining chunks (no more pauses)
       In Wireshark: filter DATA-only packets and look at the Time column —
       the 50 ms gaps between bursts of 1, 2, 4, 8 packets are clearly
       visible without any special tooling.

    RUDP header layout (at UDP payload offset 0):
      bytes  0- 3  seq     (uint32 big-endian)
      bytes  4- 7  ack     (uint32 big-endian)
      byte   8     flags   (SYN=0x01 ACK=0x02 FIN=0x04 DATA=0x08
                            RST=0x10 PING=0x20 MSG_END=0x40)
      byte   9     window  (uint8 — receiver free buffer in packets)
      bytes 10-11  length  (uint16 — payload byte count)
      bytes 12-13  crc16   (uint16)
      bytes 14+    payload (zlib-compressed JSON for exam_resp)

    Captures: rudp-server-1.pcap  client-1.pcap
    """
    out = CAPTURES_DIR / "08_rudp_sliding_window"

    # Step 0 — deploy updated rudp_socket.py into the running server container.
    #           rudp_socket.py is baked into the Docker image; we hot-patch the
    #           running container by copying the local version and restarting.
    #           (The slowsend feature is a no-op when /tmp/rudp_slowsend.json is
    #           absent, so this change is always safe to leave in the image.)
    rudp_sock_src = ROOT / "RUDP" / "rudp_socket.py"
    print("  [deploy]  Copying updated rudp_socket.py → rudp-server-1 …")
    _sh(["docker", "cp", str(rudp_sock_src), "rudp-server-1:/app/rudp_socket.py"],
        check=True)
    print("  [deploy]  Restarting rudp-server-1 to load new code …")
    _sh(["docker", "restart", "rudp-server-1"], check=True)
    _wait_ready("rudp-server-1", timeout=30)   # wait for State.Running
    time.sleep(2)   # extra buffer for Python to bind the UDP port

    # Step 1 — upload big exam JSON as the shared override (server checks
    #           /app/shared/exam.json first; loads fresh on every exam_req)
    big_json = ROOT / "bigjson.json"
    if not big_json.exists():
        print("  [warn]    bigjson.json not found in project root — aborting")
        return
    _sh(["docker", "cp", str(big_json), "rudp-server-1:/app/shared/exam.json"],
        check=True)
    print(f"  [exam]    bigjson.json ({big_json.stat().st_size // 1024} KB) "
          "→ rudp-server-1:/app/shared/exam.json")

    # Step 2 — write slowsend config to the server container.
    #           rudp_socket.send() reads /tmp/rudp_slowsend.json and sleeps
    #           delay_ms between each window-fill batch, spacing out the
    #           slow-start staircase so it is visible in Wireshark.
    _sh(["docker", "exec", "rudp-server-1",
         "sh", "-c", 'echo \'{"delay_ms": 50}\' > /tmp/rudp_slowsend.json'],
        check=True)
    print("  [slow]    rudp-server-1: slowsend delay=50ms enabled")

    # Step 3 — ensure client, set exam timing
    _ensure_client_running("client-1", 8081)
    _clear_exam_start()
    _set_exam_start(30)   # extra buffer: restart+captures consume ~10s of budget

    # Step 4 — start captures on both ends
    sidecars = _start_captures(["rudp-server-1", "client-1"])

    # Step 5 — run single client; exam_resp will be the large bigjson payload
    print("  [sim]     Large exam + 50 ms per-batch slowsend → sliding window …")
    result = _run_sim(WS["c1_rudp"], "student-window", think_ms=3000)
    _print_results("RUDP sliding window", [result])

    # Step 6 — collect pcaps
    _stop_captures(sidecars, out)

    # Step 7 — clean up slowsend flag and exam JSON override
    _sh(["docker", "exec", "rudp-server-1",
         "rm", "-f", "/tmp/rudp_slowsend.json"])
    _sh(["docker", "exec", "rudp-server-1",
         "rm", "-f", "/app/shared/exam.json"])
    print("  [slow]    slowsend disabled; bigjson.json override removed")

    # Step 8 — Wireshark tips
    _wireshark_tip(out, {
        "All RUDP traffic":
            "udp.port == 9000",
        "DATA chunks only  (flags & 0x08)":
            "udp.port == 9000 && (udp[8] & 8 != 0)",
        "Last DATA chunk   (MSG_END, flags & 0x40)":
            "udp.port == 9000 && (udp[8] & 0x40 != 0)",
        "ACK-only packets  (flags == 0x02, no payload)":
            "udp.port == 9000 && udp[8] == 0x02",
        "SYN / SYN-ACK handshake (flags & 0x01)":
            "udp.port == 9000 && (udp[8] & 0x01 != 0)",
        "Read questions from payload":
            ("click DATA packet -> Data tab -> byte offset 14 = RUDP payload "
             "(zlib-compressed JSON); 'Follow UDP Stream' reassembles all chunks"),
        "Window-field changes (byte 9 of UDP payload)":
            ("Hex view: udp[9] = receiver advertised window "
             "(64 = full, drops as recv queue fills)"),
        "Slow-start batches — look at packet timing":
            ("filter DATA packets; Time column shows groups: 1 pkt, 50ms gap, "
             "2 pkts, 50ms gap, 4 pkts, 50ms gap, 8 pkts, 50ms gap, rest"),
        "Extract DATA times with tshark":
            ('tshark -r rudp-server-1.pcap '
             '-Y "udp.port==9000 && (udp[8] & 8 != 0)" '
             '-T fields -e frame.time_relative -e udp.length '
             '> data_chunks.csv   '
             '# rows show 50ms-spaced batches: 1, 2, 4, 8, rest'),
    })


# ════════════════════════════════════════════════════════════════════════════
# Scenario 9 — TCP no-sync: large payload — questions + TCP sliding window
# ════════════════════════════════════════════════════════════════════════════

def scenario_09_tcp_nosync_large() -> None:
    """
    TCP no-sync transport with bigjson.json (34 KB) as the exam payload.

    Why TCP is different from RUDP (scenario 08):
      • exam_resp is sent as plain newline-delimited JSON — no zlib compression.
        The exam questions, options and metadata are directly human-readable in
        Wireshark's "Follow TCP Stream" view without any decompression step.
      • TCP segmentation and sliding window are handled by the OS kernel.
        With ~34 KB of exam JSON the server's TCP stack splits the payload into
        ~23 segments (~1460 B each, standard Ethernet MSS).  Wireshark shows
        the TCP sequence numbers, ACK numbers and window sizes natively.
      • The slow-start staircase (cwnd=1→2→4→…) is visible in Wireshark via
        Statistics → TCP Stream Graphs → Time-Sequence Graph without any
        code changes or artificial delays.
      • No sync fields (server_sent_at_ms / sync_hold_ms) in the exam_resp —
        use this pcap alongside scenario 10 to show the difference.

    Captures: tcp-server-nosync.pcap  client-1.pcap
    """
    out = CAPTURES_DIR / "09_tcp_nosync_large"

    # Upload big exam JSON to the shared volume (all servers see it)
    big_json = ROOT / "bigjson.json"
    if not big_json.exists():
        print("  [warn]    bigjson.json not found — aborting")
        return
    _sh(["docker", "cp", str(big_json),
         "tcp-server-nosync:/app/shared/exam.json"], check=True)
    print(f"  [exam]    bigjson.json ({big_json.stat().st_size // 1024} KB) "
          "→ tcp-server-nosync:/app/shared/exam.json")

    _ensure_client_running("client-1", 8081)
    _clear_exam_start()
    _set_exam_start(30)

    sidecars = _start_captures(["tcp-server-nosync", "client-1"])

    print("  [sim]     Single client via TCP no-sync + large payload …")
    result = _run_sim(WS["c1_tcp_no"], "student-tcp-big", think_ms=3000)
    _print_results("tcp no-sync large", [result])

    _stop_captures(sidecars, out)

    _sh(["docker", "exec", "tcp-server-nosync",
         "rm", "-f", "/app/shared/exam.json"])
    print("  [exam]    bigjson.json override removed")

    _wireshark_tip(out, {
        "All TCP exam traffic":
            "tcp.port == 9001",
        "Data segments only (no pure ACKs)":
            "tcp.port == 9001 && tcp.len > 0",
        "Read exam questions (plain JSON)":
            ("tcp.port == 9001 → right-click → Follow TCP Stream  "
             "→ exam questions visible as UTF-8 text, no decompression needed"),
        "TCP sliding window — Time/Sequence graph":
            ("Wireshark: Statistics → TCP Stream Graphs → Time-Sequence Graph  "
             "(select the tcp-server-nosync.pcap stream) — shows slow-start staircase"),
        "TCP window size field":
            "tcp.port == 9001 && tcp.len > 0  → look at tcp.window_size column",
        "Count TCP data segments for exam_resp":
            ('tshark -r tcp-server-nosync.pcap '
             '-Y "tcp.port==9001 && tcp.len>0" '
             '-T fields -e frame.time_relative -e tcp.len -e tcp.seq '
             '> tcp_segments.csv'),
        "No sync fields present (baseline)":
            ("Follow TCP Stream → search 'server_sent_at_ms' → absent  "
             "compare with scenario 10 where it IS present"),
    })


# ════════════════════════════════════════════════════════════════════════════
# Scenario 10 — TCP sync: large payload — questions + sync fields visible
# ════════════════════════════════════════════════════════════════════════════

def scenario_10_tcp_sync_large() -> None:
    """
    TCP+sync transport with bigjson.json (34 KB) as the exam payload.

    Same TCP segmentation / sliding-window view as scenario 09, with the
    addition of ExamSendCoordinator synchronization fields:
      • server_sent_at_ms — timestamp when server called sock.sendall()
      • sync_hold_ms      — bridge delay computed from RTT samples
      • bridge_forwarded_at_ms — when the bridge released the exam to the browser

    Use alongside scenario 09 to show:
      1. The payload is identical (same bigjson.json) but exam_resp in 10
         carries extra sync metadata in the JSON object.
      2. TCP carries the sync protocol transparently — the coordinator logic
         is transport-agnostic.
      3. TCP's native window and retransmission fields are still visible just
         as in scenario 09; sync annotations are purely at the JSON layer.

    Captures: tcp-server-sync.pcap  client-1.pcap
    """
    out = CAPTURES_DIR / "10_tcp_sync_large"

    big_json = ROOT / "bigjson.json"
    if not big_json.exists():
        print("  [warn]    bigjson.json not found — aborting")
        return
    _sh(["docker", "cp", str(big_json),
         "tcp-server-sync:/app/shared/exam.json"], check=True)
    print(f"  [exam]    bigjson.json ({big_json.stat().st_size // 1024} KB) "
          "→ tcp-server-sync:/app/shared/exam.json")

    _ensure_client_running("client-1", 8081)
    _clear_exam_start()
    _set_exam_start(30)

    sidecars = _start_captures(["tcp-server-sync", "client-1"])

    print("  [sim]     Single client via TCP+sync + large payload …")
    result = _run_sim(WS["c1_tcp_sync"], "student-tcp-sync-big", think_ms=3000)
    _print_results("tcp sync large", [result])

    _stop_captures(sidecars, out)

    _sh(["docker", "exec", "tcp-server-sync",
         "rm", "-f", "/app/shared/exam.json"])
    print("  [exam]    bigjson.json override removed")

    _wireshark_tip(out, {
        "All TCP exam traffic":
            "tcp.port == 9001",
        "Data segments only (no pure ACKs)":
            "tcp.port == 9001 && tcp.len > 0",
        "Read exam questions (plain JSON)":
            ("tcp.port == 9001 → Follow TCP Stream → exam questions readable as text"),
        "Sync metadata in exam_resp":
            ("Follow TCP Stream → search 'server_sent_at_ms' → present  "
             "also look for 'sync_hold_ms' and 'bridge_forwarded_at_ms'"),
        "TCP sliding window — Time/Sequence graph":
            ("Statistics → TCP Stream Graphs → Time-Sequence Graph  "
             "— slow-start staircase identical to no-sync (transport-agnostic sync)"),
        "Compare sync vs no-sync payload":
            ("open 09_tcp_nosync_large and 10_tcp_sync_large side-by-side  "
             "→ Follow TCP Stream on each → server_sent_at_ms absent/present"),
        "Extract sync timestamps with tshark":
            ('tshark -r tcp-server-sync.pcap '
             '-Y "tcp.port==9001 && tcp.len>0" '
             '-T fields -e frame.time_relative -e tcp.len '
             '> tcp_sync_segments.csv'),
    })


# ════════════════════════════════════════════════════════════════════════════

SCENARIOS: dict[int, tuple[str, callable]] = {
    1: ("Single client — DHCP + DNS + RUDP + full exam",    scenario_01_single_full_flow),
    2: ("Three clients — RUDP + ExamSendCoordinator sync",  scenario_02_three_clients_sync),
    3: ("Netem resilience — 150ms delay, self-correcting",  scenario_03_netem_resilience),
    4: ("Mid-exam disconnect — FIN/RST + autosave proof",   scenario_04_disconnect),
    5: ("TCP no-sync — baseline (uncoordinated delivery)",  scenario_05_tcp_nosync),
    6: ("TCP sync — same coordinator over TCP",             scenario_06_tcp_sync),
    7: ("Recursive DNS — resolver delegates to root",       scenario_07_recursive_dns),
    8: ("RUDP sliding window — large payload + software slow-send", scenario_08_rudp_sliding_window),
    9: ("TCP no-sync — large payload, questions + TCP window",  scenario_09_tcp_nosync_large),
   10: ("TCP sync   — large payload, questions + sync fields",  scenario_10_tcp_sync_large),
}


def _ensure_stack_running() -> None:
    """
    Check that the core services are up.
    If not, offer to bring them up automatically.
    """
    needed = ["dhcp-server", "dns-resolver", "rudp-server-1",
              "client-1", "client-2", "client-3"]
    running = set()
    r = _sh(["docker", "ps", "--format", "{{.Names}}"])
    for line in r.stdout.splitlines():
        running.add(line.strip())

    missing = [c for c in needed if c not in running]
    if not missing:
        print("  [check]   All core containers running ✓")
        return

    print(f"  [check]   Missing containers: {missing}")
    ans = input("  Bring up the stack now? [Y/n] ").strip().lower()
    if ans in ("", "y", "yes"):
        print("  [stack]   docker compose up --build -d  (this may take a while) …")
        _sh(["docker", "compose", "up", "--build", "-d"],
            check=True, timeout=300, quiet=False)
        print("  [stack]   Waiting 30 s for services to initialise …")
        time.sleep(30)
        # Wait for client ports to be reachable
        for port in (8081, 8082, 8083):
            try:
                _wait_ws_port(port, timeout=60)
            except TimeoutError:
                print(f"  [warn]    port {port} still not ready — continuing anyway")
    else:
        print("  Aborted. Start the stack manually and re-run.")
        sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Automated pcap scenario generator for exam-net",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Each scenario saves pcap files to captures/<NN_name>/",
    )
    ap.add_argument("--scenario", nargs="*", type=int, metavar="N",
                    help="Run specific scenario(s); omit to run all")
    ap.add_argument("--list", action="store_true",
                    help="Print available scenarios and exit")
    ap.add_argument("--no-stack-check", action="store_true",
                    help="Skip the stack-running check (if you know it's up)")
    args = ap.parse_args()

    if args.list:
        print("Available scenarios:")
        for n, (name, _) in SCENARIOS.items():
            print(f"  {n}.  {name}")
        return

    # Build the capture image once up front
    _build_pcap_image()

    if not args.no_stack_check:
        _ensure_stack_running()

    to_run = args.scenario or list(SCENARIOS.keys())
    CAPTURES_DIR.mkdir(exist_ok=True)

    for n in to_run:
        if n not in SCENARIOS:
            print(f"Unknown scenario {n}; use --list to see options.")
            continue
        name, fn = SCENARIOS[n]
        sep = "═" * 60
        print(f"\n{sep}")
        print(f"  Scenario {n}: {name}")
        print(f"{sep}")
        try:
            fn()
            print(f"\n  ✓ Scenario {n} complete → captures/{n:02d}_*/")
        except Exception as exc:
            import traceback
            print(f"\n  ✗ Scenario {n} failed: {exc}")
            traceback.print_exc()

    print(f"\n{'═'*60}")
    print("All done.  Open captures/<scenario>/*.pcap in Wireshark.")
    print("Merge all pcaps:  python capture_network.py merge")


if __name__ == "__main__":
    main()
