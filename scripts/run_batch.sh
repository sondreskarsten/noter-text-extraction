#!/bin/bash
# Run for max-time seconds, then exit cleanly.
# Each firm idempotent — re-running picks up where it left off.
export GOOGLE_APPLICATION_CREDENTIALS=/mnt/project/sondreskarsten-d7d14-8486be2d085b.json
cd /home/claude/noter-text-extraction
TAG="run_$(date +%H%M)_b"
timeout --kill-after=10s 450 python3 -u scripts/run_idempotent.py --workers 1 --tag "$TAG" --checkpoint-every 1 2>&1 | tail -50
