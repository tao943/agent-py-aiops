"""Thin executable wrapper for the packaged Docker Live CLI."""

from __future__ import annotations

from super_ai.evaluation.live.cli import main

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        import json

        print(
            json.dumps(
                {
                    "status": "infrastructure_failed",
                    "failureCategory": exc.__class__.__name__,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        raise SystemExit(2) from None
