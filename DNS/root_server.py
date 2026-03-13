"""
Simulated ICANN root nameserver.
Returns NS + glue delegations for .lan, NXDOMAIN for other TLDs.

When RECURSIVE_CAPABLE=1 (env) or /tmp/dns_recursive.json {"enabled": true},
the root server handles RD=1 queries by walking TLD → auth itself and
returning the final answer — simulating a recursive-capable root.
"""
from __future__ import annotations
import json
import logging
import os
import pathlib
import socket as _sock
import time as _time
from typing import List, Optional, Tuple

from dns_packet import (
    DnsQuestion, DnsRR,
    TYPE_A, TYPE_NS, TYPE_SOA, CLASS_IN,
    RCODE_NOERROR, RCODE_NXDOMAIN, RCODE_SERVFAIL,
    rdata_a, rdata_ns,
    DnsHeader, build_response, parse_query, parse_response, decode_name,
)
from dns_server import DnsServer

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [root] %(levelname)s %(message)s',
)
log = logging.getLogger(__name__)

# ── Recursive-capable flag ─────────────────────────────────────────────────────
# Set RECURSIVE_CAPABLE=1 env var, or write {"enabled": true} to the file below.
# The prof dashboard writes the file at runtime (no restart needed).
RECURSIVE_CAPABLE      = os.environ.get('RECURSIVE_CAPABLE', '0') == '1'
_RECURSIVE_MODE_FILE   = pathlib.Path('/tmp/dns_recursive.json')
_recursive_mtime       = 0.0
_recursive_enabled     = RECURSIVE_CAPABLE


def _reload_recursive_flag() -> bool:
    global _recursive_enabled, _recursive_mtime
    try:
        mt = _RECURSIVE_MODE_FILE.stat().st_mtime
        if mt != _recursive_mtime:
            data = json.loads(_RECURSIVE_MODE_FILE.read_text())
            _recursive_enabled = bool(data.get('enabled', RECURSIVE_CAPABLE))
            _recursive_mtime = mt
            log.info("recursive flag updated: %s", _recursive_enabled)
    except FileNotFoundError:
        _recursive_enabled = RECURSIVE_CAPABLE
    except Exception as exc:
        log.warning("Could not read recursive flag file: %s", exc)
    return _recursive_enabled


def _udp_query(server_ip: str, qname: str, qtype: int) -> Optional[bytes]:
    """Send a single DNS UDP query and return raw response bytes."""
    hdr = DnsHeader(
        id=int(_time.time() * 1000) & 0xFFFF,
        flags=DnsHeader.make_flags(qr=0, rd=0),
        qdcount=1,
    )
    q = DnsQuestion(qname=qname, qtype=qtype, qclass=CLASS_IN)
    msg = hdr.to_bytes() + q.to_bytes()
    try:
        with _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM) as s:
            s.settimeout(3.0)
            s.sendto(msg, (server_ip, 53))
            raw, _ = s.recvfrom(4096)
        return raw
    except Exception as exc:
        log.warning("sub-query to %s failed: %s", server_ip, exc)
        return None


def _recursive_resolve(qname: str, qtype: int,
                        tld_ip: str) -> Tuple[List[DnsRR], int]:
    """
    Walk TLD → auth to resolve qname when root acts as recursive resolver.
    tld_ip: the glue A record IP for the TLD NS (from _DELEGATIONS).
    Returns (answer_rrs, rcode).
    """
    # Step 1 — query TLD server
    raw = _udp_query(tld_ip, qname, qtype)
    if raw is None:
        return [], RCODE_SERVFAIL
    try:
        hdr, _, answers, _, additional = parse_response(raw)
    except Exception as exc:
        log.warning("TLD parse error in recursive mode: %s", exc)
        return [], RCODE_SERVFAIL

    if hdr.aa and answers:
        return [r for r in answers if r.rtype == qtype], RCODE_NOERROR
    if hdr.rcode == RCODE_NXDOMAIN:
        return [], RCODE_NXDOMAIN

    # Step 2 — follow referral to authoritative server (use glue from additional)
    auth_ip: Optional[str] = None
    for ar in additional:
        if ar.rtype == TYPE_A and len(ar.rdata) == 4:
            auth_ip = '.'.join(str(b) for b in ar.rdata)
            break
    if not auth_ip:
        log.warning("No glue for auth server in TLD referral for %s", qname)
        return [], RCODE_SERVFAIL

    raw2 = _udp_query(auth_ip, qname, qtype)
    if raw2 is None:
        return [], RCODE_SERVFAIL
    try:
        hdr2, _, answers2, _, _ = parse_response(raw2)
    except Exception as exc:
        log.warning("Auth parse error in recursive mode: %s", exc)
        return [], RCODE_SERVFAIL

    if hdr2.rcode == RCODE_NXDOMAIN:
        return [], RCODE_NXDOMAIN
    return [r for r in answers2 if r.rtype == qtype], hdr2.rcode


# Hardcoded root zone knowledge
_DELEGATIONS = {
    'lan': {
        'ns': [('ns1.lan', '10.99.0.11')],
    },
}

_ROOT_NS = 'a.root-servers.lan'
_ROOT_IP = '10.99.0.10'
_ROOT_TTL = 86400


def _get_tld(qname: str) -> str:
    """Extract the TLD from a DNS name (last label before the root)."""
    parts = qname.rstrip('.').split('.')
    return parts[-1] if parts else ''


def _handle_recursive(q: DnsQuestion, delegation: dict,
                       answers: List[DnsRR], authority: List[DnsRR],
                       additional: List[DnsRR]) -> Optional[Tuple]:
    """Try recursive resolution. Returns result tuple or None to fall through."""
    tld_ip = delegation['ns'][0][1]
    rrs, rcode = _recursive_resolve(q.qname, q.qtype, tld_ip)
    if rcode == RCODE_NXDOMAIN:
        raise _NXDomain(q.qname)
    if rcode != RCODE_NOERROR or not rrs:
        log.warning("Recursive resolve failed for %s, falling back", q.qname)
        return None
    answers.extend(rrs)
    log.info("Recursive: resolved %s → %d RR(s)", q.qname, len(rrs))
    return answers, authority, additional, False


class RootServer(DnsServer):
    def handle_query(
        self,
        questions: List[DnsQuestion],
        is_rd: bool,
    ) -> Tuple[List[DnsRR], List[DnsRR], List[DnsRR], bool]:
        answers: List[DnsRR] = []
        authority: List[DnsRR] = []
        additional: List[DnsRR] = []

        for q in questions:
            tld = _get_tld(q.qname)
            delegation = _DELEGATIONS.get(tld)

            if delegation is None:
                log.info("NXDOMAIN for %s (unknown TLD '%s')", q.qname, tld)
                raise _NXDomain(q.qname)

            if is_rd and _reload_recursive_flag():
                result = _handle_recursive(q, delegation, answers, authority, additional)
                if result is not None:
                    return result

            tld_zone = tld + '.'
            for ns_name, ns_ip in delegation['ns']:
                authority.append(DnsRR(
                    name=tld_zone, rtype=TYPE_NS, rclass=CLASS_IN,
                    ttl=_ROOT_TTL, rdata=rdata_ns(ns_name),
                ))
                additional.append(DnsRR(
                    name=ns_name, rtype=TYPE_A, rclass=CLASS_IN,
                    ttl=_ROOT_TTL, rdata=rdata_a(ns_ip),
                ))

            # Also advertise the root NS itself
            additional.append(DnsRR(
                name=_ROOT_NS, rtype=TYPE_A, rclass=CLASS_IN,
                ttl=_ROOT_TTL, rdata=rdata_a(_ROOT_IP),
            ))
            log.info("Delegation for TLD '%s': %s", tld, delegation['ns'])

        return answers, authority, additional, False   # AA=0 for delegations


class _NXDomain(Exception):
    pass


# Patch DnsServer to handle NXDOMAIN via exception
import struct
from dns_packet import DnsHeader as _DH, RCODE_NXDOMAIN as _NX, RCODE_SERVFAIL as _SF

_orig_process = DnsServer._process


def _patched_process(self, data: bytes) -> bytes:
    try:
        hdr, questions = parse_query(data)
    except Exception as exc:
        log.warning("parse error: %s", exc)
        return b''
    try:
        answers, authority, additional, aa = self.handle_query(questions, bool(hdr.rd))
    except _NXDomain:
        flags = _DH.make_flags(qr=1, opcode=hdr.opcode, rd=hdr.rd, rcode=_NX)
        return build_response(hdr, questions, [], [], [], aa=False, rcode=_NX)
    except Exception as exc:
        log.exception("handle_query error: %s", exc)
        flags = _DH.make_flags(qr=1, rcode=_SF)
        resp = _DH(id=hdr.id, flags=flags)
        return resp.to_bytes()
    return build_response(hdr, questions, answers, authority, additional, aa=aa)


DnsServer._process = _patched_process   # type: ignore[method-assign]


if __name__ == '__main__':
    host = os.environ.get('BIND_HOST', '0.0.0.0')
    port = int(os.environ.get('BIND_PORT', '53'))
    RootServer(host=host, port=port).run()
