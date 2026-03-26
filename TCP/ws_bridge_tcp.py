"""
WebSocket ↔ TCP exam server bridge.

Runs inside each client container.
nginx proxies /ws-tcp-sync   → localhost:8082
              /ws-tcp-nosync → localhost:8083

SYNC_ENABLED=1: same two-layer delivery as ws_bridge.py
                (netem + self-correcting hold on server_sent_at_ms).
SYNC_ENABLED=0: forward immediately, no delay logic.

Framing: JSON + newline on the TCP side.
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import pathlib
import random
import socket
import time

import websockets

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [tcp-bridge] %(levelname)s %(message)s',
)
log = logging.getLogger(__name__)

LISTEN_HOST      = os.environ.get('BRIDGE_HOST', '0.0.0.0')
LISTEN_PORT      = int(os.environ.get('BRIDGE_PORT', '8082'))
TCP_SERVER_HOST  = os.environ.get('TCP_SERVER_HOST', 'tcp-sync.exam.lan')
TCP_SERVER_PORT  = int(os.environ.get('TCP_SERVER_PORT', '9001'))
DNS_RESOLVER     = os.environ.get('DNS_RESOLVER', '10.99.0.2')
SYNC_ENABLED     = os.environ.get('SYNC_ENABLED', '1') == '1'

SYNC_PACKET_TYPES = {'exam_resp', 'schedule_resp'}
JITTER_BUFFER_MS  = 5

# ── Software netem ─────────────────────────────────────────────────────────────
_NETEM_FILE  = pathlib.Path('/tmp/netem_delay.json')
_netem       = {"delay_ms": 0, "jitter_ms": 0, "loss_pct": 0.0}
_netem_mtime = 0.0


def _reload_netem() -> None:
    global _netem, _netem_mtime
    try:
        mt = _NETEM_FILE.stat().st_mtime
        if mt != _netem_mtime:
            _netem = json.loads(_NETEM_FILE.read_text())
            _netem_mtime = mt
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Could not read netem file: %s", e)


async def _netem_delay() -> bool:
    _reload_netem()
    loss_pct  = float(_netem.get("loss_pct", 0))
    delay_ms  = float(_netem.get("delay_ms", 0))
    jitter_ms = float(_netem.get("jitter_ms", 0))

    if loss_pct > 0 and random.uniform(0, 100) < loss_pct:
        return True

    if jitter_ms > 0:
        delay_ms += random.uniform(-jitter_ms, jitter_ms)
    if delay_ms > 0:
        await asyncio.sleep(max(0.0, delay_ms) / 1000.0)
    return False


def _resolve_server() -> str:
    results = socket.getaddrinfo(TCP_SERVER_HOST, TCP_SERVER_PORT,
                                  socket.AF_INET, socket.SOCK_STREAM)
    ip = results[0][4][0]
    log.info("Resolved %s → %s", TCP_SERVER_HOST, ip)
    return ip


# ── WS → TCP ──────────────────────────────────────────────────────────────────

async def _ws_to_tcp(ws, writer: asyncio.StreamWriter, rtt_ms: float):
    """Forward WebSocket frames → TCP, injecting rtt_ms into hello."""
    try:
        async for message in ws:
            if isinstance(message, str):
                try:
                    obj = json.loads(message)
                    if obj.get('type') == 'hello':
                        obj['rtt_ms'] = round(rtt_ms, 2)
                        message = json.dumps(obj)
                        log.info("Injected rtt_ms=%.1f into hello", rtt_ms)
                except Exception:
                    pass
                writer.write(message.encode('utf-8') + b'\n')
            else:
                writer.write(message + b'\n')
            await writer.drain()
    except Exception as e:
        log.warning("WS→TCP error: %s", e)
    finally:
        try:
            writer.close()
        except Exception:
            pass


# ── TCP → WS ──────────────────────────────────────────────────────────────────

import time as _time_mod


async def _tcp_to_ws(ws, reader: asyncio.StreamReader, state: dict):
    """
    Forward TCP messages → WebSocket.

    SYNC_ENABLED=1: two-layer delivery (netem + self-correcting hold).
    SYNC_ENABLED=0: forward immediately.
    """
    try:
        while True:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=300)
            except asyncio.TimeoutError:
                break
            if not line:
                break

            text = line.decode('utf-8').strip()
            if not text:
                continue

            try:
                obj = json.loads(text)
                pkt_type = obj.get('type', '?')

                if SYNC_ENABLED:
                    # Layer 1: software netem
                    dropped = await _netem_delay()
                    if dropped:
                        log.info("netem: dropped %s", pkt_type)
                        continue

                    # Layer 2: self-correcting synchronized delivery
                    extra: dict = {}   # fields to inject after the hold
                    if (pkt_type in SYNC_PACKET_TYPES
                            and 'server_sent_at_ms' in obj
                            and state['max_rtt_ms'] > 0):
                        # Always pick up the latest max_rtt/my_rtt from the
                        # server — exam_resp carries fresh values computed
                        # after all clients registered Phase-3 RTT samples.
                        if 'max_rtt_ms' in obj:
                            state['max_rtt_ms'] = float(obj['max_rtt_ms'])
                        if 'my_rtt_ms' in obj:
                            state['my_rtt_ms']  = float(obj['my_rtt_ms'])
                        if pkt_type == 'schedule_resp':
                            log.info("Sync state: max_rtt=%.1f my_rtt=%.1f",
                                     state['max_rtt_ms'], state['my_rtt_ms'])

                        target_ms   = (obj['server_sent_at_ms']
                                       + state['max_rtt_ms'] / 2
                                       + JITTER_BUFFER_MS)
                        hold_ms     = target_ms - _time_mod.time() * 1000
                        actual_hold = max(0.0, hold_ms)

                        if actual_hold > 0:
                            log.info("Sync hold %.1f ms for %s", actual_hold, pkt_type)
                            await asyncio.sleep(actual_hold / 1000.0)

                        extra['bridge_forwarded_at_ms'] = int(_time_mod.time() * 1000)
                        extra['sync_hold_ms']           = round(actual_hold, 2)
                        extra['sync_target_ms']         = round(target_ms, 2)

                    elif 'deliver_delay_ms' in obj and obj['deliver_delay_ms'] > 0:
                        await asyncio.sleep(obj['deliver_delay_ms'] / 1000.0)
                        extra['bridge_forwarded_at_ms'] = int(_time_mod.time() * 1000)
                        extra['sync_hold_ms']           = obj['deliver_delay_ms']

                    if pkt_type == 'schedule_resp':
                        state['max_rtt_ms'] = float(obj.get('max_rtt_ms', state['max_rtt_ms']))
                        state['my_rtt_ms']  = float(obj.get('my_rtt_ms', state['my_rtt_ms']))

                    # ── Hot forward: avoid re-serialising the 33 KB exam payload ──
                    # json.dumps(obj) on an exam_resp takes ~27 ms.  With k clients
                    # sharing one bridge container, they serialise sequentially on
                    # the event loop → k×27 ms spread.
                    # Fix: inject the small `extra` fields directly into the raw
                    # text via string concatenation; the heavy exam payload passes
                    # through unmodified.
                    if extra and pkt_type == 'exam_resp':
                        # text ends with '}' (stripped above).  Append extra fields.
                        suffix = ''.join(
                            f',"{k}":{v}' if isinstance(v, (int, float)) else f',"{k}":{json.dumps(v)}'
                            for k, v in extra.items()
                        )
                        await ws.send(text[:-1] + suffix + '}')
                    elif extra:
                        # Small packets: safe to update obj + re-dump (< 200 bytes)
                        obj.update(extra)
                        await ws.send(json.dumps(obj))
                    else:
                        await ws.send(json.dumps(obj))
                else:
                    await ws.send(json.dumps(obj))

            except (ValueError, TypeError):
                await ws.send(text)

    except websockets.exceptions.ConnectionClosed:
        pass   # browser closed tab / navigated away — not an error
    except Exception as e:
        log.warning("TCP→WS error: %s", e)


# ── WebSocket handler ─────────────────────────────────────────────────────────

async def handle_ws(ws):
    log.info("WebSocket connection from %s", ws.remote_address)

    # Measure TCP connect time as a proxy for RTT
    t0 = _time_mod.time()
    try:
        loop = asyncio.get_running_loop()
        server_ip = await loop.run_in_executor(None, _resolve_server)
        reader, writer = await asyncio.open_connection(server_ip, TCP_SERVER_PORT)
        rtt_ms = (_time_mod.time() - t0) * 1000
        log.info("TCP connected to %s:%d  connect_time=%.1fms",
                 server_ip, TCP_SERVER_PORT, rtt_ms)
    except Exception as e:
        log.error("Failed to connect TCP server: %s", e)
        try:
            await ws.send(json.dumps({
                "type":    "error",
                "message": f"Bridge could not connect to TCP exam server: {e}",
            }))
        except Exception:
            pass
        return

    state = {
        'max_rtt_ms': rtt_ms * 2 if SYNC_ENABLED else 0.0,
        'my_rtt_ms':  rtt_ms,
    }

    await ws.send(json.dumps({
        "type":         "connection_info",
        "dns_hostname": TCP_SERVER_HOST,
        "dns_resolver": DNS_RESOLVER,
        "resolved_ip":  server_ip,
        "all_ips":      [server_ip],
        "rtt_ms":       round(rtt_ms, 2),
        "transport":    "tcp-sync" if SYNC_ENABLED else "tcp-nosync",
    }))

    try:
        await asyncio.gather(
            _ws_to_tcp(ws, writer, rtt_ms),
            _tcp_to_ws(ws, reader, state),
            return_exceptions=True,
        )
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        log.info("Session closed for %s", ws.remote_address)


async def main():
    mode = "SYNC" if SYNC_ENABLED else "NO-SYNC (baseline)"
    async with websockets.serve(handle_ws, LISTEN_HOST, LISTEN_PORT):
        log.info("TCP WS bridge on %s:%d → %s:%d  [%s]",
                 LISTEN_HOST, LISTEN_PORT, TCP_SERVER_HOST, TCP_SERVER_PORT, mode)
        await asyncio.Future()


if __name__ == '__main__':
    asyncio.run(main())
