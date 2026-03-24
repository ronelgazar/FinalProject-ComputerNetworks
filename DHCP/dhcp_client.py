import os, socket, struct
from scapy.all import BOOTP, DHCP

DHCP_SERVER_IP = os.environ.get("DHCP_SERVER_IP", "10.99.0.3")
SERVER_PORT = 67
CLIENT_PORT = 68


def read_mac() -> str:
    mac_env = os.environ.get("CONTAINER_MAC")
    if mac_env:
        return mac_env
    with open("/sys/class/net/eth0/address") as f:
        return f.read().strip()


def mac_to_bytes(mac: str) -> bytes:
    parts = mac.split(":")
    b = bytes(int(x, 16) for x in parts)
    return b.ljust(16, b"\x00")


def xid_from_mac(mac: str) -> int:
    """Derive a deterministic XID from the container's MAC address.
    Docker MACs on the same subnet share the first 4 bytes (02:42:0a:63:...),
    so we use bytes [2:6] — the last 4 bytes — which encode the container's
    IP address and are unique per container.
      e.g. 02:42:0a:63:00:64 -> 0a:63:00:64 -> XID=0x0a630064
           02:42:0a:63:00:65 -> 0a:63:00:65 -> XID=0x0a630065
    """
    parts = mac.split(":")
    b = bytes(int(x, 16) for x in parts[2:6])
    return struct.unpack("!I", b)[0]


def get_opt(options, key):
    for opt in options:
        if opt == "end":
            break
        k, v = opt
        if k == key:
            return v
    return None


def normalize_msg_type(v):
    if v in (2, "offer", "OFFER"):
        return "offer"
    if v in (5, "ack", "ACK"):
        return "ack"
    if v in (6, "nak", "NAK"):
        return "nak"
    return None


def main():
    mac_str = read_mac()
    mac_bytes = mac_to_bytes(mac_str)
    xid = xid_from_mac(mac_str)
    print(f"[DHCP client] mac={mac_str} xid=0x{xid:08x}", flush=True, file=__import__('sys').stderr)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(10)
    sock.bind(("0.0.0.0", CLIENT_PORT))

    # DISCOVER
    discover = BOOTP(op=1, xid=xid, chaddr=mac_bytes) / DHCP(
        options=[("message-type", "discover"), "end"]
    )
    sock.sendto(bytes(discover), (DHCP_SERVER_IP, SERVER_PORT))

    # Wait for OFFER
    offered_ip = None
    while True:
        data, _ = sock.recvfrom(4096)
        bootp = BOOTP(data)
        if DHCP not in bootp:
            continue
        if bootp.xid != xid:
            continue
        mtype = normalize_msg_type(get_opt(bootp[DHCP].options, "message-type"))
        if mtype == "offer":
            offered_ip = bootp.yiaddr
            break

    # REQUEST
    request = BOOTP(op=1, xid=xid, chaddr=mac_bytes) / DHCP(
        options=[
            ("message-type", "request"),
            ("requested_addr", offered_ip),
            ("server_id", DHCP_SERVER_IP),
            "end",
        ]
    )
    sock.sendto(bytes(request), (DHCP_SERVER_IP, SERVER_PORT))

    # Wait for ACK
    assigned_ip = None
    while True:
        data, _ = sock.recvfrom(4096)
        bootp = BOOTP(data)
        if DHCP not in bootp:
            continue
        if bootp.xid != xid:
            continue
        mtype = normalize_msg_type(get_opt(bootp[DHCP].options, "message-type"))
        if mtype == "ack":
            assigned_ip = bootp.yiaddr
            break
        elif mtype == "nak":
            raise RuntimeError("DHCP NAK received")

    sock.close()
    print(assigned_ip)


if __name__ == "__main__":
    main()
