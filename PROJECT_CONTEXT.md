# Exam Network Simulation — Complete Project Context

## 1. Project Goal

A full-stack **online exam delivery system** built from scratch as a Computer Networks final project. The system demonstrates real networking protocols (not wrappers around libraries) running in Docker containers on a shared `10.99.0.0/24` subnet:

- **Custom DNS hierarchy** (root → TLD → authoritative → recursive resolver) implementing RFC 1034/1035 from scratch
- **Custom RUDP protocol** (Reliable UDP with sliding window, congestion control, CRC-16)
- **TCP transport** for comparison (with and without synchronization)
- **DHCP** for dynamic IP assignment
- **Synchronized exam delivery** algorithm ensuring all students receive the exam at the same millisecond regardless of network conditions

The central innovation is the **synchronized delivery algorithm**: a two-layer timing system (server-side staggered sends + bridge self-correcting hold) that compensates for per-client RTT differences so every browser displays the exam at the same wall-clock instant.

Three transport modes let the professor demonstrate the difference synchronization makes:

| Mode | Transport | Sync | WS Route | Server |
|------|-----------|------|----------|--------|
| RUDP + Sync | Custom RUDP | Yes (staggered + bridge hold) | `/ws` | 10.99.0.20-22 |
| TCP + Sync | Plain TCP | Yes (staggered + bridge hold) | `/ws-tcp-sync` | 10.99.0.23 |
| TCP + No Sync | Plain TCP | No (immediate send) | `/ws-tcp-nosync` | 10.99.0.24 |

---

## 2. Network Architecture

All containers run on a single Docker bridge network `exam-net` (`10.99.0.0/24`).

| IP Address | Container | Role |
|------------|-----------|------|
| 10.99.0.1 | Host/Gateway | NAT gateway, default route for clients |
| 10.99.0.2 | dns-resolver | Recursive DNS resolver (what DHCP tells clients) |
| 10.99.0.3 | dhcp-server | DHCP server for client IP assignment |
| 10.99.0.10 | dns-root | Simulated ICANN root nameserver |
| 10.99.0.11 | dns-tld | .lan TLD nameserver |
| 10.99.0.12 | dns-auth | exam.lan authoritative nameserver |
| 10.99.0.20 | rudp-server-1 | RUDP exam server instance 1 |
| 10.99.0.21 | rudp-server-2 | RUDP exam server instance 2 |
| 10.99.0.22 | rudp-server-3 | RUDP exam server instance 3 |
| 10.99.0.23 | tcp-server-sync | TCP exam server (SYNC_ENABLED=1) |
| 10.99.0.24 | tcp-server-nosync | TCP exam server (SYNC_ENABLED=0) |
| 10.99.0.30 | admin | Admin panel (FastAPI, port 9999) |
| 10.99.0.100+ | client-1,2,3 | Exam client containers (DHCP pool) |

### Docker Compose Services

The `docker-compose.yml` defines:
- **dhcp-server**: builds from `DHCP/Dockerfile.server`, static IP 10.99.0.3
- **dns-root, dns-tld, dns-auth, dns-resolver**: all build from `DNS/Dockerfile` with `DNS_ROLE` env var selecting which server to run. Single image, four containers.
- **rudp-server-1/2/3**: build from `RUDP/Dockerfile.server`, share `exam-shared` + `answers-data` volumes
- **tcp-server-sync, tcp-server-nosync**: build from `TCP/Dockerfile.server`, share same volumes
- **admin**: builds from `admin/Dockerfile`, mounts both shared volumes, exposes port 9999
- **client-1/2/3**: build from `DHCP/Dockerfile.client` (multi-stage: Node build + Python runtime). Each has fixed `CONTAINER_MAC`, `RUDP_SERVER_HOST=server1/2/3.exam.lan`, NET_ADMIN capability, DNS set to 10.99.0.2. Ports 8081-8083 exposed.
- **capture-* sidecars**: optional packet capture containers (activated with `--profile capture`), use `tcpdump` to write `.pcap` files

### Shared Volumes
- `exam-shared`: `/app/shared/` — contains `start_at.txt` (shared exam start time), `clients.json` (client registry), `exam.json` (uploaded exam)
- `answers-data`: `/app/answers/` — per-client answer files
- `dhcp-leases`: DHCP lease database

---

## 3. DNS Hierarchy (RFC 1034/1035)

### 3.1 dns_packet.py — RFC 1035 Codec

Pure-stdlib DNS message parser and builder. No external dependencies.

**Data structures:**
```python
@dataclass
class DnsHeader:
    id: int          # 16-bit transaction ID
    flags: int       # 16-bit flags: QR(1) Opcode(4) AA(1) TC(1) RD(1) RA(1) Z(3) RCODE(4)
    qdcount: int     # question count
    ancount: int     # answer RR count
    nscount: int     # authority RR count
    arcount: int     # additional RR count

@dataclass
class DnsQuestion:
    qname: str       # e.g. "server.exam.lan"
    qtype: int       # TYPE_A=1, TYPE_NS=2, etc.
    qclass: int      # CLASS_IN=1

@dataclass
class DnsRR:
    name: str
    rtype: int
    rclass: int
    ttl: int
    rdata: bytes     # type-specific binary data
```

**Name encoding/decoding:**
- `encode_name("exam.lan")` → `b'\x04exam\x03lan\x00'` (length-prefixed labels + null terminator)
- `decode_name(buf, offset)` → handles pointer compression (0xC0 prefix, 14-bit offset)

**Record types:** A(1), NS(2), CNAME(5), SOA(6), PTR(12), MX(15), TXT(16), AAAA(28)

**RDATA helpers:**
- `rdata_a("10.99.0.20")` → 4 bytes
- `rdata_ns("ns1.exam.lan")` → encoded name
- `rdata_soa(mname, rname, serial, refresh, retry, expire, minimum)` → SOA record
- `rdata_ptr("server.exam.lan")` → encoded PTR target
- `rdata_cname`, `rdata_mx`, `rdata_txt`, `rdata_aaaa`

**Key functions:**
- `parse_query(data)` → `(DnsHeader, [DnsQuestion])`
- `parse_response(data)` → `(DnsHeader, questions, answers, authority, additional)`
- `build_response(req_hdr, questions, answers, authority, additional, aa, ra, rcode)` → bytes

### 3.2 dns_server.py — Base Server

Abstract base class `DnsServer`:
- Binds UDP port 53 (single socket) + TCP listener (2-byte length prefix per RFC 1035 section 4.2.2)
- UDP responses >512 bytes get TC=1 (truncated), client retries over TCP
- Subclasses implement `handle_query(questions, is_rd) → (answers, authority, additional, aa)`
- Threading: each UDP request and TCP connection handled in a daemon thread

### 3.3 root_server.py — Root Nameserver (10.99.0.10)

Simulates ICANN root servers. Hardcoded delegation:
```
.lan → NS ns1.lan (10.99.0.11)   [glue A record included]
```
- Returns `REFERRAL` (NS + glue in authority/additional) for `*.lan.` queries
- Returns `NXDOMAIN` for unknown TLDs
- `AA=0` (not authoritative for delegated zones)
- **Recursive-capable mode**: when `RECURSIVE_CAPABLE=1` (env) or `/tmp/dns_recursive.json {"enabled": true}`, handles RD=1 by walking TLD → auth itself. Togglable at runtime by prof dashboard.

### 3.4 tld_server.py — TLD Nameserver (10.99.0.11)

Authoritative for `.lan` TLD. Delegation:
```
exam.lan → NS ns1.exam.lan (10.99.0.12)   [glue A record]
```
- Returns `REFERRAL` for `*.exam.lan.` queries
- Returns `NXDOMAIN` with SOA in authority for unknown second-level domains
- `AA=0`

### 3.5 auth_server.py — Authoritative Nameserver (10.99.0.12)

Authoritative for `exam.lan` zone. `AA=1`. Zone records:
```
@         SOA  ns1.exam.lan hostmaster.exam.lan (serial=2024010101)
@         NS   ns1.exam.lan
ns1       A    10.99.0.12
dhcp      A    10.99.0.3
dns       A    10.99.0.2
gw        A    10.99.0.1
server    A    10.99.0.20    ← round-robin (3 A records)
server    A    10.99.0.21
server    A    10.99.0.22
server1   A    10.99.0.20    ← individual per-client mappings
server2   A    10.99.0.21
server3   A    10.99.0.22
tcp-sync  A    10.99.0.23
tcp-nosync A   10.99.0.24
```

Reverse PTR map for `10.99.0.X.in-addr.arpa`:
```
1  → gw.exam.lan
2  → dns.exam.lan
3  → dhcp.exam.lan
12 → ns1.exam.lan
20 → server.exam.lan
21 → server.exam.lan
22 → server.exam.lan
23 → tcp-sync.exam.lan
24 → tcp-nosync.exam.lan
```

### 3.6 resolver_server.py — Recursive Resolver (10.99.0.2)

This is what DHCP configures clients to use. Implements **iterative resolution**:

1. Check TTL-aware cache `{(name_lower, qtype): (rrs, expires_at)}`
2. Start at root (10.99.0.10:53) with RD=0
3. Follow NS delegations (use glue A records from additional section)
4. If no glue, recursively resolve the NS name
5. Cache all results by TTL
6. Handle CNAME chains (up to depth 8)
7. TCP fallback when response has TC=1 (truncated)

**Resolution modes** (switchable at runtime via `/tmp/dns_mode.json`):
- `iterative` (default): resolver walks root → TLD → auth itself
- `recursive`: resolver sends RD=1 to root; root does the work

**DNS Resolution Trace for `server.exam.lan`:**
```
Client → resolver(10.99.0.2):     Q server.exam.lan A?
Resolver → root(10.99.0.10):      Q server.exam.lan A?
  Root replies: REFERRAL  lan. NS ns1.lan. + glue A 10.99.0.11
Resolver → tld(10.99.0.11):       Q server.exam.lan A?
  TLD replies:  REFERRAL  exam.lan. NS ns1.exam.lan. + glue A 10.99.0.12
Resolver → auth(10.99.0.12):      Q server.exam.lan A?
  Auth replies: ANSWER    server.exam.lan A 10.99.0.20
                          server.exam.lan A 10.99.0.21
                          server.exam.lan A 10.99.0.22  (AA=1)
Resolver caches + returns all 3 to client
OS picks one (round-robin) → ws_bridge connects to that server
```

### 3.7 DNS Dockerfile

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY DNS/ .
ENV DNS_ROLE=resolver   # root | tld | auth | resolver
CMD ["sh", "-c", "python3 ${DNS_ROLE}_server.py"]
```

One image, four containers. Build context is the project root (`.`), dockerfile path is `DNS/Dockerfile`.

---

## 4. RUDP Protocol (Custom Reliable UDP)

### 4.1 rudp_packet.py — Packet Codec

14-byte fixed header + variable payload:

```
Offset  Size  Field
0       4     seq      (uint32, big-endian)
4       4     ack      (uint32, big-endian)
8       1     flags    (uint8)
9       1     window   (uint8, max 255, used as 8)
10      2     len      (uint16, payload length)
12      2     checksum (uint16, CRC-16/CCITT-FALSE over header[0:12]+payload)
14      len   payload
```

**Flag constants:**
```
SYN     = 0x01   — connection initiation
ACK     = 0x02   — acknowledgment
FIN     = 0x04   — connection teardown
DATA    = 0x08   — carries payload data
RST     = 0x10   — reset/abort
PING    = 0x20   — keepalive
MSG_END = 0x40   — last chunk of application message (enables reassembly)
```

**CRC-16/CCITT-FALSE:**
- Polynomial: 0x1021
- Initial value: 0xFFFF
- No reflection (refin=False, refout=False)
- Computed over header bytes [0:12] + payload (excludes the checksum field itself)

**Serialization:**
```python
@dataclass
class RudpPacket:
    seq: int = 0
    ack: int = 0
    flags: int = 0
    window: int = 8
    payload: bytes = b""

    def to_bytes(self) -> bytes:
        hdr_no_crc = struct.pack('!IIBBH', seq, ack, flags, window, len(payload))
        crc = _crc16(hdr_no_crc + payload)
        return hdr_no_crc + struct.pack('!H', crc) + payload

    @staticmethod
    def from_bytes(data: bytes) -> RudpPacket:
        # Parse header, verify CRC, return packet
```

### 4.2 rudp_socket.py — Connection Manager

`RudpSocket` — asyncio-based bidirectional RUDP connection.

**Reliability features:**
- **Sliding window**: tracks unACK'd packets in `send_buf: OrderedDict[seq, (pkt, t_sent, retry_count)]`
- **Cumulative ACK**: receiver sends ACK with highest contiguous seq received
- **Retransmission**: timeout 500ms, max 5 retries → `RudpTimeout`
- **Reorder buffer**: out-of-order packets buffered in `recv_ooo`, delivered in-sequence
- **Duplicate detection**: already-delivered seqs are discarded
- **Message framing**: MSG_END flag marks last chunk; `recv()` reassembles all chunks into complete message

**Congestion control (TCP Reno inspired):**
- **Slow start**: `cwnd` starts at 1, grows by 1 per ACKed packet (doubles each RTT) until `ssthresh`
- **Congestion avoidance (AIMD)**: once `cwnd >= ssthresh`, `cwnd += 1/cwnd` per ACK (+1 MSS per RTT)
- **Timeout**: `ssthresh = max(cwnd/2, 1)`, `cwnd = 1`. Go-Back-N retransmit all in-flight.
- **Fast retransmit / fast recovery**: on 3 duplicate ACKs, retransmit first missing packet immediately. `ssthresh = max(cwnd/2, 1)`, `cwnd = ssthresh` (skip slow start, enter CA directly).

**Flow control:**
- `window` field (uint8) advertises remaining receive-buffer space
- Effective window = `min(cwnd, peer_advertised_window, WINDOW_CAP=32)`
- Sender stalls when peer advertises `window=0`

**MSS negotiation:**
- SYN payload contains 2-byte big-endian `MAX_PAYLOAD` (default 1200)
- SYN-ACK payload contains server's MAX_PAYLOAD
- Agreed MSS = `min(local, remote)`

**Connection lifecycle:**
```
connect():  send SYN(+MSS) → wait SYN+ACK(+MSS) → send ACK → connected
accept():   wait SYN → send SYN+ACK → (client sends ACK) → accepted
close():    send FIN+ACK → done
```

**RTT tracking:** EWMA with alpha=0.125: `rtt_ms = 0.875 * rtt_ms + 0.125 * sample_ms`

**Key tunables:**
```python
MAX_PAYLOAD     = 1200   # bytes per chunk (below IP MTU)
RECV_BUF_SIZE   = 64     # advertised receive window (packets)
WINDOW_CAP      = 32     # hard upper bound on cwnd
RETRANSMIT_MS   = 500    # retransmission timeout
MAX_RETRIES     = 5      # max retransmissions
CONNECT_TIMEOUT = 5.0    # handshake timeout (seconds)
DUP_ACK_THRESH  = 3      # duplicate ACKs for fast retransmit
INIT_CWND       = 1.0    # initial congestion window
INIT_SSTHRESH   = 32.0   # initial slow-start threshold
```

**RudpServer:** UDP listener that produces `RudpSocket` connections via `accept()`. Uses `_UdpProtocol` (asyncio.DatagramProtocol) to demux packets by remote address.

**Client-side connect:** `rudp_connect(host, port)` → creates UDP endpoint, sends SYN with MSS, waits for SYN+ACK, returns connected `RudpSocket`.

---

## 5. Exam Server Protocol (JSON over RUDP or TCP)

Both `RUDP/exam_server.py` and `TCP/exam_server_tcp.py` implement the same JSON protocol:

### 5.1 Message Flow

```
Browser → WebSocket → Bridge → RUDP/TCP → Exam Server

1. hello          → hello_ack
2. time_req ×12   → time_resp (NTP-style clock sync)
3. schedule_req   → schedule_resp (exam_id, start_at_server_ms)
4. [wait until T₀]
5. exam_req       → exam_resp (full exam payload)
6. answers_save   → answers_saved (auto-save draft)
7. submit_req     → submit_resp (final submission)
```

### 5.2 Message Types

**hello** (client → server):
```json
{"type": "hello", "client_id": "student-A", "rtt_ms": 5.2}
```
Bridge injects `rtt_ms` (RUDP handshake RTT) into the hello message.

**hello_ack** (server → client):
```json
{"type": "hello_ack", "client_id": "student-A"}
```

**time_req** (client → server, sent 12 times for NTP-style sync):
```json
{"type": "time_req", "req_id": "abc123", "client_t0_ms": 1709000000000}
```

**time_resp** (server → client):
```json
{"type": "time_resp", "req_id": "abc123", "server_now_ms": 1709000000005}
```

Client computes: `rtt = t1 - t0`, `offset = server_now_ms - (t0 + rtt/2)`
Best sample (lowest RTT) used as final offset.

**schedule_req** (client → server):
```json
{
  "type": "schedule_req", "req_id": "...",
  "rtt_samples": [4.2, 5.1, 6.8],   // [min, med, max] from 12 NTP rounds
  "offset_ms": 2.3                    // NTP clock offset
}
```

**schedule_resp** (server → client):
```json
{
  "type": "schedule_resp", "req_id": "...",
  "exam_id": "exam-2024-01",
  "start_at_server_ms": 1709000120000,
  "duration_sec": 3600,
  "deliver_delay_ms": 3, "max_rtt_ms": 6.8, "my_rtt_ms": 5.1,
  "D_i": 3.2, "relay_candidate": "student-B"
}
```

**exam_req** (client → server, sent at T₀ - 500ms):
```json
{
  "type": "exam_req", "req_id": "...",
  "exam_id": "exam-2024-01",
  "rtt_samples": [4.0, 4.8, 5.5],   // fresh RTT from pre-exam re-sync
  "offset_ms": 2.1
}
```

**exam_resp** (server → client):
Contains full exam JSON with all questions, plus sync metadata:
```json
{
  "type": "exam_resp", "req_id": "...",
  "exam": { "exam_id": "...", "title": "...", "questions": [...] },
  "server_sent_at_ms": 1709000119995,
  "deliver_delay_ms": 2, "max_rtt_ms": 6.8,
  "D_i": 3.2, "T_send_planned_ms": 1709000119992.5,
  "stagger_wait_ms": 1.5
}
```

**RUDP wire format for exam_resp** ("ZS" — ZLib Split):
The exam JSON is large (~33KB). To minimize blocking after the stagger sleep:
```
Pre-computation (during 1.5s collection window):
  exam_json_str = json.dumps(exam)          # ~27 ms
  exam_zl = zlib.compress(exam_json_str)    # ~5 ms

Wire format (after stagger sleep):
  b'ZS' | uint16-BE(len(meta_bytes)) | meta_bytes | exam_zl
```
Bridge splits on length prefix, decompresses exam_zl, merges into one JSON object.
This ensures `server_sent_at_ms` is stamped with <1ms blocking.

**answers_save** / **submit_req**: Saves answers to `ANSWERS_DIR/<exam_id>/<client_id>.json` (draft) or `.final.json` (submission).

### 5.3 Exam Content

Default exam is hardcoded (Hebrew): 3 MCQ questions (IP/TCP/IPv4 addressing) + 2 text questions.
Custom exams can be uploaded via admin panel (`POST /api/exam`) → saved to `/app/shared/exam.json`.

### 5.4 Shared Start Time

All server instances share the same start time via `/app/shared/start_at.txt` (Docker named volume `exam-shared`):
- First `schedule_req` triggers: `start_at_ms = now_ms + START_OFFSET_SEC * 1000` (default 120 seconds)
- Saved to `start_at.txt`, all other instances read from it
- `ExamSendCoordinator.reset()` clears the cached value so fresh `start_at.txt` can be picked up

### 5.5 Client Tracking

All servers write to `/app/shared/clients.json`:
```json
{
  "student-A": {
    "connected_at": 1709000000000,
    "rtt_ms": 5.2,
    "transport": "rudp-sync",
    "submitted": false,
    "answers_count": 3,
    "exam_opened_at": 1709000120000,
    "student_started_at": 1709000120500,
    "total_questions": 5
  }
}
```

---

## 6. Synchronized Delivery Algorithm

### 6.1 Overview

The synchronization algorithm ensures all exam clients receive the exam at the same wall-clock millisecond, regardless of their individual RTTs.

Two complementary layers:
- **Layer A (Server-side):** ExamSendCoordinator computes staggered send times
- **Layer B (Bridge-side):** Self-correcting hold absorbs any residual jitter

### 6.2 RTT Collection

Each client performs 12 NTP-style rounds (`time_req`/`time_resp`). From these, 3 representative values are computed:
```
rtt_samples = [rtt_min, rtt_med, rtt_max]
offset_ms   = best_offset (from lowest-RTT sample)
```

These are sent twice:
1. In `schedule_req` (initial measurement)
2. In `exam_req` (fresh re-measurement 500ms before T₀)

### 6.3 D_i Computation — Per-Client One-Way Delay

For each client i:
```
s       = sorted([rtt_min, rtt_med, rtt_max])
RTT_med = s[1]                    # median RTT
J_rtt   = s[2] - s[0]            # jitter range
```

**With NTP offset (asymmetric correction):**
```
d_down_i = RTT_med / 2 - offset_i     # estimated downlink delay
D_i      = max(1, d_down_i + J_rtt / 2)
```
This corrects for asymmetric up/down paths using the measured clock offset.

**Without offset (symmetric assumption):**
```
D_i = (RTT_med + J_rtt) / 2
```

**Fallback (no samples):**
```
D_i = rtt_ms / 2
```

Higher D_i → client has slower/more jittery connection → packet sent earlier.

### 6.4 ExamSendCoordinator

Collects D_i values from all `exam_req` handlers during a `COLLECTION_WINDOW_SEC = 1.5s` window.

After the window closes:
```
max_D = max(D_i for all clients)
N     = number of clients

T_arr = start_at_ms              # anchored to shared start time

If T_arr < now + max_D + M_MARGIN_MS:
    T_arr = now + max_D + N*C_SEND_MS + M_MARGIN_MS    # dynamic fallback
```

Per-client send time:
```
T_send[i] = T_arr - D_i
```

**Constants:**
- `C_SEND_MS = 1.0` — per-send overhead estimate
- `M_MARGIN_MS = 15.0` — scheduling safety margin
- `JITTER_BUFFER_MS = 5` — bridge-side jitter buffer

**Send ordering:**
Clients sorted by ascending T_send (= descending D_i):
- Slowest client sent first, fastest client sent last
- Inter-send gap: `delta_k = max(0, T_send[k+1] - T_send[k] - C)`

### 6.5 Bridge Self-Correcting Hold (Layer B)

When the bridge receives a sync-eligible packet (`exam_resp` or `schedule_resp`) with `server_sent_at_ms`:

```
target_ms  = server_sent_at_ms + max_rtt/2 + JITTER_BUFFER_MS
hold_ms    = max(0, target_ms - time.now())
```

**Self-correction property:** If RUDP retransmission delayed the packet by delta_ms, `time.now()` has already advanced by delta_ms, so `hold_ms` shrinks by exactly delta_ms. All clients still converge to `target_ms`.

Bridge stamps the forwarded packet with:
- `bridge_forwarded_at_ms` — when bridge released to browser
- `sync_hold_ms` — how long bridge held
- `sync_target_ms` — the target delivery time

### 6.6 Cross-Instance Consistency

All 3 RUDP servers share `/app/shared/start_at.txt`. Anchoring `T_arr = start_at_ms` (rather than `now + max_D + ...`) ensures every server instance converges to the same delivery moment, regardless of when each server's `COLLECTION_WINDOW_SEC` fires.

### 6.7 Expected Precision

```
target_ms ± 5 ms
```

All clients receive the exam within a few milliseconds of each other, independent of individual RTT.

---

## 7. WebSocket Bridges

### 7.1 RUDP Bridge (ws_bridge.py, port 8081)

Runs inside each client container. Local WebSocket server that proxies to RUDP exam backend.

```
Browser → ws://container/ws → nginx → :8081 → ws_bridge → RUDP → exam_server
```

**Connection flow:**
1. Browser connects WebSocket
2. Bridge resolves `RUDP_SERVER_HOST` (e.g., `server1.exam.lan`) via DNS (10.99.0.2)
3. Bridge opens RUDP connection to resolved IP:9000
4. Bridge sends `connection_info` to browser (resolved_ip, handshake_rtt_ms, all_ips)
5. Two coroutines: `_ws_to_rudp` (forwards browser→server, injects rtt_ms into hello) and `_rudp_to_ws` (forwards server→browser with sync delivery)

**Server frame decoding:**
- `b'ZS'` — split format: `uint16-BE(meta_len) | meta_json | exam_zl`
- `b'ZL'` — full-frame zlib compression (legacy)
- else — plain UTF-8 JSON

**DNS resolution caching:** Once resolved, the IP is cached for all subsequent WebSocket sessions through this bridge. Critical because ExamSendCoordinator must see every client on the same server instance.

### 7.2 TCP Bridge — Sync (ws_bridge_tcp.py, port 8082)

Same sync logic as RUDP bridge but over TCP:
```
Browser → ws://container/ws-tcp-sync → nginx → :8082 → ws_bridge_tcp → TCP → tcp-server-sync
```

TCP framing: newline-delimited JSON (`writer.write(json_bytes + b'\n')`)

### 7.3 TCP Bridge — NoSync (ws_bridge_tcp.py, port 8083)

Forward immediately, no delay logic:
```
Browser → ws://container/ws-tcp-nosync → nginx → :8083 → ws_bridge_tcp → TCP → tcp-server-nosync
```

When `SYNC_ENABLED=0`, bridge skips netem and sync hold — packets forwarded immediately.

### 7.4 Software Netem

Both bridges support software network emulation via `/tmp/netem_delay.json`:
```json
{"delay_ms": 50, "jitter_ms": 10, "loss_pct": 2.0}
```
Written by the professor dashboard. Bridge reads on each packet:
- `delay_ms + uniform(-jitter_ms, jitter_ms)` applied as sleep
- `loss_pct > 0` → random drop simulation
No kernel module needed.

---

## 8. Client Container Architecture

### 8.1 Dockerfile.client (Multi-stage)

**Stage 1: Frontend build**
```dockerfile
FROM node:20-alpine AS frontend-builder
WORKDIR /frontend
COPY app/frontend/package*.json ./
RUN npm ci
COPY app/frontend/ ./
RUN npm run build
```

**Stage 2: Runtime**
```dockerfile
FROM python:3.11-slim
RUN apt-get install -y iproute2 nginx supervisor
COPY DHCP/dhcp_client.py RUDP/rudp_packet.py RUDP/rudp_socket.py RUDP/ws_bridge.py TCP/ws_bridge_tcp.py .
COPY DHCP/entrypoint.sh DHCP/nginx.conf DHCP/supervisord.conf
COPY --from=frontend-builder /frontend/dist /usr/share/nginx/html
ENTRYPOINT ["/entrypoint.sh"]
```

### 8.2 entrypoint.sh

1. Runs DHCP client with 3 retries and back-off
2. Adds leased IP to eth0 (guarded: skips if already configured)
3. Sets default route via 10.99.0.1 with `src $LEASED_IP` so packets show DHCP address
4. Starts supervisord (manages nginx + all bridges)

### 8.3 supervisord.conf

Manages 4 processes:
- `nginx` — serves React frontend + proxies WebSocket routes
- `bridge` — RUDP bridge on port 8081
- `bridge-tcp-sync` — TCP sync bridge on port 8082
- `bridge-tcp-nosync` — TCP nosync bridge on port 8083

### 8.4 nginx.conf

```nginx
server {
    listen 80;
    root /usr/share/nginx/html;

    location /ws           { proxy_pass http://localhost:8081; ... WebSocket upgrade }
    location /ws-tcp-sync  { proxy_pass http://localhost:8082; ... }
    location /ws-tcp-nosync{ proxy_pass http://localhost:8083; ... }
    location /             { try_files $uri $uri/ /index.html; }
}
```

---

## 9. Frontend (React/TypeScript)

### 9.1 Architecture

Single-file React app (`app/frontend/src/App.tsx`). Built with Vite.

**Types:**
```typescript
type Phase = "boot" | "connecting" | "syncing" | "waiting" | "open" | "running" | "submitted" | "error";
type TransportMode = 'rudp-sync' | 'tcp-sync' | 'tcp-nosync';
type Question = MCQ | TextQuestion;
type Answer = MCQ answer | Text answer;
type SyncSample = { rtt: number; offset: number; at: number };
```

**WS URL mapping:**
```typescript
const WS_PATHS: Record<TransportMode, string> = {
  'rudp-sync':  '/ws',
  'tcp-sync':   '/ws-tcp-sync',
  'tcp-nosync': '/ws-tcp-nosync',
};
```

URL param `?mode=rudp-sync|tcp-sync|tcp-nosync` selects initial mode.

### 9.2 Phase Flow

```
boot → ModeSelector (3 buttons with descriptions)
  → connecting (WebSocket connect + connection_info)
  → syncing (12 NTP rounds, adaptive convergence check)
  → waiting (countdown to T₀, re-syncs 5s before start)
  → open (exam displayed, "Start" button)
  → running (answering questions, auto-save every 30s, timer)
  → submitted (confirmation)
```

### 9.3 Clock Synchronization (3-Phase Adaptive)

Uses `performance.timeOrigin + performance.now()` for high-resolution timestamps.

**Helper functions (App.tsx:89-101):**
- `syncStdev(samples, w=5)` — standard deviation of last w offset samples
- `syncHasConverged(samples)` — true when `stdev < 1.5ms` AND `length >= 5`
- `buildRttSamples(samples)` — extracts `[min, median, max]` RTT from sorted samples

**Single measurement — `doTimeReq()` (App.tsx:311-320):**
```
t0 = nowMs()
send time_req {client_t0_ms: t0}
receive time_resp {server_now_ms}
t1 = nowMs()
rtt = t1 - t0
offset = server_now_ms - (t0 + rtt/2)
```

**Phase 1 — Quick Bootstrap (App.tsx:345-353):**
3 rounds at 50ms cadence. Enough to send `schedule_req` and get the exam start time.

**`schedule_req` (App.tsx:355-365):**
Sends `rtt_samples` (min/med/max) + `offset_ms` (from lowest-RTT sample) to server.

**Phase 2 — Adaptive Refinement (App.tsx:381-398):**

One sample every 200ms. Three stop conditions:

1. `syncHasConverged()` — stdev < 1.5ms over last 5 samples
2. `msToT0 < 8000` — fewer than 8 seconds remain to T₀
3. `allSamples.length >= 20` — 20 total samples gathered

**Phase 3 — Fresh Samples at T₀−5s (App.tsx:400-418):**
3 rounds at 100ms cadence. These are the most accurate measurements, sent with `exam_req` so the coordinator uses the freshest D_i.

**`exam_req` with fresh data (App.tsx:427-441):**
Sends Phase 3 `rtt_samples` + `offset_ms` for final D_i computation.

Quality assessment: `stdev < 1ms` → "good", `< 3ms` → "medium", else "weak"

### 9.4 SyncProofCard

Displays synchronization proof when exam is delivered:
- `server_sent_at_ms` — when server sent the packet
- `bridge_forwarded_at_ms` — when bridge released to browser
- `sync_hold_ms` — how long bridge held
- `sync_target_ms` — target delivery time
- Delta from target = `bridge_forwarded_at_ms - sync_target_ms`

For `tcp-nosync` mode: shows "UNCOORDINATED — baseline mode" when sync fields are absent.

Mode badge shows current transport mode in the card.

### 9.5 Exam UI

RTL (right-to-left) Hebrew interface:
- Question navigator sidebar with completion indicators
- MCQ: radio-button style options with selection highlighting
- Text: auto-sizing textarea
- Timer: countdown with red pulsing at <5 minutes
- Auto-save: saves answers every 30 seconds via `answers_save`
- Submit: sends `submit_req`, shows confirmation

---

## 10. Admin Panel (admin_server.py, port 9999)

FastAPI server with inline HTML dashboard (Hebrew RTL).

**API Endpoints:**
- `GET /api/status` — start_at_ms, server_now_ms, connected/submitted counts
- `GET /api/clients` — full clients.json
- `GET /api/submissions` — all saved/submitted answers
- `GET /api/exam` — current exam JSON
- `POST /api/exam` — upload new exam JSON
- `POST /api/reset` — wipe clients.json, start_at.txt, all answers
- `GET /api/dns/trace?name=server.exam.lan` — iterative DNS resolution trace

**Dashboard Tabs:**
1. **Sync Proof** — table showing per-client T₀ delta, opened/started times
2. **Live Monitor** — real-time progress bars, answers count, submission status
3. **Submissions** — expandable accordion per client, final/draft badges
4. **DNS Trace** — interactive DNS resolution visualization (root→TLD→auth)
5. **Upload** — drag-and-drop exam JSON upload

**DNS Trace Implementation:** Admin has its own minimal DNS client that queries servers directly (RD=0), following referrals manually. Shows each hop with RTT, AA flag, result type.

---

## 11. Professor Dashboard (prof_dashboard.py, port 7777)

FastAPI + inline HTML. Runs on the host machine (not in Docker).

**Features:**
- Real-time container status (12 services via `docker inspect`)
- Per-client network interference: latency/jitter/packet-loss sliders (writes `/tmp/netem_delay.json` into containers via `docker exec`)
- "Open exam client" buttons — opens browser tabs
- Live 3-client simulation with streaming SSE progress events
- DNS iterative resolution trace visualization
- Synchronized delivery timing-proof table + RTT chart
- Transport comparison: groups clients by transport mode, shows spread_ms per mode
- Links to admin panel (port 9999)

**Network Presets:** "Clean", "Moderate", "Bad", "Extreme" — pre-configured delay/jitter/loss values.

**DNS Controls:** Switch resolver between iterative/recursive mode, toggle root recursive-capable flag.

---

## 12. TCP Transport Extension

### 12.1 exam_server_tcp.py

Same JSON protocol as RUDP exam server but over TCP (asyncio `StreamReader`/`StreamWriter`).

**Framing:** newline-delimited JSON (`readline()` on recv, `json + \n` on send).

**SYNC_ENABLED=1:** Full ExamSendCoordinator + D_i computation + staggered sends + `server_sent_at_ms` stamp.
**SYNC_ENABLED=0:** Immediate send, no coordinator, no `server_sent_at_ms` (bridge/frontend see baseline).

Both modes: `_send()` always adds `server_sent_at_ms` when `SYNC_ENABLED=1`.

### 12.2 TCP Dockerfile

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY TCP/ .
CMD ["python3", "exam_server_tcp.py"]
```
No pip deps (stdlib asyncio only).

---

## 13. DHCP System

### 13.1 DHCP Server (`DHCP/Dockerfile.server`)

Assigns IPs from pool `10.99.0.100-149`. Stores leases in `/app/leases.json`.

### 13.2 DHCP Client (`DHCP/dhcp_client.py`)

Runs at container startup. Outputs leased IP to stdout.
Container MAC is fixed via `CONTAINER_MAC` env var to prevent pool exhaustion on container recreation.

---

## 14. Scenario Runner (run_scenarios.py)

Automated test runner that executes exam scenarios and captures pcap files.

**10 Scenarios:**
1. Normal exam flow (3 clients)
2. DNS resolution trace
3. RUDP handshake + connection
4. NTP time synchronization
5. Mid-exam disconnect and reconnect
6. RUDP sliding window + congestion control
7. Network interference (netem delay/jitter)
8. TCP sync vs nosync comparison
9. DNS mode switching (iterative → recursive)
10. Full exam with all transport modes

**Client Simulator (`scenarios/client_sim.py`):**
Headless WebSocket client that speaks the full exam protocol:
- `--ws ws://localhost:8081/ws` — WebSocket URL
- `--id student-A` — client identifier
- `--disconnect-after hello|sync|schedule|exam_req|answers_save|submit` — stage disconnect
- `--think-ms 2000` — simulated reading time between exam receipt and answer submission

**Pcap capture:** Uses Docker capture sidecars with tcpdump, copies `.pcap` files to `captures/` directory.

---

## 15. Packet Capture Infrastructure

### 15.1 Capture Sidecar

`capture/Dockerfile`:
```dockerfile
FROM alpine
RUN apk add --no-cache tcpdump
ENTRYPOINT ["tcpdump", "-i", "any", "-w"]
```

Activated with `docker compose --profile capture up`. Each sidecar shares network namespace with its target service via `network_mode: "service:..."`.

### 15.2 capture_network.py

Standalone capture script. Uses `docker exec` to run tcpdump inside containers, copies pcap files out.

---

## 16. File Structure Summary

```
FinalProject-ComputerNetworks/
├── DNS/
│   ├── Dockerfile              # Single DNS image (DNS_ROLE env)
│   ├── dns_packet.py           # RFC 1035 codec (pure stdlib)
│   ├── dns_server.py           # Base UDP+TCP DnsServer
│   ├── root_server.py          # Root nameserver (10.99.0.10)
│   ├── tld_server.py           # .lan TLD (10.99.0.11)
│   ├── auth_server.py          # exam.lan authoritative (10.99.0.12)
│   └── resolver_server.py      # Recursive resolver (10.99.0.2)
├── RUDP/
│   ├── Dockerfile.server       # RUDP exam server image
│   ├── requirements.txt        # websockets>=12.0
│   ├── rudp_packet.py          # 14-byte header + CRC-16
│   ├── rudp_socket.py          # asyncio RudpSocket + RudpServer
│   ├── exam_server.py          # Exam backend (ExamSendCoordinator)
│   └── ws_bridge.py            # WebSocket↔RUDP bridge (port 8081)
├── TCP/
│   ├── Dockerfile.server       # TCP exam server image
│   ├── requirements.txt        # websockets>=12.0
│   ├── exam_server_tcp.py      # TCP exam server (NL-JSON, SYNC_ENABLED)
│   └── ws_bridge_tcp.py        # WS↔TCP bridge (port 8082/8083)
├── DHCP/
│   ├── Dockerfile.server       # DHCP server image
│   ├── Dockerfile.client       # Multi-stage client image
│   ├── dhcp_client.py          # DHCP client
│   ├── dhcp_server.py          # DHCP server
│   ├── requirements.txt        # DHCP dependencies
│   ├── entrypoint.sh           # Container startup script
│   ├── nginx.conf              # /ws, /ws-tcp-sync, /ws-tcp-nosync proxy
│   └── supervisord.conf        # nginx + 3 bridges
├── admin/
│   ├── Dockerfile              # Admin panel image
│   └── admin_server.py         # FastAPI admin (port 9999)
├── app/
│   └── frontend/
│       ├── src/App.tsx          # React exam UI (single file)
│       ├── package.json
│       └── vite.config.ts
├── capture/
│   └── Dockerfile              # tcpdump sidecar
├── scenarios/
│   └── client_sim.py           # Headless exam client simulator
├── docker-compose.yml          # Full compose file (all services)
├── run_scenarios.py            # Automated scenario runner
├── prof_dashboard.py           # Professor control dashboard (port 7777)
├── capture_network.py          # Standalone pcap capture script
├── SYNC_ALGORITHM.md           # Sync algorithm documentation (Hebrew)
└── ASSIGNMENT_PROMPT.md        # Project assignment description
```

---

## 17. Key Constants

| Constant | Value | Location | Purpose |
|----------|-------|----------|---------|
| COLLECTION_WINDOW_SEC | 1.5 | exam_server | Time to collect all exam_req before scheduling |
| C_SEND_MS | 1.0 | exam_server | Per-send overhead estimate |
| M_MARGIN_MS | 15.0 | exam_server | Scheduling safety margin |
| JITTER_BUFFER_MS | 5 | ws_bridge | Bridge hold jitter buffer |
| START_OFFSET_SEC | 120 | exam_server | Default exam start offset (2 min from first schedule_req) |
| MAX_PAYLOAD | 1200 | rudp_socket | RUDP max segment size (below IP MTU) |
| WINDOW_CAP | 32 | rudp_socket | Max congestion window |
| RETRANSMIT_MS | 500 | rudp_socket | RUDP retransmission timeout |
| MAX_RETRIES | 5 | rudp_socket | Max RUDP retransmissions |
| DUP_ACK_THRESH | 3 | rudp_socket | Fast retransmit trigger |
| INIT_CWND | 1.0 | rudp_socket | Initial congestion window |
| INIT_SSTHRESH | 32.0 | rudp_socket | Initial slow-start threshold |
| UDP_MAX | 512 | dns_server | RFC 1035 UDP max (triggers TC=1) |
| MAX_CNAME_DEPTH | 8 | resolver | Max CNAME chain following |

---

## 18. Running the Project

```bash
# Start everything
docker compose up --build

# Start with packet capture
docker compose --profile capture up --build

# Clients are at:
# http://localhost:8081?mode=rudp-sync
# http://localhost:8082?mode=tcp-sync
# http://localhost:8083?mode=tcp-nosync

# Admin panel:
# http://localhost:9999

# Professor dashboard:
pip install fastapi uvicorn websockets
python prof_dashboard.py
# http://localhost:7777

# Run scenarios:
pip install websockets
python run_scenarios.py
```

---

## 19. Docker MAC Addresses

Docker assigns virtual MAC addresses to each container: `02:42` prefix (locally-administered bit set) + IP in hex.

| Container | IP | MAC |
|---|---|---|
| DHCP Server | 10.99.0.3 | 02:42:0a:63:00:03 |
| DNS Resolver | 10.99.0.2 | 02:42:0a:63:00:02 |
| DNS Root | 10.99.0.10 | 02:42:0a:63:00:0a |
| DNS TLD | 10.99.0.11 | 02:42:0a:63:00:0b |
| DNS Auth | 10.99.0.12 | 02:42:0a:63:00:0c |
| RUDP Server 1 | 10.99.0.20 | 02:42:0a:63:00:14 |
| RUDP Server 2 | 10.99.0.21 | 02:42:0a:63:00:15 |
| RUDP Server 3 | 10.99.0.22 | 02:42:0a:63:00:16 |
| TCP-Sync Server | 10.99.0.23 | 02:42:0a:63:00:17 |
| TCP-NoSync Server | 10.99.0.24 | 02:42:0a:63:00:18 |
| Client (example) | 10.99.0.100 | 02:42:0a:63:00:64 |

Docker bridge (`br-examnet`) acts as a virtual L2 switch forwarding frames by MAC.

---

## 20. Network Message Flow

Chronological order of messages when a client connects and receives an exam:

| # | Application | Port Src | Port Des | IP Src | IP Des | MAC Src | MAC Des |
|---|---|---|---|---|---|---|---|
| 1 | DHCP Discover | 68/udp | 67/udp | 0.0.0.0 | 255.255.255.255 | 02:42:0a:63:00:64 | ff:ff:ff:ff:ff:ff |
| 2 | DHCP Offer | 67/udp | 68/udp | 10.99.0.3 | 10.99.0.100 | 02:42:0a:63:00:03 | 02:42:0a:63:00:64 |
| 3 | DHCP Request | 68/udp | 67/udp | 0.0.0.0 | 255.255.255.255 | 02:42:0a:63:00:64 | ff:ff:ff:ff:ff:ff |
| 4 | DHCP Ack | 67/udp | 68/udp | 10.99.0.3 | 10.99.0.100 | 02:42:0a:63:00:03 | 02:42:0a:63:00:64 |
| 5 | DNS Query (client→resolver) | ephemeral/udp | 53/udp | 10.99.0.100 | 10.99.0.2 | 02:42:0a:63:00:64 | 02:42:0a:63:00:02 |
| 6 | DNS Query (resolver→root) | ephemeral/udp | 53/udp | 10.99.0.2 | 10.99.0.10 | 02:42:0a:63:00:02 | 02:42:0a:63:00:0a |
| 7 | DNS Referral (root→resolver) | 53/udp | ephemeral/udp | 10.99.0.10 | 10.99.0.2 | 02:42:0a:63:00:0a | 02:42:0a:63:00:02 |
| 8 | DNS Query (resolver→TLD) | ephemeral/udp | 53/udp | 10.99.0.2 | 10.99.0.11 | 02:42:0a:63:00:02 | 02:42:0a:63:00:0b |
| 9 | DNS Referral (TLD→resolver) | 53/udp | ephemeral/udp | 10.99.0.11 | 10.99.0.2 | 02:42:0a:63:00:0b | 02:42:0a:63:00:02 |
| 10 | DNS Query (resolver→auth) | ephemeral/udp | 53/udp | 10.99.0.2 | 10.99.0.12 | 02:42:0a:63:00:02 | 02:42:0a:63:00:0c |
| 11 | DNS Answer (auth→resolver) | 53/udp | ephemeral/udp | 10.99.0.12 | 10.99.0.2 | 02:42:0a:63:00:0c | 02:42:0a:63:00:02 |
| 12 | DNS Answer (resolver→client) | 53/udp | ephemeral/udp | 10.99.0.2 | 10.99.0.100 | 02:42:0a:63:00:02 | 02:42:0a:63:00:64 |
| 13 | WebSocket (browser→nginx) | ephemeral/tcp | 80/tcp | 127.0.0.1 | 127.0.0.1 | loopback | loopback |
| 14 | WebSocket (nginx→bridge) | ephemeral/tcp | 8081/tcp | 127.0.0.1 | 127.0.0.1 | loopback | loopback |
| 15 | RUDP (bridge→server) | ephemeral/udp | 9000/udp | 10.99.0.100 | 10.99.0.20 | 02:42:0a:63:00:64 | 02:42:0a:63:00:14 |
| 16 | RUDP (server→bridge) | 9000/udp | ephemeral/udp | 10.99.0.20 | 10.99.0.100 | 02:42:0a:63:00:14 | 02:42:0a:63:00:64 |
| 17 | TCP-Sync (bridge→server) | ephemeral/tcp | 9001/tcp | 10.99.0.100 | 10.99.0.23 | 02:42:0a:63:00:64 | 02:42:0a:63:00:17 |
| 18 | TCP-Sync (server→bridge) | 9001/tcp | ephemeral/tcp | 10.99.0.23 | 10.99.0.100 | 02:42:0a:63:00:17 | 02:42:0a:63:00:64 |
| 19 | TCP-NoSync (bridge→server) | ephemeral/tcp | 9001/tcp | 10.99.0.100 | 10.99.0.24 | 02:42:0a:63:00:64 | 02:42:0a:63:00:18 |
| 20 | TCP-NoSync (server→bridge) | 9001/tcp | ephemeral/tcp | 10.99.0.24 | 10.99.0.100 | 02:42:0a:63:00:18 | 02:42:0a:63:00:64 |

**Notes:**

- Rows 1-4 (DHCP): Discover/Request are broadcast (MAC ff:ff:ff:ff:ff:ff, IP 255.255.255.255). Client has no IP yet.
- Rows 5-12 (DNS): Full iterative resolution. If UDP response is truncated (TC=1), resolver retries over TCP on same port 53.
- Rows 13-14 (WebSocket): Browser and bridge are in the same container — loopback (127.0.0.1). nginx routes /ws→8081, /ws-tcp-sync→8082, /ws-tcp-nosync→8083.
- Rows 15-16 (RUDP): UDP packets carrying the 14-byte RUDP header as payload. Server IP determined by DNS round-robin (20/21/22).
- Rows 17-20 (TCP): Standard TCP with NL-JSON framing. Same port 9001 but different server IPs (23 for sync, 24 for nosync).

---

## 21. NAT Behavior

**In this project there is no NAT** — all containers share the same Docker bridge subnet (10.99.0.0/24).

If NAT were present (e.g., a NAT gateway at 10.99.0.1 translating to public IP 203.0.113.1):

| Field | Without NAT | With NAT |
|---|---|---|
| IP Source (outgoing) | 10.99.0.100 | 203.0.113.1 |
| Port Source (outgoing) | ephemeral (e.g. 54321) | NAT-assigned (e.g. 40001) |
| IP Dest (return) | 10.99.0.100 | 203.0.113.1 → NAT → 10.99.0.100 |
| Port Dest (return) | original ephemeral | NAT port → original |
| MAC Source (after NAT) | client MAC | gateway MAC (02:42:0a:63:00:01) |
| MAC Dest | unchanged | unchanged (same segment) |

**What does NOT change with NAT:**

- Application protocol (DNS, RUDP, TCP) — NAT is transparent to upper layers
- Destination port (53, 9000, 9001) — NAT doesn't modify destination port on outgoing
- Destination IP (outgoing) — the target server stays the same
- Payload (RUDP header, JSON) — NAT doesn't touch content
- DHCP — operates at Layer 2 broadcast, does not traverse NAT

**Impact on this project:** RTT measurement is unaffected (measured at client clock). RUDP works over NAT since it's UDP-based. TCP works natively. The synchronized delivery algorithm is NAT-transparent.

---

## 22. QUIC Comparison

This project does **not** use QUIC. QUIC (RFC 9000) runs over UDP (typically port 443) and provides TLS 1.3, stream multiplexing without head-of-line blocking, and 0-RTT connection setup.

**Similarities with our RUDP:**

- Both add reliability on top of UDP
- Both have congestion control
- Both use acknowledgments and retransmission

**Differences:**

| Feature | Our RUDP | QUIC |
|---|---|---|
| Encryption | None (isolated Docker network) | TLS 1.3 mandatory |
| Multiplexing | Single stream per connection | Multiple independent streams |
| Connection setup | 3-way handshake (1 RTT) | 0-RTT or 1-RTT |
| Congestion control | TCP Reno (slow start + AIMD) | Pluggable (default: Cubic-like) |
| Custom feature | ExamSendCoordinator for sync delivery | N/A |

If QUIC were used, the message flow table would look identical to RUDP (both over UDP), but Port Des would be 443 instead of 9000.

---

## 23. Generated Artifacts

| File | Purpose |
|---|---|
| `docs/udp_vs_rudp.puml` | PlantUML: UDP header vs RUDP header comparison |
| `docs/rudp_header.puml` | PlantUML: RUDP header field breakdown |
| `generate_q4_table.py` | Generates Q4_message_table.docx |
| `Q4_message_table.docx` | Word document: full message flow tables (with/without NAT) |
| `generate_report.py` | Generates the main academic report document |

---

## 24. Synchronized Delivery — Code Locations

Exact source locations for each step of the sync algorithm:

### Step 1 — RTT Measurement (Frontend)

| What | File | Lines |
|---|---|---|
| `doTimeReq()` — single measurement | App.tsx | 311-320 |
| `syncStdev()` / `syncHasConverged()` | App.tsx | 89-96 |
| `buildRttSamples()` — min/med/max | App.tsx | 98-101 |
| Phase 1 — 3 rounds at 50ms | App.tsx | 345-353 |
| `schedule_req` with rtt_samples | App.tsx | 355-365 |
| Phase 2 — adaptive 200ms | App.tsx | 381-398 |
| Phase 3 — 3 fresh rounds at T₀−5s | App.tsx | 400-418 |
| `exam_req` with fresh samples | App.tsx | 427-441 |

### Step 2 — D_i Computation (Server)

| What | File | Lines |
|---|---|---|
| `_compute_D` (RUDP) | RUDP/exam_server.py | 92-124 |
| `_compute_D` (TCP) | TCP/exam_server_tcp.py | 71-86 |

### Step 3 — T_arr and T_send (Coordinator)

| What | File | Lines |
|---|---|---|
| T_arr + T_send computation (RUDP) | RUDP/exam_server.py | 218-239 |
| T_arr + T_send computation (TCP) | TCP/exam_server_tcp.py | 141-152 |

### Step 4 — Staggered Sending

| What | File | Lines |
|---|---|---|
| Sort + delta scheduling (RUDP) | RUDP/exam_server.py | 241-255 |
| Sort + delta scheduling (TCP) | TCP/exam_server_tcp.py | 153-164 |
| Actual sleep in handler (RUDP) | RUDP/exam_server.py | 329-335 |
| Actual sleep in handler (TCP) | TCP/exam_server_tcp.py | 414-420 |

### Step 5 — Bridge Self-Correcting Hold

| What | File | Lines |
|---|---|---|
| target_ms / hold_ms (RUDP) | RUDP/ws_bridge.py | 220-225 |
| target_ms / hold_ms (TCP) | TCP/ws_bridge_tcp.py | 164-168 |
