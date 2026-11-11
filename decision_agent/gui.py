"""Tkinter floating HUD for Based Clef: always-on-top window with telemetry, live Playbook stage
checklist, event stream, human-in-the-loop controls, active element highlight, and settings."""
from __future__ import annotations

import asyncio
from collections import deque
import subprocess
import threading
import tkinter as tk
from tkinter import ttk

from .agent import redact_secret
from .overlay import Highlight
from .runtime import Runtime, stats_line

SOUND = "/usr/share/sounds/freedesktop/stereo/message.oga"
LABELS = {
    "approve": ("Allow", "Deny"),
    "preview": ("Execute Blueprint", "Cancel"),
    "value": ("Submit", "Skip"),
    "stuck": ("Retry with hint", "Stop"),
}


def _alert(prompt: str) -> None:
    """Soft sound + low-urgency notification; never blocks and never raises."""
    for cmd in (["paplay", "--volume=32000", SOUND], ["notify-send", "-u", "low", "based-clef", prompt]):
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass


class DecisionTk:
    def __init__(self, rt: Runtime, task: str = "", data: dict | None = None, url: str | None = None) -> None:
        self.rt, self.data, self.url = rt, data or {}, url
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.queue: deque[tuple[str, str, dict]] = deque()
        self.pending: tuple[str, str, bool, str | None, asyncio.Future] | None = None
        self.shown = None
        self.saved_names: list[str] | None = None
        self.saved_names_shown: list[str] | None = None

        self.root = r = tk.Tk()
        r.title("based-clef // HUD")
        r.geometry("820x580+40+40")
        r.attributes("-topmost", True)
        r.configure(bg="#0a0d12")

        self.highlight = Highlight(r)

        # 1. Top stats bar
        self.stats = tk.Label(
            r, text="", anchor="w", justify="left", font=("monospace", 9),
            bg="#101520", fg="#38bdf8", padx=8, pady=5, wraplength=800,
        )
        self.stats.pack(fill="x")

        # 2. Main split panes: Blueprint & Execution History + Event Stream
        panes = ttk.PanedWindow(r, orient="vertical")
        panes.pack(fill="both", expand=True, padx=4, pady=4)

        traj_frame = ttk.Frame(panes)
        panes.add(traj_frame, weight=1)

        t_header = tk.Frame(traj_frame, bg="#111827")
        t_header.pack(fill="x")
        self.t_label = tk.Label(
            t_header, text="Action Sequence Blueprint & Execution History", bg="#111827", fg="#f1f5f9",
            font=("sans-serif", 9, "bold"), padx=6, pady=4, anchor="w",
        )
        self.t_label.pack(side="left")

        cols = ("step", "action", "target", "status", "detail")
        self.tree = ttk.Treeview(traj_frame, columns=cols, show="headings", height=6)
        self.tree.heading("step", text="#")
        self.tree.column("step", width=44, stretch=False)
        self.tree.heading("action", text="Kind")
        self.tree.column("action", width=84, stretch=False)
        self.tree.heading("target", text="Stage / Target Element")
        self.tree.column("target", width=260, stretch=False)
        self.tree.heading("status", text="Status")
        self.tree.column("status", width=96, stretch=False)
        self.tree.heading("detail", text="Detail / Gate")
        self.tree.column("detail", width=300, stretch=True)

        t_scroll = ttk.Scrollbar(traj_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=t_scroll.set)
        t_scroll.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)

        self.tree.tag_configure("planned", foreground="#64748b")
        self.tree.tag_configure("active_stage", foreground="#38bdf8", font=("sans-serif", 9, "bold"))
        self.tree.tag_configure("executing", foreground="#ff33cc", font=("sans-serif", 9, "bold"))
        self.tree.tag_configure("success", foreground="#34d399")
        self.tree.tag_configure("warning", foreground="#f59e0b")
        self.tree.tag_configure("error", foreground="#f87171")
        self.tree.tag_configure("initial", foreground="#38bdf8")

        # Event stream log
        log_frame = ttk.Frame(panes)
        panes.add(log_frame, weight=1)

        self.text = tk.Text(log_frame, bg="#080b10", fg="#cbd5e1", font=("monospace", 9), wrap="word", height=6)
        l_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=l_scroll.set)
        l_scroll.pack(side="right", fill="y")
        self.text.pack(fill="both", expand=True)

        for tag, color in (
            ("stream", "#38bdf8"), ("error", "#f87171"), ("gate", "#f59e0b"), ("ask", "#fbbf24"),
            ("note", "#94a3b8"), ("secrets", "#34d399"), ("plan", "#38bdf8"), ("step", "#ff33cc"),
            ("stage", "#34d399"),
        ):
            self.text.tag_config(tag, foreground=color)

        # 3. Request banner (shown when the agent asks for approval/input)
        self.banner = tk.Frame(r, bg="#281c09")
        self.b_label = tk.Label(self.banner, text="", bg="#281c09", fg="#fde68a", anchor="w", wraplength=790)
        self.b_label.pack(fill="x", padx=6, pady=(4, 0))
        row = tk.Frame(self.banner, bg="#281c09")
        row.pack(fill="x", padx=6, pady=4)
        self.b_entry = ttk.Entry(row)
        self.b_entry.pack(side="left", fill="x", expand=True)
        self.b_entry.bind("<Return>", lambda _e: self._answer(True))
        self.b_save = tk.BooleanVar(value=True)
        self.b_savechk = ttk.Checkbutton(row, text="save", variable=self.b_save)
        self.b_ok = ttk.Button(row, command=lambda: self._answer(True))
        self.b_no = ttk.Button(row, command=lambda: self._answer(False))
        self.b_ok.pack(side="left")
        self.b_no.pack(side="left")

        # 4. Saved-secrets notice with Redact
        self.notice = tk.Frame(r, bg="#082819")
        self.n_label = tk.Label(self.notice, text="", bg="#082819", fg="#a7f3d0", anchor="w")
        self.n_label.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(self.notice, text="Redact", command=self._redact).pack(side="left")
        ttk.Button(self.notice, text="OK", command=self.notice.pack_forget).pack(side="left")

        # 5. Collapsible Settings Panel
        self.settings_frame = tk.Frame(r, bg="#101520", padx=6, pady=6, relief="ridge", bd=1)
        self._build_settings_panel(self.settings_frame)

        # 6. Hint row
        hint_bar = ttk.Frame(r)
        hint_bar.pack(fill="x", padx=4, pady=1)
        self.hold_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            hint_bar, text="Hold", variable=self.hold_var, style="Toolbutton",
            command=lambda: self.loop.call_soon_threadsafe(self.rt.agent.hold, self.hold_var.get()),
        ).pack(side="left")
        self.hint = ttk.Entry(hint_bar)
        self.hint.pack(side="left", fill="x", expand=True)
        self.hint.bind("<Return>", lambda _e: self.send_hint())
        self.remember = tk.BooleanVar(value=True)
        ttk.Checkbutton(hint_bar, text="remember", variable=self.remember).pack(side="left")
        ttk.Button(hint_bar, text="Send hint", command=self.send_hint).pack(side="left")

        # 7. Bottom action bar (Intent + Optional URL + Mode + Driver)
        bar = ttk.Frame(r)
        bar.pack(fill="x", padx=4, pady=3)
        self.entry = ttk.Entry(bar)
        self.entry.insert(0, task)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda _e: self.start())

        self.url_entry = ttk.Entry(bar, width=22)
        if url:
            self.url_entry.insert(0, url)
        self.url_entry.pack(side="left", padx=2)

        self.mode_var = tk.StringVar(value=rt.cfg.mode)
        ttk.Combobox(bar, textvariable=self.mode_var, values=["autopilot", "preview", "step"], width=9, state="readonly").pack(side="left", padx=1)

        self.driver_var = tk.StringVar(value=rt.cfg.driver)
        ttk.Combobox(bar, textvariable=self.driver_var, values=["browser", "desktop"], width=8, state="readonly").pack(side="left", padx=1)
        ttk.Button(bar, text="Engage", command=self.start).pack(side="left")
        ttk.Button(bar, text="Stop", command=lambda: self.loop.call_soon_threadsafe(self.rt.agent.stop)).pack(side="left")
        self.pin = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="pin", variable=self.pin, command=lambda: r.attributes("-topmost", self.pin.get())).pack(side="left")
        self.settings_shown = False
        self.btn_settings = ttk.Button(bar, text="⚙ Settings", command=self.toggle_settings)
        self.btn_settings.pack(side="left")

        # Wire Telemetry
        rt.tel.subscribe(self._on_telemetry_event)
        rt.agent.ask = self._ask_from_agent
        rt.agent.on_saved = self._on_saved
        r.after(100, self.tick)
        r.protocol("WM_DELETE_WINDOW", self.close)
        if task:
            self.start()

    def _build_settings_panel(self, parent: tk.Frame) -> None:
        """Construct the live tweakable settings controls."""
        r1 = tk.Frame(parent, bg="#101520")
        r1.pack(fill="x", pady=2)

        tk.Label(r1, text="Inter-Action Delay:", bg="#101520", fg="#cbd5e1", font=("sans-serif", 8)).pack(side="left")
        self.step_delay_var = tk.DoubleVar(value=self.rt.cfg.step_delay)
        self.step_delay_lbl = tk.Label(r1, text=f"{self.step_delay_var.get():.1f}s", bg="#101520", fg="#34d399", width=5)
        scale_delay = ttk.Scale(r1, from_=0.0, to=5.0, variable=self.step_delay_var, command=self._update_step_delay)
        scale_delay.pack(side="left", fill="x", expand=True, padx=4)
        self.step_delay_lbl.pack(side="left")

        tk.Label(r1, text="Highlight Decay:", bg="#101520", fg="#cbd5e1", font=("sans-serif", 8), padx=8).pack(side="left")
        self.decay_var = tk.DoubleVar(value=self.rt.cfg.highlight_decay)
        self.decay_lbl = tk.Label(r1, text=f"{self.decay_var.get():.1f}s", bg="#101520", fg="#34d399", width=5)
        scale_decay = ttk.Scale(r1, from_=0.0, to=5.0, variable=self.decay_var, command=self._update_decay)
        scale_decay.pack(side="left", fill="x", expand=True, padx=4)
        self.decay_lbl.pack(side="left")

        r2 = tk.Frame(parent, bg="#101520")
        r2.pack(fill="x", pady=2)

        self.hl_enabled_var = tk.BooleanVar(value=self.rt.cfg.highlight)
        ttk.Checkbutton(r2, text="Active Element Outline", variable=self.hl_enabled_var, command=self._update_highlight_enabled).pack(side="left", padx=4)

        self.plan_first_var = tk.BooleanVar(value=self.rt.cfg.plan_first)
        ttk.Checkbutton(r2, text="Blueprint Pre-Planning", variable=self.plan_first_var, command=self._update_plan_first).pack(side="left", padx=8)

        tk.Label(r2, text="Outline Thickness:", bg="#101520", fg="#cbd5e1", font=("sans-serif", 8)).pack(side="left", padx=(8, 2))
        self.thick_var = tk.IntVar(value=self.rt.cfg.highlight_thickness)
        ttk.Spinbox(r2, from_=1, to=10, width=3, textvariable=self.thick_var, command=self._update_thickness).pack(side="left")

        tk.Label(r2, text="Color:", bg="#101520", fg="#cbd5e1", font=("sans-serif", 8)).pack(side="left", padx=(8, 2))
        self.color_entry = ttk.Entry(r2, width=8)
        self.color_entry.insert(0, self.rt.cfg.highlight_color)
        self.color_entry.pack(side="left")
        self.color_entry.bind("<FocusOut>", lambda _e: self._update_color())
        self.color_entry.bind("<Return>", lambda _e: self._update_color())

    def toggle_settings(self) -> None:
        if self.settings_shown:
            self.settings_frame.pack_forget()
            self.settings_shown = False
            self.btn_settings.config(text="⚙ Settings")
        else:
            self.settings_frame.pack(fill="x", padx=4, pady=2, before=self.hint.master)
            self.settings_shown = True
            self.btn_settings.config(text="▲ Hide Settings")

    def _update_step_delay(self, val: str) -> None:
        v = round(float(val), 2)
        self.rt.cfg.step_delay = v
        self.step_delay_lbl.config(text=f"{v:.1f}s")

    def _update_decay(self, val: str) -> None:
        v = round(float(val), 2)
        self.rt.cfg.highlight_decay = v
        self.decay_lbl.config(text=f"{v:.1f}s")

    def _update_highlight_enabled(self) -> None:
        en = self.hl_enabled_var.get()
        self.rt.cfg.highlight = en
        if hasattr(self.rt.driver, "highlight"):
            self.rt.driver.highlight = en

    def _update_plan_first(self) -> None:
        self.rt.cfg.plan_first = self.plan_first_var.get()

    def _update_thickness(self) -> None:
        try:
            self.rt.cfg.highlight_thickness = int(self.thick_var.get())
        except ValueError:
            pass

    def _update_color(self) -> None:
        c = self.color_entry.get().strip()
        if c:
            self.rt.cfg.highlight_color = c

    # -- telemetry handling -----------------------------------------------------------
    def _on_telemetry_event(self, ev: dict) -> None:
        if ev.get("kind") == "viewport":
            return
        raw_tail = self.rt.tel.tail(1)
        tail_msg = raw_tail[0] if raw_tail else ""
        self.queue.append((ev.get("kind", "stream"), tail_msg, ev))

    # -- agent -> human ---------------------------------------------------------------
    async def _ask_from_agent(self, kind: str, prompt: str, secret: bool, name: str | None) -> dict:
        fut = self.loop.create_future()
        self.pending = (kind, prompt, secret, name, fut)
        try:
            return await asyncio.wait_for(fut, self.rt.cfg.ask_timeout)
        except asyncio.TimeoutError:
            self.queue.append(("ask", "no answer, auto-denied", {}))
            return {"ok": False, "value": None, "save": False}
        finally:
            self.pending = None

    def _on_saved(self, names: list[str]) -> None:
        self.saved_names = names
        self.saved_names_shown = None

    # -- human -> agent ---------------------------------------------------------------
    def _answer(self, ok: bool) -> None:
        if not self.pending:
            return
        kind, _p, _s, _n, fut = self.pending
        value = self.b_entry.get() or None
        res = {
            "ok": ok and (kind != "value" or value is not None),
            "value": value if ok else None,
            "save": self.b_save.get(),
        }
        self.loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(res))
        self.banner.pack_forget()
        self.shown = None

    def send_hint(self) -> None:
        txt = self.hint.get().strip()
        if txt:
            self.loop.call_soon_threadsafe(self.rt.agent.add_note, txt, self.remember.get())
            self.hint.delete(0, "end")

    def _redact(self) -> None:
        for n in self.saved_names or []:
            redact_secret(n)
        self.n_label.config(text="redacted")

    def start(self) -> None:
        if self.rt.tel.stats.state not in ("idle", "done", "error"):
            return
        task = self.entry.get().strip()
        url_val = self.url_entry.get().strip() or self.url
        if task:
            selected_driver = self.driver_var.get()
            self.rt.cfg.mode = self.mode_var.get()
            for item in self.tree.get_children():
                self.tree.delete(item)

            async def _run() -> None:
                await self.rt.set_driver(selected_driver)
                await self.rt.run_task(task, self.data, url_val)

            asyncio.run_coroutine_threadsafe(_run(), self.loop)

    def _sync_plan_rows(self, stages: list[dict]) -> None:
        tree = getattr(self, "tree", None)
        if tree is None:
            return
        for st in stages:
            sid = st.get("id")
            iid = f"plan_{sid}"
            status = st.get("status", "pending")
            label_status = "Verified" if status == "completed" else ("Active" if status == "active" else "Queued")
            tag = "success" if status == "completed" else ("active_stage" if status == "active" else "planned")
            detail = "SENSITIVE GATE" if st.get("sensitive") else (st.get("via") or "")
            vals = (f"S{sid}", str(st.get("kind", "auto")).upper(), st.get("intent", ""), label_status, detail)
            if tree.exists(iid):
                tree.item(iid, values=vals, tags=(tag,))
            else:
                tree.insert("", "end", iid=iid, values=vals, tags=(tag,))

    # -- UI refresh --------------------------------------------------------------------
    def tick(self) -> None:
        self.stats.config(text=stats_line(self.rt.tel))

        p = self.pending
        if p and self.shown is not p[4]:
            self.shown = p[4]
            kind, prompt, secret, _name, _f = p
            self.b_label.config(text=prompt)
            self.b_entry.delete(0, "end")
            self.b_entry.config(show="•" if secret else "")
            ok_lbl, no_lbl = LABELS.get(kind, ("Approve", "Deny"))
            self.b_ok.config(text=ok_lbl)
            self.b_no.config(text=no_lbl)
            if kind == "value":
                self.b_savechk.pack(side="left", before=self.b_ok)
            else:
                self.b_savechk.pack_forget()
            self.banner.pack(fill="x", before=self.hint.master)
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.b_entry.focus_force()
            _alert(prompt)
        elif not p and self.shown is not None:
            self.banner.pack_forget()
            self.shown = None

        if self.saved_names and self.saved_names_shown is not self.saved_names:
            self.saved_names_shown = self.saved_names
            self.n_label.config(text="saved secrets: " + ", ".join(self.saved_names))
            self.notice.pack(fill="x", before=self.hint.master)
            _alert("secrets saved")

        q = []
        while self.queue:
            q.append(self.queue.popleft())
        tree = getattr(self, "tree", None)
        for kind, line, ev in q:
            if line:
                self.text.insert(
                    "end",
                    line + "\n",
                    kind if kind in ("stream", "error", "gate", "ask", "note", "secrets", "plan", "step", "stage") else (),
                )

            if tree is not None:
                if kind == "initial":
                    tree.insert(
                        "", "end",
                        values=("0", "INIT", ev.get("title", ""), "Loaded", f"{ev.get('elements', 0)} elements"),
                        tags=("initial",),
                    )
                elif kind == "plan":
                    stages = ev.get("stages")
                    if stages:
                        self._sync_plan_rows(stages)
                    else:
                        for idx, st in enumerate(ev.get("steps", []), 1):
                            tree.insert("", "end", iid=f"plan_{idx}", values=(f"S{idx}", "PLAN", st, "Queued", ""), tags=("planned",))
                elif kind == "stage" and ev.get("stages"):
                    self._sync_plan_rows(ev["stages"])
                elif kind == "step":
                    n = ev.get("n", "")
                    action = ev.get("action", "")
                    target = str(ev.get("target", ""))
                    via = ev.get("via", "")
                    tree.insert("", "end", iid=f"step_{n}", values=(n, action.upper(), target, "Executing...", f"via {via}"), tags=("executing",))
                    tree.see(f"step_{n}")
                elif kind == "result":
                    n = ev.get("n", "")
                    ok = ev.get("ok", False)
                    iid = f"step_{n}"
                    if tree.exists(iid):
                        cur = list(tree.item(iid, "values"))
                        cur[3] = "Completed" if ok else "No Effect"
                        tree.item(iid, values=cur, tags=("success" if ok else "warning",))
                elif kind == "highlight":
                    bbox = ev.get("bbox")
                    if bbox and self.rt.cfg.highlight and hasattr(self, "highlight"):
                        self.highlight.show(
                            tuple(bbox),
                            self.rt.cfg.highlight_color,
                            self.rt.cfg.highlight_thickness,
                            self.rt.cfg.highlight_decay,
                        )

        n_lines = int(self.text.index("end-1c").split(".")[0])
        if n_lines > self.rt.cfg.log_lines:
            self.text.delete("1.0", f"{n_lines - self.rt.cfg.log_lines + 1}.0")
        if q:
            self.text.see("end")

        self.root.after(100, self.tick)

    def close(self) -> None:
        self.highlight.hide()
        try:
            asyncio.run_coroutine_threadsafe(self.rt.shutdown(), self.loop).result(5)
        except Exception:
            pass
        self.root.destroy()

    def mainloop(self) -> None:
        self.root.mainloop()
