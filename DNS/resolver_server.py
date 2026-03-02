"""
Recursive DNS resolver at 10.99.0.2.
Implements iterative resolution: root → TLD → auth.
TTL-aware cache. Handles CNAME chains up to depth 8.
"""
from __future__ import annotations
import logging
import os
import socket
import struct
import time
from typing import Dict, List, Optional, Tuple

from dns_packet import (
    DnsHeader, DnsQuestion, DnsRR,
    TYPE_A, TYPE_NS, TYPE_CNAME, CLASS_IN,
    RCODE_NOERROR, RCODE_NXDOMAIN, RCODE_SERVFAIL,
    parse_query, parse_response, build_response,
    encode_name, decode_name, rdata_ns,
)
from dns_server import DnsServer

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [resolver] %(levelname)s %(message)s',
)
log = logging.getLogger(__name__)

ROOT_SERVER = os.environ.get('ROOT_SERVER', '10.99.0.10')
ROOT_PORT   = int(os.environ.get('ROOT_PORT', '53'))
QUERY_TIMEOUT = 3.0
MAX_CNAME_DEPTH = 8

# ── Cache ─────────────────────────────────────────────────────────────────────
# key: (name_lower, qtype) → (list[DnsRR], expires_at)
CacheKey   = Tuple[str, int]
CacheValue = Tuple[List[DnsRR], float]
_cache: Dict[CacheKey, CacheValue] = {}


def _cache_get(name: str, qtype: int) -> Optional[List[DnsRR]]:
    key = (name.lower().rstrip('.'), qtype)
    entry = _cache.get(key)
    if entry is None:
        return None
    rrs, expires = entry
    if time.time() > expires:
        del _cache[key]
        return None
    return rrs


def _cache_put(name: str, qtype: int, rrs: List[DnsRR]):
    if not rrs:
        return
    min_ttl = min(r.ttl for r in rrs)
    key = (name.lower().rstrip('.'), qtype)
    _cache[key] = (rrs, time.time() + max(min_ttl, 1))


# ── Low-level DNS query over UDP (with TCP fallback) ─────────────────────────

def _send_query(server: str, port: int, question: DnsQuestion,
                want_recursion: bool = False) -> Optional[bytes]:
    """Send a single DNS query and return the raw response bytes, or None."""
    hdr = DnsHeader(
        id=int(time.time() * 1000) & 0xFFFF,
        flags=DnsHeader.make_flags(qr=0, rd=1 if want_recursion else 0),
        qdcount=1,
    )
    msg = hdr.to_bytes() + question.to_bytes()

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(QUERY_TIMEOUT)
            s.sendto(msg, (server, port))
            resp, _ = s.recvfrom(4096)

        resp_hdr = DnsHeader.from_bytes(resp)
        if resp_hdr.tc:
            # Truncated — retry over TCP
            return _send_query_tcp(server, port, msg)
        return resp
    except OSError as e:
        log.warning("UDP query to %s:%d failed: %s", server, port, e)
        return None


def _send_query_tcp(server: str, port: int, msg: bytes) -> Optional[bytes]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(QUERY_TIMEOUT)
            s.connect((server, port))
            s.sendall(struct.pack('!H', len(msg)) + msg)
            raw_len = s.recv(2)
            if len(raw_len) < 2:
                return None
            resp_len = struct.unpack('!H', raw_len)[0]
            resp = b''
            while len(resp) < resp_len:
                chunk = s.recv(resp_len - len(resp))
                if not chunk:
                    break
                resp += chunk
        return resp
    except OSError as e:
        log.warning("TCP query to %s:%d failed: %s", server, port, e)
        return None


# ── Iterative resolution ──────────────────────────────────────────────────────

def _decode_ns_ip(rr: DnsRR, additional: List[DnsRR]) -> Optional[str]:
    """Given an NS RR, find its A record in additional or return None."""
    try:
        ns_name, _ = decode_name(rr.rdata, 0)
    except Exception:
        return None
    ns_name_lower = ns_name.lower().rstrip('.')
    for ar in additional:
        if ar.rtype == TYPE_A and ar.name.lower().rstrip('.') == ns_name_lower:
            ip_bytes = ar.rdata
            if len(ip_bytes) == 4:
                return '.'.join(str(b) for b in ip_bytes)
    return None


def _resolve_iterative(name: str, qtype: int) -> Tuple[List[DnsRR], int]:
    """
    Iteratively resolve *name* for *qtype*.
    Returns (rrs, rcode).
    """
    # Check cache first
    cached = _cache_get(name, qtype)
    if cached is not None:
        log.debug("cache hit %s %d", name, qtype)
        return cached, RCODE_NOERROR

    next_server = ROOT_SERVER
    next_port   = ROOT_PORT

    for _ in range(16):   # max iterations to prevent infinite loops
        q = DnsQuestion(qname=name, qtype=qtype, qclass=CLASS_IN)
        raw = _send_query(next_server, next_port, q)
        if raw is None:
            log.warning("No response from %s:%d", next_server, next_port)
            return [], RCODE_SERVFAIL

        try:
            hdr, _, answers, authority, additional = parse_response(raw)
        except Exception as exc:
            log.warning("Parse response error: %s", exc)
            return [], RCODE_SERVFAIL

        rcode = hdr.rcode

        if rcode == RCODE_NXDOMAIN:
            return [], RCODE_NXDOMAIN

        if rcode != RCODE_NOERROR:
            return [], rcode

        # AA answer?
        if hdr.aa and answers:
            # Cache and return
            a_rrs = [r for r in answers if r.rtype == qtype]
            _cache_put(name, qtype, a_rrs)
            # Also cache any additional glue
            for ar in additional:
                if ar.rtype == TYPE_A:
                    _cache_put(ar.name, TYPE_A, [ar])
            return a_rrs, RCODE_NOERROR

        # Referral (authority section has NS records)?
        ns_rrs = [r for r in authority if r.rtype == TYPE_NS]
        if not ns_rrs:
            # No answer, no referral — give up
            return [], RCODE_SERVFAIL

        # Try to find glue A record in additional
        resolved_ip = None
        for ns_rr in ns_rrs:
            ip = _decode_ns_ip(ns_rr, additional)
            if ip:
                resolved_ip = ip
                break

        if resolved_ip is None:
            # No glue — need to recursively resolve the NS name
            for ns_rr in ns_rrs:
                try:
                    ns_name, _ = decode_name(ns_rr.rdata, 0)
                except Exception:
                    continue
                ns_ips, ns_rc = _resolve_iterative(ns_name, TYPE_A)
                if ns_rc == RCODE_NOERROR and ns_ips:
                    ip_bytes = ns_ips[0].rdata
                    resolved_ip = '.'.join(str(b) for b in ip_bytes)
                    break

        if resolved_ip is None:
            log.warning("Could not resolve NS for %s", name)
            return [], RCODE_SERVFAIL

        log.debug("Following referral to %s for %s", resolved_ip, name)
        next_server = resolved_ip
        next_port   = 53

    return [], RCODE_SERVFAIL   # too many iterations


# ── CNAME chain resolution ────────────────────────────────────────────────────

def _resolve_with_cname(name: str, qtype: int, depth: int = 0) -> Tuple[List[DnsRR], int]:
    if depth > MAX_CNAME_DEPTH:
        return [], RCODE_SERVFAIL
    rrs, rcode = _resolve_iterative(name, qtype)
    if rcode != RCODE_NOERROR:
        return rrs, rcode
    if not rrs:
        # Check for CNAME
        cname_rrs, crc = _resolve_iterative(name, TYPE_CNAME)
        if crc == RCODE_NOERROR and cname_rrs:
            try:
                target, _ = decode_name(cname_rrs[0].rdata, 0)
            except Exception:
                return [], RCODE_SERVFAIL
            target_rrs, trc = _resolve_with_cname(target, qtype, depth + 1)
            return cname_rrs + target_rrs, trc
    return rrs, rcode


# ── Resolver server ───────────────────────────────────────────────────────────

class ResolverServer(DnsServer):
    def handle_query(
        self,
        questions: List[DnsQuestion],
        is_rd: bool,
    ) -> Tuple[List[DnsRR], List[DnsRR], List[DnsRR], bool]:
        answers: List[DnsRR] = []
        authority: List[DnsRR] = []
        additional: List[DnsRR] = []
        final_rcode = RCODE_NOERROR

        for q in questions:
            if not is_rd:
                # Non-recursive: just return referral from root
                q2 = DnsQuestion(qname=q.qname, qtype=q.qtype, qclass=CLASS_IN)
                raw = _send_query(ROOT_SERVER, ROOT_PORT, q2, want_recursion=False)
                if raw:
                    try:
                        _, _, ans, auth, add = parse_response(raw)
                        answers.extend(ans)
                        authority.extend(auth)
                        additional.extend(add)
                    except Exception:
                        pass
                continue

            rrs, rcode = _resolve_with_cname(q.qname, q.qtype)
            if rcode == RCODE_NXDOMAIN:
                final_rcode = RCODE_NXDOMAIN
            elif rcode != RCODE_NOERROR:
                final_rcode = rcode
            answers.extend(rrs)

        if final_rcode != RCODE_NOERROR:
            raise _RcodeException(final_rcode)

        return answers, authority, additional, False


class _RcodeException(Exception):
    def __init__(self, rcode: int):
        self.rcode = rcode


# Patch process to handle arbitrary rcodes
from dns_packet import RCODE_SERVFAIL as _SF

_orig_process = DnsServer._process


def _patched_process(self, data: bytes) -> bytes:
    try:
        hdr, questions = parse_query(data)
    except Exception as exc:
        log.warning("parse error: %s", exc)
        return b''
    try:
        answers, authority, additional, aa = self.handle_query(questions, bool(hdr.rd))
    except _RcodeException as e:
        return build_response(hdr, questions, [], [], [],
                               aa=False, ra=True, rcode=e.rcode)
    except Exception as exc:
        log.exception("handle_query: %s", exc)
        return build_response(hdr, questions, [], [], [], rcode=_SF)
    return build_response(hdr, questions, answers, authority, additional,
                          aa=aa, ra=True)


DnsServer._process = _patched_process   # type: ignore[method-assign]


if __name__ == '__main__':
    host = os.environ.get('BIND_HOST', '0.0.0.0')
    port = int(os.environ.get('BIND_PORT', '53'))
    ResolverServer(host=host, port=port).run()
