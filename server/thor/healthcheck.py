"""On-device health check: wait for the proxy port, then run one translation."""

import json
import socket
import sys
import time
import urllib.request

deadline = time.time() + 30
while time.time() < deadline:
    try:
        socket.create_connection(("127.0.0.1", 8000), timeout=2).close()
        break
    except OSError:
        time.sleep(1)
else:
    print("FAIL: port 8000 never opened")
    sys.exit(1)

body = {
    "model": "healthcheck",
    "messages": [
        {
            "role": "user",
            "content": "Please translate the following Japanese text into Chinese:\n\nでも　みんなからは　「ポケモンはかせ」と　よばれて　いるよ！",
        }
    ],
}
req = urllib.request.Request(
    "http://127.0.0.1:8000/v1/chat/completions",
    data=json.dumps(body).encode(),
    headers={"Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.load(resp)
    print("model:", data.get("model"))
    print("content:", data["choices"][0]["message"]["content"])
except Exception as exc:  # noqa: BLE001 - report anything for diagnosis
    print("FAIL:", exc)
    sys.exit(1)
