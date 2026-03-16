#!/bin/bash
set -e

echo "=== Starting Exam Network ==="

# Build and launch all containers
docker compose up --build -d

# Wait for services to be healthy
echo "Waiting for services to start..."
sleep 10

# Start prof dashboard in background
pip install -q flask requests 2>/dev/null
nohup python3 prof_dashboard.py > /tmp/prof_dashboard.log 2>&1 &

echo ""
echo "=== Exam Network is running ==="
echo ""
echo "  Client 1 (RUDP-sync):   port 8081"
echo "  Client 2 (TCP-sync):    port 8082"
echo "  Client 3 (TCP-nosync):  port 8083"
echo "  Admin Panel:            port 9999"
echo "  Prof Dashboard:         port 7777"
echo ""
echo "Click the port numbers in the PORTS tab to open in browser."
echo ""
docker compose ps
