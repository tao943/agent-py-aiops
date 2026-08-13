"""Thin executable wrapper for the packaged Docker Live CLI."""

from __future__ import annotations

from super_ai.evaluation.live.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
