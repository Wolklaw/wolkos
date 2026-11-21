"""The photo viewer: fit, zoom around the pointer, pan, rotate, slideshow, Trash, and simple
edits (rotate, crop, auto-enhance) saved as a new file unless you pick Replace.

Camera RAW photos open on the JPEG the camera stored inside them; when that's smaller than the
photo, the full picture is developed in the background and swapped in."""

import os
import threading
from typing import Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import messagebox

from PIL import Image, ImageTk

from . import edits as ed
from . import raw
from .core import (Photo, PhotoError, clamp_center, fit_scale, flatten, human_size, log,
                   move_to_trash, visible_region)
from .text import tr
from .widgets import T, IconButton, Segmented, Toggle, WolkButton

ZOOM_STEPS = (0.05, 0.1, 0.15, 0.2, 0.25, 0.33, 0.5, 0.67, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0,
              6.0, 8.0, 12.0, 16.0, 24.0, 32.0)
SLIDE_MS = 4000
SHARPEN_MS = 140          # after the view settles, redraw with the slower, sharper filter
CACHE = 5
EDIT_PREVIEW = 2400       # edit mode shows (and enhances) a copy at most this big
HANDLE = 10               # px around the crop's corners and edges that grab them


class PhotoView(tk.Frame):
    kind = 'photo'

    def __init__(self, app, path: str, playlist: List[str], slideshow: bool = False):
        super().__init__(app.body, bg=T["bg"])
        self.app = app
        self.files = playlist
        self.index = playlist.index(path) if path in playlist else 0
        px = app.px
        self.canvas = tk.Canvas(self, bg=T["out_bg"], highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.bar = tk.Frame(self, bg=T["panel"])
        self.bar.pack(fill="x")
        tk.Frame(self.bar, bg=T["line"], height=1).pack(fill="x", side="top")
        inner = tk.Frame(self.bar, bg=T["panel"])
        inner.pack(fill="x", padx=px(10), pady=px(6))
        size = px(30)
        self.buttons: Dict[str, IconButton] = {}
        for name, cmd in (("prev", self.prev), ("next", self.next)):
            self.buttons[name] = IconButton(inner, name, cmd, size, tip=tr(f'tip.{name}'))
            self.buttons[name].pack(side="left")
        self.info = tk.Label(inner, text="", bg=T["panel"], fg=T["text"], font=app.font(8),
                             anchor="w")
        self.info.pack(side="left", padx=(px(10), 0))
        self.facts = tk.Label(inner, text="", bg=T["panel"], fg=T["muted"],
                              font=app.font(7, False), anchor="w")
        self.facts.pack(side="left", padx=(px(10), 0), fill="x", expand=True)
        for name, cmd in (("fullscreen", app.toggle_fullscreen), ("trash", self.trash),
                          ("slideshow", self.toggle_slideshow), ("edit", self.start_edit),
                          ("rotate", self.rotate), ("zoom_in", lambda: self.zoom_step(1)),
                          ("fit", self.fit), ("zoom_out", lambda: self.zoom_step(-1))):
            self.buttons[name] = IconButton(inner, name, cmd, size, tip=tr(f'tip.{name}'))
            self.buttons[name].pack(side="right")
        self.zoom_label = tk.Label(inner, text="", bg=T["panel"], fg=T["accent"],
                                   font=app.font(8), width=6, anchor="e")
        self.zoom_label.pack(side="right", padx=(0, px(6)))
        self._build_edit_bar()

        self.photo: Optional[Photo] = None
        self.error: Optional[str] = None
        self.cache: Dict[str, Photo] = {}
        self._ready: Dict[str, Photo] = {}        # preloaded by a worker thread
        self.rotation = 0
        self.scale = 1.0
        self.fitted = True
        self.center = (0.0, 0.0)
        self.frame_index = 0
        self.tkimage: Optional[ImageTk.PhotoImage] = None
        self._sharpen_id = self._anim_id = self._slide_id = self._decode_id = None
        self._drag: Optional[Tuple[int, int, Tuple[float, float]]] = None
        self._rotated: Dict[int, Image.Image] = {}
        self._decoded: Dict[str, Optional[Image.Image]] = {}     # filled by the decode thread
        self._decoding: Optional[str] = None
        self._decode_thread: Optional[threading.Thread] = None
        self._decode_next: Optional[str] = None
        # edit mode
        self.editing = False
        self.edits = ed.Edits()
        self.ratio = 'free'
        self._edit_cache: Optional[Tuple[Tuple, Image.Image, float]] = None
        self._edit_view: Optional[Tuple[Tuple, Image.Image, Image.Image]] = None
        self._edit_map = (0.0, 0.0, 1.0)          # view x/y offset, view px per photo px
        self._crop_drag: Optional[Tuple[str, Tuple[float, float], ed.Box]] = None

        c = self.canvas
        c.bind("<Configure>", lambda _e: self.render())
        c.bind("<ButtonPress-1>", self._press)
        c.bind("<B1-Motion>", self._move)
        c.bind("<ButtonRelease-1>", lambda _e: self._end_drag())
        c.bind("<Double-Button-1>", self._double)
        c.bind("<Button-3>", lambda _e: None if self.editing else self.toggle_actual())
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            c.bind(seq, self._wheel)
        self.show(self.index)
        if slideshow:
            self.toggle_slideshow()

    # ── loading ──
    def _load(self, path: str) -> Photo:
        photo = self.cache.get(path)
        if photo is None:
            photo = self._ready.pop(path, None) or Photo(path)    # preloaded, or open it now
            self.cache[path] = photo
            while len(self.cache) > CACHE:
                self.cache.pop(next(iter(self.cache)))
        return photo

    def _preload(self, index: int):
        if not (0 <= index < len(self.files)):
            return
        if self.files[index] in self.cache or self.files[index] in self._ready:
            return
        path = self.files[index]

        def work():
            # no Tk calls here: the photo is only handed over, and _load picks it up
            try:
                self._ready[path] = Photo(path)
            except PhotoError:
                pass

        threading.Thread(target=work, daemon=True).start()

    def show(self, index: int):
        if not self.files:
            return
        if self.editing:
            self._leave_edit()
        self.index = index % len(self.files)
        path = self.files[self.index]
        self._stop_animation()
        self.rotation, self.fitted, self.frame_index = 0, True, 0
        self._rotated.clear()
        try:
            self.photo = self._load(path)
            self.error = None
        except PhotoError as exc:
            self.photo, self.error = None, str(exc)
            log.warning("cannot show %s: %s", path, exc)
            self.app.glitch.now(self.app.title, strong=True)
        self.app.set_window_title(os.path.basename(path))
        self._update_info()
        self.render()
        if self.photo is not None and self.photo.animated:
            self._anim_id = self.after(self.photo.durations[0], self._animate)
        if self.photo is not None and self.photo.needs_decode:
            self._start_decode(path)
        # preloaded photos that are no longer next to this one: let them go (each is a whole
        # decoded picture)
        near = {self.files[(self.index + d) % len(self.files)] for d in (-1, 0, 1)}
        for stale in [p for p in list(self._ready) if p not in near]:
            self._ready.pop(stale, None)
        many = len(self.files) > 1
        self.buttons["prev"].set_enabled(many)
        self.buttons["next"].set_enabled(many)
        self.buttons["edit"].set_enabled(self.photo is not None and not self.photo.animated)
        self._preload(self.index + 1)
        self._preload(self.index - 1)

    # ── RAW: the full picture, developed in the background ──
    def _start_decode(self, path: str):
        """One LibRaw decode at a time (each holds the whole photo in memory): flipping through
        a folder of RAWs develops the one you stop on, not every one you passed."""
        busy = self._decode_thread is not None and self._decode_thread.is_alive()
        if busy:
            if self._decoding != path:
                self._decode_next = path
        else:
            self._decoding, self._decode_next = path, None

            def work():
                self._decoded[path] = raw.decode(path)

            self._decode_thread = threading.Thread(target=work, daemon=True, name='raw-decode')
            self._decode_thread.start()
        if self._decode_id is None:
            self._decode_id = self.after(200, self._decode_poll)

    def _decode_poll(self):
        self._decode_id = None
        if self._decode_thread is not None and self._decode_thread.is_alive():
            self._decode_id = self.after(200, self._decode_poll)      # only while developing
            return
        path, self._decoding, self._decode_thread = self._decoding, None, None
        image = self._decoded.pop(path, None) if path else None
        self._decoded.clear()
        photo = self.cache.get(path) if path else None
        if photo is not None and photo.needs_decode:
            if image is None:
                photo.needs_decode = False          # keep the preview; the log says why
                log.info("keeping the camera preview of %s", path)
            else:
                if self.photo is photo:
                    self._rescale_crop(image.width / max(1, photo.size[0]))
                photo.use_decoded(image)
            if self.photo is photo:
                self._rotated.clear()
                self._edit_cache = None
                self._update_info()
                self.render()
        wanted, self._decode_next = self._decode_next, None
        if wanted and self.photo is not None and self.photo.path == wanted \
                and self.photo.needs_decode:
            self._start_decode(wanted)

    def _update_info(self):
        path = self.files[self.index]
        count = f"  {self.index + 1} / {len(self.files)}" if len(self.files) > 1 else ""
        self.info.config(text=os.path.basename(path) + count)
        if self.photo is None:
            self.facts.config(text=tr('photo.unreadable'), fg=T["pink"])
            return
        w, h = self._image().size if not self.editing else \
            ed.rotated_size(self.photo.size, self.edits.rotation)
        parts = [f"{w} × {h}", self.photo.format, human_size(self.photo.bytes)]
        if self.photo.camera:
            parts.append(self.photo.camera)
        if self.photo.taken:
            parts.append(self.photo.taken)
        if self.photo.needs_decode:
            parts.append(tr('photo.raw_preview'))
        elif self.photo.developed:
            parts.append(tr('photo.raw_developed'))
        if self.photo.animated:
            parts.append(tr('photo.frames', n=len(self.photo.frames)))
        if self.rotation and not self.editing:
            parts.append(tr('photo.rotated_view'))
        self.facts.config(text="  ·  ".join(p for p in parts if p), fg=T["muted"])

    def _image(self) -> Image.Image:
        if self.editing:
            return self._edit_image()[0]
        base = self.photo.frames[self.frame_index] if self.photo.animated else self.photo.image
        if not self.rotation:
            return base
        if self.photo.animated:
            return base.rotate(-self.rotation, expand=True)
        if self.rotation not in self._rotated:
            self._rotated[self.rotation] = base.rotate(-self.rotation, expand=True)
        return self._rotated[self.rotation]

    # ── drawing ──
    def _view(self) -> Tuple[int, int]:
        return max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())

    def render(self, quick: bool = False):
        c = self.canvas
        c.delete("all")
        vw, vh = self._view()
        if self.photo is None:
            if self.error:
                c.create_text(vw / 2, vh / 2 - self.app.px(12), text=tr('photo.cant_show'),
                              fill=T["pink"], font=self.app.font(11))
                c.create_text(vw / 2, vh / 2 + self.app.px(14), text=self.error,
                              fill=T["muted"], font=self.app.font(8, False))
            return
        if self.editing:
            self._render_edit()
            return
        image = self._image()
        if self.fitted:
            self.scale = fit_scale(image.size, (vw, vh))
            self.center = (image.width / 2, image.height / 2)
        self.center = clamp_center(image.size, (vw, vh), self.scale, self.center)
        box, (ox, oy) = visible_region(image.size, (vw, vh), self.scale, self.center)
        crop = image.crop(box)
        target = (max(1, round(crop.width * self.scale)), max(1, round(crop.height * self.scale)))
        if target != crop.size:
            if quick or self.scale > 1.0:
                # nearest neighbour above 100% keeps pixels sharp, like every viewer
                method = Image.Resampling.NEAREST if self.scale >= 2 else Image.Resampling.BILINEAR
                crop = crop.resize(target, method)
            else:
                crop = crop.resize(target, Image.Resampling.LANCZOS, reducing_gap=3.0)
        self.tkimage = ImageTk.PhotoImage(flatten(crop, T["out_bg"]))
        c.create_image(ox, oy, image=self.tkimage, anchor="nw")
        self.zoom_label.config(text=f"{self.scale * 100:.0f}%")
        if quick:
            if self._sharpen_id:
                self.after_cancel(self._sharpen_id)
            self._sharpen_id = self.after(SHARPEN_MS, self._sharpen)

    def _render_edit(self):
        """Edit mode: the whole photo with room around it for the crop handles; outside the
        crop it's darkened (in the picture itself, so it looks the same on every display)."""
        c = self.canvas
        c.delete("all")
        vw, vh = self._view()
        image, k = self._edit_image()
        pad = self.app.px(24)
        scale = fit_scale(image.size, (max(1, vw - 2 * pad), max(1, vh - 2 * pad)), upscale=True)
        size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
        key = (self._edit_cache[0], size)
        if self._edit_view is None or self._edit_view[0] != key:
            shown = flatten(image.resize(size, Image.Resampling.LANCZOS), T["out_bg"])
            self._edit_view = (key, shown, shown.point(lambda v: int(v * 0.35)))
        _key, shown, dark = self._edit_view
        ox, oy = (vw - size[0]) // 2, (vh - size[1]) // 2
        self.scale = scale
        self._edit_map = (float(ox), float(oy), scale * k)
        picture = shown
        if self.edits.crop:
            s = scale * k
            x0, y0, x1, y1 = self.edits.crop
            box = (max(0, round(x0 * s)), max(0, round(y0 * s)),
                   min(size[0], round(x1 * s)), min(size[1], round(y1 * s)))
            picture = dark.copy()
            if box[2] > box[0] and box[3] > box[1]:
                picture.paste(shown.crop(box), box[:2])
        self.tkimage = ImageTk.PhotoImage(picture)
        c.create_image(ox, oy, image=self.tkimage, anchor="nw")
        self.zoom_label.config(text="")
        self._draw_crop()

    def _sharpen(self):
        self._sharpen_id = None
        self.render()

    def _animate(self):
        if self.photo is None or not self.photo.animated:
            return
        self.frame_index = (self.frame_index + 1) % len(self.photo.frames)
        self.render(quick=True)
        self._anim_id = self.after(self.photo.durations[self.frame_index], self._animate)

    def _stop_animation(self):
        if self._anim_id:
            self.after_cancel(self._anim_id)
            self._anim_id = None

    # ── zoom and pan ──
    def zoom_to(self, scale: float, anchor: Optional[Tuple[int, int]] = None):
        if self.photo is None or self.editing:
            return
        scale = max(ZOOM_STEPS[0], min(ZOOM_STEPS[-1], scale))
        vw, vh = self._view()
        ax, ay = anchor if anchor else (vw / 2, vh / 2)
        # keep the image point under the anchor where it is
        ix = self.center[0] + (ax - vw / 2) / self.scale
        iy = self.center[1] + (ay - vh / 2) / self.scale
        self.scale, self.fitted = scale, False
        self.center = (ix - (ax - vw / 2) / scale, iy - (ay - vh / 2) / scale)
        self.render(quick=True)

    def zoom_step(self, direction: int, anchor: Optional[Tuple[int, int]] = None):
        if direction > 0:
            nxt = next((z for z in ZOOM_STEPS if z > self.scale * 1.01), ZOOM_STEPS[-1])
        else:
            nxt = next((z for z in reversed(ZOOM_STEPS) if z < self.scale * 0.99), ZOOM_STEPS[0])
        self.zoom_to(nxt, anchor)

    def fit(self):
        self.fitted = True
        self.render()

    def toggle_actual(self, anchor: Optional[Tuple[int, int]] = None):
        if self.fitted or abs(self.scale - 1.0) > 0.001:
            self.zoom_to(1.0, anchor)
        else:
            self.fit()

    def _wheel(self, event):
        if self.editing:
            return
        up = getattr(event, 'num', None) == 4 or getattr(event, 'delta', 0) > 0
        if event.state & 0x4 or not len(self.files) > 1 or not self.fitted:
            self.zoom_step(1 if up else -1, (event.x, event.y))      # Ctrl+wheel, or zoomed
        else:
            self.prev() if up else self.next()

    def _double(self, _event):
        if not self.editing:
            self.app.toggle_fullscreen()

    def _press(self, event):
        self.canvas.focus_set()
        if self.editing:
            self._crop_press(event)
            return
        self._drag = (event.x, event.y, self.center)
        self.canvas.configure(cursor="fleur")

    def _move(self, event):
        if self.editing:
            self._crop_move(event)
            return
        if self._drag is None or self.fitted:
            return
        x, y, (cx, cy) = self._drag
        self.center = (cx - (event.x - x) / self.scale, cy - (event.y - y) / self.scale)
        self.render(quick=True)

    def _end_drag(self):
        if self.editing:
            self._crop_drag = None
            self._render_edit()
            return
        self._drag = None
        self.canvas.configure(cursor="")

    # ── actions ──
    def next(self):
        self.show(self.index + 1)

    def prev(self):
        self.show(self.index - 1)

    def rotate(self):
        if self.photo is None:
            return
        if self.editing:
            self.edit_rotate(True)
            return
        self.rotation = (self.rotation + 90) % 360
        self.fitted = True
        self._update_info()
        self.render()

    def toggle_slideshow(self):
        if self._slide_id:
            self.after_cancel(self._slide_id)
            self._slide_id = None
            self.buttons["slideshow"].set_icon("slideshow")
            self.app.set_status(tr('status.slideshow_off'), T["muted"])
        elif len(self.files) > 1 and not self.editing:
            self._slide_id = self.after(SLIDE_MS, self._slide)
            self.buttons["slideshow"].set_icon("pause")
            self.app.set_status(tr('status.slideshow'), T["accent"])

    def _slide(self):
        self.next()
        self._slide_id = self.after(SLIDE_MS, self._slide)

    def trash(self):
        if not self.files or self.editing:
            return
        path = self.files[self.index]
        if not messagebox.askyesno(tr('trash.title'),
                                   tr('trash.ask', name=os.path.basename(path)), parent=self):
            return
        problem = move_to_trash(path)
        if problem:
            self.app.set_status(tr('status.trash_failed'), T["pink"])
            self.app.glitch.now(self.app.title, strong=True)
            messagebox.showerror(tr('trash.title'), problem, parent=self)
            return
        log.info("moved to trash: %s", path)
        self.cache.pop(path, None)
        del self.files[self.index]
        self.app.set_status(tr('status.trashed'), T["accent"])
        if not self.files:
            self.app.show_home('photo')
        else:
            self.show(min(self.index, len(self.files) - 1))

    # ── editing ──
    def _build_edit_bar(self):
        app, px = self.app, self.app.px
        self.edit_bar = tk.Frame(self, bg=T["panel"])
        tk.Frame(self.edit_bar, bg=T["line"], height=1).pack(fill="x", side="top")
        row1 = tk.Frame(self.edit_bar, bg=T["panel"])
        row1.pack(fill="x", padx=px(10), pady=(px(6), 0))
        IconButton(row1, "rotate_ccw", lambda: self.edit_rotate(False), px(30),
                   tip=tr('tip.rotate_left')).pack(side="left")
        IconButton(row1, "rotate", lambda: self.edit_rotate(True), px(30),
                   tip=tr('tip.rotate_right')).pack(side="left", padx=(0, px(12)))
        tk.Label(row1, text=tr('edit.crop'), bg=T["panel"], fg=T["dim"],
                 font=app.font(7)).pack(side="left", padx=(0, px(6)))
        self.ratio_switch = Segmented(row1, [(tr(f'crop.{k}') if k in ('free', 'original') else k, k)
                                             for k, _w, _h in ed.RATIOS], 'free',
                                      self.set_ratio, app.font(8))
        self.ratio_switch.pack(side="left")
        self.enhance_toggle = Toggle(row1, tr('edit.enhance'), False, self.set_enhance,
                                     app.font(8))
        self.enhance_toggle.pack(side="left", padx=(px(12), 0))
        row2 = tk.Frame(self.edit_bar, bg=T["panel"])
        row2.pack(fill="x", padx=px(10), pady=px(6))
        self.edit_hint = tk.Label(row2, text=tr('edit.hint'), bg=T["panel"], fg=T["muted"],
                                  font=app.font(7, False), anchor="w")
        self.edit_hint.pack(side="left", fill="x", expand=True)
        self.save_button = WolkButton(row2, tr('edit.save_copy'), lambda: self.save_edit(False),
                                      app.font(8), variant="primary", padx=px(10), pady=px(3))
        self.save_button.pack(side="right")
        self.replace_button = WolkButton(row2, tr('edit.replace'), lambda: self.save_edit(True),
                                         app.font(8), variant="danger", padx=px(10), pady=px(3))
        self.replace_button.pack(side="right", padx=(0, px(6)))
        WolkButton(row2, tr('edit.cancel'), self.cancel_edit, app.font(8), padx=px(10),
                   pady=px(3)).pack(side="right", padx=(0, px(6)))
        WolkButton(row2, tr('edit.reset'), self.reset_edit, app.font(8), padx=px(10),
                   pady=px(3)).pack(side="right", padx=(0, px(6)))

    def start_edit(self):
        if self.photo is None or self.photo.animated or self.editing:
            return
        if self._slide_id:
            self.toggle_slideshow()
        self.editing = True
        self.edits = ed.Edits()
        self.edits.rotation = self.rotation          # what was turned in view carries over
        self.rotation = 0
        self._rotated.clear()
        self.ratio = 'free'
        self.ratio_switch.set('free')
        self.enhance_toggle.set(False)
        self._edit_cache = None
        self.bar.pack_forget()
        if not self.app.fullscreen:
            self.edit_bar.pack(fill="x")
        replace_ok = ed.can_replace(self.photo.format, self.files[self.index])
        self.replace_button.set_enabled(replace_ok)
        self._paint_edit_state()
        self._update_info()
        self.render()
        self.app.set_status(tr('status.editing'), T["accent"])

    def _leave_edit(self):
        self.editing = False
        self._crop_drag = None
        self._edit_cache = None
        self.edit_bar.pack_forget()
        if not self.app.fullscreen:
            self.bar.pack(fill="x")
        self.canvas.configure(cursor="")

    def cancel_edit(self) -> bool:
        if not self.editing:
            return False
        self._leave_edit()
        self._update_info()
        self.render()
        self.app.set_status(tr('status.edit_cancelled'), T["muted"])
        return True

    def reset_edit(self):
        self.edits = ed.Edits()
        self.ratio = 'free'
        self.ratio_switch.set('free')
        self.enhance_toggle.set(False)
        self._edit_cache = None
        self._paint_edit_state()
        self._update_info()
        self.render()

    def _rescale_crop(self, k: float):
        """The developed RAW replaced a preview of another size: keep the same area."""
        if self.edits.crop and abs(k - 1) > 0.001:
            size = ed.rotated_size((round(self.photo.size[0] * k), round(self.photo.size[1] * k)),
                                   self.edits.rotation)
            self.edits.crop = ed.clamp_box(tuple(v * k for v in self.edits.crop), size)

    def _full_size(self) -> Tuple[int, int]:
        """The photo's size after the edit's rotation, before the crop."""
        return ed.rotated_size(self.photo.size, self.edits.rotation)

    def _edit_image(self) -> Tuple[Image.Image, float]:
        """(what edit mode shows: a copy at most EDIT_PREVIEW px, rotated and enhanced; its
        scale to the full photo)."""
        key = (self.edits.rotation, self.edits.enhance, id(self.photo.image))
        if self._edit_cache is None or self._edit_cache[0] != key:
            base = self.photo.image
            k = min(1.0, EDIT_PREVIEW / max(base.size))
            if k < 1.0:
                base = base.resize((max(1, round(base.width * k)), max(1, round(base.height * k))),
                                   Image.Resampling.BILINEAR)
            shown = ed.rotate(base, self.edits.rotation)
            if self.edits.enhance:
                shown = ed.enhance(shown)
            self._edit_cache = (key, shown, k)
        return self._edit_cache[1], self._edit_cache[2]

    def edit_rotate(self, clockwise: bool):
        if not self.editing:
            self.start_edit()
        self.edits.rotate(self.photo.size, clockwise)
        if self.edits.crop and self.ratio not in ('free',):
            self.edits.crop = ed.fit_ratio(self.edits.crop, ed.ratio_for(self.ratio, self._full_size()),
                                           self._full_size())
        self._paint_edit_state()
        self._update_info()
        self.render()

    def set_ratio(self, key: str):
        self.ratio = key
        size = self._full_size()
        ratio = ed.ratio_for(key, size)
        if ratio > 0:
            self.edits.crop = ed.fit_ratio(self.edits.crop, ratio, size) if self.edits.crop \
                else ed.largest_box(size, ratio)
        self._paint_edit_state()
        self.render()

    def set_enhance(self, on: bool):
        self.edits.enhance = on
        self._paint_edit_state()
        self.render()

    def _paint_edit_state(self):
        changed = self.edits.changed
        self.save_button.set_enabled(changed)
        self.replace_button.set_enabled(changed and ed.can_replace(self.photo.format,
                                                                   self.files[self.index]))
        if self.edits.crop:
            x0, y0, x1, y1 = self.edits.crop
            self.edit_hint.config(text=tr('edit.crop_size', w=x1 - x0, h=y1 - y0))
        else:
            self.edit_hint.config(text=tr('edit.hint'))

    # crop box on screen
    def _to_view(self, x: float, y: float) -> Tuple[float, float]:
        ox, oy, s = self._edit_map
        return ox + x * s, oy + y * s

    def _to_photo(self, vx: float, vy: float) -> Tuple[float, float]:
        ox, oy, s = self._edit_map
        return (vx - ox) / s, (vy - oy) / s

    def _draw_crop(self):
        c = self.canvas
        c.delete("crop")
        size = self._full_size()
        box = self.edits.crop
        x0, y0 = self._to_view(0, 0)
        x1, y1 = self._to_view(*size)
        if box is None:
            c.create_rectangle(x0, y0, x1, y1, outline=T["line2"], tags="crop")
            return
        bx0, by0 = self._to_view(box[0], box[1])
        bx1, by1 = self._to_view(box[2], box[3])
        if self._crop_drag is not None:
            for i in (1, 2):
                gx = bx0 + (bx1 - bx0) * i / 3
                gy = by0 + (by1 - by0) * i / 3
                c.create_line(gx, by0, gx, by1, fill=T["line2"], tags="crop")
                c.create_line(bx0, gy, bx1, gy, fill=T["line2"], tags="crop")
        c.create_rectangle(bx0 - 1, by0, bx1 - 1, by1, outline=T["ch_a"], tags="crop")
        c.create_rectangle(bx0 + 1, by0, bx1 + 1, by1, outline=T["ch_b"], tags="crop")
        c.create_rectangle(bx0, by0, bx1, by1, outline=T["accent"], width=2, tags="crop")
        h = self.app.px(4)
        for hx, hy in ((bx0, by0), (bx1, by0), (bx0, by1), (bx1, by1)):
            c.create_rectangle(hx - h, hy - h, hx + h, hy + h, fill=T["accent"], outline="",
                               tags="crop")

    def _handle_at(self, vx: float, vy: float) -> str:
        box = self.edits.crop
        if box is None:
            return 'new'
        bx0, by0 = self._to_view(box[0], box[1])
        bx1, by1 = self._to_view(box[2], box[3])
        r = self.app.px(HANDLE)
        near_l, near_r = abs(vx - bx0) <= r, abs(vx - bx1) <= r
        near_t, near_b = abs(vy - by0) <= r, abs(vy - by1) <= r
        inside_x, inside_y = bx0 - r <= vx <= bx1 + r, by0 - r <= vy <= by1 + r
        v = 'n' if near_t and inside_x else 's' if near_b and inside_x else ''
        hz = 'w' if near_l and inside_y else 'e' if near_r and inside_y else ''
        if v or hz:
            return v + hz
        if bx0 < vx < bx1 and by0 < vy < by1:
            return 'move'
        return 'new'

    def _crop_press(self, event):
        handle = self._handle_at(event.x, event.y)
        size = self._full_size()
        start = self._to_photo(event.x, event.y)
        start = (max(0.0, min(size[0], start[0])), max(0.0, min(size[1], start[1])))
        box = self.edits.crop if handle != 'new' else \
            (int(start[0]), int(start[1]), int(start[0]), int(start[1]))
        self._crop_drag = (handle, start, box)

    def _crop_move(self, event):
        if self._crop_drag is None:
            cursor = {'move': 'fleur', 'n': 'top_side', 's': 'bottom_side', 'e': 'right_side',
                      'w': 'left_side', 'nw': 'top_left_corner', 'ne': 'top_right_corner',
                      'sw': 'bottom_left_corner', 'se': 'bottom_right_corner'}
            self.canvas.configure(cursor=cursor.get(self._handle_at(event.x, event.y), 'crosshair'))
            return
        handle, start, box = self._crop_drag
        x, y = self._to_photo(event.x, event.y)
        dx, dy = x - start[0], y - start[1]
        size = self._full_size()
        ratio = ed.ratio_for(self.ratio, size)
        if handle == 'new':
            if abs(dx) < 4 and abs(dy) < 4:
                return
            grab = ('s' if dy >= 0 else 'n') + ('e' if dx >= 0 else 'w')
            new = ed.drag_box(box, grab, dx, dy, ratio, size, minimum=1)
        else:
            new = ed.drag_box(box, handle, dx, dy, ratio, size)
        self.edits.crop = new
        self._paint_edit_state()
        self._render_edit()

    def save_edit(self, replace: bool):
        if not self.editing or self.photo is None or not self.edits.changed:
            return
        path = self.files[self.index]
        if replace and not messagebox.askyesno(
                tr('edit.replace_title'), tr('edit.replace_ask', name=os.path.basename(path)),
                parent=self):
            return
        photo = self.photo
        self.app.set_status(tr('status.saving'), T["accent"])
        self.update_idletasks()
        if photo.raw and photo.needs_decode:
            full = raw.decode(path)                 # save from the developed photo, not the preview
            if full is not None:
                self._rescale_crop(full.width / max(1, photo.size[0]))
                photo.use_decoded(full)
        try:
            result = ed.apply(photo.image, self.edits)
            saved = ed.save(result, path, photo.format, photo.exif, photo.icc, replace=replace)
        except (OSError, ValueError) as exc:
            log.exception("could not save the edit of %s", path)
            self.app.set_status(tr('status.save_failed'), T["pink"])
            self.app.glitch.now(self.app.title, strong=True)
            messagebox.showerror(tr('edit.save_copy'), tr('edit.save_failed', error=exc),
                                 parent=self)
            return
        self._leave_edit()
        self.cache.pop(saved, None)
        self._ready.pop(saved, None)            # a preload from before the save is out of date
        if saved not in self.files:
            self.files.insert(self.index + 1, saved)
        self.app.set_status(tr('status.saved_as', name=os.path.basename(saved)), T["accent"])
        self.app.glitch.now(self.app.title)
        self.show(self.files.index(saved))

    def back(self) -> bool:
        """Esc: leave edit mode first."""
        return self.cancel_edit()

    def fullscreen_changed(self, on: bool):
        bar = self.edit_bar if self.editing else self.bar
        if on:
            bar.pack_forget()
        else:
            bar.pack(fill="x")

    def key(self, event) -> bool:
        k = event.keysym
        ctrl = bool(event.state & 0x4)
        shift = bool(event.state & 0x1)
        if self.editing:
            if k in ('r', 'R'):
                self.edit_rotate(not shift)
            elif k in ('s', 'S') and ctrl:
                self.save_edit(False)
            elif k in ('e', 'E'):
                self.enhance_toggle.set(not self.edits.enhance, fire=True)
            elif k in ('Return', 'KP_Enter'):
                self.save_edit(False)
            else:
                return k in ('Right', 'Left', 'Up', 'Down', 'space', 'Delete', 'Next', 'Prior')
            return True
        if k in ('Right', 'Next', 'space', 'Down') and not ctrl:
            if not self.fitted and k in ('Right', 'Down'):
                return self._nudge(k)
            self.next()
        elif k in ('Left', 'Prior', 'BackSpace', 'Up') and not ctrl:
            if not self.fitted and k in ('Left', 'Up'):
                return self._nudge(k)
            self.prev()
        elif k == 'Home':
            self.show(0)
        elif k == 'End':
            self.show(len(self.files) - 1)
        elif k in ('plus', 'equal', 'KP_Add'):
            self.zoom_step(1)
        elif k in ('minus', 'KP_Subtract'):
            self.zoom_step(-1)
        elif k in ('0', 'KP_0'):
            self.fit()
        elif k in ('1', 'KP_1'):
            self.zoom_to(1.0)
        elif k in ('r', 'R'):
            self.rotate()
        elif k in ('e', 'E'):
            self.start_edit()
        elif k in ('s', 'S'):
            self.toggle_slideshow()
        elif k == 'Delete':
            self.trash()
        else:
            return False
        return True

    def _nudge(self, k: str) -> bool:
        step = 80 / self.scale
        dx = {'Left': -step, 'Right': step}.get(k, 0)
        dy = {'Up': -step, 'Down': step}.get(k, 0)
        self.center = (self.center[0] + dx, self.center[1] + dy)
        self.render(quick=True)
        return True

    def destroy(self):
        for job in (self._sharpen_id, self._anim_id, self._slide_id, self._decode_id):
            if job:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        super().destroy()
