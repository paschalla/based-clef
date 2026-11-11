# Agent Operating Instructions & Coding Guidelines (`based-clef`)

This document specifies developer guardrails, architecture invariants, and verification commands for autonomous AI coding agents and contributors working on **Based Clef**.

---

## 🛠️ Environment & Toolchain
- **Language**: Python 3.11+ (tested on Python 3.11, 3.12, and 3.13).
- **Virtual Environment**: `.venv` located in repository root (`pip install -e ".[all]"`).
- **Package Management**: Standard `pip` and `setuptools` with `pyproject.toml`.
- **Browser Dependencies**: Google Chrome, system Chromium, or Playwright's bundled Chromium (`playwright install chromium`).

---

## 🧪 Verification Commands
Every agent and Pull Request must pass verification before merging:

```bash
# 1. Run full unit & headless browser test suite
./.venv/bin/python3 -m unittest discover tests

# 2. Run local login & portal demos in headless sandbox mode
./.venv/bin/bclef demo --offline --headless
./.venv/bin/bclef demo --portal --offline --headless

# 3. Verify legacy CLI alias compatibility
./.venv/bin/da run "Log in with the given email and password" \
  --url "file://$PWD/decision_agent/demo/login.html" \
  -d email=dev@example.com -d password=hunter2 \
  --headless --offline
```

---

## 📐 Architecture Rules & Invariants
1. **Model Hierarchy**:
   - Primary System 1: `@cf/cloudflare/clef-flash` for high-speed non-autoregressive decision classification.
   - Rewriter: `@cf/meta/llama-3.3-70b-instruct-fp8-fast` for compact context rewrites during ladder attempts 1–2.
   - Escalation: `@cf/meta/llama-4-scout-17b-16e-instruct` for final multimodal decision step.
   - Avoid slow "thinking" models (e.g. `gpt-oss`, `qwen3`) at small `max_tokens` limits.
2. **Vision & Context Limits**:
   - Screenshots must be JPEG at `quality=60` and sent as inline data URLs (`data:image/jpeg;base64,...`).
   - Only 1 image is sent per step by default. Never exceed Clef's 4-image maximum.
   - Do NOT append raw base64 image strings to `training.jsonl`.
3. **Security & Secrets**:
   - Secrets (`~/.config/based-clef/secrets.env`, with legacy fallback to `~/.config/decision/secrets.env`) must remain strictly in memory during execution (`chmod 600`). Never send raw secret values in prompts to LLMs or Clef, or write them into `.playbook.json` files.
   - Passwords and API tokens must never be logged, printed, or committed to git.
4. **Code Quality**:
   - Keep files consolidated and maintain documentation integrity.
   - Preserve existing comments and docstrings.
5. **Clef Decision Model Reference**:
   - Detailed schema constraints, choice criteria invariants (`>= 2` items), and target parsing rules are documented in [`docs/clef_ref.md`](docs/clef_ref.md) (symlinked at [`clef_ref.md`](clef_ref.md)).
