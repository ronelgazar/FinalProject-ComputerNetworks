#!/bin/bash
set -e

echo "=== [1/3] Installing Python dependencies ==="
pip install flask requests python-docx

echo "=== [2/3] Installing frontend dependencies ==="
if [ -d "app/frontend" ]; then
  npm install --prefix app/frontend
fi

echo "=== [3/3] Pre-building Docker images ==="
# Wait for Docker daemon to be ready
for i in $(seq 1 30); do
  if docker info >/dev/null 2>&1; then
    echo "Docker is ready."
    break
  fi
  echo "Waiting for Docker daemon... ($i/30)"
  sleep 2
done

docker compose build

echo "=== Setup complete ==="
