#!/usr/bin/env python3
"""capture_network.py — Wireshark-ready packet capture for the exam network.

Captures traffic from every container on the exam-net Docker bridge,
producing per-container .pcap files in the captures/ directory.

Usage:
    python capture_network.py start                 # capture all containers
    python capture_network.py start --duration 120  # auto-stop after 120 s
    python capture_network.py stop                  # stop and show file sizes
    python capture_network.py status                # show active captures
    python capture_network.py merge                 # merge into combined.pcap
    python capture_network.py summary               # list all pcap files

Docker Compose alternative (recommended):
    docker compose --profile capture up -d
    docker compose --profile capture down
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
CAPTURES_DIR = PROJECT_ROOT / "captures"
CAPTURE_IMAGE = "alpine:3.19"
PREFIX = "pcap-"

# Known Docker network names (compose project name may vary)
NETWORK_CANDIDATES = [
    "finalproject-computernetworks_exam-net",
    "finalprojectcomputernetworks_exam-net",
    "exam-net",
]


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def _fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ── Discovery ────────────────────────────────────────────────────────────────

def discover_containers():
    """Return {container_name: ip} for every container on exam-net."""
    for net in NETWORK_CANDIDATES:
        r = _run(["docker", "network", "inspect", net])
        if r.returncode == 0:
            data = json.loads(r.stdout)
            containers = {}
            for _id, info in data[0].get("Containers", {}).items():
                name = info["Name"]
                ip = info.get("IPv4Address", "").split("/")[0]
                containers[name] = ip
            if containers:
                return containers
    print("ERROR: Could not find exam-net network. Is the stack running?")
    sys.exit(1)


# ── Start / Stop ─────────────────────────────────────────────────────────────

def start_captures(containers=None, duration=None):
    """Launch a sidecar capture container for each target, sharing its netns."""
    CAPTURES_DIR.mkdir(exist_ok=True)
    if containers is None:
        containers = discover_containers()

    active = []
    for name, ip in sorted(containers.items()):
        cap_name = f"{PREFIX}{name}"
        # remove stale sidecar if any
        _run(["docker", "rm", "-f", cap_name])

        pcap_path = f"/captures/{name}.pcap"
        tcpdump_cmd = f"tcpdump -i any -s 0 -U -w {pcap_path}"
        if duration:
            tcpdump_cmd = f"timeout {duration} {tcpdump_cmd} ; true"

        captures_posix = str(CAPTURES_DIR).replace("\\", "/")
        cmd = [
            "docker", "run", "-d", "--rm",
            "--name", cap_name,
            f"--net=container:{name}",
            "--cap-add=NET_ADMIN", "--cap-add=NET_RAW",
            "-v", f"{captures_posix}:/captures",
            CAPTURE_IMAGE,
            "sh", "-c",
            f"apk add --no-cache tcpdump >/dev/null 2>&1 && {tcpdump_cmd}",
        ]

        r = _run(cmd)
        if r.returncode == 0:
            print(f"  + {name:30s} ({ip:>13s}) -> captures/{name}.pcap")
            active.append(cap_name)
        else:
            print(f"  x {name:30s} -- {r.stderr.strip()[:120]}")

    return active


def stop_captures():
    """Stop all running pcap-* sidecar containers."""
    r = _run(["docker", "ps", "--filter", f"name={PREFIX}",
              "--format", "{{.Names}}"])
    names = [n.strip() for n in r.stdout.strip().split("\n") if n.strip()]
    if not names:
        print("No active captures found.")
        return

    for cap_name in sorted(names):
        _run(["docker", "stop", "-t", "2", cap_name])
        target = cap_name.replace(PREFIX, "", 1)
        pcap = CAPTURES_DIR / f"{target}.pcap"
        size = pcap.stat().st_size if pcap.exists() else 0
        print(f"  - {target:30s}  {_fmt_size(size):>10}")


def status():
    """Show currently running capture sidecars."""
    r = _run(["docker", "ps", "--filter", f"name={PREFIX}",
              "--format", "table {{.Names}}\t{{.Status}}\t{{.RunningFor}}"])
    print(r.stdout if r.stdout.strip() else "No active captures.")


# ── Merge ────────────────────────────────────────────────────────────────────

def merge_pcaps():
    """Merge all .pcap files into captures/combined.pcap using mergecap."""
    mergecap = None
    for candidate in ["mergecap",
                      r"C:\Program Files\Wireshark\mergecap.exe",
                      r"C:\Program Files (x86)\Wireshark\mergecap.exe"]:
        if os.path.isfile(candidate) or _run(["where" if os.name == "nt" else "which",
                                               candidate]).returncode == 0:
            mergecap = candidate
            break
    if not mergecap:
        print("ERROR: mergecap not found. Install Wireshark or add it to PATH.")
        return

    pcaps = sorted(p for p in CAPTURES_DIR.glob("*.pcap") if p.name != "combined.pcap")
    if not pcaps:
        print("No pcap files found in captures/")
        return

    output = CAPTURES_DIR / "combined.pcap"
    r = _run([mergecap, "-w", str(output)] + [str(p) for p in pcaps])
    if r.returncode == 0:
        print(f"Merged {len(pcaps)} files -> {output}")
        print(f"  Size: {_fmt_size(output.stat().st_size)}")
    else:
        print(f"Merge failed: {r.stderr}")


# ── Summary ──────────────────────────────────────────────────────────────────

IP_MAP = {
    "dhcp-server":      "10.99.0.3",
    "dns-root":         "10.99.0.10",
    "dns-tld":          "10.99.0.11",
    "dns-auth":         "10.99.0.12",
    "dns-resolver":     "10.99.0.2",
    "rudp-server-1":    "10.99.0.20",
    "rudp-server-2":    "10.99.0.21",
    "rudp-server-3":    "10.99.0.22",
    "tcp-server-sync":  "10.99.0.23",
    "tcp-server-nosync":"10.99.0.24",
    "admin":            "10.99.0.30",
}

FILTERS = {
    "DHCP traffic":       "bootp || dhcp",
    "DNS traffic":        "dns",
    "RUDP (port 9000)":   "udp.port == 9000",
    "TCP exam (port 9001)":"tcp.port == 9001",
    "WebSocket upgrades":  "http.upgrade",
}


def summary():
    """Print all .pcap files with sizes and Wireshark tips."""
    pcaps = sorted(CAPTURES_DIR.glob("*.pcap"))
    if not pcaps:
        print("No captures found.  Run 'start' first.")
        return

    print(f"\n{'Container':<30} {'Size':>10}  IP Address")
    print("-" * 65)
    total = 0
    for p in pcaps:
        size = p.stat().st_size
        total += size
        name = p.stem
        ip = IP_MAP.get(name, "")
        print(f"  {name:<28} {_fmt_size(size):>10}  {ip}")
    print("-" * 65)
    print(f"  {'TOTAL':<28} {_fmt_size(total):>10}  ({len(pcaps)} files)")

    print("\nWireshark display filters:")
    for label, filt in FILTERS.items():
        print(f"  {label:<25} {filt}")
    print(f"\n  Filter by container IP:    ip.addr == 10.99.0.20")
    print(f"  Open:  wireshark captures/<file>.pcap")
    print(f"  Merge: python capture_network.py merge  ->  captures/combined.pcap\n")


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Automated Wireshark packet capture for exam-net containers")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("start", help="Start per-container captures")
    sp.add_argument("--duration", type=int, metavar="SEC",
                    help="Auto-stop after N seconds")
    sp.add_argument("--containers", nargs="*", metavar="NAME",
                    help="Capture only these containers (default: all)")

    sub.add_parser("stop",    help="Stop all active captures")
    sub.add_parser("status",  help="Show active capture containers")
    sub.add_parser("merge",   help="Merge all pcaps into combined.pcap")
    sub.add_parser("summary", help="List captured files with sizes")

    args = p.parse_args()

    if args.cmd == "start":
        print("Discovering containers on exam-net...")
        targets = discover_containers()
        if args.containers:
            targets = {k: v for k, v in targets.items() if k in args.containers}
        print(f"Found {len(targets)} containers.  Starting captures...\n")
        active = start_captures(targets, duration=args.duration)
        print(f"\n{len(active)} captures running.")
        print("Stop with:  python capture_network.py stop")

    elif args.cmd == "stop":
        print("Stopping captures...\n")
        stop_captures()
        summary()

    elif args.cmd == "status":
        status()

    elif args.cmd == "merge":
        merge_pcaps()

    elif args.cmd == "summary":
        summary()

    else:
        p.print_help()
        print("\nQuick start:")
        print("  python capture_network.py start       # begin capturing")
        print("  python capture_network.py stop        # stop  + summary")
        print("  python capture_network.py merge       # combine all pcaps")
        print("\nDocker Compose (recommended):")
        print("  docker compose --profile capture up -d")
        print("  docker compose --profile capture down")


if __name__ == "__main__":
    main()
