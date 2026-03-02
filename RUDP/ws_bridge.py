"""
WebSocket ↔ RUDP bridge.

Runs inside each client container as a local WebSocket server on port 8081.
nginx proxies /ws → localhost:8081.

For each browser WebSocket session, it opens one RUDP connection to the
exam backend (resolved via DNS: server.exam.lan → 10.99.0.20/21/22).
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
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


def _resolve_server() -> str:
    """
    Resolve RUDP_SERVER_HOST via the container's configured DNS (10.99.0.2).
    getaddrinfo returns multiple A records; Python picks one (OS round-robin).
    """
    results = socket.getaddrinfo(RUDP_SERVER_HOST, RUDP_SERVER_PORT,
                                  socket.AF_INET, socket.SOCK_DGRAM)
    ip = results[0][4][0]
    log.info("Resolved %s → %s", RUDP_SERVER_HOST, ip)
    return ip


async def _ws_to_rudp(ws, rudp):
    """Forward WebSocket frames → RUDP."""
    async for message in ws:
        try:
            if isinstance(message, str):
                await rudp.send(message.encode('utf-8'))
            else:
                await rudp.send(message)
        except (RudpTimeout, RudpReset) as e:
            log.warning("RUDP send error: %s", e)
            break


async def _rudp_to_ws(ws, rudp):
    """Forward RUDP messages → WebSocket."""
    while not rudp._closed:
        try:
            data = await rudp.recv()
            if not data:
                break
            await ws.send(data.decode('utf-8'))
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
        server_ip = await loop.run_in_executor(None, _resolve_server)
        rudp = await rudp_connect(server_ip, RUDP_SERVER_PORT)
        log.info("RUDP connected to %s:%d", server_ip, RUDP_SERVER_PORT)
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
