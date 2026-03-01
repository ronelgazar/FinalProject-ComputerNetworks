"""
Authoritative nameserver for the .lan TLD.
Returns NS + glue delegations for second-level domains, NXDOMAIN otherwise.
"""
from __future__ import annotations
import logging
import os
from typing import List, Tuple

from dns_packet import (
    DnsQuestion, DnsRR,
    TYPE_A, TYPE_NS, TYPE_SOA, CLASS_IN,
    RCODE_NOERROR, RCODE_NXDOMAIN,
    rdata_a, rdata_ns, rdata_soa,
    DnsHeader, build_response, parse_query,
)
from dns_server import DnsServer

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [tld] %(levelname)s %(message)s',
)
log = logging.getLogger(__name__)

_TTL = 86400
_SOA_TTL = 3600

# Known second-level delegations under .lan
_DELEGATIONS = {
    'exam': {
        'ns': [('ns1.exam.lan', '10.99.0.12')],
    },
}

_SOA_RR = DnsRR(
    name='lan.', rtype=TYPE_SOA, rclass=CLASS_IN, ttl=_SOA_TTL,
    rdata=rdata_soa(
        mname='ns1.lan', rname='admin.lan',
        serial=2024010101, refresh=3600, retry=900,
        expire=604800, minimum=300,
    ),
)


def _get_sld(qname: str) -> str:
    """Return the second-level domain label for a name under .lan."""
    parts = qname.rstrip('.').split('.')
    # e.g. "server.exam.lan" → parts = ["server","exam","lan"]
    # SLD = parts[-2] when TLD is parts[-1]="lan"
    if len(parts) >= 2 and parts[-1] == 'lan':
        return parts[-2]
    return ''


class TldServer(DnsServer):
    def handle_query(
        self,
        questions: List[DnsQuestion],
        is_rd: bool,
    ) -> Tuple[List[DnsRR], List[DnsRR], List[DnsRR], bool]:
        answers: List[DnsRR] = []
        authority: List[DnsRR] = []
        additional: List[DnsRR] = []

        for q in questions:
            sld = _get_sld(q.qname)
            delegation = _DELEGATIONS.get(sld)

            if delegation is None:
                log.info("NXDOMAIN for %s (unknown SLD '%s')", q.qname, sld)
                raise _NXDomain(q.qname)

            zone = sld + '.lan.'
            for ns_name, ns_ip in delegation['ns']:
                authority.append(DnsRR(
                    name=zone, rtype=TYPE_NS, rclass=CLASS_IN,
                    ttl=_TTL, rdata=rdata_ns(ns_name),
                ))
                additional.append(DnsRR(
                    name=ns_name, rtype=TYPE_A, rclass=CLASS_IN,
                    ttl=_TTL, rdata=rdata_a(ns_ip),
                ))
            log.info("Delegation for '%s': %s", sld, delegation['ns'])

        return answers, authority, additional, False   # AA=0


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
        return build_response(hdr, questions, [], [_SOA_RR], [], aa=True, rcode=_NX)
    except Exception as exc:
        log.exception("handle_query: %s", exc)
        return build_response(hdr, questions, [], [], [], rcode=_SF)
    return build_response(hdr, questions, answers, authority, additional, aa=aa)


DnsServer._process = _patched_process   # type: ignore[method-assign]


if __name__ == '__main__':
    host = os.environ.get('BIND_HOST', '0.0.0.0')
    port = int(os.environ.get('BIND_PORT', '53'))
    TldServer(host=host, port=port).run()
