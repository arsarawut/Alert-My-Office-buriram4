#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
/usr/bin/env python3 check_myoffice_news.py >> check_myoffice_news.log 2>&1
