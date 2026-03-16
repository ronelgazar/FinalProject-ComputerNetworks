#!/bin/bash
set -e

echo "=== Starting Exam Network ==="

# Wait for Docker daemon to be ready
for i in $(seq 1 30); do
  if docker info >/dev/null 2>&1; then
    break
  fi
  echo "Waiting for Docker daemon... ($i/30)"
  sleep 2
done

# Start all containers (images already built in setup.sh)
docker compose up -d

# Wait for containers to be healthy
echo "Waiting for containers to start..."
for i in $(seq 1 30); do
  RUNNING=$(docker compose ps --format json 2>/dev/null | grep -c '"running"' || echo "0")
  if [ "$RUNNING" -ge 10 ]; then
    echo "All containers are up ($RUNNING running)."
    break
  fi
  echo "  $RUNNING containers running... ($i/30)"
  sleep 3
done

# Start prof dashboard in background
pkill -f prof_dashboard.py 2>/dev/null || true
nohup python3 prof_dashboard.py > /tmp/prof_dashboard.log 2>&1 &

# Verify prof dashboard is running
sleep 2
if curl -s http://localhost:7777 >/dev/null 2>&1; then
  echo "Prof Dashboard is running on port 7777"
else
  echo "WARNING: Prof Dashboard may not be ready yet. Check /tmp/prof_dashboard.log"
fi

echo ""
echo "========================================="
echo "  Exam Network is running!"
echo "========================================="
echo ""
echo "  Client 1 (RUDP-sync):   port 8081"
echo "  Client 2 (TCP-sync):    port 8082"
echo "  Client 3 (TCP-nosync):  port 8083"
echo "  Admin Panel:            port 9999"
echo "  Prof Dashboard:         port 7777"
echo ""
echo "  Open the PORTS tab below and click"
echo "  the globe icon to open in browser."
echo ""
docker compose ps
