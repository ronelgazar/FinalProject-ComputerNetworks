"""
Authoritative nameserver for the exam.lan zone.
AA=1. Returns NXDOMAIN with SOA in authority for unknown names.
"""
from __future__ import annotations
import logging
import os
import socket
from typing import List, Tuple

from dns_packet import (
    DnsQuestion, DnsRR,
    TYPE_A, TYPE_NS, TYPE_SOA, TYPE_PTR, CLASS_IN,
    RCODE_NOERROR, RCODE_NXDOMAIN,
    rdata_a, rdata_ns, rdata_soa, rdata_ptr,
    DnsHeader, build_response, parse_query,
)
from dns_server import DnsServer

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [auth] %(levelname)s %(message)s',
)
log = logging.getLogger(__name__)

_ORIGIN = 'exam.lan'
_TTL = 300
_SOA_SERIAL = 2024010101

_SOA = DnsRR(
    name='exam.lan.', rtype=TYPE_SOA, rclass=CLASS_IN, ttl=_TTL,
    rdata=rdata_soa(
        mname='ns1.exam.lan', rname='hostmaster.exam.lan',
        serial=_SOA_SERIAL, refresh=3600, retry=900,
        expire=604800, minimum=300,
    ),
)

# Zone records: label → list of (type, rdata_bytes)
# 'server' has three A records for round-robin load balancing.
_ZONE: dict = {
    '@': [
        (TYPE_SOA, _SOA.rdata),
        (TYPE_NS,  rdata_ns('ns1.exam.lan')),
    ],
    'ns1':    [(TYPE_A, rdata_a('10.99.0.12'))],
    'dhcp':   [(TYPE_A, rdata_a('10.99.0.3'))],
    'dns':    [(TYPE_A, rdata_a('10.99.0.2'))],
    'gw':     [(TYPE_A, rdata_a('10.99.0.1'))],
    'server': [
        (TYPE_A, rdata_a('10.99.0.20')),
        (TYPE_A, rdata_a('10.99.0.21')),
        (TYPE_A, rdata_a('10.99.0.22')),
    ],
    # Individual server names — each client container uses its own server
    'server1': [(TYPE_A, rdata_a('10.99.0.20'))],
    'server2': [(TYPE_A, rdata_a('10.99.0.21'))],
    'server3': [(TYPE_A, rdata_a('10.99.0.22'))],
    'tcp-sync':   [(TYPE_A, rdata_a('10.99.0.23'))],
    'tcp-nosync': [(TYPE_A, rdata_a('10.99.0.24'))],
}

# Reverse PTR map: last-octet → fqdn
_PTR_MAP = {
    '1':   'gw.exam.lan',
    '2':   'dns.exam.lan',
    '3':   'dhcp.exam.lan',
    '12':  'ns1.exam.lan',
    '20':  'server.exam.lan',
    '21':  'server.exam.lan',
    '22':  'server.exam.lan',
    '23':  'tcp-sync.exam.lan',
    '24':  'tcp-nosync.exam.lan',
}


def _label(qname: str) -> str:
    """Return the label relative to exam.lan or '@' for the zone apex."""
    name = qname.rstrip('.').lower()
    if name == _ORIGIN:
        return '@'
    suffix = '.' + _ORIGIN
    if name.endswith(suffix):
        return name[:-len(suffix)]
    return ''


class AuthServer(DnsServer):
    def handle_query(
        self,
        questions: List[DnsQuestion],
        is_rd: bool,
    ) -> Tuple[List[DnsRR], List[DnsRR], List[DnsRR], bool]:
        answers: List[DnsRR] = []
        authority: List[DnsRR] = []
        additional: List[DnsRR] = []

        for q in questions:
            # Handle PTR (reverse) queries
            if q.qtype == TYPE_PTR:
                self._handle_ptr(q, answers, authority)
                continue

            label = _label(q.qname)
            if not label:
                log.info("NXDOMAIN for %s (outside zone)", q.qname)
                raise _NXDomain(q.qname)

            records = _ZONE.get(label)
            if records is None:
                log.info("NXDOMAIN for %s", q.qname)
                raise _NXDomain(q.qname)

            fqdn = q.qname if q.qname.endswith('.') else q.qname + '.'
            for rtype, rd in records:
                if q.qtype == TYPE_A and rtype == TYPE_A:
                    answers.append(DnsRR(name=fqdn, rtype=rtype,
                                         rclass=CLASS_IN, ttl=_TTL, rdata=rd))
                elif q.qtype == 255:   # ANY
                    answers.append(DnsRR(name=fqdn, rtype=rtype,
                                         rclass=CLASS_IN, ttl=_TTL, rdata=rd))

            if not answers:
                # Type exists but wrong qtype — return SOA in authority (NOERROR)
                authority.append(_SOA)

            log.info("ANSWER %s → %d record(s)", q.qname, len(answers))

        return answers, authority, additional, True    # AA=1

    def _handle_ptr(self, q: DnsQuestion,
                    answers: List[DnsRR], authority: List[DnsRR]):
        """Handle reverse PTR queries like 20.0.99.10.in-addr.arpa."""
        name = q.qname.rstrip('.')
        # Strip .in-addr.arpa suffix
        suffix = '.in-addr.arpa'
        if name.endswith(suffix):
            octets = name[:-len(suffix)].split('.')
            # octets are reversed: last-octet first
            last = octets[0]
            ptr_target = _PTR_MAP.get(last)
            if ptr_target:
                answers.append(DnsRR(
                    name=q.qname, rtype=TYPE_PTR, rclass=CLASS_IN,
                    ttl=_TTL, rdata=rdata_ptr(ptr_target),
                ))
                return
        authority.append(_SOA)


class _NXDomain(Exception):
    pass


from dns_packet import RCODE_NXDOMAIN as _NX, RCODE_SERVFAIL as _SF


def _patched_process(self, data: bytes) -> bytes:
    try:
        hdr, questions = parse_query(data)
    except Exception as exc:
        log.warning("parse error: %s", exc)
        return b''
    try:
        answers, authority, additional, aa = self.handle_query(questions, bool(hdr.rd))
    except _NXDomain:
        return build_response(hdr, questions, [], [_SOA], [], aa=True, rcode=_NX)
    except Exception as exc:
        log.exception("handle_query: %s", exc)
        return build_response(hdr, questions, [], [], [], rcode=_SF)
    return build_response(hdr, questions, answers, authority, additional, aa=aa)


DnsServer._process = _patched_process   # type: ignore[method-assign]


if __name__ == '__main__':
    host = os.environ.get('BIND_HOST', '0.0.0.0')
    port = int(os.environ.get('BIND_PORT', '53'))
    AuthServer(host=host, port=port).run()
