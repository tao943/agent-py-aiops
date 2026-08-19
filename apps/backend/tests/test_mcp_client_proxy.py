from __future__ import annotations

from super_ai import mcp_client


def test_loopback_mcp_urls_bypass_environment_proxies() -> None:
    trust_environment = getattr(
        mcp_client,
        "_trust_environment_for_mcp_url",
        None,
    )
    assert callable(trust_environment)

    assert not trust_environment("http://127.0.0.1:3000/sse")
    assert not trust_environment("http://localhost:3000/sse")
    assert not trust_environment("http://[::1]:3000/sse")
    assert trust_environment("https://mcp.example.com/sse")
