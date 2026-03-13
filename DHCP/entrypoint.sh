#!/bin/sh

# Run DHCP client with retries (up to 3 attempts with back-off)
RETRY=0
MAX_RETRY=3
LEASED_IP=""
while [ "$RETRY" -lt "$MAX_RETRY" ]; do
    LEASED_IP=$(python3 /app/dhcp_client.py 2>/dev/null)
    if [ $? -eq 0 ] && echo "$LEASED_IP" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
        break
    fi
    RETRY=$((RETRY + 1))
    echo "[entrypoint] DHCP attempt $RETRY/$MAX_RETRY failed, retrying in ${RETRY}s..."
    sleep "$RETRY"
    LEASED_IP=""
done

if [ -z "$LEASED_IP" ]; then
    echo "[entrypoint] ERROR: DHCP failed after $MAX_RETRY attempts" >&2
    exit 1
fi

echo "[entrypoint] DHCP assigned IP: $LEASED_IP"

# Add the DHCP-leased IP only if not already configured
if ip addr show dev eth0 | grep -q "${LEASED_IP}/"; then
    echo "[entrypoint] IP ${LEASED_IP} already configured on eth0 — skipping"
else
    ip addr add "${LEASED_IP}/24" dev eth0
fi

# Route all traffic through the NAT gateway, preferring the DHCP-leased IP
# as the source so packets on the wire show the DHCP-assigned address rather
# than the internal Docker-assigned IP.
ip route replace default via 10.99.0.1 dev eth0 src "${LEASED_IP}"

echo "[entrypoint] IP=${LEASED_IP} — starting services"

exec supervisord -c /etc/supervisord.conf
