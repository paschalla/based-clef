"""CLI entry for Based Clef (`bclef`, `based-clef`, and legacy alias `da`)."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import shutil
import sys
import webbrowser
from pathlib import Path

from .core import APP_NAME, LOG_DIR, Config, _resolve_env_path
from .intent import Playbook, compile_intent_fast

DEMO = Path(__file__).parent / "demo" / "login.html"
DEMO_PORTAL = Path(__file__).parent / "demo" / "portal.html"


def _kv(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i in items or []:
        if "=" not in i:
            raise SystemExit(f"Invalid -d '{i}': expected key=value")
        k, v = i.split("=", 1)
        out[k] = v
    return out


def _ask_cli_sync(kind: str, prompt: str, secret: bool = False, name: str | None = None) -> dict:
    try:
        if kind == "approve":
            ans = input(f"{prompt} [y/N]: ").strip().lower()
            return {"ok": ans in ("y", "yes"), "value": None, "save": False}
        if kind == "preview":
            ans = input(f"{prompt} [Y/n or type note]: ").strip()
            low = ans.lower()
            if low in ("n", "no", "cancel", "stop", "deny"):
                return {"ok": False, "value": None, "save": False}
            note = None if low in ("", "y", "yes") else ans
            return {"ok": True, "value": note, "save": False}
        if kind == "value":
            val = getpass.getpass(f"{prompt}: ") if secret else input(f"{prompt}: ")
            if val:
                return {"ok": True, "value": val, "save": True}
            return {"ok": False, "value": None, "save": False}
        if kind == "stuck":
            ans = input(f"{prompt} [hint / Enter to stop]: ").strip()
            if ans and ans.lower() not in ("n", "no", "stop", "deny"):
                return {"ok": True, "value": ans, "save": False}
            return {"ok": False, "value": None, "save": False}
        ans = input(f"{prompt}: ").strip()
        return {"ok": bool(ans), "value": ans or None, "save": False}
    except (EOFError, KeyboardInterrupt):
        return {"ok": False, "value": None, "save": False}


async def _plain(rt, task: str, data: dict[str, str], url: str | None, playbook: Playbook | None = None) -> bool:
    """Rich Terminal HUD: Telemetry + Probability Gates + Live Action Sequence Blueprint + Event Stream."""
    from rich.console import Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    from .runtime import stats_line

    def view():
        term_h = shutil.get_terminal_size().lines
        s = rt.tel.stats

        # Probability gates sub-line
        gates = []
        for label, val, color in (
            ("P1[tgt]", s.p_target, "bright_magenta"),
            ("P2[act]", s.p_action, "dark_orange"),
            ("P3[done]", s.p_done, "green"),
            ("P4[sens]", s.p_sensitive, "yellow"),
        ):
            v_str = "-" if val is None else f"{val:.2f}"
            gates.append(f"[{color}]{label}[/{color}]={v_str}")
        hdr_text = Text.from_markup(stats_line(rt.tel) + "\n" + "  ".join(gates))
        panels = [Panel(hdr_text, title="based-clef // system 1 telemetry", border_style="cyan")]

        pb = rt.agent.playbook
        stage_rows = len(pb.stages) if pb and pb.stages else 0
        if pb and pb.stages:
            tbl = Table.grid(padding=(0, 2))
            tbl.add_column(width=4)
            tbl.add_column(width=8, style="dim")
            tbl.add_column(ratio=1)
            for st in pb.stages[:8]:
                if st.status == "completed":
                    mark, style = "[green]✓[/green]", "green"
                elif st.status == "active":
                    mark, style = "[bright_cyan]▶[/bright_cyan]", "bold bright_white"
                else:
                    mark, style = f"[dim]{st.id:02d}[/dim]", "dim"
                gate_tag = " [yellow][GATE][/yellow]" if st.sensitive else ""
                tbl.add_row(mark, st.kind.upper(), f"[{style}]{st.intent}[/{style}]{gate_tag}")
            panels.append(Panel(tbl, title=f"action sequence blueprint ({s.stage_index}/{s.stage_total})", border_style="bright_black"))

        log_budget = max(6, term_h - 9 - stage_rows)
        body = Text("\n".join(rt.tel.tail(min(log_budget, rt.cfg.log_lines))), no_wrap=False, overflow="fold")
        panels.append(Panel(body, title=f"telemetry stream (last {rt.cfg.log_lines})", border_style="blue"))
        return Group(*panels)

    live = Live(view(), refresh_per_second=10, screen=False, get_renderable=view)

    async def _ask(kind: str, prompt: str, secret: bool = False, name: str | None = None) -> dict:
        def _call() -> dict:
            paused = live.is_started
            if paused:
                live.stop()
            try:
                return _ask_cli_sync(kind, prompt, secret, name)
            finally:
                if paused:
                    live.start()

        return await asyncio.to_thread(_call)

    rt.agent.ask = _ask
    with live:
        ok = await rt.run_task(task, data, url, playbook=playbook)
    await rt.shutdown()
    return ok


def _print_overview() -> int:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    cfg = Config.from_env()
    cred_state = "[green]Configured[/green]" if cfg.has_credentials() else "[yellow]Not set (run `bclef init` or use `--offline`)[/yellow]"

    tbl = Table(show_header=True, header_style="bold cyan", box=None, pad_edge=False)
    tbl.add_column("Command", style="bold white", width=34)
    tbl.add_column("Description", style="white")
    tbl.add_row("bclef studio", "Launch Based Clef Studio web UI (live viewport + blueprint editor)")
    tbl.add_row('bclef run "<intent>"', "Compile natural intent or `->` shorthand & execute")
    tbl.add_row('bclef plan "<intent>"', "Preview compiled Action Sequence Blueprint without acting")
    tbl.add_row("bclef playbook <file.json>", "Execute a saved, parameterized Playbook")
    tbl.add_row("bclef demo [--offline]", "Run bundled demo (zero API keys needed with --offline)")
    tbl.add_row("bclef gui", "Launch floating X11 always-on-top HUD")
    tbl.add_row("bclef init / bclef doctor", "Configure credentials (0600) & verify system readiness")
    tbl.add_row("bclef mcp / bclef ipc", "Start Model Context Protocol server or Unix socket daemon")

    Console().print(
        Panel(
            tbl,
            title="[bold bright_magenta]// based-clef //[/bold bright_magenta] System 1 Computer-Use Engine",
            subtitle=f"Workers AI: {cred_state} • Model: {cfg.decision_model}",
            border_style="bright_black",
            padding=(1, 2),
        )
    )
    return 0


def _cmd_init() -> int:
    from rich.console import Console

    con = Console()
    target = Path("~/.config/based-clef/env").expanduser()
    con.print("[bold cyan]Based Clef — Credential Setup[/bold cyan]")
    con.print(f"Credentials will be written with `0600` permissions to [bold]{target}[/bold]\n")
    try:
        acc = input("Cloudflare Account ID (32-char hex): ").strip()
        tok = getpass.getpass("Cloudflare Workers AI API Token (hidden): ").strip()
    except (EOFError, KeyboardInterrupt):
        con.print("\n[yellow]Aborted.[/yellow]")
        return 1
    if not acc or not tok:
        con.print("[red]Both CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN are required.[/red]")
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(
            f"CLOUDFLARE_ACCOUNT_ID={acc}\n"
            f"CLOUDFLARE_API_TOKEN={tok}\n"
            "BASED_CLEF_MODEL=@cf/cloudflare/clef-flash\n"
        )
    os.chmod(target, 0o600)
    con.print(f"[green]✓ Saved {target} (mode 0600). Run `bclef doctor` or `bclef demo` to verify![/green]")
    return 0


def _cmd_doctor(as_json: bool = False) -> int:
    from .browser import find_browser_executable

    cfg = Config.from_env()
    env_file = _resolve_env_path()
    env_mode_ok = (not env_file.exists()) or ((env_file.stat().st_mode & 0o077) == 0)
    channel, exe = find_browser_executable(cfg.browser_path)
    has_xdotool = shutil.which("xdotool") is not None
    has_tesseract = shutil.which("tesseract") is not None
    try:
        import mcp as _mcp  # noqa: F401
        has_mcp = True
    except ImportError:
        has_mcp = False

    report = {
        "python": sys.version.split()[0],
        "env_file": str(env_file),
        "env_file_exists": env_file.exists(),
        "env_permissions_0600": env_mode_ok,
        "cloudflare_credentials": cfg.has_credentials(),
        "decision_model": cfg.decision_model,
        "browser": channel or exe or "playwright-default",
        "browser_found": bool(channel or exe),
        "desktop_xdotool": has_xdotool,
        "desktop_tesseract_ocr": has_tesseract,
        "mcp_installed": has_mcp,
    }
    if as_json:
        print(json.dumps(report, indent=2))
        return 0

    from rich.console import Console
    from rich.table import Table

    tbl = Table(title="Based Clef — System Readiness (`bclef doctor`)", header_style="bold cyan")
    tbl.add_column("Check")
    tbl.add_column("Status")
    tbl.add_column("Detail")

    def row(name: str, ok: bool, detail: str, optional: bool = False) -> None:
        badge = "[green]OK[/green]" if ok else ("[yellow]OPTIONAL[/yellow]" if optional else "[red]MISSING[/red]")
        tbl.add_row(name, badge, detail)

    row("Python >= 3.11", sys.version_info >= (3, 11), report["python"])
    row("Cloudflare Workers AI Creds", cfg.has_credentials(), f"{env_file} ({'found' if env_file.exists() else 'use `bclef init` or `--offline`'})", optional=True)
    row("Config Permissions (0600)", env_mode_ok, "strict owner-only" if env_mode_ok else f"run: chmod 600 {env_file}")
    row("Browser Engine", bool(channel or exe), str(channel or exe or "run `playwright install chromium`"))
    row("MCP Server SDK", has_mcp, "ready (`bclef mcp`)" if has_mcp else "pip install -e '.[mcp]'", optional=True)
    row("Desktop X11 (`xdotool`)", has_xdotool, "ready" if has_xdotool else "install xdotool for --driver desktop", optional=True)
    row("Desktop OCR (`tesseract`)", has_tesseract, "ready" if has_tesseract else "optional OCR fallback", optional=True)

    Console().print(tbl)
    return 0


def _cmd_plan(ns) -> int:
    data = _kv(ns.data)
    pb = compile_intent_fast(ns.intent, url=ns.url, data=data)
    if ns.save:
        saved = pb.save(ns.save)
        if not ns.json:
            print(f"Saved playbook to {saved}", file=sys.stderr)
    if ns.json:
        print(json.dumps(pb.to_dict(), indent=2))
        return 0

    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    tbl = Table(show_header=True, header_style="bold cyan", box=None)
    tbl.add_column("#", width=4)
    tbl.add_column("Kind", width=10)
    tbl.add_column("Stage Intent")
    tbl.add_column("Target / Key", style="dim")
    tbl.add_column("Gate", width=10)
    for st in pb.stages:
        hint = st.value_key or st.target_hint or "-"
        gate = "[yellow]SENSITIVE[/yellow]" if st.sensitive else "[green]AUTO[/green]"
        tbl.add_row(f"{st.id:02d}", st.kind.upper(), st.intent, hint, gate)

    meta = f"Start URL: [bold]{pb.start_url or '(current / none)'}[/bold]  •  Missing keys: [yellow]{', '.join(pb.missing_keys(data)) or 'none'}[/yellow]"
    Console().print(Panel(tbl, title=f"Action Sequence Blueprint — {pb.goal}", subtitle=meta, border_style="cyan"))
    return 0


def _run_studio(rt, host: str, port: int, open_browser: bool = False, initial_task: str = "", data: dict | None = None, url: str | None = None) -> int:
    from .web import StudioServer

    srv = StudioServer(rt, host=host, port=port)

    async def _serve() -> None:
        await srv.start()
        print(f"Based Clef Studio listening at {srv.base_url} (Ctrl+C to stop)", flush=True)
        if open_browser:
            webbrowser.open(srv.base_url)
        if initial_task:
            asyncio.create_task(rt.run_task(initial_task, data or {}, url))
        try:
            assert srv._server is not None
            async with srv._server:
                await srv._server.serve_forever()
        finally:
            await srv.stop()
            await rt.shutdown()

    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        print("\nBased Clef Studio stopped.")
    return 0


def main(argv: list[str] | None = None) -> int:
    raw_args = sys.argv[1:] if argv is None else argv
    if not raw_args:
        return _print_overview()

    p = argparse.ArgumentParser(
        prog=Path(sys.argv[0]).name if argv is None else "bclef",
        description="Based Clef — System 1 computer-use engine driven by Cloudflare Clef decision models",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="compile intent into an action sequence and execute")
    r.add_argument("task", help="natural language intent or `->` pipeline shorthand")
    r.add_argument("--url", help="optional starting URL (auto-extracted from intent if omitted)")
    r.add_argument("--data", "-d", action="append", default=[], help="key=value (e.g. email=a@b.com)")
    r.add_argument("--ui", choices=["plain", "tk", "web"], default="plain")
    r.add_argument("--headless", action="store_true")
    r.add_argument("--threshold", type=float)
    r.add_argument("--attempts", type=int)
    r.add_argument("--driver", choices=["browser", "desktop"], default=None, help="automation driver (browser|desktop)")
    r.add_argument("--window", default="Google Chrome", help="target window name for desktop driver")
    r.add_argument("--vision", action=argparse.BooleanOptionalAction, default=None, help="enable/disable vision screenshots for Clef")
    r.add_argument("--highlight", action=argparse.BooleanOptionalAction, default=None, help="highlight target elements before acting")
    r.add_argument("--highlight-delay", type=float, default=None, help="delay in seconds for element highlight")
    r.add_argument("--voice-prompt", action=argparse.BooleanOptionalAction, default=None, help="enable audible voice prompts on OAuth/credential checks")
    r.add_argument("--mode", choices=["autopilot", "preview", "step"], default=None, help="execution mode")
    r.add_argument("--preview", action="store_true", help="preview & confirm compiled blueprint before step 1")
    r.add_argument("--step", dest="step_mode", action="store_true", help="co-pilot mode: confirm each action step")
    r.add_argument("--playbook", type=Path, default=None, help="execute a pre-compiled Playbook JSON file")
    r.add_argument("--save-playbook", default=None, help="save verified action sequence to Playbook JSON on completion")
    r.add_argument("--offline", action="store_true", help="use deterministic local sandbox engine (no API token required)")

    pl = sub.add_parser("plan", help="compile user intent into an Action Sequence Blueprint without acting")
    pl.add_argument("intent")
    pl.add_argument("--url", default=None)
    pl.add_argument("--data", "-d", action="append", default=[])
    pl.add_argument("--json", action="store_true", help="emit machine-readable Playbook JSON")
    pl.add_argument("--save", default=None, help="write Playbook JSON to path")

    pb_p = sub.add_parser("playbook", help="run a saved Playbook JSON file")
    pb_p.add_argument("file", type=Path, help="path to .playbook.json")
    pb_p.add_argument("--url", default=None)
    pb_p.add_argument("--data", "-d", action="append", default=[])
    pb_p.add_argument("--headless", action="store_true")
    pb_p.add_argument("--offline", action="store_true")
    pb_p.add_argument("--ui", choices=["plain", "tk", "web"], default="plain")

    st_p = sub.add_parser("studio", help="launch Based Clef Studio local web command center")
    st_p.add_argument("--host", default="127.0.0.1")
    st_p.add_argument("--port", type=int, default=8765)
    st_p.add_argument("--offline", action="store_true", help="start in Sandbox Offline mode")
    st_p.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True, help="run browser headlessly behind Studio viewport")
    st_p.add_argument("--open", action="store_true", help="open Studio URL in system browser")

    sub.add_parser("init", help="interactively configure Cloudflare Workers AI credentials (0600)")

    doc_p = sub.add_parser("doctor", help="check environment, browser, permissions, and optional drivers")
    doc_p.add_argument("--json", action="store_true")

    a = sub.add_parser("ask", help="one-shot Clef decision on stdin/JSON state")
    a.add_argument("question")
    a.add_argument("--options", "-o", nargs="+", help="choice options (omit for yes/no)")
    a.add_argument("--state", default="-", help="text, or - for stdin")
    a.add_argument("--offline", action="store_true")

    t = sub.add_parser("tail", help="show last N events from the log")
    t.add_argument("-n", type=int, default=100)
    t.add_argument("-f", action="store_true")

    ipc_p = sub.add_parser("ipc", help="run Based Clef as an IPC Unix socket daemon")
    ipc_p.add_argument("--socket", type=Path, default=None, help="path to custom unix socket")
    ipc_p.add_argument("--offline", action="store_true")

    gui_p = sub.add_parser("gui", help="launch interactive Tkinter HUD")
    gui_p.add_argument("task", nargs="?", default="", help="optional task to run immediately")
    gui_p.add_argument("--url", default=None)
    gui_p.add_argument("--data", "-d", action="append", default=[], help="key=value (e.g. email=a@b.com)")
    gui_p.add_argument("--driver", choices=["browser", "desktop"], default=None, help="automation driver")
    gui_p.add_argument("--window", default="Google Chrome", help="target window name for desktop driver")
    gui_p.add_argument("--headless", action="store_true")
    gui_p.add_argument("--offline", action="store_true")

    sub.add_parser("mcp", help="run Based Clef as an MCP stdio server")

    dm = sub.add_parser("demo", help="run on the bundled demo page")
    dm.add_argument("--offline", action="store_true", help="force local heuristic sandbox engine")
    dm.add_argument("--headless", action="store_true")
    dm.add_argument("--portal", action="store_true", help="run multi-step API Console demo instead of login demo")
    dm.add_argument("--ui", choices=["plain", "tk", "web"], default="plain")

    ns = p.parse_args(raw_args)

    if ns.cmd == "init":
        return _cmd_init()
    if ns.cmd == "doctor":
        return _cmd_doctor(as_json=ns.json)
    if ns.cmd == "plan":
        return _cmd_plan(ns)
    if ns.cmd == "tail":
        return _tail(ns.n, ns.f)
    if ns.cmd == "ask":
        return asyncio.run(_ask(ns))
    if ns.cmd == "mcp":
        from .mcp_server import run_server
        run_server()
        return 0

    from .runtime import build

    if ns.cmd == "studio":
        cfg = Config.from_env()
        cfg.headless = ns.headless
        if ns.offline or not cfg.has_credentials():
            cfg.offline = True
        cfg.step_delay = min(cfg.step_delay, 0.4)
        rt = build(cfg)
        return _run_studio(rt, host=ns.host, port=ns.port, open_browser=ns.open)

    if ns.cmd == "gui":
        from .gui import DecisionTk
        cfg = Config.from_env()
        cfg.headless = ns.headless
        if ns.offline or not cfg.has_credentials():
            cfg.offline = True
        if ns.driver is not None:
            cfg.driver = ns.driver
        cfg.target_window = ns.window
        rt = build(cfg)
        data = _kv(ns.data)
        DecisionTk(rt, ns.task, data, ns.url).mainloop()
        return 0

    if ns.cmd == "ipc":
        from .ipc import DEFAULT_SOCK, IPCServer
        cfg = Config.from_env()
        cfg.headless = True
        if ns.offline:
            cfg.offline = True
        rt = build(cfg)
        sock_path = ns.socket or DEFAULT_SOCK
        server = IPCServer(rt, socket_path=sock_path)
        print(f"Starting Based Clef IPC socket daemon at {sock_path}...")
        try:
            asyncio.run(server.run_forever())
        except KeyboardInterrupt:
            print("IPC server stopped.")
        return 0

    cfg = Config.from_env()
    loaded_playbook: Playbook | None = None

    if ns.cmd == "playbook":
        loaded_playbook = Playbook.load(ns.file)
        task = loaded_playbook.goal
        url = ns.url or loaded_playbook.start_url
        data = {**loaded_playbook.extracted_data, **_kv(ns.data)}
        cfg.headless = ns.headless
        if ns.offline:
            cfg.offline = True
        ui_mode = ns.ui
    elif ns.cmd == "demo":
        if ns.portal:
            task = "Search 'clef-flash' -> click 'Filter Catalog' -> click 'Generate API Key'"
            url = DEMO_PORTAL.as_uri()
            data = {}
        else:
            task = "Log in with the given email and password"
            url = DEMO.as_uri()
            data = {"email": "andy@example.com", "password": "hunter2"}
        cfg.headless = ns.headless
        if ns.offline or not cfg.has_credentials():
            if not cfg.has_credentials() and not ns.offline:
                print(f"[{APP_NAME}] No Cloudflare credentials configured — running demo in local Sandbox Offline mode.", file=sys.stderr)
            cfg.offline = True
            cfg.step_delay = 0.25
        ui_mode = ns.ui
    else:
        task = ns.task
        url = ns.url
        data = _kv(ns.data)
        ui_mode = ns.ui
        cfg.headless = ns.headless
        if getattr(ns, "offline", False):
            cfg.offline = True
        if getattr(ns, "driver", None) is not None:
            cfg.driver = ns.driver
        if ns.threshold is not None:
            cfg.threshold = ns.threshold
        if ns.attempts is not None:
            cfg.max_attempts = ns.attempts
        if getattr(ns, "vision", None) is not None:
            cfg.vision = ns.vision
        if getattr(ns, "highlight", None) is not None:
            cfg.highlight = ns.highlight
        if getattr(ns, "highlight_delay", None) is not None:
            cfg.highlight_delay = ns.highlight_delay
        if getattr(ns, "voice_prompt", None) is not None:
            cfg.voice_prompt = ns.voice_prompt
        if getattr(ns, "window", None) is not None:
            cfg.target_window = ns.window
        if getattr(ns, "mode", None) is not None:
            cfg.mode = ns.mode
        elif getattr(ns, "preview", False):
            cfg.mode = "preview"
        elif getattr(ns, "step_mode", False):
            cfg.mode = "step"
        if getattr(ns, "save_playbook", None):
            cfg.save_playbook = ns.save_playbook
        if getattr(ns, "playbook", None) is not None:
            loaded_playbook = Playbook.load(ns.playbook)

    rt = build(cfg)

    if ui_mode == "tk":
        from .gui import DecisionTk
        DecisionTk(rt, task, data, url).mainloop()
        return 0
    if ui_mode == "web":
        return _run_studio(rt, host="127.0.0.1", port=8765, open_browser=True, initial_task=task, data=data, url=url)
    if loaded_playbook is not None:
        return 0 if asyncio.run(_plain(rt, task, data, url, playbook=loaded_playbook)) else 1
    return 0 if asyncio.run(_plain(rt, task, data, url)) else 1


def _stdin_has_data(timeout: float = 2.0) -> bool:
    """True if stdin has data (or EOF) ready without blocking forever."""
    import select
    try:
        r, _, _ = select.select([sys.stdin], [], [], timeout)
        return bool(r)
    except Exception:
        return True  # cannot poll: fall back to a normal read


async def _ask(ns) -> int:
    from .core import ClefClient, HeuristicClefClient, Telemetry

    cfg = Config.from_env()
    if getattr(ns, "offline", False):
        cfg.offline = True
    cfg.require()
    if ns.state == "-":
        if sys.stdin.isatty():
            print(
                "bclef ask: no --state given and stdin is a terminal; using empty state "
                '(pipe it in or pass --state "...")',
                file=sys.stderr,
            )
            state = ""
        elif _stdin_has_data():
            state = sys.stdin.read()
        else:
            print(
                "bclef ask: no --state given and no stdin data arrived within 2s; using empty state "
                '(pipe it in or pass --state "...")',
                file=sys.stderr,
            )
            state = ""
    else:
        state = ns.state
    if ns.options:
        criteria = {o: o for o in ns.options}
        if len(criteria) < 2:
            criteria["none"] = "none of these"
        q = {"type": "choice", "instructions": ns.question, "criteria": criteria}
    else:
        q = {"type": "noul", "instructions": ns.question}
    c = HeuristicClefClient(cfg, Telemetry()) if cfg.offline else ClefClient(cfg, Telemetry())
    res = await c.decide(state, {"q": q})
    print(json.dumps(res["answers"]["q"], indent=2))
    await c.http.aclose()
    return 0


def _tail(n: int, follow: bool) -> int:
    import time

    path = LOG_DIR / "events.jsonl"
    if not path.exists():
        print("no log yet", file=sys.stderr)
        return 1
    lines = path.read_text().splitlines()[-n:]
    fmt = lambda e: f'{time.strftime("%H:%M:%S", time.localtime(e["t"]))} {e["kind"]:<8} {e["msg"]}'  # noqa: E731
    for l in lines:
        print(fmt(json.loads(l)))
    if follow:
        with path.open() as f:
            f.seek(0, 2)
            try:
                while True:
                    l = f.readline()
                    if l:
                        print(fmt(json.loads(l)), flush=True)
                    else:
                        time.sleep(0.2)
            except KeyboardInterrupt:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
