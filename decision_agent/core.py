"""Core: config, telemetry (100-line ring + JSONL log), Clef, streaming-LLM, and offline heuristic clients."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable
import httpx
import json
import os
import re
import threading
import time
import urllib.request


# ===== config =====

APP_NAME = "based-clef"


def _resolve_env_path() -> Path:
    explicit = os.environ.get("BASED_CLEF_ENV") or os.environ.get("DECISION_ENV")
    if explicit:
        return Path(explicit).expanduser()
    primary = Path("~/.config/based-clef/env").expanduser()
    legacy = Path("~/.config/decision/env").expanduser()
    if primary.exists() or not legacy.exists():
        return primary
    return legacy


def _resolve_log_dir() -> Path:
    explicit = os.environ.get("BASED_CLEF_LOG_DIR") or os.environ.get("DECISION_LOG_DIR")
    if explicit:
        return Path(explicit).expanduser()
    primary = Path("~/.local/state/based-clef").expanduser()
    legacy = Path("~/.local/state/decision-agent").expanduser()
    if primary.exists() or not legacy.exists():
        return primary
    return legacy


ENV_PATH = _resolve_env_path()
LOG_DIR = _resolve_log_dir()


def load_env(path: Path | None = None) -> None:
    """Load KEY=value lines into os.environ (existing env wins)."""
    candidates = [path] if path is not None else [
        _resolve_env_path(),
        Path("~/.config/decision/env").expanduser(),
    ]
    seen: set[Path] = set()
    for p in candidates:
        if p is None or p in seen or not p.exists():
            continue
        seen.add(p)
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


def _env_get(primary: str, legacy: str, default: Any = None) -> Any:
    if primary in os.environ:
        return os.environ[primary]
    if legacy in os.environ:
        return os.environ[legacy]
    return default


def _env_has(primary: str, legacy: str) -> bool:
    return primary in os.environ or legacy in os.environ


def _make_http_client(timeout: float | httpx.Timeout) -> httpx.AsyncClient:
    """Construct httpx.AsyncClient while tolerating IPv6 literals in NO_PROXY."""
    try:
        return httpx.AsyncClient(timeout=timeout)
    except httpx.InvalidURL:
        proxies = urllib.request.getproxies()
        verify: bool | str = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE") or True
        mounts: dict[str, httpx.AsyncBaseTransport | None] = {}
        for scheme in ("http", "https"):
            if proxies.get(scheme):
                mounts[f"{scheme}://"] = httpx.AsyncHTTPTransport(proxy=proxies[scheme], verify=verify)
        raw_no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
        for host in raw_no_proxy.split(","):
            h = host.strip()
            if not h or ":" in h:
                continue
            mounts[f"all://*{h}" if h.startswith(".") else f"all://{h}"] = None
        return httpx.AsyncClient(timeout=timeout, trust_env=False, verify=verify, mounts=mounts)


@dataclass
class Config:
    account_id: str = ""
    api_token: str = ""
    # Base URL is overridable so a self-hosted Jev-compatible server can be dropped in later.
    base_url: str = "https://api.cloudflare.com/client/v4"
    decision_model: str = "@cf/cloudflare/clef-flash"
    rewriter_model: str = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
    escalation_model: str = "@cf/meta/llama-4-scout-17b-16e-instruct"
    threshold: float = 0.70          # gate on Clef `confidence`
    max_attempts: int = 3            # Clef attempts per step before LLM decides
    max_steps: int = 25              # total step budget per task
    sensitive_threshold: float = 0.5 # Clef yes/no 'irreversible/sensitive?' gate
    ask_timeout: float = 120.0       # seconds before an unanswered prompt auto-denies
    max_elements: int = 80           # element list cap (keeps state under Clef context)
    log_lines: int = 100             # ring buffer length for UI + on-disk tail
    headless: bool = False
    driver: str = "browser"          # browser | desktop
    vision: bool = True
    max_images: int = 1
    image_quality: int = 60
    highlight: bool = False
    highlight_delay: float = 0.3
    step_delay: float = 1.0          # pause after every action, for human observability while testing
    highlight_decay: float = 1.5     # seconds the target outline takes to fade (0 = no outline)
    highlight_color: str = "#ff33cc"
    highlight_thickness: int = 4
    plan_first: bool = True          # compile an intended trajectory / playbook before step 1
    voice_prompt: bool = False
    target_window: str = "Google Chrome"
    mode: str = "autopilot"          # autopilot | preview | step
    offline: bool = False            # deterministic local heuristic engine (no API token needed)
    browser_path: str = ""           # optional explicit Chrome/Chromium binary path
    save_playbook: str = ""          # optional path to export verified playbook JSON

    @classmethod
    def from_env(cls) -> "Config":
        load_env()
        c = cls(
            account_id=str(_env_get("BASED_CLEF_ACCOUNT_ID", "CLOUDFLARE_ACCOUNT_ID", "")),
            api_token=str(_env_get("BASED_CLEF_API_TOKEN", "CLOUDFLARE_API_TOKEN", "")),
        )
        c.base_url = str(_env_get("BASED_CLEF_BASE_URL", "DECISION_BASE_URL", c.base_url))
        c.decision_model = str(_env_get("BASED_CLEF_MODEL", "DECISION_MODEL", c.decision_model))
        c.rewriter_model = str(_env_get("BASED_CLEF_REWRITER", "DECISION_REWRITER", c.rewriter_model))
        c.escalation_model = str(_env_get("BASED_CLEF_ESCALATION", "DECISION_ESCALATION", c.escalation_model))
        c.threshold = float(_env_get("BASED_CLEF_THRESHOLD", "DECISION_THRESHOLD", c.threshold))
        c.max_attempts = int(_env_get("BASED_CLEF_MAX_ATTEMPTS", "DECISION_MAX_ATTEMPTS", c.max_attempts))
        c.max_steps = int(_env_get("BASED_CLEF_MAX_STEPS", "DECISION_MAX_STEPS", c.max_steps))
        c.ask_timeout = float(_env_get("BASED_CLEF_ASK_TIMEOUT", "DECISION_ASK_TIMEOUT", c.ask_timeout))
        if _env_has("BASED_CLEF_DRIVER", "DECISION_DRIVER"):
            c.driver = str(_env_get("BASED_CLEF_DRIVER", "DECISION_DRIVER", c.driver))
        if _env_has("BASED_CLEF_VISION", "DECISION_VISION"):
            c.vision = str(_env_get("BASED_CLEF_VISION", "DECISION_VISION", "")).lower() in ("1", "true", "yes")
        if _env_has("BASED_CLEF_MAX_IMAGES", "DECISION_MAX_IMAGES"):
            c.max_images = int(_env_get("BASED_CLEF_MAX_IMAGES", "DECISION_MAX_IMAGES", c.max_images))
        if _env_has("BASED_CLEF_IMAGE_QUALITY", "DECISION_IMAGE_QUALITY"):
            c.image_quality = int(_env_get("BASED_CLEF_IMAGE_QUALITY", "DECISION_IMAGE_QUALITY", c.image_quality))
        if _env_has("BASED_CLEF_HIGHLIGHT", "DECISION_HIGHLIGHT"):
            c.highlight = str(_env_get("BASED_CLEF_HIGHLIGHT", "DECISION_HIGHLIGHT", "")).lower() in ("1", "true", "yes")
        if _env_has("BASED_CLEF_HIGHLIGHT_DELAY", "DECISION_HIGHLIGHT_DELAY"):
            c.highlight_delay = float(_env_get("BASED_CLEF_HIGHLIGHT_DELAY", "DECISION_HIGHLIGHT_DELAY", c.highlight_delay))
        if _env_has("BASED_CLEF_STEP_DELAY", "DECISION_STEP_DELAY"):
            c.step_delay = float(_env_get("BASED_CLEF_STEP_DELAY", "DECISION_STEP_DELAY", c.step_delay))
        if _env_has("BASED_CLEF_HIGHLIGHT_DECAY", "DECISION_HIGHLIGHT_DECAY"):
            c.highlight_decay = float(_env_get("BASED_CLEF_HIGHLIGHT_DECAY", "DECISION_HIGHLIGHT_DECAY", c.highlight_decay))
        if _env_has("BASED_CLEF_PLAN", "DECISION_PLAN"):
            c.plan_first = str(_env_get("BASED_CLEF_PLAN", "DECISION_PLAN", "")).lower() in ("1", "true", "yes")
        if _env_has("BASED_CLEF_VOICE_PROMPT", "DECISION_VOICE_PROMPT"):
            c.voice_prompt = str(_env_get("BASED_CLEF_VOICE_PROMPT", "DECISION_VOICE_PROMPT", "")).lower() in ("1", "true", "yes")
        if _env_has("BASED_CLEF_MODE", "DECISION_MODE"):
            c.mode = str(_env_get("BASED_CLEF_MODE", "DECISION_MODE", c.mode))
        if _env_has("BASED_CLEF_OFFLINE", "DECISION_OFFLINE"):
            c.offline = str(_env_get("BASED_CLEF_OFFLINE", "DECISION_OFFLINE", "")).lower() in ("1", "true", "yes")
        if _env_has("BASED_CLEF_BROWSER_PATH", "DECISION_BROWSER_PATH"):
            c.browser_path = str(_env_get("BASED_CLEF_BROWSER_PATH", "DECISION_BROWSER_PATH", ""))
        return c

    def has_credentials(self) -> bool:
        return bool(self.account_id and self.api_token)

    def require(self) -> None:
        if self.offline:
            return
        if not self.has_credentials():
            raise SystemExit(
                f"Missing CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN (looked in {_resolve_env_path()}).\n"
                "Run `bclef init` to configure Cloudflare Workers AI credentials, or pass `--offline` for local sandbox mode."
            )


# ===== events =====


MAX_LOG_BYTES = 2_000_000  # rotate jsonl at ~2 MB, keep one backup


@dataclass
class Stats:
    state: str = "idle"            # idle|planning|observing|orienting|deciding|rewriting|llm|acting|waiting|hold|done|error
    model: str = "-"
    task: str = ""
    step: int = 0
    attempt: int = 0
    last_latency_ms: float = 0.0
    last_confidence: float | None = None
    p_target: float | None = None
    p_action: float | None = None
    p_done: float | None = None
    p_sensitive: float | None = None
    stage_index: int = 0
    stage_total: int = 0
    stage_label: str = ""
    clef_calls: int = 0
    llm_calls: int = 0
    in_tokens: int = 0
    out_tokens: int = 0
    stream_tps: float = 0.0        # live tokens/sec of the active LLM stream
    stream_chars: int = 0
    started: float = field(default_factory=time.time)
    state_since: float = field(default_factory=time.time)

    def elapsed(self) -> float:
        return time.time() - self.started


class Telemetry:
    def __init__(self, max_lines: int = 100, log_path: Path | None = None) -> None:
        self.lines: deque[str] = deque(maxlen=max_lines)
        self.stats = Stats()
        self._subs: list[Callable[[dict], None]] = []
        self._lock = threading.RLock()
        self.log_path = log_path or (LOG_DIR / "events.jsonl")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._stream_start = 0.0
        self._stream_tokens = 0
        # RAM-only live viewport cache for Web Studio / HUD (never persisted to disk logs)
        self.last_frame_b64: str = ""
        self.last_snapshot: dict[str, Any] = {}

    # -- subscription -----------------------------------------------------
    def subscribe(self, fn: Callable[[dict], None]) -> None:
        self._subs.append(fn)

    def unsubscribe(self, fn: Callable[[dict], None]) -> None:
        if fn in self._subs:
            self._subs.remove(fn)

    # -- RAM viewport update (never written to events.jsonl) ---------------
    def update_frame(self, snap: dict[str, Any], frame_b64: str | None = None) -> None:
        with self._lock:
            self.last_snapshot = snap
            if frame_b64 is not None:
                self.last_frame_b64 = frame_b64
        ev = {
            "t": round(time.time(), 3),
            "kind": "viewport",
            "msg": snap.get("title", ""),
            "url": snap.get("url", ""),
            "title": snap.get("title", ""),
            "elements": snap.get("elements", []),
            "has_frame": bool(frame_b64 or self.last_frame_b64),
        }
        for fn in list(self._subs):
            try:
                fn(ev)
            except Exception:
                pass

    # -- core emit --------------------------------------------------------
    def emit(self, kind: str, msg: str = "", **data) -> None:
        ev = {"t": round(time.time(), 3), "kind": kind, "msg": msg, **data}
        with self._lock:
            ts = time.strftime("%H:%M:%S", time.localtime(ev["t"]))
            self.lines.append(f"{ts} {kind:<8} {msg}".rstrip())
            self._write(ev)
        for fn in list(self._subs):
            try:
                fn(ev)
            except Exception:  # a broken UI must never kill the agent
                pass

    def _write(self, ev: dict) -> None:
        try:
            if self.log_path.exists() and self.log_path.stat().st_size > MAX_LOG_BYTES:
                self.log_path.replace(self.log_path.with_suffix(".jsonl.1"))
            with self.log_path.open("a") as f:
                f.write(json.dumps(ev, default=str) + "\n")
        except OSError:
            pass

    # -- state helpers ----------------------------------------------------
    def set_state(self, state: str, model: str | None = None) -> None:
        with self._lock:
            self.stats.state = state
            self.stats.state_since = time.time()
            if model:
                self.stats.model = model
        self.emit("state", state + (f" [{model}]" if model else ""))

    def clef_done(
        self,
        latency_ms: float,
        confidence: float | None,
        in_tokens: int,
        answers: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            s = self.stats
            s.clef_calls += 1
            s.last_latency_ms = latency_ms
            s.last_confidence = confidence
            s.in_tokens += in_tokens
            if answers:
                if "target" in answers:
                    s.p_target = answers["target"].get("confidence")
                if "action" in answers:
                    s.p_action = answers["action"].get("confidence")
                if "done" in answers:
                    s.p_done = answers["done"].get("noul")
                if "sensitive" in answers:
                    s.p_sensitive = answers["sensitive"].get("noul")
        self.emit(
            "clef",
            f"{latency_ms:.0f} ms conf={confidence if confidence is None else round(confidence, 3)} in={in_tokens}tok",
            latency_ms=round(latency_ms, 1),
            confidence=confidence,
            p_target=self.stats.p_target,
            p_action=self.stats.p_action,
            p_done=self.stats.p_done,
            p_sensitive=self.stats.p_sensitive,
        )

    # -- LLM streaming throughput ----------------------------------------
    def stream_begin(self) -> None:
        with self._lock:
            self._stream_start = time.time()
            self._stream_tokens = 0
            self.stats.stream_tps = 0.0
            self.stats.stream_chars = 0
            self.stats.llm_calls += 1

    def stream_chunk(self, text: str) -> None:
        """Each SSE chunk is ~1 token; good enough for a live throughput gauge."""
        text = str(text)
        with self._lock:
            self._stream_tokens += 1
            self.stats.out_tokens += 1
            self.stats.stream_chars += len(text)
            dt = time.time() - self._stream_start
            self.stats.stream_tps = self._stream_tokens / dt if dt > 0.05 else 0.0
        self.emit("stream", text.replace("\n", "⏎"), tps=round(self.stats.stream_tps, 1))

    def stream_end(self) -> None:
        with self._lock:
            tps = self.stats.stream_tps
        self.emit("llm", f"stream done {self._stream_tokens} chunks @ {tps:.1f}/s")

    def tail(self, n: int | None = None) -> list[str]:
        with self._lock:
            ls = list(self.lines)
        return ls[-n:] if n else ls


# ===== clients =====


class ClefClient:
    """Jev-compatible decision client. Swap `base_url`/model to self-host later."""

    def __init__(self, cfg: Config, tel: Telemetry, http: httpx.AsyncClient | None = None) -> None:
        self.cfg, self.tel = cfg, tel
        self.http = http or _make_http_client(timeout=60)

    def _url(self) -> str:
        return f"{self.cfg.base_url}/accounts/{self.cfg.account_id}/ai/run/{self.cfg.decision_model}"

    async def decide(self, state: Any, questions: dict, images_b64: list[str] | None = None) -> dict:
        model_sel = self.cfg.decision_model.rsplit("/", 1)[-1]
        body: dict = {"model": model_sel, "state": state, "questions": questions}
        if images_b64:
            cap = min(len(images_b64), 4, self.cfg.max_images)
            formatted = []
            for img in images_b64[:cap]:
                if not img.startswith("data:"):
                    img = f"data:image/jpeg;base64,{img}"
                formatted.append(img)
            if formatted:
                body["images"] = formatted
        self.tel.set_state("deciding", self.cfg.decision_model)
        t0 = time.perf_counter()
        r = await self.http.post(self._url(), json=body, headers={"Authorization": f"Bearer {self.cfg.api_token}"})
        ms = (time.perf_counter() - t0) * 1000
        if r.status_code >= 400:
            self.tel.emit("error", f"clef HTTP {r.status_code}: {r.text[:200]}")
            r.raise_for_status()
        body = r.json()
        res = body.get("result", body)
        answers = res.get("answers", {})
        conf = min((a.get("confidence") for a in answers.values() if a.get("confidence") is not None), default=None)
        self.tel.clef_done(ms, conf, res.get("usage", {}).get("input_tokens", 0), answers=answers)
        return res


class LLMClient:
    """Streaming chat via Workers AI's OpenAI-compatible endpoint."""

    def __init__(self, cfg: Config, tel: Telemetry, http: httpx.AsyncClient | None = None) -> None:
        self.cfg, self.tel = cfg, tel
        self.http = http or _make_http_client(timeout=httpx.Timeout(120, connect=10))

    def _url(self) -> str:
        return f"{self.cfg.base_url}/accounts/{self.cfg.account_id}/ai/v1/chat/completions"

    async def stream(self, model: str, messages: list[dict], **kw) -> AsyncIterator[str]:
        body = {"model": model, "messages": messages, "stream": True, **kw}
        self.tel.set_state("llm", model)
        self.tel.stream_begin()
        try:
            async with self.http.stream("POST", self._url(), json=body,
                                        headers={"Authorization": f"Bearer {self.cfg.api_token}"}) as r:
                if r.status_code >= 400:
                    txt = (await r.aread()).decode(errors="replace")[:300]
                    self.tel.emit("error", f"llm HTTP {r.status_code}: {txt}")
                    r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        j = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    delta = ""
                    if "choices" in j and j["choices"]:
                        delta = (j["choices"][0].get("delta") or {}).get("content") or ""
                    elif "response" in j:
                        delta = j["response"] or ""
                    if delta:
                        delta = str(delta)
                        self.tel.stream_chunk(delta)
                        yield delta
        finally:
            self.tel.stream_end()

    async def complete(self, model: str, messages: list[dict], **kw) -> str:
        return "".join([c async for c in self.stream(model, messages, **kw)])


# ===== deterministic offline / sandbox clients (zero API keys needed) =====

_SENSITIVE_HINTS = (
    "pay", "buy", "purchase", "delete", "remove", "send", "submit order",
    "place order", "confirm", "checkout", "transfer", "unsubscribe", "cancel subscription",
)


class _NoopHttp:
    async def aclose(self) -> None:
        return None


class HeuristicClefClient:
    """Deterministic System 1 simulator for `--offline` demos, CI verification, and sandbox previews."""

    def __init__(self, cfg: Config, tel: Telemetry) -> None:
        self.cfg, self.tel = cfg, tel
        self.http = _NoopHttp()

    async def decide(self, state: Any, questions: dict, images_b64: list[str] | None = None) -> dict:
        self.tel.set_state("deciding", "offline/clef-heuristic")
        t0 = time.perf_counter()

        # Handle generic one-shot `da ask` questions as well as full browser states
        if not isinstance(state, dict) or "elements" not in state:
            answers: dict[str, Any] = {}
            for qk, qv in questions.items():
                if qv.get("type") == "choice":
                    first_key = next(iter(qv.get("criteria", {"0": "none"})))
                    answers[qk] = {"choice": first_key, "confidence": 0.88}
                else:
                    answers[qk] = {"noul": 0.5, "confidence": 0.85}
            ms = max(4.0, (time.perf_counter() - t0) * 1000)
            self.tel.clef_done(ms, 0.88, len(str(state)) // 4, answers=answers)
            return {"answers": answers}

        els: list[dict[str, Any]] = state.get("elements", [])
        page_text = (state.get("page_text") or "").lower()
        task_text = str(questions.get("done", {}).get("instructions") or json.dumps(questions)).lower()
        stage_text = str(state.get("current_stage") or "").lower()
        notes_text = str(state.get("user_notes") or "").lower()
        hint_text = str(state.get("hint") or "").lower()
        directive = f"{notes_text} {stage_text} {hint_text} {task_text}"

        # 1. Check completion signals on page (or all planned stages verified)
        done_p = 0.04
        if any(phrase in page_text for phrase in ("you are now logged in", "welcome back!", "key generated:", "task complete")):
            if "log out" not in directive:
                done_p = 0.96
        elif "all " in stage_text and "planned stages completed" in stage_text:
            done_p = 0.94

        # 2. Check explicit `#id` directive in user notes or hint
        explicit_id = None
        m_id = re.search(r"#(\d+)\b", f"{notes_text} {hint_text}")
        if m_id:
            cand_id = int(m_id.group(1))
            explicit_id = next((e for e in els if int(e["id"]) == cand_id), None)

        chosen_el: dict[str, Any] | None = explicit_id
        chosen_action = "wait"

        if done_p >= 0.85:
            chosen_el = None
            chosen_action = "wait"
        elif chosen_el is not None:
            is_textbox = chosen_el.get("tag") in ("input", "textarea", "select") and chosen_el.get("type") not in (
                "submit", "button", "checkbox", "radio"
            )
            chosen_action = "fill" if is_textbox else "click"
        else:
            # Score fillable inputs/selects and clickable buttons against active stage & task intent
            best_inp = None
            best_inp_score = -1
            for e in els:
                tag = e.get("tag")
                etype = e.get("type")
                if tag == "select":
                    cur_val = str(e.get("value") or "").lower()
                    opts = [o.strip().lower() for o in str(e.get("options") or "").split(",") if o.strip()]
                    wanted_opt = next((o for o in opts if o != cur_val and (o in stage_text or o in task_text)), None)
                    if wanted_opt:
                        nm = (e.get("name", "") + " " + wanted_opt).lower()
                        tokens = [w for w in re.split(r"\W+", nm) if len(w) >= 3]
                        score = 4 + sum(3 for w in tokens if w in stage_text) + sum(1 for w in tokens if w in task_text)
                        if score > best_inp_score:
                            best_inp_score = score
                            best_inp = e
                elif tag in ("input", "textarea") and e.get("empty") is True and etype not in ("submit", "button", "checkbox", "radio", "hidden"):
                    nm = (e.get("name", "") + " " + str(etype or "")).lower()
                    tokens = [w for w in re.split(r"\W+", nm) if len(w) >= 3]
                    score = sum(4 for w in tokens if w in stage_text) + sum(2 for w in tokens if w in task_text)
                    if e.get("required"):
                        score += 2
                    if score > best_inp_score:
                        best_inp_score = score
                        best_inp = e

            clickables = [
                e for e in els
                if e.get("tag") in ("button", "a")
                or e.get("role") in ("button", "link", "tab")
                or e.get("type") in ("submit", "button")
            ]
            best_btn = None
            best_btn_score = -1
            for e in clickables:
                nm = e.get("name", "").lower()
                if not nm or nm in ("forgot password?", "log out"):
                    score = 0
                else:
                    words = [w for w in re.split(r"\W+", nm) if len(w) >= 3]
                    score = sum(4 for w in words if w in stage_text) + sum(2 for w in words if w in task_text)
                    if e.get("type") == "submit" or any(k in nm for k in ("sign in", "log in", "submit", "continue")):
                        score += 1
                if score > best_btn_score:
                    best_btn_score = score
                    best_btn = e

            if best_inp is not None and (best_inp_score > 0 and best_inp_score >= best_btn_score):
                chosen_el = best_inp
                chosen_action = "fill"
            elif best_btn is not None and best_btn_score > 0:
                chosen_el = best_btn
                chosen_action = "click"
            elif best_inp is not None:
                chosen_el = best_inp
                chosen_action = "fill"
            elif best_btn is not None:
                chosen_el = best_btn
                chosen_action = "click"

        target_choice = str(chosen_el["id"]) if chosen_el else "0"
        el_name_low = (chosen_el.get("name", "") if chosen_el else "").lower()
        sens_p = 0.92 if (chosen_action == "click" and any(w in el_name_low for w in _SENSITIVE_HINTS)) else 0.03
        conf = 0.94 if (chosen_el or done_p >= 0.85) else 0.76

        answers = {
            "done": {"noul": done_p, "confidence": 0.95},
            "target": {"choice": target_choice, "confidence": conf},
            "action": {"choice": chosen_action, "confidence": conf},
            "sensitive": {"noul": sens_p, "confidence": 0.94},
        }
        if "stage_done" in questions:
            answers["stage_done"] = {"noul": 0.0, "confidence": 0.90}

        ms = max(6.0, (time.perf_counter() - t0) * 1000)
        in_tok = max(32, len(json.dumps(state)) // 4)
        self.tel.clef_done(ms, conf, in_tok, answers=answers)
        return {"answers": answers, "usage": {"input_tokens": in_tok}}


class HeuristicLLMClient:
    """Offline companion for HeuristicClefClient so `--offline` requires zero network calls."""

    def __init__(self, cfg: Config, tel: Telemetry) -> None:
        self.cfg, self.tel = cfg, tel
        self.http = _NoopHttp()

    async def complete(self, model: str, messages: list[dict], **kw) -> str:
        self.tel.set_state("llm", "offline/heuristic-planner")
        # Return empty JSON so IntentCompiler uses its deterministic DOM-grounded stages cleanly
        return "{}"
