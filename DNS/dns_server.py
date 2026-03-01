"""
Base DNS server — UDP port 53 with optional TCP fallback.
Subclasses implement handle_query().
"""
from __future__ import annotations
import socket
import threading
import logging
import struct
from abc import ABC, abstractmethod
from typing import List, Tuple

from dns_packet import (
    DnsHeader, DnsQuestion, DnsRR,
    parse_query, build_response,
    RCODE_SERVFAIL, RCODE_NOTIMP,
)

log = logging.getLogger(__name__)
UDP_MAX = 512          # RFC 1035 §2.3.4
TCP_TIMEOUT = 5.0


class DnsServer(ABC):
    def __init__(self, host: str = '0.0.0.0', port: int = 53):
        self.host = host
        self.port = port

    # ── Subclass contract ──────────────────────────────────────────────────
    @abstractmethod
    def handle_query(
        self,
        questions: List[DnsQuestion],
        is_rd: bool,
    ) -> Tuple[List[DnsRR], List[DnsRR], List[DnsRR], bool]:
        """
        Return (answers, authority, additional, aa).
        aa=True  → authoritative answer.
        """

    # ── Internal helpers ───────────────────────────────────────────────────
    def _process(self, data: bytes) -> bytes:
        try:
            hdr, questions = parse_query(data)
        except Exception as exc:
            log.warning("parse error: %s", exc)
            return b''

        if hdr.opcode != 0:          # only QUERY supported
            err_flags = DnsHeader.make_flags(qr=1, opcode=hdr.opcode, rcode=RCODE_NOTIMP)
            resp = DnsHeader(id=hdr.id, flags=err_flags, qdcount=0)
            return resp.to_bytes()

        try:
            answers, authority, additional, aa = self.handle_query(questions, bool(hdr.rd))
        except Exception as exc:
            log.exception("handle_query error: %s", exc)
            err_flags = DnsHeader.make_flags(qr=1, rcode=RCODE_SERVFAIL)
            resp = DnsHeader(id=hdr.id, flags=err_flags, qdcount=0)
            return resp.to_bytes()

        return build_response(hdr, questions, answers, authority, additional, aa=aa)

    def _handle_udp_request(self, data: bytes, addr, sock: socket.socket):
        resp = self._process(data)
        if not resp:
            return
        if len(resp) > UDP_MAX:
            # Truncate: set TC=1, send first 512 bytes
            hdr = DnsHeader.from_bytes(resp)
            tc_flags = hdr.flags | (1 << 9)   # TC bit
            resp = resp[:2] + struct.pack('!H', tc_flags) + resp[4:UDP_MAX]
        try:
            sock.sendto(resp, addr)
        except OSError as e:
            log.error("UDP send to %s failed: %s", addr, e)

    def _handle_tcp_client(self, conn: socket.socket, addr):
        conn.settimeout(TCP_TIMEOUT)
        try:
            with conn:
                # RFC 1035 §4.2.2: 2-byte length prefix
                raw_len = conn.recv(2)
                if len(raw_len) < 2:
                    return
                msg_len = struct.unpack('!H', raw_len)[0]
                data = b''
                while len(data) < msg_len:
                    chunk = conn.recv(msg_len - len(data))
                    if not chunk:
                        break
                    data += chunk
                resp = self._process(data)
                if resp:
                    conn.sendall(struct.pack('!H', len(resp)) + resp)
        except OSError as e:
            log.debug("TCP client %s error: %s", addr, e)

    # ── Main loop ──────────────────────────────────────────────────────────
    def run(self):
        udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        udp_sock.bind((self.host, self.port))

        tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        tcp_sock.bind((self.host, self.port))
        tcp_sock.listen(16)

        log.info("%s listening on %s:%d (UDP+TCP)",
                 self.__class__.__name__, self.host, self.port)

        # TCP acceptor in background thread
        def tcp_acceptor():
            while True:
                try:
                    conn, addr = tcp_sock.accept()
                    t = threading.Thread(target=self._handle_tcp_client,
                                        args=(conn, addr), daemon=True)
                    t.start()
                except OSError:
                    break

        threading.Thread(target=tcp_acceptor, daemon=True).start()

        # UDP main loop
        while True:
            try:
                data, addr = udp_sock.recvfrom(4096)
                threading.Thread(
                    target=self._handle_udp_request,
                    args=(data, addr, udp_sock),
                    daemon=True,
                ).start()
            except OSError as e:
                log.error("UDP recv error: %s", e)
                break
