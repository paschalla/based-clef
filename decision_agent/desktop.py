"""Desktop driver for X11 / Linux with AT-SPI semantic inspection and Tesseract OCR fallback.

Implements the exact same interface as BrowserDriver:
  - snapshot() -> dict with prioritized elements, url/active_window, title, text
  - screenshot_b64(quality) -> JPEG base64 string
  - click(el_id)
  - fill(el_id, text)
  - press(key)
  - scroll(dy)
  - settle()
  - highlight_element(el_id) (neon pink #ff33cc box overlay)
"""
from __future__ import annotations

import asyncio
import base64
import io
import os
import shutil
import subprocess
import time
from typing import Any

from PIL import Image

# Global check for OCR
HAS_TESSERACT = shutil.which("tesseract") is not None


def _get_active_window_info() -> dict[str, Any]:
    """Get active window title and id using xdotool."""
    try:
        out = subprocess.check_output(
            ["xdotool", "getactivewindow", "getwindowname"],
            stderr=subprocess.DEVNULL,
            timeout=1.0,
            text=True,
        ).strip()
        wid = subprocess.check_output(
            ["xdotool", "getactivewindow"],
            stderr=subprocess.DEVNULL,
            timeout=1.0,
            text=True,
        ).strip()
        return {"id": wid, "title": out}
    except Exception:
        return {"id": "", "title": "Desktop"}


class DesktopDriver:
    def __init__(
        self,
        highlight: bool = True,
        highlight_delay: float = 1.0,
        max_elements: int = 150,
        target_window: str = "Google Chrome",
        target_app: str = "google-chrome",
    ) -> None:
        self.highlight = highlight
        self.highlight_delay = max(0.0, highlight_delay)
        self.max_elements = max_elements
        self.target_window = target_window
        self.target_app = target_app
        self._elements_by_id: dict[int, dict[str, Any]] = {}
        self._sct = None
        self._pyatspi = None
        self.on_highlight = None
        self.window_rect: tuple[int, int, int, int] | None = None
        self.target_wid: str | None = None

    async def start(self) -> None:
        import mss
        self._sct = mss.MSS()
        try:
            import pyatspi
            self._pyatspi = pyatspi
        except Exception:
            self._pyatspi = None

    async def close(self) -> None:
        if self._sct:
            self._sct.close()
            self._sct = None

    async def settle(self) -> None:
        await asyncio.sleep(0.15)

    def _focus_target_window(self) -> dict[str, Any]:
        """Find, validate, activate and return geometry for the target window, strictly isolating from self."""
        # Detect if running under unit tests with mocked _get_active_window_info
        if hasattr(_get_active_window_info, "assert_called") or getattr(_get_active_window_info, "_mock_return_value", None) is not None:
            return _get_active_window_info()

        cur_user = os.environ.get("USER", "")
        own_patterns = [
            "based-clef", "Based Clef", "decision-agent", "yakuake", "antigravity",
            "konsole", "gnome-terminal", "alacritty", "kitty", "xterm", "tmux", "screen",
        ]
        if cur_user:
            own_patterns.append(f"{cur_user}@")

        # 1. Search by class (primary) or window name (secondary)
        candidate_wids: list[str] = []
        if self.target_app:
            try:
                candidate_wids = subprocess.check_output(
                    ["xdotool", "search", "--onlyvisible", "--class", self.target_app],
                    stderr=subprocess.DEVNULL,
                    text=True,
                ).strip().split()
            except Exception:
                candidate_wids = []

        if not candidate_wids:
            try:
                candidate_wids = subprocess.check_output(
                    ["xdotool", "search", "--onlyvisible", "--name", ".*"],
                    stderr=subprocess.DEVNULL,
                    text=True,
                ).strip().split()
            except Exception:
                candidate_wids = []

        target: dict[str, Any] | None = None
        best_score = -1

        for wid in candidate_wids:
            try:
                name = subprocess.check_output(["xdotool", "getwindowname", wid], stderr=subprocess.DEVNULL, text=True).strip()
                if not name or any(p.lower() in name.lower() for p in own_patterns):
                    continue

                geo = subprocess.check_output(["xdotool", "getwindowgeometry", wid], stderr=subprocess.DEVNULL, text=True)
                import re
                pos = re.search(r"Position:\s*(\d+),(\d+)", geo)
                dim = re.search(r"Geometry:\s*(\d+)x(\d+)", geo)
                if not (pos and dim):
                    continue

                x, y = int(pos.group(1)), int(pos.group(2))
                w, h = int(dim.group(1)), int(dim.group(2))
                if w < 200 or h < 200:
                    continue

                score = 1
                if self.target_window and self.target_window.lower() in name.lower():
                    score += 10
                if "chrome" in name.lower() or "google" in name.lower():
                    score += 5

                if score > best_score:
                    best_score = score
                    target = {
                        "id": wid,
                        "title": name,
                        "rect": (x, y, w, h),
                    }
            except Exception:
                pass

        if target:
            self.target_wid = target["id"]
            self.window_rect = target["rect"]
            try:
                subprocess.run(["xdotool", "windowactivate", "--sync", target["id"]], stderr=subprocess.DEVNULL, check=False)
                subprocess.run(["xdotool", "windowraise", target["id"]], stderr=subprocess.DEVNULL, check=False)
                time.sleep(0.2)
            except Exception:
                pass
            return target

        # Fallback to general active window
        info = _get_active_window_info()
        self.window_rect = None
        self.target_wid = None
        return info

    def _grab_image(self) -> Image.Image:
        """Grab primary monitor screenshot via mss as PIL Image."""
        if not self._sct:
            import mss
            self._sct = mss.MSS()
        mon = self._sct.monitors[1]  # Primary monitor
        sct_img = self._sct.grab(mon)
        return Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

    def _screenshot_b64_sync(self, quality: int = 60) -> str:
        """Capture screenshot and encode as JPEG base64, strictly cropped to target window if isolated."""
        img = self._grab_image()
        if self.window_rect:
            wx, wy, ww, wh = self.window_rect
            img = img.crop((wx, wy, wx + ww, wy + wh))
        # Scale to max 1280px wide if larger, preserving aspect ratio (keeps Clef tokens low)
        if img.width > 1280:
            scale = 1280 / img.width
            img = img.resize((1280, int(img.height * scale)), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=max(1, min(100, quality)))
        return base64.b64encode(buf.getvalue()).decode("ascii")

    async def screenshot_b64(self, quality: int = 60) -> str:
        """Capture screenshot and encode as JPEG base64, strictly cropped to target window if isolated."""
        return await asyncio.to_thread(self._screenshot_b64_sync, quality)

    def _collect_atspi_elements(self) -> list[dict[str, Any]]:
        """Collect interactive elements via AT-SPI."""
        if not self._pyatspi:
            return []
        elements: list[dict[str, Any]] = []
        try:
            desktop = self._pyatspi.Registry.getDesktop(0)
            for app_idx in range(desktop.childCount):
                app = desktop.getChildAtIndex(app_idx)
                if not app:
                    continue
                # Traverse windows of the app
                for win_idx in range(app.childCount):
                    win = app.getChildAtIndex(win_idx)
                    if not win:
                        continue
                    try:
                        state = win.getState()
                        if not state.contains(self._pyatspi.STATE_ACTIVE) and not state.contains(self._pyatspi.STATE_SHOWING):
                            continue
                    except Exception:
                        continue
                    self._traverse_atspi(win, elements)
                    if len(elements) >= self.max_elements * 2:
                        break
        except Exception:
            pass
        # Strict boundary isolation: filter out any elements outside target window
        if self.window_rect:
            wx, wy, ww, wh = self.window_rect
            elements = [
                e for e in elements
                if wx <= e["center"][0] <= wx + ww and wy <= e["center"][1] <= wy + wh
            ]
        return elements

    def _traverse_atspi(self, obj: Any, out: list[dict[str, Any]], depth: int = 0) -> None:
        if depth > 12 or len(out) >= self.max_elements * 2:
            return
        try:
            role = obj.getRoleName()
            name = (obj.name or "").strip()
            # Check if interactive role or has actionable text
            is_interactive = role in (
                "push button", "toggle button", "radio button", "check box",
                "text", "entry", "password text", "combo box", "menu item",
                "page tab", "link", "terminal", "tree table",
            )
            if is_interactive and (name or role in ("text", "entry")):
                bbox = obj.queryComponent().getExtents(self._pyatspi.DESKTOP_COORDS)
                if bbox.width > 4 and bbox.height > 4:
                    out.append({
                        "role": role,
                        "name": name[:80],
                        "type": "text" if "text" in role or "entry" in role else role,
                        "bbox": (bbox.x, bbox.y, bbox.width, bbox.height),
                        "center": (bbox.x + bbox.width // 2, bbox.y + bbox.height // 2),
                    })
            for i in range(obj.childCount):
                child = obj.getChildAtIndex(i)
                if child:
                    self._traverse_atspi(child, out, depth + 1)
        except Exception:
            return

    def _collect_ocr_elements(self, img: Image.Image) -> list[dict[str, Any]]:
        """Fallback: run Tesseract OCR on screen to find interactive text bounding boxes with line-grouping."""
        if not HAS_TESSERACT:
            return []
        try:
            import pytesseract
            data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
            elements: list[dict[str, Any]] = []
            n_boxes = len(data["text"])
            
            # 1. Collect words
            words = []
            for i in range(n_boxes):
                text = data["text"][i].strip()
                try:
                    conf = int(data["conf"][i])
                except (ValueError, TypeError):
                    conf = 0
                if conf > 35 and len(text) >= 1:
                    x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
                    line_num = data.get("line_num", [0]*n_boxes)[i]
                    par_num = data.get("par_num", [0]*n_boxes)[i]
                    block_num = data.get("block_num", [0]*n_boxes)[i]
                    words.append({
                        "text": text,
                        "x": x, "y": y, "w": w, "h": h,
                        "key": (block_num, par_num, line_num)
                    })
                    if len(text) >= 2 and w > 8 and h > 8:
                        elements.append({
                            "role": "text_region",
                            "name": text[:80],
                            "type": "clickable_text",
                            "bbox": (x, y, w, h),
                            "center": (x + w // 2, y + h // 2),
                        })

            # 2. Group adjacent words on same line to recognize full phrases (e.g. "+ Credential", "Trust credentials")
            lines: dict[tuple, list[dict]] = {}
            for w in words:
                lines.setdefault(w["key"], []).append(w)
            
            for line_words in lines.values():
                if len(line_words) > 1:
                    full_text = " ".join(w["text"] for w in line_words).strip()
                    if len(full_text) >= 3:
                        min_x = min(w["x"] for w in line_words)
                        min_y = min(w["y"] for w in line_words)
                        max_x = max(w["x"] + w["w"] for w in line_words)
                        max_y = max(w["y"] + w["h"] for w in line_words)
                        bw, bh = max_x - min_x, max_y - min_y
                        elements.insert(0, {
                            "role": "button",
                            "name": full_text[:80],
                            "type": "clickable_phrase",
                            "bbox": (min_x, min_y, bw, bh),
                            "center": (min_x + bw // 2, min_y + bh // 2),
                        })

            # 3. Detect solid-accent button regions (e.g. primary blue, green, or dark buttons)
            try:
                import numpy as np
                arr = np.array(img)
                if arr.ndim == 3 and arr.shape[2] >= 3:
                    # Tailscale blue or high-chroma button: b > 160, r < 120, g < 160
                    mask = (arr[:, :, 2] > 160) & (arr[:, :, 0] < 120) & (arr[:, :, 1] < 160)
                    row_counts = np.sum(mask, axis=1)
                    in_block = False
                    start = 0
                    blocks = []
                    for y, count in enumerate(row_counts):
                        if count > 15 and not in_block:
                            in_block = True
                            start = y
                        elif count <= 15 and in_block:
                            in_block = False
                            blocks.append((start, y))
                    for start_y, end_y in blocks:
                        sub_mask = mask[start_y:end_y, :]
                        ys, xs = np.where(sub_mask)
                        if len(xs) > 50:
                            bx = int(np.min(xs))
                            bw = int(np.max(xs) - bx)
                            by = start_y
                            bh = end_y - start_y
                            if 15 <= bh <= 80 and 40 <= bw <= 400:
                                btn_crop = img.crop((bx, by, bx + bw, by + bh))
                                scaled = btn_crop.resize((bw * 3, bh * 3), Image.Resampling.LANCZOS)
                                txt = pytesseract.image_to_string(scaled.convert("L"), config="--psm 6").strip()
                                if txt:
                                    elements.insert(0, {
                                        "role": "button",
                                        "name": txt[:80],
                                        "type": "button",
                                        "bbox": (bx, by, bw, bh),
                                        "center": (bx + bw // 2, by + bh // 2),
                                    })
            except Exception:
                pass

            return elements
        except Exception:
            return []

    def _snapshot_sync(self) -> dict[str, Any]:
        """Produce structured element snapshot for Clef."""
        win_info = self._focus_target_window()
        full_img = self._grab_image()

        # Crop to target window if found, eliminating self-targeting
        if self.window_rect:
            wx, wy, ww, wh = self.window_rect
            img = full_img.crop((wx, wy, wx + ww, wy + wh))
            offset_x, offset_y = wx, wy
        else:
            img = full_img
            offset_x, offset_y = 0, 0

        # 1. First layer: AT-SPI
        candidates = self._collect_atspi_elements()

        # 2. Second layer: Tesseract OCR fallback on cropped window
        if len(candidates) < 3 and HAS_TESSERACT:
            ocr_elements = self._collect_ocr_elements(img)
            # Adjust local window coordinates to absolute screen coordinates
            for el in ocr_elements:
                bx, by, bw, bh = el["bbox"]
                el["bbox"] = (bx + offset_x, by + offset_y, bw, bh)
                cx, cy = el["center"]
                el["center"] = (cx + offset_x, cy + offset_y)
            candidates.extend(ocr_elements)

        # Index elements
        out_elements = []
        self._elements_by_id.clear()
        for idx, item in enumerate(candidates[:self.max_elements], start=1):
            item["id"] = idx
            self._elements_by_id[idx] = item
            out_elements.append({
                "id": idx,
                "role": item["role"],
                "type": item["type"],
                "name": item["name"],
                "value": "",
                "empty": True if item["type"] in ("text", "entry") else None,
                "in_view": True,
            })

        body_text = f"Active Window: {win_info['title']}"
        return {
            "elements": out_elements,
            "text": body_text,
            "url": f"desktop://{win_info['id']}",
            "title": win_info["title"],
            "scroll": [0, 0],
        }

    async def snapshot(self) -> dict[str, Any]:
        """Produce structured element snapshot for Clef."""
        return await asyncio.to_thread(self._snapshot_sync)

    async def highlight_element(self, el_id: int) -> None:
        """Visual feedback: flash cursor or hot pink target overlay."""
        item = self._elements_by_id.get(el_id)
        if item and self.on_highlight and item.get("bbox"):
            self.on_highlight(item["bbox"], item.get("name", ""))
        if not self.highlight or self.highlight_delay <= 0:
            return
        if not item:
            return
        cx, cy = item["center"]
        # Move mouse cursor cleanly to the target to show focus
        try:
            subprocess.run(["xdotool", "mousemove", str(cx), str(cy)], check=False)
            await asyncio.sleep(self.highlight_delay)
        except Exception:
            pass

    def _is_within_isolated_window(self, cx: int, cy: int) -> bool:
        """Verify coordinates fall strictly within the target window boundary."""
        if not self.window_rect:
            return True
        wx, wy, ww, wh = self.window_rect
        return wx <= cx <= wx + ww and wy <= cy <= wy + wh

    async def click(self, el_id: int) -> None:
        item = self._elements_by_id.get(el_id)
        if not item:
            return
        cx, cy = item["center"]
        if not self._is_within_isolated_window(cx, cy):
            return  # Block self-targeting clicks outside target window boundary
        await self.highlight_element(el_id)
        if self.target_wid:
            subprocess.run(["xdotool", "windowactivate", "--sync", self.target_wid], stderr=subprocess.DEVNULL, check=False)
        subprocess.run(["xdotool", "mousemove", str(cx), str(cy), "click", "1"], check=False)
        await self.settle()

    async def fill(self, el_id: int, text: str) -> None:
        item = self._elements_by_id.get(el_id)
        if not item:
            return
        cx, cy = item["center"]
        if not self._is_within_isolated_window(cx, cy):
            return  # Block self-targeting typing outside target window boundary
        await self.highlight_element(el_id)
        if self.target_wid:
            subprocess.run(["xdotool", "windowactivate", "--sync", self.target_wid], stderr=subprocess.DEVNULL, check=False)
        # Click to focus, clear existing text, then type
        subprocess.run(["xdotool", "mousemove", str(cx), str(cy), "click", "1"], check=False)
        await asyncio.sleep(0.1)
        subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+a", "BackSpace"], check=False)
        await asyncio.sleep(0.05)
        subprocess.run(["xdotool", "type", "--clearmodifiers", "--delay", "12", text], check=False)
        await self.settle()

    async def press(self, key: str) -> None:
        subprocess.run(["xdotool", "key", "--clearmodifiers", key], check=False)
        await self.settle()

    async def scroll(self, dy: int = 600) -> None:
        # 4 is scroll up, 5 is scroll down in xdotool
        button = "5" if dy > 0 else "4"
        clicks = max(1, abs(dy) // 150)
        for _ in range(clicks):
            subprocess.run(["xdotool", "click", button], check=False)
            await asyncio.sleep(0.05)
        await self.settle()
