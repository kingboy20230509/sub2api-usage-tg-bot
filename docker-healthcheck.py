#!/usr/bin/env python3
import os
import urllib.request


port = int(os.environ.get("LISTEN_PORT", "8099"))
with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as response:
    if response.status != 200 or response.read(16) != b"ok":
        raise SystemExit(1)
