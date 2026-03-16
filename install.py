#!/usr/bin/env python3
"""
Exam Network Simulation — Full Project Installer
==================================================
Run after unzipping the project:

    python install.py          # interactive (recommended)
    python install.py --all    # install everything non-interactively
    python install.py --check  # only check prerequisites, don't install

Automatically installs all missing prerequisites:
  - Docker Desktop (via winget / brew / apt)
  - Node.js 20 LTS (via winget / brew / apt)
  - Git (via winget / brew / apt)
  - Python pip packages (fastapi, uvicorn, websockets, python-docx)
  - Frontend npm dependencies
  - Docker image builds
"""
from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional

# ── Constants ────────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent
FRONTEND_DIR = ROOT / "app" / "frontend"
DOWNLOADS_DIR = ROOT / "_installer_downloads"

REQUIRED_PYTHON = (3, 9)
REQUIRED_NODE = (18,)

HOST_PIP_PACKAGES = [
    "fastapi>=0.111.0",
    "uvicorn>=0.29.0",
    "websockets>=12.0",
    "python-docx>=1.0.0",
    "python-multipart>=0.0.9",
]

# ── Platform detection ───────────────────────────────────────────────────────

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
ARCH = platform.machine().lower()  # x86_64, amd64, arm64, aarch64

# Fix Windows console encoding
if IS_WIN:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ── Colors / Output ─────────────────────────────────────────────────────────

def _supports_color() -> bool:
    if IS_WIN:
        return os.environ.get("TERM") in ("xterm", "xterm-256color") or "WT_SESSION" in os.environ
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

USE_COLOR = _supports_color()

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text

def ok(msg: str):      print(f"  {_c('32', '+')} {msg}")
def warn(msg: str):    print(f"  {_c('33', '!')} {msg}")
def fail(msg: str):    print(f"  {_c('31', 'x')} {msg}")
def info(msg: str):    print(f"  {_c('36', '>')} {msg}")

def header(msg: str):
    w = 60
    print(f"\n{_c('1;34', '=' * w)}")
    print(f"  {_c('1;34', msg)}")
    print(f"{_c('1;34', '=' * w)}")

def ask(prompt: str, default: bool = True) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    try:
        resp = input(f"  {_c('33', '?')} {prompt} {hint} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not resp:
        return default
    return resp in ("y", "yes")


# ── Utility ──────────────────────────────────────────────────────────────────

def run(cmd: list[str], cwd: Optional[Path] = None, capture: bool = False,
        timeout: int = 300, shell: bool = False) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd, cwd=cwd, capture_output=capture, text=True,
            timeout=timeout, shell=shell,
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"Command not found: {cmd[0]}")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "Timeout")


def cmd_exists(name: str) -> bool:
    return shutil.which(name) is not None


def cmd_version(cmd: list[str]) -> Optional[str]:
    r = run(cmd, capture=True, timeout=10)
    if r.returncode == 0:
        return r.stdout.strip().splitlines()[0] if r.stdout.strip() else "unknown"
    return None


def parse_version(v: str) -> tuple[int, ...]:
    nums = re.findall(r'\d+', v)
    return tuple(int(n) for n in nums[:3])


def has_winget() -> bool:
    return IS_WIN and cmd_exists("winget")


def has_brew() -> bool:
    return (IS_MAC or IS_LINUX) and cmd_exists("brew")


def has_apt() -> bool:
    return IS_LINUX and cmd_exists("apt-get")


def download_file(url: str, dest: Path) -> bool:
    """Download a file with progress indication."""
    try:
        info(f"Downloading {dest.name}...")
        dest.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(url, str(dest))
        ok(f"Downloaded {dest.name} ({dest.stat().st_size / 1024 / 1024:.1f} MB)")
        return True
    except Exception as e:
        fail(f"Download failed: {e}")
        return False


def run_as_admin_win(cmd: str) -> bool:
    """Run a command with UAC elevation on Windows."""
    import ctypes
    info(f"Requesting administrator privileges...")
    ret = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", "cmd.exe", f"/c {cmd}", None, 1
    )
    return ret > 32


# ══════════════════════════════════════════════════════════════════════════════
#  Installers for each prerequisite
# ══════════════════════════════════════════════════════════════════════════════

def install_docker(interactive: bool) -> bool:
    """Install Docker Desktop."""
    info("Docker is required to run this project.")
    if interactive and not ask("Install Docker Desktop?"):
        return False

    if IS_WIN:
        if has_winget():
            info("Installing Docker Desktop via winget...")
            r = run(["winget", "install", "-e", "--id", "Docker.DockerDesktop",
                      "--accept-source-agreements", "--accept-package-agreements"],
                    timeout=600)
            if r.returncode == 0:
                ok("Docker Desktop installed via winget")
                warn("You need to RESTART your computer or log out/in for Docker to work.")
                warn("After restart, open Docker Desktop and wait for it to start.")
                return True
        # Fallback: direct download
        installer = DOWNLOADS_DIR / "DockerDesktopInstaller.exe"
        url = "https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe"
        if download_file(url, installer):
            info("Running Docker Desktop installer...")
            info("Follow the installer prompts. Enable WSL 2 when asked.")
            r = run([str(installer), "install", "--quiet"], timeout=600)
            if r.returncode == 0:
                ok("Docker Desktop installed")
                warn("RESTART your computer, then open Docker Desktop.")
                return True
            else:
                # Try interactive install
                os.startfile(str(installer))
                warn("Docker installer opened. Follow the prompts.")
                warn("After install, RESTART and re-run this script.")
                return True

    elif IS_MAC:
        if has_brew():
            info("Installing Docker Desktop via Homebrew...")
            r = run(["brew", "install", "--cask", "docker"], timeout=600)
            if r.returncode == 0:
                ok("Docker Desktop installed via brew")
                info("Open Docker Desktop from Applications to start the daemon.")
                return True
        fail("Install Docker Desktop manually: https://docker.com/products/docker-desktop")
        return False

    elif IS_LINUX:
        info("Installing Docker Engine via official script...")
        script = DOWNLOADS_DIR / "get-docker.sh"
        if download_file("https://get.docker.com", script):
            r = run(["sudo", "sh", str(script)], timeout=300)
            if r.returncode == 0:
                # Add user to docker group
                user = os.environ.get("USER", "")
                if user:
                    run(["sudo", "usermod", "-aG", "docker", user])
                    info(f"Added {user} to docker group. Log out/in to apply.")
                # Install compose plugin
                run(["sudo", "apt-get", "install", "-y", "docker-compose-plugin"],
                    timeout=120)
                ok("Docker Engine installed")
                run(["sudo", "systemctl", "start", "docker"], timeout=30)
                run(["sudo", "systemctl", "enable", "docker"], timeout=30)
                return True
        fail("Docker install failed. See: https://docs.docker.com/engine/install/")
        return False

    fail("Install Docker Desktop manually: https://docker.com/products/docker-desktop")
    return False


def install_node(interactive: bool) -> bool:
    """Install Node.js 20 LTS."""
    info("Node.js is needed for frontend development (optional — Docker builds it internally).")
    if interactive and not ask("Install Node.js 20 LTS?"):
        return False

    if IS_WIN:
        if has_winget():
            info("Installing Node.js via winget...")
            r = run(["winget", "install", "-e", "--id", "OpenJS.NodeJS.LTS",
                      "--accept-source-agreements", "--accept-package-agreements"],
                    timeout=300)
            if r.returncode == 0:
                ok("Node.js installed via winget. Restart your terminal.")
                return True
        # Fallback: direct download
        installer = DOWNLOADS_DIR / "node-setup.msi"
        url = "https://nodejs.org/dist/v20.18.1/node-v20.18.1-x64.msi"
        if download_file(url, installer):
            info("Running Node.js installer...")
            r = run(["msiexec", "/i", str(installer), "/quiet", "/norestart"], timeout=300)
            if r.returncode == 0:
                ok("Node.js installed. Restart your terminal.")
                return True
            os.startfile(str(installer))
            warn("Node.js installer opened. Follow the prompts.")
            return True

    elif IS_MAC:
        if has_brew():
            r = run(["brew", "install", "node@20"], timeout=300)
            if r.returncode == 0:
                ok("Node.js 20 installed via brew")
                return True

    elif IS_LINUX:
        if has_apt():
            info("Installing Node.js 20 via NodeSource...")
            run(["sudo", "bash", "-c",
                 "curl -fsSL https://deb.nodesource.com/setup_20.x | bash -"],
                timeout=120)
            r = run(["sudo", "apt-get", "install", "-y", "nodejs"], timeout=120)
            if r.returncode == 0:
                ok("Node.js installed")
                return True

    fail("Install Node.js manually: https://nodejs.org/")
    return False


def install_git(interactive: bool) -> bool:
    """Install Git."""
    if interactive and not ask("Install Git?"):
        return False

    if IS_WIN and has_winget():
        r = run(["winget", "install", "-e", "--id", "Git.Git",
                  "--accept-source-agreements", "--accept-package-agreements"],
                timeout=300)
        if r.returncode == 0:
            ok("Git installed via winget. Restart your terminal.")
            return True
    elif IS_MAC and has_brew():
        r = run(["brew", "install", "git"], timeout=120)
        if r.returncode == 0:
            ok("Git installed via brew")
            return True
    elif IS_LINUX and has_apt():
        r = run(["sudo", "apt-get", "install", "-y", "git"], timeout=120)
        if r.returncode == 0:
            ok("Git installed")
            return True

    fail("Install Git manually: https://git-scm.com/downloads")
    return False


def install_pip() -> bool:
    """Ensure pip is available."""
    r = run([sys.executable, "-m", "ensurepip", "--upgrade"], timeout=60)
    if r.returncode == 0:
        ok("pip installed via ensurepip")
        return True
    # Try get-pip.py
    getpip = DOWNLOADS_DIR / "get-pip.py"
    if download_file("https://bootstrap.pypa.io/get-pip.py", getpip):
        r = run([sys.executable, str(getpip)], timeout=60)
        if r.returncode == 0:
            ok("pip installed via get-pip.py")
            return True
    fail("Could not install pip")
    return False


def start_docker_daemon(interactive: bool) -> bool:
    """Try to start Docker Desktop / daemon."""
    if IS_WIN:
        # Try common Docker Desktop paths
        docker_paths = [
            Path(os.environ.get("ProgramFiles", "C:\\Program Files")) / "Docker" / "Docker" / "Docker Desktop.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Docker" / "Docker Desktop.exe",
        ]
        for p in docker_paths:
            if p.exists():
                if interactive and not ask("Start Docker Desktop?"):
                    return False
                info(f"Starting Docker Desktop...")
                os.startfile(str(p))
                info("Waiting for Docker to start (up to 60s)...")
                for i in range(12):
                    time.sleep(5)
                    r = run(["docker", "info"], capture=True, timeout=5)
                    if r.returncode == 0:
                        ok("Docker daemon is running")
                        return True
                    info(f"  Still waiting... ({(i+1)*5}s)")
                warn("Docker started but may need more time. Wait and re-run.")
                return False
        warn("Docker Desktop not found. Start it manually.")
        return False

    elif IS_LINUX:
        r = run(["sudo", "systemctl", "start", "docker"], timeout=15)
        if r.returncode == 0:
            ok("Docker daemon started")
            return True

    elif IS_MAC:
        r = run(["open", "-a", "Docker"], timeout=10)
        if r.returncode == 0:
            info("Waiting for Docker to start (up to 60s)...")
            for i in range(12):
                time.sleep(5)
                if run(["docker", "info"], capture=True, timeout=5).returncode == 0:
                    ok("Docker daemon is running")
                    return True
            warn("Docker started but may need more time.")
            return False

    return False


# ══════════════════════════════════════════════════════════════════════════════
#  Step 1: Check & Install Prerequisites
# ══════════════════════════════════════════════════════════════════════════════

def check_and_install_prerequisites(interactive: bool, install: bool) -> dict:
    header("Step 1/5 -- Checking & Installing Prerequisites")
    results = {}
    needs_restart = False

    # ── Python ───────────────────────────────────────────────────────────────
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info >= REQUIRED_PYTHON:
        ok(f"Python {py_ver}")
        results["python"] = True
    else:
        fail(f"Python {py_ver} (need >= {'.'.join(map(str, REQUIRED_PYTHON))})")
        fail("You're running this script with an old Python. Install Python 3.11+ and re-run.")
        results["python"] = False

    # ── pip ───────────────────────────────────────────────────────────────────
    pip_ver = cmd_version([sys.executable, "-m", "pip", "--version"])
    if pip_ver:
        ok(f"pip: {pip_ver}")
        results["pip"] = True
    else:
        warn("pip not found")
        if install:
            results["pip"] = install_pip()
        else:
            results["pip"] = False

    # ── Git ───────────────────────────────────────────────────────────────────
    git_ver = cmd_version(["git", "--version"])
    if git_ver:
        ok(f"Git: {git_ver}")
        results["git"] = True
    else:
        warn("Git not found")
        if install:
            results["git"] = install_git(interactive)
            if results["git"]:
                needs_restart = True
        else:
            results["git"] = False

    # ── Node.js ──────────────────────────────────────────────────────────────
    node_ver = cmd_version(["node", "--version"])
    if node_ver and parse_version(node_ver) >= REQUIRED_NODE:
        ok(f"Node.js {node_ver}")
        results["node"] = True
    else:
        if node_ver:
            warn(f"Node.js {node_ver} is too old (need >= {REQUIRED_NODE[0]})")
        else:
            warn("Node.js not found")
        if install:
            results["node"] = install_node(interactive)
            if results["node"]:
                needs_restart = True
        else:
            results["node"] = False

    # ── npm ──────────────────────────────────────────────────────────────────
    npm_cmd = "npm.cmd" if IS_WIN else "npm"
    npm_ver = cmd_version([npm_cmd, "--version"])
    if npm_ver:
        ok(f"npm {npm_ver}")
        results["npm"] = True
    else:
        results["npm"] = results.get("node", False)  # npm comes with node

    # ── Docker ───────────────────────────────────────────────────────────────
    docker_ver = cmd_version(["docker", "--version"])
    if docker_ver:
        ok(f"Docker: {docker_ver}")
        results["docker"] = True
    else:
        fail("Docker not found")
        if install:
            results["docker"] = install_docker(interactive)
            if results["docker"]:
                needs_restart = True
        else:
            results["docker"] = False

    # ── Docker Compose ───────────────────────────────────────────────────────
    compose_ver = cmd_version(["docker", "compose", "version"])
    if not compose_ver:
        compose_ver = cmd_version(["docker-compose", "--version"])
    if compose_ver:
        ok(f"Docker Compose: {compose_ver}")
        results["compose"] = True
    else:
        if results.get("docker"):
            warn("Docker Compose not found (should come with Docker Desktop)")
        results["compose"] = results.get("docker", False)

    # ── Docker daemon ────────────────────────────────────────────────────────
    results["docker_running"] = False
    if results.get("docker"):
        r = run(["docker", "info"], capture=True, timeout=15)
        if r.returncode == 0:
            ok("Docker daemon is running")
            results["docker_running"] = True
        else:
            warn("Docker installed but daemon is not running")
            if install:
                results["docker_running"] = start_docker_daemon(interactive)

    # ── Restart warning ──────────────────────────────────────────────────────
    if needs_restart:
        print()
        warn("=" * 55)
        warn("Some tools were just installed. You may need to:")
        warn("  1. RESTART your terminal / command prompt")
        warn("  2. Re-run: python install.py")
        warn("so the newly installed tools are on your PATH.")
        warn("=" * 55)
        results["needs_restart"] = True

    return results


# ══════════════════════════════════════════════════════════════════════════════
#  Step 2: Install Host Python Packages
# ══════════════════════════════════════════════════════════════════════════════

def install_host_python_packages(interactive: bool = True) -> bool:
    header("Step 2/5 -- Installing Host Python Packages")
    info("Needed for: prof_dashboard.py, run_scenarios.py, generate_report.py")
    print()
    for pkg in HOST_PIP_PACKAGES:
        info(f"  {pkg}")
    print()

    if interactive and not ask("Install host Python packages?"):
        warn("Skipped")
        return False

    r = run(
        [sys.executable, "-m", "pip", "install", "--upgrade"] + HOST_PIP_PACKAGES,
        cwd=ROOT,
    )
    if r.returncode == 0:
        ok("All host Python packages installed")
        return True
    else:
        fail("Some packages failed to install -- check output above")
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  Step 3: Install Frontend Dependencies
# ══════════════════════════════════════════════════════════════════════════════

def install_frontend(interactive: bool = True) -> bool:
    header("Step 3/5 -- Frontend Dependencies (optional)")
    info("Docker builds the frontend internally. This is only for local dev.")
    print()

    if not (FRONTEND_DIR / "package.json").exists():
        fail(f"package.json not found at {FRONTEND_DIR}")
        return False

    if interactive and not ask("Install frontend Node dependencies? (optional)", default=False):
        warn("Skipped (Docker will handle it)")
        return False

    npm_cmd = "npm.cmd" if IS_WIN else "npm"
    r = run([npm_cmd, "ci"], cwd=FRONTEND_DIR)
    if r.returncode == 0:
        ok("Frontend dependencies installed")
        return True
    r = run([npm_cmd, "install"], cwd=FRONTEND_DIR)
    if r.returncode == 0:
        ok("Frontend dependencies installed (npm install)")
        return True
    fail("Frontend install failed")
    return False


# ══════════════════════════════════════════════════════════════════════════════
#  Step 4: Build Docker Images
# ══════════════════════════════════════════════════════════════════════════════

def build_docker(interactive: bool = True) -> bool:
    header("Step 4/5 -- Building Docker Images")
    info("Builds 7 images: DNS, RUDP, TCP, DHCP server, DHCP client, Admin, Capture")
    info("First build takes 3-5 minutes (downloads base images).")
    print()

    if interactive and not ask("Build Docker images now?"):
        warn("Skipped -- run manually: docker compose build")
        return False

    info("Building... (this may take a few minutes)")
    start = time.time()
    r = run(["docker", "compose", "build"], cwd=ROOT, timeout=600)
    elapsed = time.time() - start

    if r.returncode == 0:
        ok(f"All Docker images built ({elapsed:.0f}s)")
        return True
    else:
        fail("Docker build failed -- check output above")
        info("Common fixes:")
        info("  - Make sure Docker Desktop is running")
        info("  - Check internet connection (pulls python:3.11-slim, node:20-alpine)")
        info("  - Try: docker compose build --no-cache")
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  Step 5: Validate Setup
# ══════════════════════════════════════════════════════════════════════════════

def validate_setup() -> bool:
    header("Step 5/5 -- Validating Setup")
    all_ok = True

    critical_files = [
        "docker-compose.yml",
        "DNS/Dockerfile", "DNS/dns_packet.py", "DNS/dns_server.py",
        "DNS/root_server.py", "DNS/tld_server.py", "DNS/auth_server.py",
        "DNS/resolver_server.py",
        "RUDP/Dockerfile.server", "RUDP/rudp_packet.py", "RUDP/rudp_socket.py",
        "RUDP/exam_server.py", "RUDP/ws_bridge.py",
        "TCP/Dockerfile.server", "TCP/exam_server_tcp.py", "TCP/ws_bridge_tcp.py",
        "DHCP/Dockerfile.server", "DHCP/Dockerfile.client",
        "DHCP/dhcp_server.py", "DHCP/dhcp_client.py",
        "DHCP/entrypoint.sh", "DHCP/nginx.conf", "DHCP/supervisord.conf",
        "admin/Dockerfile", "admin/admin_server.py",
        "app/frontend/src/App.tsx", "app/frontend/package.json",
        "prof_dashboard.py",
    ]

    missing = [f for f in critical_files if not (ROOT / f).exists()]
    if missing:
        fail(f"{len(missing)} critical files missing:")
        for f in missing[:10]:
            fail(f"  {f}")
        if len(missing) > 10:
            fail(f"  ... and {len(missing) - 10} more")
        all_ok = False
    else:
        ok(f"All {len(critical_files)} critical files present")

    # Docker Compose config
    r = run(["docker", "compose", "config", "--images"], cwd=ROOT, capture=True, timeout=15)
    if r.returncode == 0:
        images = [l.strip() for l in r.stdout.splitlines() if l.strip()]
        ok(f"Docker Compose config valid ({len(images)} images)")
    else:
        warn("Could not validate Docker Compose config (Docker not running?)")

    # Host packages
    host_ok = True
    for pkg_name in ["fastapi", "uvicorn", "websockets", "docx"]:
        try:
            __import__(pkg_name)
        except ImportError:
            if host_ok:
                warn("Missing host Python packages:")
            warn(f"  {pkg_name}")
            host_ok = False
    if host_ok:
        ok("All host Python packages importable")

    if all_ok and host_ok:
        ok("Setup validation passed")
    return all_ok


# ══════════════════════════════════════════════════════════════════════════════
#  Print Usage Guide
# ══════════════════════════════════════════════════════════════════════════════

def print_usage():
    header("Setup Complete -- Quick Start Guide")
    print(f"""
  {_c('1', 'Start the full system:')}
    cd {ROOT}
    docker compose up --build

  {_c('1', 'Open exam clients in browser:')}
    http://localhost:8081?mode=rudp-sync
    http://localhost:8082?mode=tcp-sync
    http://localhost:8083?mode=tcp-nosync

  {_c('1', 'Admin panel:')}
    http://localhost:9999

  {_c('1', 'Professor dashboard (run on host):')}
    python prof_dashboard.py
    http://localhost:7777

  {_c('1', 'Run automated scenarios:')}
    python run_scenarios.py

  {_c('1', 'Start with packet capture:')}
    docker compose --profile capture up --build

  {_c('1', 'Generate report document:')}
    python generate_report.py

  {_c('1', 'Scale to more clients:')}
    docker compose up --build --scale client=5

  {_c('1', 'Stop everything:')}
    docker compose down -v

  {_c('1', 'Network layout:')}
    10.99.0.2     DNS Resolver
    10.99.0.3     DHCP Server
    10.99.0.10    DNS Root
    10.99.0.11    DNS TLD (.lan)
    10.99.0.12    DNS Auth (exam.lan)
    10.99.0.20-22 RUDP Exam Servers
    10.99.0.23    TCP Sync Server
    10.99.0.24    TCP NoSync Server
    10.99.0.30    Admin Panel
    10.99.0.100+  Client Pool (DHCP)
""")


# ══════════════════════════════════════════════════════════════════════════════
#  Cleanup
# ══════════════════════════════════════════════════════════════════════════════

def cleanup():
    if DOWNLOADS_DIR.exists():
        try:
            shutil.rmtree(DOWNLOADS_DIR)
            info("Cleaned up installer downloads.")
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Exam Network Simulation -- Full Project Installer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python install.py          # interactive (recommended)
  python install.py --all    # install everything non-interactively
  python install.py --check  # only check prerequisites
        """,
    )
    parser.add_argument("--all", action="store_true",
                        help="Install everything non-interactively")
    parser.add_argument("--check", action="store_true",
                        help="Only check prerequisites, don't install anything")
    parser.add_argument("--skip-docker-build", action="store_true",
                        help="Skip Docker image build step")
    parser.add_argument("--skip-frontend", action="store_true",
                        help="Skip frontend npm install")
    args = parser.parse_args()

    print(f"\n{_c('1;36', '  Exam Network Simulation -- Full Installer')}")
    print(f"  {_c('90', 'DNS + RUDP + TCP synchronized exam delivery')}")
    print(f"  {_c('90', f'Project root: {ROOT}')}")
    print(f"  {_c('90', f'Platform: {sys.platform} / {platform.machine()}')}\n")

    interactive = not args.all
    install = not args.check

    # Step 1: Check & install prerequisites
    prereqs = check_and_install_prerequisites(interactive, install)

    if args.check:
        critical = prereqs.get("python") and prereqs.get("docker") and prereqs.get("compose")
        if critical:
            print(f"\n  {_c('32', 'All critical prerequisites met.')}")
        else:
            print(f"\n  {_c('31', 'Missing critical prerequisites -- see above.')}")
        sys.exit(0 if critical else 1)

    if prereqs.get("needs_restart"):
        print()
        warn("Please RESTART your terminal and re-run: python install.py")
        sys.exit(0)

    if not prereqs.get("python"):
        fail("Python >= 3.9 is required. Install from https://python.org and re-run.")
        sys.exit(1)

    # Step 2: Host Python packages
    if prereqs.get("pip"):
        install_host_python_packages(interactive=interactive)
    else:
        warn("Skipping pip packages (pip not available)")

    # Step 3: Frontend (optional)
    if not args.skip_frontend:
        if args.all:
            warn("Skipping frontend npm install (Docker handles it)")
        else:
            install_frontend(interactive=True)

    # Step 4: Docker build
    if not args.skip_docker_build:
        if prereqs.get("docker_running"):
            build_docker(interactive=interactive)
        elif prereqs.get("docker"):
            warn("Docker daemon not running -- skipping image build")
            info("Start Docker Desktop, then run: docker compose build")
        else:
            warn("Docker not installed -- skipping image build")
    else:
        warn("Skipped Docker build (--skip-docker-build)")

    # Step 5: Validate
    validate_setup()

    # Cleanup downloads
    cleanup()

    # Usage guide
    print_usage()


if __name__ == "__main__":
    main()
