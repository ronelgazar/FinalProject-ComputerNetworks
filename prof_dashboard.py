#!/usr/bin/env python3
"""
prof_dashboard.py — Professor's live control panel for the Exam Network demo.

Serves a web dashboard at http://localhost:7777

Features:
  · Real-time container status (all 12 services via SSE)
  · Per-client network interference: latency / jitter / packet-loss (tc netem)
  · One-click "Open exam client" buttons — opens each student tab in the browser
  · Live 3-client simulation with streaming progress events
  · DNS iterative resolution trace visualization
  · Synchronized delivery timing-proof table + RTT chart
  · Link to admin panel (port 9999)

Usage:
    pip install fastapi uvicorn websockets
    python prof_dashboard.py
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import socket
import string
import subprocess
import sys
import time
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Dict, List, Optional

# ── dependency check ──────────────────────────────────────────────────────────
for pkg in ("fastapi", "uvicorn", "websockets"):
    try:
        __import__(pkg)
    except ImportError:
        print(f"Missing: pip install {pkg}")
        sys.exit(1)

import uvicorn
import websockets
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse

# ── Config ────────────────────────────────────────────────────────────────────
DASHBOARD_PORT = 7777
ADMIN_URL      = "http://localhost:9999"
CLIENT_PORTS   = [8081, 8082, 8083]
NTP_ROUNDS     = 12
PROJECT_ROOT   = os.path.dirname(os.path.abspath(__file__))

# ── Globals ───────────────────────────────────────────────────────────────────
netem_state: Dict[int, Dict] = {p: {"delay": 0, "jitter": 0, "loss": 0.0} for p in CLIENT_PORTS}
_sim_running       = False
_load_test_running = False
_sync_cmp_running  = False

MODE_WS_PATH: Dict[str, str] = {
    "rudp-sync":  "/ws",
    "tcp-sync":   "/ws-tcp-sync",
    "tcp-nosync": "/ws-tcp-nosync",
}

# ─────────────────────────────────────────────────────────────────────────────
# HTML DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Exam Network — Professor Dashboard</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{background:#0f172a;color:#e2e8f0;font-family:'Segoe UI',system-ui,sans-serif;font-size:14px;min-height:100vh}
::-webkit-scrollbar{width:6px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:#334155;border-radius:99px}

/* Layout */
.navbar{background:#1e293b;border-bottom:1px solid #334155;padding:14px 24px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:50}
.grid{display:grid;grid-template-columns:320px 1fr;gap:16px;padding:16px;max-width:1400px;margin:0 auto}
.card{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:16px}
.card-title{font-weight:700;font-size:13px;color:#94a3b8;text-transform:uppercase;letter-spacing:.06em;margin-bottom:12px}

/* Badges */
.badge{display:inline-flex;align-items:center;gap:5px;padding:3px 10px;border-radius:99px;font-size:12px;font-weight:600}
.badge-ok{background:#14532d;color:#4ade80}.badge-err{background:#7f1d1d;color:#f87171}
.badge-warn{background:#78350f;color:#fbbf24}.badge-blue{background:#1e3a5f;color:#60a5fa}
.dot{width:7px;height:7px;border-radius:50%;background:currentColor}

/* Status grid */
.svc-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}
.svc-item{background:#0f172a;border-radius:8px;padding:8px 10px;display:flex;align-items:center;gap:8px}
.svc-dot{width:9px;height:9px;border-radius:50%;flex-shrink:0}
.svc-name{font-size:12px;font-family:monospace;color:#94a3b8;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}

/* Sliders */
.slider-group{margin-bottom:14px}
.slider-row{display:flex;align-items:center;gap:8px;margin-bottom:4px}
.slider-label{font-size:11px;color:#64748b;width:44px}
.slider-val{font-size:11px;color:#60a5fa;font-family:monospace;width:44px;text-align:right}
input[type=range]{flex:1;accent-color:#3b82f6;height:4px;cursor:pointer}
.client-card{background:#0f172a;border-radius:10px;padding:12px;margin-bottom:10px;border:1px solid #1e3a5f}
.client-title{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
.client-name{font-weight:700;font-size:13px}
.port-badge{font-size:11px;font-family:monospace;color:#60a5fa;background:#172554;padding:2px 8px;border-radius:99px}

/* Buttons */
.btn{padding:7px 14px;border-radius:8px;border:none;font-family:inherit;font-size:13px;font-weight:600;cursor:pointer;transition:opacity .15s}
.btn:disabled{opacity:.4;cursor:not-allowed}
.btn-blue{background:#2563eb;color:#fff}.btn-blue:hover:not(:disabled){background:#1d4ed8}
.btn-red{background:#dc2626;color:#fff}.btn-red:hover:not(:disabled){background:#b91c1c}
.btn-ghost{background:#1e293b;color:#94a3b8;border:1px solid #334155}.btn-ghost:hover{background:#334155;color:#e2e8f0}
.btn-green{background:#16a34a;color:#fff}.btn-green:hover:not(:disabled){background:#15803d}
.btn-row{display:flex;gap:8px;margin-top:8px}
.preset-row{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}

/* Table */
table{width:100%;border-collapse:collapse;font-size:12px}
th{color:#64748b;font-weight:600;padding:6px 8px;text-align:left;border-bottom:1px solid #1e293b}
td{padding:6px 8px;border-bottom:1px solid #0f172a;font-family:monospace}
tr:hover td{background:#0f172a}

/* DNS trace */
.dns-step{display:flex;align-items:center;gap:8px;padding:8px 10px;background:#0f172a;border-radius:8px;margin-bottom:6px}
.dns-num{width:22px;height:22px;background:#1e3a5f;border-radius:50%;display:grid;place-items:center;font-size:11px;font-weight:700;color:#60a5fa;flex-shrink:0}
.dns-role{font-size:11px;color:#94a3b8;width:70px}
.dns-server{font-family:monospace;font-size:11px;color:#60a5fa;width:95px}
.dns-rtt{font-size:11px;color:#64748b;width:48px;text-align:right}
.dns-result{font-size:11px;color:#4ade80;flex:1}
.dns-arrow{color:#334155;flex-shrink:0}

/* Simulation */
.sim-progress{display:grid;gap:8px}
.sim-client{background:#0f172a;border-radius:8px;padding:10px 14px;display:flex;align-items:center;gap:12px}
.sim-port{font-family:monospace;font-weight:700;color:#60a5fa;width:42px}
.sim-bar-wrap{flex:1;background:#1e293b;border-radius:99px;height:8px;overflow:hidden}
.sim-bar{height:8px;border-radius:99px;background:#3b82f6;transition:width .4s ease}
.sim-status{font-size:11px;color:#94a3b8;width:120px;text-align:right}
.sim-rtt{font-size:11px;font-family:monospace;color:#4ade80;width:60px;text-align:right}

/* Sync comparison */
.sync-cmp-canvas{width:100%;height:160px;display:block}
.sync-legend{display:flex;gap:16px;flex-wrap:wrap;font-size:11px;margin-top:6px;margin-bottom:10px}
.sync-legend span{display:flex;align-items:center;gap:5px}
.sync-alg-table{width:100%;border-collapse:collapse;font-size:12px;margin-top:10px}
.sync-alg-table th{color:#64748b;font-size:11px;padding:4px 8px;text-align:left;border-bottom:1px solid #1e293b}
.sync-alg-table td{padding:5px 8px;font-family:monospace;border-bottom:1px solid #0f172a}

/* Timing proof */
.timing-row{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:8px;background:#0f172a;margin-bottom:6px}
.timing-port{font-family:monospace;font-weight:700;color:#60a5fa;width:44px}
.timing-delta{font-family:monospace;font-weight:800;font-size:15px;width:80px}
.timing-bar{flex:1;background:#1e293b;border-radius:99px;height:10px;overflow:hidden}
.timing-fill{height:10px;border-radius:99px}
.timing-info{font-size:11px;color:#64748b;width:120px;text-align:right}
.verdict-box{margin-top:12px;padding:12px 16px;border-radius:10px;font-weight:700;font-size:14px;text-align:center}

/* Canvas chart */
#rtt-chart{width:100%;height:120px;display:block}

/* Load test */
.lt-controls{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:14px}
.lt-controls label{font-size:12px;color:#94a3b8}
.lt-controls input[type=number]{width:72px;background:#0f172a;border:1px solid #334155;border-radius:6px;padding:5px 8px;color:#e2e8f0;font-size:13px}
.lt-controls select{background:#0f172a;border:1px solid #334155;border-radius:6px;padding:5px 8px;color:#e2e8f0;font-size:13px;cursor:pointer}
.lt-counters{display:flex;gap:16px;font-size:12px;margin-bottom:10px;flex-wrap:wrap}
.lt-counters span{display:flex;align-items:center;gap:4px}
.lt-dot-grid{display:flex;flex-wrap:wrap;gap:4px;max-height:120px;overflow-y:auto;padding:4px;background:#0f172a;border-radius:8px;margin-bottom:10px}
.lt-dot{width:14px;height:14px;border-radius:3px;transition:background .25s;cursor:default;flex-shrink:0}
.lt-verdict{margin-top:10px;padding:10px 14px;border-radius:8px;font-weight:700;font-size:13px;text-align:center;display:none}
#lt-rtt-canvas{width:100%;height:80px;display:block;margin-top:8px}

/* Footer */
.footer{background:#1e293b;border-top:1px solid #334155;padding:14px 24px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;position:sticky;bottom:0}
</style>
</head>
<body>

<!-- NAVBAR -->
<nav class="navbar">
  <div style="display:flex;align-items:center;gap:14px">
    <div style="width:36px;height:36px;background:#2563eb;border-radius:8px;display:grid;place-items:center;font-size:18px">🎓</div>
    <div>
      <div style="font-weight:800;font-size:16px">Exam Network Simulation</div>
      <div style="font-size:11px;color:#64748b">Professor Control Dashboard — Computer Networks Final Project</div>
    </div>
  </div>
  <div id="nav-badges" style="display:flex;gap:8px;align-items:center">
    <span id="badge-containers" class="badge badge-warn"><span class="dot"></span> Loading…</span>
    <span id="badge-dns" class="badge badge-warn"><span class="dot"></span> DNS</span>
    <span id="badge-rudp" class="badge badge-warn"><span class="dot"></span> RUDP</span>
    <span id="badge-clients" class="badge badge-blue"><span class="dot"></span> <span id="client-count">0</span> clients</span>
  </div>
</nav>

<div class="grid">

  <!-- ══ LEFT PANEL ══ -->
  <div style="display:flex;flex-direction:column;gap:12px">

    <!-- Network Interference -->
    <div class="card">
      <div class="card-title">⚡ Network Interference Simulator</div>
      <div style="font-size:11px;color:#475569;margin-bottom:12px">
        Software delay/jitter/loss applied by <code style="color:#60a5fa">ws_bridge.py</code> via <code style="color:#60a5fa">/tmp/netem_delay.json</code>
      </div>

      <!-- Client 1 -->
      <div class="client-card" id="cc-8081">
        <div class="client-title">
          <span class="client-name">Client 1</span>
          <span class="port-badge">:8081</span>
        </div>
        <div class="slider-group">
          <div class="slider-row">
            <span class="slider-label">Delay</span>
            <input type="range" min="0" max="500" value="0" id="delay-8081" oninput="updLbl(8081)">
            <span class="slider-val" id="lbl-delay-8081">0 ms</span>
          </div>
          <div class="slider-row">
            <span class="slider-label">Jitter</span>
            <input type="range" min="0" max="100" value="0" id="jitter-8081" oninput="updLbl(8081)">
            <span class="slider-val" id="lbl-jitter-8081">0 ms</span>
          </div>
          <div class="slider-row">
            <span class="slider-label">Loss</span>
            <input type="range" min="0" max="50" step="0.5" value="0" id="loss-8081" oninput="updLbl(8081)">
            <span class="slider-val" id="lbl-loss-8081">0%</span>
          </div>
        </div>
        <div class="btn-row">
          <button class="btn btn-blue" onclick="applyNetem(8081)">Apply</button>
          <button class="btn btn-ghost" onclick="resetNetem(8081)">Reset</button>
        </div>
        <div id="netem-status-8081" style="font-size:11px;color:#64748b;margin-top:6px"></div>
      </div>

      <!-- Client 2 -->
      <div class="client-card" id="cc-8082">
        <div class="client-title">
          <span class="client-name">Client 2</span>
          <span class="port-badge">:8082</span>
        </div>
        <div class="slider-group">
          <div class="slider-row">
            <span class="slider-label">Delay</span>
            <input type="range" min="0" max="500" value="0" id="delay-8082" oninput="updLbl(8082)">
            <span class="slider-val" id="lbl-delay-8082">0 ms</span>
          </div>
          <div class="slider-row">
            <span class="slider-label">Jitter</span>
            <input type="range" min="0" max="100" value="0" id="jitter-8082" oninput="updLbl(8082)">
            <span class="slider-val" id="lbl-jitter-8082">0 ms</span>
          </div>
          <div class="slider-row">
            <span class="slider-label">Loss</span>
            <input type="range" min="0" max="50" step="0.5" value="0" id="loss-8082" oninput="updLbl(8082)">
            <span class="slider-val" id="lbl-loss-8082">0%</span>
          </div>
        </div>
        <div class="btn-row">
          <button class="btn btn-blue" onclick="applyNetem(8082)">Apply</button>
          <button class="btn btn-ghost" onclick="resetNetem(8082)">Reset</button>
        </div>
        <div id="netem-status-8082" style="font-size:11px;color:#64748b;margin-top:6px"></div>
      </div>

      <!-- Client 3 -->
      <div class="client-card" id="cc-8083">
        <div class="client-title">
          <span class="client-name">Client 3</span>
          <span class="port-badge">:8083</span>
        </div>
        <div class="slider-group">
          <div class="slider-row">
            <span class="slider-label">Delay</span>
            <input type="range" min="0" max="500" value="0" id="delay-8083" oninput="updLbl(8083)">
            <span class="slider-val" id="lbl-delay-8083">0 ms</span>
          </div>
          <div class="slider-row">
            <span class="slider-label">Jitter</span>
            <input type="range" min="0" max="100" value="0" id="jitter-8083" oninput="updLbl(8083)">
            <span class="slider-val" id="lbl-jitter-8083">0 ms</span>
          </div>
          <div class="slider-row">
            <span class="slider-label">Loss</span>
            <input type="range" min="0" max="50" step="0.5" value="0" id="loss-8083" oninput="updLbl(8083)">
            <span class="slider-val" id="lbl-loss-8083">0%</span>
          </div>
        </div>
        <div class="btn-row">
          <button class="btn btn-blue" onclick="applyNetem(8083)">Apply</button>
          <button class="btn btn-ghost" onclick="resetNetem(8083)">Reset</button>
        </div>
        <div id="netem-status-8083" style="font-size:11px;color:#64748b;margin-top:6px"></div>
      </div>

      <!-- Presets -->
      <div style="font-size:11px;color:#64748b;margin-top:4px;margin-bottom:6px">Scenario presets (applies to ALL clients):</div>
      <div class="preset-row">
        <button class="btn btn-ghost" style="font-size:11px;padding:5px 10px" onclick="setScenario('easy')">🟢 Easy<br><span style='font-size:10px;color:#64748b'>0ms 0% loss</span></button>
        <button class="btn btn-ghost" style="font-size:11px;padding:5px 10px" onclick="setScenario('medium')">🟡 Medium<br><span style='font-size:10px;color:#64748b'>50ms 1% loss</span></button>
        <button class="btn btn-ghost" style="font-size:11px;padding:5px 10px" onclick="setScenario('hard')">🟠 Hard<br><span style='font-size:10px;color:#64748b'>150ms 5% loss</span></button>
        <button class="btn btn-ghost" style="font-size:11px;padding:5px 10px" onclick="setScenario('chaos')">🔴 Chaos<br><span style='font-size:10px;color:#64748b'>300ms 20% loss</span></button>
        <button class="btn btn-red" style="font-size:11px;padding:5px 10px" onclick="resetAll()">↺ Reset All</button>
      </div>

      <!-- Different latencies preset -->
      <button class="btn btn-ghost" style="font-size:11px;padding:5px 10px;margin-top:6px;width:100%" onclick="setScenario('unequal')">
        🎯 Unequal Latency — showcase sync algorithm<br>
        <span style='font-size:10px;color:#64748b'>Client1=10ms · Client2=80ms · Client3=200ms</span>
      </button>
    </div>

    <!-- Start Time Control -->
    <div class="card">
      <div class="card-title">⏱ Exam Schedule</div>
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
        <span style="font-size:12px;color:#94a3b8">Start in:</span>
        <input type="number" id="start-offset" value="20" min="5" max="300" step="5"
               style="width:70px;background:#0f172a;border:1px solid #334155;border-radius:6px;padding:4px 8px;color:#e2e8f0;font-size:13px">
        <span style="font-size:12px;color:#94a3b8">seconds</span>
        <button class="btn btn-blue" style="padding:5px 12px;font-size:12px" onclick="setStartTime()">Set T₀</button>
      </div>
      <div id="start-time-display" style="font-size:11px;color:#64748b">—</div>
    </div>

  </div>

  <!-- ══ RIGHT PANEL ══ -->
  <div style="display:flex;flex-direction:column;gap:12px">

    <!-- Container Status Grid -->
    <div class="card">
      <div class="card-title">🐳 Container Status <span id="svc-count" style="color:#60a5fa"></span></div>
      <div class="svc-grid" id="svc-grid">
        <div class="svc-item"><div class="svc-dot" style="background:#334155"></div><div class="svc-name">Loading…</div></div>
      </div>
    </div>

    <!-- Two-column: DNS + Connected Clients -->
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">

      <!-- DNS Trace -->
      <div class="card">
        <div class="card-title">🌐 DNS Hierarchy Trace</div>
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
          <span style="font-size:11px;color:#64748b">מצב רזולוציה:</span>
          <button id="dns-mode-iter" class="btn btn-ghost" style="font-size:11px;padding:3px 10px"
            onclick="setDnsMode('iterative')">🔁 Iterative</button>
          <button id="dns-mode-recur" class="btn btn-ghost" style="font-size:11px;padding:3px 10px"
            onclick="setDnsMode('recursive')">↩ Recursive</button>
          <span id="dns-mode-badge" style="font-size:10px;color:#94a3b8;margin-left:4px">…</span>
        </div>
        <div id="dns-trace-panel">
          <div style="color:#475569;font-size:12px">Click "Trace DNS" to run</div>
        </div>
        <button class="btn btn-ghost" style="margin-top:10px;font-size:12px;width:100%" onclick="loadDns()">🔍 Trace DNS</button>
      </div>

      <!-- Connected Clients -->
      <div class="card">
        <div class="card-title">👥 Connected Clients (live)</div>
        <div id="clients-table">
          <div style="color:#475569;font-size:12px">Waiting for connections…</div>
        </div>
      </div>

    </div>

    <!-- Simulation -->
    <div class="card">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
        <div class="card-title" style="margin-bottom:0">▶ Concurrent Client Simulation</div>
        <div style="display:flex;align-items:center;gap:8px">
          <select id="sim-mode" style="background:#0f172a;border:1px solid #334155;border-radius:6px;padding:4px 8px;color:#e2e8f0;font-size:12px">
            <option value="rudp-sync">RUDP + Sync</option>
            <option value="tcp-sync">TCP + Sync</option>
            <option value="tcp-nosync">TCP + No Sync</option>
          </select>
          <button class="btn btn-green" id="sim-btn" onclick="runSimulation()">Run Simulation</button>
        </div>
      </div>

      <!-- Progress bars -->
      <div class="sim-progress" id="sim-progress">
        <div class="sim-client" id="sim-8081"><span class="sim-port">:8081</span><div class="sim-bar-wrap"><div class="sim-bar" id="bar-8081" style="width:0%"></div></div><span class="sim-status" id="st-8081">idle</span><span class="sim-rtt" id="rtt-8081">—</span></div>
        <div class="sim-client" id="sim-8082"><span class="sim-port">:8082</span><div class="sim-bar-wrap"><div class="sim-bar" id="bar-8082" style="width:0%"></div></div><span class="sim-status" id="st-8082">idle</span><span class="sim-rtt" id="rtt-8082">—</span></div>
        <div class="sim-client" id="sim-8083"><span class="sim-port">:8083</span><div class="sim-bar-wrap"><div class="sim-bar" id="bar-8083" style="width:0%"></div></div><span class="sim-status" id="st-8083">idle</span><span class="sim-rtt" id="rtt-8083">—</span></div>
      </div>

      <!-- RTT mini chart -->
      <div style="margin-top:12px">
        <div style="font-size:11px;color:#64748b;margin-bottom:6px">RTT samples per client (most recent 12):</div>
        <canvas id="rtt-chart"></canvas>
      </div>
    </div>

    <!-- Clock Sync Algorithm Comparison -->
    <div class="card" id="sync-cmp-card">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
        <div class="card-title" style="margin-bottom:0">🕐 Clock Sync Algorithm Comparison</div>
        <div style="display:flex;gap:6px;align-items:center">
          <select id="sc-port" style="background:#0f172a;border:1px solid #334155;border-radius:6px;padding:4px 8px;color:#e2e8f0;font-size:12px">
            <option value="8081">:8081 (RUDP+Sync)</option>
            <option value="8082">:8082 (TCP+Sync)</option>
            <option value="8083">:8083 (TCP+NoSync)</option>
          </select>
          <button class="btn btn-ghost" style="font-size:12px;padding:5px 12px" id="sc-btn" onclick="runSyncCompare()">▶ Compare</button>
        </div>
      </div>
      <div style="font-size:11px;color:#475569;margin-bottom:10px;line-height:1.5">
        12-round RTT sampling sent to the exam server. Each algorithm uses the same raw samples
        but different selection/aggregation strategies.  Our adaptive algorithm (amber) uses the
        3 lowest-RTT samples and picks the median — the same logic the exam server uses for D_i.
        <b style="color:#94a3b8"> True offset = 0 ms</b> (all containers share the host clock).
      </div>

      <!-- Convergence canvas -->
      <canvas class="sync-cmp-canvas" id="sc-canvas"></canvas>

      <!-- Legend -->
      <div class="sync-legend">
        <span><span style="width:18px;height:3px;background:#64748b;display:inline-block;border-radius:2px;border-top:2px dashed #64748b"></span>Naive (mean of all)</span>
        <span><span style="width:18px;height:3px;background:#3b82f6;display:inline-block;border-radius:2px"></span>Cristian's algorithm (reference)</span>
        <span><span style="width:18px;height:3px;background:#22c55e;display:inline-block;border-radius:2px"></span>NTP clock filter (8-sample window)</span>
        <span><span style="width:18px;height:3px;background:#f59e0b;display:inline-block;border-radius:2px"></span>Ours — adaptive 3-best (an>
        <span><span style="width:10px;height:10px;border-radius:2px;background:rgba(34,197,94,.15);display:inline-block"></span>NTP dispersion band</span>
        <span><span style="width:10px;height:10px;border-radius:2px;background:rgba(245,158,11,.15);display:inline-block"></span>Ours dispersion band</span>
      </div>

      <!-- Per-round log -->
      <div id="sc-log" style="font-size:11px;font-family:monospace;color:#475569;max-height:80px;overflow-y:auto;background:#0f172a;border-radius:6px;padding:8px;margin-bottom:8px">
        Click Compare to run…
      </div>

      <!-- Algorithm results table -->
      <table class="sync-alg-table" id="sc-table">
        <thead><tr>
          <th>Algorithm</th><th>Final offset</th><th>Dispersion ±</th><th>Best RTT</th><th>Description</th>
        </tr></thead>
        <tbody id="sc-tbody">
          <tr><td colspan="5" style="color:#475569;font-style:italic;padding:10px 8px">Run comparison to see results</td></tr>
        </tbody>
      </table>
    </div>

    <!-- Timing Proof -->
    <div class="card" id="timing-card" style="display:none">
      <div class="card-title">⚡ Synchronized Delivery — Timing Proof</div>
      <div style="font-size:11px;color:#475569;margin-bottom:8px;line-height:1.5">
        Each port is an <b style="color:#94a3b8">independent server</b> with its own stagger coordinator.
        Spread is measured among <b style="color:#4ade80">sync-enabled</b> modes only; the <b style="color:#f87171">NoSync</b> baseline
        has no delivery alignment and shows raw scheduling jitter.
      </div>
      <div id="timing-rows"></div>
      <div id="timing-formula" style="font-size:11px;color:#475569;margin-top:10px;line-height:1.8;font-family:monospace"></div>
      <div id="timing-verdict" class="verdict-box"></div>
    </div>

    <!-- Transport Comparison -->
    <div class="card">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px">
        <div class="card-title" style="margin-bottom:0">📊 Transport Mode Comparison</div>
        <div style="display:flex;align-items:center;gap:10px">
          <label style="display:flex;align-items:center;gap:5px;font-size:11px;color:#64748b;cursor:pointer">
            <input type="checkbox" id="cmp-auto" onchange="toggleCmpAuto(this.checked)" style="accent-color:#3b82f6">
            Auto-refresh
          </label>
          <button class="btn btn-ghost" style="font-size:12px;padding:4px 10px" onclick="loadComparison()">↻ Refresh</button>
        </div>
      </div>

      <!-- 3 hero metric cards -->
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:16px">
        <!-- RUDP + Sync -->
        <div style="background:#0c1830;border:1px solid #1d4ed8;border-radius:10px;padding:14px">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px">
            <div>
              <div style="font-size:10px;color:#60a5fa;text-transform:uppercase;letter-spacing:.1em;font-weight:600">RUDP</div>
              <div style="font-size:13px;font-weight:700;color:#e2e8f0;margin-top:1px">+ Sync</div>
            </div>
            <div style="background:#1e3a5f;border-radius:6px;padding:3px 7px;font-size:10px;color:#93c5fd">SW ARQ · 14B</div>
          </div>
          <div id="cmp-spread-rudp" style="font-size:32px;font-weight:800;color:#e2e8f0;line-height:1;margin-bottom:2px">—</div>
          <div style="font-size:10px;color:#64748b;margin-bottom:10px">spread ms</div>
          <div id="cmp-verdict-rudp" style="font-size:11px;font-weight:600;padding:3px 8px;border-radius:5px;display:inline-block;background:#14532d;color:#4ade80">—</div>
          <div style="display:flex;justify-content:space-between;margin-top:10px;padding-top:10px;border-top:1px solid #1e293b">
            <div style="text-align:center">
              <div id="cmp-n-rudp" style="font-size:16px;font-weight:700;color:#60a5fa">—</div>
              <div style="font-size:9px;color:#475569;text-transform:uppercase">clients</div>
            </div>
            <div style="text-align:center">
              <div id="cmp-rtt-rudp" style="font-size:16px;font-weight:700;color:#60a5fa">—</div>
              <div style="font-size:9px;color:#475569;text-transform:uppercase">avg RTT ms</div>
            </div>
            <div style="text-align:center">
              <div id="cmp-sub-rudp" style="font-size:16px;font-weight:700;color:#60a5fa">—</div>
              <div style="font-size:9px;color:#475569;text-transform:uppercase">submitted</div>
            </div>
          </div>
        </div>

        <!-- TCP + Sync -->
        <div style="background:#100c2a;border:1px solid #4338ca;border-radius:10px;padding:14px">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px">
            <div>
              <div style="font-size:10px;color:#818cf8;text-transform:uppercase;letter-spacing:.1em;font-weight:600">TCP</div>
              <div style="font-size:13px;font-weight:700;color:#e2e8f0;margin-top:1px">+ Sync</div>
            </div>
            <div style="background:#1e1b4b;border-radius:6px;padding:3px 7px;font-size:10px;color:#a5b4fc">Kernel · NL-JSON</div>
          </div>
          <div id="cmp-spread-tcp" style="font-size:32px;font-weight:800;color:#e2e8f0;line-height:1;margin-bottom:2px">—</div>
          <div style="font-size:10px;color:#64748b;margin-bottom:10px">spread ms</div>
          <div id="cmp-verdict-tcp" style="font-size:11px;font-weight:600;padding:3px 8px;border-radius:5px;display:inline-block;background:#14532d;color:#4ade80">—</div>
          <div style="display:flex;justify-content:space-between;margin-top:10px;padding-top:10px;border-top:1px solid #1e293b">
            <div style="text-align:center">
              <div id="cmp-n-tcp" style="font-size:16px;font-weight:700;color:#818cf8">—</div>
              <div style="font-size:9px;color:#475569;text-transform:uppercase">clients</div>
            </div>
            <div style="text-align:center">
              <div id="cmp-rtt-tcp" style="font-size:16px;font-weight:700;color:#818cf8">—</div>
              <div style="font-size:9px;color:#475569;text-transform:uppercase">avg RTT ms</div>
            </div>
            <div style="text-align:center">
              <div id="cmp-sub-tcp" style="font-size:16px;font-weight:700;color:#818cf8">—</div>
              <div style="font-size:9px;color:#475569;text-transform:uppercase">submitted</div>
            </div>
          </div>
        </div>

        <!-- TCP + NoSync -->
        <div style="background:#1a0e05;border:1px solid #9a3412;border-radius:10px;padding:14px">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px">
            <div>
              <div style="font-size:10px;color:#fb923c;text-transform:uppercase;letter-spacing:.1em;font-weight:600">TCP</div>
              <div style="font-size:13px;font-weight:700;color:#e2e8f0;margin-top:1px">No Sync</div>
            </div>
            <div style="background:#2c1406;border-radius:6px;padding:3px 7px;font-size:10px;color:#fdba74">Kernel · NL-JSON</div>
          </div>
          <div id="cmp-spread-nosync" style="font-size:32px;font-weight:800;color:#e2e8f0;line-height:1;margin-bottom:2px">—</div>
          <div style="font-size:10px;color:#64748b;margin-bottom:10px">spread ms</div>
          <div id="cmp-verdict-nosync" style="font-size:11px;font-weight:600;padding:3px 8px;border-radius:5px;display:inline-block;background:#7f1d1d;color:#fca5a5">—</div>
          <div style="display:flex;justify-content:space-between;margin-top:10px;padding-top:10px;border-top:1px solid #1e293b">
            <div style="text-align:center">
              <div id="cmp-n-nosync" style="font-size:16px;font-weight:700;color:#fb923c">—</div>
              <div style="font-size:9px;color:#475569;text-transform:uppercase">clients</div>
            </div>
            <div style="text-align:center">
              <div id="cmp-rtt-nosync" style="font-size:16px;font-weight:700;color:#fb923c">—</div>
              <div style="font-size:9px;color:#475569;text-transform:uppercase">avg RTT ms</div>
            </div>
            <div style="text-align:center">
              <div id="cmp-sub-nosync" style="font-size:16px;font-weight:700;color:#fb923c">—</div>
              <div style="font-size:9px;color:#475569;text-transform:uppercase">submitted</div>
            </div>
          </div>
        </div>
      </div>

      <!-- Grouped bar chart -->
      <div style="margin-bottom:4px;font-size:10px;color:#475569;text-transform:uppercase;letter-spacing:.06em">
        Timing spread per mode — lower is better &nbsp;
        <span style="color:#22c55e">▬</span> &lt;30 ms target
        <span style="color:#f59e0b;margin-left:8px">▬</span> 100 ms warning
      </div>
      <canvas id="cmp-bar-canvas" style="width:100%;height:130px;display:block;border-radius:6px"></canvas>

      <!-- Sync indicator comparison row -->
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-top:12px;padding-top:12px;border-top:1px solid #1e293b">
        <div style="text-align:center">
          <div style="font-size:10px;color:#475569;margin-bottom:4px">Reliability layer</div>
          <div style="font-size:12px;font-weight:600;color:#60a5fa">SW ARQ (custom)</div>
          <div style="font-size:10px;color:#475569;margin-top:2px">14-byte header · CRC-16</div>
        </div>
        <div style="text-align:center">
          <div style="font-size:10px;color:#475569;margin-bottom:4px">Reliability layer</div>
          <div style="font-size:12px;font-weight:600;color:#818cf8">Kernel TCP</div>
          <div style="font-size:10px;color:#475569;margin-top:2px">NL-JSON framing · 20B+ hdr</div>
        </div>
        <div style="text-align:center">
          <div style="font-size:10px;color:#475569;margin-bottom:4px">Sync mechanism</div>
          <div style="font-size:12px;font-weight:600;color:#f87171">None — baseline</div>
          <div style="font-size:10px;color:#475569;margin-top:2px">immediate send · raw jitter</div>
        </div>
      </div>

      <!-- Mode launch buttons -->
      <div style="margin-top:14px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <span style="font-size:11px;color:#475569">Open client UI:</span>
        <button style="background:#1e3a5f;color:#93c5fd;border:1px solid #1d4ed8;border-radius:5px;padding:4px 10px;font-size:11px;cursor:pointer" onclick="window.open('http://localhost:8081?mode=rudp-sync','_blank')">RUDP + Sync ↗</button>
        <button style="background:#1e1b4b;color:#a5b4fc;border:1px solid #4338ca;border-radius:5px;padding:4px 10px;font-size:11px;cursor:pointer" onclick="window.open('http://localhost:8081?mode=tcp-sync','_blank')">TCP + Sync ↗</button>
        <button style="background:#2c1406;color:#fdba74;border:1px solid #9a3412;border-radius:5px;padding:4px 10px;font-size:11px;cursor:pointer" onclick="window.open('http://localhost:8081?mode=tcp-nosync','_blank')">TCP + NoSync ↗</button>
      </div>

      <div id="cmp-note" style="font-size:11px;color:#475569;margin-top:10px"></div>
    </div>

    <!-- Load Test -->
    <div class="card">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
        <div class="card-title" style="margin-bottom:0">🔥 Concurrent Load Test</div>
        <div style="display:flex;gap:6px">
          <button class="btn btn-red" id="lt-stop-btn" onclick="stopLoadTest()" style="display:none;font-size:12px;padding:5px 10px">■ Stop</button>
          <button class="btn btn-green" id="lt-run-btn" onclick="runLoadTest()">▶ Run Load Test</button>
        </div>
      </div>

      <div class="lt-controls">
        <label>Clients:</label>
        <input type="number" id="lt-count" value="10" min="1" max="50">
        <label>Mode:</label>
        <select id="lt-mode">
          <option value="rudp-sync">RUDP + Sync</option>
          <option value="tcp-sync">TCP + Sync</option>
          <option value="tcp-nosync">TCP + NoSync</option>
        </select>
        <label>Start in:</label>
        <input type="number" id="lt-offset" value="20" min="5" max="120">
        <label style="margin-left:-6px">s</label>
      </div>

      <!-- Live counters -->
      <div class="lt-counters">
        <span><span style="background:#334155;width:10px;height:10px;border-radius:2px;display:inline-block"></span> <span id="lt-c-idle">0</span> idle</span>
        <span><span style="background:#3b82f6;width:10px;height:10px;border-radius:2px;display:inline-block"></span> <span id="lt-c-sync">0</span> syncing</span>
        <span><span style="background:#f59e0b;width:10px;height:10px;border-radius:2px;display:inline-block"></span> <span id="lt-c-wait">0</span> waiting T₀</span>
        <span><span style="background:#22c55e;width:10px;height:10px;border-radius:2px;display:inline-block"></span> <span id="lt-c-done">0</span> done</span>
        <span><span style="background:#ef4444;width:10px;height:10px;border-radius:2px;display:inline-block"></span> <span id="lt-c-err">0</span> error</span>
      </div>

      <!-- Dot grid: one square per client -->
      <div class="lt-dot-grid" id="lt-dot-grid"></div>

      <!-- RTT histogram canvas -->
      <div style="font-size:11px;color:#64748b;margin-bottom:4px">RTT distribution across all clients:</div>
      <canvas id="lt-rtt-canvas"></canvas>

      <!-- Verdict -->
      <div class="lt-verdict" id="lt-verdict"></div>
    </div>

  </div>
</div>

<!-- FOOTER -->
<footer class="footer">
  <span style="font-size:12px;color:#64748b;margin-right:6px">Open exam client:</span>
  <button class="btn btn-blue" onclick="window.open('http://localhost:8081','_blank')">Client 1 :8081</button>
  <button class="btn btn-blue" onclick="window.open('http://localhost:8082','_blank')">Client 2 :8082</button>
  <button class="btn btn-blue" onclick="window.open('http://localhost:8083','_blank')">Client 3 :8083</button>
  <button class="btn btn-ghost" onclick="window.open('http://localhost:9999','_blank')">⚙ Admin Panel</button>
  <button class="btn btn-ghost" onclick="window.open('http://localhost:9999/api/clients','_blank')">📋 Raw clients.json</button>
  <span style="margin-left:auto;font-size:11px;color:#334155">prof_dashboard.py · port 7777</span>
</footer>

<script>
// ── Live stream (SSE) ─────────────────────────────────────────────────────────
function startStream() {
  const es = new EventSource('/stream');
  es.onmessage = ev => {
    const d = JSON.parse(ev.data);
    updateStatus(d);
  };
  es.onerror = () => setTimeout(startStream, 3000);
}

const SVC_LABELS = {
  'dns-root':'dns-root','dns-tld':'dns-tld','dns-auth':'dns-auth',
  'dns-resolver':'resolver','dhcp-server':'dhcp','rudp-server-1':'rudp-1',
  'rudp-server-2':'rudp-2','rudp-server-3':'rudp-3','admin':'admin',
  'tcp-server-sync':'tcp-sync','tcp-server-nosync':'tcp-nosync'
};
const CLIENT_LABELS = {'8081':'client-1','8082':'client-2','8083':'client-3'};

function updateStatus(d) {
  // Container grid
  const svcs = d.containers || {};
  const grid = document.getElementById('svc-grid');
  let html = '';
  let allOk = true, dnsOk = true, rudpOk = true;
  for (const [name, status] of Object.entries(svcs)) {
    const ok = status === 'running';
    if (!ok) allOk = false;
    if (name.includes('dns') && !ok) dnsOk = false;
    if (name.includes('rudp') && !ok) rudpOk = false;
    const color = ok ? '#22c55e' : '#ef4444';
    const label = SVC_LABELS[name] || name.replace('finalproject-computernetworks-','').slice(0,16);
    html += `<div class="svc-item"><div class="svc-dot" style="background:${color}"></div><div class="svc-name" title="${name}">${label}</div></div>`;
  }
  grid.innerHTML = html;
  const total = Object.keys(svcs).length;
  const running = Object.values(svcs).filter(s => s === 'running').length;
  document.getElementById('svc-count').textContent = `${running}/${total}`;

  // Nav badges
  document.getElementById('badge-containers').className = `badge ${allOk ? 'badge-ok' : 'badge-warn'}`;
  document.getElementById('badge-containers').innerHTML = `<span class="dot"></span> ${running}/${total} up`;
  document.getElementById('badge-dns').className = `badge ${dnsOk ? 'badge-ok' : 'badge-err'}`;
  document.getElementById('badge-dns').innerHTML = `<span class="dot"></span> DNS ${dnsOk ? '✓' : '✗'}`;
  document.getElementById('badge-rudp').className = `badge ${rudpOk ? 'badge-ok' : 'badge-err'}`;
  document.getElementById('badge-rudp').innerHTML = `<span class="dot"></span> RUDP ${rudpOk ? '✓' : '✗'}`;

  // Client count
  const clients = d.clients || [];
  document.getElementById('client-count').textContent = clients.length;

  // Clients table
  if (clients.length) {
    let t = '<table><tr><th>Client ID</th><th>RTT</th><th>Delay</th><th>Answers</th><th>Status</th></tr>';
    for (const c of clients.slice(-10)) {
      const sub = c.submitted ? '<span style="color:#4ade80">✓ submitted</span>' : '<span style="color:#94a3b8">pending</span>';
      const rtt = c.rtt_ms != null ? c.rtt_ms + ' ms' : '—';
      t += `<tr><td style="color:#60a5fa">${(c.client_id||'').slice(0,20)}</td><td>${rtt}</td><td>${c.deliver_delay_ms||'—'}</td><td>${c.answers_count||0}</td><td>${sub}</td></tr>`;
    }
    t += '</table>';
    document.getElementById('clients-table').innerHTML = t;
  }

  // Start time
  if (d.start_at_ms) {
    const t = new Date(d.start_at_ms);
    const remaining = (d.start_at_ms - Date.now()) / 1000;
    const remStr = remaining > 0 ? `(in ${remaining.toFixed(1)}s)` : '(started)';
    document.getElementById('start-time-display').innerHTML =
      `T₀ = <span style="color:#60a5fa;font-family:monospace">${t.toLocaleTimeString()}.${String(d.start_at_ms%1000).padStart(3,'0')}</span> ${remStr}`;
  }

  // Netem state from server
  if (d.netem_state) {
    for (const [port, state] of Object.entries(d.netem_state)) {
      const p = parseInt(port);
      if (!state) continue;
      document.getElementById('delay-'+p).value = state.delay||0;
      document.getElementById('jitter-'+p).value = state.jitter||0;
      document.getElementById('loss-'+p).value = state.loss||0;
      updLbl(p);
      const active = (state.delay||0)>0 || (state.jitter||0)>0 || (state.loss||0)>0;
      document.getElementById('cc-'+p).style.borderColor = active ? '#3b82f6' : '#1e3a5f';
    }
  }
}

// ── Slider labels ─────────────────────────────────────────────────────────────
function updLbl(port) {
  document.getElementById('lbl-delay-'+port).textContent = document.getElementById('delay-'+port).value + ' ms';
  document.getElementById('lbl-jitter-'+port).textContent = document.getElementById('jitter-'+port).value + ' ms';
  document.getElementById('lbl-loss-'+port).textContent = parseFloat(document.getElementById('loss-'+port).value).toFixed(1) + '%';
}

// ── Netem controls ────────────────────────────────────────────────────────────
async function applyNetem(port) {
  const delay = parseInt(document.getElementById('delay-'+port).value);
  const jitter = parseInt(document.getElementById('jitter-'+port).value);
  const loss = parseFloat(document.getElementById('loss-'+port).value);
  const el = document.getElementById('netem-status-'+port);
  el.textContent = 'Applying…';
  const r = await fetch('/api/netem/'+port, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({delay, jitter, loss})
  });
  const d = await r.json();
  el.textContent = d.ok ? `✓ Applied: delay=${delay}ms jitter=${jitter}ms loss=${loss}%` : '✗ '+d.error;
  el.style.color = d.ok ? '#4ade80' : '#ef4444';
  document.getElementById('cc-'+port).style.borderColor = (delay||jitter||loss) ? '#3b82f6' : '#1e3a5f';
}

async function resetNetem(port) {
  document.getElementById('delay-'+port).value = 0;
  document.getElementById('jitter-'+port).value = 0;
  document.getElementById('loss-'+port).value = 0;
  updLbl(port);
  const el = document.getElementById('netem-status-'+port);
  el.textContent = 'Resetting…';
  const r = await fetch('/api/netem/'+port, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({delay:0, jitter:0, loss:0})
  });
  const d = await r.json();
  el.textContent = d.ok ? '✓ Reset (no interference)' : '✗ '+d.error;
  el.style.color = d.ok ? '#64748b' : '#ef4444';
  document.getElementById('cc-'+port).style.borderColor = '#1e3a5f';
}

async function resetAll() {
  for (const p of [8081,8082,8083]) await resetNetem(p);
}

const SCENARIOS = {
  easy:    {8081:{delay:0,jitter:0,loss:0},    8082:{delay:0,jitter:0,loss:0},    8083:{delay:0,jitter:0,loss:0}},
  medium:  {8081:{delay:50,jitter:10,loss:1},  8082:{delay:50,jitter:10,loss:1},  8083:{delay:50,jitter:10,loss:1}},
  hard:    {8081:{delay:150,jitter:30,loss:5}, 8082:{delay:150,jitter:30,loss:5}, 8083:{delay:150,jitter:30,loss:5}},
  chaos:   {8081:{delay:300,jitter:50,loss:20},8082:{delay:300,jitter:50,loss:20},8083:{delay:300,jitter:50,loss:20}},
  unequal: {8081:{delay:10,jitter:2,loss:0},   8082:{delay:80,jitter:10,loss:1},  8083:{delay:200,jitter:20,loss:2}},
};
async function setScenario(name) {
  const s = SCENARIOS[name]; if (!s) return;
  for (const [port, v] of Object.entries(s)) {
    const p = parseInt(port);
    document.getElementById('delay-'+p).value = v.delay;
    document.getElementById('jitter-'+p).value = v.jitter;
    document.getElementById('loss-'+p).value = v.loss;
    updLbl(p);
    await fetch('/api/netem/'+p, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(v)
    });
    const active = v.delay>0 || v.jitter>0 || v.loss>0;
    document.getElementById('cc-'+p).style.borderColor = active ? '#3b82f6' : '#1e3a5f';
    document.getElementById('netem-status-'+p).textContent = `✓ ${name}: ${v.delay}ms delay ${v.loss}% loss`;
    document.getElementById('netem-status-'+p).style.color = active ? '#60a5fa' : '#64748b';
  }
}

// ── Start time ────────────────────────────────────────────────────────────────
async function setStartTime() {
  const offset = parseInt(document.getElementById('start-offset').value);
  await fetch('/api/set_start', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({offset_sec:offset})});
}

// ── DNS Mode toggle ───────────────────────────────────────────────────────────
async function loadDnsMode() {
  try {
    const r = await fetch('/api/dns/mode');
    const d = await r.json();
    _applyDnsModeBadge(d.mode || 'iterative');
  } catch(_) {}
}

function _applyDnsModeBadge(mode) {
  const badge = document.getElementById('dns-mode-badge');
  const iterBtn  = document.getElementById('dns-mode-iter');
  const recurBtn = document.getElementById('dns-mode-recur');
  if (!badge) return;
  const isIter = mode === 'iterative';
  badge.textContent = isIter ? '● iterative (פעיל)' : '● recursive (פעיל)';
  badge.style.color = isIter ? '#4ade80' : '#c084fc';
  iterBtn.style.borderColor  = isIter  ? '#4ade80' : '';
  recurBtn.style.borderColor = !isIter ? '#c084fc' : '';
}

async function setDnsMode(mode) {
  const badge = document.getElementById('dns-mode-badge');
  if (badge) { badge.textContent = '…'; badge.style.color = '#64748b'; }
  try {
    const r = await fetch(`/api/dns/mode?mode=${mode}`, {method:'POST'});
    const d = await r.json();
    _applyDnsModeBadge(d.mode);
  } catch(e) {
    if (badge) { badge.textContent = '✗ שגיאה'; badge.style.color = '#f87171'; }
  }
}

// ── DNS Trace ─────────────────────────────────────────────────────────────────
async function loadDns() {
  document.getElementById('dns-trace-panel').innerHTML = '<div style="color:#64748b;font-size:12px">Resolving…</div>';
  const r = await fetch('/api/dns');
  const d = await r.json();
  const steps = d.hops || d.steps || [];
  const resolved = d.resolved || [];
  if (!steps.length) {
    document.getElementById('dns-trace-panel').innerHTML = `<div style="color:#ef4444;font-size:12px">${d.error||'No trace available'}</div>`;
    return;
  }
  let html = '';
  for (let i=0; i<steps.length; i++) {
    const s = steps[i];
    const isLast = i === steps.length-1;
    const role = s.role || s.server_ip || '?';
    const server = s.server_ip || s.server || '?';
    const rtt = s.rtt_ms != null ? s.rtt_ms+'ms' : '?';
    const answer = isLast
      ? (s.answers||[]).join(', ') || (resolved.join(', ')) || '—'
      : (s.referral || s.ns_ips || 'referral…');
    const color = isLast ? '#4ade80' : '#60a5fa';
    html += `<div class="dns-step">
      <div class="dns-num">${i+1}</div>
      <div class="dns-role">${role.replace('10.99.0.','10.99.…')}</div>
      <div class="dns-server">${server}</div>
      <div class="dns-rtt">${rtt}</div>
      <div class="dns-arrow">${isLast?'✓':'→'}</div>
      <div class="dns-result" style="color:${color}">${typeof answer==='object'?JSON.stringify(answer).slice(0,60):String(answer).slice(0,60)}</div>
    </div>`;
  }
  if (resolved.length) {
    html += `<div style="margin-top:8px;font-size:12px;color:#4ade80">✓ server.exam.lan → ${resolved.join(', ')}</div>`;
  }
  document.getElementById('dns-trace-panel').innerHTML = html;
}

// ── Simulation ────────────────────────────────────────────────────────────────
const rttData = {8081:[], 8082:[], 8083:[]};

function initChart() {
  // Simple canvas bar chart — no CDN needed
  const canvas = document.getElementById('rtt-chart');
  if (!canvas) return;
  canvas._draw = function() {
    const ctx = canvas.getContext('2d');
    const W = canvas.offsetWidth; const H = 120;
    canvas.width = W; canvas.height = H;
    ctx.fillStyle = '#0f172a';
    ctx.fillRect(0,0,W,H);
    const colors = {8081:'#3b82f6', 8082:'#22c55e', 8083:'#f59e0b'};
    const allVals = Object.values(rttData).flat();
    const maxV = Math.max(...allVals, 10);
    const ports = [8081,8082,8083];
    const n = Math.max(...ports.map(p => rttData[p].length), 1);
    const barW = Math.max(4, (W / (n * ports.length + n)) | 0);
    const gap = barW;
    let x = 4;
    for (let i=0; i<n; i++) {
      for (const p of ports) {
        const v = rttData[p][i];
        if (v == null) { x += barW + 1; continue; }
        const h = Math.max(3, (v / maxV) * (H-18));
        ctx.fillStyle = colors[p];
        ctx.fillRect(x, H-h-14, barW, h);
        x += barW + 1;
      }
      x += gap;
    }
    // Legend
    let lx = 4;
    for (const p of ports) {
      ctx.fillStyle = colors[p];
      ctx.fillRect(lx, H-10, 10, 8);
      ctx.fillStyle = '#94a3b8';
      ctx.font = '9px monospace';
      ctx.fillText(':'+p, lx+13, H-3);
      lx += 52;
    }
    ctx.fillStyle = '#334155';
    ctx.fillText('RTT (ms) — max='+maxV.toFixed(1), W-90, 12);
  };
  window.addEventListener('resize', canvas._draw);
}

function redrawChart() {
  const c = document.getElementById('rtt-chart');
  if (c && c._draw) c._draw();
}

let _simRunning = false;
async function runSimulation() {
  if (_simRunning) return;
  _simRunning = true;
  const btn = document.getElementById('sim-btn');
  btn.disabled = true; btn.textContent = '⏳ Running…';

  // Reset UI
  for (const p of [8081,8082,8083]) {
    document.getElementById('bar-'+p).style.width = '0%';
    document.getElementById('bar-'+p).style.background = '#3b82f6';
    document.getElementById('st-'+p).textContent = 'starting…';
    document.getElementById('rtt-'+p).textContent = '—';
    rttData[p] = [];
  }
  document.getElementById('timing-card').style.display = 'none';
  redrawChart();

  const offset = parseInt(document.getElementById('start-offset').value) || 20;
  const simMode = document.getElementById('sim-mode').value;
  const es = new EventSource('/api/simulate?offset='+offset+'&mode='+simMode);
  es.onmessage = ev => {
    const d = JSON.parse(ev.data);
    handleSimEvent(d);
  };
  es.onerror = () => {
    es.close();
    btn.disabled = false; btn.textContent = 'Run Simulation';
    _simRunning = false;
  };
}

function handleSimEvent(d) {
  const btn = document.getElementById('sim-btn');
  switch (d.type) {
    case 'ntp_progress': {
      const pct = (d.samples / d.total) * 40;
      document.getElementById('bar-'+d.port).style.width = pct+'%';
      document.getElementById('st-'+d.port).textContent = `NTP ${d.samples}/${d.total}`;
      if (d.best_rtt) document.getElementById('rtt-'+d.port).textContent = d.best_rtt.toFixed(1)+'ms';
      if (d.sample_rtt) { rttData[d.port].push(d.sample_rtt); redrawChart(); }
      break;
    }
    case 'schedule': {
      document.getElementById('bar-'+d.port).style.width = '55%';
      document.getElementById('st-'+d.port).textContent = `waiting T₀ (delay=${d.deliver_delay_ms}ms)`;
      break;
    }
    case 'exam_opening': {
      document.getElementById('bar-'+d.port).style.width = '70%';
      document.getElementById('bar-'+d.port).style.background = '#f59e0b';
      document.getElementById('st-'+d.port).textContent = 'exam opening…';
      break;
    }
    case 'exam_received': {
      document.getElementById('bar-'+d.port).style.width = '85%';
      document.getElementById('bar-'+d.port).style.background = '#22c55e';
      document.getElementById('st-'+d.port).textContent = `exam ✓ (Δ=${d.delta_ms.toFixed(1)}ms)`;
      break;
    }
    case 'submitted': {
      document.getElementById('bar-'+d.port).style.width = '100%';
      document.getElementById('st-'+d.port).textContent = '✓ submitted';
      break;
    }
    case 'error': {
      document.getElementById('bar-'+d.port).style.background = '#ef4444';
      document.getElementById('st-'+d.port).textContent = '✗ ' + (d.message||'error').slice(0,30);
      break;
    }
    case 'complete': {
      showTimingProof(d.results);
      btn.disabled = false; btn.textContent = 'Run Again';
      _simRunning = false;
      // SSE will close naturally
      break;
    }
  }
}

function showTimingProof(results) {
  const card = document.getElementById('timing-card');
  card.style.display = 'block';

  const MODE_LABELS = {8081:'RUDP+Sync', 8082:'TCP+Sync', 8083:'TCP+NoSync'};
  const SYNC_PORTS  = [8081, 8082];

  const valid = results.filter(r => r.exam_resp_delta_ms != null && !r.error);
  if (!valid.length) return;

  const maxDelta = Math.max(...valid.map(r => r.exam_resp_delta_ms), 1);

  // Spread among sync-enabled modes only
  const syncResults   = valid.filter(r => SYNC_PORTS.includes(r.port));
  const nosyncResults = valid.filter(r => !SYNC_PORTS.includes(r.port));
  const syncSpread = syncResults.length >= 2
    ? Math.max(...syncResults.map(r => r.exam_resp_delta_ms)) - Math.min(...syncResults.map(r => r.exam_resp_delta_ms))
    : 0;
  const totalSpread = maxDelta;

  let rowHtml = '';
  for (const r of results) {
    const mode  = MODE_LABELS[r.port] || `:${r.port}`;
    const isSync = SYNC_PORTS.includes(r.port);
    if (r.error) {
      rowHtml += `<div class="timing-row"><span class="timing-port">:${r.port}</span><span style="color:#94a3b8;font-size:11px;margin-left:4px">${mode}</span><span style="color:#ef4444;margin-left:8px">ERROR: ${r.error.slice(0,40)}</span></div>`;
      continue;
    }
    const delta = r.exam_resp_delta_ms || 0;
    const pct = Math.max(2, (delta / Math.max(maxDelta, 1)) * 100);
    const color = delta < 10 ? '#22c55e' : delta < 50 ? '#f59e0b' : '#ef4444';
    const fillColor = delta < 10 ? '#22c55e' : delta < 50 ? '#f59e0b' : '#ef4444';
    const syncBadge = isSync
      ? '<span style="font-size:9px;background:#14532d;color:#4ade80;padding:1px 5px;border-radius:3px;margin-left:6px">SYNC</span>'
      : '<span style="font-size:9px;background:#7f1d1d;color:#f87171;padding:1px 5px;border-radius:3px;margin-left:6px">NO-SYNC</span>';
    rowHtml += `<div class="timing-row">
      <span class="timing-port">:${r.port}</span>
      <span style="color:#94a3b8;font-size:11px;min-width:75px">${mode}${syncBadge}</span>
      <span class="timing-delta" style="color:${color}">+${delta.toFixed(1)}ms</span>
      <div class="timing-bar"><div class="timing-fill" style="width:${pct}%;background:${fillColor}"></div></div>
      <span class="timing-info">RTT=${r.my_rtt_ms||'?'}ms delay=${r.deliver_delay_ms||0}ms</span>
    </div>`;
  }
  document.getElementById('timing-rows').innerHTML = rowHtml;

  // Formula
  let fHtml = 'deliver_delay = (max_rtt − my_rtt) / 2 + buffer(30ms)\n';
  for (const r of valid) {
    const mode = MODE_LABELS[r.port] || `:${r.port}`;
    const isSync = SYNC_PORTS.includes(r.port);
    fHtml += `  :${r.port} ${mode}  (${r.max_rtt_ms||0}ms − ${r.my_rtt_ms||0}ms)/2 + 30 = ${r.deliver_delay_ms||0}ms  →  bridge held ${r.deliver_delay_ms||0}ms`;
    if (!isSync) fHtml += '  (no sync — baseline)';
    fHtml += '\n';
  }
  if (nosyncResults.length > 0 && syncResults.length > 0) {
    const nosyncDelta = nosyncResults[0].exam_resp_delta_ms || 0;
    const syncBest = Math.min(...syncResults.map(r => r.exam_resp_delta_ms || 0));
    const gap = Math.abs(nosyncDelta - syncBest);
    fHtml += `\nNote: each server coordinates independently. Cross-server spread (${totalSpread.toFixed(1)}ms) reflects independent scheduling.\n`;
    fHtml += `Sync-enabled spread: ${syncSpread.toFixed(1)}ms (RUDP+Sync vs TCP+Sync).  NoSync baseline offset: ${gap.toFixed(1)}ms from nearest sync mode.`;
  }
  document.getElementById('timing-formula').textContent = fHtml;

  // Verdict — judge sync quality only among sync-enabled modes
  const vEl = document.getElementById('timing-verdict');
  if (syncResults.length >= 2) {
    if (syncSpread < 30) {
      vEl.style.background = '#14532d'; vEl.style.color = '#4ade80';
      vEl.innerHTML = `✓ SYNCHRONIZED — sync-enabled spread ${syncSpread.toFixed(1)} ms < 30 ms jitter buffer`
        + (nosyncResults.length ? `<br><span style="font-size:11px;opacity:.7">NoSync baseline adds ${(totalSpread - syncSpread).toFixed(1)} ms — expected without stagger algorithm</span>` : '');
    } else if (syncSpread < 100) {
      vEl.style.background = '#78350f'; vEl.style.color = '#fbbf24';
      vEl.innerHTML = `⚠ NEAR-SYNC — sync-enabled spread ${syncSpread.toFixed(1)} ms (within 100 ms)`
        + (nosyncResults.length ? `<br><span style="font-size:11px;opacity:.7">Total cross-server spread ${totalSpread.toFixed(1)} ms includes unsynchronized baseline</span>` : '');
    } else {
      vEl.style.background = '#7f1d1d'; vEl.style.color = '#f87171';
      vEl.textContent = `✗ OUT OF SYNC — sync-enabled spread ${syncSpread.toFixed(1)} ms`;
    }
  } else if (syncResults.length === 1) {
    vEl.style.background = '#1e293b'; vEl.style.color = '#94a3b8';
    vEl.textContent = `ℹ Only 1 sync-enabled mode responded — need ≥2 for spread comparison`;
  } else {
    vEl.style.background = '#1e293b'; vEl.style.color = '#94a3b8';
    vEl.textContent = `ℹ No sync-enabled results — cannot measure sync quality`;
  }
}

// ── Transport Comparison ───────────────────────────────────────────────────────

const CMP_COLORS = {
  'rudp-sync':  { bar: '#3b82f6', bg: '#1e3a5f', border: '#1d4ed8', text: '#60a5fa' },
  'tcp-sync':   { bar: '#6366f1', bg: '#1e1b4b', border: '#4338ca', text: '#818cf8' },
  'tcp-nosync': { bar: '#f97316', bg: '#2c1406', border: '#9a3412', text: '#fb923c' },
};
const CMP_LABELS = {
  'rudp-sync':  'RUDP+Sync',
  'tcp-sync':   'TCP+Sync',
  'tcp-nosync': 'TCP+NoSync',
};

let _cmpAutoTimer = null;
function toggleCmpAuto(on) {
  if (_cmpAutoTimer) { clearInterval(_cmpAutoTimer); _cmpAutoTimer = null; }
  if (on) {
    loadComparison();
    _cmpAutoTimer = setInterval(loadComparison, 5000);
  }
}

async function loadComparison() {
  try {
    const r = await fetch('/api/compare');
    const d = await r.json();
    _renderCmpCards(d);
    _renderCmpBarChart(d);
    const noteEl = document.getElementById('cmp-note');
    if (noteEl) noteEl.textContent = d.note || '';
  } catch(e) {
    const noteEl = document.getElementById('cmp-note');
    if (noteEl) noteEl.textContent = 'Error loading: ' + e.message;
  }
}

function _spreadColor(spread) {
  if (spread === null) return '#475569';
  if (spread < 30)  return '#22c55e';
  if (spread < 100) return '#f59e0b';
  return '#ef4444';
}

function _spreadVerdict(spread, hasData) {
  if (!hasData) return ['#334155', '#94a3b8', '— no clients yet'];
  if (spread === null) return ['#334155', '#94a3b8', '— no exam data'];
  if (spread < 30)  return ['#14532d', '#4ade80', '✓ SYNCHRONIZED'];
  if (spread < 100) return ['#713f12', '#fbbf24', '⚠ MARGINAL'];
  return ['#7f1d1d', '#fca5a5', '✗ UNCOORDINATED'];
}

function _renderCmpCards(d) {
  const map = { 'rudp-sync': 'rudp', 'tcp-sync': 'tcp', 'tcp-nosync': 'nosync' };
  for (const m of (d.modes || [])) {
    const key = map[m.mode];
    if (!key) continue;
    const spreadEl  = document.getElementById('cmp-spread-' + key);
    const verdictEl = document.getElementById('cmp-verdict-' + key);
    const nEl       = document.getElementById('cmp-n-' + key);
    const rttEl     = document.getElementById('cmp-rtt-' + key);
    const subEl     = document.getElementById('cmp-sub-' + key);

    const spread = m.spread_ms;
    const clr = CMP_COLORS[m.mode] || { text: '#94a3b8' };

    if (spreadEl) {
      spreadEl.textContent = spread !== null ? spread.toFixed(1) : '—';
      spreadEl.style.color = _spreadColor(spread);
    }
    if (verdictEl) {
      const [bg, fg, label] = _spreadVerdict(spread, m.count > 0);
      verdictEl.style.background = bg;
      verdictEl.style.color = fg;
      verdictEl.textContent = label;
    }
    if (nEl)   { nEl.textContent   = m.count;                                            nEl.style.color   = clr.text; }
    if (rttEl) { rttEl.textContent = m.avg_rtt_ms !== null ? m.avg_rtt_ms.toFixed(1) : '—'; rttEl.style.color = clr.text; }
    if (subEl) { subEl.textContent = m.count > 0 ? `${m.submitted}/${m.count}` : '—';   subEl.style.color = clr.text; }
  }
}

function _renderCmpBarChart(d) {
  const canvas = document.getElementById('cmp-bar-canvas');
  if (!canvas) return;
  const W = canvas.offsetWidth || 500, H = 130;
  canvas.width  = W * (window.devicePixelRatio || 1);
  canvas.height = H * (window.devicePixelRatio || 1);
  canvas.style.width  = W + 'px';
  canvas.style.height = H + 'px';
  const ctx = canvas.getContext('2d');
  ctx.scale(window.devicePixelRatio || 1, window.devicePixelRatio || 1);

  ctx.fillStyle = '#0f172a'; ctx.fillRect(0, 0, W, H);

  const modes = ['rudp-sync', 'tcp-sync', 'tcp-nosync'];
  const spreads = modes.map(m => {
    const found = (d.modes || []).find(x => x.mode === m);
    return found ? found.spread_ms : null;
  });

  const maxSpread = Math.max(...spreads.filter(v => v !== null), 50, 1);
  const PAD = { l: 36, r: 10, t: 14, b: 28 };
  const CW = W - PAD.l - PAD.r;
  const CH = H - PAD.t - PAD.b;

  const barW = Math.floor(CW / modes.length * 0.55);
  const gap  = CW / modes.length;

  // Threshold lines
  const drawThreshold = (val, color, label) => {
    if (val > maxSpread * 1.05) return;
    const y = PAD.t + CH - (val / maxSpread) * CH;
    ctx.strokeStyle = color; ctx.lineWidth = 1; ctx.setLineDash([4, 3]);
    ctx.beginPath(); ctx.moveTo(PAD.l, y); ctx.lineTo(W - PAD.r, y); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = color; ctx.font = '9px monospace'; ctx.textAlign = 'left';
    ctx.fillText(label, PAD.l + 2, y - 2);
  };
  drawThreshold(30,  '#22c55e', '30 ms');
  drawThreshold(100, '#f59e0b', '100 ms');

  // Y axis
  ctx.strokeStyle = '#1e293b'; ctx.lineWidth = 1; ctx.setLineDash([]);
  ctx.beginPath(); ctx.moveTo(PAD.l, PAD.t); ctx.lineTo(PAD.l, PAD.t + CH); ctx.stroke();
  ctx.fillStyle = '#475569'; ctx.font = '9px monospace'; ctx.textAlign = 'right';
  ctx.fillText('0', PAD.l - 3, PAD.t + CH + 3);
  ctx.fillText(Math.round(maxSpread), PAD.l - 3, PAD.t + 8);

  modes.forEach((mode, i) => {
    const spread = spreads[i];
    const cx     = PAD.l + gap * i + gap / 2;
    const clr    = CMP_COLORS[mode] || { bar: '#334155', text: '#94a3b8' };

    // Bar background (empty track)
    ctx.fillStyle = '#1e293b';
    const bx = cx - barW / 2;
    ctx.beginPath();
    ctx.roundRect(bx, PAD.t, barW, CH, 4);
    ctx.fill();

    if (spread !== null) {
      const barH = Math.max(4, (spread / maxSpread) * CH);
      const barY = PAD.t + CH - barH;

      // Bar fill with gradient
      const grad = ctx.createLinearGradient(0, barY, 0, PAD.t + CH);
      grad.addColorStop(0, clr.bar);
      grad.addColorStop(1, clr.bg || clr.bar + '66');
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.roundRect(bx, barY, barW, barH, 4);
      ctx.fill();

      // Value label above bar
      ctx.fillStyle = _spreadColor(spread);
      ctx.font = 'bold 11px monospace';
      ctx.textAlign = 'center';
      ctx.fillText(spread.toFixed(1), cx, barY - 4);
    } else {
      ctx.fillStyle = '#334155'; ctx.font = '10px monospace'; ctx.textAlign = 'center';
      ctx.fillText('—', cx, PAD.t + CH / 2 + 4);
    }

    // X label
    ctx.fillStyle = clr.text || '#94a3b8';
    ctx.font = '10px monospace'; ctx.textAlign = 'center';
    ctx.fillText(CMP_LABELS[mode], cx, PAD.t + CH + 16);
  });
}

// ── Sync Algorithm Comparison ─────────────────────────────────────────────────

const SC_COLORS = {
  naive:      '#64748b',
  cristian:   '#3b82f6',
  ntp_filter: '#22c55e',
  ours:       '#f59e0b',
};
// Per-round data buffers (indexed 0-11)
let _scRounds = [];    // [{round, rtt_ms, naive_est, cristian_est, ntp_filter_est, ntp_filter_disp}]
let _scRunning = false;

function _scDraw() {
  const canvas = document.getElementById('sc-canvas');
  if (!canvas) return;
  const W = canvas.offsetWidth || 500, H = 160;
  canvas.width = W; canvas.height = H;
  const ctx = canvas.getContext('2d');
  const PAD = {l:40, r:10, t:14, b:22};
  const CW = W - PAD.l - PAD.r, CH = H - PAD.t - PAD.b;

  ctx.fillStyle = '#0f172a'; ctx.fillRect(0, 0, W, H);

  const n = _scRounds.length;
  if (n === 0) {
    ctx.fillStyle = '#334155'; ctx.font = '11px monospace';
    ctx.fillText('Waiting for data…', PAD.l + 10, H / 2);
    return;
  }

  // Y range: ±max dispersion or ±max|offset| across all algorithms, min ±0.5ms
  const allVals = _scRounds.flatMap(r => [
    r.naive_est, r.cristian_est, r.ntp_filter_est, r.ours_est,
    r.ntp_filter_est + r.ntp_filter_disp, r.ntp_filter_est - r.ntp_filter_disp,
    r.ours_est + (r.ours_disp || 0), r.ours_est - (r.ours_disp || 0),
  ]).filter(v => v != null);
  const yAbsMax = Math.max(0.5, ...allVals.map(Math.abs));
  const yMin = -yAbsMax * 1.2, yMax = yAbsMax * 1.2;
  const yRange = yMax - yMin;

  const xOf = i => PAD.l + (i / 11) * CW;          // round 0-11 → pixel
  const yOf = v => PAD.t + ((yMax - v) / yRange) * CH;

  // Grid lines
  ctx.strokeStyle = '#1e293b'; ctx.lineWidth = 1;
  for (let v of [-yAbsMax, 0, yAbsMax]) {
    const y = yOf(v);
    ctx.beginPath(); ctx.moveTo(PAD.l, y); ctx.lineTo(W - PAD.r, y); ctx.stroke();
  }
  // Zero line thicker
  ctx.strokeStyle = '#334155'; ctx.lineWidth = 1.5;
  ctx.beginPath(); ctx.moveTo(PAD.l, yOf(0)); ctx.lineTo(W - PAD.r, yOf(0)); ctx.stroke();

  // NTP dispersion band (shaded)
  if (n > 1) {
    ctx.fillStyle = 'rgba(34,197,94,0.08)';
    ctx.beginPath();
    _scRounds.forEach((r, i) => {
      const x = xOf(i), y = yOf(r.ntp_filter_est + r.ntp_filter_disp);
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    for (let i = n - 1; i >= 0; i--) {
      const r = _scRounds[i];
      ctx.lineTo(xOf(i), yOf(r.ntp_filter_est - r.ntp_filter_disp));
    }
    ctx.closePath(); ctx.fill();
  }

  // Draw the 4 convergence lines
  const drawLine = (key, color, dashed) => {
    ctx.strokeStyle = color; ctx.lineWidth = 2;
    ctx.setLineDash(dashed ? [5, 3] : []);
    ctx.beginPath();
    _scRounds.forEach((r, i) => {
      const x = xOf(i), y = yOf(r[key]);
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.setLineDash([]);
  };
  // Ours dispersion band (shaded amber)
  if (n > 1) {
    ctx.fillStyle = 'rgba(245,158,11,0.10)';
    ctx.beginPath();
    _scRounds.forEach((r, i) => {
      const x = xOf(i), y = yOf(r.ours_est + (r.ours_disp || 0));
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    for (let i = n - 1; i >= 0; i--) {
      const r = _scRounds[i];
      ctx.lineTo(xOf(i), yOf(r.ours_est - (r.ours_disp || 0)));
    }
    ctx.closePath(); ctx.fill();
  }

  drawLine('naive_est',      SC_COLORS.naive,      true);
  drawLine('cristian_est',   SC_COLORS.cristian,   false);
  drawLine('ntp_filter_est', SC_COLORS.ntp_filter, false);
  drawLine('ours_est',       SC_COLORS.ours,       false);

  // Y axis labels
  ctx.fillStyle = '#475569'; ctx.font = '9px monospace';
  ctx.textAlign = 'right';
  ctx.fillText(yMax.toFixed(1), PAD.l - 3, PAD.t + 8);
  ctx.fillText('0', PAD.l - 3, yOf(0) + 4);
  ctx.fillText(yMin.toFixed(1), PAD.l - 3, H - PAD.b + 2);
  ctx.textAlign = 'left';
  ctx.fillText('offset (ms)', PAD.l, PAD.t - 2);

  // X axis labels
  ctx.fillStyle = '#334155'; ctx.textAlign = 'center';
  [0, 3, 6, 9, 11].forEach(i => {
    ctx.fillText(`r${i+1}`, xOf(i), H - PAD.b + 12);
  });
}

function _scUpdateTable(data) {
  const tbody = document.getElementById('sc-tbody');
  const rows = [
    {
      name: 'Naive (mean)',
      color: SC_COLORS.naive,
      offset: data.naive.offset,
      disp: data.naive.disp,
      rtt: '—',
      desc: 'Average of all 12 offset samples — biased by high-RTT rounds',
    },
    {
      name: "Cristian's algorithm",
      color: SC_COLORS.cristian,
      offset: data.cristian.offset,
      disp: data.cristian.disp,
      rtt: data.cristian.rtt,
      desc: 'Reference: offset from the single lowest-RTT round — our adaptive algo builds on this but refines over multiple phases',
    },
    {
      name: 'NTP clock filter',
      color: SC_COLORS.ntp_filter,
      offset: data.ntp_filter.offset,
      disp: data.ntp_filter.disp,
      rtt: data.ntp_filter.rtt,
      desc: '8-sample sliding window; selects minimum-dispersion sample + tracks jitter',
    },
    {
      name: 'Ours (adaptive)',
      color: SC_COLORS.ours,
      offset: data.ours.offset,
      disp: data.ours.disp,
      rtt: data.ours.rtt,
      desc: 'Top-3 lowest-RTT samples; median-RTT offset; dispersion = J_rtt/2',
    },
  ];
  tbody.innerHTML = rows.map(r => {
    const offColor = Math.abs(r.offset) < 1 ? '#4ade80' : Math.abs(r.offset) < 5 ? '#fbbf24' : '#f87171';
    return `<tr>
      <td style="color:${r.color};font-weight:700">${r.name}</td>
      <td style="color:${offColor}">${r.offset > 0 ? '+' : ''}${r.offset.toFixed(2)} ms</td>
      <td>±${r.disp.toFixed(2)} ms</td>
      <td>${r.rtt === '—' ? '—' : r.rtt.toFixed(1) + ' ms'}</td>
      <td style="font-family:sans-serif;color:#64748b;font-size:11px">${r.desc}</td>
    </tr>`;
  }).join('');
}

let _scEs = null;
function runSyncCompare() {
  if (_scRunning) return;
  _scRunning = true;
  _scRounds = [];
  const port = parseInt(document.getElementById('sc-port').value);
  const btn  = document.getElementById('sc-btn');
  btn.disabled = true; btn.textContent = '⏳ Running…';
  document.getElementById('sc-log').textContent = 'Connecting…';
  document.getElementById('sc-tbody').innerHTML =
    '<tr><td colspan="5" style="color:#475569;font-style:italic;padding:10px 8px">Running…</td></tr>';
  _scDraw();

  _scEs = new EventSource('/api/sync_compare?port=' + port);
  _scEs.onmessage = ev => {
    const d = JSON.parse(ev.data);
    if (d.type === 'sync_round') {
      _scRounds.push(d);
      const logEl = document.getElementById('sc-log');
      logEl.textContent +=
        `\nr${String(d.round).padStart(2)} RTT=${d.rtt_ms.toFixed(1).padStart(6)}ms  `+
        `offset=${d.offset_ms.toFixed(2).padStart(7)}ms  `+
        `naive=${d.naive_est.toFixed(2).padStart(7)}ms  `+
        `cristian=${d.cristian_est.toFixed(2).padStart(7)}ms  `+
        `ntp=${d.ntp_filter_est.toFixed(2).padStart(7)}ms ±${d.ntp_filter_disp.toFixed(2)}ms  `+
        `ours=${d.ours_est.toFixed(2).padStart(7)}ms ±${(d.ours_disp||0).toFixed(2)}ms`;
      logEl.scrollTop = logEl.scrollHeight;
      _scDraw();
    } else if (d.type === 'sync_complete') {
      _scEs.close(); _scEs = null;
      _scRunning = false;
      btn.disabled = false; btn.textContent = '▶ Compare';
      _scUpdateTable(d);
      _scDraw();
    } else if (d.type === 'sync_error') {
      _scEs.close(); _scEs = null;
      _scRunning = false;
      btn.disabled = false; btn.textContent = '▶ Compare';
      document.getElementById('sc-log').textContent += '\n✗ Error: ' + d.message;
    }
  };
  _scEs.onerror = () => {
    if (_scEs) { _scEs.close(); _scEs = null; }
    _scRunning = false;
    btn.disabled = false; btn.textContent = '▶ Compare';
  };
}

// ── Load Test ─────────────────────────────────────────────────────────────────

const LT_COLORS = {
  idle:      '#334155',
  syncing:   '#3b82f6',
  waiting:   '#f59e0b',
  received:  '#818cf8',
  done:      '#22c55e',
  error:     '#ef4444',
};

let _ltEs = null;
let _ltStates = [];   // per-client state string
let _ltRtts   = [];   // per-client RTT (ms)

function _ltCounters() {
  const counts = { idle:0, syncing:0, waiting:0, received:0, done:0, error:0 };
  for (const s of _ltStates) counts[s] = (counts[s] || 0) + 1;
  document.getElementById('lt-c-idle').textContent  = counts.idle;
  document.getElementById('lt-c-sync').textContent  = counts.syncing;
  document.getElementById('lt-c-wait').textContent  = counts.waiting + counts.received;
  document.getElementById('lt-c-done').textContent  = counts.done;
  document.getElementById('lt-c-err').textContent   = counts.error;
  // update dot colors
  const grid = document.getElementById('lt-dot-grid');
  const dots = grid.querySelectorAll('.lt-dot');
  _ltStates.forEach((s, i) => { if (dots[i]) dots[i].style.background = LT_COLORS[s] || '#334155'; });
}

function _ltRttDraw(rtts) {
  const canvas = document.getElementById('lt-rtt-canvas');
  if (!canvas) return;
  const W = canvas.offsetWidth || 400, H = 80;
  canvas.width = W; canvas.height = H;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#0f172a'; ctx.fillRect(0, 0, W, H);
  if (!rtts.length) return;

  // Histogram with 20 buckets
  const min = Math.min(...rtts), max = Math.max(...rtts, min + 1);
  const buckets = 20;
  const bw = (max - min) / buckets || 1;
  const bins = Array(buckets).fill(0);
  rtts.forEach(v => { const b = Math.min(buckets - 1, Math.floor((v - min) / bw)); bins[b]++; });
  const maxBin = Math.max(...bins, 1);
  const barW = W / buckets;
  bins.forEach((cnt, i) => {
    const h = (cnt / maxBin) * (H - 16);
    ctx.fillStyle = '#3b82f6';
    ctx.fillRect(i * barW + 1, H - h - 14, barW - 2, h);
  });
  ctx.fillStyle = '#475569'; ctx.font = '9px monospace';
  ctx.fillText(`${min.toFixed(0)} ms`, 2, H - 2);
  ctx.fillText(`${max.toFixed(0)} ms`, W - 38, H - 2);
  ctx.fillText(`n=${rtts.length}  avg=${(rtts.reduce((a,b)=>a+b,0)/rtts.length).toFixed(1)} ms`, W/2 - 40, H - 2);
}

function stopLoadTest() {
  if (_ltEs) { _ltEs.close(); _ltEs = null; }
  document.getElementById('lt-run-btn').disabled = false;
  document.getElementById('lt-run-btn').textContent = '▶ Run Load Test';
  document.getElementById('lt-stop-btn').style.display = 'none';
}

async function runLoadTest() {
  if (_ltEs) return;
  const count  = Math.max(1, Math.min(50, parseInt(document.getElementById('lt-count').value) || 10));
  const mode   = document.getElementById('lt-mode').value;
  const offset = parseInt(document.getElementById('lt-offset').value) || 20;

  // Build dot grid
  _ltStates = Array(count).fill('idle');
  _ltRtts   = [];
  const grid = document.getElementById('lt-dot-grid');
  grid.innerHTML = '';
  for (let i = 0; i < count; i++) {
    const d = document.createElement('div');
    d.className = 'lt-dot';
    d.title = `Client ${i}`;
    d.style.background = LT_COLORS.idle;
    grid.appendChild(d);
  }

  // Reset counters + verdict
  _ltCounters();
  const verdict = document.getElementById('lt-verdict');
  verdict.style.display = 'none';
  _ltRttDraw([]);

  // Disable run, show stop
  const runBtn = document.getElementById('lt-run-btn');
  runBtn.disabled = true; runBtn.textContent = '⏳ Running…';
  document.getElementById('lt-stop-btn').style.display = 'inline-flex';

  _ltEs = new EventSource(`/api/load_test?count=${count}&mode=${encodeURIComponent(mode)}&offset=${offset}`);
  _ltEs.onmessage = ev => handleLtEvent(JSON.parse(ev.data));
  _ltEs.onerror = () => { stopLoadTest(); };
}

function handleLtEvent(d) {
  switch (d.type) {
    case 'lt_ntp': {
      if (_ltStates[d.idx] === 'idle') _ltStates[d.idx] = 'syncing';
      break;
    }
    case 'lt_scheduled': {
      _ltStates[d.idx] = 'waiting';
      if (d.rtt_ms != null) _ltRtts[d.idx] = d.rtt_ms;
      _ltRttDraw(_ltRtts.filter(v => v != null));
      break;
    }
    case 'lt_exam_recv': {
      _ltStates[d.idx] = 'received';
      break;
    }
    case 'lt_done': {
      _ltStates[d.idx] = d.submitted ? 'done' : 'error';
      break;
    }
    case 'lt_error': {
      _ltStates[d.idx] = 'error';
      break;
    }
    case 'lt_complete': {
      stopLoadTest();
      _ltRttDraw(_ltRtts.filter(v => v != null));
      // Force all remaining "waiting"/"received" states to final
      _ltStates = _ltStates.map(s => s === 'waiting' || s === 'received' ? 'done' : s);
      _ltCounters();
      // Verdict
      const verdict = document.getElementById('lt-verdict');
      verdict.style.display = 'block';
      const spread = d.spread_ms;
      const rate   = d.count > 0 ? ((d.success / d.count) * 100).toFixed(0) : 0;
      const sync   = d.mode === 'tcp-nosync';
      if (sync) {
        verdict.style.background = '#431407'; verdict.style.color = '#fb923c';
        verdict.textContent = `⚠ NoSync baseline — spread ${spread.toFixed(1)} ms | ${rate}% submitted (${d.success}/${d.count})`;
      } else if (spread < 30) {
        verdict.style.background = '#14532d'; verdict.style.color = '#4ade80';
        verdict.textContent = `✓ SYNCHRONIZED — spread ${spread.toFixed(1)} ms | avg RTT ${d.avg_rtt_ms} ms | ${rate}% submitted (${d.success}/${d.count})`;
      } else if (spread < 100) {
        verdict.style.background = '#78350f'; verdict.style.color = '#fbbf24';
        verdict.textContent = `⚠ NEAR-SYNC — spread ${spread.toFixed(1)} ms | avg RTT ${d.avg_rtt_ms} ms | ${rate}% submitted`;
      } else {
        verdict.style.background = '#7f1d1d'; verdict.style.color = '#f87171';
        verdict.textContent = `✗ OUT OF SYNC — spread ${spread.toFixed(1)} ms | ${rate}% submitted`;
      }
      return;  // skip _ltCounters below
    }
  }
  _ltCounters();
}

// ── Init ──────────────────────────────────────────────────────────────────────
window.onload = () => {
  startStream();
  initChart();
  loadDnsMode();
  loadDns();
  loadComparison();
};
</script>
</body>
</html>
"""

# ─────────────────────────────────────────────────────────────────────────────
# Docker helpers
# ─────────────────────────────────────────────────────────────────────────────
def _sh(cmd: list, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=capture, text=True,
                          cwd=PROJECT_ROOT, timeout=10)

def _container_status(name: str) -> str:
    try:
        r = _sh(["docker", "inspect", "--format", "{{.State.Status}}", name])
        return r.stdout.strip() if r.returncode == 0 else "missing"
    except Exception:
        return "error"

def _get_client_containers() -> dict[int, str]:
    """Return {port: container_name} for running client containers."""
    try:
        r = _sh(["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}", "--filter", "name=client"])
        result: dict[int, str] = {}
        for line in r.stdout.strip().splitlines():
            if "\t" not in line:
                continue
            name, ports = line.split("\t", 1)
            for port in CLIENT_PORTS:
                if f":{port}->" in ports or f"0.0.0.0:{port}" in ports:
                    result[port] = name.strip()
        return result
    except Exception:
        return {}

def _api_get(path: str) -> Any:
    try:
        with urllib.request.urlopen(f"{ADMIN_URL}{path}", timeout=3) as r:
            return json.loads(r.read())
    except Exception:
        return {}

# ─────────────────────────────────────────────────────────────────────────────
# Software netem (writes /tmp/netem_delay.json inside container)
# ws_bridge.py reads this file and applies delay/jitter/loss in Python —
# no kernel sch_netem module required (works on Docker Desktop for Windows/Mac).
# ─────────────────────────────────────────────────────────────────────────────
def apply_netem(container: str, delay_ms: int, jitter_ms: int, loss_pct: float) -> str:
    payload = json.dumps({"delay_ms": delay_ms,
                          "jitter_ms": jitter_ms,
                          "loss_pct": loss_pct})
    py_cmd = (
        f"import pathlib, json; "
        f"pathlib.Path('/tmp/netem_delay.json').write_text({payload!r})"
    )
    r = _sh(["docker", "exec", container, "python3", "-c", py_cmd])
    if r.returncode != 0:
        return r.stderr.strip() or "failed to write netem file"
    return ""   # success

# ─────────────────────────────────────────────────────────────────────────────
# Live stats
# ─────────────────────────────────────────────────────────────────────────────
def _get_live_data() -> dict:
    # Core services
    containers = {}
    for svc in ["dns-root", "dns-tld", "dns-auth", "dns-resolver",
                "dhcp-server", "rudp-server-1", "rudp-server-2", "rudp-server-3",
                "tcp-server-sync", "tcp-server-nosync", "admin"]:
        containers[svc] = _container_status(svc)

    # Client containers
    client_map = _get_client_containers()
    for port, name in client_map.items():
        containers[name] = _container_status(name)

    # Admin data
    status  = _api_get("/api/status")
    clients_raw = _api_get("/api/clients")
    if isinstance(clients_raw, dict):
        # admin returns {client_id: {fields}} — flatten to list
        clients = [{"client_id": k, **v} for k, v in clients_raw.items()]
    elif isinstance(clients_raw, list):
        clients = clients_raw
    else:
        clients = []

    # start_at.txt
    start_at_ms: Optional[int] = None
    try:
        r = _sh(["docker", "exec", "rudp-server-1", "cat", "/app/shared/start_at.txt"])
        if r.returncode == 0 and r.stdout.strip():
            start_at_ms = int(r.stdout.strip())
    except Exception:
        pass

    return {
        "containers":      containers,
        "admin_status":    status,
        "clients":         clients,
        "start_at_ms":     start_at_ms,
        "netem_state":     {str(p): netem_state[p] for p in CLIENT_PORTS},
        "client_map":      {str(k): v for k, v in client_map.items()},
        "ts":              int(time.time() * 1000),
    }

# ─────────────────────────────────────────────────────────────────────────────
# WebSocket client simulation (streams events via asyncio.Queue)
# ─────────────────────────────────────────────────────────────────────────────
def _rid():
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=8))

@dataclass
class SimResult:
    port:             int
    client_id:        str   = ""
    my_rtt_ms:        float = 0.0
    max_rtt_ms:       float = 0.0
    deliver_delay_ms: int   = 0
    exam_req_at:      float = 0.0
    exam_resp_at:     float = 0.0
    exam_resp_delta_ms: Optional[float] = None
    submitted:        bool  = False
    error:            Optional[str] = None

async def _recv_type(ws, msg_type: str, req_id: str, timeout: float = 20.0) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError(f"Timeout waiting for {msg_type}")
        raw  = await asyncio.wait_for(ws.recv(), timeout=remaining)
        msg  = json.loads(raw)
        if msg.get("type") == msg_type and msg.get("req_id") == req_id:
            return msg

class SimpleBarrier:
    def __init__(self, n: int):
        self._n = n; self._count = 0; self._ev = asyncio.Event()
    async def wait(self, timeout: float = 60.0):
        self._count += 1
        if self._count >= self._n:
            self._ev.set()
        try:
            await asyncio.wait_for(asyncio.shield(self._ev.wait()), timeout=timeout)
        except asyncio.TimeoutError:
            self._ev.set()   # release everyone so nothing hangs
    def release(self):
        """Force-release all waiters (call when a client errors before reaching wait())."""
        self._ev.set()

async def _sim_client(port: int, result: SimResult,
                      sched_barrier: SimpleBarrier,
                      exam_barrier: SimpleBarrier,
                      q: asyncio.Queue,
                      mode: str = "rudp-sync"):
    client_id = f"prof-{port}-{_rid()}"
    result.client_id = client_id
    ws_path = MODE_WS_PATH.get(mode, "/ws")
    try:
        async with websockets.connect(f"ws://localhost:{port}{ws_path}",
                                      open_timeout=15, ping_interval=None) as ws:
            # connection_info
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                msg = json.loads(raw)
                if msg.get("type") != "connection_info":
                    pass  # might be something else, continue
            except asyncio.TimeoutError:
                pass

            # hello
            await ws.send(json.dumps({"type": "hello", "client_id": client_id}))

            # ── Phase 1: 3-round quick bootstrap (50 ms cadence) ─────────
            samples = []
            for i in range(3):
                req_id = _rid()
                t0 = time.time() * 1000
                await ws.send(json.dumps({"type": "time_req", "req_id": req_id, "client_t0_ms": t0}))
                resp = await _recv_type(ws, "time_resp", req_id, timeout=5)
                t1 = time.time() * 1000
                rtt = t1 - t0
                offset = resp["server_now_ms"] - (t0 + rtt / 2)
                samples.append({"rtt": rtt, "offset": offset})
                best = min(samples, key=lambda s: s["rtt"])
                await q.put({"type": "ntp_progress", "port": port,
                             "samples": i + 1, "total": 3,
                             "best_rtt": best["rtt"], "sample_rtt": rtt})
                if i < 2:
                    await asyncio.sleep(0.05)

            best = min(samples, key=lambda s: s["rtt"])
            result.my_rtt_ms = best["rtt"]

            def _best_offset():
                return min(samples, key=lambda s: s["rtt"])["offset"]
            def _local_t0(start_at_ms):
                return start_at_ms - _best_offset()

            # ── schedule_req with Phase 1 RTT data ───────────────────────
            sorted_rtts = sorted(s["rtt"] for s in samples)
            rtt_samples_p1 = [sorted_rtts[0], sorted_rtts[1], sorted_rtts[2]]
            req_id = _rid()
            await ws.send(json.dumps({"type": "schedule_req", "req_id": req_id,
                                      "rtt_samples": rtt_samples_p1,
                                      "offset_ms": _best_offset()}))
            sched = await _recv_type(ws, "schedule_resp", req_id, timeout=10)
            result.max_rtt_ms       = sched.get("max_rtt_ms", 0)
            result.deliver_delay_ms = sched.get("deliver_delay_ms", 0)
            await q.put({"type": "schedule", "port": port,
                         "deliver_delay_ms": result.deliver_delay_ms,
                         "max_rtt_ms": result.max_rtt_ms})

            await sched_barrier.wait()

            start_at = sched["start_at_server_ms"]

            # ── Phase 2: Adaptive refinement during wait ──────────────────
            while True:
                ms_to_t0 = _local_t0(start_at) - time.time() * 1000
                if ms_to_t0 < 8000:
                    break
                if len(samples) >= 5:
                    offs = [s["offset"] for s in samples[-5:]]
                    m = sum(offs) / len(offs)
                    sd = (sum((x - m) ** 2 for x in offs) / len(offs)) ** 0.5
                    if sd < 1.5:
                        break
                if len(samples) >= 20:
                    break
                await asyncio.sleep(0.2)
                try:
                    req_id = _rid()
                    t0 = time.time() * 1000
                    await ws.send(json.dumps({"type": "time_req", "req_id": req_id, "client_t0_ms": t0}))
                    resp = await _recv_type(ws, "time_resp", req_id, timeout=5)
                    t1 = time.time() * 1000
                    rtt = t1 - t0
                    samples.append({"rtt": rtt, "offset": resp["server_now_ms"] - (t0 + rtt / 2)})
                except Exception:
                    pass

            # ── Phase 3: Fresh 3-round sample at T₀ − 5 s ────────────────
            ms_to_refresh = _local_t0(start_at) - time.time() * 1000 - 5000
            if ms_to_refresh > 0.1:
                await asyncio.sleep(ms_to_refresh / 1000)
            fresh_samples = []
            for i in range(3):
                try:
                    req_id = _rid()
                    t0 = time.time() * 1000
                    await ws.send(json.dumps({"type": "time_req", "req_id": req_id, "client_t0_ms": t0}))
                    resp = await _recv_type(ws, "time_resp", req_id, timeout=5)
                    t1 = time.time() * 1000
                    rtt = t1 - t0
                    s_entry = {"rtt": rtt, "offset": resp["server_now_ms"] - (t0 + rtt / 2)}
                    fresh_samples.append(s_entry)
                    samples.append(s_entry)
                except Exception:
                    pass
                if i < 2:
                    await asyncio.sleep(0.1)

            # ── Phase 4: wait for T₀ → exam_req ──────────────────────────
            wait_sec = (_local_t0(start_at) - time.time() * 1000) / 1000
            if wait_sec > 0:
                await asyncio.sleep(min(wait_sec, 120))

            fresh_rtt_samples = None
            fresh_offset_ms = None
            if fresh_samples:
                sr = sorted(s["rtt"] for s in fresh_samples)
                fresh_rtt_samples = [sr[0], sr[len(sr) // 2], sr[-1]]
                fresh_offset_ms = min(fresh_samples, key=lambda s: s["rtt"])["offset"]

            await q.put({"type": "exam_opening", "port": port})
            req_id = _rid()
            result.exam_req_at = time.monotonic()
            exam_req_msg = {"type": "exam_req", "req_id": req_id, "exam_id": sched["exam_id"]}
            if fresh_rtt_samples:
                exam_req_msg["rtt_samples"] = fresh_rtt_samples
                exam_req_msg["offset_ms"]   = fresh_offset_ms
            await ws.send(json.dumps(exam_req_msg))
            exam_resp = await _recv_type(ws, "exam_resp", req_id, timeout=30)
            result.exam_resp_at = time.monotonic()

            await exam_barrier.wait()

            await q.put({"type": "exam_received", "port": port,
                         "delta_ms": 0,   # computed after barrier
                         "deliver_delay_ms": result.deliver_delay_ms})

            exam = exam_resp["exam"]

            # exam_begin
            req_id = _rid()
            await ws.send(json.dumps({"type": "exam_begin", "req_id": req_id,
                                      "exam_id": exam["exam_id"],
                                      "opened_at_ms": result.exam_resp_at * 1000}))
            try:
                await asyncio.wait_for(ws.recv(), timeout=4)
            except asyncio.TimeoutError:
                pass

            # answers
            answers = []
            for q_item in exam["questions"]:
                if q_item["type"] == "mcq" and q_item.get("options"):
                    answers.append({"qid": q_item["id"], "type": "mcq",
                                    "optionId": q_item["options"][0]["id"]})
                else:
                    answers.append({"qid": q_item["id"], "type": "text",
                                    "text": f"Prof demo answer from :{port}"})

            # save + submit
            req_id = _rid()
            await ws.send(json.dumps({"type": "answers_save", "req_id": req_id,
                                      "exam_id": exam["exam_id"], "answers": answers}))
            try:
                await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                pass

            req_id = _rid()
            await ws.send(json.dumps({"type": "submit_req", "req_id": req_id,
                                      "exam_id": exam["exam_id"], "answers": answers}))
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=6)
                result.submitted = json.loads(raw).get("ok", False)
            except asyncio.TimeoutError:
                pass

            if result.submitted:
                await q.put({"type": "submitted", "port": port})

    except Exception as e:
        result.error = str(e)
        # Release barriers so surviving clients aren't stuck waiting
        sched_barrier.release()
        exam_barrier.release()
        await q.put({"type": "error", "port": port, "message": str(e)})

# ─────────────────────────────────────────────────────────────────────────────
# Clock sync algorithm comparison
# Runs 12 RTT rounds against the exam server and computes four algorithms:
#   1. Naive        — mean of all offset samples
#   2. Cristian's   — offset from the single minimum-RTT round (reference algorithm)
#   3. NTP filter   — 8-sample sliding window, picks min-dispersion sample,
#                     also tracks jitter (RMS of consecutive offset deltas)
#   4. Ours         — adaptive 3-best: keep 3 lowest-RTT samples, use median
#                     sample's offset; dispersion = J_rtt/2 
# ─────────────────────────────────────────────────────────────────────────────

async def _run_sync_compare(port: int, q: asyncio.Queue) -> None:
    """Connect to ws://localhost:{port}/ws and run 12-round sync comparison."""
    import math
    ws_url    = f"ws://localhost:{port}/ws"
    client_id = f"sync-cmp-{_rid()}"
    samples: List[dict] = []   # [{rtt, offset}]

    try:
        async with websockets.connect(ws_url, open_timeout=10,
                                      ping_interval=None) as ws:
            # consume optional connection_info
            try:
                await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                pass

            await ws.send(json.dumps({"type": "hello", "client_id": client_id}))

            for i in range(NTP_ROUNDS):
                req_id = _rid()
                t0 = time.time() * 1000
                await ws.send(json.dumps({
                    "type":        "time_req",
                    "req_id":      req_id,
                    "client_t0_ms": t0,
                }))

                # wait for matching time_resp
                deadline = time.time() + 5.0
                resp = None
                while time.time() < deadline:
                    try:
                        raw = await asyncio.wait_for(
                            ws.recv(), timeout=max(0.1, deadline - time.time()))
                        msg = json.loads(raw)
                        if msg.get("type") == "time_resp" and msg.get("req_id") == req_id:
                            resp = msg
                            break
                    except asyncio.TimeoutError:
                        break
                if resp is None:
                    raise TimeoutError(f"round {i+1} timed out")

                t1     = time.time() * 1000
                rtt    = t1 - t0
                offset = resp["server_now_ms"] - (t0 + rtt / 2)
                samples.append({"rtt": rtt, "offset": offset})

                # ── Algorithm 1: Naive (running mean) ────────────────────────
                naive_est  = sum(s["offset"] for s in samples) / len(samples)
                naive_disp = (
                    math.sqrt(sum((s["offset"] - naive_est) ** 2 for s in samples)
                              / len(samples))
                    if len(samples) > 1 else 0.0
                )

                # ── Algorithm 2: Cristian's min-RTT ──────────────────────────
                best_cristian  = min(samples, key=lambda s: s["rtt"])
                cristian_est   = best_cristian["offset"]
                cristian_disp  = best_cristian["rtt"] / 2

                # ── Algorithm 3: NTP clock filter (8-sample window) ───────────
                window          = samples[-8:]
                ntp_best        = min(window, key=lambda s: s["rtt"])
                ntp_filter_est  = ntp_best["offset"]
                ntp_filter_disp = ntp_best["rtt"] / 2   # peer dispersion bound
                # NTP jitter: RMS of consecutive offset differences (RFC 5905 §11.3)
                if len(samples) >= 2:
                    diffs      = [abs(samples[j+1]["offset"] - samples[j]["offset"])
                                  for j in range(len(samples) - 1)]
                    ntp_jitter = math.sqrt(sum(d * d for d in diffs) / len(diffs))
                else:
                    ntp_jitter = 0.0

                #  Algorithm 4: Ours (adaptive 3-best, 
                # Keep the 3 lowest-RTT samples, use median sample's offset.
                # Dispersion = J_rtt / 2 where J_rtt = max(rtt) - min(rtt) of
                # the 3-best. With asymmetric correction when offset is known.
                top3 = sorted(samples, key=lambda s: s["rtt"])[:3]
                if len(top3) >= 3:
                    rtt_med  = top3[1]["rtt"]          # median of 3 best
                    j_rtt    = top3[2]["rtt"] - top3[0]["rtt"]
                    ours_est = top3[1]["offset"]        # offset of median-RTT sample
                    ours_disp = j_rtt / 2.0
                elif len(top3) == 2:
                    ours_est  = top3[0]["offset"]
                    ours_disp = abs(top3[1]["rtt"] - top3[0]["rtt"]) / 2.0
                else:
                    ours_est  = top3[0]["offset"]
                    ours_disp = top3[0]["rtt"] / 2.0

                await q.put({
                    "type":            "sync_round",
                    "port":            port,
                    "round":           i + 1,
                    "total":           NTP_ROUNDS,
                    "rtt_ms":          round(rtt, 2),
                    "offset_ms":       round(offset, 2),
                    "naive_est":       round(naive_est, 2),
                    "naive_disp":      round(naive_disp, 2),
                    "cristian_est":    round(cristian_est, 2),
                    "cristian_disp":   round(cristian_disp, 2),
                    "ntp_filter_est":  round(ntp_filter_est, 2),
                    "ntp_filter_disp": round(ntp_filter_disp, 2),
                    "ntp_jitter":      round(ntp_jitter, 2),
                    "ours_est":        round(ours_est, 2),
                    "ours_disp":       round(ours_disp, 2),
                })
                await asyncio.sleep(0.1)

            # ── Final summary ─────────────────────────────────────────────────
            naive_mean = sum(s["offset"] for s in samples) / len(samples)
            naive_std  = math.sqrt(
                sum((s["offset"] - naive_mean) ** 2 for s in samples) / len(samples)
            )
            best_c   = min(samples, key=lambda s: s["rtt"])
            win8     = samples[-8:]
            ntp_b    = min(win8, key=lambda s: s["rtt"])
            diffs    = [abs(samples[j+1]["offset"] - samples[j]["offset"])
                        for j in range(len(samples) - 1)]
            final_jitter = math.sqrt(sum(d*d for d in diffs) / len(diffs)) if diffs else 0.0

            # Ours (adaptive 3-best)
            top3_final = sorted(samples, key=lambda s: s["rtt"])[:3]
            ours_rtt_med   = top3_final[1]["rtt"]
            ours_j_rtt     = top3_final[2]["rtt"] - top3_final[0]["rtt"]
            ours_offset    = top3_final[1]["offset"]
            ours_disp_final = ours_j_rtt / 2.0

            await q.put({
                "type": "sync_complete",
                "port": port,
                "naive": {
                    "offset": round(naive_mean, 2),
                    "disp":   round(naive_std, 2),
                    "rtt":    None,
                },
                "cristian": {
                    "offset": round(best_c["offset"], 2),
                    "disp":   round(best_c["rtt"] / 2, 2),
                    "rtt":    round(best_c["rtt"], 2),
                },
                "ntp_filter": {
                    "offset": round(ntp_b["offset"], 2),
                    "disp":   round(ntp_b["rtt"] / 2, 2),
                    "rtt":    round(ntp_b["rtt"], 2),
                    "jitter": round(final_jitter, 2),
                },
                "ours": {
                    "offset": round(ours_offset, 2),
                    "disp":   round(ours_disp_final, 2),
                    "rtt":    round(ours_rtt_med, 2),
                },
            })

    except Exception as exc:
        await q.put({"type": "sync_error", "port": port, "message": str(exc)})


# ─────────────────────────────────────────────────────────────────────────────
# N-client load test
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class LoadResult:
    idx:              int
    ws_url:           str
    client_id:        str   = ""
    my_rtt_ms:        float = 0.0
    deliver_delay_ms: int   = 0
    exam_resp_at:     float = 0.0
    exam_resp_delta_ms: Optional[float] = None
    submitted:        bool  = False
    error:            Optional[str] = None


async def _load_client(idx: int, result: LoadResult,
                       sched_barrier: SimpleBarrier,
                       pre_exam_barrier: SimpleBarrier,
                       exam_barrier: SimpleBarrier,
                       q: asyncio.Queue) -> None:
    client_id = f"load-{idx:03d}-{_rid()}"
    result.client_id = client_id
    try:
        async with websockets.connect(result.ws_url, open_timeout=15,
                                      ping_interval=None) as ws:
            # consume optional connection_info frame
            try:
                await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                pass

            await ws.send(json.dumps({"type": "hello", "client_id": client_id}))

            # ── Phase 1: 3-round quick bootstrap (50 ms cadence) ─────────
            samples: List[dict] = []
            for i in range(3):
                req_id = _rid()
                t0 = time.time() * 1000
                await ws.send(json.dumps({"type": "time_req", "req_id": req_id,
                                          "client_t0_ms": t0}))
                resp = await _recv_type(ws, "time_resp", req_id, timeout=5)
                t1 = time.time() * 1000
                rtt = t1 - t0
                offset = resp["server_now_ms"] - (t0 + rtt / 2)
                samples.append({"rtt": rtt, "offset": offset})
                await q.put({"type": "lt_ntp", "idx": idx,
                             "sample": i + 1, "total": 3, "rtt": round(rtt, 1)})
                if i < 2:
                    await asyncio.sleep(0.05)

            def _lt_best_offset():
                return min(samples, key=lambda s: s["rtt"])["offset"]
            def _lt_local_t0(start_at_ms):
                return start_at_ms - _lt_best_offset()

            result.my_rtt_ms = min(samples, key=lambda s: s["rtt"])["rtt"]

            # ── schedule_req with Phase 1 RTT data ───────────────────────
            sr1 = sorted(s["rtt"] for s in samples)
            rtt_samples_p1 = [sr1[0], sr1[1], sr1[2]]
            req_id = _rid()
            await ws.send(json.dumps({"type": "schedule_req", "req_id": req_id,
                                      "rtt_samples": rtt_samples_p1,
                                      "offset_ms": _lt_best_offset()}))
            sched = await _recv_type(ws, "schedule_resp", req_id, timeout=10)
            result.deliver_delay_ms = sched.get("deliver_delay_ms", 0)
            await q.put({"type": "lt_scheduled", "idx": idx,
                         "rtt_ms": round(result.my_rtt_ms, 1),
                         "delay_ms": result.deliver_delay_ms})

            await sched_barrier.wait()

            start_at = sched["start_at_server_ms"]

            # ── Phase 2: Adaptive refinement during wait ──────────────────
            while True:
                ms_to_t0 = _lt_local_t0(start_at) - time.time() * 1000
                if ms_to_t0 < 8000:
                    break
                if len(samples) >= 5:
                    offs = [s["offset"] for s in samples[-5:]]
                    m = sum(offs) / len(offs)
                    sd = (sum((x - m) ** 2 for x in offs) / len(offs)) ** 0.5
                    if sd < 1.5:
                        break
                if len(samples) >= 20:
                    break
                await asyncio.sleep(0.2)
                try:
                    req_id = _rid()
                    t0 = time.time() * 1000
                    await ws.send(json.dumps({"type": "time_req", "req_id": req_id,
                                              "client_t0_ms": t0}))
                    resp = await _recv_type(ws, "time_resp", req_id, timeout=5)
                    t1 = time.time() * 1000
                    rtt = t1 - t0
                    samples.append({"rtt": rtt, "offset": resp["server_now_ms"] - (t0 + rtt / 2)})
                except Exception:
                    pass

            # ── Phase 3: Fresh 3-round sample at T₀ − 5 s ────────────────
            ms_to_refresh = _lt_local_t0(start_at) - time.time() * 1000 - 5000
            if ms_to_refresh > 0.1:
                await asyncio.sleep(ms_to_refresh / 1000)
            fresh_samples: List[dict] = []
            for i in range(3):
                try:
                    req_id = _rid()
                    t0 = time.time() * 1000
                    await ws.send(json.dumps({"type": "time_req", "req_id": req_id,
                                              "client_t0_ms": t0}))
                    resp = await _recv_type(ws, "time_resp", req_id, timeout=5)
                    t1 = time.time() * 1000
                    rtt = t1 - t0
                    s_entry = {"rtt": rtt, "offset": resp["server_now_ms"] - (t0 + rtt / 2)}
                    fresh_samples.append(s_entry)
                    samples.append(s_entry)
                except Exception:
                    pass
                if i < 2:
                    await asyncio.sleep(0.1)

            result.my_rtt_ms = min(samples, key=lambda s: s["rtt"])["rtt"]

            # ── Phase 4: wait for T₀ → exam_req ──────────────────────────
            wait_sec = (_lt_local_t0(start_at) - time.time() * 1000) / 1000
            if wait_sec > 0:
                await asyncio.sleep(min(wait_sec, 120))

            fresh_rtt_samples = None
            fresh_offset_ms = None
            if fresh_samples:
                sr = sorted(s["rtt"] for s in fresh_samples)
                fresh_rtt_samples = [sr[0], sr[len(sr) // 2], sr[-1]]
                fresh_offset_ms = min(fresh_samples, key=lambda s: s["rtt"])["offset"]

            # Synchronise all load-test clients before sending exam_req
            # so they all land in the same ExamSendCoordinator window.
            # Without this, NTP offset variance can shift local_t0 by
            # hundreds of ms, causing late clients to start a new
            # coordinator round ~COLLECTION_WINDOW_SEC later → 297 ms spread.
            await pre_exam_barrier.wait()

            req_id = _rid()
            exam_req_msg = {"type": "exam_req", "req_id": req_id,
                            "exam_id": sched["exam_id"]}
            if fresh_rtt_samples:
                exam_req_msg["rtt_samples"] = fresh_rtt_samples
                exam_req_msg["offset_ms"]   = fresh_offset_ms
            await ws.send(json.dumps(exam_req_msg))
            exam_resp = await _recv_type(ws, "exam_resp", req_id, timeout=30)
            result.exam_resp_at = time.monotonic()

            await exam_barrier.wait()
            await q.put({"type": "lt_exam_recv", "idx": idx})

            exam = exam_resp["exam"]

            # exam_begin
            req_id = _rid()
            await ws.send(json.dumps({"type": "exam_begin", "req_id": req_id,
                                      "exam_id": exam["exam_id"],
                                      "opened_at_ms": result.exam_resp_at * 1000}))
            try:
                await asyncio.wait_for(ws.recv(), timeout=3)
            except asyncio.TimeoutError:
                pass

            # submit first option for every question
            answers = []
            for q_item in exam["questions"]:
                if q_item["type"] == "mcq" and q_item.get("options"):
                    answers.append({"qid": q_item["id"], "type": "mcq",
                                    "optionId": q_item["options"][0]["id"]})
                else:
                    answers.append({"qid": q_item["id"], "type": "text",
                                    "text": f"load-test client {idx}"})

            req_id = _rid()
            await ws.send(json.dumps({"type": "submit_req", "req_id": req_id,
                                      "exam_id": exam["exam_id"], "answers": answers}))
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=6)
                result.submitted = json.loads(raw).get("ok", False)
            except asyncio.TimeoutError:
                pass

            await q.put({"type": "lt_done", "idx": idx,
                         "submitted": result.submitted})

    except Exception as exc:
        result.error = str(exc)
        sched_barrier.release()
        pre_exam_barrier.release()
        exam_barrier.release()
        await q.put({"type": "lt_error", "idx": idx, "message": str(exc)})


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────────────────────────────────────
app = FastAPI()

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML

@app.get("/stream")
async def live_stream():
    async def gen() -> AsyncGenerator[str, None]:
        while True:
            loop = asyncio.get_event_loop()
            data = await loop.run_in_executor(None, _get_live_data)
            yield f"data: {json.dumps(data)}\n\n"
            await asyncio.sleep(2)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})

@app.post("/api/netem/{port}")
async def set_netem(port: int, body: dict):
    client_map = _get_client_containers()
    container  = client_map.get(port)
    if not container:
        return {"ok": False, "error": f"No running container on port {port}"}

    delay  = int(body.get("delay", 0))
    jitter = int(body.get("jitter", 0))
    loss   = float(body.get("loss", 0))

    loop = asyncio.get_event_loop()
    err  = await loop.run_in_executor(None, apply_netem, container, delay, jitter, loss)

    if not err:
        netem_state[port] = {"delay": delay, "jitter": jitter, "loss": loss}
        return {"ok": True, "container": container,
                "applied": f"delay={delay}ms jitter={jitter}ms loss={loss}%"}
    return {"ok": False, "error": err}

@app.post("/api/set_start")
async def set_start(body: dict):
    offset = int(body.get("offset_sec", 20))
    start_ms = int((time.time() + offset) * 1000)
    py_cmd = (f"import pathlib; p=pathlib.Path('/app/shared');"
              f"p.mkdir(parents=True,exist_ok=True);"
              f"p.joinpath('start_at.txt').write_text('{start_ms}')")
    loop = asyncio.get_event_loop()
    r = await loop.run_in_executor(
        None,
        lambda: subprocess.run(
            ["docker", "exec", "rudp-server-1", "python3", "-c", py_cmd],
            capture_output=True, text=True, cwd=PROJECT_ROOT
        )
    )
    ok = r.returncode == 0
    return {"ok": ok, "start_at_ms": start_ms,
            "t": time.strftime("%H:%M:%S", time.localtime(start_ms / 1000))}

@app.get("/api/dns")
async def dns_trace():
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, lambda: _api_get("/api/dns/trace"))
    return data


_current_dns_mode = "iterative"


def _write_dns_mode_to_containers(mode: str) -> bool:
    """Write /tmp/dns_mode.json into dns-resolver and /tmp/dns_recursive.json into dns-root."""
    recursive_enabled = mode == "recursive"
    resolver_payload  = json.dumps({"mode": mode})
    root_payload      = json.dumps({"enabled": recursive_enabled})

    resolver_cmd = (
        f"import pathlib; "
        f"pathlib.Path('/tmp/dns_mode.json').write_text({resolver_payload!r})"
    )
    root_cmd = (
        f"import pathlib; "
        f"pathlib.Path('/tmp/dns_recursive.json').write_text({root_payload!r})"
    )

    r1 = _sh(["docker", "exec", "dns-resolver", "python3", "-c", resolver_cmd])
    r2 = _sh(["docker", "exec", "dns-root",     "python3", "-c", root_cmd])
    return r1.returncode == 0 and r2.returncode == 0


@app.get("/api/dns/mode")
async def get_dns_mode():
    return {"mode": _current_dns_mode}


@app.post("/api/dns/mode")
async def set_dns_mode(mode: str = "iterative"):
    global _current_dns_mode
    if mode not in ("iterative", "recursive"):
        return {"ok": False, "error": "mode must be iterative or recursive", "mode": _current_dns_mode}
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(None, lambda: _write_dns_mode_to_containers(mode))
    if ok:
        _current_dns_mode = mode
    return {"ok": ok, "mode": _current_dns_mode}

@app.get("/api/compare")
async def compare():
    """
    Return per-transport statistics derived from clients.json.
    Groups clients by their 'transport' field (rudp-sync / tcp-sync / tcp-nosync).
    Computes: count, avg_rtt_ms, spread_ms (max - min exam_opened_at).
    """
    loop = asyncio.get_event_loop()
    clients_data = await loop.run_in_executor(None, lambda: _api_get("/api/clients"))

    # clients_data is a dict of {client_id: {...fields...}}
    from collections import defaultdict
    all_clients = list((clients_data or {}).values())

    # Filter to the most recent exam session only.
    # clients.json accumulates across sessions; mixing timestamps from different
    # days produces absurd spread values. Keep only clients whose connected_at
    # is within 2 hours of the most recently connected client.
    SESSION_WINDOW_MS = 2 * 3600 * 1000
    connected_times = [c['connected_at'] for c in all_clients if c.get('connected_at')]
    if connected_times:
        latest_connected = max(connected_times)
        all_clients = [c for c in all_clients
                       if c.get('connected_at', 0) >= latest_connected - SESSION_WINDOW_MS]

    groups: dict[str, list] = defaultdict(list)
    for info in all_clients:
        transport = info.get('transport', 'rudp-sync')
        groups[transport].append(info)

    modes_order = ['rudp-sync', 'tcp-sync', 'tcp-nosync']
    result_modes = []
    for mode in modes_order:
        clients = groups.get(mode, [])
        if not clients:
            result_modes.append({'mode': mode, 'count': 0, 'submitted': 0,
                                  'avg_rtt_ms': None, 'min_rtt_ms': None,
                                  'max_rtt_ms': None, 'spread_ms': None})
            continue
        rtts = [c['rtt_ms'] for c in clients if c.get('rtt_ms')]
        avg_rtt = sum(rtts) / len(rtts) if rtts else None
        min_rtt = min(rtts) if rtts else None
        max_rtt = max(rtts) if rtts else None
        submitted = sum(1 for c in clients if c.get('submitted'))
        opened = [c['exam_opened_at'] for c in clients
                  if c.get('exam_opened_at') and c.get('submitted') is not None]
        spread = (max(opened) - min(opened)) if len(opened) >= 2 else (0.0 if len(opened) == 1 else None)
        result_modes.append({
            'mode':        mode,
            'count':       len(clients),
            'submitted':   submitted,
            'avg_rtt_ms':  round(avg_rtt, 1) if avg_rtt is not None else None,
            'min_rtt_ms':  round(min_rtt, 1) if min_rtt is not None else None,
            'max_rtt_ms':  round(max_rtt, 1) if max_rtt is not None else None,
            'spread_ms':   round(spread, 1) if spread is not None else None,
        })

    note = None
    if all(m['count'] == 0 for m in result_modes):
        note = "Open browser tabs with ?mode=rudp-sync, ?mode=tcp-sync, ?mode=tcp-nosync to populate."
    return {'modes': result_modes, 'note': note}


@app.get("/api/sync_compare")
async def sync_compare_endpoint(port: int = 8081):
    global _sync_cmp_running
    if _sync_cmp_running:
        async def _busy():
            yield f'data: {{"type":"sync_error","message":"already running"}}\n\n'
        return StreamingResponse(_busy(), media_type="text/event-stream")

    async def gen() -> AsyncGenerator[str, None]:
        global _sync_cmp_running
        _sync_cmp_running = True
        try:
            q: asyncio.Queue = asyncio.Queue()
            task = asyncio.create_task(_run_sync_compare(port, q))
            while not (task.done() and q.empty()):
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=0.3)
                    yield f"data: {json.dumps(evt)}\n\n"
                except asyncio.TimeoutError:
                    pass
            await task
        finally:
            _sync_cmp_running = False

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


_MODE_INJECT_CONTAINER = {
    "rudp-sync":  "rudp-server-1",
    "tcp-sync":   "tcp-server-sync",
    "tcp-nosync": "tcp-server-nosync",
}


@app.get("/api/simulate")
async def simulate(offset: int = 20, mode: str = "rudp-sync"):
    global _sim_running
    if _sim_running:
        async def busy():
            yield f'data: {{"type":"error","message":"simulation already running"}}\n\n'
        return StreamingResponse(busy(), media_type="text/event-stream")

    async def gen() -> AsyncGenerator[str, None]:
        global _sim_running
        _sim_running = True
        try:
            q: asyncio.Queue = asyncio.Queue()
            n = len(CLIENT_PORTS)
            results = [SimResult(port=p) for p in CLIENT_PORTS]
            sched_b = SimpleBarrier(n)
            exam_b  = SimpleBarrier(n)

            # Inject start time into the appropriate container for this mode
            inject_container = _MODE_INJECT_CONTAINER.get(mode, "rudp-server-1")
            start_ms = int((time.time() + offset) * 1000)
            py_cmd   = (f"import pathlib; p=pathlib.Path('/app/shared');"
                        f"p.mkdir(parents=True,exist_ok=True);"
                        f"p.joinpath('start_at.txt').write_text('{start_ms}')")
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    ["docker", "exec", inject_container, "python3", "-c", py_cmd],
                    capture_output=True, text=True, cwd=PROJECT_ROOT
                )
            )

            tasks = [
                asyncio.create_task(_sim_client(CLIENT_PORTS[i], results[i],
                                                sched_b, exam_b, q, mode))
                for i in range(n)
            ]

            sent_complete = False
            while not sent_complete:
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=0.5)
                    yield f"data: {json.dumps(evt)}\n\n"
                except asyncio.TimeoutError:
                    pass
                if all(t.done() for t in tasks) and q.empty():
                    sent_complete = True

            await asyncio.gather(*tasks, return_exceptions=True)

            # Compute deltas
            valid = [r for r in results if r.exam_resp_at > 0 and not r.error]
            t_ref = min((r.exam_resp_at for r in valid), default=0)
            for r in results:
                if r.exam_resp_at > 0:
                    r.exam_resp_delta_ms = round((r.exam_resp_at - t_ref) * 1000, 2)

            spread = max((r.exam_resp_delta_ms or 0 for r in valid), default=0)
            payload = {
                "type": "complete",
                "spread_ms": round(spread, 2),
                "results": [
                    {
                        "port":             r.port,
                        "client_id":        r.client_id,
                        "my_rtt_ms":        round(r.my_rtt_ms, 2),
                        "max_rtt_ms":       round(r.max_rtt_ms, 2),
                        "deliver_delay_ms": r.deliver_delay_ms,
                        "exam_resp_delta_ms": r.exam_resp_delta_ms,
                        "submitted":        r.submitted,
                        "error":            r.error,
                    }
                    for r in results
                ],
            }
            yield f"data: {json.dumps(payload)}\n\n"
        finally:
            _sim_running = False

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})

@app.get("/api/load_test")
async def load_test_endpoint(count: int = 10, mode: str = "rudp-sync", offset: int = 20):
    global _load_test_running
    if _load_test_running:
        async def _busy():
            yield f'data: {{"type":"lt_error","idx":0,"message":"load test already running"}}\n\n'
        return StreamingResponse(_busy(), media_type="text/event-stream")

    count   = max(1, min(50, count))
    ws_path = MODE_WS_PATH.get(mode, "/ws")

    async def gen() -> AsyncGenerator[str, None]:
        global _load_test_running
        _load_test_running = True
        try:
            q: asyncio.Queue = asyncio.Queue()
            # All clients use the same container so they hit one
            # ExamSendCoordinator — the coordinator needs to see every
            # client to compute a global stagger schedule.
            port = CLIENT_PORTS[0]
            results = [
                LoadResult(
                    idx=i,
                    ws_url=f"ws://localhost:{port}{ws_path}",
                )
                for i in range(count)
            ]
            sched_b      = SimpleBarrier(count)
            pre_exam_b   = SimpleBarrier(count)
            exam_b       = SimpleBarrier(count)

            # Write shared start_at
            start_ms = int((time.time() + offset) * 1000)
            py_cmd = (
                f"import pathlib; p=pathlib.Path('/app/shared');"
                f"p.mkdir(parents=True,exist_ok=True);"
                f"p.joinpath('start_at.txt').write_text('{start_ms}')"
            )
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    ["docker", "exec", "rudp-server-1", "python3", "-c", py_cmd],
                    capture_output=True, text=True, cwd=PROJECT_ROOT,
                )
            )

            yield f"data: {json.dumps({'type': 'lt_start', 'count': count, 'mode': mode})}\n\n"

            tasks = [
                asyncio.create_task(
                    _load_client(i, results[i], sched_b, pre_exam_b, exam_b, q)
                )
                for i in range(count)
            ]

            while not (all(t.done() for t in tasks) and q.empty()):
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=0.3)
                    yield f"data: {json.dumps(evt)}\n\n"
                except asyncio.TimeoutError:
                    pass

            await asyncio.gather(*tasks, return_exceptions=True)

            # Compute timing deltas
            valid = [r for r in results if r.exam_resp_at > 0 and not r.error]
            t_ref = min((r.exam_resp_at for r in valid), default=0.0)
            for r in results:
                if r.exam_resp_at > 0:
                    r.exam_resp_delta_ms = round((r.exam_resp_at - t_ref) * 1000, 2)

            spread  = max((r.exam_resp_delta_ms or 0 for r in valid), default=0.0)
            success = sum(1 for r in results if r.submitted)
            rtts    = [r.my_rtt_ms for r in valid]
            avg_rtt = round(sum(rtts) / len(rtts), 2) if rtts else 0.0

            payload = {
                "type":       "lt_complete",
                "count":      count,
                "mode":       mode,
                "spread_ms":  round(spread, 2),
                "success":    success,
                "avg_rtt_ms": avg_rtt,
                "results": [
                    {
                        "idx":       r.idx,
                        "delta_ms":  r.exam_resp_delta_ms,
                        "submitted": r.submitted,
                        "rtt_ms":    round(r.my_rtt_ms, 1),
                        "error":     r.error,
                    }
                    for r in results
                ],
            }
            yield f"data: {json.dumps(payload)}\n\n"

        finally:
            _load_test_running = False

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Force UTF-8 on Windows
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print(f"\n  Professor Dashboard starting on http://localhost:{DASHBOARD_PORT}")
    print(f"  Admin panel:  {ADMIN_URL}")
    print(f"  Clients:      " + "  ".join(f"http://localhost:{p}" for p in CLIENT_PORTS))
    print()

    # Open browser after a short delay
    async def _open_browser():
        await asyncio.sleep(1.5)
        webbrowser.open(f"http://localhost:{DASHBOARD_PORT}")

    async def _main():
        asyncio.create_task(_open_browser())
        config = uvicorn.Config(app, host="0.0.0.0", port=DASHBOARD_PORT,
                                log_level="warning")
        server = uvicorn.Server(config)
        await server.serve()

    asyncio.run(_main())
