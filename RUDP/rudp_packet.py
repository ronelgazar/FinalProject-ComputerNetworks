"""
RUDP packet codec — 14-byte fixed header + variable payload.

Header layout (14 bytes total):
  Offset  Size  Field
  0       4     seq      (uint32, big-endian)
  4       4     ack      (uint32, big-endian)
  8       1     flags    (uint8)
  9       1     window   (uint8)   — max window 255 frames; we use 8
  10      2     len      (uint16)  — payload length
  12      2     checksum (uint16)  — CRC-16/CCITT-FALSE over header[0:12]+payload
  14      len   payload

Flags:
  SYN  = 0x01
  ACK  = 0x02
  FIN  = 0x04
  DATA = 0x08
  RST  = 0x10
  PING = 0x20
"""
from __future__ import annotations
import struct
from dataclasses import dataclass, field

HEADER_SIZE = 14   # bytes before payload

# Flag constants
SYN  = 0x01
ACK  = 0x02
FIN  = 0x04
DATA = 0x08
RST  = 0x10
PING = 0x20

# struct format: seq(I) ack(I) flags(B) window(B) len(H) crc(H)
#                4      4      1        1         2     2   = 14 bytes
_HDR_FMT    = '!IIBBHH'
_HDR_NO_CRC = '!IIBBH'   # first 12 bytes (no crc field)
_HDR_CRC_ONLY = '!H'     # last 2 bytes


def _crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE (poly=0x1021, init=0xFFFF, refin=False, refout=False)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc


@dataclass
class RudpPacket:
    seq:     int   = 0
    ack:     int   = 0
    flags:   int   = 0
    window:  int   = 8
    payload: bytes = field(default_factory=bytes)

    # ── Flag helpers ──────────────────────────────────────────────
    def is_syn(self)  -> bool: return bool(self.flags & SYN)
    def is_ack(self)  -> bool: return bool(self.flags & ACK)
    def is_fin(self)  -> bool: return bool(self.flags & FIN)
    def is_data(self) -> bool: return bool(self.flags & DATA)
    def is_rst(self)  -> bool: return bool(self.flags & RST)
    def is_ping(self) -> bool: return bool(self.flags & PING)

    # ── Serialisation ─────────────────────────────────────────────
    def to_bytes(self) -> bytes:
        # Build header without CRC (12 bytes)
        hdr_no_crc = struct.pack(_HDR_NO_CRC,
                                  self.seq, self.ack,
                                  self.flags & 0xFF,
                                  self.window & 0xFF,
                                  len(self.payload) & 0xFFFF)
        crc = _crc16(hdr_no_crc + self.payload)
        return hdr_no_crc + struct.pack(_HDR_CRC_ONLY, crc) + self.payload

    @staticmethod
    def from_bytes(data: bytes) -> 'RudpPacket':
        if len(data) < HEADER_SIZE:
            raise ValueError(f"Packet too short: {len(data)} < {HEADER_SIZE}")
        seq, ack, flags, window, plen, crc = struct.unpack_from(_HDR_FMT, data, 0)
        payload = data[HEADER_SIZE: HEADER_SIZE + plen]
        if len(payload) < plen:
            raise ValueError("Truncated payload")
        # Verify checksum
        hdr_no_crc = data[:12]
        expected = _crc16(hdr_no_crc + payload)
        if expected != crc:
            raise ValueError(f"CRC mismatch: got {crc:#06x}, expected {expected:#06x}")
        return RudpPacket(seq=seq, ack=ack, flags=flags,
                          window=window, payload=payload)

    def __repr__(self) -> str:
        flag_names = []
        for bit, name in ((SYN,'SYN'),(ACK,'ACK'),(FIN,'FIN'),
                          (DATA,'DATA'),(RST,'RST'),(PING,'PING')):
            if self.flags & bit:
                flag_names.append(name)
        return (f"RudpPacket(seq={self.seq}, ack={self.ack}, "
                f"flags={'+'.join(flag_names) or '0'}, "
                f"win={self.window}, len={len(self.payload)})")
