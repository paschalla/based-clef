# Clef Decision Model Reference (`based-clef`)

This document details the operational constraints, API schema invariants, and target resolution rules for Cloudflare Workers AI's **Clef** decision models (`@cf/cloudflare/clef-flash`, `@cf/cloudflare/clef`, and `@cf/cloudflare/clef-omni`).

---

## 1. Available Models & Selection

Cloudflare Workers AI hosts three non-autoregressive Clef decision models:

| Model ID | Architecture | Parameters | Context | Typical Latency | Notes |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `@cf/cloudflare/clef-flash` | Multimodal Decision | 9B dense | 24,576 | ~200 ms | Fast System 1 classifier (default) |
| `@cf/cloudflare/clef` | Multimodal Decision | 27B dense | 65,536 | ~350 ms | Higher accuracy on dense DOM trees |
| `@cf/cloudflare/clef-omni` | Multimodal Decision | 30B MoE (3B active) | 65,536 | ~250 ms | Multimodal MoE backbone |

Configure the active model via `BASED_CLEF_MODEL` (or `DECISION_MODEL`) in `~/.config/based-clef/env`.

---

## 2. API Schema Invariants

### A. Choice Questions Criteria ($\ge 2$ items)
Cloudflare's Clef endpoint (`/ai/run/@cf/cloudflare/clef*`) strictly validates question schemas. For any question where `type == "choice"`, the `criteria` dictionary **must contain at least 2 key-value pairs after validation**.

- **Failure mode**: Sending a `criteria` dictionary with fewer than 2 items returns:
  ```json
  HTTP 422 Unprocessable Entity
  {"errors":[{"message":"AiError: Request body failed validation: questions.target.choice.criteria: Dictionary should have at least 2 items after validation, not 1"}]}
  ```
- **Invariant**: When constructing `questions.target.choice.criteria`:
  ```python
  criteria = {**ids, "0": "none of these"}
  if len(criteria) < 2:
      criteria["-1"] = "wait / page loading"
  ```
  Even on blank pages (`about:blank`), error screens, or during initial SPA hydration when 0 DOM elements are detected, `criteria` always maintains $\ge 2$ options.

### B. Multimodal Vision Constraints
- Screenshots are encoded as JPEG (`quality=60`) and formatted as inline data URLs (`data:image/jpeg;base64,...`).
- By default, `max_images=1` frame is sent per decision step, and `ClefClient` enforces a hard ceiling of 4 images (Clef's API maximum).
- Raw base64 image payloads are **never** persisted to `events.jsonl` or `training.jsonl`.

### C. Calibrated Probability Gates (`P1` – `P4`)
In a single ~200 ms forward pass, Based Clef evaluates four typed heads simultaneously:
1. **`P1[target]`** (`choice`): Calibrated confidence over prioritized interactive elements (`1..N`, `"0"` none, `"-1"` loading).
2. **`P2[action]`** (`choice`): Calibrated confidence over `click`, `fill`, `press`, `scroll`, `wait`.
3. **`P3[done]`** (`noul`): Probability (`[0, 1]`) that the user's goal has been satisfied (`done >= 0.85` completes the run).
4. **`P4[sensitive]`** (`noul`): Probability (`[0, 1]`) that the proposed click is irreversible or sensitive (payments, deletions, outbound messages, order submissions; `sensitive >= 0.50` triggers a Human-in-the-Loop approval gate).

---

## 3. Target Parsing & Escalation Resilience

1. **Target Types**:
   - Element targets emitted by Clef map to DOM numeric IDs (`"1"`, `"2"`, etc.) or sentinel values (`"0"`, `"-1"`).
   - Targets or actions emitted by escalation LLMs (`@cf/meta/llama-4-scout-17b-16e-instruct`) may contain element labels (`"Continue"`) or URLs (`"https://..."` / `"action": "goto"`).
2. **Execution Safety**:
   - `Agent._resolve_el` resolves exact numeric IDs first, then case-insensitive element names, and rejects null/sentinel strings (`None`, `"None"`, `"null"`, `""`, `"0"`, `"-1"`).
   - Raw `target` strings are never passed unconditionally into `int(target)`.
   - URL targets and `goto`/`navigate` actions route cleanly through `driver.goto(url)`.
