"""The decision loop: Intent -> Playbook -> Clef System 1 -> LLM rewrite -> Escalation, with HITL safety.

Human-in-the-loop controls live here too:
  * hold(on)      silent freeze: cancels in-flight model calls, discards the pending action,
                  and on release re-observes from scratch (no log noise, no acknowledgement).
  * add_note()    separate channel: a hint included in every Clef state / LLM prompt
                  (persistent) or only the next decision (one-shot).
  * ask callback  approval before sensitive/irreversible actions, blueprint preview confirmation,
                  values the agent lacks (masked for passwords), and help when the ladder is stuck.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from .browser import BrowserDriver
from .core import APP_NAME, LOG_DIR, MAX_LOG_BYTES, ClefClient, Config, LLMClient, Telemetry
from .intent import Playbook, Stage, compile_intent_fast, extract_url_from_intent

ACTIONS = {
    "click": "Click an element",
    "fill": "Type text into a textbox or select an option",
    "press": "Press Enter to submit active input",
    "scroll": "Scroll down for more",
    "wait": "Wait; page still loading",
}
SENSITIVE_WORDS = (
    "pay", "buy", "purchase", "delete", "remove", "send", "submit order", "place order", "confirm",
    "checkout", "transfer", "unsubscribe", "cancel subscription",
)
OAUTH_WORDS = (
    "sign in with google", "continue with google", "sign in with github", "continue with github",
    "authorize", "oauth", "two-factor", "verification code", "authenticator", "passkey", "captcha",
    "verify you are human",
)


def _resolve_secrets_path() -> Path:
    explicit = os.environ.get("BASED_CLEF_SECRETS") or os.environ.get("DECISION_SECRETS")
    if explicit:
        return Path(explicit).expanduser()
    primary = Path("~/.config/based-clef/secrets.env").expanduser()
    legacy = Path("~/.config/decision/secrets.env").expanduser()
    if primary.exists() or not legacy.exists():
        return primary
    return legacy


SECRETS_PATH = _resolve_secrets_path()


def _voice_prompt(text: str) -> None:
    """Publish audible voice prompt to unified MQTT TTS (`voice/say`) without blocking."""
    payload = json.dumps({"source": APP_NAME, "text": text})
    for cmd in (
        ["mosquitto_pub", "-h", "127.0.0.1", "-p", "1883", "-t", "voice/say", "-m", payload],
        ["notify-send", "-u", "normal", APP_NAME, text],
    ):
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass


# ask(kind, prompt, secret, name) -> {"ok": bool, "value": str|None, "save": bool}
#   kind: "approve" | "preview" | "value" | "stuck"
AskFn = Callable[[str, str, bool, str | None], Awaitable[dict]]


# -- secrets: RAM during the run, persisted to secrets.env only on task success ----------
def load_secrets(path: Path | None = None) -> dict[str, str]:
    target = path if path is not None else SECRETS_PATH
    if not target.exists():
        return {}
    if target.stat().st_mode & 0o077:
        raise SystemExit(f"{target} is group/world-accessible; run: chmod 600 {target}")
    out = {}
    for line in target.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def save_secrets(new: dict[str, str], path: Path | None = None) -> None:
    target = path if path is not None else SECRETS_PATH
    merged = load_secrets(target) | new
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("".join(f"{k}={v}\n" for k, v in merged.items()))
    os.chmod(target, 0o600)


def redact_secret(name: str, path: Path | None = None) -> None:
    target = path if path is not None else SECRETS_PATH
    kept = {k: v for k, v in load_secrets(target).items() if k != name}
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("".join(f"{k}={v}\n" for k, v in kept.items()))


def _compact(snap: dict, hint: str = "", notes: str = "", current_stage: str = "") -> dict:
    els = [
        {k: v for k, v in e.items() if k != "bbox" and (v not in (None, "", False) or k in ("empty",))}
        for e in snap["elements"]
    ]
    st: dict[str, Any] = {
        "url": snap["url"],
        "title": snap["title"],
        "scroll_y/max": snap["scroll"],
        "page_text": snap["text"],
        "elements": els,
    }
    if current_stage:
        st["current_stage"] = current_stage
    if hint:
        st["hint"] = hint
    if notes:
        st["user_notes"] = notes
    return st


def _questions(task: str, snap: dict, stage_context: str = "") -> dict:
    ids = {
        str(e["id"]): f'{e["role"]} "{e["name"]}"' + (f' value="{e["value"]}"' if e["value"] else "")
        for e in snap["elements"]
    }
    criteria = {**ids, "0": "none of these"}
    if len(criteria) < 2:
        criteria["-1"] = "wait / page loading"
    stage_suffix = f" Active stage: {stage_context}." if stage_context else ""
    return {
        "done": {"type": "noul", "instructions": f"Has this task been fully completed? Task: {task}"},
        "target": {
            "type": "choice",
            "criteria": criteria,  # Clef needs >=2 options
            "instructions": (
                f"Which element should be acted on next to make progress? Prefer empty required "
                f"fields before submitting.{stage_suffix} Task: {task}"
            ),
        },
        "action": {
            "type": "choice",
            "criteria": ACTIONS,
            "instructions": "What kind of action should be taken next?",
        },
        "sensitive": {
            "type": "noul",
            "instructions": (
                "Would the most likely next click be irreversible or sensitive "
                "(a payment, purchase, deletion, sending a message, or confirming an order)? "
                "Logging in or navigating is NOT sensitive."
            ),
        },
    }


def _gate(res: dict) -> tuple[float, bool, dict]:
    a = res["answers"]
    conf = min(a["target"].get("confidence", 0), a["action"].get("confidence", 0))
    done = a["done"].get("noul", 0.0)
    return conf, done >= 0.85, {
        "target": a["target"].get("choice"),
        "action": a["action"].get("choice"),
        "done_p": done,
        "sensitive_p": a.get("sensitive", {}).get("noul", 0.0),
        "stage_done_p": a.get("stage_done", {}).get("noul", 0.0),
        "confidence": conf,
    }


def _json_from(text: str) -> dict:
    cleaned = re.sub(r":\s*,", ": false,", text)
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    for chunk in re.findall(r"\{[^{}]*\}", cleaned):
        try:
            return json.loads(chunk)
        except json.JSONDecodeError:
            continue
    m = re.search(r"\{.*\}", cleaned, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "value"


class Agent:
    def __init__(self, cfg: Config, tel: Telemetry, clef: ClefClient, llm: LLMClient, browser: BrowserDriver) -> None:
        self.cfg, self.tel, self.clef, self.llm, self.br = cfg, tel, clef, llm, browser
        self.stop_flag = False
        self.train_log = LOG_DIR / "training.jsonl"
        self.ask: AskFn | None = None           # set by the UI; None = deny/skip automatically
        self.held = False
        self._gate_ev = asyncio.Event()
        self._gate_ev.set()
        self._step_task: asyncio.Task | None = None
        self.notes: list[tuple[str, bool]] = []  # (text, remember)
        self.data: dict[str, str] = {}
        self.new_secrets: dict[str, str] = {}    # typed this run; saved only on success
        self.on_saved: Callable[[list[str]], None] | None = None
        self.last_action: str | None = None
        self.last_target: str | None = None
        self.last_effect: bool | None = None
        self.playbook: Playbook | None = None

    # -- controls (call on the agent's event loop) ------------------------------------
    def stop(self) -> None:
        self.stop_flag = True
        self.hold(False)

    def hold(self, on: bool) -> None:
        """Silent interrupt: cancel the current step so nothing in flight can act."""
        self.held = on
        if on:
            self._gate_ev.clear()
            self.tel.stats.state = "hold"
            self.tel.stats.state_since = time.time()
            if self._step_task and not self._step_task.done():
                self._step_task.cancel()
        else:
            self._gate_ev.set()

    def add_note(self, text: str, remember: bool = True) -> None:
        self.notes.append((text, remember))
        self.tel.emit("note", ("remembered: " if remember else "next step: ") + text)

    def _notes_text(self) -> str:
        return " | ".join(t for t, _ in self.notes)

    def _drop_oneshot_notes(self) -> None:
        self.notes = [(t, keep) for t, keep in self.notes if keep]

    def _sync_playbook_stats(self) -> None:
        s = self.tel.stats
        if not self.playbook:
            s.stage_index, s.stage_total, s.stage_label = 0, 0, ""
            return
        idx, total, label = self.playbook.active_index_and_total()
        s.stage_index, s.stage_total, s.stage_label = idx, total, label

    async def _ask(self, kind: str, prompt: str, secret: bool = False, name: str | None = None) -> dict:
        if self.cfg.voice_prompt:
            _voice_prompt(f"Based Clef needs input: {prompt}")
        if not self.ask:
            return {"ok": False, "value": None, "save": False}
        prev = self.tel.stats.state
        self.tel.stats.state = "waiting"
        self.tel.emit("ask", f"{kind}: {prompt}", ask_kind=kind, prompt=prompt, secret=secret, name=name)
        try:
            return await self.ask(kind, prompt, secret, name)
        finally:
            self.tel.stats.state = prev

    # -- main loop --------------------------------------------------------------------
    async def run(
        self,
        task: str,
        data: dict[str, str] | None = None,
        url: str | None = None,
        playbook: Playbook | None = None,
        save_playbook_path: str | Path | None = None,
    ) -> bool:
        self.playbook = playbook
        effective_task = task or (playbook.goal if playbook else "")
        self.data = {**load_secrets(), **(playbook.extracted_data if playbook else {}), **(data or {})}
        self.new_secrets.clear()
        s = self.tel.stats
        s.task, s.step, s.started = effective_task, 0, time.time()
        self.tel.emit("task", effective_task)

        # Auto-extract URL from natural intent or provided Playbook if `--url` wasn't passed
        start_url = url or (playbook.start_url if playbook else None) or extract_url_from_intent(effective_task)
        if start_url and hasattr(self.br, "goto"):
            await self.br.goto(start_url)

        await self._plan(effective_task, start_url=start_url)

        # Blueprint Preview gate (`--mode preview` / `--preview`): let human inspect/edit before step 1
        if self.cfg.mode == "preview" and self.playbook:
            summary = " -> ".join(f"[{st.id}] {st.intent}" for st in self.playbook.stages[:6])
            r = await self._ask("preview", f"Blueprint ready ({len(self.playbook.stages)} stages): {summary}. Execute?")
            if not r["ok"]:
                self.tel.set_state("idle")
                self.tel.emit("task", "stopped: blueprint preview cancelled")
                return False
            if r.get("value"):
                self.add_note(str(r["value"]), remember=True)

        step = noeffect = 0
        while step < self.cfg.max_steps:
            await self._gate_ev.wait()
            if self.stop_flag:
                self.tel.set_state("idle")
                self.tel.emit("task", "stopped")
                return False
            self._step_task = asyncio.ensure_future(self._step(effective_task, step + 1))
            await asyncio.wait({self._step_task})
            if self._step_task.cancelled():
                continue                      # held: re-observe and redo this step, attempt counter reset
            result = self._step_task.result()
            step += 1
            if result == "done":
                if self.playbook:
                    self.playbook.mark_all_done()
                    self._sync_playbook_stats()
                    self.tel.emit(
                        "stage",
                        f"all {len(self.playbook.stages)} stages complete",
                        stages=[st.to_dict() for st in self.playbook.stages],
                    )
                    out_pb = save_playbook_path or self.cfg.save_playbook
                    if out_pb:
                        saved_p = self.playbook.save(out_pb)
                        self.tel.emit("playbook", f"saved verified playbook to {saved_p}", path=str(saved_p))
                self.tel.set_state("done")
                self.tel.emit("task", f"complete in {step} steps, {s.elapsed():.1f}s")
                self._persist_secrets()
                return True
            if result == "denied":
                self.tel.emit("task", "stopped: action denied")
                self.tel.set_state("idle")
                return False
            noeffect = noeffect + 1 if result == "noeffect" else 0
            if noeffect >= 2:
                r = await self._ask("stuck", "No progress after two tries. Type a hint to retry, or Deny to stop.")
                if not r["ok"]:
                    self.tel.set_state("idle")
                    self.tel.emit("task", "stopped: stuck")
                    return False
                if r.get("value"):
                    self.add_note(r["value"], remember=True)
                noeffect = 0
        self.tel.set_state("error")
        self.tel.emit("error", f"step budget {self.cfg.max_steps} exhausted")
        return False

    async def _plan(self, task: str, start_url: str | None = None) -> Playbook:
        """Compile intent + initial screen state into an explicit, live-tracked `Playbook`."""
        self.tel.set_state("planning")
        snap = await self.br.snapshot()
        self.tel.update_frame(snap)
        self.tel.emit(
            "initial",
            f'{snap["title"]} @ {snap["url"]}',
            task=task,
            title=snap["title"],
            url=snap["url"],
            elements=len(snap["elements"]),
        )

        if self.playbook is None:
            pb = compile_intent_fast(task, url=start_url, data=self.data, snap=snap, driver=self.cfg.driver)
            # Merge non-secret inline parameters (e.g. search query, inline email) without overriding explicit -d data
            for k, v in pb.extracted_data.items():
                self.data.setdefault(k, v)
        else:
            pb = self.playbook

        if start_url:
            pb.mark_navigated(start_url)

        if self.cfg.plan_first and not self.cfg.offline and self.playbook is None:
            els = "\n".join(f'{e["role"]} "{e["name"]}"' for e in snap["elements"][:40])
            prompt = (
                f"Task: {task}\nData keys: {list(self.data)}\nCurrent screen: {snap['title']} ({snap['url']})\n"
                f"Visible elements:\n{els}\n"
                "Decompose this goal into short, concrete UI action stages in order (max 8). "
                "Reply ONLY a JSON object: "
                '{"stages": [{"intent": "...", "kind": "fill|click|press|scroll|verify", "target_hint": "...", "sensitive": false}]}'
            )
            try:
                out = await self.llm.complete(self.cfg.escalation_model, [{"role": "user", "content": prompt}], max_tokens=320)
                parsed = _json_from(out)
                raw_stages = parsed.get("stages") or parsed.get("steps") or []
                if raw_stages and isinstance(raw_stages, list):
                    llm_stages: list[Stage] = []
                    for idx, item in enumerate(raw_stages[:10], 1):
                        if isinstance(item, dict):
                            llm_stages.append(Stage.from_dict(item, idx))
                        elif isinstance(item, str) and item.strip():
                            llm_stages.append(Stage(id=idx, intent=str(item)[:140]))
                    if llm_stages:
                        pb.stages = llm_stages
                        pb.ensure_active()
            except Exception as e:  # noqa: BLE001  planning is advisory; never block the run
                self.tel.emit("warn", f"planner unavailable: {type(e).__name__}: {str(e)[:100]}")

        self.playbook = pb
        self._sync_playbook_stats()
        step_texts = [st.intent for st in pb.stages]
        self.tel.emit(
            "plan",
            f"{len(pb.stages)} intended stages",
            steps=step_texts,
            stages=[st.to_dict() for st in pb.stages],
            playbook=pb.to_dict(),
            missing_keys=pb.missing_keys(self.data),
        )
        return pb

    async def _orient(self, task: str, snap: dict) -> dict[str, Any]:
        """Pre-flight orientation: 'Where am I right now and what is in front of me?'"""
        self.tel.set_state("orienting")
        title = snap.get("title", "Unknown")
        url = snap.get("url", "")
        n_els = len(snap.get("elements", []))

        # Check if the active window/page matches target expectations
        is_target_lost = False
        target_win = getattr(self.br, "target_window", None)
        if isinstance(target_win, str) and target_win and target_win.lower() not in title.lower():
            is_target_lost = True
            self.tel.emit("warn", f"window drift detected: expected '{target_win}', active is '{title}'")

        # Healing context if last action had no visible effect
        healing_context = ""
        if self.last_effect is False and self.last_action:
            healing_context = (
                f"Previous action '{self.last_action}' on '{self.last_target or 'target'}' "
                "had no visible effect. State did not change. Select an alternative element or approach."
            )
            self.tel.emit("heal", healing_context)

        orient_summary = f"{title[:45]} ({n_els} els)" + (" [DRIFT]" if is_target_lost else "")
        self.tel.emit("orient", orient_summary, title=title, url=url, elements=n_els, drift=is_target_lost)
        return {
            "title": title,
            "url": url,
            "elements": n_els,
            "drift": is_target_lost,
            "healing_context": healing_context,
        }

    async def _step(self, task: str, step: int) -> str:
        s = self.tel.stats
        s.step = step
        self.tel.set_state("observing")
        snap = await self.br.snapshot()
        img_b64 = None
        if self.cfg.vision:
            try:
                img_b64 = await self.br.screenshot_b64(self.cfg.image_quality)
            except Exception as e:
                self.tel.emit("warn", f"screenshot failed: {e}")
        self.tel.update_frame(snap, img_b64)
        self.tel.emit("observe", f'{len(snap["elements"])} elements @ {snap["url"][:60]}' + (" [img]" if img_b64 else ""))

        orient = await self._orient(task, snap)
        if orient.get("drift") and hasattr(self.br, "_focus_target_window"):
            await self.br.settle()
            snap = await self.br.snapshot()
            self.tel.update_frame(snap)

        decision, via = await self._decide(task, snap, img_b64, healing_hint=orient.get("healing_context", ""))
        if decision.get("done"):
            return "done"

        el = self._resolve_el(snap, decision.get("target"))
        active_stage = self.playbook.ensure_active() if self.playbook else None

        # Step-by-step Co-Pilot gate (`--mode step`)
        if self.cfg.mode == "step":
            conf_val = decision.get("confidence")
            conf_txt = f"{conf_val:.2f}" if isinstance(conf_val, (int, float)) else "-"
            r = await self._ask(
                "approve",
                f'Step #{step}: {decision.get("action")} -> "{el["name"] if el else decision.get("target")}" (conf={conf_txt}). Execute?',
            )
            if not r["ok"]:
                return "denied"

        if decision.get("action") == "click" and await self._is_sensitive(decision, el, active_stage):
            r = await self._ask("approve", f'Sensitive action: click "{el["name"] if el else "?"}". Allow?')
            if not r["ok"]:
                return "denied"
        if decision.get("action") == "click" and self._is_oauth_gate(el, snap):
            r = await self._ask("approve", f'OAuth or credential check: "{el["name"] if el else snap.get("title", "auth")}". Proceed?')
            if not r["ok"]:
                return "denied"

        before = (snap["url"], snap.get("scroll"), snap.get("text", ""), json.dumps(snap["elements"], sort_keys=True))
        target_label = el["name"] if el else decision.get("target")
        self.tel.emit(
            "step",
            f'#{step} {decision.get("action")} -> {target_label}',
            n=step,
            action=decision.get("action"),
            target=target_label,
            el_id=el["id"] if el else None,
            bbox=el.get("bbox") if el else None,
            via=via,
            confidence=decision.get("confidence"),
        )

        self.last_action = decision.get("action")
        self.last_target = str(target_label) if target_label is not None else None

        ok = await self._act(decision, task, snap)
        await self.br.settle()
        if self.cfg.step_delay > 0:
            await asyncio.sleep(self.cfg.step_delay)     # live-tunable observability pause
        after = await self.br.snapshot()
        after_img = None
        if self.cfg.vision:
            try:
                after_img = await self.br.screenshot_b64(self.cfg.image_quality)
            except Exception:
                after_img = None
        self.tel.update_frame(after, after_img)

        changed = (after["url"], after.get("scroll"), after.get("text", ""), json.dumps(after["elements"], sort_keys=True)) != before
        self.last_effect = bool(ok and changed)
        self.tel.emit("result", "effect observed" if (ok and changed) else "no visible effect", n=step, ok=bool(ok and changed))
        self._log_example(task, snap, decision, via, ok and changed)

        if ok and changed and self.playbook:
            completed = self.playbook.record_effect(
                action=str(decision.get("action") or ""),
                target_name=str(target_label or ""),
                step_num=step,
                confidence=decision.get("confidence"),
                via=via,
                stage_done_p=float(decision.get("stage_done_p") or 0.0),
            )
            self._sync_playbook_stats()
            if completed:
                self.tel.emit(
                    "stage",
                    f"stage {completed.id}/{len(self.playbook.stages)} verified: {completed.intent}",
                    stage_id=completed.id,
                    stages=[st.to_dict() for st in self.playbook.stages],
                )

        if not (ok and changed):
            self.tel.emit("warn", f"action had no visible effect ({decision})")
            return "noeffect"
        return "ok"

    @staticmethod
    def _resolve_el(snap: dict, target: object) -> dict | None:
        if target in (None, "None", "null", "", "0", "-1"):
            return None
        t_str = str(target).strip()
        for e in snap.get("elements", []):
            if str(e["id"]) == t_str:
                return e
        t_low = t_str.lower()
        for e in snap.get("elements", []):
            if e.get("name", "").strip().lower() == t_low:
                return e
        return None

    async def _is_sensitive(self, d: dict, el: dict | None, active_stage: Stage | None = None) -> bool:
        name = (el["name"] if el else "").lower()
        stage_sens = bool(active_stage and active_stage.sensitive and active_stage.target_hint and active_stage.target_hint.lower() in name)
        return (
            d.get("sensitive_p", 0.0) >= self.cfg.sensitive_threshold
            or any(w in name for w in SENSITIVE_WORDS)
            or stage_sens
        )

    def _is_oauth_gate(self, el: dict | None, snap: dict) -> bool:
        if not self.cfg.voice_prompt or not el:
            return False
        name = el.get("name", "").lower()
        return any(w in name for w in OAUTH_WORDS)

    # -- the retry ladder ---------------------------------------------------
    async def _decide(self, task: str, snap: dict, img_b64: str | None = None, healing_hint: str = "") -> tuple[dict, str]:
        hint = healing_hint or ""
        images = [img_b64] if img_b64 else None
        stage_ctx = self.playbook.prompt_context() if self.playbook else ""
        for attempt in range(1, self.cfg.max_attempts + 1):
            self.tel.stats.attempt = attempt
            res = await self.clef.decide(
                _compact(snap, hint, self._notes_text(), current_stage=stage_ctx),
                _questions(task, snap, stage_context=stage_ctx),
                images_b64=images,
            )
            conf, done, d = _gate(res)
            if done:
                self._drop_oneshot_notes()
                return {"done": True, "confidence": conf}, "clef"
            if conf >= self.cfg.threshold:
                self.tel.emit("gate", f"attempt {attempt} PASS conf={conf:.2f} -> {d}")
                self._drop_oneshot_notes()
                return d, f"clef#{attempt}"
            self.tel.emit("gate", f"attempt {attempt} low conf={conf:.2f} (<{self.cfg.threshold}) -> {d}")
            if attempt < self.cfg.max_attempts:
                hint = await self._rewrite(task, snap, d, hint, stage_ctx=stage_ctx)
        out = await self._llm_decide(task, snap, healing_hint=healing_hint, stage_ctx=stage_ctx)
        self._drop_oneshot_notes()
        return out, "llm"

    async def _rewrite(self, task: str, snap: dict, guess: dict, prev: str, stage_ctx: str = "") -> str:
        self.tel.set_state("rewriting", self.cfg.rewriter_model)
        els = "\n".join(
            f'{e["id"]}: {e["role"]} "{e["name"]}" value="{e["value"]}" empty={e["empty"]}'
            for e in snap["elements"]
        )
        stage_line = f"Current playbook stage: {stage_ctx}\n" if stage_ctx else ""
        prompt = (
            f"Task: {task}\n{stage_line}Page: {snap['title']} ({snap['url']})\nElements:\n{els}\n"
            f"A classifier was unsure; its guess: {guess}. Previous hint: {prev or 'none'}\n"
            f"User notes: {self._notes_text() or 'none'}\n"
            "In <=2 short sentences, state the single best next step and why (which empty field is next, "
            "or whether it is time to submit). Be concrete and reference element ids."
        )
        return (await self.llm.complete(self.cfg.rewriter_model, [{"role": "user", "content": prompt}], max_tokens=120)).strip()

    async def _llm_decide(self, task: str, snap: dict, healing_hint: str = "", stage_ctx: str = "") -> dict:
        self.tel.emit("escalate", f"LLM decides ({self.cfg.escalation_model})")
        els = "\n".join(f'{e["id"]}: {e["role"]} "{e["name"]}" value="{e["value"]}"' for e in snap["elements"])
        healing_block = f"\nFailure / Healing Note: {healing_hint}\n" if healing_hint else ""
        stage_line = f"Active stage: {stage_ctx}\n" if stage_ctx else ""
        prompt = (
            f"Task: {task}\n{stage_line}Data keys: {list(self.data)}\nUser notes: {self._notes_text() or 'none'}\n"
            f"Page: {snap['title']}\nText: {snap['text'][:400]}\n"
            f"{healing_block}"
            f"Elements:\n{els}\n"
            "Reply ONLY a single JSON object (no markdown, no other text): "
            '{"done": false, "action": "click|fill|press|scroll|wait", "target": <id or null>, "sensitive": false}'
        )
        out = await self.llm.complete(self.cfg.escalation_model, [{"role": "user", "content": prompt}], max_tokens=200)
        try:
            j = _json_from(out)
        except json.JSONDecodeError:
            j = {}
        if j.get("done"):
            return {"done": True}
        raw_t = j.get("target")
        target = None if raw_t in (None, "None", "null", "") else str(raw_t)
        return {
            "target": target,
            "action": j.get("action", "wait"),
            "done_p": 0.0,
            "sensitive_p": 1.0 if j.get("sensitive") else 0.0,
        }

    # -- acting -------------------------------------------------------------
    async def _act(self, d: dict, task: str, snap: dict) -> bool:
        self.tel.set_state("acting")
        action, target = d.get("action"), d.get("target")
        try:
            if action in ("goto", "navigate", "page.goto") or (
                isinstance(target, str) and target.startswith(("http://", "https://", "file://"))
            ):
                if hasattr(self.br, "goto") and isinstance(target, str) and target:
                    self.tel.emit("act", f"goto {target}")
                    await self.br.goto(target)
                    return True
                return False
            if action == "scroll":
                await self.br.scroll()
                return True
            if action == "press":
                el = self._resolve_el(snap, target)
                el_id = int(el["id"]) if el else None
                key = "Enter"
                self.tel.emit("act", f"press {key}" + (f' on #{el_id}' if el_id else ""))
                if hasattr(self.br, "press"):
                    try:
                        await self.br.press(key, el_id)
                    except TypeError:
                        await self.br.press(key)
                    return True
                return False
            if action == "wait" or target in (None, "None", "null", "", "0", "-1"):
                await asyncio.sleep(1)
                return action == "wait" or target in ("0", "-1")
            el = self._resolve_el(snap, target)
            if action == "fill":
                if el is None:
                    self.tel.emit("warn", f"fill target not found: {target}")
                    return False
                return await self._fill(task, el)
            if el and el.get("tag") in ("input", "textarea", "select") and el.get("type") not in ("checkbox", "radio", "submit", "button"):
                # model said "click" on a textbox/select: treat as fill, the useful interpretation
                return await self._fill(task, el)
            el_id = int(el["id"]) if el else (int(target) if str(target).isdigit() and int(target) > 0 else None)
            if el_id is None:
                self.tel.emit("warn", f"invalid click target: {target}")
                return False
            self.tel.emit("act", f'click #{el_id} "{el["name"] if el else "?"}"')
            await self.br.click(el_id)
            return True
        except Exception as e:  # noqa: BLE001
            self.tel.emit("error", f"act failed: {type(e).__name__}: {str(e)[:120]}")
            return False

    async def _fill(self, task: str, el: dict) -> bool:
        text = await self._text_for(task, el)
        if text is None:
            return False
        # Never log the value, only which field was filled.
        self.tel.emit("act", f'fill #{el["id"]} "{el["name"]}"')
        await self.br.fill(int(el["id"]), text)
        return True

    async def _text_for(self, task: str, el: dict) -> str | None:
        """Values never reach a model: it only ever sees field names and data *keys*."""
        name = (el["name"] + " " + el.get("type", "")).lower()
        # Check active Playbook stage value_key hint first if available in self.data
        if self.playbook:
            cur = self.playbook.ensure_active()
            if cur and cur.value_key and cur.value_key in self.data and cur.value_key.lower() in name:
                return self.data[cur.value_key]
        for k, v in self.data.items():           # deterministic match first (free + instant)
            if k.lower() in name:
                return v
        key = _slug(el["name"])
        if key in self.data:
            return self.data[key]
        if "search" in name or el.get("type") == "search":
            m = re.search(r"['\"]([^'\"]+)['\"]", task)
            if m:
                return m.group(1)
        secret = el.get("type") == "password" or "password" in name or "secret" in name or "token" in name
        r = await self._ask("value", f'Value needed for "{el["name"]}"', secret=secret, name=key)
        if not r["ok"] or r.get("value") is None:
            return None
        self.data[key] = r["value"]
        if r.get("save", True):                   # saved to secrets.env only after the task succeeds
            self.new_secrets[key] = r["value"]
        return r["value"]

    def _persist_secrets(self) -> None:
        if not self.new_secrets:
            return
        save_secrets(self.new_secrets)
        names = sorted(self.new_secrets)
        self.new_secrets.clear()
        self.tel.emit("secrets", f"saved to {SECRETS_PATH}: {', '.join(names)}")
        if self.on_saved:
            self.on_saved(names)

    def _log_example(self, task, snap, decision, via, ok) -> None:
        """Training data for later RL fine-tuning of the decision model."""
        try:
            state = _compact(snap)
            state.pop("images", None)
            state.pop("image", None)
            if self.train_log.exists() and self.train_log.stat().st_size > MAX_LOG_BYTES:
                self.train_log.replace(self.train_log.with_suffix(".jsonl.1"))
            with self.train_log.open("a") as f:
                f.write(
                    json.dumps({
                        "t": time.time(),
                        "task": task,
                        "state": state,
                        "decision": decision,
                        "via": via,
                        "reward": 1 if ok else 0,
                    }) + "\n"
                )
        except OSError:
            pass
