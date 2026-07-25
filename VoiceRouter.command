#!/bin/bash
# Voice Router (macOS) 起動スクリプト — ダブルクリックで起動できます
cd "$(dirname "$0")"

PY=python3.11
command -v "$PY" >/dev/null 2>&1 || PY=python3

if [ ! -x .venv/bin/python ]; then
  echo "初回セットアップ中… (数分かかります)"
  "$PY" -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
fi

exec .venv/bin/python voice_router.py
