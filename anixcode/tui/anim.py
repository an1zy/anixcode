"""Animated logo, the "agent is working" indicator, and the built-in themes."""
from __future__ import annotations
from ..i18n import tr

import math
import random
import time

from rich.color import Color, ColorTriplet, blend_rgb
from rich.style import Style
from rich.text import Text
from textual.widgets import Static

PEACH, BLUE, PURPLE = "#fab283", "#5c9cf5", "#9d7cd8"
WHITE = ColorTriplet(255, 255, 255)
BLACK = ColorTriplet(0, 0, 0)
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
WAVE = "▁▂▃▄▅▆▇█"
SCRAMBLE = "░▒▓█▚▞▙▟▛▜"

# 5-row block font. Only █ and spaces: looks the same in cmd.exe and Windows Terminal.
FONT = {
    "A": [" ████ ", "██  ██", "██████", "██  ██", "██  ██"],
    "N": ["██   ██", "███  ██", "██ █ ██", "██  ███", "██   ██"],
    "I": ["██", "██", "██", "██", "██"],
    "X": ["██   ██", " ██ ██ ", "  ███  ", " ██ ██ ", "██   ██"],
}
THEME_LABELS = {
    "anix": tr("ANIX · персиковая"),
    "noir": tr("Noir · чёрно-белая"),
    "tokyo-night": "Tokyo Night",
    "catppuccin-mocha": "Catppuccin Mocha",
    "dracula": "Dracula",
    "nord": "Nord",
    "gruvbox": "Gruvbox",
    "monokai": "Monokai",
    "flexoki": "Flexoki",
    "textual-dark": "Textual Dark",
}


def make_themes() -> list:
    """Our Textual themes (empty on old Textual versions without theme support)."""
    try:
        from textual.theme import Theme
    except ImportError:
        return []
    base = {"foreground": "#eeeeee", "background": "#0a0a0a", "surface": "#141414", "panel": "#1e1e1e",
            "dark": True}
    return [
        Theme(name="anix", primary=PEACH, secondary=BLUE, accent=PURPLE, success="#7fd88f", warning="#f5a742",
              error="#e06c75", **base),
        Theme(name="noir", primary="#eeeeee", secondary="#a0a0a0", accent="#c8c8c8", success="#a6d189",
              warning="#d8c08c", error="#e06c75", **base),
    ]


def _rgb(hex_color: str) -> ColorTriplet:
    return Color.parse(hex_color).get_truecolor()


def palette(app) -> tuple[str, str, str]:
    """(primary, accent, secondary) of the active theme."""
    try:
        t = app.current_theme
        return t.primary or PEACH, t.accent or PURPLE, t.secondary or BLUE
    except Exception:
        return PEACH, PURPLE, BLUE


class Gradient:
    """Cyclic gradient through the given colors: at(0) == at(1)."""

    def __init__(self, colors: tuple[str, ...]) -> None:
        self.stops = [_rgb(c) for c in colors]

    def at(self, x: float) -> ColorTriplet:
        n = len(self.stops)
        x = (x % 1.0) * n
        i = int(x)
        return blend_rgb(self.stops[i % n], self.stops[(i + 1) % n], x - i)


def _style(c: ColorTriplet, bold: bool = False) -> Style:
    return Style(color=c.hex, bold=bold)


def render_word(word: str, gap: int = 2) -> list[str]:
    rows = [""] * 5
    for i, ch in enumerate(word):
        glyph = FONT[ch]
        width = max(len(r) for r in glyph)
        for r in range(5):
            rows[r] += glyph[r].ljust(width) + (" " * gap if i < len(word) - 1 else "")
    return rows


class AnimatedLogo(Static):
    """Big ANIX logo.

    Intro: every block "decodes" from random glyphs, in a wave from left to right.
    Then: the theme gradient flows through it, a diagonal shine crosses it every few
    seconds, a soft 3D shadow sits underneath, and once in a while it glitches.
    """

    def __init__(self, footer: Text, animate: bool = True, word: str = "ANIX", **kwargs) -> None:
        super().__init__("", **kwargs)
        self.logo_rows = render_word(word)
        self.logo_footer = footer
        self.logo_motion = animate
        self._logo_t0 = time.monotonic()
        self._theme_name = ""
        self._gradient = Gradient((PEACH, PURPLE, BLUE))
        self.logo_w = max(len(r) for r in self.logo_rows)
        self.logo_h = len(self.logo_rows)
        rnd = random.Random(7)
        self._cells = {(r, c) for r in range(self.logo_h) for c in range(self.logo_w)
                       if c < len(self.logo_rows[r]) and self.logo_rows[r][c] != " "}
        self._settle = {cell: 0.15 + cell[1] / self.logo_w * 0.9 + rnd.random() * 0.3 for cell in self._cells}
        self._noise = {cell: rnd.random() for cell in self._cells}

    def on_mount(self) -> None:
        self._frame()
        if self.logo_motion:
            self.set_interval(1 / 24, self._frame)

    def _colors(self) -> None:
        name = getattr(self.app, "theme", "")
        if name != self._theme_name:
            self._theme_name = name
            self._gradient = Gradient(palette(self.app))

    def _frame(self) -> None:
        self._colors()
        t = time.monotonic() - self._logo_t0 if self.logo_motion else 99.0
        w, h = self.logo_w, self.logo_h
        span = w + h * 2
        shine = ((t - 1.6) % 5.0) / 5.0 * (span + 16) - 8  # diagonal band, every 5 s
        glitching = self.logo_motion and t > 2 and (t % 7.3) < 0.14
        frame_no = int(t * 24)
        text = Text()
        for r in range(h + 1):  # +1 row for the shadow
            for c in range(w + 1):
                cell = (r, c)
                if cell in self._cells:
                    settle = self._settle[cell]
                    if t < settle - 0.45:
                        text.append(" ")
                        continue
                    base = self._gradient.at(c / w * 0.55 + r * 0.035 - t * 0.07)
                    if t < settle:  # still decoding: random glyph, bright
                        ch = SCRAMBLE[(frame_no + int(self._noise[cell] * 97)) % len(SCRAMBLE)]
                        text.append(ch, _style(blend_rgb(base, WHITE, 0.55), bold=True))
                        continue
                    if glitching and self._noise[cell] < 0.09:
                        text.append("▓", _style(blend_rgb(base, WHITE, 0.3), bold=True))
                        continue
                    glow = max(0.0, 1 - abs((c + r * 2) - shine) / 3.5)
                    pulse = 0.06 * math.sin(t * 1.7 + c * 0.25)  # gentle breathing
                    color = blend_rgb(base, WHITE, min(1.0, max(0.0, glow * 0.8 + pulse)))
                    text.append("█", _style(color, bold=True))
                elif (r - 1, c - 1) in self._cells and t >= self._settle[(r - 1, c - 1)]:
                    shade = self._gradient.at(c / w * 0.55 - t * 0.07)
                    text.append("░", _style(blend_rgb(shade, BLACK, 0.72)))
                else:
                    text.append(" ")
            text.append("\n")
        # "c o d e" typed in after the logo settles
        sub = "c  o  d  e"
        shown = max(0, min(len(sub), int((t - 1.3) * 18)))
        line = sub[:shown].ljust(len(sub))
        for i, ch in enumerate(line):
            text.append(ch, _style(self._gradient.at(i / len(sub) * 0.4 - t * 0.07)))
        text.append("\n")
        text.append_text(self.logo_footer)
        self.update(text)


class ActivityIndicator(Static):
    """⠹ Пишет ответ…  ▂▃▅▇▆▄▂▁▂▃▅▇  12s · ≈1.2k токенов — shown while the agent works."""

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self.phase = ""
        self.chars = 0
        self.paused = False
        self.motion = True
        self._t_start = time.monotonic()
        self._gradient = Gradient((PEACH, PURPLE, BLUE))
        self._c_primary = _rgb(PEACH)
        self._c_dim = _rgb("#2a2a2a")

    def on_mount(self) -> None:
        self.display = False
        self.set_interval(1 / 15, self._frame)

    def start(self) -> None:
        primary, accent, secondary = palette(self.app)
        self._gradient = Gradient((primary, accent, secondary))
        self._c_primary = _rgb(primary)
        self._t_start = time.monotonic()
        self.chars = 0
        self.set_phase(tr("Думает"))
        self.display = True

    def stop(self) -> None:
        self.display = False

    def set_phase(self, phase: str, paused: bool = False) -> None:
        self.phase = phase
        self.paused = paused
        self._frame()

    def _frame(self) -> None:
        if not self.display:
            return
        t = time.monotonic() - self._t_start
        moving = self.motion and not self.paused
        text = Text()
        spin = "⏸" if self.paused else (SPINNER[int(t * 12) % len(SPINNER)] if moving else "●")
        text.append(spin + " ", _style(self._gradient.at(t * 0.25), bold=True))
        label = self.phase[:60] + ("" if self.paused else "…")
        band = (t * 14) % (len(label) + 8) - 4  # shimmer running through the label
        for i, ch in enumerate(label):
            glow = max(0.0, 1 - abs(i - band) / 3) if moving else 0.0
            text.append(ch, _style(blend_rgb(self._c_primary, WHITE, glow * 0.8), bold=glow > 0.4))
        text.append("  ")
        for i in range(16):  # equalizer-style wave
            if moving:
                level = (math.sin(t * 5.2 - i * 0.55) * 0.6 + math.sin(t * 2.1 + i * 0.9) * 0.4 + 1) / 2
            else:
                level = 0.0
            ch = WAVE[min(len(WAVE) - 1, int(level * len(WAVE)))]
            color = blend_rgb(self._c_dim, self._gradient.at(i / 16 * 0.6 + t * 0.15), 0.35 + 0.65 * level)
            text.append(ch, _style(color))
        text.append(f"  {int(t)}s", style="dim")
        if self.chars:
            approx = self.chars // 4
            text.append(tr(' · ≈{0:.1f}k токенов', approx / 1000) if approx >= 1000 else tr(' · ≈{0} токенов', approx),
                        style="dim")
        text.append(tr("   esc — стоп"), style="dim")
        self.update(text)
