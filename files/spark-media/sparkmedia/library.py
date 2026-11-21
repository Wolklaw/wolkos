"""The start screen: Pictures, Videos and Music as browsable folders, with thumbnails. (Music
opens on the library by artist and album, musicview.py; this is its "by folder" view.)"""

import os
import queue
import threading
from typing import Dict, List, Optional, Tuple

import tkinter as tk

from PIL import Image, ImageTk

from . import player as gst
from .core import folder_items, log, photo_thumbnail, pick_files, pick_folder, user_dir
from .listviews import ScrollBar
from .text import tr
from .widgets import T, IconButton, Segmented, WolkButton

MAX_THUMBS = 3000


def kind_words(kind: str) -> Tuple[str, str]:
    """('PHOTOS', 'photos') in the current language."""
    return tr(f'kind.{kind}'), tr(f'kind.{kind}.files')


class HomeView(tk.Frame):
    """A folder of one kind of media, as tiles (photos, videos) or rows (music)."""

    def __init__(self, app, kind: str, folder: Optional[str] = None):
        super().__init__(app.body, bg=T["bg"])
        self.app, self.kind = app, kind
        self.folder = folder or app.last_folder.get(kind) or user_dir(kind)
        if not os.path.isdir(self.folder):
            self.folder = user_dir(kind)
        px, font = app.px, app.font

        bar = tk.Frame(self, bg=T["bg"])
        bar.pack(fill="x", padx=px(16), pady=(px(12), px(8)))
        self.switch = Segmented(bar, [(kind_words(k)[0], k) for k in ('photo', 'video', 'music')],
                                kind, self._switch_kind, font(8))
        self.switch.pack(side="left")
        WolkButton(bar, tr('btn.open_folder'), self._open_folder, font(8), padx=px(10),
                   pady=px(3)).pack(side="right")
        WolkButton(bar, tr('btn.open_files'), self._open_files, font(8), padx=px(10),
                   pady=px(3)).pack(side="right", padx=(0, px(6)))
        if kind == 'music':
            WolkButton(bar, tr('btn.library'), self._library, font(8), padx=px(10),
                       pady=px(3)).pack(side="right", padx=(0, px(6)))
        self.play_all = None
        if kind in ('music', 'photo'):
            self.play_all = WolkButton(bar, tr('btn.play_all') if kind == 'music'
                                       else tr('btn.slideshow'),
                                       self._play_all, font(8), variant="primary",
                                       padx=px(10), pady=px(3))
            self.play_all.pack(side="right", padx=(0, px(6)))

        where = tk.Frame(self, bg=T["bg"])
        where.pack(fill="x", padx=px(16), pady=(0, px(8)))
        self.up = IconButton(where, "back", self._go_up, px(22))
        self.up.pack(side="left")
        self.path_label = tk.Label(where, text="", bg=T["bg"], fg=T["muted"], font=font(8, False),
                                   anchor="w")
        self.path_label.pack(side="left", fill="x", expand=True, padx=(px(6), 0))
        self.count_label = tk.Label(where, text="", bg=T["bg"], fg=T["dim"], font=font(7))
        self.count_label.pack(side="right")

        tk.Frame(self, bg=T["line"], height=1).pack(fill="x", padx=px(16))
        self.now_bar = None
        if kind == 'music':
            from .musicview import NowPlayingBar
            self.now_bar = NowPlayingBar(self, app)
        holder = tk.Frame(self, bg=T["bg"])
        holder.pack(fill="both", expand=True, padx=(px(16), px(4)), pady=(px(10), px(8)))
        self.canvas = tk.Canvas(holder, bg=T["bg"], highlightthickness=0, bd=0, takefocus=1)
        self.scroll = ScrollBar(holder, self.canvas, px(6))
        self.scroll.pack(side="right", fill="y", padx=(px(6), 0))
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.canvas.bind("<Configure>", lambda _e: self._layout())
        self.canvas.bind("<Motion>", self._motion)
        self.canvas.bind("<Leave>", lambda _e: self._set_hover(None))
        self.canvas.bind("<ButtonRelease-1>", self._click)
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.bind(seq, self._wheel)

        self.entries: List[Tuple[str, str]] = []        # (kind: 'dir' or media kind, path)
        self.boxes: List[Tuple[int, int, int, int]] = []
        self.photos: Dict[int, ImageTk.PhotoImage] = {}
        self.thumbs: Dict[int, Image.Image] = {}
        self.no_preview: set = set()
        self.hover: Optional[int] = None
        self.selected = 0
        self.generation = 0
        self.results: "queue.Queue[Tuple[int, int, Optional[Image.Image]]]" = queue.Queue()
        self.jobs: "queue.Queue[Optional[Tuple[int, int, str, str]]]" = queue.Queue()
        self.pending = 0                  # thumbnails asked for and not back yet
        self.worker = threading.Thread(target=self._thumb_worker, daemon=True)
        self.worker.start()
        self._poll_id: Optional[str] = None
        self.load(self.folder)

    # ── data ──
    def load(self, folder: str):
        self.folder = folder
        self.app.last_folder[self.kind] = folder
        self.generation += 1
        self.photos.clear()
        self.thumbs.clear()
        self.no_preview.clear()
        dirs, files = folder_items(folder, self.kind)
        self.entries = [('dir', d) for d in dirs] + [(self.kind, f) for f in files]
        self.files = files
        self.selected = 0
        self.hover = None
        home = os.path.expanduser('~')
        shown = folder.replace(home, '~', 1) if folder.startswith(home) else folder
        self.path_label.config(text=shown)
        self.up.set_enabled(os.path.dirname(folder) != folder)
        word = kind_words(self.kind)[1]
        self.count_label.config(text=f"{len(files)} {word.upper()}" if files else "")
        if self.play_all is not None:
            self.play_all.set_enabled(bool(files))
        self.pending = 0
        if self.kind != 'music':
            for i, (kind, path) in enumerate(self.entries[:MAX_THUMBS]):
                if kind != 'dir':
                    self.jobs.put((self.generation, i, kind, path))
                    self.pending += 1
        if self.pending and self._poll_id is None:
            self._poll_id = self.after(80, self._poll)
        self.canvas.yview_moveto(0)
        self._layout()
        self.app.set_window_title(None)

    def _thumb_worker(self):
        while True:
            job = self.jobs.get()
            if job is None:                      # the view closed
                return
            generation, index, kind, path = job
            if generation != self.generation:
                continue
            try:
                if kind == 'photo':
                    image = photo_thumbnail(path)
                elif kind == 'video' and gst.available():
                    image = gst.video_thumbnail(path)
                else:
                    image = None
            except Exception:      # noqa: BLE001 - one bad file must not stop the rest
                log.exception("thumbnail failed for %s", path)
                image = None
            self.results.put((generation, index, image))

    def _poll(self):
        self._poll_id = None
        try:
            while True:
                generation, index, image = self.results.get_nowait()
                if generation != self.generation:
                    continue
                self.pending -= 1
                if image is None:
                    self.no_preview.add(index)
                else:
                    self.thumbs[index] = image
                self._draw_thumb(index)
        except queue.Empty:
            pass
        if self.pending > 0:                     # nothing runs once every thumbnail is in
            self._poll_id = self.after(80, self._poll)

    def destroy(self):
        self.generation += 1
        self.jobs.put(None)
        if self._poll_id:
            try:
                self.after_cancel(self._poll_id)
            except (tk.TclError, ValueError):
                pass
        super().destroy()

    # ── layout and drawing ──
    def _metrics(self):
        px = self.app.px
        if self.kind == 'music':
            return None
        tile_w, thumb_h, label_h, gap = px(184), px(138), px(24), px(12)
        return tile_w, thumb_h, label_h, gap

    def _layout(self):
        c, px = self.canvas, self.app.px
        c.delete("all")
        self.boxes = []
        width = max(1, c.winfo_width())
        if not self.entries:
            what = kind_words(self.kind)[1]
            c.create_text(width / 2, px(70), text=tr('home.none_here', what=what.upper()),
                          fill=T["muted"], font=self.app.font(10))
            c.create_text(width / 2, px(96), fill=T["dim"], font=self.app.font(8, False),
                          text=tr('home.none_hint'))
            c.configure(scrollregion=(0, 0, width, c.winfo_height()))
            return
        if self.kind == 'music':
            row_h = px(34)
            for i in range(len(self.entries)):
                self.boxes.append((0, i * row_h, width - 1, (i + 1) * row_h - 1))
        else:
            tile_w, thumb_h, label_h, gap = self._metrics()
            cols = max(1, (width + gap) // (tile_w + gap))
            for i in range(len(self.entries)):
                r, col = divmod(i, cols)
                x0, y0 = col * (tile_w + gap), r * (thumb_h + label_h + gap)
                self.boxes.append((x0, y0, x0 + tile_w, y0 + thumb_h + label_h))
        for i in range(len(self.entries)):
            self._draw_entry(i)
        bottom = self.boxes[-1][3] + px(12)
        c.configure(scrollregion=(0, 0, width, max(bottom, c.winfo_height())))

    def _draw_entry(self, i: int):
        c, px = self.canvas, self.app.px
        tag = f"e{i}"
        c.delete(tag)
        kind, path = self.entries[i]
        x0, y0, x1, y1 = self.boxes[i]
        hot = i == self.hover or (i == self.selected and c.focus_get() is c)
        name = os.path.basename(path) or path
        if self.kind == 'music':
            if hot:
                c.create_rectangle(x0, y0, x1, y1, fill=T["hover"], outline="", tags=tag)
                c.create_rectangle(x0, y0, x0 + 2, y1, fill=T["ch_a"], outline="", tags=tag)
                c.create_rectangle(x0 + 2, y0, x0 + 4, y1, fill=T["ch_b"], outline="", tags=tag)
            c.create_line(x0, y1, x1, y1, fill=T["line"], tags=tag)
            mid = (y0 + y1) / 2
            label = self._fit(name + ("/" if kind == 'dir' else ""), x1 - x0 - px(90), 9)
            c.create_text(x0 + px(12), mid, text=label, anchor="w", tags=tag,
                          fill=T["text"] if kind != 'dir' else T["accent"],
                          font=self.app.font(9, kind == 'dir'))
            tagtext = tr('home.folder') if kind == 'dir' else os.path.splitext(name)[1][1:].upper()
            c.create_text(x1 - px(10), mid, text=tagtext, anchor="e", fill=T["dim"],
                          font=self.app.font(7), tags=tag)
            return
        tile_w, thumb_h, label_h, _gap = self._metrics()
        c.create_rectangle(x0, y0, x1, y0 + thumb_h, fill=T["tile"],
                           outline=T["accent"] if hot else T["line"], tags=tag)
        if hot:
            c.create_rectangle(x0 - 3, y0, x0 - 1, y0 + thumb_h, fill=T["ch_a"], outline="",
                               tags=tag)
            c.create_rectangle(x1 + 1, y0, x1 + 3, y0 + thumb_h, fill=T["ch_b"], outline="",
                               tags=tag)
        if kind == 'dir':
            cx, cy = (x0 + x1) / 2, y0 + thumb_h / 2
            w, h = px(44), px(32)
            c.create_line(cx - w / 2, cy + h / 2, cx - w / 2, cy - h / 2, cx - w / 6, cy - h / 2,
                          cx - w / 12, cy - h / 2 + px(5), cx + w / 2, cy - h / 2 + px(5),
                          cx + w / 2, cy + h / 2, cx - w / 2, cy + h / 2,
                          fill=T["accent"] if hot else T["line2"], width=max(2, px(2)), tags=tag)
        elif i in self.thumbs:
            self._place_thumb(i, tag)
        elif i in self.no_preview and kind == 'photo':
            c.create_text((x0 + x1) / 2, y0 + thumb_h / 2, text=tr('home.no_preview'), fill=T["dim"],
                          font=self.app.font(7), tags=tag)
        elif kind == 'video':
            self._play_glyph((x0 + x1) / 2, y0 + thumb_h / 2, px(12), tag)
        label = self._fit(name, tile_w - px(4), 8)
        c.create_text(x0 + px(2), y0 + thumb_h + label_h / 2 + px(2), text=label, anchor="w",
                      fill=T["text"] if kind != 'dir' else T["accent"],
                      font=self.app.font(8, kind == 'dir'), tags=tag)

    def _play_glyph(self, cx, cy, r, tag):
        self.canvas.create_polygon(cx - r * 0.6, cy - r, cx - r * 0.6, cy + r, cx + r, cy,
                                   fill=T["text"], outline=T["bg"], tags=tag)

    def _place_thumb(self, i: int, tag: str):
        tile_w, thumb_h, _label_h, _gap = self._metrics()
        x0, y0, x1, _y1 = self.boxes[i]
        if i not in self.photos:
            image = self.thumbs[i].copy()
            if image.mode == 'RGBA':
                base = Image.new('RGB', image.size, T["tile"])
                base.paste(image, mask=image.getchannel('A'))
                image = base
            image.thumbnail((tile_w - 2, thumb_h - 2), Image.Resampling.LANCZOS)
            self.photos[i] = ImageTk.PhotoImage(image)
        self.canvas.create_image((x0 + x1) / 2, y0 + thumb_h / 2, image=self.photos[i], tags=tag)
        if self.entries[i][0] == 'video':
            self._play_glyph((x0 + x1) / 2, y0 + thumb_h / 2, self.app.px(12), tag)

    def _draw_thumb(self, i: int):
        if i < len(self.boxes):
            self._draw_entry(i)

    def _fit(self, text: str, width: int, size: int) -> str:
        f = self.app.tkfont(size, False)
        if f.measure(text) <= width:
            return text
        while text and f.measure(text + "…") > width:
            text = text[:-1]
        return text + "…"

    # ── interaction ──
    def _index_at(self, x: int, y: int) -> Optional[int]:
        cx, cy = self.canvas.canvasx(x), self.canvas.canvasy(y)
        for i, (x0, y0, x1, y1) in enumerate(self.boxes):
            if x0 <= cx <= x1 and y0 <= cy <= y1:
                return i
        return None

    def _set_hover(self, index: Optional[int]):
        if index == self.hover:
            return
        old, self.hover = self.hover, index
        for i in (old, index):
            if i is not None and i < len(self.boxes):
                self._draw_entry(i)
        self.canvas.configure(cursor="hand2" if index is not None else "")

    def _motion(self, event):
        self._set_hover(self._index_at(event.x, event.y))

    def _click(self, event):
        index = self._index_at(event.x, event.y)
        if index is not None:
            self.activate(index)

    def _wheel(self, event):
        if getattr(event, 'num', None) == 4 or getattr(event, 'delta', 0) > 0:
            self.canvas.yview_scroll(-3, "units")
        else:
            self.canvas.yview_scroll(3, "units")
        self._motion(event)

    def activate(self, index: int):
        kind, path = self.entries[index]
        if kind == 'dir':
            self.load(path)
            self.app.glitch.now(self.app.title)
        else:
            self.app.open_path(path, playlist=self.files)

    def _go_up(self):
        parent = os.path.dirname(self.folder)
        if parent and parent != self.folder:
            child = self.folder
            self.load(parent)
            for i, (_k, p) in enumerate(self.entries):
                if p == child:
                    self._select(i)
                    break

    def _select(self, index: int):
        if not self.entries:
            return
        old = self.selected
        self.selected = max(0, min(len(self.entries) - 1, index))
        self.canvas.focus_set()
        for i in (old, self.selected):
            if i < len(self.boxes):
                self._draw_entry(i)
        x0, y0, x1, y1 = self.boxes[self.selected]
        total = float(self.canvas.cget("scrollregion").split()[3] or 1)
        top, bottom = self.canvas.canvasy(0), self.canvas.canvasy(self.canvas.winfo_height())
        if y0 < top:
            self.canvas.yview_moveto(y0 / total)
        elif y1 > bottom:
            self.canvas.yview_moveto((y1 - self.canvas.winfo_height() + 4) / total)

    def _columns(self) -> int:
        if self.kind == 'music' or len(self.boxes) < 2:
            return 1
        first_y = self.boxes[0][1]
        return sum(1 for b in self.boxes if b[1] == first_y)

    def key(self, event) -> bool:
        k = event.keysym
        cols = self._columns()
        moves = {'Left': -1, 'Right': 1, 'Up': -cols, 'Down': cols}
        if k == 'BackSpace' or (k == 'Up' and event.state & 0x8):      # Alt+Up, as in Dolphin
            self._go_up()
        elif k in moves and self.kind == 'music' and k in ('Left', 'Right'):
            return False
        elif k in moves:
            self._select(self.selected + moves[k])
        elif k in ('Return', 'KP_Enter'):
            if self.entries:
                self.activate(self.selected)
        elif k == 'Home':
            self._select(0)
        elif k == 'End':
            self._select(len(self.entries) - 1)
        elif k in ('Prior', 'Next'):
            self.canvas.yview_scroll(-1 if k == 'Prior' else 1, "pages")
        else:
            return False
        return True

    # ── buttons ──
    def _switch_kind(self, kind: str):
        self.app.show_home(kind)

    def _open_files(self):
        paths = pick_files(self.kind, self.folder)
        if paths:
            self.app.open_path(paths[0], playlist=paths if len(paths) > 1 else None)

    def _open_folder(self):
        folder = pick_folder(self.folder)
        if folder:
            self.load(folder)

    def _library(self):
        self.app.settings['music_mode'] = 'library'
        self.app.show_home('music')

    def _play_all(self):
        if self.files:
            self.app.open_path(self.files[0], playlist=self.files,
                               slideshow=self.kind == 'photo')
