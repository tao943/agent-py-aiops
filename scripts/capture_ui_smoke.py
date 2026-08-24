"""Capture authenticated desktop and narrow frontend smoke evidence with Chrome CDP."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect


async def capture(*, chrome: Path, base_url: str, output: Path, token: str) -> None:
    profile = Path(tempfile.mkdtemp(prefix="agent-py-chrome-"))
    port = 9237
    process = subprocess.Popen(
        [
            str(chrome),
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        version_url = f"http://127.0.0.1:{port}/json/version"
        for _ in range(40):
            try:
                with urllib.request.urlopen(version_url, timeout=1):
                    break
            except OSError:
                await asyncio.sleep(0.25)
        login_url = urllib.parse.quote(base_url + "/login", safe=":/?=&")
        create_url = f"http://127.0.0.1:{port}/json/new?{login_url}"
        request = urllib.request.Request(create_url, method="PUT")
        with urllib.request.urlopen(request, timeout=5) as response:
            page = json.load(response)
        async with connect(page["webSocketDebuggerUrl"], max_size=8_000_000) as socket:
            client = CdpClient(socket)
            await client.call("Page.enable")
            await client.call("Runtime.enable")
            await asyncio.sleep(1)
            expression = f"localStorage.setItem('super-ai.auth-token', {json.dumps(token)})"
            await client.call("Runtime.evaluate", {"expression": expression})
            output.mkdir(parents=True, exist_ok=True)
            await capture_page(
                client,
                base_url + "/incidents",
                output / "incidents-desktop.png",
                1440,
                900,
                marker="事件中心",
            )
            await capture_page(
                client,
                base_url + "/agent-config?node=conversation",
                output / "agent-config-desktop.png",
                1440,
                900,
                marker="配置控制台",
            )
            await capture_page(
                client,
                base_url + "/system",
                output / "system-status-desktop.png",
                1440,
                900,
                marker="进程在线",
                timeout=30,
            )
            await capture_page(
                client,
                base_url + "/agent-config?node=conversation&viewport=narrow",
                output / "agent-config-narrow.png",
                390,
                844,
                marker="配置控制台",
                mobile=True,
            )
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        shutil.rmtree(profile, ignore_errors=True)


async def capture_page(
    client: CdpClient,
    url: str,
    target: Path,
    width: int,
    height: int,
    *,
    marker: str,
    mobile: bool = False,
    timeout: int = 15,
) -> None:
    await client.call(
        "Emulation.setDeviceMetricsOverride",
        {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": mobile},
    )
    await client.call("Page.navigate", {"url": url})
    for _ in range(timeout * 2):
        result = await client.call(
            "Runtime.evaluate",
            {"expression": "document.body?.innerText || ''", "returnByValue": True},
        )
        text = result.get("result", {}).get("value", "")
        if marker in text:
            break
        await asyncio.sleep(0.5)
    else:
        raise RuntimeError(f"Page did not reach expected UI state: {marker}")
    await asyncio.sleep(0.5)
    result = await client.call(
        "Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False}
    )
    target.write_bytes(base64.b64decode(result["data"]))


class CdpClient:
    def __init__(self, socket: Any) -> None:
        self.socket = socket
        self.sequence = 0

    async def call(self, method: str, params: dict[str, object] | None = None) -> dict[str, Any]:
        self.sequence += 1
        current = self.sequence
        await self.socket.send(
            json.dumps({"id": current, "method": method, "params": params or {}})
        )
        while True:
            message = json.loads(await self.socket.recv())
            if message.get("id") != current:
                continue
            if "error" in message:
                raise RuntimeError(f"Chrome CDP call failed: {method}")
            return message.get("result", {})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chrome", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:5173")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    token = os.environ.get("UI_SMOKE_ACCESS_TOKEN", "")
    if not token:
        raise SystemExit("UI_SMOKE_ACCESS_TOKEN is required")
    asyncio.run(
        capture(
            chrome=args.chrome, base_url=args.base_url.rstrip("/"), output=args.output, token=token
        )
    )


if __name__ == "__main__":
    main()
