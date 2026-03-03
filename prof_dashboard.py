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
_sim_running = False

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
        Uses <code style="color:#60a5fa">tc netem</code> inside each client container to inject real network conditions
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
        <button class="btn btn-green" id="sim-btn" onclick="runSimulation()">Run Simulation</button>
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

    <!-- Timing Proof -->
    <div class="card" id="timing-card" style="display:none">
      <div class="card-title">⚡ Synchronized Delivery — Timing Proof</div>
      <div id="timing-rows"></div>
      <div id="timing-formula" style="font-size:11px;color:#475569;margin-top:10px;line-height:1.8;font-family:monospace"></div>
      <div id="timing-verdict" class="verdict-box"></div>
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
  'rudp-server-2':'rudp-2','rudp-server-3':'rudp-3','admin':'admin'
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
  const es = new EventSource('/api/simulate?offset='+offset);
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

  const valid = results.filter(r => r.exam_resp_delta_ms != null && !r.error);
  if (!valid.length) return;

  const maxDelta = Math.max(...valid.map(r => r.exam_resp_delta_ms), 1);
  const spread = maxDelta;

  let rowHtml = '';
  for (const r of results) {
    if (r.error) {
      rowHtml += `<div class="timing-row"><span class="timing-port">:${r.port}</span><span style="color:#ef4444">ERROR: ${r.error.slice(0,40)}</span></div>`;
      continue;
    }
    const delta = r.exam_resp_delta_ms || 0;
    const pct = Math.max(2, (delta / Math.max(maxDelta, 1)) * 100);
    const color = delta < 10 ? '#22c55e' : delta < 50 ? '#f59e0b' : '#ef4444';
    const fillColor = delta < 10 ? '#22c55e' : delta < 50 ? '#f59e0b' : '#ef4444';
    rowHtml += `<div class="timing-row">
      <span class="timing-port">:${r.port}</span>
      <span class="timing-delta" style="color:${color}">+${delta.toFixed(1)}ms</span>
      <div class="timing-bar"><div class="timing-fill" style="width:${pct}%;background:${fillColor}"></div></div>
      <span class="timing-info">RTT=${r.my_rtt_ms||'?'}ms delay=${r.deliver_delay_ms||0}ms</span>
    </div>`;
  }
  document.getElementById('timing-rows').innerHTML = rowHtml;

  // Formula
  let fHtml = 'deliver_delay = (max_rtt − my_rtt) / 2 + buffer(30ms)\n';
  for (const r of valid) {
    fHtml += `  :${r.port}  (${r.max_rtt_ms||0}ms − ${r.my_rtt_ms||0}ms)/2 + 30 = ${r.deliver_delay_ms||0}ms  →  bridge held ${r.deliver_delay_ms||0}ms\n`;
  }
  document.getElementById('timing-formula').textContent = fHtml;

  // Verdict
  const vEl = document.getElementById('timing-verdict');
  if (spread < 30) {
    vEl.style.background = '#14532d'; vEl.style.color = '#4ade80';
    vEl.textContent = `✓ SYNCHRONIZED — spread ${spread.toFixed(1)} ms < 30 ms jitter buffer`;
  } else if (spread < 100) {
    vEl.style.background = '#78350f'; vEl.style.color = '#fbbf24';
    vEl.textContent = `⚠ NEAR-SYNC — spread ${spread.toFixed(1)} ms (within 100 ms)`;
  } else {
    vEl.style.background = '#7f1d1d'; vEl.style.color = '#f87171';
    vEl.textContent = `✗ OUT OF SYNC — spread ${spread.toFixed(1)} ms`;
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────
window.onload = () => {
  startStream();
  initChart();
  loadDns();
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
# tc netem
# ─────────────────────────────────────────────────────────────────────────────
def _get_iface(container: str) -> str:
    """Get the default network interface inside the container."""
    try:
        r = _sh(["docker", "exec", container, "sh", "-c",
                 "ip route show default | head -1 | awk '{print $5}'"])
        iface = r.stdout.strip()
        return iface if iface else "eth0"
    except Exception:
        return "eth0"

def apply_netem(container: str, delay_ms: int, jitter_ms: int, loss_pct: float) -> str:
    iface = _get_iface(container)

    # Build netem parameters
    parts = []
    if delay_ms > 0:
        parts.append(f"delay {delay_ms}ms")
        if jitter_ms > 0:
            parts.append(f"{jitter_ms}ms")
    if loss_pct > 0:
        parts.append(f"loss {loss_pct}%")

    # Delete existing root qdisc, then add fresh netem (or pfifo if clearing)
    _sh(["docker", "exec", container, "tc", "qdisc", "del", "dev", iface, "root"])

    if parts:
        cmd = ["docker", "exec", container, "tc", "qdisc", "add", "dev", iface,
               "root", "netem"] + " ".join(parts).split()
        r = _sh(cmd)
        if r.returncode != 0:
            return r.stderr.strip() or "tc netem failed"
    return ""   # success

# ─────────────────────────────────────────────────────────────────────────────
# Live stats
# ─────────────────────────────────────────────────────────────────────────────
def _get_live_data() -> dict:
    # Core services
    containers = {}
    for svc in ["dns-root", "dns-tld", "dns-auth", "dns-resolver",
                "dhcp-server", "rudp-server-1", "rudp-server-2", "rudp-server-3", "admin"]:
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
    async def wait(self):
        self._count += 1
        if self._count >= self._n:
            self._ev.set()
        await self._ev.wait()

async def _sim_client(port: int, result: SimResult,
                      sched_barrier: SimpleBarrier,
                      exam_barrier: SimpleBarrier,
                      q: asyncio.Queue):
    client_id = f"prof-{port}-{_rid()}"
    result.client_id = client_id
    try:
        async with websockets.connect(f"ws://localhost:{port}/ws",
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

            # NTP sync
            samples = []
            for i in range(NTP_ROUNDS):
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
                             "samples": i + 1, "total": NTP_ROUNDS,
                             "best_rtt": best["rtt"], "sample_rtt": rtt})
                await asyncio.sleep(0.12)

            best = min(samples, key=lambda s: s["rtt"])
            result.my_rtt_ms = best["rtt"]
            offset_ms = best["offset"]

            # schedule
            req_id = _rid()
            await ws.send(json.dumps({"type": "schedule_req", "req_id": req_id}))
            sched = await _recv_type(ws, "schedule_resp", req_id, timeout=10)
            result.max_rtt_ms       = sched.get("max_rtt_ms", 0)
            result.deliver_delay_ms = sched.get("deliver_delay_ms", 0)
            await q.put({"type": "schedule", "port": port,
                         "deliver_delay_ms": result.deliver_delay_ms,
                         "max_rtt_ms": result.max_rtt_ms})

            await sched_barrier.wait()

            # wait for T₀
            start_at = sched["start_at_server_ms"]
            local_start = start_at - offset_ms
            wait_sec = (local_start - time.time() * 1000) / 1000
            if wait_sec > 0:
                await asyncio.sleep(wait_sec)

            # exam_req
            await q.put({"type": "exam_opening", "port": port})
            req_id = _rid()
            result.exam_req_at = time.monotonic()
            await ws.send(json.dumps({"type": "exam_req", "req_id": req_id,
                                      "exam_id": sched["exam_id"]}))
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
        await q.put({"type": "error", "port": port, "message": str(e)})

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

@app.get("/api/simulate")
async def simulate(offset: int = 20):
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

            # Inject start time
            start_ms = int((time.time() + offset) * 1000)
            py_cmd   = (f"import pathlib; p=pathlib.Path('/app/shared');"
                        f"p.mkdir(parents=True,exist_ok=True);"
                        f"p.joinpath('start_at.txt').write_text('{start_ms}')")
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    ["docker", "exec", "rudp-server-1", "python3", "-c", py_cmd],
                    capture_output=True, text=True, cwd=PROJECT_ROOT
                )
            )

            tasks = [
                asyncio.create_task(_sim_client(CLIENT_PORTS[i], results[i],
                                                sched_b, exam_b, q))
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
