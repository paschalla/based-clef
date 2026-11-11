# Contributing to Based Clef (`based-clef`)

Thank you for contributing to **Based Clef**! We welcome improvements across the intent compiler, browser/desktop drivers, Studio web UI, MCP/IPC integrations, and model adapters.

## Local Development Setup

```bash
git clone https://github.com/paschalla/based-clef.git
cd based-clef
python3 -m venv .venv
./.venv/bin/pip install -e ".[all]"
```

If you don't have Google Chrome or system Chromium installed yet, install Playwright's Chromium bundle:

```bash
./.venv/bin/playwright install chromium
```

## Core Architecture Invariants

Before opening a Pull Request, please verify your changes preserve these invariants:

1. **Clef Choice Schema (`>= 2` items)**: Every `choice` question sent to Cloudflare Clef must contain at least 2 `criteria` entries after validation (see [`docs/clef_ref.md`](docs/clef_ref.md)).
2. **Secret Isolation**: Never pass raw user secret values into LLM/Clef prompts, telemetry events, `training.jsonl`, or exported `.playbook.json` files.
3. **Vision Budget**: Screenshots default to `max_images=1` JPEG (`quality=60`), capped at 4 frames, and must never be written as base64 strings to `training.jsonl` or `events.jsonl`.
4. **Zero Framework Bloat**: Keep dependencies minimal (`httpx`, `rich`, `playwright`, `pillow`, `mss`) so `based-clef` installs in seconds anywhere.

## Verification Suite

Run the full unit + headless browser integration suite and offline demo before submitting a PR:

```bash
# 1. Run full unit + real-world headless browser test suite
./.venv/bin/python3 -m unittest discover -s tests -v

# 2. Verify CLI doctor & offline end-to-end demo
./.venv/bin/bclef doctor
./.venv/bin/bclef demo --offline --headless
```
