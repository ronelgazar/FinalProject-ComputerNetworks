"""
RFC 1034/1035 DNS packet codec — pure stdlib, no external deps.
"""
from __future__ import annotations
import struct
from dataclasses import dataclass, field
from typing import List, Tuple, Optional

# ── Record type constants ────────────────────────────────────────────────────
TYPE_A     = 1
TYPE_NS    = 2
TYPE_CNAME = 5
TYPE_SOA   = 6
TYPE_PTR   = 12
TYPE_MX    = 15
TYPE_TXT   = 16
TYPE_AAAA  = 28

CLASS_IN = 1

# ── Opcodes / RCODEs ────────────────────────────────────────────────────────
OPCODE_QUERY  = 0
RCODE_NOERROR = 0
RCODE_FORMERR = 1
RCODE_SERVFAIL= 2
RCODE_NXDOMAIN= 3
RCODE_NOTIMP  = 4
RCODE_REFUSED = 5

# Flag bit positions in the 16-bit flags word
QR_BIT  = 15   # 0=query 1=response
AA_BIT  = 10   # authoritative answer
TC_BIT  = 9    # truncated
RD_BIT  = 8    # recursion desired
RA_BIT  = 7    # recursion available


# ── Name encoding / decoding ─────────────────────────────────────────────────

def encode_name(name: str) -> bytes:
    """Encode a DNS name into length-prefixed labels.
    'exam.lan' → b'\\x04exam\\x03lan\\x00'
    Trailing dot is stripped before encoding.
    """
    if name.endswith('.'):
        name = name[:-1]
    if name == '':
        return b'\x00'
    parts = name.split('.')
    out = bytearray()
    for label in parts:
        enc = label.encode('ascii')
        out += bytes([len(enc)]) + enc
    out += b'\x00'
    return bytes(out)


def decode_name(buf: bytes, offset: int) -> Tuple[str, int]:
    """Decode a DNS name from *buf* starting at *offset*.
    Handles pointer compression (0xC0 prefix).
    Returns (name_str, new_offset) where new_offset is past the name in the
    *original* (non-followed) position.
    """
    labels: List[str] = []
    visited = set()
    jumped = False
    orig_offset = offset

    while True:
        if offset >= len(buf):
            raise ValueError(f"Name decode overrun at offset {offset}")
        length = buf[offset]

        if length == 0:
            offset += 1
            break

        if (length & 0xC0) == 0xC0:
            # Pointer
            if offset + 1 >= len(buf):
                raise ValueError("Pointer truncated")
            ptr = ((length & 0x3F) << 8) | buf[offset + 1]
            if ptr in visited:
                raise ValueError("Pointer loop detected")
            visited.add(ptr)
            if not jumped:
                orig_offset = offset + 2
                jumped = True
            offset = ptr
        else:
            offset += 1
            if offset + length > len(buf):
                raise ValueError("Label overrun")
            labels.append(buf[offset:offset + length].decode('ascii'))
            offset += length

    name = '.'.join(labels)
    end = orig_offset if jumped else offset
    return name, end


# ── Structures ───────────────────────────────────────────────────────────────

@dataclass
class DnsHeader:
    id: int = 0
    flags: int = 0          # raw 16-bit flags field
    qdcount: int = 0
    ancount: int = 0
    nscount: int = 0
    arcount: int = 0

    # ── Flag helpers ──────────────────────────────────────────────
    @property
    def qr(self) -> int:
        return (self.flags >> QR_BIT) & 1

    @property
    def opcode(self) -> int:
        return (self.flags >> 11) & 0xF

    @property
    def aa(self) -> int:
        return (self.flags >> AA_BIT) & 1

    @property
    def tc(self) -> int:
        return (self.flags >> TC_BIT) & 1

    @property
    def rd(self) -> int:
        return (self.flags >> RD_BIT) & 1

    @property
    def ra(self) -> int:
        return (self.flags >> RA_BIT) & 1

    @property
    def rcode(self) -> int:
        return self.flags & 0xF

    @staticmethod
    def make_flags(qr=1, opcode=0, aa=0, tc=0, rd=0, ra=0, rcode=0) -> int:
        return (
            (qr    & 1)  << QR_BIT  |
            (opcode& 0xF)<< 11      |
            (aa    & 1)  << AA_BIT  |
            (tc    & 1)  << TC_BIT  |
            (rd    & 1)  << RD_BIT  |
            (ra    & 1)  << RA_BIT  |
            (rcode & 0xF)
        )

    def to_bytes(self) -> bytes:
        return struct.pack('!HHHHHH',
            self.id, self.flags,
            self.qdcount, self.ancount, self.nscount, self.arcount)

    @staticmethod
    def from_bytes(data: bytes, offset: int = 0) -> 'DnsHeader':
        id_, flags, qd, an, ns, ar = struct.unpack_from('!HHHHHH', data, offset)
        return DnsHeader(id=id_, flags=flags, qdcount=qd,
                         ancount=an, nscount=ns, arcount=ar)


@dataclass
class DnsQuestion:
    qname: str = ''
    qtype: int = TYPE_A
    qclass: int = CLASS_IN

    def to_bytes(self) -> bytes:
        return encode_name(self.qname) + struct.pack('!HH', self.qtype, self.qclass)


@dataclass
class DnsRR:
    name: str = ''
    rtype: int = TYPE_A
    rclass: int = CLASS_IN
    ttl: int = 300
    rdata: bytes = field(default_factory=bytes)

    def to_bytes(self) -> bytes:
        name_enc = encode_name(self.name)
        return (name_enc
                + struct.pack('!HHiH', self.rtype, self.rclass, self.ttl, len(self.rdata))
                + self.rdata)


# ── RDATA helpers ─────────────────────────────────────────────────────────────

def rdata_a(ip: str) -> bytes:
    """Encode an IPv4 address to 4-byte RDATA."""
    parts = ip.split('.')
    return bytes(int(p) for p in parts)

def rdata_ns(name: str) -> bytes:
    return encode_name(name)

def rdata_cname(name: str) -> bytes:
    return encode_name(name)

def rdata_ptr(name: str) -> bytes:
    return encode_name(name)

def rdata_soa(mname: str, rname: str, serial: int,
              refresh: int, retry: int, expire: int, minimum: int) -> bytes:
    return (encode_name(mname) + encode_name(rname)
            + struct.pack('!IIIII', serial, refresh, retry, expire, minimum))

def rdata_mx(preference: int, exchange: str) -> bytes:
    return struct.pack('!H', preference) + encode_name(exchange)

def rdata_txt(text: str) -> bytes:
    enc = text.encode('utf-8')
    return bytes([len(enc)]) + enc

def rdata_aaaa(ip6: str) -> bytes:
    import socket
    return socket.inet_pton(socket.AF_INET6, ip6)


# ── Message parsing ───────────────────────────────────────────────────────────

def parse_query(data: bytes) -> Tuple[DnsHeader, List[DnsQuestion]]:
    """Parse a DNS query message. Returns (header, questions)."""
    if len(data) < 12:
        raise ValueError("DNS message too short")
    hdr = DnsHeader.from_bytes(data)
    offset = 12
    questions: List[DnsQuestion] = []
    for _ in range(hdr.qdcount):
        qname, offset = decode_name(data, offset)
        qtype, qclass = struct.unpack_from('!HH', data, offset)
        offset += 4
        questions.append(DnsQuestion(qname=qname, qtype=qtype, qclass=qclass))
    return hdr, questions


def _parse_rr(data: bytes, offset: int) -> Tuple[DnsRR, int]:
    name, offset = decode_name(data, offset)
    rtype, rclass, ttl, rdlen = struct.unpack_from('!HHiH', data, offset)
    offset += 10
    rdata = data[offset:offset + rdlen]
    offset += rdlen
    return DnsRR(name=name, rtype=rtype, rclass=rclass, ttl=ttl, rdata=rdata), offset


def parse_response(data: bytes):
    """Parse a full DNS response. Returns (header, questions, answers, authority, additional)."""
    hdr = DnsHeader.from_bytes(data)
    offset = 12
    questions = []
    for _ in range(hdr.qdcount):
        qname, offset = decode_name(data, offset)
        qtype, qclass = struct.unpack_from('!HH', data, offset)
        offset += 4
        questions.append(DnsQuestion(qname=qname, qtype=qtype, qclass=qclass))
    answers, authority, additional = [], [], []
    for lst, count in ((answers, hdr.ancount), (authority, hdr.nscount), (additional, hdr.arcount)):
        for _ in range(count):
            rr, offset = _parse_rr(data, offset)
            lst.append(rr)
    return hdr, questions, answers, authority, additional


# ── Message building ──────────────────────────────────────────────────────────

def build_response(
    req_hdr: DnsHeader,
    questions: List[DnsQuestion],
    answers: List[DnsRR],
    authority: List[DnsRR],
    additional: List[DnsRR],
    aa: bool = False,
    ra: bool = False,
    rcode: int = RCODE_NOERROR,
) -> bytes:
    """Build a DNS response message."""
    flags = DnsHeader.make_flags(
        qr=1,
        opcode=req_hdr.opcode,
        aa=1 if aa else 0,
        rd=req_hdr.rd,
        ra=1 if ra else 0,
        rcode=rcode,
    )
    resp_hdr = DnsHeader(
        id=req_hdr.id,
        flags=flags,
        qdcount=len(questions),
        ancount=len(answers),
        nscount=len(authority),
        arcount=len(additional),
    )
    parts = [resp_hdr.to_bytes()]
    for q in questions:
        parts.append(q.to_bytes())
    for rr in answers + authority + additional:
        parts.append(rr.to_bytes())
    return b''.join(parts)
