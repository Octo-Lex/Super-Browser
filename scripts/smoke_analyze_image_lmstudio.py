#!/usr/bin/env python
"""Narrow real-provider smoke for analyze_image via LM Studio (OpenAI-compatible).

LM Studio exposes an OpenAI-compatible API at a custom base_url. The current
OpenAIResponseProvider hardcodes the official OpenAI base_url, so this smoke
constructs the provider and repoints its AsyncOpenAI client at LM Studio, then
runs the SDK's analyze_image path against a local HTML fixture.

This is the quick-path smoke (Option 1). A follow-up PR should add
SB_OPENAI_BASE_URL support to the provider + factory.

Run:
    set LMSTUDIO_URL=http://100.64.0.1:1234/v1
    set LMSTUDIO_MODEL=glm-ocr
    .venv-smoke/Scripts/python scripts/smoke_analyze_image_lmstudio.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import super_browser
from super_browser.vision import VisionController
from super_browser.vision.providers import OpenAIResponseProvider


def _build_lmstudio_provider() -> OpenAIResponseProvider:
    base_url = os.environ.get("LMSTUDIO_URL", "http://100.64.0.1:1234/v1")
    model = os.environ.get("LMSTUDIO_MODEL", "glm-ocr")
    # Construct the provider, then repoint its client at LM Studio.
    provider = OpenAIResponseProvider(api_key="lm-studio", model=model)
    from openai import AsyncOpenAI
    provider._client = AsyncOpenAI(base_url=base_url, api_key="lm-studio")
    return provider


async def main() -> int:
    print(f"superbrowser-sdk version: {super_browser.__version__}")
    base_url = os.environ.get("LMSTUDIO_URL", "http://100.64.0.1:1234/v1")
    model = os.environ.get("LMSTUDIO_MODEL", "glm-ocr")
    print(f"LM Studio base_url: {base_url}")
    print(f"LM Studio model:    {model}")

    sb = super_browser.SuperBrowser()
    # Inject the LM Studio-backed provider via a manual controller.
    from super_browser.vision.factory import VisionProviderFactory
    provider = _build_lmstudio_provider()
    factory = VisionProviderFactory(providers={"openai": provider})
    sb._vision_controller = VisionController(factory=factory)

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
    try:
        rc = asyncio.run(main())
    finally:
        pass
    sys.exit(rc)
