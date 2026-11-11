"""Playwright browser driver: deterministic screen parsing into numbered elements."""
from __future__ import annotations

import asyncio
import base64
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, Page, async_playwright

# Collect visible, interactive elements and tag them with data-da-id.
_SNAPSHOT_JS = r"""
(max) => {
  const sel = 'a[href],button,input,select,textarea,[role=button],[role=link],[role=checkbox],[role=tab],[role=menuitem],[role=option],[contenteditable=true]';
  document.querySelectorAll('[data-da-id]').forEach(e => e.removeAttribute('data-da-id'));
  const candidates = [];
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    if (r.width < 2 || r.height < 2 || cs.visibility === 'hidden' || cs.display === 'none') continue;
    if (r.bottom < 0 || r.top > innerHeight * 3) continue;
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute('role') || tag;
    const isInput = tag === 'input' || tag === 'button' || tag === 'select' || tag === 'textarea' || role === 'button' || role === 'searchbox';
    const in_view = r.top < innerHeight && r.bottom > 0 && r.left < innerWidth && r.right > 0;
    const prio = (in_view ? 0 : 2) + (isInput ? 0 : 1);
    candidates.push({el, r, tag, role, in_view, prio});
  }
  candidates.sort((a, b) => a.prio - b.prio);
  const out = [];
  for (let i = 0; i < Math.min(candidates.length, max); i++) {
    const {el, r, tag, role, in_view} = candidates[i];
    const id = i + 1;
    el.setAttribute('data-da-id', id);
    const label = (el.labels && el.labels[0] && el.labels[0].innerText) || el.getAttribute('aria-label')
      || el.getAttribute('placeholder') || el.innerText || el.value || el.getAttribute('title') || el.getAttribute('name') || '';
    const type = el.getAttribute('type') || '';
    const value = (tag === 'input' && type === 'password') ? (el.value ? '***' : '') : (el.value || '');
    out.push({
      id, tag, role, type,
      name: label.trim().replace(/\s+/g,' ').slice(0,80),
      value: value.slice(0,40),
      empty: ('value' in el) ? !el.value : null,
      required: !!el.required,
      disabled: !!el.disabled,
      checked: el.checked ?? null,
      in_view,
      bbox: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)]
    });
  }
  const text = (document.body ? (document.body.innerText || '') : '').replace(/\s+/g,' ').slice(0, 800);
  return {
    elements: out,
    text,
    url: location.href,
    title: document.title,
    scroll: [Math.round(scrollY), Math.round(document.documentElement.scrollHeight - innerHeight)],
    viewport: [innerWidth, innerHeight]
  };
}
"""


def find_browser_executable(explicit_path: str = "") -> tuple[str | None, str | None]:
    """Return `(channel, executable_path)` for cross-platform Chrome/Chromium launch."""
    env_path = explicit_path or os.environ.get("BASED_CLEF_BROWSER_PATH") or os.environ.get("DECISION_BROWSER_PATH") or ""
    if env_path and Path(env_path).exists():
        return None, env_path

    # Prefer official Google Chrome channel when actually installed at standard OS locations
    if sys.platform.startswith("linux") and Path("/opt/google/chrome/chrome").exists():
        return "chrome", None
    if sys.platform == "darwin" and Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome").exists():
        return "chrome", None

    for bin_name in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
        found = shutil.which(bin_name)
        if found:
            resolved = str(Path(found).resolve())
            if resolved == "/opt/google/chrome/chrome":
                return "chrome", None
            return None, found

    return None, None


class BrowserDriver:
    def __init__(
        self,
        headless: bool = False,
        max_elements: int = 80,
        highlight: bool = False,
        highlight_delay: float = 0.3,
        highlight_color: str = "#ff33cc",
        browser_path: str = "",
    ) -> None:
        self.headless, self.max_elements = headless, max_elements
        self.highlight, self.highlight_delay = highlight, highlight_delay
        self.highlight_color = highlight_color
        self.browser_path = browser_path
        self._pw = None
        self.browser: Browser | None = None
        self.page: Page | None = None

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        channel, exe = find_browser_executable(self.browser_path)
        if channel:
            self.browser = await self._pw.chromium.launch(channel=channel, headless=self.headless)
        elif exe:
            self.browser = await self._pw.chromium.launch(executable_path=exe, headless=self.headless)
        else:
            # Fallback to Playwright's installed chromium bundle
            try:
                self.browser = await self._pw.chromium.launch(headless=self.headless)
            except Exception as e:
                await self._pw.stop()
                self._pw = None
                raise RuntimeError(
                    "No Chrome or Chromium binary found. Install Google Chrome / Chromium or run: "
                    "`playwright install chromium`"
                ) from e
        ctx = await self.browser.new_context(viewport={"width": 1100, "height": 800}, ignore_https_errors=True)
        self.page = await ctx.new_page()

    async def close(self) -> None:
        if self.browser:
            await self.browser.close()
        if self._pw:
            await self._pw.stop()
        self.page = self.browser = self._pw = None

    async def goto(self, url: str) -> None:
        await self.page.goto(url, wait_until="domcontentloaded")

    async def snapshot(self) -> dict[str, Any]:
        try:
            return await self.page.evaluate(_SNAPSHOT_JS, self.max_elements)
        except Exception as e:
            msg = str(e)
            if "Execution context was destroyed" in msg or "TargetClosed" in type(e).__name__ or "target closed" in msg.lower():
                await self.settle()
                return await self.page.evaluate(_SNAPSHOT_JS, self.max_elements)
            raise

    async def screenshot_b64(self, quality: int = 60) -> str:
        q = max(1, min(100, quality))
        data = await self.page.screenshot(type="jpeg", quality=q)
        return base64.b64encode(data).decode("ascii").strip()

    # -- actions ----------------------------------------------------------
    def _loc(self, el_id: int):
        return self.page.locator(f'[data-da-id="{el_id}"]')

    async def highlight_element(self, el_id: int) -> None:
        if self.headless or not self.highlight or self.highlight_delay <= 0 or not self.page:
            return
        try:
            loc = self._loc(el_id)
            color = self.highlight_color or "#ff33cc"
            await loc.evaluate(
                r"""
                (el, color) => {
                    const prevOutline = el.style.outline;
                    const prevShadow = el.style.boxShadow;
                    el.style.outline = `3px solid ${color}`;
                    el.style.boxShadow = `0 0 12px ${color}`;
                    setTimeout(() => {
                        el.style.outline = prevOutline;
                        el.style.boxShadow = prevShadow;
                    }, 850);
                }
                """,
                color,
            )
            await asyncio.sleep(self.highlight_delay)
        except Exception:
            pass

    async def click(self, el_id: int) -> None:
        await self.highlight_element(el_id)
        loc = self._loc(el_id)
        try:
            await loc.click(timeout=5000)
        except Exception:
            await loc.click(timeout=5000, force=True)

    async def fill(self, el_id: int, text: str) -> None:
        await self.highlight_element(el_id)
        loc = self._loc(el_id)
        try:
            tag = await loc.evaluate("el => el.tagName ? el.tagName.toLowerCase() : ''")
        except Exception:
            tag = ""
        if tag == "select":
            try:
                await loc.select_option(label=text, timeout=3000)
                return
            except Exception:
                await loc.select_option(value=text, timeout=3000)
                return
        await loc.fill(text, timeout=5000)

    async def press(self, key: str = "Enter", el_id: int | None = None) -> None:
        if el_id is not None:
            await self.highlight_element(el_id)
            await self._loc(el_id).press(key, timeout=4000)
        else:
            await self.page.keyboard.press(key)

    async def scroll(self, dy: int = 600) -> None:
        await self.page.mouse.wheel(0, dy)

    async def settle(self) -> None:
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:
            pass
        await self.page.wait_for_timeout(250)
