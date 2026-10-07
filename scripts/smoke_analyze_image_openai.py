#!/usr/bin/env python
"""Narrow real-provider smoke test for analyze_image (P7.C, v2.13.1+).

Run:
    set SB_OPENAI_API_KEY=sk-...
    set SB_VISION_DEFAULT_PROVIDER=openai
    .venv-smoke/Scripts/python scripts/smoke_analyze_image_openai.py

Pass criteria:
  - imports superbrowser-sdk 2.13.0 from PyPI
  - browser starts, local page loads
  - analyze_image returns ok=True
  - data.provider == "openai"
  - data.model is set
  - data.answer is non-empty and semantically correct (mentions "red")
  - no locate()/coordinate path errors
  - no vision_unavailable
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import super_browser


def _check_env() -> None:
    key = os.environ.get("SB_OPENAI_API_KEY")
    if not key:
        print("FAIL: SB_OPENAI_API_KEY is not set.", file=sys.stderr)
        sys.exit(2)
    print(f"superbrowser-sdk version: {super_browser.__version__}")
    if not super_browser.__version__.startswith("2.13."):
        print(f"WARN: expected 2.13.x, got {super_browser.__version__}")


async def main() -> int:
    _check_env()

    sb = super_browser.SuperBrowser()
    await sb.start()
    print("STARTED")
    try:
        fixture = (Path(__file__).parent / "smoke_fixture.html").resolve().as_uri()
        await sb.navigate(fixture)
        await asyncio.sleep(1.0)
        print(f"NAVIGATED to {fixture}")

        result = await sb.analyze_image(question="What color is the large square?")
        print(f"RESULT ok={result.ok} error={result.error!r}")

        if not result.ok:
            print(f"FAIL: analyze_image did not succeed. error={result.error}")
            return 1

        data = result.data or {}
        print(f"  provider = {data.get('provider')!r}")
        print(f"  model    = {data.get('model')!r}")
        print(f"  answer   = {data.get('answer')!r}")
        print(f"  conf     = {data.get('confidence')!r}")

        failures = []

        if data.get("provider") != "openai":
            failures.append(f"provider != 'openai' (got {data.get('provider')!r})")

        if not data.get("model"):
            failures.append("model is empty")

        answer = str(data.get("answer", "")).lower()
        if not answer:
            failures.append("answer is empty")
        elif "red" not in answer:
            failures.append(f"answer does not mention 'red' (got {answer!r})")

        if failures:
            print("FAIL:")
            for f in failures:
                print(f"  - {f}")
            return 1

        print("PASS: all criteria met")
        return 0
    finally:
        try:
            await sb.stop()
        except Exception:
            pass


if __name__ == "__main__":
    rc = asyncio.run(main())
    sys.exit(rc)
