"""The small status card over the game.

The card has no background or box: the game shows through everywhere except the text, a status dot
and small team faces. Text is light with a thin dark outline, so it reads on bright and dark screens.

It's its own layered window with per-pixel alpha, so edges stay smooth with no color-key fringe. Like the old
overlay it is click-through, never takes focus and is excluded from capture, so the bot's own
frames never contain it. The bitmap is rebuilt only when what it shows changes.

The window is plain Win32 rather than a Tk toplevel, because UpdateLayeredWindow rejects Tk's window
class. It's created on the Tk thread, so Tk's event loop pumps its messages.
"""
import ctypes
import ctypes.wintypes as wt

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import art

user32 = ctypes.WinDLL("user32", use_last_error=True)   # private handles: these prototypes stay out of game.py and hud.py
gdi32 = ctypes.WinDLL("gdi32")
kernel32 = ctypes.WinDLL("kernel32")
GWL_EXSTYLE = -20
WS_EX = 0x80000 | 0x20 | 0x80 | 0x08000000 | 0x8   # layered, transparent, tool window, no-activate, topmost
WS_POPUP = 0x80000000
CLASS_NAME = "BAClientCard"
WDA_EXCLUDEFROMCAPTURE = 0x11
ULW_ALPHA = 2
SWP_NOACTIVATE, SWP_SHOWWINDOW, SWP_HIDEWINDOW = 0x10, 0x40, 0x80
HWND_TOPMOST = -1
FONTS = {"title": "seguisb.ttf", "body": "segoeui.ttf"}
COLORS = {"running": (49, 178, 245), "idle": (150, 170, 192), "halted": (232, 72, 92)}
TITLE, SUBTITLE = (255, 255, 255), (232, 241, 250)
EDGE, HALT_EDGE = (14, 28, 46), (104, 10, 28)   # the thin outline and soft shadow behind the text
SCALE = 2                       # drawn at twice the size, then reduced, for smooth edges
_fonts, _faces = {}, {}

for _sig, _args in ((user32.GetWindowLongW, (wt.HWND, ctypes.c_int)),
                    (user32.SetWindowLongW, (wt.HWND, ctypes.c_int, wt.LONG)),
                    (user32.GetDC, (wt.HWND,)),
                    (user32.ReleaseDC, (wt.HWND, wt.HDC)),
                    (user32.SetWindowDisplayAffinity, (wt.HWND, wt.DWORD)),
                    (user32.SetWindowPos, (wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_int, wt.UINT)),
                    (gdi32.CreateCompatibleDC, (wt.HDC,)),
                    (gdi32.SelectObject, (wt.HDC, wt.HGDIOBJ)),
                    (gdi32.DeleteObject, (wt.HGDIOBJ,)),
                    (gdi32.DeleteDC, (wt.HDC,)),
                    (gdi32.CreateDIBSection, (wt.HDC, ctypes.c_void_p, wt.UINT, ctypes.c_void_p, wt.HANDLE, wt.DWORD)),
                    (user32.UpdateLayeredWindow, (wt.HWND, wt.HDC, ctypes.c_void_p, ctypes.c_void_p, wt.HDC,
                                                  ctypes.c_void_p, wt.DWORD, ctypes.c_void_p, wt.DWORD))):
    _sig.argtypes = _args
user32.DefWindowProcW.argtypes = (wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.CreateWindowExW.argtypes = (wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID)
user32.CreateWindowExW.restype = wt.HWND
user32.DestroyWindow.argtypes = (wt.HWND,)
kernel32.GetModuleHandleW.argtypes = (wt.LPCWSTR,)
kernel32.GetModuleHandleW.restype = wt.HMODULE
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
user32.GetDC.restype = wt.HDC
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.CreateDIBSection.restype = wt.HBITMAP


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
                ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR),
                ("hIconSm", wt.HICON)]


user32.RegisterClassExW.argtypes = (ctypes.POINTER(WNDCLASSEXW),)
user32.RegisterClassExW.restype = wt.ATOM
_class_ready = False


def _make_window():
    """A hidden layered popup. It has no Python callback, so it can't stall the Tk thread."""
    global _class_ready
    instance = kernel32.GetModuleHandleW(None)
    if not _class_ready:
        wc = WNDCLASSEXW(cbSize=ctypes.sizeof(WNDCLASSEXW), lpfnWndProc=WNDPROC(("DefWindowProcW", user32)),
                         hInstance=instance, lpszClassName=CLASS_NAME)
        if not user32.RegisterClassExW(ctypes.byref(wc)) and ctypes.get_last_error() != 1410:   # already registered
            raise ctypes.WinError(ctypes.get_last_error())
        _class_ready = True
    hwnd = user32.CreateWindowExW(WS_EX, CLASS_NAME, "BAClient overlay", WS_POPUP, -32000, -32000, 10, 10,
                                  None, None, instance, None)
    if not hwnd:
        raise ctypes.WinError(ctypes.get_last_error())
    return hwnd


def _font(kind, px):
    key = (kind, px)
    if key not in _fonts:
        try:
            _fonts[key] = ImageFont.truetype(FONTS[kind], px)
        except OSError:
            _fonts[key] = ImageFont.load_default(px)
    return _fonts[key]


def _fit(text, font, width):
    text = str(text or "")
    if font.getlength(text) <= width:
        return text
    while text and font.getlength(text + "…") > width:
        text = text[:-1]
    return text + "…"


def _face(dev, size):
    """A round portrait at `size` drawn pixels, cached."""
    key = (dev, size)
    if key in _faces:
        return _faces[key]
    face = None
    rel = (art.urls(dev).get("small") or art.urls(dev).get("icon") or "")[5:]
    if rel:
        try:
            img = Image.open(art.ART / rel).convert("RGBA")
            side = min(img.size)
            left = (img.width - side) // 2
            img = img.crop((left, 0, left + side, side)).resize((size, size), Image.LANCZOS)
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
            img.putalpha(mask)
            face = img
        except OSError:
            face = None
    if len(_faces) > 64:
        _faces.clear()
    _faces[key] = face
    return face


def _layer(size, color, mask):
    """A solid colour shown through `mask`. Straight alpha, so edges never pick up a dark fringe."""
    layer = Image.new("RGBA", size, (*color, 0))
    layer.putalpha(mask)
    return layer


def render_card(info, width, height):
    """The card as an RGBA image of exactly width x height screen pixels, transparent behind the text."""
    w, h = width * SCALE, height * SCALE
    size = (w, h)
    unit = h / 64
    state = info["state"]
    edge, ring = Image.new("L", size, 0), Image.new("L", size, 0)
    edge_draw, ring_draw = ImageDraw.Draw(edge), ImageDraw.Draw(ring)
    stroke = max(2, round(1.4 * unit))
    outline = HALT_EDGE if state == "halted" else EDGE

    faces = [] if state == "halted" else [m for m in info.get("team", []) if m.get("dev")][:3]  # a halt needs the room
    face_size = round(36 * unit)
    border = max(2, round(1.5 * unit))
    y = (h - face_size) // 2
    fx = w - round(6 * unit) - face_size
    right, placed = w - round(4 * unit), []
    for m in reversed(faces):
        face = _face(m["dev"], face_size)
        if face is None:
            continue
        edge_draw.ellipse((fx - border - stroke, y - border - stroke, fx + face_size + border + stroke - 1,
                           y + face_size + border + stroke - 1), fill=255)
        ring_draw.ellipse((fx - border, y - border, fx + face_size + border - 1, y + face_size + border - 1), fill=255)
        placed.append((face, fx))
        right = fx - border
        fx -= round(21 * unit)

    dot_r, dot_x, dot_y = round(5 * unit), round(12 * unit), round(22 * unit)
    dot_box = (dot_x - dot_r, dot_y - dot_r, dot_x + dot_r, dot_y + dot_r)
    edge_draw.ellipse((dot_box[0] - stroke, dot_box[1] - stroke, dot_box[2] + stroke, dot_box[3] + stroke), fill=255)
    dot = Image.new("L", size, 0)
    ImageDraw.Draw(dot).ellipse(dot_box, fill=255)

    x = round(24 * unit)
    room = right - x - round(6 * unit)
    title_font, body_font = _font("title", round(20 * unit)), _font("body", round(15 * unit))
    title, body = _fit(info["title"], title_font, room), _fit(info["sub"], body_font, room)
    title_at, body_at = (x, round(8 * unit)), (x, round(34 * unit))
    edge_draw.text(title_at, title, font=title_font, fill=255, stroke_width=stroke, stroke_fill=255)
    edge_draw.text(body_at, body, font=body_font, fill=255, stroke_width=stroke, stroke_fill=255)
    title_mask, body_mask = Image.new("L", size, 0), Image.new("L", size, 0)
    ImageDraw.Draw(title_mask).text(title_at, title, font=title_font, fill=255)
    ImageDraw.Draw(body_mask).text(body_at, body, font=body_font, fill=255)

    img = Image.new("RGBA", size, (*outline, 0))
    soft = edge.filter(ImageFilter.GaussianBlur(1.4 * unit)).point(lambda v: v * 0.5)
    img.alpha_composite(_layer(size, outline, soft))
    img.alpha_composite(_layer(size, outline, edge.point(lambda v: v * 0.96)))
    img.alpha_composite(_layer(size, (255, 255, 255), ring))
    img.alpha_composite(_layer(size, COLORS[state], dot))
    img.alpha_composite(_layer(size, TITLE, title_mask))
    img.alpha_composite(_layer(size, SUBTITLE, body_mask))
    for face, at in placed:
        img.alpha_composite(face, (at, y))
    return img.resize((width, height), Image.BOX)


def card_info(hud_state, screen, team, stats=None):
    """What the card shows, from values the client already holds."""
    state, title = hud_state
    sub = str(screen or "-").replace("_", " ")
    if stats:
        key, value = next(iter(stats.items()))
        sub = f"{sub}  ·  {key} {value}"
    return {"state": state, "title": title, "sub": sub,
            "team": [{"dev": m.get("dev")} for m in team]}


class CardWindow:
    """The card's window. Create, show and hide it on the Tk thread."""

    def __init__(self):
        self.hwnd = _make_window()
        self.excluded = bool(user32.SetWindowDisplayAffinity(self.hwnd, WDA_EXCLUDEFROMCAPTURE))
        self.error = None               # a drawing failure, so the client can report it once
        self._shown_key = None
        self._rect = None
        self.visible = True             # so the first hide() really hides it
        self.hide()

    def hide(self):
        if self.visible:
            user32.SetWindowPos(self.hwnd, None, 0, 0, 0, 0, 0x1 | 0x2 | 0x4 | SWP_NOACTIVATE | SWP_HIDEWINDOW)
            self.visible = False

    def show(self, info, rect):
        """Place the card. Redraws only if the content or the size changed."""
        if not self.excluded or self.error:   # unexcluded, it would appear in the bot's own frames
            self.hide()
            return
        x, y, w, h = rect
        key = (repr(info), w, h)
        if key != self._shown_key or rect != self._rect:
            if not self._blit(render_card(info, w, h), x, y):
                self.error = f"UpdateLayeredWindow failed ({ctypes.get_last_error()})"
                self.hide()
                return
            self._shown_key, self._rect = key, rect
        if not self.visible:
            user32.SetWindowPos(self.hwnd, wt.HWND(HWND_TOPMOST), x, y, w, h, SWP_NOACTIVATE | SWP_SHOWWINDOW)
            self.visible = True

    def _blit(self, image, x, y):
        w, h = image.size
        data = _premultiplied(image)            # UpdateLayeredWindow needs premultiplied BGRA
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, w * h * 4, 0, 0, 0, 0)
        screen = user32.GetDC(None)
        mem = gdi32.CreateCompatibleDC(screen)
        bits = ctypes.c_void_p()
        bmp = gdi32.CreateDIBSection(screen, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        old = gdi32.SelectObject(mem, bmp)
        try:
            ctypes.memmove(bits, data, len(data))
            blend = BLENDFUNCTION(0, 0, 255, 1)
            return bool(user32.UpdateLayeredWindow(self.hwnd, screen, ctypes.byref(wt.POINT(x, y)),
                                                   ctypes.byref(wt.SIZE(w, h)), mem, ctypes.byref(wt.POINT(0, 0)),
                                                   0, ctypes.byref(blend), ULW_ALPHA))
        finally:
            gdi32.SelectObject(mem, old)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem)
            user32.ReleaseDC(None, screen)

    def destroy(self):
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None


def _premultiplied(image):
    """BGRA bytes with colour multiplied by alpha."""
    import numpy as np
    arr = np.asarray(image, dtype=np.uint16)
    alpha = arr[:, :, 3:4]
    rgb = (arr[:, :, :3] * alpha + 127) // 255
    out = np.dstack((rgb[:, :, 2], rgb[:, :, 1], rgb[:, :, 0], arr[:, :, 3])).astype(np.uint8)
    return out.tobytes()
