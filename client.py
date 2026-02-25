# client.py (fix)
import random, socket
from scapy.all import BOOTP, DHCP

SERVER_IP   = "127.0.0.1"
SERVER_PORT = 1067
CLIENT_PORT = 1068
MAC = "aa:bb:cc:dd:ee:aa"


def mac_to_chaddr(mac: str) -> bytes:
    b = bytes(int(x, 16) for x in mac.split(":"))
    return b + b"\x00" * (16 - len(b))

def norm_mtype(v):
    # v יכול להיות מספר או מחרוזת, תלוי בגרסת scapy
    if v in (1, "discover", "DISCOVER"): return "discover"
    if v in (2, "offer", "OFFER"):       return "offer"
    if v in (3, "request", "REQUEST"):   return "request"
    if v in (5, "ack", "ACK"):           return "ack"
    if v in (6, "nak", "NAK"):           return "nak"
    return None

def get_msg_type(opts):
    for opt in opts:
        if opt == "end":
            break
        k, v = opt
        if k == "message-type":
            return norm_mtype(v)
    return None

def recv_type(sock, xid, want):
    while True:
        data, _ = sock.recvfrom(4096)
        b = BOOTP(data)
        if b.xid != xid or DHCP not in b:
            continue
        t = get_msg_type(b[DHCP].options)
        if t == want:
            return b

def main():
    xid = random.randint(1, 2**31 - 1)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("0.0.0.0", CLIENT_PORT))
    sock.settimeout(5)

    chaddr = mac_to_chaddr(MAC)

    # DISCOVER
    discover = BOOTP(op=1, xid=xid, chaddr=chaddr) / DHCP(options=[("message-type", "discover"), "end"])
    sock.sendto(bytes(discover), (SERVER_IP, SERVER_PORT))

    offer = recv_type(sock, xid, "offer")
    ip = offer.yiaddr
    print("OFFER:", ip)

    # REQUEST
    request = BOOTP(op=1, xid=xid, chaddr=chaddr) / DHCP(
        options=[("message-type", "request"), ("requested_addr", ip), "end"]
    )
    sock.sendto(bytes(request), (SERVER_IP, SERVER_PORT))

    ack = recv_type(sock, xid, "ack")
    print("ACK:", ack.yiaddr)

if __name__ == "__main__":
    main()