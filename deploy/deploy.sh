#!/usr/bin/env bash
set -euo pipefail

cd /var/www/peptiprices
git pull

pkill -f run_worker.py || true
sleep 1

nohup python run_worker.py >> /var/log/peptiprices-worker.log 2>&1 &
sleep 2

curl -s -X POST http://localhost:8002/api/admin/vendors/48/crawl

tail -f /var/log/peptiprices-worker.log
