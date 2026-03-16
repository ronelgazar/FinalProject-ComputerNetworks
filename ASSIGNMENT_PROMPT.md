# Assignment Writing Prompt

Use this prompt in a new chat to get help writing the assignment report.

---

## Prompt to paste

You are helping me write a computer networks course final project assignment report (Hebrew university, year 2, semester 1). The project is a **Docker-based exam network simulation** that demonstrates key networking concepts:

---

### Project Overview

A fully containerised exam system built in Python + React, running in Docker. It simulates a real university exam environment and showcases three major networking subsystems:

**1. RFC-compliant DNS hierarchy**
- 4 Docker containers: root nameserver → .lan TLD → exam.lan authoritative → recursive resolver
- Implements RFC 1035: label encoding, pointer compression, NS/A/SOA/PTR records
- `DNS/dns_packet.py` — pure stdlib DNS codec (no deps)
- `DNS/resolver_server.py` — iterative resolution with TTL-aware cache, CNAME chain following
- `DNS/auth_server.py` — exam.lan zone with A records for `server.exam.lan` (round-robin LB to 3 RUDP servers), `tcp-sync.exam.lan` (10.99.0.23), `tcp-nosync.exam.lan` (10.99.0.24)

**2. Custom RUDP protocol over UDP**
- 14-byte fixed header: seq(4) ack(4) flags(1) window(1) len(2) crc16(2)
- Flags: SYN=0x01 ACK=0x02 FIN=0x04 DATA=0x08 RST=0x10 PING=0x20 MSG_END=0x40
- CRC-16/CCITT-FALSE checksum (poly=0x1021, init=0xFFFF) over header[0:12]+payload
- Sliding window (size 8), cumulative ACK, retransmit timer 500ms, max 5 retries
- Congestion control (TCP Reno inspired): slow start, AIMD, fast retransmit on 3 dup ACKs
- 3-way handshake (SYN→SYN+ACK→ACK) with MSS negotiation (2B payload), graceful close (FIN+ACK)
- MSG_END flag for multi-packet message reassembly (large JSON payloads)
- `RUDP/rudp_socket.py` — asyncio-based RudpSocket + RudpServer
- `RUDP/exam_server.py` — exam backend over RUDP with ExamSendCoordinator
- `RUDP/ws_bridge.py` — WebSocket↔RUDP bridge (port 8081)

**3. Three transport modes for comparison**

| Mode | Transport | Sync | WS route | Server IP |
|------|-----------|------|-----------|-----------|
| RUDP + Sync | custom RUDP | ✓ | `/ws` | 10.99.0.20-22 |
| TCP + Sync | plain TCP | ✓ | `/ws-tcp-sync` | 10.99.0.23 |
| TCP + No Sync | plain TCP | ✗ baseline | `/ws-tcp-nosync` | 10.99.0.24 |

**4. Synchronized exam delivery (the core research contribution)**

The key innovation: all exam clients receive the exam at exactly the same millisecond regardless of different RTTs.

Two-layer mechanism:

- **Layer A (server-side staggered sends — "friend's formula"):**
  - Per-client RTT measurement from adaptive clock sync samples
  - `D_i = (RTT_med_i + J_rtt_i) / 2` (conservative one-way delay estimate)
  - OR asymmetric-corrected: `d_down_i = RTT_med_i/2 - offset_i; D_i = d_down_i + J_rtt_i/2`
  - Compute: `T_arr = now + max(D_i) + N×C + M`
    - N = number of clients, C = 1.0 ms per-send overhead, M = 15 ms scheduling margin
  - Each client's send time: `T_send[i] = T_arr − D_i` (slower clients sent first)
  - Wait `COLLECTION_WINDOW_SEC = 1.5s` for all `exam_req` to arrive before scheduling

- **Layer B (bridge self-correcting hold):**
  - `hold_ms = (server_sent_at_ms + max_rtt/2 + JITTER_BUFFER_MS) − now()`
  - `JITTER_BUFFER_MS = 5` (reduced from 30 to prevent ~1ms asyncio sleep jitter from dominating)
  - Self-corrects for RUDP retransmission: if retransmit delayed by Δms, `now()` already advanced → hold shrinks by Δ, compensating automatically

**5. Adaptive 4-phase clock synchronisation (replaces fixed 12-round NTP burst)**

Unlike Cristian's algorithm (single best-RTT round, reference baseline), our algorithm uses:
- **Phase 1**: 3 quick rounds at 50ms cadence → enough to send `schedule_req`
- **Phase 2**: adaptive refinement every 200ms, stops when stdev(last 5 offsets) < 1.5ms, or at T₀−8s, or after 20 samples
- **Phase 3**: fresh 3 rounds at T₀−5s (final correction before exam start)
- **Phase 4**: wait → send `exam_req`

This ensures clock offset accuracy improves over time and reaches < 2ms stdev before exam delivery.

**Cristian's algorithm is used as a reference baseline** in the Prof Dashboard clock comparison chart — it represents a single minimum-RTT round. Our adaptive multi-phase algorithm achieves better accuracy over multiple phases.

**6. DHCP server** — leases IPs from 10.99.0.100-149 pool

**7. React frontend** (TypeScript/Vite) with:
- Mode selector boot screen (RUDP+Sync / TCP+Sync / TCP+NoSync) + `?mode=` URL param
- Adaptive RTT clock sync display with live phase indicator
- Synchronized Delivery Proof card (shows `server_sent_at_ms`, `bridge_forwarded_at_ms`, `browser_received_at_ms`, Δ from target)
- Mode badge: RUDP+Sync / TCP+Sync / TCP+NoSync (no-sync shows "⚠ Sync disabled — baseline mode")
- Live exam UI with MCQ + text questions, autosave, countdown timer

**8. Admin panel** (FastAPI, port 9999) — live monitor, sync proof table, submission viewer, exam upload

**9. Prof dashboard** (FastAPI, port 7777) — container status, netem simulation, DNS trace, transport comparison chart, concurrent load test (SimpleBarrier-based with 60s timeout + error release)

---

### Network Layout

```
10.99.0.0/24
10.99.0.1   gateway
10.99.0.2   DNS recursive resolver
10.99.0.3   DHCP server
10.99.0.10  DNS root
10.99.0.11  DNS TLD (.lan)
10.99.0.12  DNS auth (exam.lan)
10.99.0.20-22  RUDP exam servers (3 instances, shared start_at.txt volume)
10.99.0.23  TCP exam server (SYNC_ENABLED=1)
10.99.0.24  TCP exam server (SYNC_ENABLED=0, baseline)
10.99.0.30  Admin panel
10.99.0.100-149  DHCP client pool
```

---

### Key Implementation Details

**DNS resolution trace for `server.exam.lan`:**
```
Client → resolver(10.99.0.2) → root(10.99.0.10): REFERRAL lan. NS → glue
resolver → tld(10.99.0.11): REFERRAL exam.lan. NS → glue A 10.99.0.12
resolver → auth(10.99.0.12): ANSWER server.exam.lan A 10.99.0.20/21/22 (AA=1)
```

**DNS zone — exam.lan authoritative:**
```
server      300 A   10.99.0.20, 10.99.0.21, 10.99.0.22  (round-robin)
tcp-sync    300 A   10.99.0.23
tcp-nosync  300 A   10.99.0.24
ns1         300 A   10.99.0.12
dhcp        300 A   10.99.0.3
dns         300 A   10.99.0.2
gw          300 A   10.99.0.1
```

**RUDP header layout:**
```
 0       4       8   9    10      12      14
 +-------+-------+---+----+-------+-------+------...
 | seq   | ack   |flg|win | len   | crc16 | payload
 +-------+-------+---+----+-------+-------+------...
   4B      4B     1B  1B    2B      2B      len bytes
```
Total overhead: 14 bytes (vs TCP minimum 20 bytes — 30% smaller)

**RUDP vs standard UDP header:**
- Standard UDP: 8 bytes (src port, dst port, length, checksum) — no reliability
- Our RUDP: 14 bytes riding INSIDE UDP payload — adds seq/ack, flags, window, CRC-16
- Full wire format: IP(20B) → UDP(8B) → RUDP(14B) → JSON payload = 42B minimum overhead

**RUDP vs TCP comparison:**
| Feature | Our RUDP | TCP | Standard UDP |
|---------|----------|-----|-------------|
| Header size | 14B | ≥20B | 8B |
| Reliable delivery | Yes (seq/ack + retransmit) | Yes (kernel) | No |
| Ordering | Yes (seq number) | Yes | No |
| Flow control | Yes (window field, default 8) | Yes (rwnd + cwnd) | No |
| Congestion control | Yes (Reno: slow start + AIMD) | Yes (Cubic/Reno) | No |
| Integrity check | CRC-16 mandatory | Checksum mandatory | Checksum optional (IPv4) |
| Connection lifecycle | SYN/ACK/FIN | SYN/ACK/FIN | Connectionless |
| Message reassembly | MSG_END flag | Stream-based | One datagram = one message |
| Keepalive | PING flag | TCP keepalive | No |

**Key constants (exam_server.py / ws_bridge.py):**
```
JITTER_BUFFER_MS      = 5     (ms, bridge self-correcting hold buffer)
C_SEND_MS             = 1.0   (ms, per-client send overhead estimate)
M_MARGIN_MS           = 15.0  (ms, scheduling margin added to T_arr)
COLLECTION_WINDOW_SEC = 1.5   (s, wait for all exam_req before scheduling)
RUDP_PORT             = 9000
TCP_PORT              = 9001
```

**Scalability of stagger window:**
- Sync: `T_arr − now ≈ max(D_i) + N×1ms + 15ms` — grows linearly with N
- No-sync: 0ms overhead regardless of N

**Protocol stack per mode:**
```
Browser ──WS──► nginx ──► ws_bridge (port 8081/8082/8083)
                              │ RUDP (9000) / TCP (9001)
                              ▼
                         exam_server (port 9000 RUDP / 9001 TCP)
```

**ExamSendCoordinator — shared T_arr via start_at_ms:**
The RUDP coordinator originally computed `T_arr = now + max(D_i) + N×C + M` independently on each server instance. Since DNS round-robin distributes clients across 3 servers, each server's `COLLECTION_WINDOW_SEC` fires at a different wall-clock time, causing ~200-300ms cross-server T_arr divergence.

Fix: pin `T_arr = start_at_ms` (read from the shared `/app/shared/start_at.txt` volume). All 3 servers share the same logical delivery moment. A dynamic fallback (`now + max(D_i) + N×C + M`) is used only if `start_at_ms` is already in the past or too close to send in time (e.g., prof-dashboard simulation with a very short offset).

`reset()` also clears the in-memory `_EXAM_START_AT_MS` cache so a freshly injected `start_at.txt` is picked up by subsequent simulation runs.

**SimpleBarrier (concurrent load test sync primitive):**
- All simulated clients call `wait()` → barrier fires when all N arrive
- `timeout=60s` prevents deadlock if one client errors before reaching barrier
- `release()` called from `except` block so a failing client unblocks the rest

**Resolved — RUDP multi-server coordinator divergence:**
Previously, 3 RUDP server instances each computed `T_arr = now + max(D_i) + ...` independently. With clients distributed via DNS round-robin, each server's coordinator fired at a different wall-clock time → ~250ms spread. Fixed by anchoring `T_arr = start_at_ms` (from the shared `/app/shared/start_at.txt` volume) in all coordinators, eliminating cross-server divergence.

**Remaining limitations:**
- **Windows Docker asyncio sleep granularity**: Python asyncio sleep on Windows has ~1ms granularity. `JITTER_BUFFER_MS=5` was chosen to keep the bridge hold small enough that ±1ms wakeup variation doesn't dominate spread. Lower values risk sub-millisecond asyncio precision; higher values unnecessarily widen the delivery window.
- **DNS TTL cache**: DNS resolver caches A records for 300s. During a test run, all 3 RUDP server IPs are returned and cached; bridge processes connect to whichever IP their DNS query returns. Cache eviction could transiently affect load distribution.
- **COLLECTION_WINDOW_SEC vs latency**: The 1.5s window waits for all `exam_req` to arrive before scheduling. A client that connects late (e.g., > 1.5s after the first exam_req) will be scheduled alone in a new coordinator round with its own `T_arr = start_at_ms`, which is still correct since all coordinators share the same anchor.

### Files Structure
```
DNS/           dns_packet.py, dns_server.py, root_server.py, tld_server.py,
               auth_server.py, resolver_server.py, Dockerfile
RUDP/          rudp_packet.py, rudp_socket.py, exam_server.py, ws_bridge.py,
               Dockerfile.server, requirements.txt
TCP/           exam_server_tcp.py, ws_bridge_tcp.py, Dockerfile.server,
               requirements.txt
DHCP/          dhcp_server.py, dhcp_client.py, entrypoint.sh, nginx.conf,
               supervisord.conf, Dockerfile.server, Dockerfile.client
admin/         admin_server.py, Dockerfile
app/frontend/  src/App.tsx (React/TS/Vite)
prof_dashboard.py
docker-compose.yml
```

**DHCP/supervisord.conf programs:**
1. `nginx` — serves React frontend + proxies WebSocket routes
2. `bridge` — `ws_bridge.py` RUDP bridge on port 8081 (`/ws`)
3. `bridge-tcp-sync` — `ws_bridge_tcp.py` TCP sync bridge on port 8082 (`/ws-tcp-sync`)
4. `bridge-tcp-nosync` — `ws_bridge_tcp.py` TCP no-sync bridge on port 8083 (`/ws-tcp-nosync`)

**docker-compose.yml services:**
- dhcp-server, dns-root, dns-tld, dns-auth, dns-resolver
- rudp-server-1/2/3 (10.99.0.20-22, shared `exam-shared` + `answers-data` volumes)
- tcp-server-sync (10.99.0.23, SYNC_ENABLED=1), tcp-server-nosync (10.99.0.24, SYNC_ENABLED=0)
- admin (10.99.0.30, port 9999)
- client-1/2/3 (DHCP pool, each runs nginx+bridge+bridge-tcp-sync+bridge-tcp-nosync)
- Packet capture sidecars (optional, `--profile capture`)

---

### Docker MAC Addresses

Docker assigns virtual MAC addresses: `02:42` prefix (locally-administered) + IP in hex.
Example: 10.99.0.100 → `02:42:0a:63:00:64`.

All containers are on the same Docker bridge (`br-examnet`) which acts as a virtual L2 switch.

### Network Message Flow (chronological)

| # | Application | Port Src | Port Des | IP Src | IP Des |
|---|---|---|---|---|---|
| 1 | DHCP Discover | 68/udp | 67/udp | 0.0.0.0 | 255.255.255.255 (broadcast) |
| 2 | DHCP Offer/Ack | 67/udp | 68/udp | 10.99.0.3 | 10.99.0.100 |
| 3 | DNS Query (client→resolver) | ephemeral/udp | 53/udp | 10.99.0.100 | 10.99.0.2 |
| 4 | DNS iterative (resolver→root→TLD→auth) | ephemeral/udp | 53/udp | 10.99.0.2 | 10/11/12 |
| 5 | DNS Answer (resolver→client) | 53/udp | ephemeral/udp | 10.99.0.2 | 10.99.0.100 |
| 6 | WebSocket (browser↔nginx↔bridge) | ephemeral/tcp | 80→8081/tcp | 127.0.0.1 | 127.0.0.1 (loopback) |
| 7 | RUDP exam traffic | ephemeral/udp | 9000/udp | 10.99.0.100 | 10.99.0.20 |
| 8 | TCP-sync exam traffic | ephemeral/tcp | 9001/tcp | 10.99.0.100 | 10.99.0.23 |
| 9 | TCP-nosync exam traffic | ephemeral/tcp | 9001/tcp | 10.99.0.100 | 10.99.0.24 |

Full table with MAC addresses available in `Q4_message_table.docx` (generated by `generate_q4_table.py`).

### NAT Behavior

**No NAT in this project** — all containers on same Docker bridge (10.99.0.0/24).

If NAT were present:
- Source IP changes: 10.99.0.100 → NAT public IP (e.g., 203.0.113.1)
- Source Port changes: ephemeral → NAT-assigned port
- MAC Source changes: client MAC → gateway MAC (changes at every L3 hop)
- Destination port, payload, and application protocol are NOT affected
- DHCP does NOT traverse NAT (Layer 2 broadcast)
- RTT measurement is unaffected (measured at client clock)
- Both RUDP (UDP-based) and TCP work through NAT transparently

### QUIC Comparison

Project does NOT use QUIC. Our RUDP is similar in concept (reliability over UDP) but differs:
- No encryption (isolated Docker network doesn't need TLS)
- No multiplexing (one connection = one exam)
- No 0-RTT (we use 3-way handshake like TCP)
- Has ExamSendCoordinator — unique sync mechanism not in QUIC
- If QUIC were used, wire format would be identical to RUDP table (both over UDP), port 443 instead of 9000

---

### Measured Results

| Mode | 2-client spread | 5-client spread | Notes |
|------|-----------------|-----------------|-------|
| RUDP + Sync | < 5 ms | < 15 ms | T_arr pinned to shared start_at_ms — all 3 server instances converge |
| TCP + Sync | < 5 ms | < 15 ms | Single coordinator, consistently SYNCHRONIZED |
| TCP + No Sync | N/A (no sync) | N/A | "⚠ UNCOORDINATED" baseline |

Thresholds in dashboard: ≤ 30ms = SYNCHRONIZED, 31-99ms = NEAR-SYNC, ≥ 100ms = OUT OF SYNC.

---

### Assignment Tasks for You

Please help me write a **professional assignment report** in Hebrew covering:

1. **מבוא (Introduction)** — motivate the problem: how do you ensure exam fairness when students have different network latencies? Introduce the three transport modes and the synchronized delivery mechanism.

2. **ארכיטקטורה (Architecture)** — diagram and description of all components, network layout (10.99.0.0/24 subnet), Docker service map, protocol stack per mode.

3. **DNS היררכיה (DNS Hierarchy)** — explain the iterative resolution flow, RFC 1035 implementation details (label encoding, pointer compression, AA flag, delegation chain). Show the full resolution trace for `server.exam.lan`. Explain round-robin load balancing across 3 RUDP servers.

4. **פרוטוקול RUDP (RUDP Protocol)** — header format (14 bytes, field-by-field), CRC-16/CCITT-FALSE, sliding window (size 8), 3-way handshake, retransmit timer (500ms, 5 retries), comparison to TCP (14B vs ≥20B, no OS stack).

5. **סנכרון בחינה (Exam Synchronization)** — explain the two-layer mechanism in detail:
   - Adaptive 4-phase clock sync (contrast with Cristian's single-round algorithm as reference)
   - Server-side staggered sends: `D_i` formula, `T_arr` computation, staggered send order
   - Bridge self-correcting hold: `hold_ms` formula, `JITTER_BUFFER_MS=5`, retransmission compensation
   - Mathematical proof that all clients receive at `T_arr ± ε` (ε ≈ JITTER_BUFFER_MS + asyncio granularity)
   - Collection window: why wait 1.5s before scheduling

6. **השוואת מצבי תחבורה (Transport Mode Comparison)** — table comparing RUDP+Sync vs TCP+Sync vs TCP+NoSync on: header overhead, sync accuracy, scalability (linear N), reliability mechanism (SW-ARQ vs Kernel TCP vs none), measured spread.

7. **תוצאות (Results)** — describe what the prof_dashboard shows:
   - TCP+Sync: consistently < 5ms spread, SYNCHRONIZED
   - RUDP+Sync (any number of clients, across all 3 servers): < 15ms spread, SYNCHRONIZED — T_arr anchored to shared start_at_ms eliminates cross-server divergence
   - TCP+NoSync: UNCOORDINATED, timing fields absent
   - Clock sync comparison: adaptive algo vs Cristian's reference

8. **מגבלות ושיפורים אפשריים (Limitations and Future Work)**:
   - RUDP multi-server coordinator divergence — **resolved** by anchoring T_arr to shared start_at_ms; describe the root cause and fix
   - Windows Docker asyncio sleep granularity impact (±1ms) and the JITTER_BUFFER_MS=5 tuning trade-off
   - DNS TTL cache and its effect on load distribution across RUDP servers
   - COLLECTION_WINDOW_SEC latency trade-off: faster response time vs larger synchronized groups
   - Possible improvements: Redis-based shared state, actual hardware NTP, QUIC transport

9. **מסקנות (Conclusions)** — what was learned, networking concepts demonstrated, value of layered synchronization.

Write each section in formal academic Hebrew (approximately 200-300 words each). Use technical terms correctly in Hebrew/English. Include LaTeX-style formulas where appropriate (e.g., for `D_i`, `T_arr`, clock offset, NTP Cristian's formula).
