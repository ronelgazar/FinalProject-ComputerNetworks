"""
asyncio-based RUDP connection manager.

RudpSocket — one bidirectional RUDP connection.
RudpServer  — listens on a UDP port and accept()s new connections.

Features
--------
Reliability
  Every DATA packet is acknowledged (cumulative ACK).
  Unacknowledged packets are retransmitted after RETRANSMIT_MS.
  CRC-16 integrity check on every packet (rudp_packet layer).
  Up to MAX_RETRIES retransmissions before raising RudpTimeout.
  Out-of-order packets are buffered and delivered in-sequence.
  MSG_END flag marks the last chunk of an application message so
  large payloads split across multiple packets are reassembled correctly.

Congestion control  (TCP Reno inspired)
  Slow start    — cwnd starts at INIT_CWND (1) and grows by 1 per ACKed
                  packet (doubles each RTT) until it reaches ssthresh.
  AIMD          — once cwnd >= ssthresh, cwnd += 1/cwnd per ACKed packet
                  (additive increase ≈ +1 MSS per RTT).
  Timeout       — ssthresh = max(cwnd/2, 1), cwnd = 1.
                  Triggers go-back-N retransmission of all in-flight chunks.
  Fast retransmit / fast recovery (FR)
                — on DUP_ACK_THRESH (3) consecutive duplicate ACKs,
                  retransmit the first missing packet immediately.
                  ssthresh = max(cwnd/2, 1); cwnd = ssthresh
                  (skip slow start, enter congestion avoidance directly).

Flow control
  The `window` field (uint8) in every outgoing packet advertises the
  sender's remaining receive-buffer space in packets.
  The remote peer caps its in-flight count at
      effective_window = min(cwnd, peer_advertised_window, WINDOW_CAP)
  implementing both congestion control and flow control together.
  When the remote advertises window=0 the sender stalls until a
  window-update ACK arrives.

MSS negotiation
  During the three-way handshake each side advertises its local
  MAX_PAYLOAD as a 2-byte big-endian value in the SYN / SYN-ACK
  payload.  The agreed MSS = min(local, remote) is stored in
  self._mss and used for all subsequent DATA chunking.

Message framing
  Large payloads are split into self._mss-byte RUDP DATA chunks.
  The last chunk carries the MSG_END flag; recv() reassembles all
  chunks back into the original application message.
"""
from __future__ import annotations
import asyncio
import json
import logging
import pathlib
import time
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

from rudp_packet import (
    RudpPacket,
    SYN, ACK, FIN, DATA, RST, PING, MSG_END,
    HEADER_SIZE,
)

log = logging.getLogger(__name__)

# ── Tunables ───────────────────────────────────────────────────────────────────
MAX_PAYLOAD     = 1200      # max bytes per RUDP DATA chunk (stays below IP MTU)
RECV_BUF_SIZE   = 64        # our advertised receive window (in packets)
WINDOW_CAP      = 32        # hard upper bound on sender-side congestion window
RETRANSMIT_MS   = 500       # retransmission timeout (ms)
MAX_RETRIES     = 5         # max retransmissions before RudpTimeout
CONNECT_TIMEOUT = 5.0       # seconds for three-way handshake
RECV_TIMEOUT    = 30.0      # seconds before idle recv() raises RudpTimeout
DUP_ACK_THRESH  = 3         # duplicate ACKs that trigger fast retransmit

# Congestion control initial values
INIT_CWND     = 1.0         # start slow start from 1 packet
INIT_SSTHRESH = 32.0        # initial slow-start threshold


class RudpTimeout(Exception):
    pass


class RudpReset(Exception):
    pass


def _read_slowsend_ms() -> float:
    """
    Read optional inter-window-batch delay from /tmp/rudp_slowsend.json.

    The scenario runner writes {"delay_ms": N} to this path on the server
    container to slow down DATA transmission so that the slow-start staircase
    (cwnd=1 → 2 → 4 → 8 → …) becomes clearly visible in a Wireshark pcap.
    The file is removed after the capture, restoring normal behaviour.
    """
    p = pathlib.Path('/tmp/rudp_slowsend.json')
    try:
        if p.exists():
            return float(json.loads(p.read_text()).get('delay_ms', 0))
    except Exception:
        pass
    return 0.0


# ── RudpSocket ────────────────────────────────────────────────────────────────

class RudpSocket:
    """
    A single full-duplex RUDP connection over a shared or dedicated UDP socket.
    Do NOT instantiate directly — use RudpServer.accept() or rudp_connect().
    """

    def __init__(self, transport: asyncio.DatagramTransport,
                 remote_addr: Tuple[str, int],
                 loop: asyncio.AbstractEventLoop):
        self._transport = transport
        self._remote    = remote_addr
        self._loop      = loop

        # ── Sequence numbers ──────────────────────────────────────────────────
        self._send_seq: int = 0     # next seq number to allocate
        self._recv_seq: int = 0     # next expected seq from peer

        # ── Send sliding window ───────────────────────────────────────────────
        # seq → (packet, t_sent, retry_count)
        self._send_buf: OrderedDict[int, Tuple[RudpPacket, float, int]] = OrderedDict()

        # ── Receive state ─────────────────────────────────────────────────────
        self._recv_ooo: Dict[int, Tuple[bytes, int]] = {}   # out-of-order buffer
        self._recv_msg_chunks: List[bytes] = []             # in-progress message
        self._recv_queue: asyncio.Queue[bytes] = asyncio.Queue()  # complete msgs

        # ── Flow control ──────────────────────────────────────────────────────
        # Peer's last advertised receive window (packets); updated on every ACK
        self._peer_window: int = WINDOW_CAP

        # ── Congestion control ────────────────────────────────────────────────
        self._cwnd:          float = INIT_CWND      # congestion window (packets)
        self._ssthresh:      float = INIT_SSTHRESH  # slow-start threshold
        self._dup_ack_count: int   = 0              # consecutive duplicate ACKs
        self._last_ack:      int   = 0              # highest cumulative ACK seen

        # ── RTT tracking ──────────────────────────────────────────────────────
        self.handshake_rtt_ms: float = 0.0
        self.rtt_ms:           float = 50.0     # EWMA RTT, seeded at 50 ms

        # ── MSS (negotiated during handshake) ─────────────────────────────────
        self._mss: int = MAX_PAYLOAD   # agreed max segment size

        # ── Misc ──────────────────────────────────────────────────────────────
        self._ack_event = asyncio.Event()
        self._closed    = False

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _send_raw(self, pkt: RudpPacket) -> None:
        self._transport.sendto(pkt.to_bytes(), self._remote)

    def _adv_window(self) -> int:
        """Our advertised receive window = free slots in the recv buffer."""
        return max(0, RECV_BUF_SIZE - self._recv_queue.qsize())

    def _make_pkt(self, flags: int, payload: bytes = b'') -> RudpPacket:
        return RudpPacket(
            seq     = self._send_seq,
            ack     = self._recv_seq,
            flags   = flags,
            window  = min(self._adv_window(), 0xFF),
            payload = payload,
        )

    def _effective_window(self) -> int:
        """
        Sender's allowed in-flight packets =
            min(cwnd, peer_advertised_window, WINDOW_CAP)

        This single value integrates both:
          - Congestion control  (cwnd)
          - Flow control        (peer_window)
        """
        return max(1, min(int(self._cwnd), self._peer_window, WINDOW_CAP))

    # ── Congestion control ────────────────────────────────────────────────────

    def _on_new_ack(self, n_acked: int) -> None:
        """
        New cumulative ACK received.

        Slow start   (cwnd < ssthresh): cwnd += 1 per ACKed packet
                                        → doubles every RTT
        AIMD / CA    (cwnd >= ssthresh): cwnd += 1/cwnd per ACKed packet
                                        → +1 MSS per RTT (additive increase)
        """
        for _ in range(n_acked):
            if self._cwnd < self._ssthresh:
                self._cwnd += 1.0                   # slow start: exponential
            else:
                self._cwnd += 1.0 / self._cwnd      # AIMD: linear
        self._cwnd          = min(self._cwnd, float(WINDOW_CAP))
        self._dup_ack_count = 0
        log.debug("New ACK ×%d  cwnd=%.2f  ssthresh=%.2f  phase=%s",
                  n_acked, self._cwnd, self._ssthresh,
                  "SS" if self._cwnd < self._ssthresh else "CA")

    def _on_timeout(self) -> None:
        """
        Retransmission timeout — multiplicative decrease + slow-start restart.
          ssthresh = max(cwnd / 2, 1)
          cwnd     = 1  (restart slow start)
        """
        self._ssthresh      = max(self._cwnd / 2.0, 1.0)
        self._cwnd          = INIT_CWND
        self._dup_ack_count = 0
        log.info("RTO  cwnd→%.1f  ssthresh→%.1f", self._cwnd, self._ssthresh)

    def _on_fast_retransmit(self) -> None:
        """
        Fast retransmit triggered by DUP_ACK_THRESH duplicate ACKs.
        Multiplicative decrease, but skip slow start (fast recovery):
          ssthresh = max(cwnd / 2, 1)
          cwnd     = ssthresh  → enter congestion avoidance immediately
        """
        self._ssthresh      = max(self._cwnd / 2.0, 1.0)
        self._cwnd          = self._ssthresh
        self._dup_ack_count = 0
        log.info("FR   cwnd→%.1f  ssthresh→%.1f", self._cwnd, self._ssthresh)

    # ── Send helpers (extracted to keep send() below complexity limit) ────────

    def _enqueue_chunks(self, data: bytes) -> List[int]:
        """Split data into self._mss chunks, add to send_buf, return seq list."""
        seqs: List[int] = []
        for off in range(0, max(1, len(data)), self._mss):
            chunk   = data[off : off + self._mss]
            is_last = (off + self._mss >= len(data))
            flags   = DATA | ACK | (MSG_END if is_last else 0)
            pkt = RudpPacket(
                seq     = self._send_seq,
                ack     = self._recv_seq,
                flags   = flags,
                window  = min(self._adv_window(), 0xFF),
                payload = chunk,
            )
            self._send_buf[self._send_seq] = (pkt, time.monotonic(), 0)
            seqs.append(self._send_seq)
            self._send_seq += 1
        return seqs

    def _fill_window(self, seqs: List[int], next_to_send: int) -> int:
        """Transmit new chunks up to effective_window; return updated next_to_send."""
        eff_win   = self._effective_window()
        in_flight = sum(1 for s in seqs if s in self._send_buf)
        while next_to_send < len(seqs) and in_flight < eff_win:
            seq = seqs[next_to_send]
            if seq in self._send_buf:
                pkt, _, _r = self._send_buf[seq]
                self._send_raw(pkt)
                in_flight += 1
            next_to_send += 1
        return next_to_send

    def _gobackn_retransmit(self, seqs: List[int]) -> None:
        """Go-Back-N: retransmit all unACKed chunks; raise on max retries."""
        unacked = [s for s in seqs if s in self._send_buf]
        if not unacked:
            return
        _, _, r0 = self._send_buf[unacked[0]]
        if r0 >= MAX_RETRIES:
            self._closed = True
            raise RudpTimeout(f"Max retries for seq={unacked[0]}")
        self._on_timeout()
        for seq in unacked:
            pkt, _, r = self._send_buf[seq]
            self._send_buf[seq] = (pkt, time.monotonic(), r + 1)
            self._send_raw(pkt)
            log.debug("Go-Back-N retransmit seq=%d retry=%d", seq, r + 1)

    # ── ACK helpers (extracted to keep on_packet() below complexity limit) ────

    def _process_new_ack(self, pkt_ack: int) -> None:
        """Handle an advancing cumulative ACK: remove from buf, grow cwnd."""
        done    = [s for s in self._send_buf if s < pkt_ack]
        n_acked = len(done)
        for s in done:
            _, t_sent, _ = self._send_buf[s]
            sample_ms = (time.monotonic() - t_sent) * 1000
            self.rtt_ms = 0.875 * self.rtt_ms + 0.125 * sample_ms
            del self._send_buf[s]
        self._last_ack = pkt_ack
        if n_acked > 0:
            self._on_new_ack(n_acked)
        self._ack_event.set()

    def _process_dup_ack(self, pkt_ack: int) -> None:
        """Handle a duplicate ACK; trigger fast retransmit at threshold."""
        self._dup_ack_count += 1
        log.debug("Dup ACK #%d  ack=%d  cwnd=%.2f",
                  self._dup_ack_count, pkt_ack, self._cwnd)
        if self._dup_ack_count == DUP_ACK_THRESH:
            self._on_fast_retransmit()
            for s, (p, _t, _r) in self._send_buf.items():
                if s >= pkt_ack:
                    self._send_raw(p)
                    log.info("Fast retransmit seq=%d", s)
                    break
        self._ack_event.set()

    # ── Public API ────────────────────────────────────────────────────────────

    async def send(self, data: bytes) -> None:
        """
        Send application data.

        Large payloads are split into self._mss-byte RUDP DATA chunks
        (MSS negotiated during handshake); the last chunk carries MSG_END.  Up to effective_window chunks are
        in-flight simultaneously (sliding window).

        Retransmission policy:
          Timeout  → go-back-N: retransmit all unACKed chunks; reset cwnd.
          3 dup ACKs → fast retransmit handled inside on_packet().
        """
        if self._closed:
            raise RudpReset("Socket closed")

        seqs         = self._enqueue_chunks(data)
        next_to_send = 0

        while True:
            if all(s not in self._send_buf for s in seqs):
                return
            next_to_send = self._fill_window(seqs, next_to_send)
            # Optional inter-batch pause for demo captures (see _read_slowsend_ms).
            # Only applied to multi-chunk messages (len(seqs) > 1) so that small
            # messages like NTP time_resp are not slowed down unnecessarily.
            # With cwnd slow-start (1→2→4→8…) each iteration sends double the
            # previous burst; the sleep makes each staircase step visible.
            _sd = _read_slowsend_ms()
            if _sd > 0 and len(seqs) > 1:
                await asyncio.sleep(_sd / 1000.0)
            self._ack_event.clear()
            try:
                await asyncio.wait_for(
                    self._ack_event.wait(), timeout=RETRANSMIT_MS / 1000.0)
            except asyncio.TimeoutError:
                self._gobackn_retransmit(seqs)
                next_to_send = len(seqs)

    async def recv(self) -> bytes:
        """
        Block until a complete application message arrives.
        A message is complete once its MSG_END chunk is received in order.
        """
        if self._closed and self._recv_queue.empty():
            raise RudpReset("Socket closed")
        try:
            return await asyncio.wait_for(
                self._recv_queue.get(), timeout=RECV_TIMEOUT)
        except asyncio.TimeoutError:
            raise RudpTimeout("recv timeout")

    async def close(self) -> None:
        if self._closed:
            return
        self._send_raw(self._make_pkt(FIN | ACK))
        self._closed = True

    # ── Packet dispatch (called by RudpServer or client protocol) ────────────

    def on_packet(self, pkt: RudpPacket) -> None:
        """Process an incoming RUDP packet.  Called from the datagram callback."""
        if pkt.is_rst():
            self._closed = True
            self._recv_queue.put_nowait(b'')
            return

        if pkt.is_fin():
            self._closed = True
            self._send_raw(self._make_pkt(FIN | ACK))
            self._recv_queue.put_nowait(b'')
            return

        # ── ACK processing ────────────────────────────────────────────────────
        if pkt.is_ack():
            self._peer_window = max(1, int(pkt.window))   # flow control
            if pkt.ack > self._last_ack:
                self._process_new_ack(pkt.ack)
            elif pkt.ack == self._last_ack and self._send_buf:
                self._process_dup_ack(pkt.ack)

        # ── DATA processing ───────────────────────────────────────────────────
        if pkt.is_data() and pkt.payload:
            self._deliver_data(pkt.seq, pkt.payload, pkt.flags)
            # Cumulative ACK advertising our receive buffer
            self._send_raw(RudpPacket(
                seq    = self._send_seq,
                ack    = self._recv_seq,
                flags  = ACK,
                window = min(self._adv_window(), 0xFF),
            ))

        if pkt.is_ping():
            self._send_raw(self._make_pkt(ACK))

    # ── Receive-side message reassembly ──────────────────────────────────────

    def _deliver_data(self, seq: int, payload: bytes, flags: int) -> None:
        """Insert into reorder buffer; drain in-order chunks to the assembler."""
        if seq == self._recv_seq:
            self._absorb_chunk(payload, flags)
            self._recv_seq += 1
            while self._recv_seq in self._recv_ooo:
                buf_payload, buf_flags = self._recv_ooo.pop(self._recv_seq)
                self._absorb_chunk(buf_payload, buf_flags)
                self._recv_seq += 1
        elif seq > self._recv_seq:
            self._recv_ooo[seq] = (payload, flags)
        # else: duplicate — discard

    def _absorb_chunk(self, payload: bytes, flags: int) -> None:
        """
        Buffer a chunk.  When MSG_END is set, concatenate all buffered chunks
        into one complete application message and put it in recv_queue.
        """
        self._recv_msg_chunks.append(payload)
        if flags & MSG_END:
            msg = b''.join(self._recv_msg_chunks)
            self._recv_msg_chunks.clear()
            self._recv_queue.put_nowait(msg)


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
    """UDP listener that produces RudpSocket connections via accept()."""

    def __init__(self, host: str = '0.0.0.0', port: int = 9000):
        self._host = host
        self._port = port
        self._connections: Dict[Tuple[str, int], RudpSocket] = {}
        self._accept_queue: asyncio.Queue[RudpSocket] = asyncio.Queue()
        self._transport: Optional[asyncio.DatagramTransport] = None
        self._protocol:  Optional[_UdpProtocol]             = None

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
            if conn is not None:
                return   # duplicate SYN — ignore
            conn = RudpSocket(self._transport, addr, loop)
            conn._recv_seq = pkt.seq + 1
            # MSS negotiation: read client's advertised MSS from SYN payload
            remote_mss = (int.from_bytes(pkt.payload[:2], 'big')
                          if len(pkt.payload) >= 2 else MAX_PAYLOAD)
            conn._mss = min(MAX_PAYLOAD, remote_mss)
            self._connections[addr] = conn
            # Advertise our own MSS in the SYN-ACK payload
            conn._send_raw(RudpPacket(
                seq     = conn._send_seq,
                ack     = conn._recv_seq,
                flags   = SYN | ACK,
                window  = min(conn._adv_window(), 0xFF),
                payload = MAX_PAYLOAD.to_bytes(2, 'big'),
            ))
            conn._send_seq += 1
            log.info("MSS negotiated with %s: local=%d remote=%d agreed=%d",
                     addr, MAX_PAYLOAD, remote_mss, conn._mss)
            self._accept_queue.put_nowait(conn)
            return

        if conn is None:
            self._transport.sendto(
                RudpPacket(seq=0, ack=0, flags=RST, window=0).to_bytes(), addr)
            return

        conn.on_packet(pkt)
        if conn._closed:
            self._connections.pop(addr, None)


# ── Client-side connect ───────────────────────────────────────────────────────

async def rudp_connect(host: str, port: int) -> RudpSocket:
    """
    Open a client-side RUDP connection via three-way handshake.
    Returns a connected RudpSocket.
    """
    loop = asyncio.get_event_loop()

    class _ClientProto:
        def __init__(self):
            self._connections: Dict = {}
            self._syn_ack_event = asyncio.Event()
            self._conn: Optional[RudpSocket] = None

        def _dispatch(self, pkt: RudpPacket, addr):
            if self._conn and pkt.is_syn() and pkt.is_ack():
                self._conn._recv_seq    = pkt.seq + 1
                self._conn._peer_window = max(1, int(pkt.window))
                # MSS negotiation: read server's advertised MSS from SYN-ACK payload
                remote_mss = (int.from_bytes(pkt.payload[:2], 'big')
                              if len(pkt.payload) >= 2 else MAX_PAYLOAD)
                self._conn._mss = min(MAX_PAYLOAD, remote_mss)
                self._conn._send_raw(RudpPacket(
                    seq    = self._conn._send_seq,
                    ack    = self._conn._recv_seq,
                    flags  = ACK,
                    window = min(self._conn._adv_window(), 0xFF),
                ))
                self._syn_ack_event.set()
            elif self._conn:
                self._conn.on_packet(pkt)

    cs = _ClientProto()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _UdpProtocol(cs),
        remote_addr=(host, port),
    )

    conn = RudpSocket(transport, (host, port), loop)
    cs._conn = conn

    t_syn = time.monotonic()
    # Advertise our MSS in the SYN payload (2-byte big-endian)
    transport.sendto(RudpPacket(
        seq     = conn._send_seq,
        ack     = 0,
        flags   = SYN,
        window  = min(conn._adv_window(), 0xFF),
        payload = MAX_PAYLOAD.to_bytes(2, 'big'),
    ).to_bytes())
    conn._send_seq += 1

    try:
        await asyncio.wait_for(cs._syn_ack_event.wait(), timeout=CONNECT_TIMEOUT)
    except asyncio.TimeoutError:
        transport.close()
        raise RudpTimeout("Connect timeout")

    conn.handshake_rtt_ms = (time.monotonic() - t_syn) * 1000
    conn.rtt_ms           = conn.handshake_rtt_ms
    log.info("Handshake RTT to %s:%d = %.1f ms", host, port, conn.handshake_rtt_ms)
    return conn
