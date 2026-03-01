#!/bin/sh
set -e

# Run DHCP client and capture the assigned IP
LEASED_IP=$(python3 /app/dhcp_client.py)

if [ -z "$LEASED_IP" ]; then
    echo "[entrypoint] ERROR: DHCP client returned no IP" >&2
    exit 1
fi

echo "[entrypoint] DHCP assigned IP: $LEASED_IP"

# Add the DHCP-leased IP to the interface
ip addr add "${LEASED_IP}/24" dev eth0

# Route all traffic through the NAT gateway
ip route replace default via 10.99.0.1 dev eth0

echo "[entrypoint] IP=${LEASED_IP} — starting services"

exec supervisord -c /etc/supervisord.conf
