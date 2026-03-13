"""
Admin panel for the exam network.
Reads data written by exam_server.py to shared volumes.

Endpoints:
  GET  /                  — Dashboard HTML
  GET  /api/status        — start_at, server_now, summary counts
  GET  /api/clients       — full clients.json
  GET  /api/submissions   — all saved/submitted answers
  GET  /api/exam          — current exam JSON
  POST /api/exam          — upload new exam JSON
  POST /api/reset         — wipe clients.json, start_at.txt, all answers
"""
from __future__ import annotations
import json
import os
import pathlib
import random
import socket
import struct
import time as _time
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse

app = FastAPI(title="Exam Admin Panel")

SHARED_DIR  = pathlib.Path(os.environ.get('SHARED_DIR',  '/app/shared'))
ANSWERS_DIR = pathlib.Path(os.environ.get('ANSWERS_DIR', '/app/answers'))
DNS_ROOT    = os.environ.get('DNS_ROOT',     '10.99.0.10')


# ── Minimal inline DNS client (for trace endpoint) ────────────────────────────

def _dns_encode_name(name: str) -> bytes:
    out = b''
    for label in name.rstrip('.').split('.'):
        out += bytes([len(label)]) + label.encode()
    return out + b'\x00'


def _dns_decode_name(data: bytes, offset: int):
    labels: List[str] = []
    visited: set = set()
    while offset < len(data):
        if offset in visited:
            break
        visited.add(offset)
        length = data[offset]
        if length == 0:
            offset += 1
            break
        elif (length & 0xC0) == 0xC0:
            ptr = ((length & 0x3F) << 8) | data[offset + 1]
            sub, _ = _dns_decode_name(data, ptr)
            if sub:
                labels.append(sub)
            offset += 2
            break
        else:
            labels.append(data[offset + 1: offset + 1 + length].decode('ascii', errors='replace'))
            offset += 1 + length
    return '.'.join(labels), offset


def _dns_parse_rrs(data: bytes, offset: int, count: int):
    rrs = []
    for _ in range(count):
        name, offset = _dns_decode_name(data, offset)
        if offset + 10 > len(data):
            break
        rtype, _cls, ttl, rdlen = struct.unpack_from('!HHIH', data, offset)
        offset += 10
        rdata_offset = offset
        rdata = data[offset: offset + rdlen]
        offset += rdlen
        if rtype == 1 and rdlen == 4:           # A
            rrs.append({'name': name, 'type': 'A',  'ttl': ttl, 'value': '.'.join(str(b) for b in rdata)})
        elif rtype == 2:                         # NS
            ns, _ = _dns_decode_name(data, rdata_offset)
            rrs.append({'name': name, 'type': 'NS', 'ttl': ttl, 'value': ns})
        else:
            rrs.append({'name': name, 'type': rtype, 'ttl': ttl, 'value': rdata.hex()})
    return rrs, offset


def _dns_query(server_ip: str, name: str, timeout: float = 2.0) -> dict:
    """Send a DNS A-record query; return parsed response dict."""
    tx_id = random.randint(0, 65535)
    flags = 0x0100                           # standard query, RD=0 (we do iteration ourselves)
    encoded = _dns_encode_name(name)
    header   = struct.pack('!HHHHHH', tx_id, flags, 1, 0, 0, 0)
    question = encoded + struct.pack('!HH', 1, 1)  # QTYPE=A, QCLASS=IN
    query = header + question

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        t0 = _time.monotonic()
        sock.sendto(query, (server_ip, 53))
        data, _ = sock.recvfrom(1024)
        rtt_ms = round((_time.monotonic() - t0) * 1000, 1)
    finally:
        sock.close()

    hdr = struct.unpack_from('!HHHHHH', data, 0)
    resp_flags = hdr[1]
    aa     = bool((resp_flags >> 10) & 1)
    rcode  = resp_flags & 0xF
    qdcount, ancount, nscount, arcount = hdr[2], hdr[3], hdr[4], hdr[5]

    offset = 12
    for _ in range(qdcount):                 # skip question section
        _, offset = _dns_decode_name(data, offset)
        offset += 4

    answers,    offset = _dns_parse_rrs(data, offset, ancount)
    authority,  offset = _dns_parse_rrs(data, offset, nscount)
    additional, _      = _dns_parse_rrs(data, offset, arcount)
    return {
        'aa': aa, 'rcode': rcode, 'rtt_ms': rtt_ms,
        'answers': answers, 'authority': authority, 'additional': additional,
    }


def _dns_iterative_trace(name: str) -> List[dict]:
    """Walk the DNS hierarchy root → TLD → auth, recording each hop."""
    steps: List[dict] = []
    server_ip   = DNS_ROOT
    server_label = f'Root nameserver ({DNS_ROOT})'

    for _ in range(6):
        step: dict = {'server': server_label, 'server_ip': server_ip, 'query': f'{name} A?'}
        try:
            r = _dns_query(server_ip, name)
        except Exception as e:
            step['error'] = str(e)
            steps.append(step)
            break

        step['rtt_ms']    = r['rtt_ms']
        step['aa']        = r['aa']
        step['rcode']     = r['rcode']
        step['answers']   = r['answers']
        step['authority'] = r['authority']
        step['additional']= r['additional']

        if r['answers']:
            step['result'] = 'ANSWER'
            steps.append(step)
            break

        # Referral — follow the first NS with glue
        ns_rrs = [x for x in r['authority'] if x['type'] == 'NS']
        glue   = {x['name']: x['value'] for x in r['additional'] if x['type'] == 'A'}

        if not ns_rrs:
            step['result'] = 'NXDOMAIN' if r['rcode'] == 3 else 'NO_NS'
            steps.append(step)
            break

        ns_name = ns_rrs[0]['value']
        next_ip = glue.get(ns_name) or glue.get(ns_name.rstrip('.') + '.')
        if not next_ip:
            step['result'] = f'REFERRAL (no glue for {ns_name})'
            steps.append(step)
            break

        step['result'] = f'REFERRAL → {ns_name} ({next_ip})'
        steps.append(step)
        server_ip    = next_ip
        server_label = f'{ns_name} ({next_ip})'

    return steps


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_clients() -> Dict[str, Any]:
    cf = SHARED_DIR / 'clients.json'
    if not cf.exists():
        return {}
    try:
        return json.loads(cf.read_text(encoding='utf-8'))
    except Exception:
        return {}


# ── API endpoints ─────────────────────────────────────────────────────────────

@app.get("/api/status")
async def api_status():
    start_at_ms = None
    start_file = SHARED_DIR / 'start_at.txt'
    if start_file.exists():
        try:
            start_at_ms = int(start_file.read_text().strip())
        except Exception:
            pass

    clients = _load_clients()
    submitted = sum(1 for c in clients.values() if c.get('submitted'))
    connected = sum(1 for c in clients.values() if not c.get('submitted'))

    return {
        "server_now_ms":   int(_time.time() * 1000),
        "start_at_ms":     start_at_ms,
        "total_clients":   len(clients),
        "connected_count": connected,
        "submitted_count": submitted,
    }


@app.get("/api/clients")
async def api_clients():
    return _load_clients()


@app.get("/api/submissions")
async def api_submissions():
    result: Dict[str, Any] = {}
    if not ANSWERS_DIR.exists():
        return result
    for exam_dir in sorted(ANSWERS_DIR.iterdir()):
        if not exam_dir.is_dir():
            continue
        exam_id = exam_dir.name
        result[exam_id] = {}
        for f in sorted(exam_dir.iterdir()):
            name = f.name
            if name.endswith('.final.json'):
                client_id = name[:-len('.final.json')]
                is_final  = True
            elif name.endswith('.json'):
                client_id = name[:-len('.json')]
                is_final  = False
            else:
                continue
            try:
                answers = json.loads(f.read_text(encoding='utf-8'))
                # Final always wins over draft
                if client_id not in result[exam_id] or is_final:
                    result[exam_id][client_id] = {
                        "final":   is_final,
                        "answers": answers,
                    }
            except Exception:
                pass
    return result


@app.get("/api/exam")
async def api_exam():
    path = SHARED_DIR / 'exam.json'
    if path.exists():
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {"error": "No exam uploaded yet — using server default"}


@app.post("/api/exam")
async def upload_exam(file: UploadFile = File(...)):
    data = await file.read()
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"Invalid JSON: {e}")
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    (SHARED_DIR / 'exam.json').write_bytes(data)
    return {"ok": True, "exam_id": parsed.get("exam_id"), "questions": len(parsed.get("questions", []))}


@app.post("/api/reset")
async def api_reset():
    """Wipe all session data: clients.json, start_at.txt, and all answers."""
    import shutil
    deleted: list[str] = []
    errors:  list[str] = []

    # Remove clients.json
    cf = SHARED_DIR / 'clients.json'
    if cf.exists():
        try:
            cf.unlink()
            deleted.append('clients.json')
        except Exception as e:
            errors.append(f'clients.json: {e}')

    # Remove start_at.txt
    sf = SHARED_DIR / 'start_at.txt'
    if sf.exists():
        try:
            sf.unlink()
            deleted.append('start_at.txt')
        except Exception as e:
            errors.append(f'start_at.txt: {e}')

    # Wipe all answer files (keep the directory structure)
    if ANSWERS_DIR.exists():
        for exam_dir in ANSWERS_DIR.iterdir():
            if exam_dir.is_dir():
                try:
                    shutil.rmtree(exam_dir)
                    deleted.append(f'answers/{exam_dir.name}/')
                except Exception as e:
                    errors.append(f'answers/{exam_dir.name}: {e}')

    if errors:
        raise HTTPException(500, detail={"deleted": deleted, "errors": errors})
    return {"ok": True, "deleted": deleted}


@app.get("/api/dns/trace")
async def dns_trace(name: str = "server.exam.lan"):
    """Perform iterative DNS resolution and return each hop's details."""
    import asyncio
    loop = asyncio.get_running_loop()
    steps = await loop.run_in_executor(None, _dns_iterative_trace, name)
    # Collect final A records
    answers = []
    for step in steps:
        if step.get('answers'):
            answers = step['answers']
    return {"name": name, "steps": steps, "final_answers": answers}


# ── Dashboard HTML ────────────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Admin — מערכת הבחינות</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0d1117;color:#c9d1d9;min-height:100vh}
a{color:#58a6ff}

/* Header */
header{background:#161b22;border-bottom:1px solid #30363d;padding:16px 28px;display:flex;align-items:center;gap:20px}
header h1{font-size:1.25rem;font-weight:600;color:#f0f6fc}
#server-clock{margin-right:auto;font-size:.85rem;color:#8b949e;font-family:monospace}
#exam-badge{padding:4px 12px;border-radius:20px;font-size:.8rem;font-weight:600}
.badge-waiting{background:#1f2d3d;color:#58a6ff;border:1px solid #58a6ff}
.badge-running{background:#1a3a1a;color:#3fb950;border:1px solid #3fb950}
.badge-ended  {background:#3a1a1a;color:#f85149;border:1px solid #f85149}

/* Stat bar */
#stats{display:flex;gap:24px;padding:14px 28px;background:#161b22;border-bottom:1px solid #30363d}
.stat{display:flex;flex-direction:column;align-items:center;gap:2px}
.stat-val{font-size:1.6rem;font-weight:700;color:#f0f6fc}
.stat-lbl{font-size:.72rem;color:#8b949e;text-transform:uppercase;letter-spacing:.05em}

/* Tabs */
nav{display:flex;gap:0;border-bottom:1px solid #30363d;background:#161b22}
.tab{padding:12px 24px;cursor:pointer;font-size:.9rem;color:#8b949e;border-bottom:2px solid transparent;user-select:none}
.tab.active{color:#f0f6fc;border-bottom-color:#1f6feb}
.tab:hover:not(.active){color:#c9d1d9}

/* Panels */
.panel{display:none;padding:24px 28px}
.panel.active{display:block}

/* Tables */
table{width:100%;border-collapse:collapse;font-size:.875rem}
th{text-align:right;padding:10px 14px;background:#161b22;color:#8b949e;font-weight:500;border-bottom:1px solid #30363d;position:sticky;top:0}
td{padding:9px 14px;border-bottom:1px solid #21262d;vertical-align:middle}
tr:hover td{background:#161b22}
.tbl-wrap{overflow-x:auto;border:1px solid #30363d;border-radius:8px;margin-top:16px}

/* Progress bar */
.prog-wrap{background:#21262d;border-radius:4px;height:8px;min-width:80px}
.prog-fill{height:8px;border-radius:4px;background:#1f6feb;transition:width .4s}
.prog-fill.done{background:#3fb950}

/* Badges */
.tag{display:inline-block;padding:2px 8px;border-radius:10px;font-size:.75rem;font-weight:600}
.tag-ok   {background:#1a3a1a;color:#3fb950}
.tag-wait {background:#1f2d3d;color:#58a6ff}
.tag-final{background:#1f3a1a;color:#3fb950}
.tag-draft{background:#2d2614;color:#d29922}

/* Delta */
.delta-good{color:#3fb950}
.delta-warn{color:#d29922}
.delta-bad {color:#f85149}

/* Accordion */
.accordion{border:1px solid #30363d;border-radius:8px;margin-bottom:12px;overflow:hidden}
.acc-head{padding:12px 16px;background:#161b22;cursor:pointer;display:flex;justify-content:space-between;align-items:center;font-weight:500}
.acc-head:hover{background:#1c2128}
.acc-body{display:none;padding:16px;background:#0d1117}
.acc-body.open{display:block}
.acc-arr{transition:transform .2s;color:#8b949e}
.acc-arr.open{transform:rotate(180deg)}

/* Answer card */
.answer-card{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:12px 16px;margin-bottom:10px}
.answer-card h4{font-size:.8rem;color:#8b949e;margin-bottom:6px}
.answer-card p{font-size:.9rem;color:#c9d1d9}
.answer-mcq{display:inline-block;background:#1f2d3d;color:#58a6ff;padding:3px 10px;border-radius:4px;font-size:.85rem}

/* Upload */
.upload-box{border:2px dashed #30363d;border-radius:8px;padding:40px;text-align:center;margin-top:16px}
.upload-box.drag{border-color:#1f6feb;background:#0d1929}
.file-input{display:none}
.btn{display:inline-block;padding:9px 20px;border-radius:6px;font-size:.875rem;font-weight:500;cursor:pointer;border:none;transition:opacity .15s}
.btn-primary{background:#1f6feb;color:#fff}
.btn-primary:hover{opacity:.85}
.btn-secondary{background:#21262d;color:#c9d1d9;border:1px solid #30363d}
.btn-secondary:hover{background:#2d333b}
.upload-status{margin-top:16px;padding:12px 16px;border-radius:6px;display:none}
.upload-ok {background:#1a3a1a;color:#3fb950;border:1px solid #3fb950}
.upload-err{background:#3a1a1a;color:#f85149;border:1px solid #f85149}

/* Exam preview */
pre{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:16px;font-size:.8rem;overflow:auto;max-height:400px;margin-top:16px}

/* Misc */
.section-title{font-size:1rem;font-weight:600;color:#f0f6fc;margin-bottom:4px}
.section-sub{font-size:.82rem;color:#8b949e;margin-bottom:16px}
.empty{color:#8b949e;font-style:italic;padding:20px 0;text-align:center}
.mono{font-family:monospace;font-size:.85rem}

/* Reset button */
.btn-reset{background:#3a1a1a;color:#f85149;border:1px solid #f85149;padding:6px 16px;border-radius:6px;font-size:.85rem;cursor:pointer;font-weight:600;transition:background .15s}
.btn-reset:hover{background:#5a1a1a}

/* Reset modal */
#reset-modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:999;align-items:center;justify-content:center}
#reset-modal.show{display:flex}
#reset-box{background:#161b22;border:1px solid #f85149;border-radius:10px;padding:32px 36px;max-width:420px;width:90%;text-align:center}
#reset-box h2{color:#f85149;margin-bottom:12px;font-size:1.1rem}
#reset-box p{color:#8b949e;font-size:.9rem;margin-bottom:24px;line-height:1.5}
#reset-box .btn-row{display:flex;gap:12px;justify-content:center}
#reset-box .btn-cancel{background:#21262d;color:#c9d1d9;border:1px solid #30363d;padding:8px 24px;border-radius:6px;cursor:pointer;font-size:.9rem}
#reset-box .btn-confirm{background:#da3633;color:#fff;border:none;padding:8px 24px;border-radius:6px;cursor:pointer;font-size:.9rem;font-weight:600}
#reset-box .btn-confirm:hover{background:#b62324}
#reset-result{margin-top:16px;font-size:.85rem;color:#3fb950;min-height:20px}
</style>
</head>
<body>

<header>
  <h1>🎓 Admin Panel — מערכת הבחינות</h1>
  <span id="server-clock">טוען...</span>
  <span id="exam-badge" class="badge-waiting">ממתין לבחינה</span>
  <button class="btn-reset" onclick="openResetModal()">🗑 איפוס מלא</button>
</header>

<!-- Reset confirmation modal -->
<div id="reset-modal">
  <div id="reset-box">
    <h2>⚠ איפוס מלא של המערכת</h2>
    <p>פעולה זו תמחק את כל הנתונים:<br>
       <strong>clients.json · start_at.txt · כל ההגשות</strong><br><br>
       לא ניתן לבטל פעולה זו.
    </p>
    <div class="btn-row">
      <button class="btn-cancel" onclick="closeResetModal()">ביטול</button>
      <button class="btn-confirm" onclick="confirmReset()">כן, אפס הכל</button>
    </div>
    <div id="reset-result"></div>
  </div>
</div>

<div id="stats">
  <div class="stat"><span class="stat-val" id="s-clients">—</span><span class="stat-lbl">משתתפים</span></div>
  <div class="stat"><span class="stat-val" id="s-connected">—</span><span class="stat-lbl">מחוברים</span></div>
  <div class="stat"><span class="stat-val" id="s-submitted">—</span><span class="stat-lbl">הגישו</span></div>
  <div class="stat"><span class="stat-val" id="s-t0">—</span><span class="stat-lbl">T₀ (שרת)</span></div>
</div>

<nav>
  <div class="tab active" onclick="switchTab('sync')">סנכרון זמן</div>
  <div class="tab" onclick="switchTab('monitor')">מעקב בזמן אמת</div>
  <div class="tab" onclick="switchTab('submissions')">הגשות</div>
  <div class="tab" onclick="switchTab('dns')">🌐 DNS Trace</div>
  <div class="tab" onclick="switchTab('upload')">העלאת שאלון</div>
</nav>

<!-- ── Tab 1: Sync Proof ── -->
<div id="panel-sync" class="panel active">
  <div class="section-title">הוכחת סנכרון — כל הלקוחות פותחים את הבחינה באותו מילישניה</div>
  <div class="section-sub">
    T₀ הרשמי נקבע על ידי שרת הבחינה ונשמר בקובץ משותף. הטבלה מציגה את ה-delta בין T₀ לבין
    הרגע בו כל לקוח אכן קיבל ופתח את השאלון.
  </div>
  <div class="tbl-wrap">
    <table id="sync-table">
      <thead><tr>
        <th>מזהה לקוח</th>
        <th>T₀ רשמי</th>
        <th>נפתח אצל הלקוח</th>
        <th>Δ פתיחה מ-T₀</th>
        <th>לחץ "התחל"</th>
        <th>Δ עד לחיצה</th>
        <th>סטטוס</th>
      </tr></thead>
      <tbody id="sync-body"><tr><td colspan="7" class="empty">טוען נתונים...</td></tr></tbody>
    </table>
  </div>
</div>

<!-- ── Tab 2: Live Monitor ── -->
<div id="panel-monitor" class="panel">
  <div class="section-title">מעקב חי אחר המשתתפים</div>
  <div class="section-sub">מתרענן אוטומטית כל 3 שניות. מציג את התקדמות כל לקוח בבחינה.</div>
  <div class="tbl-wrap">
    <table id="monitor-table">
      <thead><tr>
        <th>מזהה לקוח</th>
        <th>התחבר ב-</th>
        <th>התקדמות</th>
        <th>שאלות שענה</th>
        <th>פעיל לאחרונה</th>
        <th>הגשה</th>
      </tr></thead>
      <tbody id="monitor-body"><tr><td colspan="6" class="empty">טוען נתונים...</td></tr></tbody>
    </table>
  </div>
</div>

<!-- ── Tab 3: Submissions ── -->
<div id="panel-submissions" class="panel">
  <div class="section-title">הגשות</div>
  <div class="section-sub">סימון <span class="tag tag-final">סופי</span> = הוגש רשמית. <span class="tag tag-draft">טיוטה</span> = נשמר אוטומטית בלבד.</div>
  <div id="submissions-wrap"><p class="empty">טוען...</p></div>
</div>

<!-- ── Tab 4: DNS Trace ── -->
<div id="panel-dns" class="panel">
  <div class="section-title">DNS Trace — מעקב אחר פתרון שמות</div>
  <div class="section-sub">
    מבצע resolution איטרטיבי ידני: Root → TLD (.lan) → Authoritative (exam.lan) → תשובה.
    מדגים כי הלקוחות משתמשים בהיררכיית ה-DNS שבנינו ומקבלים round-robin load balancing.
  </div>

  <div style="display:flex;gap:10px;margin-top:16px;align-items:center">
    <input id="dns-name" type="text" value="server.exam.lan"
      style="flex:1;padding:9px 12px;border:1px solid #30363d;border-radius:6px;background:#161b22;color:#c9d1d9;font-family:monospace;font-size:.9rem"/>
    <button class="btn btn-primary" onclick="runDnsTrace()" id="dns-run-btn">▶ הרץ Lookup</button>
  </div>

  <div id="dns-result" style="margin-top:20px"></div>
</div>

<!-- ── Tab 5: Upload ── -->
<div id="panel-upload" class="panel">
  <div class="section-title">העלאת שאלון חדש</div>
  <div class="section-sub">
    העלה קובץ JSON בפורמט השאלון. השאלון החדש ייטען אוטומטית על ידי כל הלקוחות החדשים.
    לקוחות שכבר פתחו את הבחינה לא יושפעו.
  </div>

  <div class="upload-box" id="drop-zone">
    <p style="font-size:2rem;margin-bottom:12px">📄</p>
    <p style="margin-bottom:16px;color:#8b949e">גרור קובץ JSON לכאן, או</p>
    <input type="file" id="file-input" class="file-input" accept=".json"/>
    <button class="btn btn-secondary" onclick="document.getElementById('file-input').click()">בחר קובץ</button>
    <p id="file-name" style="margin-top:12px;color:#8b949e;font-size:.85rem"></p>
  </div>

  <div style="text-align:center;margin-top:20px">
    <button class="btn btn-primary" id="upload-btn" onclick="doUpload()" disabled>העלה שאלון</button>
  </div>
  <div class="upload-status" id="upload-status"></div>

  <div style="margin-top:32px">
    <div class="section-title">שאלון נוכחי בשרת</div>
    <pre id="exam-preview">טוען...</pre>
  </div>
</div>

<script>
// ── Utilities ─────────────────────────────────────────────────────────────────

function fmtMs(ms) {
  if (!ms) return '—';
  return new Date(ms).toLocaleTimeString('he-IL', {hour:'2-digit',minute:'2-digit',second:'2-digit'}) +
         '.' + String(ms % 1000).padStart(3,'0');
}

function fmtDelta(deltaMs) {
  if (deltaMs === null || deltaMs === undefined) return '<span class="tag tag-wait">לא פתח</span>';
  const abs = Math.abs(deltaMs);
  const sign = deltaMs >= 0 ? '+' : '';
  const cls = abs < 100 ? 'delta-good' : abs < 500 ? 'delta-warn' : 'delta-bad';
  return `<span class="${cls}">${sign}${deltaMs} ms</span>`;
}

function timeSince(ms) {
  if (!ms) return '—';
  const sec = Math.floor((Date.now() - ms) / 1000);
  if (sec < 60) return `${sec}ש`;
  if (sec < 3600) return `${Math.floor(sec/60)}ד`;
  return `${Math.floor(sec/3600)}ש`;
}

// ── Tab switching ─────────────────────────────────────────────────────────────

let activeTab = 'sync';

function switchTab(name) {
  document.querySelectorAll('.tab').forEach((t,i) => {
    const tabs = ['sync','monitor','submissions','dns','upload'];
    t.classList.toggle('active', tabs[i] === name);
  });
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.getElementById('panel-' + name).classList.add('active');
  activeTab = name;
  if (name === 'submissions') loadSubmissions();
  if (name === 'upload') loadExamPreview();
}

// ── DNS Trace ─────────────────────────────────────────────────────────────────

async function runDnsTrace() {
  const name = document.getElementById('dns-name').value.trim() || 'server.exam.lan';
  const btn  = document.getElementById('dns-run-btn');
  const out  = document.getElementById('dns-result');
  btn.disabled = true;
  btn.textContent = '⏳ מריץ...';
  out.innerHTML = '<p style="color:#8b949e">מבצע iterative DNS resolution...</p>';
  try {
    const r = await fetch('/api/dns/trace?name=' + encodeURIComponent(name));
    const d = await r.json();
    renderDnsTrace(d, out);
  } catch(e) {
    out.innerHTML = `<p style="color:#f85149">שגיאה: ${e}</p>`;
  } finally {
    btn.disabled = false;
    btn.textContent = '▶ הרץ Lookup';
  }
}

function renderDnsTrace(data, container) {
  const rcode_name = {0:'NOERROR',1:'FORMERR',2:'SERVFAIL',3:'NXDOMAIN',5:'REFUSED'};
  let html = `<div style="margin-bottom:16px">
    <span style="font-family:monospace;font-size:1rem;color:#f0f6fc;font-weight:700">${data.name}</span>
    <span style="color:#8b949e;margin:0 8px">→</span>
    ${data.final_answers.length
      ? data.final_answers.map(a => `<span style="display:inline-block;margin:2px 4px;padding:3px 10px;background:#1a3a1a;color:#3fb950;border-radius:6px;font-family:monospace">${a.value}</span>`).join('')
      : '<span style="color:#f85149">לא נפתר</span>'}
  </div>`;

  data.steps.forEach((step, i) => {
    const isAnswer   = step.result === 'ANSWER';
    const isReferral = step.result && step.result.startsWith('REFERRAL');
    const borderColor = isAnswer ? '#3fb950' : isReferral ? '#58a6ff' : '#f85149';
    const stepLabel   = isAnswer ? '✅ ANSWER' : isReferral ? '↪ REFERRAL' : step.result || '?';

    html += `<div style="margin-bottom:12px;border:1px solid #30363d;border-right:3px solid ${borderColor};border-radius:0 8px 8px 0;overflow:hidden">
      <div style="display:flex;justify-content:space-between;align-items:center;padding:10px 14px;background:#161b22">
        <div style="display:flex;align-items:center;gap:10px">
          <span style="width:22px;height:22px;border-radius:50%;background:#30363d;display:grid;place-items:center;font-size:.8rem;font-weight:700;color:#8b949e">${i+1}</span>
          <span style="font-family:monospace;font-size:.875rem;color:#c9d1d9">${step.server}</span>
          <span style="font-size:.8rem;color:#8b949e">← ${step.query}</span>
        </div>
        <div style="display:flex;gap:10px;align-items:center">
          ${step.rtt_ms != null ? `<span style="font-family:monospace;font-size:.75rem;color:#8b949e">${step.rtt_ms} ms</span>` : ''}
          ${step.aa ? `<span class="tag" style="background:#1a3a1a;color:#3fb950">AA</span>` : ''}
          <span style="font-size:.8rem;font-weight:600;color:${borderColor}">${stepLabel}</span>
        </div>
      </div>
      <div style="padding:10px 14px;background:#0d1117;font-family:monospace;font-size:.8rem">`;

    if (step.error) {
      html += `<div style="color:#f85149">שגיאה: ${step.error}</div>`;
    } else {
      if (step.answers?.length) {
        html += `<div style="color:#8b949e;margin-bottom:4px">ANSWER:</div>`;
        step.answers.forEach(a => {
          html += `<div style="color:#3fb950">  ${a.name} &nbsp; ${a.type} &nbsp; TTL=${a.ttl} &nbsp; <strong>${a.value}</strong></div>`;
        });
      }
      if (step.authority?.length) {
        html += `<div style="color:#8b949e;margin-top:6px;margin-bottom:4px">AUTHORITY:</div>`;
        step.authority.forEach(a => {
          html += `<div style="color:#58a6ff">  ${a.name} &nbsp; ${a.type} &nbsp; ${a.value}</div>`;
        });
      }
      if (step.additional?.length) {
        html += `<div style="color:#8b949e;margin-top:6px;margin-bottom:4px">ADDITIONAL (glue):</div>`;
        step.additional.forEach(a => {
          html += `<div style="color:#d29922">  ${a.name} &nbsp; ${a.type} &nbsp; <strong>${a.value}</strong></div>`;
        });
      }
    }
    html += `</div></div>`;
  });

  // Arrow-diagram summary
  html += `<div style="margin-top:16px;padding:14px;background:#161b22;border:1px solid #30363d;border-radius:8px;font-family:monospace;font-size:.82rem;color:#8b949e">
    <div style="color:#f0f6fc;font-weight:700;margin-bottom:8px">מסלול Resolution:</div>
    Client → Resolver (10.99.0.2) → Root (10.99.0.10) → TLD/lan (10.99.0.11) → Auth/exam.lan (10.99.0.12) → ${data.final_answers.map(a=>a.value).join(' / ')}
  </div>`;

  container.innerHTML = html;
}

// ── Header clock ──────────────────────────────────────────────────────────────

let serverOffset = 0;  // ms, server_now - client_now at last fetch

function updateClock() {
  const now = Date.now() + serverOffset;
  const d = new Date(now);
  document.getElementById('server-clock').textContent =
    'שרת: ' + d.toLocaleTimeString('he-IL') + '.' + String(d.getMilliseconds()).padStart(3,'0');
}
setInterval(updateClock, 100);

// ── Status bar ────────────────────────────────────────────────────────────────

let globalStatus = null;

async function fetchStatus() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    globalStatus = d;
    serverOffset = d.server_now_ms - Date.now();

    document.getElementById('s-clients').textContent   = d.total_clients;
    document.getElementById('s-connected').textContent = d.connected_count;
    document.getElementById('s-submitted').textContent = d.submitted_count;
    document.getElementById('s-t0').textContent        = d.start_at_ms ? fmtMs(d.start_at_ms) : '—';

    // Exam badge
    const badge = document.getElementById('exam-badge');
    const now = d.server_now_ms;
    if (!d.start_at_ms) {
      badge.textContent = 'ממתין לשאלון';
      badge.className = 'badge-waiting';
    } else if (now < d.start_at_ms) {
      const sec = Math.ceil((d.start_at_ms - now) / 1000);
      badge.textContent = `מתחיל בעוד ${sec}ש`;
      badge.className = 'badge-waiting';
    } else {
      badge.textContent = 'בחינה פעילה';
      badge.className = 'badge-running';
    }
  } catch(e) {
    console.error('status fetch failed', e);
  }
}

// ── Sync table ────────────────────────────────────────────────────────────────

async function loadSyncTable() {
  try {
    const r = await fetch('/api/clients');
    const clients = await r.json();
    const t0 = globalStatus?.start_at_ms;
    const tbody = document.getElementById('sync-body');
    const rows = Object.entries(clients);
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="empty">אין לקוחות מחוברים עדיין</td></tr>';
      return;
    }
    tbody.innerHTML = rows.map(([cid, c]) => {
      const opened  = c.exam_opened_at;        // when exam became visible to client (≈ T₀)
      const started = c.student_started_at;    // when student clicked "Start"
      const deltaOpen  = (t0 && opened)  ? (opened  - t0)      : null;  // Δ from T₀ to exam open
      const deltaStart = (opened && started) ? (started - opened) : null; // Δ from open to click
      const status = c.submitted
        ? '<span class="tag tag-ok">הגיש</span>'
        : started
          ? '<span class="tag tag-wait">עונה</span>'
          : opened
            ? '<span class="tag" style="background:#2d1f3d;color:#c792e9">נפתח — לא התחיל</span>'
            : '<span style="color:#8b949e">ממתין</span>';
      return `<tr>
        <td class="mono">${cid}</td>
        <td class="mono">${t0 ? fmtMs(t0) : '—'}</td>
        <td class="mono">${opened  ? fmtMs(opened)  : '—'}</td>
        <td>${fmtDelta(deltaOpen)}</td>
        <td class="mono">${started ? fmtMs(started) : '—'}</td>
        <td>${deltaStart !== null ? `<span class="mono" style="color:#8b949e">+${deltaStart} ms</span>` : '—'}</td>
        <td>${status}</td>
      </tr>`;
    }).join('');
  } catch(e) {
    console.error('sync fetch failed', e);
  }
}

// ── Monitor table ─────────────────────────────────────────────────────────────

async function loadMonitorTable() {
  try {
    const r = await fetch('/api/clients');
    const clients = await r.json();
    const tbody = document.getElementById('monitor-body');
    const rows = Object.entries(clients);
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="empty">אין לקוחות מחוברים עדיין</td></tr>';
      return;
    }
    tbody.innerHTML = rows.map(([cid, c]) => {
      const total    = c.total_questions || 5;
      const answered = c.answers_count   || 0;
      const pct      = Math.round((answered / total) * 100);
      const isDone   = pct >= 100;
      return `<tr>
        <td class="mono">${cid}</td>
        <td class="mono">${c.connected_at ? fmtMs(c.connected_at) : '—'}</td>
        <td>
          <div style="display:flex;align-items:center;gap:8px">
            <div class="prog-wrap" style="flex:1">
              <div class="prog-fill ${isDone?'done':''}" style="width:${pct}%"></div>
            </div>
            <span style="font-size:.8rem;color:#8b949e">${pct}%</span>
          </div>
        </td>
        <td>${answered} / ${total}</td>
        <td>${c.last_active ? timeSince(c.last_active) + ' ago' : '—'}</td>
        <td>${c.submitted
          ? '<span class="tag tag-ok">✓ הגיש</span>'
          : '<span class="tag tag-wait">בתהליך</span>'}</td>
      </tr>`;
    }).join('');
  } catch(e) {
    console.error('monitor fetch failed', e);
  }
}

// ── Submissions ───────────────────────────────────────────────────────────────

async function loadSubmissions() {
  const wrap = document.getElementById('submissions-wrap');
  try {
    const r = await fetch('/api/submissions');
    const data = await r.json();
    const examIds = Object.keys(data);
    if (!examIds.length) {
      wrap.innerHTML = '<p class="empty">אין הגשות עדיין</p>';
      return;
    }
    let html = '';
    for (const examId of examIds) {
      const clients = data[examId];
      html += `<h3 style="color:#f0f6fc;margin:16px 0 10px">${examId}</h3>`;
      for (const [cid, info] of Object.entries(clients)) {
        const tag = info.final
          ? '<span class="tag tag-final">סופי</span>'
          : '<span class="tag tag-draft">טיוטה</span>';
        const answers = info.answers || [];
        const answeredCount = answers.filter(a =>
          (a.type==='mcq' && a.optionId != null) ||
          (a.type==='text' && String(a.text||'').trim())
        ).length;
        html += `<div class="accordion">
          <div class="acc-head" onclick="toggleAcc(this)">
            <span>${cid} ${tag} — ${answeredCount}/${answers.length} שאלות נענו</span>
            <span class="acc-arr">▼</span>
          </div>
          <div class="acc-body">
            ${answers.map((a,i) => renderAnswer(a, i+1)).join('') || '<p class="empty">אין תשובות</p>'}
          </div>
        </div>`;
      }
    }
    wrap.innerHTML = html;
  } catch(e) {
    wrap.innerHTML = '<p class="empty" style="color:#f85149">שגיאה בטעינת הגשות</p>';
  }
}

function renderAnswer(a, num) {
  const qid = a.questionId || `ש${num}`;
  if (a.type === 'mcq') {
    return `<div class="answer-card">
      <h4>שאלה ${qid}</h4>
      <p>${a.optionId != null ? `<span class="answer-mcq">תשובה: ${a.optionId}</span>` : '<em style="color:#8b949e">לא נענתה</em>'}</p>
    </div>`;
  }
  if (a.type === 'text') {
    const txt = String(a.text || '').trim();
    return `<div class="answer-card">
      <h4>שאלה ${qid}</h4>
      <p>${txt || '<em style="color:#8b949e">לא נענתה</em>'}</p>
    </div>`;
  }
  return `<div class="answer-card"><h4>שאלה ${qid}</h4><pre>${JSON.stringify(a,null,2)}</pre></div>`;
}

function toggleAcc(head) {
  const body = head.nextElementSibling;
  const arr  = head.querySelector('.acc-arr');
  body.classList.toggle('open');
  arr.classList.toggle('open');
}

// ── Reset ─────────────────────────────────────────────────────────────────────

function openResetModal() {
  document.getElementById('reset-result').textContent = '';
  document.getElementById('reset-modal').classList.add('show');
}
function closeResetModal() {
  document.getElementById('reset-modal').classList.remove('show');
}
async function confirmReset() {
  const btn = document.querySelector('#reset-box .btn-confirm');
  btn.disabled = true;
  btn.textContent = 'מאפס...';
  const resultEl = document.getElementById('reset-result');
  try {
    const r = await fetch('/api/reset', { method: 'POST' });
    const d = await r.json();
    if (r.ok) {
      resultEl.style.color = '#3fb950';
      resultEl.textContent = '✓ אופס: ' + (d.deleted || []).join(', ');
      setTimeout(closeResetModal, 2000);
    } else {
      resultEl.style.color = '#f85149';
      resultEl.textContent = 'שגיאה: ' + JSON.stringify(d.detail || d);
    }
  } catch (e) {
    resultEl.style.color = '#f85149';
    resultEl.textContent = 'שגיאת רשת: ' + e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = 'כן, אפס הכל';
  }
}
// Close modal on backdrop click
document.addEventListener('click', e => {
  const modal = document.getElementById('reset-modal');
  if (e.target === modal) closeResetModal();
});

// ── Exam upload ───────────────────────────────────────────────────────────────

let selectedFile = null;

document.addEventListener('DOMContentLoaded', () => {
  const fi = document.getElementById('file-input');
  fi.addEventListener('change', () => {
    if (fi.files[0]) selectFile(fi.files[0]);
  });
  const dz = document.getElementById('drop-zone');
  dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('drag'); });
  dz.addEventListener('dragleave', () => dz.classList.remove('drag'));
  dz.addEventListener('drop', e => {
    e.preventDefault();
    dz.classList.remove('drag');
    if (e.dataTransfer.files[0]) selectFile(e.dataTransfer.files[0]);
  });
});

function selectFile(f) {
  selectedFile = f;
  document.getElementById('file-name').textContent = `נבחר: ${f.name} (${(f.size/1024).toFixed(1)} KB)`;
  document.getElementById('upload-btn').disabled = false;
}

async function doUpload() {
  if (!selectedFile) return;
  const fd = new FormData();
  fd.append('file', selectedFile);
  const status = document.getElementById('upload-status');
  status.style.display = 'none';
  try {
    const r = await fetch('/api/exam', { method: 'POST', body: fd });
    const d = await r.json();
    if (r.ok) {
      status.className = 'upload-status upload-ok';
      status.textContent = `✓ שאלון הועלה בהצלחה! ${d.questions} שאלות, exam_id: ${d.exam_id}`;
    } else {
      status.className = 'upload-status upload-err';
      status.textContent = `✗ שגיאה: ${d.detail || JSON.stringify(d)}`;
    }
    status.style.display = 'block';
    loadExamPreview();
  } catch(e) {
    status.className = 'upload-status upload-err';
    status.textContent = `✗ שגיאת רשת: ${e}`;
    status.style.display = 'block';
  }
}

async function loadExamPreview() {
  const pre = document.getElementById('exam-preview');
  try {
    const r = await fetch('/api/exam');
    const d = await r.json();
    pre.textContent = JSON.stringify(d, null, 2);
  } catch(e) {
    pre.textContent = 'שגיאה בטעינת השאלון';
  }
}

// ── Refresh loop ──────────────────────────────────────────────────────────────

async function refresh() {
  await fetchStatus();
  if (activeTab === 'sync')     await loadSyncTable();
  if (activeTab === 'monitor')  await loadMonitorTable();
}

refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(_HTML)
