#!/usr/bin/env python3
"""Injects mouse and keyboard input into an X display via XTEST, talking to
Xlib directly.

Why ctypes and not xdotool: a trackpad sends ~60 events per second while the
finger moves. Forking xdotool per event would cost a few ms each and turn the
cursor into a choppy animation; this way a movement goes out in ~2
microseconds. It also keeps the project free of external dependencies (see
the server.py docstring).

The hard part is the keyboard. XTEST sends *keycodes*, not characters, and
the translation depends on the loaded layout. So the keyboard map is read
once and inverted into a keysym -> (keycode, level) index. Anything missing
from the map - an accented letter that isn't on the layout, an emoji, some
symbol - is temporarily written to a spare keycode, used, and the map is
restored.
"""

import ctypes
import ctypes.util
import threading
import time

NoSymbol = 0

# Levels of a keycode in the map: which modifier combination produces each.
# X stores a keycode's keysyms in groups of 4: no mod, shift, altgr,
# altgr+shift. The index is translated back into the modifiers to hold.
LEVEL_MODS = {
    0: (),
    1: ("shift",),
    2: ("altgr",),
    3: ("altgr", "shift"),
}

MOD_KEYSYM = {
    "shift": 0xFFE1,   # Shift_L
    "ctrl": 0xFFE3,    # Control_L
    "alt": 0xFFE9,     # Alt_L
    "super": 0xFFEB,   # Super_L
    "altgr": 0xFE03,   # ISO_Level3_Shift
}

# Names the page sends for keys without a character. These are X's own names
# (XStringToKeysym handles them), but the ones used here are pinned so the
# client doesn't have to guess the exact spelling.
NAMED_KEYS = {
    "enter": "Return", "return": "Return",
    "backspace": "BackSpace", "delete": "Delete",
    "tab": "Tab", "escape": "Escape", "esc": "Escape",
    "space": "space",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "home": "Home", "end": "End",
    "pageup": "Prior", "pagedown": "Next",
    "insert": "Insert",
    "menu": "Menu",
    "printscreen": "Print",
    "capslock": "Caps_Lock",
    **{f"f{i}": f"F{i}" for i in range(1, 13)},
}

BUTTON_SCROLL = {"up": 4, "down": 5, "left": 6, "right": 7}


class XTestError(RuntimeError):
    pass


class Injector:
    """A dedicated X connection. Not thread-safe: use one per client."""

    def __init__(self, display=":0"):
        libx11 = ctypes.util.find_library("X11")
        libxtst = ctypes.util.find_library("Xtst")
        if not libx11 or not libxtst:
            raise XTestError("libX11/libXtst not found")
        self.x11 = ctypes.CDLL(libx11)
        self.xtst = ctypes.CDLL(libxtst)
        self._declare()

        self.dpy = self.x11.XOpenDisplay(display.encode())
        if not self.dpy:
            raise XTestError(f"could not open display {display}")
        self.dpy = ctypes.c_void_p(self.dpy)

        self.lock = threading.Lock()
        self.held_mods = set()      # modifiers held by combo/sticky
        self.held_buttons = set()   # buttons held by drag
        self._load_keymap()

    # ------------------------------------------------------------- ctypes

    def _declare(self):
        x, t = self.x11, self.xtst
        x.XOpenDisplay.restype = ctypes.c_void_p
        x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x.XCloseDisplay.argtypes = [ctypes.c_void_p]
        x.XFlush.argtypes = [ctypes.c_void_p]
        x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x.XFree.argtypes = [ctypes.c_void_p]
        x.XStringToKeysym.restype = ctypes.c_ulong
        x.XStringToKeysym.argtypes = [ctypes.c_char_p]
        x.XKeysymToKeycode.restype = ctypes.c_ubyte
        x.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x.XDisplayKeycodes.argtypes = [ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_int),
                                       ctypes.POINTER(ctypes.c_int)]
        x.XGetKeyboardMapping.restype = ctypes.POINTER(ctypes.c_ulong)
        x.XGetKeyboardMapping.argtypes = [ctypes.c_void_p, ctypes.c_ubyte,
                                          ctypes.c_int,
                                          ctypes.POINTER(ctypes.c_int)]
        x.XChangeKeyboardMapping.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                             ctypes.c_int,
                                             ctypes.POINTER(ctypes.c_ulong),
                                             ctypes.c_int]
        t.XTestFakeRelativeMotionEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
        t.XTestFakeMotionEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_ulong]
        t.XTestFakeButtonEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
        t.XTestFakeKeyEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]

    # ------------------------------------------------------------- keymap

    def _load_keymap(self):
        lo, hi = ctypes.c_int(), ctypes.c_int()
        self.x11.XDisplayKeycodes(self.dpy, ctypes.byref(lo), ctypes.byref(hi))
        self.kc_min, self.kc_max = lo.value, hi.value
        count = self.kc_max - self.kc_min + 1

        per = ctypes.c_int()
        syms = self.x11.XGetKeyboardMapping(
            self.dpy, ctypes.c_ubyte(self.kc_min), count, ctypes.byref(per))
        if not syms:
            raise XTestError("XGetKeyboardMapping failed")
        self.per_code = per.value

        # keysym -> (keycode, level). First one wins: the map comes in keycode
        # order and low levels are the cheapest to type.
        self.by_keysym = {}
        self.spare = []             # keycodes with no symbols: used for remap
        for i in range(count):
            kc = self.kc_min + i
            levels = [syms[i * self.per_code + j] for j in range(self.per_code)]
            if not any(levels):
                self.spare.append(kc)
                continue
            for lvl, ks in enumerate(levels[:4]):
                if ks and ks not in self.by_keysym:
                    self.by_keysym[ks] = (kc, lvl)
        self.x11.XFree(ctypes.cast(syms, ctypes.c_void_p))

        if not self.spare:
            # Fully packed keyboard (rare). Sacrifice the last keycode: the
            # remap stops being reversible, but only exotic characters use it.
            self.spare = [self.kc_max]

    def _keysym_for(self, ch):
        """Keysym for a character. Latin-1 is the codepoint itself; everything
        else uses X's Unicode convention (0x1000000 + codepoint)."""
        cp = ord(ch)
        return cp if cp < 0x100 else 0x1000000 + cp

    # --------------------------------------------------------------- mouse

    def move(self, dx, dy):
        with self.lock:
            self.xtst.XTestFakeRelativeMotionEvent(
                self.dpy, int(dx), int(dy), 0)
            self.x11.XFlush(self.dpy)

    def move_to(self, x, y):
        with self.lock:
            self.xtst.XTestFakeMotionEvent(self.dpy, -1, int(x), int(y), 0)
            self.x11.XFlush(self.dpy)

    def button(self, btn, press):
        btn = int(btn)
        if btn not in (1, 2, 3, 4, 5, 6, 7, 8, 9):
            return
        with self.lock:
            self.xtst.XTestFakeButtonEvent(self.dpy, btn, 1 if press else 0, 0)
            self.x11.XFlush(self.dpy)
            if press:
                self.held_buttons.add(btn)
            else:
                self.held_buttons.discard(btn)

    def click(self, btn, count=1):
        for _ in range(max(1, min(int(count), 3))):
            self.button(btn, True)
            self.button(btn, False)

    def scroll(self, direction, ticks=1):
        btn = BUTTON_SCROLL.get(direction)
        if not btn:
            return
        for _ in range(max(1, min(int(ticks), 20))):
            self.button(btn, True)
            self.button(btn, False)

    # ------------------------------------------------------------ keyboard

    def _tap_keycode(self, kc, mods=()):
        """Holds the requested modifiers, taps the key, releases what it held.

        Modifiers the user locked (sticky) are left alone here: whoever locked
        them decides when to release, otherwise a locked Ctrl would be useless
        for any combo.
        """
        extra = [m for m in mods if m not in self.held_mods]
        for m in extra:
            self._mod_event(m, True)
        self.xtst.XTestFakeKeyEvent(self.dpy, kc, 1, 0)
        self.xtst.XTestFakeKeyEvent(self.dpy, kc, 0, 0)
        for m in reversed(extra):
            self._mod_event(m, False)
        self.x11.XFlush(self.dpy)

    def _mod_event(self, name, press):
        ks = MOD_KEYSYM.get(name)
        if not ks:
            return
        kc = self.x11.XKeysymToKeycode(self.dpy, ctypes.c_ulong(ks))
        if kc:
            self.xtst.XTestFakeKeyEvent(self.dpy, kc, 1 if press else 0, 0)

    def _with_remap(self, keysym, fn):
        """Writes a keysym to a spare keycode, runs fn(keycode), undoes it.

        The XSync + pause is not optional: the server notifies clients via
        MappingNotify and they re-read the map. Without the delay, the key
        arrives before the notification and the app on the other side types
        the keycode's old symbol.
        """
        kc = self.spare[0]
        arr = (ctypes.c_ulong * self.per_code)()
        for i in range(self.per_code):
            arr[i] = keysym if i < 2 else NoSymbol
        self.x11.XChangeKeyboardMapping(self.dpy, kc, self.per_code, arr, 1)
        self.x11.XSync(self.dpy, 0)
        time.sleep(0.012)
        try:
            fn(kc)
            self.x11.XSync(self.dpy, 0)
        finally:
            for i in range(self.per_code):
                arr[i] = NoSymbol
            self.x11.XChangeKeyboardMapping(self.dpy, kc, self.per_code, arr, 1)
            self.x11.XSync(self.dpy, 0)

    def _press_keysym(self, ks, mods=()):
        found = self.by_keysym.get(ks)
        if found:
            kc, lvl = found
            self._tap_keycode(kc, tuple(mods) + LEVEL_MODS.get(lvl, ()))
        else:
            self._with_remap(ks, lambda kc: self._tap_keycode(kc, tuple(mods)))

    def key(self, name, mods=()):
        """A named key ('enter', 'f5', 'a') with optional modifiers."""
        name = str(name)
        xname = NAMED_KEYS.get(name.lower())
        with self.lock:
            if xname:
                ks = self.x11.XStringToKeysym(xname.encode())
            elif len(name) == 1:
                ks = self._keysym_for(name)
            else:
                ks = self.x11.XStringToKeysym(name.encode())
            if not ks:
                return
            self._press_keysym(ks, mods)

    def type_text(self, text):
        """Types a string. Characters missing from the layout go via remap."""
        with self.lock:
            for ch in text[:4096]:
                if ch == "\n":
                    ks = self.x11.XStringToKeysym(b"Return")
                elif ch == "\t":
                    ks = self.x11.XStringToKeysym(b"Tab")
                else:
                    ks = self._keysym_for(ch)
                if ks:
                    self._press_keysym(ks)

    def set_mod(self, name, press):
        """Sticky modifier: the on-screen button stays lit until released."""
        if name not in MOD_KEYSYM:
            return
        with self.lock:
            self._mod_event(name, press)
            self.x11.XFlush(self.dpy)
            if press:
                self.held_mods.add(name)
            else:
                self.held_mods.discard(name)

    # ------------------------------------------------------------- cleanup

    def release_all(self):
        """Releases everything left held. Called when the client disappears:
        without this, closing the tab mid-drag leaves the mouse button down
        and the desktop turns into an endless selection."""
        with self.lock:
            for btn in list(self.held_buttons):
                self.xtst.XTestFakeButtonEvent(self.dpy, btn, 0, 0)
            self.held_buttons.clear()
            for m in list(self.held_mods):
                self._mod_event(m, False)
            self.held_mods.clear()
            self.x11.XFlush(self.dpy)

    def close(self):
        try:
            self.release_all()
        finally:
            if self.dpy:
                self.x11.XCloseDisplay(self.dpy)
                self.dpy = None
