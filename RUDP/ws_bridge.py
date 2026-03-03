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

import websockets

from rudp_socket import rudp_connect, RudpTimeout, RudpReset

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [bridge] %(levelname)s %(message)s',
)
log = logging.getLogger(__name__)

LISTEN_HOST     = os.environ.get('BRIDGE_HOST', '0.0.0.0')
LISTEN_PORT     = int(os.environ.get('BRIDGE_PORT', '8081'))
RUDP_SERVER_HOST= os.environ.get('RUDP_SERVER_HOST', 'server.exam.lan')
RUDP_SERVER_PORT= int(os.environ.get('RUDP_SERVER_PORT', '9000'))
DNS_RESOLVER    = os.environ.get('DNS_RESOLVER', '10.99.0.2')

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


def _resolve_server() -> tuple[str, list[str]]:
    """
    Resolve RUDP_SERVER_HOST via the container's configured DNS (10.99.0.2).
    Returns (chosen_ip, all_ips).  getaddrinfo may return multiple A records
    (DNS round-robin load balancing); the OS picks one.
    """
    results = socket.getaddrinfo(RUDP_SERVER_HOST, RUDP_SERVER_PORT,
                                  socket.AF_INET, socket.SOCK_DGRAM)
    all_ips = list(dict.fromkeys(r[4][0] for r in results))   # unique, ordered
    ip = results[0][4][0]
    log.info("Resolved %s → %s  (all: %s)", RUDP_SERVER_HOST, ip, all_ips)
    return ip, all_ips


async def _ws_to_rudp(ws, rudp):
    """Forward WebSocket frames → RUDP, injecting RTT into 'hello' messages."""
    async for message in ws:
        try:
            if isinstance(message, str):
                # Intercept 'hello' to inject current RUDP RTT measurement
                try:
                    obj = json.loads(message)
                    if obj.get('type') == 'hello':
                        obj['rtt_ms'] = round(rudp.rtt_ms, 2)
                        obj['handshake_rtt_ms'] = round(rudp.handshake_rtt_ms, 2)
                        message = json.dumps(obj)
                        log.info("Injected rtt_ms=%.1f into hello", rudp.rtt_ms)
                except Exception:
                    pass   # not JSON or not hello — forward as-is
                await rudp.send(message.encode('utf-8'))
            else:
                await rudp.send(message)
        except (RudpTimeout, RudpReset) as e:
            log.warning("RUDP send error: %s", e)
            break


async def _rudp_to_ws(ws, rudp):
    """
    Forward RUDP messages → WebSocket.

    Two layers of delay are applied in sequence:
    1. Software netem (professor-controlled): delay + jitter + loss.
       Written to /tmp/netem_delay.json by the professor dashboard.
    2. Synchronized delivery (server-controlled): deliver_delay_ms.
       Ensures all clients receive exam packets at the same wall-clock time.
    """
    while not rudp._closed:
        try:
            data = await rudp.recv()
            if not data:
                break
            text = data.decode('utf-8')
            try:
                obj = json.loads(text)

                # 1. Software netem — applied first (simulates network path)
                dropped = await _netem_delay()
                if dropped:
                    log.info("netem: dropped %s packet (loss simulation)",
                             obj.get('type', '?'))
                    continue

                # 2. Synchronized delivery — applied second (server-stamped)
                sync_delay_ms = obj.get('deliver_delay_ms', 0)
                if sync_delay_ms > 0:
                    log.info("Holding %s for %.0f ms (sync delay)",
                             obj.get('type', '?'), sync_delay_ms)
                    await asyncio.sleep(sync_delay_ms / 1000.0)

                await ws.send(json.dumps(obj))
            except (ValueError, TypeError):
                # Not JSON — still apply netem, then forward raw
                dropped = await _netem_delay()
                if not dropped:
                    await ws.send(text)
        except (RudpTimeout, RudpReset) as e:
            log.warning("RUDP recv error: %s", e)
            break
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

        # Tell the browser which server was selected via DNS and measured RTT
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
                "type": "error",
                "message": f"Bridge could not connect to exam server: {e}",
            }))
        except Exception:
            pass
        return

    try:
        await asyncio.gather(
            _ws_to_rudp(ws, rudp),
            _rudp_to_ws(ws, rudp),
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
