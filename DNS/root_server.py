"""
Simulated ICANN root nameserver.
Returns NS + glue delegations for .lan, NXDOMAIN for other TLDs.
"""
from __future__ import annotations
import logging
import os
from typing import List, Tuple

from dns_packet import (
    DnsQuestion, DnsRR,
    TYPE_A, TYPE_NS, TYPE_SOA, CLASS_IN,
    RCODE_NOERROR, RCODE_NXDOMAIN,
    rdata_a, rdata_ns,
    DnsHeader, build_response, parse_query,
)
from dns_server import DnsServer

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [root] %(levelname)s %(message)s',
)
log = logging.getLogger(__name__)

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
                # NXDOMAIN — no such TLD in our simulation
                log.info("NXDOMAIN for %s (unknown TLD '%s')", q.qname, tld)
                # We return NXDOMAIN via rcode — the base server handles it;
                # raise a special exception to signal this
                raise _NXDomain(q.qname)

            # Return NS delegation + glue A records
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
