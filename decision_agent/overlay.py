"""Element highlight for the desktop driver: four thin always-on-top bars (so the interior stays
click-through) that fade out over `decay` seconds. Must live on the Tk thread."""
from __future__ import annotations

import time
import tkinter as tk


class Highlight:
    def __init__(self, root: tk.Misc) -> None:
        self.root = root
        self.bars: list[tk.Toplevel] = []
        self.t0 = 0.0
        self.decay = 0.0
        self.job: str | None = None

    def _make(self) -> None:
        if self.bars:
            return
        for _ in range(4):
            w = tk.Toplevel(self.root)
            w.overrideredirect(True)
            w.attributes("-topmost", True)
            w.withdraw()
            self.bars.append(w)

    def show(self, bbox: tuple[int, int, int, int], color: str, thickness: int, decay: float) -> None:
        self.hide()
        if decay <= 0:
            return
        self._make()
        pad, t = 2, max(1, int(thickness))
        x, y, w, h = bbox[0] - pad, bbox[1] - pad, bbox[2] + 2 * pad, bbox[3] + 2 * pad
        for bar, (rx, ry, rw, rh) in zip(self.bars, ((x, y, w, t), (x, y + h - t, w, t),
                                                     (x, y, t, h), (x + w - t, y, t, h))):
            bar.configure(bg=color)
            bar.geometry(f"{rw}x{rh}+{rx}+{ry}")
            bar.attributes("-alpha", 1.0)
            bar.deiconify()
        self.t0, self.decay = time.time(), decay
        self._fade()

    def _fade(self) -> None:
        a = 1.0 - (time.time() - self.t0) / self.decay
        if a <= 0:
            self.hide()
            return
        for b in self.bars:
            b.attributes("-alpha", a)
        self.job = self.root.after(40, self._fade)

    def hide(self) -> None:
        if self.job:
            self.root.after_cancel(self.job)
            self.job = None
        for b in self.bars:
            b.withdraw()
