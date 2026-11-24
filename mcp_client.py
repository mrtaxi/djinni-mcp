#!/usr/bin/env python3
"""Minimal stdio MCP client for this repo's server.py.

Used only for things that need the account (profile, inbox). Job scanning does
not need it: public pages are read anonymously by fresh_jobs.py.

    from mcp_client import session
    replies = session([("call", {"name": "list_threads", "arguments": {}})], read_only=True)
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "server.py")


def session(calls, read_only=True, timeout=180, quiet=True):
    """Run the server, perform `calls` ([("list", None) | ("call", payload)]), return the replies."""
    env = dict(os.environ)
    if read_only:
        env["DJINNI_MCP_READ_ONLY"] = "1"

    proc = subprocess.Popen([sys.executable, SERVER], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1, env=env)

    replies = queue.Queue()

    def read_stdout():
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                replies.put(json.loads(line))
            except ValueError:
                pass

    def read_stderr():
        for line in proc.stderr:
            text = line.strip()
            if not quiet and text:
                print("[server] " + text[:200], file=sys.stderr)

    threading.Thread(target=read_stdout, daemon=True).start()
    threading.Thread(target=read_stderr, daemon=True).start()

    def send(message):
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    def wait(message_id, limit=timeout):
        while True:
            try:
                message = replies.get(timeout=limit)
            except queue.Empty:
                raise TimeoutError(f"no reply for id={message_id}")
            if message.get("id") == message_id:
                return message

    out, next_id = [], 1
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "mcp-client", "version": "0"}}})
        wait(1, 60)
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        for kind, payload in calls:
            next_id += 1
            if kind == "list":
                send({"jsonrpc": "2.0", "id": next_id, "method": "tools/list", "params": {}})
            else:
                send({"jsonrpc": "2.0", "id": next_id, "method": "tools/call", "params": payload})
            out.append(wait(next_id))
        return out
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.terminate()
        except OSError:
            pass


def payload(reply):
    """Tool reply -> parsed JSON payload (the server returns JSON text in content[0])."""
    content = reply["result"]["content"]
    text = "".join(part.get("text", "") for part in content if part.get("type") == "text")
    try:
        return json.loads(text)
    except ValueError:
        return {"text": text}


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "list":
        result = session([("list", None)], read_only=True)
        tools = result[0]["result"]["tools"]
        print(f"{len(tools)} tools")
        for tool in tools:
            print("  -", tool["name"])
    elif len(sys.argv) > 2 and sys.argv[1] == "call":
        name, args = sys.argv[2], (json.loads(sys.argv[3]) if len(sys.argv) > 3 else {})
        reply = session([("call", {"name": name, "arguments": args})], read_only=True)[0]
        print(json.dumps(payload(reply), ensure_ascii=False, indent=1)[:20000])
    else:
        print(__doc__)
