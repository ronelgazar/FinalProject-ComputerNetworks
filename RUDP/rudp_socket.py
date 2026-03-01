"""
asyncio-based RUDP connection manager.

RudpSocket — one bidirectional RUDP connection.
RudpServer  — listens on a UDP port and accept()s new connections.
"""
from __future__ import annotations
import asyncio
import logging
import os
import time
from collections import OrderedDict
from typing import Dict, Optional, Tuple

from rudp_packet import (
    RudpPacket,
    SYN, ACK, FIN, DATA, RST, PING,
    HEADER_SIZE,
)

log = logging.getLogger(__name__)

WINDOW_SIZE    = 8
RETRANSMIT_MS  = 500
MAX_RETRIES    = 5
CONNECT_TIMEOUT= 5.0
RECV_TIMEOUT   = 30.0    # seconds before idle connection is considered dead


class RudpTimeout(Exception):
    pass

class RudpReset(Exception):
    pass


# ── RudpSocket ────────────────────────────────────────────────────────────────

class RudpSocket:
    """
    A single RUDP connection (over a shared UDP socket).
    Do NOT instantiate directly — use RudpServer.accept() or RudpSocket.connect().
    """

    def __init__(self, transport: asyncio.DatagramTransport,
                 remote_addr: Tuple[str, int],
                 loop: asyncio.AbstractEventLoop):
        self._transport  = transport
        self._remote     = remote_addr
        self._loop       = loop

        # Sequence numbers
        self._send_seq: int = 0      # next seq to send
        self._recv_seq: int = 0      # next expected seq from peer

        # Send buffer: seq → (packet, send_time, retry_count)
        self._send_buf: OrderedDict[int, Tuple[RudpPacket, float, int]] = OrderedDict()

        # Receive reorder buffer: seq → payload
        self._recv_buf: Dict[int, bytes] = {}

        # Delivery queue (in-order payloads)
        self._recv_queue: asyncio.Queue[bytes] = asyncio.Queue()

        # Events
        self._ack_event   = asyncio.Event()
        self._closed      = False
        self._close_event = asyncio.Event()

        # Retransmit task
        self._retransmit_task: Optional[asyncio.Task] = None

    # ── Internal send ─────────────────────────────────────────────────────
    def _send_raw(self, pkt: RudpPacket):
        data = pkt.to_bytes()
        self._transport.sendto(data, self._remote)

    def _make_pkt(self, flags: int, payload: bytes = b'') -> RudpPacket:
        return RudpPacket(
            seq=self._send_seq,
            ack=self._recv_seq,
            flags=flags,
            window=WINDOW_SIZE,
            payload=payload,
        )

    # ── Public API ────────────────────────────────────────────────────────

    async def send(self, data: bytes):
        """Send application data, waiting for window space if needed."""
        if self._closed:
            raise RudpReset("Socket closed")
        pkt = self._make_pkt(DATA | ACK, data)
        seq = self._send_seq
        self._send_seq += 1
        self._send_buf[seq] = (pkt, time.monotonic(), 0)
        self._send_raw(pkt)

        # Wait until this packet is ACK'd
        for _ in range(MAX_RETRIES + 1):
            if seq not in self._send_buf:
                return
            try:
                await asyncio.wait_for(self._ack_event.wait(),
                                       timeout=RETRANSMIT_MS / 1000)
            except asyncio.TimeoutError:
                pass
            self._ack_event.clear()
            if seq not in self._send_buf:
                return
            # Retransmit
            pkt, _, retries = self._send_buf[seq]
            if retries >= MAX_RETRIES:
                self._closed = True
                raise RudpTimeout(f"No ACK for seq={seq} after {MAX_RETRIES} retries")
            self._send_buf[seq] = (pkt, time.monotonic(), retries + 1)
            log.debug("Retransmit seq=%d (retry %d)", seq, retries + 1)
            self._send_raw(pkt)

        raise RudpTimeout("Max retries exceeded")

    async def recv(self) -> bytes:
        """Block until a complete in-order message arrives."""
        if self._closed and self._recv_queue.empty():
            raise RudpReset("Socket closed")
        try:
            return await asyncio.wait_for(self._recv_queue.get(),
                                          timeout=RECV_TIMEOUT)
        except asyncio.TimeoutError:
            raise RudpTimeout("recv timeout")

    async def close(self):
        if self._closed:
            return
        # Send FIN
        fin = self._make_pkt(FIN | ACK)
        self._send_raw(fin)
        self._closed = True
        self._close_event.set()

    # ── Called by RudpServer when a packet arrives for this connection ────

    def on_packet(self, pkt: RudpPacket):
        if pkt.is_rst():
            self._closed = True
            self._recv_queue.put_nowait(b'')   # unblock recv
            return

        if pkt.is_fin():
            self._closed = True
            # Send FIN-ACK
            fin_ack = self._make_pkt(FIN | ACK)
            self._send_raw(fin_ack)
            self._recv_queue.put_nowait(b'')
            return

        if pkt.is_ack():
            # Remove all send_buf entries with seq <= pkt.ack
            done = [s for s in self._send_buf if s < pkt.ack]
            for s in done:
                del self._send_buf[s]
            self._ack_event.set()

        if pkt.is_data() and pkt.payload:
            seq = pkt.seq
            if seq == self._recv_seq:
                # In-order delivery
                self._recv_queue.put_nowait(pkt.payload)
                self._recv_seq += 1
                # Deliver any buffered continuations
                while self._recv_seq in self._recv_buf:
                    self._recv_queue.put_nowait(self._recv_buf.pop(self._recv_seq))
                    self._recv_seq += 1
            elif seq > self._recv_seq:
                # Out-of-order — buffer it
                self._recv_buf[seq] = pkt.payload
            # else: duplicate, discard

            # Send cumulative ACK
            ack_pkt = RudpPacket(
                seq=self._send_seq,
                ack=self._recv_seq,
                flags=ACK,
                window=WINDOW_SIZE,
            )
            self._send_raw(ack_pkt)

        if pkt.is_ping():
            pong = self._make_pkt(ACK)
            self._send_raw(pong)


# ── RudpServer ────────────────────────────────────────────────────────────────

class _UdpProtocol(asyncio.DatagramProtocol):
    """asyncio DatagramProtocol that demuxes packets to RudpSocket instances."""

    def __init__(self, server: 'RudpServer'):
        self._server = server
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr: Tuple[str, int]):
        try:
            pkt = RudpPacket.from_bytes(data)
        except Exception as e:
            log.debug("Bad packet from %s: %s", addr, e)
            return
        self._server._dispatch(pkt, addr)

    def error_received(self, exc):
        log.warning("UDP error: %s", exc)


class RudpServer:
    """
    UDP listener that produces RudpSocket connections via accept().
    """

    def __init__(self, host: str = '0.0.0.0', port: int = 9000):
        self._host = host
        self._port = port
        self._connections: Dict[Tuple[str, int], RudpSocket] = {}
        self._accept_queue: asyncio.Queue[RudpSocket] = asyncio.Queue()
        self._transport: Optional[asyncio.DatagramTransport] = None
        self._protocol: Optional[_UdpProtocol] = None

    async def start(self):
        loop = asyncio.get_event_loop()
        self._transport, self._protocol = await loop.create_datagram_endpoint(
            lambda: _UdpProtocol(self),
            local_addr=(self._host, self._port),
        )
        log.info("RudpServer listening on %s:%d", self._host, self._port)

    async def accept(self) -> RudpSocket:
        return await self._accept_queue.get()

    def _dispatch(self, pkt: RudpPacket, addr: Tuple[str, int]):
        loop = asyncio.get_event_loop()
        conn = self._connections.get(addr)

        if pkt.is_syn() and not pkt.is_ack():
            # New connection handshake
            if conn is not None:
                return   # duplicate SYN — ignore
            conn = RudpSocket(self._transport, addr, loop)
            conn._recv_seq = pkt.seq + 1   # peer's ISN + 1
            self._connections[addr] = conn

            # Send SYN-ACK
            syn_ack = RudpPacket(
                seq=conn._send_seq,
                ack=conn._recv_seq,
                flags=SYN | ACK,
                window=WINDOW_SIZE,
            )
            conn._send_seq += 1
            conn._send_raw(syn_ack)
            # Enqueue for accept()
            self._accept_queue.put_nowait(conn)
            return

        if conn is None:
            # RST unknown connection
            rst = RudpPacket(seq=0, ack=0, flags=RST, window=0)
            self._transport.sendto(rst.to_bytes(), addr)
            return

        conn.on_packet(pkt)

        if conn._closed:
            self._connections.pop(addr, None)


# ── Client-side connect ───────────────────────────────────────────────────────

async def rudp_connect(host: str, port: int) -> RudpSocket:
    """
    Create a client-side RUDP connection via three-way handshake.
    Returns a connected RudpSocket.
    """
    loop = asyncio.get_event_loop()

    # We create a temporary server-like structure just to reuse _UdpProtocol
    class _ClientServer:
        def __init__(self):
            self._connections = {}
            self._syn_ack_event = asyncio.Event()
            self._conn: Optional[RudpSocket] = None

        def _dispatch(self, pkt: RudpPacket, addr):
            if self._conn and pkt.is_syn() and pkt.is_ack():
                self._conn._recv_seq = pkt.seq + 1
                # Send final ACK
                ack = RudpPacket(
                    seq=self._conn._send_seq,
                    ack=self._conn._recv_seq,
                    flags=ACK,
                    window=WINDOW_SIZE,
                )
                self._conn._send_raw(ack)
                self._syn_ack_event.set()
            elif self._conn:
                self._conn.on_packet(pkt)

    cs = _ClientServer()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _UdpProtocol(cs),
        remote_addr=(host, port),
    )

    conn = RudpSocket(transport, (host, port), loop)
    cs._conn = conn

    # Send SYN
    syn = RudpPacket(seq=conn._send_seq, ack=0, flags=SYN, window=WINDOW_SIZE)
    conn._send_seq += 1
    transport.sendto(syn.to_bytes())

    try:
        await asyncio.wait_for(cs._syn_ack_event.wait(), timeout=CONNECT_TIMEOUT)
    except asyncio.TimeoutError:
        transport.close()
        raise RudpTimeout("Connect timeout")

    return conn
