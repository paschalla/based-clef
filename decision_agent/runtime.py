"""Wire everything together; shared by Web Studio, Rich CLI, Tk HUD, IPC, and MCP surfaces."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .agent import Agent
from .browser import BrowserDriver
from .core import ClefClient, Config, HeuristicClefClient, HeuristicLLMClient, LLMClient, Telemetry
from .intent import Playbook, compile_intent_fast

if TYPE_CHECKING:
    from .desktop import DesktopDriver


@dataclass
class Runtime:
    cfg: Config
    tel: Telemetry
    agent: Agent
    driver: BrowserDriver | DesktopDriver
    clef: ClefClient | HeuristicClefClient
    llm: LLMClient | HeuristicLLMClient
    _run_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def set_driver(self, driver: str) -> None:
        if not driver or driver == self.cfg.driver:
            return
        await self.driver.close()
        self.cfg.driver = driver
        self.driver = self.agent.br = _make_driver(self.cfg, self.tel)

    def set_offline(self, offline: bool) -> None:
        if offline == self.cfg.offline:
            return
        self.cfg.offline = offline
        if offline:
            self.clef = HeuristicClefClient(self.cfg, self.tel)
            self.llm = HeuristicLLMClient(self.cfg, self.tel)
        else:
            self.clef = ClefClient(self.cfg, self.tel)
            self.llm = LLMClient(self.cfg, self.tel)
        self.agent.clef = self.clef
        self.agent.llm = self.llm

    async def ensure_started(self) -> bool:
        if hasattr(self.driver, "page") and self.driver.page is None:
            await self.driver.start()
            return True
        if hasattr(self.driver, "_sct") and self.driver._sct is None:
            await self.driver.start()
            return True
        return False

    async def compile_blueprint(
        self,
        task: str,
        data: dict[str, str] | None = None,
        url: str | None = None,
        inspect_live: bool = False,
    ) -> Playbook:
        snap = None
        if inspect_live:
            await self.ensure_started()
            target_url = url
            if target_url and hasattr(self.driver, "goto"):
                await self.driver.goto(target_url)
            snap = await self.driver.snapshot()
            img_b64 = None
            if self.cfg.vision:
                try:
                    img_b64 = await self.driver.screenshot_b64(self.cfg.image_quality)
                except Exception:
                    img_b64 = None
            self.tel.update_frame(snap, img_b64)
        pb = compile_intent_fast(task, url=url, data=data, snap=snap, driver=self.cfg.driver)
        self.agent.playbook = pb
        self.agent._sync_playbook_stats()
        self.tel.emit(
            "plan",
            f"{len(pb.stages)} intended stages",
            steps=[s.intent for s in pb.stages],
            stages=[s.to_dict() for s in pb.stages],
            playbook=pb.to_dict(),
            missing_keys=pb.missing_keys(data or {}),
        )
        return pb

    async def run_task(
        self,
        task: str,
        data: dict[str, str],
        url: str | None,
        playbook: Playbook | None = None,
    ) -> bool:
        async with self._run_lock:
            self.agent.stop_flag = False
            started = False
            try:
                started = await self.ensure_started()
                return await self.agent.run(task, data, url, playbook=playbook)
            except Exception as e:  # noqa: BLE001
                self.tel.set_state("error")
                self.tel.emit("error", f"{type(e).__name__}: {str(e)[:200]}")
                return False
            finally:
                if started and self.cfg.headless:
                    await self.driver.close()

    async def shutdown(self) -> None:
        await self.driver.close()
        await self.clef.http.aclose()
        await self.llm.http.aclose()


def wire_highlight(drv, tel: Telemetry) -> None:
    """Route a desktop driver's highlight requests into telemetry so a front-end can draw them."""
    if hasattr(drv, "on_highlight"):
        drv.on_highlight = lambda bbox, name: tel.emit("highlight", name, bbox=list(bbox))


def _make_driver(cfg: Config, tel: Telemetry | None = None) -> BrowserDriver | DesktopDriver:
    if cfg.driver == "desktop":
        from .desktop import DesktopDriver
        drv = DesktopDriver(
            highlight=cfg.highlight,
            highlight_delay=cfg.highlight_delay,
            max_elements=cfg.max_elements,
            target_window=getattr(cfg, "target_window", "Google Chrome"),
        )
    else:
        drv = BrowserDriver(
            cfg.headless,
            cfg.max_elements,
            highlight=cfg.highlight,
            highlight_delay=cfg.highlight_delay,
            highlight_color=cfg.highlight_color,
            browser_path=cfg.browser_path,
        )
    if tel is not None:
        wire_highlight(drv, tel)
    return drv


def build(cfg: Config | None = None) -> Runtime:
    cfg = cfg or Config.from_env()
    cfg.require()
    tel = Telemetry(cfg.log_lines)
    if cfg.offline:
        clef: ClefClient | HeuristicClefClient = HeuristicClefClient(cfg, tel)
        llm: LLMClient | HeuristicLLMClient = HeuristicLLMClient(cfg, tel)
    else:
        clef, llm = ClefClient(cfg, tel), LLMClient(cfg, tel)
    drv = _make_driver(cfg, tel)
    return Runtime(cfg, tel, Agent(cfg, tel, clef, llm, drv), drv, clef, llm)


def stats_line(tel: Telemetry) -> str:
    s = tel.stats
    conf = "-" if s.last_confidence is None else f"{s.last_confidence:.2f}"
    stage = f" [{s.stage_index}/{s.stage_total}]" if s.stage_total else ""
    return (
        f"{s.state.upper():<10}{stage} model={s.model.rsplit('/', 1)[-1]:<16} step={s.step} try={s.attempt} "
        f"t={s.elapsed():5.1f}s  clef={s.last_latency_ms:4.0f}ms conf={conf}  "
        f"calls clef/llm={s.clef_calls}/{s.llm_calls}  tok in/out={s.in_tokens}/{s.out_tokens}  "
        f"stream={s.stream_tps:5.1f} tok/s"
    )
