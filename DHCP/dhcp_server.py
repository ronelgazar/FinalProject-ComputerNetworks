import json, os, time, socket, ipaddress
from scapy.all import BOOTP, DHCP

# ================= CONFIG =================
BIND_IP = "0.0.0.0"

# "רגיל": 67/68 (דורש sudo). לפיתוח בלי sudo: 1067/1068.
SERVER_PORT = 1067
CLIENT_PORT = 1068

POOL_START = "10.99.0.100"
POOL_END   = "10.99.0.149"

SUBNET_MASK = "255.255.255.0"
ROUTER_IP   = "10.99.0.1"   # ה-NAT שלכם
DNS_IP      = "10.99.0.2"   # ה-DNS שלכם
LEASE_SEC   = 100

STATE_FILE  = "leases.json"
OFFER_TTL   = 30            # כמה זמן הצעה נשמרת לפני שנזרקת (שניות)

BROADCAST_REPLY = False     # True אם אתה רוצה שהשרת ישלח ל-255.255.255.255:CLIENT_PORT

# =========================================

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"by_mac": {}, "by_ip": {}}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)

def cleanup_leases(st):
    t = time.time()
    expired = [mac for mac, rec in st["by_mac"].items() if rec["exp"] <= t]
    for mac in expired:
        ip = st["by_mac"][mac]["ip"]
        st["by_mac"].pop(mac, None)
        st["by_ip"].pop(ip, None)

def mac_from_chaddr(chaddr: bytes) -> str:
    b = bytes(chaddr[:6])
    return ":".join(f"{x:02x}" for x in b)

def get_opt(options, key):
    # options contains tuples + "end"
    for opt in options:
        if opt == "end":
            break
        k, v = opt
        if k == key:
            return v
    return None

def normalize_msg_type(v):
    # scapy sometimes gives 'discover'/'request' strings, sometimes numbers
    if v in (1, "discover", "DISCOVER"):
        return "discover"
    if v in (3, "request", "REQUEST"):
        return "request"
    if v in (7, "release", "RELEASE"):
        return "release"
    if v in (4, "decline", "DECLINE"):
        return "decline"
    return None

def ip_is_free(st, ip: str) -> bool:
    return ip not in st["by_ip"]

def alloc_ip(st) -> str | None:
    start = ipaddress.ip_address(POOL_START)
    end   = ipaddress.ip_address(POOL_END)
    cur = start
    while cur <= end:
        ip = str(cur)
        if ip_is_free(st, ip):
            return ip
        cur += 1
    return None

def send_reply(sock, bootp_req, addr_ip, msg_type, yiaddr, server_id_ip):
    options = [
        ("message-type", msg_type),     # 'offer' / 'ack' / 'nak'
        ("server_id", server_id_ip),
        ("lease_time", LEASE_SEC),
        ("subnet_mask", SUBNET_MASK),
        ("router", ROUTER_IP),
        ("name_server", DNS_IP),
        "end",
    ]
    resp = BOOTP(op=2, xid=bootp_req.xid, yiaddr=yiaddr, chaddr=bootp_req.chaddr) / DHCP(options=options)

    target_ip = "255.255.255.255" if BROADCAST_REPLY else addr_ip
    sock.sendto(bytes(resp), (target_ip, CLIENT_PORT))

def main():
    st = load_state()
    offers = {}  # mac -> {"ip":..., "xid":..., "exp":...}

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind((BIND_IP, SERVER_PORT))

    print(f"[DHCP] listen {BIND_IP}:{SERVER_PORT} pool {POOL_START}-{POOL_END}")

    while True:
        data, addr = sock.recvfrom(4096)

        # parse BOOTP/DHCP
        try:
            bootp = BOOTP(data)
        except Exception:
            continue

        if DHCP not in bootp:
            # לא DHCP -> מתעלמים (כדי לא לקרוס)
            continue

        dhcp = bootp[DHCP]
        mac = mac_from_chaddr(bootp.chaddr)
        mtype = normalize_msg_type(get_opt(dhcp.options, "message-type"))

        cleanup_leases(st)

        # מנקים offers שפג תוקפן
        t = time.time()
        for m in list(offers.keys()):
            if offers[m]["exp"] <= t:
                offers.pop(m, None)

        server_id_ip = addr[0]  # מספיק לבדיקות מקומיות

        if mtype == "discover":
            # אם כבר יש lease פעיל -> מציעים אותו, אחרת מקצים חדש (אבל לא "סוגרים" עד REQUEST)
            rec = st["by_mac"].get(mac)
            if rec and rec["exp"] > time.time():
                ip = rec["ip"]
            else:
                ip = alloc_ip(st)

            if ip is None:
                # אין כתובות — אפשר גם לשלוח NAK, אבל בד"כ פשוט לא עונים
                print(f"[DHCP] DISCOVER {mac} -> NO IPs")
                continue

            offers[mac] = {"ip": ip, "xid": bootp.xid, "exp": time.time() + OFFER_TTL}
            send_reply(sock, bootp, addr[0], "offer", ip, server_id_ip)
            print(f"[DHCP] DISCOVER {mac} -> OFFER {ip}")

        elif mtype == "request":
            requested = get_opt(dhcp.options, "requested_addr") or bootp.ciaddr
            if not requested:
                send_reply(sock, bootp, addr[0], "nak", "0.0.0.0", server_id_ip)
                print(f"[DHCP] REQUEST {mac} -> NAK (no requested_addr)")
                continue

            # מקבלים REQUEST רק אם זה תואם OFFER (או lease קיים)
            ok = False
            off = offers.get(mac)

            if off and off["xid"] == bootp.xid and off["ip"] == requested:
                ok = True
            else:
                rec = st["by_mac"].get(mac)
                if rec and rec["ip"] == requested:
                    ok = True

            # אם ה-IP תפוס ע"י מישהו אחר -> NAK
            owner = st["by_ip"].get(requested)
            if owner and owner != mac:
                ok = False

            if not ok:
                send_reply(sock, bootp, addr[0], "nak", "0.0.0.0", server_id_ip)
                print(f"[DHCP] REQUEST {mac} {requested} -> NAK")
                continue

            # סוגרים lease
            st["by_mac"][mac] = {"ip": requested, "exp": time.time() + LEASE_SEC}
            st["by_ip"][requested] = mac
            save_state(st)
            offers.pop(mac, None)

            send_reply(sock, bootp, addr[0], "ack", requested, server_id_ip)
            print(f"[DHCP] REQUEST {mac} -> ACK {requested}")

        elif mtype == "release":
            # אופציונלי: הלקוח “משחרר” IP
            rec = st["by_mac"].pop(mac, None)
            if rec:
                st["by_ip"].pop(rec["ip"], None)
                save_state(st)
                print(f"[DHCP] RELEASE {mac} freed {rec['ip']}")

        else:
            # decline/unknown -> מתעלמים
            continue

if __name__ == "__main__":
    main()