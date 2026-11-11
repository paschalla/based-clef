# Security Policy & Safety Architecture

Based Clef (`based-clef`) is designed from the ground up so autonomous UI execution never compromises operator credentials or executes irreversible actions silently.

## Built-in Security Guarantees

1. **Zero-Leak Secret Isolation**:
   - Form credentials and secret values (`password`, `token`, `secret`, `api_key`) remain strictly in local process RAM during task execution.
   - Models (`@cf/cloudflare/clef-flash`, rewriter, and escalation LLMs) **only ever receive field labels and key names** — never raw secret values.
   - DOM snapshots automatically mask `<input type="password">` values as `***` inside the browser before returning state to Python.
   - Action telemetry (`events.jsonl`), RL trajectories (`training.jsonl`), and exported Playbooks (`*.playbook.json`) never log typed input values or raw base64 screenshots.

2. **Strict `0600` Credential Storage**:
   - `load_secrets()` refuses to load `~/.config/based-clef/secrets.env` if group or world permission bits (`0o077`) are set.
   - New secrets entered interactively are persisted only **after** task completion using `os.open(..., 0o600)` and come with an instant one-click **Redact** control in both Studio and the desktop HUD.

3. **Human-in-the-Loop (HITL) Gates**:
   - **Calibrated Sensitivity Gate (`P4[sens] >= 0.50`) + Keyword Guard**: Actions involving payments, purchases, deletions, transfers, or account changes pause execution and require explicit operator approval.
   - **Window Boundary Isolation (`DesktopDriver`)**: Strictly crops screenshots and blocks `xdotool` clicks/keystrokes outside the isolated target window rectangle, preventing terminal or HUD self-targeting.
   - **Silent Hold**: Cancels in-flight model calls immediately and discards pending actions without polluting context.

## Reporting a Vulnerability

Please report security issues privately via [GitHub Security Advisories](https://github.com/paschalla/based-clef/security/advisories/new) rather than opening a public issue. Include steps to reproduce and affected driver/interface surfaces.
