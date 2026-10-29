#!/usr/bin/env python3
"""
Side-by-side comparison of AI Furnish's existing provider chain (Claude, or
Ollama if no Anthropic key) against the new opt-in Nebius/Nemotron provider —
run this BEFORE trusting Nebius for anything real. It never changes which
provider actually runs for a normal request; "provider" is a per-request
override /ai-furnish only honors when explicitly sent (see api_server_mrcnn.py).

Requires api_server_mrcnn.py already running (default http://localhost:8001)
with NEBIUS_API_KEY set in its environment for the Nebius half to work at
all — the default-provider half runs either way.

Usage:
    python compare_furnish_providers.py
    python compare_furnish_providers.py --base-url http://localhost:8001
"""
import argparse
import json
import sys

import requests

TEST_CASES = [
    {"roomName": "Office", "widthM": 6.5, "depthM": 5.5, "prompt": "furnish the office"},
    {"roomName": "Reception", "widthM": 5.9, "depthM": 5.5, "prompt": "furnish the reception"},
    {"roomName": "Conference Room", "widthM": 6.0, "depthM": 5.0, "prompt": "furnish the conference room"},
    {"roomName": "Dining Room", "widthM": 4.0, "depthM": 3.5, "prompt": "furnish this dining room with a table for 6"},
    {"roomName": "Living Room", "widthM": 5.0, "depthM": 4.0, "prompt": "add a sofa facing the tv"},
]


def call(base_url, body):
    try:
        r = requests.post(f"{base_url}/ai-furnish", json=body, timeout=90)
    except requests.exceptions.RequestException as e:
        return {"error": f"request failed: {e}"}
    try:
        return r.json()
    except ValueError:
        return {"error": f"non-JSON response (HTTP {r.status_code}): {r.text[:300]}"}


def summarize(resp):
    if "error" in resp:
        return f"ERROR: {resp['error']}"
    model = resp.get("model", "?")
    note = f" [{resp['note']}]" if resp.get("note") else ""
    items = resp.get("items", [])
    lines = [f"model: {model}{note}"]
    for it in items:
        tag = f" [{it['role']}/{it['group']}]" if it.get("role") else ""
        lines.append(f"  - {it.get('item')} x{it.get('qty', 1)}{tag}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8001")
    args = ap.parse_args()

    for case in TEST_CASES:
        print(f"\n{'='*72}\n{case['roomName']}: \"{case['prompt']}\"\n{'='*72}")

        print("\n--- default provider (existing Claude/Ollama chain, unchanged) ---")
        default_resp = call(args.base_url, case)
        print(summarize(default_resp))

        print("\n--- provider: nebius ---")
        nebius_body = dict(case, provider="nebius")
        nebius_resp = call(args.base_url, nebius_body)
        print(summarize(nebius_resp))

    print(f"\n{'='*72}\nDone. Read through both columns above for each room — does Nebius's\n"
          f"plan use the right group/role patterns (surround for dining/conference,\n"
          f"workstation for office, etc.) as reliably as the default? That's the\n"
          f"actual question, not which one 'sounds' more sophisticated.\n{'='*72}")


if __name__ == "__main__":
    sys.exit(main())
