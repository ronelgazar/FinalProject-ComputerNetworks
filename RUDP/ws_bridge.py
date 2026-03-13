"""
WebSocket ↔ RUDP bridge.

Runs inside each client container as a local WebSocket server on port 8081.
nginx proxies /ws → localhost:8081.

For each browser WebSocket session, it opens one RUDP connection to the
exam backend (resolved via DNS: server.exam.lan → 10.99.0.20/21/22).

Per-packet synchronized delivery
---------------------------------
The bridge injects the measured handshake RTT into the 'hello' message so
the server can compute per-client delivery delays.  When the server stamps
a response with 'deliver_delay_ms', the bridge holds the message for that
many milliseconds before forwarding to the browser — guaranteeing all
clients receive the packet at the same wall-clock moment.

Software netem simulation
--------------------------
The professor dashboard writes /tmp/netem_delay.json with:
  {"delay_ms": N, "jitter_ms": M, "loss_pct": P}
The bridge reads this file and applies artificial delay, jitter, and loss
to the server→browser direction — no kernel module required.
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import pathlib
import random
import socket
import struct
import time
import zlib

import websockets

from rudp_socket import rudp_connect, RudpTimeout, RudpReset

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [bridge] %(levelname)s %(message)s',
)
log = logging.getLogger(__name__)

LISTEN_HOST      = os.environ.get('BRIDGE_HOST', '0.0.0.0')
LISTEN_PORT      = int(os.environ.get('BRIDGE_PORT', '8081'))
RUDP_SERVER_HOST = os.environ.get('RUDP_SERVER_HOST', 'server.exam.lan')
RUDP_SERVER_PORT = int(os.environ.get('RUDP_SERVER_PORT', '9000'))
DNS_RESOLVER     = os.environ.get('DNS_RESOLVER', '10.99.0.2')

# Packet types that receive synchronized delivery treatment.
# time_resp is intentionally excluded — it is timing-sensitive for NTP
# and must be forwarded immediately to keep offset measurements accurate.
SYNC_PACKET_TYPES = {'exam_resp', 'schedule_resp'}
JITTER_BUFFER_MS  = 5

# ── Software netem (professor dashboard writes /tmp/netem_delay.json) ─────────
_NETEM_FILE  = pathlib.Path('/tmp/netem_delay.json')
_netem       = {"delay_ms": 0, "jitter_ms": 0, "loss_pct": 0.0}
_netem_mtime = 0.0


def _reload_netem() -> None:
    """Re-read netem config from disk if it changed."""
    global _netem, _netem_mtime
    try:
        mt = _NETEM_FILE.stat().st_mtime
        if mt != _netem_mtime:
            _netem = json.loads(_NETEM_FILE.read_text())
            _netem_mtime = mt
            log.info("netem updated: delay=%sms jitter=%sms loss=%s%%",
                     _netem.get("delay_ms", 0),
                     _netem.get("jitter_ms", 0),
                     _netem.get("loss_pct", 0))
    except FileNotFoundError:
        pass   # no file → no interference
    except Exception as e:
        log.warning("Could not read netem file: %s", e)


async def _netem_delay() -> bool:
    """
    Apply current netem parameters.
    Returns True if the packet should be DROPPED (loss simulation).
    Otherwise sleeps for delay+jitter and returns False.
    """
    _reload_netem()
    loss_pct  = float(_netem.get("loss_pct", 0))
    delay_ms  = float(_netem.get("delay_ms", 0))
    jitter_ms = float(_netem.get("jitter_ms", 0))

    if loss_pct > 0 and random.uniform(0, 100) < loss_pct:
        return True   # drop this packet

    if jitter_ms > 0:
        delay_ms += random.uniform(-jitter_ms, jitter_ms)

    if delay_ms > 0:
        await asyncio.sleep(max(0.0, delay_ms) / 1000.0)

    return False


_cached_resolve: tuple[str, list[str]] | None = None


def _resolve_server() -> tuple[str, list[str]]:
    """
    Resolve RUDP_SERVER_HOST via the container's configured DNS (10.99.0.2).
    Returns (chosen_ip, all_ips).  getaddrinfo may return multiple A records
    (DNS round-robin load balancing); the result is cached so all WebSocket
    sessions through this bridge hit the same backend — critical for the
    ExamSendCoordinator which must see every client on the same server.
    """
    global _cached_resolve
    if _cached_resolve is not None:
        log.info("Using cached DNS: %s → %s  (all: %s)",
                 RUDP_SERVER_HOST, _cached_resolve[0], _cached_resolve[1])
        return _cached_resolve

    results = socket.getaddrinfo(RUDP_SERVER_HOST, RUDP_SERVER_PORT,
                                  socket.AF_INET, socket.SOCK_DGRAM)
    all_ips = list(dict.fromkeys(r[4][0] for r in results))   # unique, ordered
    ip = results[0][4][0]
    _cached_resolve = (ip, all_ips)
    log.info("Resolved %s → %s  (all: %s)", RUDP_SERVER_HOST, ip, all_ips)
    return ip, all_ips


async def _ws_to_rudp(ws, rudp, state: dict):
    """Forward WebSocket frames → RUDP, injecting RTT into 'hello' messages."""
    async for message in ws:
        try:
            if isinstance(message, str):
                try:
                    obj = json.loads(message)
                    if obj.get('type') == 'hello':
                        obj['rtt_ms']           = round(rudp.rtt_ms, 2)
                        obj['handshake_rtt_ms'] = round(rudp.handshake_rtt_ms, 2)
                        message = json.dumps(obj)
                        log.info("Injected rtt_ms=%.1f into hello", rudp.rtt_ms)
                except Exception:
                    pass
                await rudp.send(message.encode('utf-8'))
            else:
                await rudp.send(message)
        except (RudpTimeout, RudpReset) as e:
            log.warning("RUDP send error: %s", e)
            break


async def _rudp_to_ws(ws, rudp, state: dict):
    """
    Forward RUDP messages → WebSocket with two-layer timing control.

    Layer 1 — Software netem (professor dashboard):
      Reads /tmp/netem_delay.json; applies delay + jitter + optional loss.

    Layer 2 — Self-correcting synchronized delivery:
      For content packets (exam_resp, schedule_resp) the bridge computes:

        target_ms = server_sent_at_ms + max_rtt/2 + JITTER_BUFFER_MS
        hold_ms   = target_ms - time.now()

      Because every container on the same Docker host shares the same clock,
      server_sent_at_ms and time.now() are in the same reference frame.

      Self-correction property: if RUDP retransmission delayed the packet
      by Δ ms, time.now() has already advanced by Δ, so hold_ms shrinks
      by exactly Δ — guaranteeing all clients still hit target_ms.

      The bridge also stamps bridge_forwarded_at_ms and sync_hold_ms onto
      the forwarded packet so the browser can display the proof.
    """
    while not rudp._closed:
        try:
            data = await rudp.recv()
            if not data:
                break
            # Decode server frame format:
            #  b'ZS' — split format: uint16-BE(meta_len) | meta_json | exam_zl
            #  b'ZL' — legacy full-frame compress (fallback)
            #  else  — plain UTF-8 JSON
            if data[:2] == b'ZS':
                meta_len = struct.unpack('>H', data[2:4])[0]
                meta     = json.loads(data[4:4 + meta_len])
                meta['exam'] = json.loads(zlib.decompress(data[4 + meta_len:]))
                text = json.dumps(meta)
            elif data[:2] == b'ZL':
                text = zlib.decompress(data[2:]).decode('utf-8')
            else:
                text = data.decode('utf-8')
            try:
                obj = json.loads(text)
                pkt_type = obj.get('type', '?')

                # ── Layer 1: software netem ───────────────────────────────
                dropped = await _netem_delay()
                if dropped:
                    log.info("netem: dropped %s (loss simulation)", pkt_type)
                    continue

                # ── Layer 2: synchronized delivery ────────────────────────
                if (pkt_type in SYNC_PACKET_TYPES
                        and 'server_sent_at_ms' in obj
                        and state['max_rtt_ms'] > 0):
                    # Always pick up the latest max_rtt/my_rtt from the
                    # server — exam_resp carries fresh values computed after
                    # all clients registered their Phase-3 RTT samples.
                    if 'max_rtt_ms' in obj:
                        state['max_rtt_ms'] = float(obj['max_rtt_ms'])
                    if 'my_rtt_ms' in obj:
                        state['my_rtt_ms']  = float(obj['my_rtt_ms'])
                    if pkt_type == 'schedule_resp':
                        log.info("Sync state updated: max_rtt=%.1f my_rtt=%.1f",
                                 state['max_rtt_ms'], state['my_rtt_ms'])

                    target_ms  = (obj['server_sent_at_ms']
                                  + state['max_rtt_ms'] / 2
                                  + JITTER_BUFFER_MS)
                    now_ms     = time.time() * 1000
                    hold_ms    = target_ms - now_ms
                    actual_hold = max(0.0, hold_ms)
                    transit_ms = now_ms - obj['server_sent_at_ms']

                    if pkt_type == 'exam_resp':
                        log.info("exam_resp transit=%.1fms hold=%.1fms "
                                 "max_rtt=%.1f server_sent=%d",
                                 transit_ms, actual_hold,
                                 state['max_rtt_ms'],
                                 obj['server_sent_at_ms'])

                    if actual_hold > 0:
                        log.info("Sync hold %.1f ms for %s  "
                                 "(target=%.0f retransmission_absorbed=%.1f ms)",
                                 actual_hold, pkt_type,
                                 target_ms, max(0.0, -hold_ms))
                        await asyncio.sleep(actual_hold / 1000.0)

                    obj['bridge_forwarded_at_ms'] = int(time.time() * 1000)
                    obj['sync_hold_ms']           = round(actual_hold, 2)
                    obj['sync_target_ms']         = round(target_ms, 2)

                elif 'deliver_delay_ms' in obj and obj['deliver_delay_ms'] > 0:
                    # Fallback for schedule_resp before max_rtt_ms is known
                    await asyncio.sleep(obj['deliver_delay_ms'] / 1000.0)
                    obj['bridge_forwarded_at_ms'] = int(time.time() * 1000)
                    obj['sync_hold_ms']           = obj['deliver_delay_ms']

                # Update sync state from schedule_resp even in fallback path
                if pkt_type == 'schedule_resp':
                    state['max_rtt_ms'] = float(obj.get('max_rtt_ms', state['max_rtt_ms']))
                    state['my_rtt_ms']  = float(obj.get('my_rtt_ms',  rudp.rtt_ms))

                await ws.send(json.dumps(obj))

            except (ValueError, TypeError):
                dropped = await _netem_delay()
                if not dropped:
                    await ws.send(text)
        except (RudpTimeout, RudpReset) as e:
            log.warning("RUDP recv error: %s", e)
            break
        except websockets.exceptions.ConnectionClosed:
            break   # browser closed tab / navigated away — not an error
        except Exception as e:
            log.warning("WS send error: %s", e)
            break


async def handle_ws(ws):
    log.info("WebSocket connection from %s", ws.remote_address)
    try:
        loop = asyncio.get_running_loop()
        server_ip, all_ips = await loop.run_in_executor(None, _resolve_server)
        rudp = await rudp_connect(server_ip, RUDP_SERVER_PORT)
        log.info("RUDP connected to %s:%d  handshake_rtt=%.1f ms",
                 server_ip, RUDP_SERVER_PORT, rudp.handshake_rtt_ms)

        # Per-connection sync state — populated from schedule_resp
        state = {
            'max_rtt_ms': 0.0,
            'my_rtt_ms':  rudp.rtt_ms,
        }

        await ws.send(json.dumps({
            "type":             "connection_info",
            "dns_hostname":     RUDP_SERVER_HOST,
            "dns_resolver":     DNS_RESOLVER,
            "resolved_ip":      server_ip,
            "all_ips":          all_ips,
            "handshake_rtt_ms": round(rudp.handshake_rtt_ms, 2),
            "rtt_ms":           round(rudp.rtt_ms, 2),
        }))
    except Exception as e:
        log.error("Failed to connect RUDP: %s", e)
        try:
            await ws.send(json.dumps({
                "type":    "error",
                "message": f"Bridge could not connect to exam server: {e}",
            }))
        except Exception:
            pass
        return

    try:
        await asyncio.gather(
            _ws_to_rudp(ws, rudp, state),
            _rudp_to_ws(ws, rudp, state),
            return_exceptions=True,
        )
    finally:
        await rudp.close()
        log.info("Session closed for %s", ws.remote_address)


async def main():
    async with websockets.serve(handle_ws, LISTEN_HOST, LISTEN_PORT):
        log.info("WS bridge listening on %s:%d", LISTEN_HOST, LISTEN_PORT)
        await asyncio.Future()   # run forever


if __name__ == '__main__':
    asyncio.run(main())
