"""Intent-to-Action-Sequence compiler and live Playbook tracker for Based Clef.

Bridges natural-language user intent (or `->` Clef pipeline shorthand) into an explicit,
inspectable, editable sequence of UI `Stage`s that guide Clef's 200ms System 1 loop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any

SENSITIVE_STAGE_WORDS = (
    "pay", "buy", "purchase", "delete", "remove", "destroy", "drop",
    "send", "submit order", "place order", "confirm order", "checkout",
    "transfer", "unsubscribe", "cancel subscription", "revoke",
)

_URL_RE = re.compile(r"\b((?:https?|file)://[^\s<>\"')]+)", re.I)
# Match bare domains like `wikipedia.org` or `console.groq.com/keys` NOT preceded by `@` (emails)
_DOMAIN_RE = re.compile(
    r"(?<![@\w/])([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
    r"\.(?:com|org|net|io|ai|dev|app|edu|gov|co|uk|de|fr|xyz|sh|gg|me|info)(?:/[^\s<>\"'),;]*)?)",
    re.I,
)
_EMAIL_RE = re.compile(r"\b([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)\b")
_PIPELINE_SPLIT_RE = re.compile(r"\s*(?:->|=>|»|\|\|)\s*")
_NUMBERED_SPLIT_RE = re.compile(r"(?:^|\s+)(?:\d+[\.\)]\s+)")


def _is_sensitive_text(text: str) -> bool:
    low = text.lower()
    return any(w in low for w in SENSITIVE_STAGE_WORDS)


def _slug_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


@dataclass
class Stage:
    id: int
    intent: str
    kind: str = "auto"           # navigate | fill | click | press | scroll | verify | auto
    target_hint: str = ""
    value_key: str = ""          # key name in data/secrets (NEVER raw secret value)
    url: str = ""
    sensitive: bool = False
    status: str = "pending"      # pending | active | completed | skipped | failed
    executed_step: int | None = None
    confidence: float | None = None
    via: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "intent": self.intent,
            "kind": self.kind,
            "target_hint": self.target_hint,
            "value_key": self.value_key,
            "sensitive": self.sensitive,
            "status": self.status,
        }
        if self.url:
            out["url"] = self.url
        if self.executed_step is not None:
            out["executed_step"] = self.executed_step
        if self.confidence is not None:
            out["confidence"] = round(self.confidence, 3)
        if self.via:
            out["via"] = self.via
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any], idx: int = 1) -> "Stage":
        intent = str(d.get("intent") or d.get("step") or d.get("description") or f"Step {idx}").strip()
        return cls(
            id=int(d.get("id", idx)),
            intent=intent,
            kind=str(d.get("kind", "auto")).lower(),
            target_hint=str(d.get("target_hint", "")),
            value_key=str(d.get("value_key", "")),
            url=str(d.get("url", "")),
            sensitive=bool(d.get("sensitive", _is_sensitive_text(intent))),
            status=str(d.get("status", "pending")),
        )


@dataclass
class Playbook:
    raw_intent: str
    goal: str
    start_url: str | None = None
    driver: str = "browser"
    required_keys: list[str] = field(default_factory=list)
    extracted_data: dict[str, str] = field(default_factory=dict)  # non-secret inline values (e.g. search query, email)
    stages: list[Stage] = field(default_factory=list)

    def ensure_active(self) -> Stage | None:
        for s in self.stages:
            if s.status == "active":
                return s
        for s in self.stages:
            if s.status == "pending":
                s.status = "active"
                return s
        return None

    def active_index_and_total(self) -> tuple[int, int, str]:
        total = len(self.stages)
        if not total:
            return 0, 0, ""
        cur = self.ensure_active()
        if cur is None:
            return total, total, "All stages complete"
        return cur.id, total, cur.intent

    def mark_navigated(self, url: str) -> Stage | None:
        """Mark a leading `navigate` stage complete once initial `goto(url)` runs."""
        cur = self.ensure_active()
        if cur and cur.kind == "navigate":
            cur.status = "completed"
            cur.via = "goto"
            self.ensure_active()
            return cur
        return None

    def record_effect(
        self,
        action: str,
        target_name: str,
        step_num: int,
        confidence: float | None = None,
        via: str = "",
        stage_done_p: float = 0.0,
    ) -> Stage | None:
        """Advance current stage when a verified DOM/screen action satisfies or aligns with it."""
        cur = self.ensure_active()
        if cur is None:
            return None

        act_low = (action or "").lower()
        tgt_low = (target_name or "").lower()
        hint_low = (cur.target_hint or "").lower()
        intent_low = cur.intent.lower()

        matched = stage_done_p >= 0.72
        if not matched:
            if cur.kind in ("auto", "verify"):
                matched = True
            elif cur.kind == "navigate" and act_low in ("goto", "navigate", "page.goto"):
                matched = True
            elif cur.kind == "fill" and act_low in ("fill", "click"):
                if not hint_low or hint_low in tgt_low or tgt_low in hint_low or any(
                    w in tgt_low for w in re.split(r"\W+", intent_low) if len(w) >= 3
                ):
                    matched = True
            elif cur.kind in ("click", "press", "scroll") and act_low in ("click", "press", "scroll"):
                if not hint_low or hint_low in tgt_low or tgt_low in hint_low or any(
                    w in tgt_low for w in re.split(r"\W+", intent_low) if len(w) >= 3
                ):
                    matched = True
            elif len(self.stages) == 1:
                matched = True

        if matched:
            cur.status = "completed"
            cur.executed_step = step_num
            cur.confidence = confidence
            cur.via = via
            self.ensure_active()
            return cur
        return None

    def mark_all_done(self) -> None:
        for s in self.stages:
            if s.status in ("pending", "active"):
                s.status = "completed"

    def missing_keys(self, available: dict[str, str] | None = None) -> list[str]:
        have = {k.lower() for k in self.extracted_data} | {k.lower() for k in (available or {})}
        return [k for k in self.required_keys if k.lower() not in have]

    def prompt_context(self) -> str:
        """Compact stage context injected into Clef state and LLM prompts."""
        if not self.stages:
            return ""
        cur = self.ensure_active()
        done_cnt = sum(1 for s in self.stages if s.status == "completed")
        if cur is None:
            return f"All {len(self.stages)} planned stages completed; verify final state."
        hint_part = f" (target hint: {cur.target_hint})" if cur.target_hint else ""
        return f"Stage {cur.id}/{len(self.stages)} ({done_cnt} done): {cur.intent}{hint_part}"

    def to_dict(self, reset_status: bool = False) -> dict[str, Any]:
        stages_out = []
        for idx, s in enumerate(self.stages, 1):
            d = s.to_dict()
            d["id"] = idx
            if reset_status:
                d["status"] = "pending"
                d.pop("executed_step", None)
                d.pop("confidence", None)
                d.pop("via", None)
            stages_out.append(d)
        return {
            "schema": "based-clef/playbook@1",
            "raw_intent": self.raw_intent,
            "goal": self.goal,
            "start_url": self.start_url,
            "driver": self.driver,
            "required_keys": list(self.required_keys),
            # Only non-secret query/navigation hints; filter any secret-like keys defensively
            "parameters": {
                k: v for k, v in self.extracted_data.items()
                if not any(sec in k.lower() for sec in ("pass", "secret", "token", "key", "code", "otp"))
            },
            "stages": stages_out,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Playbook":
        raw_stages = d.get("stages") or d.get("steps") or []
        stages: list[Stage] = []
        for idx, item in enumerate(raw_stages, 1):
            if isinstance(item, str):
                stages.append(_stage_from_clause(idx, item))
            elif isinstance(item, dict):
                stages.append(Stage.from_dict(item, idx))
        goal = str(d.get("goal") or d.get("raw_intent") or d.get("task") or "Execute playbook")
        return cls(
            raw_intent=str(d.get("raw_intent") or goal),
            goal=goal,
            start_url=d.get("start_url") or d.get("url") or None,
            driver=str(d.get("driver", "browser")),
            required_keys=list(d.get("required_keys", [])),
            extracted_data=dict(d.get("parameters", {})),
            stages=stages,
        )

    def save(self, path: str | Path) -> Path:
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(reset_status=True), indent=2) + "\n", encoding="utf-8")
        return p

    @classmethod
    def load(cls, path: str | Path) -> "Playbook":
        p = Path(path).expanduser()
        return cls.from_dict(json.loads(p.read_text(encoding="utf-8")))


# ===== deterministic parsing helpers =====


def extract_url_from_intent(text: str) -> str | None:
    """Extract an explicit URL or bare web domain from natural intent, ignoring email addresses."""
    if not text:
        return None
    # Support `@https://...` or `@wikipedia.org` shorthand prefix
    cleaned = re.sub(r"(?:^|\s)@((?:https?://|[a-z0-9-]+\.[a-z]{2,})\S*)", r" \1", text, flags=re.I)
    m_url = _URL_RE.search(cleaned)
    if m_url:
        return m_url.group(1).rstrip(".,;)")
    m_dom = _DOMAIN_RE.search(cleaned)
    if m_dom:
        dom = m_dom.group(1).rstrip(".,;)")
        return f"https://{dom}"
    return None


def _stage_from_clause(idx: int, clause: str) -> Stage:
    c = clause.strip().strip(".;")
    low = c.lower()
    sens = _is_sensitive_text(c)

    # Navigation clause
    u = extract_url_from_intent(c)
    if u and re.match(r"^(?:@|go\s+to|open|visit|navigate\s+to|goto)\b", low):
        return Stage(id=idx, intent=f"Navigate to {u}", kind="navigate", url=u, target_hint=u, sensitive=False)

    # Search / fill clause
    q_match = re.search(r"['\"]([^'\"]+)['\"]", c)
    if any(w in low for w in ("search", "type", "enter", "fill", "input", "write")):
        hint = q_match.group(1) if q_match else ""
        key = "search" if "search" in low else ""
        if "email" in low:
            key, hint = "email", "Email"
        elif "password" in low:
            key, hint = "password", "Password"
        elif "username" in low:
            key, hint = "username", "Username"
        return Stage(id=idx, intent=c, kind="fill", target_hint=hint, value_key=key, sensitive=sens)

    if any(w in low for w in ("scroll", "read down", "page down")):
        return Stage(id=idx, intent=c, kind="scroll", sensitive=False)

    if any(w in low for w in ("press ", "hit enter", "press enter")):
        return Stage(id=idx, intent=c, kind="press", target_hint="Enter", sensitive=sens)

    if any(w in low for w in ("verify", "confirm that", "check that", "ensure")):
        return Stage(id=idx, intent=c, kind="verify", target_hint=q_match.group(1) if q_match else "", sensitive=False)

    if any(w in low for w in ("click", "select", "choose", "open", "submit", "sign in", "log in", "generate", "create")):
        return Stage(
            id=idx,
            intent=c,
            kind="click",
            target_hint=q_match.group(1) if q_match else "",
            sensitive=sens,
        )

    return Stage(id=idx, intent=c, kind="auto", target_hint=q_match.group(1) if q_match else "", sensitive=sens)


def compile_intent_fast(
    task: str,
    url: str | None = None,
    data: dict[str, str] | None = None,
    snap: dict[str, Any] | None = None,
    driver: str = "browser",
) -> Playbook:
    """Zero-latency deterministic compiler from user intent + optional DOM snapshot to a `Playbook`."""
    raw = (task or "").strip()
    extracted: dict[str, str] = {}
    provided = dict(data or {})

    # 1. Auto-detect starting URL if not explicitly supplied
    resolved_url = url or extract_url_from_intent(raw)

    # 2. Auto-detect inline email or search query (never passwords!)
    m_email = _EMAIL_RE.search(raw)
    if m_email and "email" not in {k.lower() for k in provided}:
        extracted["email"] = m_email.group(1)

    if "search" in raw.lower() and "search" not in {k.lower() for k in provided}:
        m_q = re.search(r"(?:search(?:\s+\S+){0,3}\s+for|search)\s+['\"]([^'\"]+)['\"]", raw, re.I)
        if not m_q:
            m_q = re.search(r"['\"]([^'\"]+)['\"]", raw)
        if m_q:
            extracted["search"] = m_q.group(1)

    # 3. Decompose clauses: pipeline shorthand (`->`), numbered (`1. ... 2. ...`), or compound prose
    clauses: list[str] = []
    if _PIPELINE_SPLIT_RE.search(raw):
        clauses = [p.strip() for p in _PIPELINE_SPLIT_RE.split(raw) if p.strip()]
    elif len(_NUMBERED_SPLIT_RE.split(raw)) > 2:
        clauses = [p.strip() for p in _NUMBERED_SPLIT_RE.split(raw) if p.strip()]
    else:
        parts = re.split(
            r"\s*(?:,\s*then\s+|\s+and\s+then\s+|;\s*|\.\s+(?=[A-Z])|,\s+(?=(?:click|open|search|fill|enter|sign|log|generate|create|verify|scroll|select)\b))\s*",
            raw,
            flags=re.I,
        )
        # Further split simple two-part "Search X for 'Y' and open Z"
        expanded: list[str] = []
        for p in parts:
            sub = re.split(
                r"\s+and\s+(?=(?:open|click|submit|sign\s+in|log\s+in|generate|create|scroll|verify|select)\b)",
                p,
                flags=re.I,
            )
            expanded.extend(s.strip() for s in sub if s.strip())
        clauses = expanded

    stages: list[Stage] = []
    required_keys: list[str] = []

    # Add leading navigation stage when a URL is resolved and current page isn't already there
    cur_snap_url = (snap or {}).get("url") or ""
    if resolved_url and (not cur_snap_url or cur_snap_url == "about:blank"):
        if not clauses or not (extract_url_from_intent(clauses[0]) and re.match(r"^(?:@|go\s+to|open|visit|goto)", clauses[0], re.I)):
            stages.append(Stage(id=1, intent=f"Open {resolved_url}", kind="navigate", url=resolved_url, target_hint=resolved_url))

    # DOM-grounded expansion when task is a concise form/login goal (e.g. "Log in with email and password")
    snap_els = (snap or {}).get("elements") or []
    low_raw = raw.lower()
    is_login_or_form = len(clauses) <= 1 and any(w in low_raw for w in ("log in", "login", "sign in", "signin", "fill out", "register"))
    empty_dom_inputs = [
        e for e in snap_els
        if e.get("empty") is True and e.get("type") not in ("submit", "button", "checkbox", "radio", "hidden")
    ]

    if is_login_or_form and empty_dom_inputs:
        for inp in empty_dom_inputs[:5]:
            nm = inp.get("name") or inp.get("type") or "field"
            key = _slug_key(nm) or "value"
            if inp.get("type") == "email" or "email" in key:
                key = "email"
            elif inp.get("type") == "password" or "password" in key:
                key = "password"
            if key not in required_keys:
                required_keys.append(key)
            stages.append(
                Stage(
                    id=len(stages) + 1,
                    intent=f'Fill "{nm}"',
                    kind="fill",
                    target_hint=nm,
                    value_key=key,
                    sensitive=False,
                )
            )
        submit_btn = next(
            (e for e in snap_els if e.get("type") == "submit" or any(k in (e.get("name") or "").lower() for k in ("sign in", "log in", "submit", "continue"))),
            None,
        )
        btn_name = submit_btn["name"] if submit_btn and submit_btn.get("name") else "Sign in"
        stages.append(
            Stage(
                id=len(stages) + 1,
                intent=f'Click "{btn_name}"',
                kind="click",
                target_hint=btn_name,
                sensitive=_is_sensitive_text(btn_name),
            )
        )
    else:
        for cl in clauses:
            st = _stage_from_clause(len(stages) + 1, cl)
            # Avoid duplicating the leading navigate stage
            if st.kind == "navigate" and stages and stages[0].kind == "navigate":
                continue
            # Expand login / credential clauses into Email + Password + Sign in stages when no DOM snapshot yet
            cl_low = cl.lower()
            is_auth_clause = any(w in cl_low for w in ("log in", "login", "sign in", "signin")) and (
                "email" in extracted or "email" in cl_low or " as " in cl_low or "password" in cl_low or not snap_els
            )
            if (("email" in cl_low and "password" in cl_low) or is_auth_clause) and st.kind in ("fill", "auto", "click"):
                stages.append(Stage(id=len(stages) + 1, intent="Enter Email", kind="fill", target_hint="Email", value_key="email"))
                stages.append(Stage(id=len(stages) + 1, intent="Enter Password", kind="fill", target_hint="Password", value_key="password"))
                if any(w in cl_low for w in ("log in", "login", "sign in", "signin", "submit")):
                    stages.append(Stage(id=len(stages) + 1, intent="Submit Sign in", kind="click", target_hint="Sign in"))
                for k in ("email", "password"):
                    if k not in required_keys:
                        required_keys.append(k)
                continue
            if st.value_key and st.value_key not in required_keys:
                required_keys.append(st.value_key)
            stages.append(st)

    for k in provided:
        if k not in required_keys:
            required_keys.append(k)
    for s in stages:
        if s.value_key and s.value_key not in required_keys:
            required_keys.append(s.value_key)

    if not stages:
        stages.append(Stage(id=1, intent=raw or "Complete task", kind="auto", sensitive=_is_sensitive_text(raw)))

    for idx, s in enumerate(stages, 1):
        s.id = idx

    pb = Playbook(
        raw_intent=raw,
        goal=raw,
        start_url=resolved_url,
        driver=driver,
        required_keys=required_keys,
        extracted_data=extracted,
        stages=stages,
    )
    pb.ensure_active()
    return pb
