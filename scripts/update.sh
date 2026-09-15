#!/bin/bash
# Rebuild the local search database. Safe to run any time; intended monthly.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
{
  echo "==== $(date '+%Y-%m-%d %H:%M:%S') update start"
  python3 -m pip install --user -q -r requirements.txt
  python3 build_index.py
  echo "==== $(date '+%Y-%m-%d %H:%M:%S') update done"
} 2>&1 | tee -a "logs/update-$(date +%Y%m).log"
