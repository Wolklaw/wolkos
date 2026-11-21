"""Long lists for the music library: rows and cover tiles drawn only where you can see them, so
10 000 songs scroll as fast as 10. Covers load in a thread, and only while some are missing."""

import queue
import threading
from collections import OrderedDict
from typing import Any, Callable, List, Optional, Sequence, Tuple

import tkinter as tk

from PIL import Image, ImageTk

from .widgets import T

Column = Tuple[str, float, str]          # (field, width: <1 = share of the rest, >=1 = px), anchor


class ScrollBar(tk.Canvas):
    """A thin flat scrollbar in the house style; drag it or click the track."""

    def __init__(self, parent: tk.Misc, target: tk.Canvas, width: int):
        super().__init__(parent, width=width, bg=T["bg"], highlightthickness=0, bd=0)
        self.target, self.first, self.last = target, 0.0, 1.0
        self._grab: Optional[float] = None
        self.bind("<Configure>", lambda _e: self.draw())
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", lambda _e: setattr(self, '_grab', None))

    def set(self, first, last):
        self.first, self.last = float(first), float(last)
        self.draw()

    def draw(self):
        self.delete("all")
        if self.last - self.first >= 0.999:
            return
        h, w = self.winfo_height(), int(self.cget("width"))
        y0, y1 = self.first * h, max(self.first * h + 12, self.last * h)
        self.create_rectangle(1, y0, w - 1, y1, fill=T["line2"], outline="")

    def _press(self, event):
        h = max(1, self.winfo_height())
        pos = event.y / h
        if self.first <= pos <= self.last:
            self._grab = pos - self.first
        else:
            self._grab = (self.last - self.first) / 2
            self._drag(event)

    def _drag(self, event):
        if self._grab is None:
            return
        h = max(1, self.winfo_height())
        self.target.yview_moveto(max(0.0, event.y / h - self._grab))


class _Virtual(tk.Frame):
    """A canvas whose items are drawn only for the part in view."""

    def __init__(self, parent: tk.Misc, app, on_activate: Callable[[int], None],
                 on_menu: Optional[Callable[[int, Any], None]] = None):
        super().__init__(parent, bg=T["bg"])
        self.app = app
        self.on_activate, self.on_menu = on_activate, on_menu
        self.canvas = tk.Canvas(self, bg=T["bg"], highlightthickness=0, bd=0, takefocus=1)
        self.scroll = ScrollBar(self, self.canvas, app.px(6))
        self.scroll.pack(side="right", fill="y", padx=(app.px(6), 0))
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.configure(yscrollcommand=self._scrolled)
        self.count = 0
        self.hover: Optional[int] = None
        self.selected = 0
        self.empty_text = ""
        self._drawn: Tuple[int, int] = (0, -1)
        self._redraw_id: Optional[str] = None
        c = self.canvas
        c.bind("<Configure>", lambda _e: self.layout())
        c.bind("<Motion>", self._motion)
        c.bind("<Leave>", lambda _e: self._set_hover(None))
        c.bind("<ButtonRelease-1>", self._click)
        c.bind("<Double-Button-1>", lambda _e: None)
        c.bind("<Button-3>", self._menu)
        c.bind("<FocusIn>", lambda _e: self._redraw_rows((self.selected,)))
        c.bind("<FocusOut>", lambda _e: self._redraw_rows((self.selected,)))
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            c.bind(seq, self._wheel)

    # subclasses: box(i) -> (x0, y0, x1, y1); total_height(); index_at(x, y); draw_item(i, tag)
    def _scrolled(self, first, last):
        self.scroll.set(first, last)
        if self._redraw_id is None:
            self._redraw_id = self.after_idle(self._draw_visible)

    def layout(self):
        c = self.canvas
        c.delete("all")
        self._drawn = (0, -1)
        width = max(1, c.winfo_width())
        if not self.count:
            if self.empty_text:
                c.create_text(width / 2, self.app.px(60), text=self.empty_text, fill=T["muted"],
                              font=self.app.font(9), width=width - self.app.px(40),
                              justify="center")
            c.configure(scrollregion=(0, 0, width, c.winfo_height()))
            return
        c.configure(scrollregion=(0, 0, width, max(self.total_height(), c.winfo_height())))
        self._draw_visible()

    def _visible_range(self) -> Tuple[int, int]:
        c = self.canvas
        top, bottom = c.canvasy(0), c.canvasy(c.winfo_height())
        return self.range_for(top, bottom)

    def _draw_visible(self):
        self._redraw_id = None
        if not self.count:
            return
        first, last = self._visible_range()
        old_first, old_last = self._drawn
        for i in range(old_first, old_last + 1):
            if i < first or i > last:
                self.canvas.delete(f"r{i}")
        for i in range(first, last + 1):
            if i < old_first or i > old_last:
                self.draw_item(i, f"r{i}")
        self._drawn = (first, last)

    def _redraw_rows(self, rows: Sequence[Optional[int]]):
        first, last = self._drawn
        for i in rows:
            if i is not None and first <= i <= last and i < self.count:
                self.canvas.delete(f"r{i}")
                self.draw_item(i, f"r{i}")

    def redraw(self):
        """Draw the visible part again (data changed, same count)."""
        first, last = self._drawn
        self._redraw_rows(range(first, last + 1))

    # ── interaction ──
    def _set_hover(self, index: Optional[int]):
        if index == self.hover:
            return
        old, self.hover = self.hover, index
        self._redraw_rows((old, index))
        self.canvas.configure(cursor="hand2" if index is not None else "")

    def _motion(self, event):
        self._set_hover(self.index_at(self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)))

    def _click(self, event):
        index = self.index_at(self.canvas.canvasx(event.x), self.canvas.canvasy(event.y))
        if index is not None:
            self.select(index)
            self.on_activate(index)

    def _menu(self, event):
        index = self.index_at(self.canvas.canvasx(event.x), self.canvas.canvasy(event.y))
        if index is not None and self.on_menu:
            self.select(index)
            self.on_menu(index, event)

    def _wheel(self, event):
        up = getattr(event, 'num', None) == 4 or getattr(event, 'delta', 0) > 0
        self.canvas.yview_scroll(-3 if up else 3, "units")
        self._motion(event)

    def select(self, index: int, see: bool = True):
        if not self.count:
            return
        old = self.selected
        self.selected = max(0, min(self.count - 1, index))
        self._redraw_rows((old, self.selected))
        if see:
            self.see(self.selected)

    def see(self, index: int):
        c = self.canvas
        _x0, y0, _x1, y1 = self.box(index)
        total = max(1.0, float(self.total_height()))
        top, bottom = c.canvasy(0), c.canvasy(c.winfo_height())
        if y0 < top:
            c.yview_moveto(y0 / total)
        elif y1 > bottom:
            c.yview_moveto((y1 - c.winfo_height() + 4) / total)

    def columns_per_row(self) -> int:
        return 1

    def key(self, event) -> bool:
        k = event.keysym
        cols = self.columns_per_row()
        moves = {'Up': -cols, 'Down': cols}
        if cols > 1:
            moves.update(Left=-1, Right=1)
        page = max(1, (self.canvas.winfo_height() // max(1, self.row_height())) * cols)
        if k in moves:
            self.select(self.selected + moves[k])
        elif k in ('Prior', 'Next'):
            self.select(self.selected + (page if k == 'Next' else -page))
        elif k == 'Home':
            self.select(0)
        elif k == 'End':
            self.select(self.count - 1)
        elif k in ('Return', 'KP_Enter'):
            if self.count:
                self.on_activate(self.selected)
        elif k == 'Menu' and self.on_menu and self.count:
            x0, y0, _x1, y1 = self.box(self.selected)
            c = self.canvas
            event.x_root = c.winfo_rootx() + int(x0) + 20
            event.y_root = c.winfo_rooty() + int(y1 - c.canvasy(0))
            self.on_menu(self.selected, event)
        else:
            return False
        self.canvas.focus_set()
        return True

    def hot(self, i: int) -> bool:
        return i == self.hover or (i == self.selected and self.canvas.focus_get() is self.canvas)

    def fit(self, text: str, width: float, size: int, bold: bool = False) -> str:
        f = self.app.tkfont(size, bold)
        if f.measure(text) <= width:
            return text
        lo, hi = 0, len(text)
        while lo < hi:                      # binary search: long titles stay fast
            mid = (lo + hi + 1) // 2
            if f.measure(text[:mid] + "…") <= width:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo] + "…"


class RowList(_Virtual):
    """Rows of text in columns. rows are tuples of strings (one per column); marks gives each
    row a look: '' normal, 'current' (now playing), 'dim' (a missing file)."""

    def __init__(self, parent: tk.Misc, app, columns: Sequence[Column],
                 on_activate: Callable[[int], None],
                 on_menu: Optional[Callable[[int, Any], None]] = None):
        super().__init__(parent, app, on_activate, on_menu)
        self.columns = list(columns)
        self.rows: List[Tuple[str, ...]] = []
        self.marks: List[str] = []
        self._xs: List[Tuple[float, float, str]] = []

    def set_rows(self, rows: Sequence[Tuple[str, ...]], marks: Optional[Sequence[str]] = None,
                 keep_scroll: bool = False):
        self.rows = list(rows)
        self.marks = list(marks) if marks else []
        self.count = len(self.rows)
        self.hover = None
        self.selected = min(self.selected, max(0, self.count - 1)) if keep_scroll else 0
        if not keep_scroll:
            self.canvas.yview_moveto(0)
        self.layout()

    def row_height(self) -> int:
        return self.app.px(30)

    def total_height(self) -> int:
        return self.count * self.row_height()

    def range_for(self, top: float, bottom: float) -> Tuple[int, int]:
        h = self.row_height()
        return max(0, int(top // h)), min(self.count - 1, int(bottom // h))

    def box(self, i: int):
        h = self.row_height()
        return 0, i * h, max(1, self.canvas.winfo_width()) - 1, (i + 1) * h - 1

    def index_at(self, x: float, y: float) -> Optional[int]:
        i = int(y // self.row_height())
        return i if 0 <= i < self.count else None

    def layout(self):
        width = max(1, self.canvas.winfo_width()) - self.app.px(12)
        fixed = sum(w for _f, w, _a in self.columns if w >= 1)
        rest = max(0, width - fixed - self.app.px(12) * len(self.columns))
        x = self.app.px(12)
        self._xs = []
        for _field, w, anchor in self.columns:
            span = w if w >= 1 else rest * w
            self._xs.append((x, span, anchor))
            x += span + self.app.px(12)
        super().layout()

    def draw_item(self, i: int, tag: str):
        c = self.canvas
        x0, y0, x1, y1 = self.box(i)
        mark = self.marks[i] if i < len(self.marks) else ''
        if self.hot(i):
            c.create_rectangle(x0, y0, x1, y1, fill=T["hover"], outline="", tags=tag)
            c.create_rectangle(x0, y0, x0 + 2, y1, fill=T["ch_a"], outline="", tags=tag)
            c.create_rectangle(x0 + 2, y0, x0 + 4, y1, fill=T["ch_b"], outline="", tags=tag)
        c.create_line(x0, y1, x1, y1, fill=T["line"], tags=tag)
        mid = (y0 + y1) / 2
        for n, ((x, span, anchor), text) in enumerate(zip(self._xs, self.rows[i])):
            if not text:
                continue
            first = n == 0 or (n == 1 and self.columns[0][1] >= 1)
            colour = T["accent"] if mark == 'current' else \
                T["dim"] if mark == 'dim' else (T["text"] if first else T["muted"])
            label = self.fit(text, span, 9 if first else 8, mark == 'current')
            c.create_text(x + span if anchor == 'e' else x, mid, text=label, anchor=anchor,
                          fill=colour, font=self.app.font(9 if first else 8, mark == 'current'),
                          tags=tag)


class TileGrid(_Virtual):
    """Album covers in a grid with two lines under each. load(key) -> PIL image or None runs
    in a worker thread; tiles show a note until their cover arrives."""

    MAX_IMAGES = 400

    def __init__(self, parent: tk.Misc, app, on_activate: Callable[[int], None],
                 on_menu: Optional[Callable[[int, Any], None]] = None,
                 load: Optional[Callable[[str], Optional[Image.Image]]] = None):
        super().__init__(parent, app, on_activate, on_menu)
        self.tiles: List[Tuple[str, str, str]] = []    # (cover key, line 1, line 2)
        self.load = load
        self.images: "OrderedDict[str, Optional[ImageTk.PhotoImage]]" = OrderedDict()
        self._wanted: set = set()
        self._jobs: "queue.Queue[Optional[Tuple[int, str, int]]]" = queue.Queue()
        self._results: "queue.Queue[Tuple[int, str, Optional[Image.Image]]]" = queue.Queue()
        self._generation = 0
        self._poll_id: Optional[str] = None
        self._worker = threading.Thread(target=self._work, daemon=True, name='covers')
        self._worker.start()

    def set_tiles(self, tiles: Sequence[Tuple[str, str, str]], keep_scroll: bool = False):
        self.tiles = list(tiles)
        self.count = len(self.tiles)
        self.hover = None
        self._generation += 1
        self._wanted.clear()
        self.selected = min(self.selected, max(0, self.count - 1)) if keep_scroll else 0
        if not keep_scroll:
            self.canvas.yview_moveto(0)
        self.layout()

    def forget_cover(self, key: str):
        self.images.pop(key, None)

    # geometry
    def _metrics(self):
        px = self.app.px
        return px(168), px(168), px(42), px(14)    # tile w, cover h, text h, gap

    def columns_per_row(self) -> int:
        tile_w, _ch, _th, gap = self._metrics()
        return max(1, (max(1, self.canvas.winfo_width()) + gap) // (tile_w + gap))

    def row_height(self) -> int:
        _tw, cover_h, text_h, gap = self._metrics()
        return cover_h + text_h + gap

    def total_height(self) -> int:
        rows = (self.count + self.columns_per_row() - 1) // self.columns_per_row()
        return rows * self.row_height()

    def range_for(self, top: float, bottom: float) -> Tuple[int, int]:
        cols, h = self.columns_per_row(), self.row_height()
        first_row, last_row = max(0, int(top // h)), int(bottom // h)
        return first_row * cols, min(self.count - 1, (last_row + 1) * cols - 1)

    def box(self, i: int):
        tile_w, cover_h, text_h, gap = self._metrics()
        r, col = divmod(i, self.columns_per_row())
        x0, y0 = col * (tile_w + gap), r * self.row_height()
        return x0, y0, x0 + tile_w, y0 + cover_h + text_h

    def index_at(self, x: float, y: float) -> Optional[int]:
        tile_w, cover_h, text_h, gap = self._metrics()
        col, row = int(x // (tile_w + gap)), int(y // self.row_height())
        if col >= self.columns_per_row() or x - col * (tile_w + gap) > tile_w:
            return None
        if y - row * self.row_height() > cover_h + text_h:
            return None
        i = row * self.columns_per_row() + col
        return i if 0 <= i < self.count else None

    def draw_item(self, i: int, tag: str):
        c, px = self.canvas, self.app.px
        tile_w, cover_h, text_h, _gap = self._metrics()
        x0, y0, x1, _y1 = self.box(i)
        key, line1, line2 = self.tiles[i]
        hot = self.hot(i)
        c.create_rectangle(x0, y0, x1, y0 + cover_h, fill=T["tile"],
                           outline=T["accent"] if hot else T["line"], tags=tag)
        if hot:
            c.create_rectangle(x0 - 3, y0, x0 - 1, y0 + cover_h, fill=T["ch_a"], outline="",
                               tags=tag)
            c.create_rectangle(x1 + 1, y0, x1 + 3, y0 + cover_h, fill=T["ch_b"], outline="",
                               tags=tag)
        image = self.images.get(key)
        if image is not None:
            self.images.move_to_end(key)
            c.create_image((x0 + x1) / 2, y0 + cover_h / 2, image=image, tags=tag)
        else:
            from .playview import draw_note
            draw_note(c, (x0 + x1) / 2, y0 + cover_h / 2, px(36) / 48)
            if key and key not in self.images and key not in self._wanted and self.load:
                self._wanted.add(key)
                self._jobs.put((self._generation, key, i))
                self._ensure_poll()
        c.create_text(x0 + px(2), y0 + cover_h + px(12), text=self.fit(line1, tile_w - px(4), 8, True),
                      anchor="w", fill=T["text"], font=self.app.font(8), tags=tag)
        if line2:
            c.create_text(x0 + px(2), y0 + cover_h + px(30), anchor="w", fill=T["muted"],
                          text=self.fit(line2, tile_w - px(4), 8), font=self.app.font(8, False),
                          tags=tag)

    # covers
    def _work(self):
        while True:
            job = self._jobs.get()
            if job is None:
                return
            generation, key, index = job
            if generation != self._generation:
                self._results.put((generation, key, None))
                continue
            image = None
            try:
                image = self.load(key) if self.load else None
                if image is not None:
                    _tw, cover_h, _th, _gap = self._metrics()
                    image = image.convert('RGB')
                    image.thumbnail((cover_h - 2, cover_h - 2), Image.Resampling.LANCZOS)
            except Exception:      # noqa: BLE001
                image = None
            self._results.put((generation, key, image))

    def _ensure_poll(self):
        if self._poll_id is None:
            self._poll_id = self.after(60, self._poll)

    def _poll(self):
        self._poll_id = None
        changed = set()
        while True:
            try:
                generation, key, image = self._results.get_nowait()
            except queue.Empty:
                break
            if generation != self._generation:
                continue                     # asked for by a list that's gone (the new list
                                             # may want the same cover: leave it wanted)
            self._wanted.discard(key)
            self.images[key] = ImageTk.PhotoImage(image) if image is not None else None
            changed.add(key)
        while len(self.images) > self.MAX_IMAGES:
            self.images.popitem(last=False)
        if changed:
            first, last = self._drawn
            self._redraw_rows([i for i in range(first, last + 1)
                               if i < self.count and self.tiles[i][0] in changed])
        if self._wanted:
            self._poll_id = self.after(60, self._poll)      # only while covers are on the way

    def destroy(self):
        self._generation += 1
        self._jobs.put(None)
        if self._poll_id:
            try:
                self.after_cancel(self._poll_id)
            except tk.TclError:
                pass
        super().destroy()
