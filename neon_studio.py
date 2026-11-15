"""
Neon Studio - glass-style desktop controller for MELK / ELK-BLEDOM style Bluetooth LED lights.

Install once:   pip install -r requirements.txt
Run:            python neon_studio.py
"""
import asyncio
import colorsys
import json
import math
import os
import queue
import re
import subprocess
import random
import threading
import time
import webbrowser
from urllib.parse import quote
from pathlib import Path
from tkinter import colorchooser, messagebox

import customtkinter as ctk
from concurrent.futures import ThreadPoolExecutor
from bleak import BleakClient, BleakScanner

import licensing

try:  # needed only for the Ambient (screen sync) tab
    import mss
    from PIL import Image
    AMBIENT_OK = True
except ImportError:
    AMBIENT_OK = False

try:  # Music mode + voice
    import numpy as np
    import soundcard as sc
    AUDIO_OK = True
except Exception:
    AUDIO_OK = False
try:  # voice commands
    import speech_recognition as sr
    VOICE_OK = AUDIO_OK
except Exception:
    VOICE_OK = False
try:  # system tray
    import pystray
    from PIL import Image, ImageDraw
    TRAY_OK = True
except Exception:
    TRAY_OK = False
try:  # typing reactive (only counts keystrokes)
    from pynput import keyboard as pynput_kb
    PYNPUT_OK = True
except Exception:
    PYNPUT_OK = False
try:  # PC meter
    import psutil
    PSUTIL_OK = True
except Exception:
    PSUTIL_OK = False

# ----------------------------------------------------------------------------
# Your light settings (same as your original script)
# ----------------------------------------------------------------------------
CONFIG_FILE = Path.home() / ".neon_studio.json"


def load_cfg():
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


def save_cfg(cfg):
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    except Exception:
        pass


CFG = load_cfg()
LIGHTS_MAC_ADDRESS = CFG.get("mac", "")   # chosen on first run, saved automatically

# Look & feel
BG, GLASS, GLASS2, EDGE = "#0a0a14", "#15152a", "#1f1f3d", "#30305c"
ACCENT, ACCENT2 = "#8b5cf6", "#22d3ee"


def glass(parent, **kw):
    return ctk.CTkFrame(parent, fg_color=GLASS, corner_radius=20, border_width=1, border_color=EDGE, **kw)
WRITE_CHARACTERISTIC = "0000fff3-0000-1000-8000-00805f9b34fb"

CMD_ON = "7e0404f00001ff00ef"
CMD_OFF = "7e0404000000ff00ef"

FPS = 15  # colour updates per second while fading (BLE can't go much faster)
PATTERN_FILE = Path(__file__).with_name("my_patterns.json")

# Built-in step-based presets. Each step: colour, hold (sec), fade (sec)
PRESETS = {
    "Sunset": [((255, 94, 0), 1.0, 2.0), ((255, 0, 80), 1.0, 2.0),
               ((140, 0, 170), 1.0, 2.0), ((20, 0, 90), 1.0, 2.0)],
    "Aurora": [((0, 255, 140), 0.5, 2.5), ((0, 120, 255), 0.5, 2.5),
               ((140, 0, 255), 0.5, 2.5), ((0, 255, 200), 0.5, 2.5)],
    "Cyberpunk": [((255, 0, 140), 1.0, 1.0), ((0, 240, 255), 1.0, 1.0),
                  ((140, 0, 255), 1.0, 1.0)],
    "Candy": [((255, 105, 180), 0.8, 1.5), ((130, 220, 255), 0.8, 1.5),
              ((255, 240, 120), 0.8, 1.5), ((180, 255, 180), 0.8, 1.5)],
    "Christmas": [((255, 0, 0), 1.0, 0.5), ((0, 255, 0), 1.0, 0.5)],
    "Heartbeat": [((255, 0, 30), 0.1, 0.1), ((40, 0, 5), 0.1, 0.15),
                  ((255, 0, 30), 0.1, 0.1), ((10, 0, 2), 0.7, 0.4)],
}

SWATCHES = [
    (255, 0, 0), (255, 90, 0), (255, 200, 0), (180, 255, 0),
    (0, 255, 0), (0, 255, 160), (0, 255, 255), (0, 120, 255),
    (0, 0, 255), (120, 0, 255), (255, 0, 255), (255, 0, 120),
    (255, 255, 255), (255, 180, 110), (150, 200, 255), (255, 105, 180),
]


def hexc(rgb):
    return "#%02x%02x%02x" % tuple(int(v) for v in rgb)


def hsv255(h, s=1.0, v=1.0):
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return int(r * 255), int(g * 255), int(b * 255)


# ----------------------------------------------------------------------------
# BLE engine: runs on its own thread + asyncio loop so the GUI never freezes
# ----------------------------------------------------------------------------
COLOURS = {
    "red": (255, 0, 0), "orange": (255, 90, 0), "yellow": (255, 200, 0), "green": (0, 255, 0),
    "cyan": (0, 255, 255), "blue": (0, 0, 255), "purple": (150, 0, 255), "violet": (150, 0, 255),
    "pink": (255, 0, 120), "magenta": (255, 0, 255), "white": (255, 255, 255), "warm": (255, 170, 90),
}


def listen_once(seconds=5, lang="en-IN"):
    """Record from the default microphone and return the recognised text."""
    rate = 16000
    mic = sc.default_microphone()
    with mic.recorder(samplerate=rate, channels=1) as rec:
        data = rec.record(numframes=rate * seconds)[:, 0]
    pcm = (np.clip(data, -1, 1) * 32767).astype(np.int16).tobytes()
    return sr.Recognizer().recognize_google(sr.AudioData(pcm, rate, 2), language=lang)


LIGHT_HINTS = ("elk", "melk", "bledom", "ledble", "led", "triones", "lotus", "magic", "neon", "qhm",
               "hue", "light", "strip", "happy", "duo")


def looks_like_light(name):
    n = (name or "").lower()
    return any(h in n for h in LIGHT_HINTS)


class ClapListener(threading.Thread):
    """Listens to the mic; calls on_double() when it hears two claps in a row."""

    def __init__(self, on_double, get_mult):
        super().__init__(daemon=True)
        self.on_double, self.get_mult = on_double, get_mult
        self.running, self.level, self.err = True, 0.0, ""

    def run(self):
        try:
            mic = sc.default_microphone()
            floor, last_hit, cooldown, was_loud = 0.01, 0.0, 0.0, False
            with mic.recorder(samplerate=44100, channels=1, blocksize=1024) as rec:
                while self.running:
                    peak = float(np.max(np.abs(rec.record(numframes=1024)[:, 0])))
                    self.level = peak
                    now = time.monotonic()
                    loud = peak > max(0.06, floor * self.get_mult())
                    if loud and not was_loud and now > cooldown:
                        if 0.12 < now - last_hit < 0.9:      # second clap
                            last_hit = 0.0
                            cooldown = now + 1.5
                            self.on_double()
                        else:
                            last_hit = now
                    if not loud:
                        floor = floor * 0.98 + peak * 0.02   # learn the room's background noise
                    was_loud = loud
        except Exception as e:  # noqa
            self.err = str(e)[:70] or "mic error"


class Lights:
    def __init__(self, status_cb):
        self.status = status_cb
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.client = None
        self.task = None
        self.brightness = 1.0
        self.speed = 1.0
        self.base = (255, 0, 120)   # colour used by effects like Breathing
        self.current = (0, 0, 0)    # last colour sent (before brightness)
        # ambient (screen sync) settings, changed live from the GUI
        self.amb = {"mode": "Dominant", "react": 0.5, "sat": 1.3, "monitor": 1,
                    "fps": 15, "dead": 4, "edge": False,
                    "cut": 0.06, "poweroff": False}
        self.amb_color = (0, 0, 0)
        self._grabber = ThreadPoolExecutor(max_workers=1)  # screen grabs run here
        self._sct = None
        self.no_response = True      # lights support write-without-response
        self.paced = True            # wait for the lights to confirm each write (no backlog)
        self.last_ms = 0
        self.static_task = None
        self.target = None
        self.info_text = ""
        self.amb_level = 1.0   # overall screen brightness 0..1, set by the grabber
        self.mac = LIGHTS_MAC_ADDRESS
        self.char = WRITE_CHARACTERISTIC
        self.want = False          # user wants to be connected -> auto-reconnect if the link drops
        self.reconnecting = False
        self.resume = None         # what to restart after a reconnect
        self.drops = 0
        self.scan_result = None
        self.auto = True       # auto-tune update rate from measured write time
        self.ema_ms = 60.0
        self.auto_fps = 15.0
        self.typing = {"style": "Flash on keys"}
        self.key_count = 0
        self.meter = {"metric": "CPU"}
        self.meter_value = 0.0
        self.music = {"style": "Beat colours", "sens": 1.0, "source": "System audio"}
        self.audio = (0.0, 0.0, 0.0)
        self.audio_err = ""
        self._audio_run = False

    # ---- plumbing --------------------------------------------------------
    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def play(self, factory):
        """Cancel whatever is running and start factory() (an async function)."""
        self.resume = factory
        def start():
            if self.task and not self.task.done():
                self.task.cancel()
            if self.static_task and not self.static_task.done():
                self.static_task.cancel()
            self.target = None
            self.task = self.loop.create_task(factory())
        self.loop.call_soon_threadsafe(start)

    def send_static(self, rgb):
        """Latest-colour-wins sender: never builds up a backlog of old colours."""
        self.resume = lambda: self.set_rgb(*rgb)
        def kick():
            self.target = rgb
            if self.task and not self.task.done():
                self.task.cancel()
            if not self.static_task or self.static_task.done():
                self.static_task = self.loop.create_task(self._static_loop())
        self.loop.call_soon_threadsafe(kick)

    async def _static_loop(self):
        while self.target is not None:
            t, self.target = self.target, None
            await self.set_rgb(*t)

    def stop(self):
        self.resume = None

        def _stop():
            if self.task and not self.task.done():
                self.task.cancel()
            if self.static_task and not self.static_task.done():
                self.static_task.cancel()
            self.target = None
        self.loop.call_soon_threadsafe(_stop)

    @property
    def connected(self):
        return bool(self.client and self.client.is_connected)

    # ---- connection ------------------------------------------------------
    def connect(self):
        self.want = True
        self.submit(self._connect())

    def disconnect(self):
        self.want = False
        self.resume = None
        self.stop()
        return self.submit(self._disconnect())

    def _pick_characteristic(self):
        try:
            svcs = self.client.services
            ch = svcs.get_characteristic(self.char)
            if ch is None:
                for cand in ("0000fff3-0000-1000-8000-00805f9b34fb", "0000ffe1-0000-1000-8000-00805f9b34fb"):
                    ch = svcs.get_characteristic(cand)
                    if ch:
                        break
            if ch is None:
                for svc in svcs:
                    for c in svc.characteristics:
                        if "write" in c.properties or "write-without-response" in c.properties:
                            ch = c
                            break
                    if ch:
                        break
            if ch:
                self.char = ch.uuid
            self.no_response = bool(ch and "write-without-response" in ch.properties)
        except Exception:
            self.no_response = True

    async def _connect(self, quiet=False):
        if self.connected:
            return True
        if not self.mac:
            self.status("No light selected", "#e74c3c")
            return False
        err = "unknown"
        for attempt in range(1, 4):
            if not quiet:
                self.status(f"Connecting… ({attempt}/3)", "orange")
            try:
                self.client = BleakClient(self.mac, disconnected_callback=self._lost)
                await self.client.connect()
                self._pick_characteristic()
                self.status("Connected", "#2ecc71")
                return True
            except Exception as e:  # noqa
                self.client = None
                err = str(e) or type(e).__name__
                await asyncio.sleep(1)
        if not quiet:
            self.status(f"Couldn't connect: {err[:50]}", "#e74c3c")
        return False

    async def _disconnect(self):
        try:
            if self.client:
                await self.client.disconnect()
        except Exception:
            pass
        self.client = None
        self.status("Disconnected", "gray")

    def _lost(self, _client):
        if not self.want:
            return
        self.drops += 1
        self.ema_ms = max(self.ema_ms, 150.0)    # be gentle until the link proves stable
        self.status("Connection lost - reconnecting…", "orange")
        self.loop.call_soon_threadsafe(self._start_reconnect)

    def _start_reconnect(self):
        if not self.reconnecting:
            self.reconnecting = True
            self.loop.create_task(self._reconnect_loop())

    async def _reconnect_loop(self):
        try:
            delay = 1.0
            while self.want and not self.connected:
                await asyncio.sleep(delay)
                if await self._connect(quiet=True):
                    self.status("Reconnected", "#2ecc71")
                    if self.resume:
                        self.play(self.resume)
                    return
                delay = min(10.0, delay * 1.7)
        finally:
            self.reconnecting = False

    # ---- low level writes -----------------------------------------------
    async def _write(self, data: bytes):
        if not self.connected:
            self.status("Not connected", "#e74c3c")
            return False
        t0 = time.perf_counter()
        resp = self.paced or self.auto or not self.no_response
        try:
            await self.client.write_gatt_char(self.char, data, response=resp)
        except asyncio.CancelledError:
            raise
        except Exception:
            try:  # the other write mode may be the one the lights want
                await self.client.write_gatt_char(self.char, data, response=not resp)
            except Exception as e:  # noqa
                self.status(f"Write failed: {str(e)[:50]}", "#e74c3c")
                return False
        self.last_ms = int((time.perf_counter() - t0) * 1000)
        self.ema_ms = self.ema_ms * 0.8 + max(1, self.last_ms) * 0.2
        self.auto_fps = max(5.0, min(30.0, 1000.0 / (self.ema_ms * 1.15)))
        return True

    async def set_rgb(self, r, g, b):
        self.current = (r, g, b)
        k = self.brightness
        r, g, b = (max(0, min(255, int(v * k))) for v in (r, g, b))
        await self._write(bytes.fromhex(f"7e070503{r:02x}{g:02x}{b:02x}00ef"))

    async def power(self, on: bool):
        await self._write(bytes.fromhex(CMD_ON if on else CMD_OFF))

    async def sleep(self, seconds):
        await asyncio.sleep(max(0.01, seconds / self.speed))

    async def fade(self, c1, c2, seconds):
        d = seconds / self.speed
        if d <= 0.02:
            await self.set_rgb(*c2)
            return
        n = max(1, int(d * FPS))
        for i in range(1, n + 1):
            t = i / n
            await self.set_rgb(*(c1[j] + (c2[j] - c1[j]) * t for j in range(3)))
            await asyncio.sleep(d / n)

    # ---- patterns --------------------------------------------------------
    async def play_steps(self, steps, loop=True):
        """steps: list of (colour, hold_seconds, fade_seconds)"""
        prev = self.current
        while True:
            for color, hold, fade in steps:
                await self.fade(prev, color, fade)
                prev = color
                await self.sleep(hold)
                await asyncio.sleep(0.02)
            if not loop:
                return

    async def fx_rainbow(self):
        h = 0.0
        while True:
            await self.set_rgb(*hsv255(h))
            h += 0.012
            await self.sleep(0.06)

    async def fx_breathing(self):
        i = 0
        while True:
            lvl = (1 - math.cos(2 * math.pi * i / 60)) / 2
            lvl = 0.03 + 0.97 * lvl
            await self.set_rgb(*(v * lvl for v in self.base))
            i += 1
            await self.sleep(0.05)

    async def fx_fire(self):
        while True:
            lvl = random.uniform(0.35, 1.0)
            await self.set_rgb(255 * lvl, random.randint(30, 110) * lvl, 0)
            await asyncio.sleep(random.uniform(0.05, 0.16) / self.speed)

    async def fx_police(self):
        while True:
            for color in ((255, 0, 0), (0, 0, 255)):
                for _ in range(3):
                    await self.set_rgb(*color)
                    await self.sleep(0.07)
                    await self.set_rgb(0, 0, 0)
                    await self.sleep(0.05)

    async def fx_strobe(self):
        while True:
            await self.set_rgb(*self.base)
            await self.sleep(0.07)
            await self.set_rgb(0, 0, 0)
            await self.sleep(0.07)

    async def fx_ocean(self):
        t = 0.0
        while True:
            h = 0.52 + 0.08 * math.sin(t)
            v = 0.6 + 0.4 * math.sin(t * 1.7 + 1)
            await self.set_rgb(*hsv255(h, 1.0, v))
            t += 0.12
            await self.sleep(0.07)

    def _grab_colour(self):
        """Runs in the grabber thread: returns the screen's representative colour."""
        if self._sct is None:
            self._sct = mss.mss()
        mons = self._sct.monitors
        idx = self.amb["monitor"]
        if idx >= len(mons):
            idx = 1
        shot = self._sct.grab(mons[idx])
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        img = img.reduce(max(1, img.width // 48))
        data = list(img.getdata())

        # overall average (also used as fallback and for brightness)
        tr = tg = tb = tw = 0.0
        vsum = 0.0
        for r, g, b in data:
            mx = max(r, g, b)
            v = mx / 255
            vsum += v
            w = 1.0 if v > 0.08 else 0.05   # mostly ignore black bars
            tr += r * w
            tg += g * w
            tb += b * w
            tw += w
        avg = (tr / tw, tg / tw, tb / tw)
        mean_v = vsum / len(data)
        self.amb_level = mean_v
        if self.amb["mode"] == "Average":
            return avg

        # Dominant colour: bucket pixels by hue, pick the strongest hue family
        bins = [[0.0, 0.0, 0.0, 0.0] for _ in range(12)]   # weight, r, g, b
        W, H = img.width, img.height
        edge = self.amb.get("edge", False)
        for i, (r, g, b) in enumerate(data):
            h, sat, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            if v < 0.12 or sat < 0.25:
                continue
            w = sat * v
            if edge:   # Ambilight style: the outer ring of the screen counts 3x
                x, y = i % W, i // W
                if min(x / W, 1 - x / W, y / H, 1 - y / H) < 0.2:
                    w *= 3
            bn = bins[int(h * 12) % 12]
            bn[0] += w
            bn[1] += r * w
            bn[2] += g * w
            bn[3] += b * w
        scores = [bins[i][0] + 0.5 * (bins[i - 1][0] + bins[(i + 1) % 12][0]) for i in range(12)]
        best = max(range(12), key=scores.__getitem__)
        if scores[best] < 0.5:      # grey / white / dark scene -> just use the average
            return avg
        grp = [bins[best], bins[best - 1], bins[(best + 1) % 12]]
        w = sum(x[0] for x in grp)
        r, g, b = (sum(x[k] for x in grp) / w for k in (1, 2, 3))
        # keep dark scenes dim even if a small bright object is on screen
        k = 0.3 + 0.7 * min(1.0, mean_v * 2.5)
        return r * k, g * k, b * k

    async def fx_ambient(self):
        loop = asyncio.get_running_loop()
        cur = None
        sent = None
        write_task = None
        last_send = 0.0
        dark_frames = 0
        is_off = False
        dark_sent = False   # light was left black/off, so it needs an ON command to wake
        wake = 0            # frames after waking where every colour is re-sent
        try:
            while True:
                wait = 1 / self._fps() - (loop.time() - last_send)
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    r, g, b = await loop.run_in_executor(self._grabber, self._grab_colour)
                except Exception as e:  # noqa
                    self.status(f"Screen capture failed: {str(e)[:40]}", "#e74c3c")
                    return

                # black scene detection: 2 dark frames in a row -> snap to black, never fade
                dark_frames = dark_frames + 1 if self.amb_level < self.amb["cut"] else 0
                black = dark_frames >= 2
                if black:
                    cur = (0.0, 0.0, 0.0)
                else:
                    if dark_sent:                    # scene is back: switch the light ON first
                        if write_task:
                            await write_task
                        await self.power(True)
                        await asyncio.sleep(0.3)     # give the controller a moment to wake up
                        dark_sent = False
                        is_off = False
                        sent = None
                        wake = 6
                    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
                    s = min(1.0, s * self.amb["sat"])
                    target = tuple(c * 255 for c in colorsys.hsv_to_rgb(h, s, v))
                    if cur is None:
                        cur = target
                    else:
                        # big changes (scene cuts) pass quickly, small flicker gets smoothed
                        dist = max(abs(t - c) for c, t in zip(cur, target))
                        a = min(1.0, self.amb["react"] + dist / 255)
                        cur = tuple(c + (t - c) * a for c, t in zip(cur, target))
                self.amb_color = cur

                if black and self.amb["poweroff"]:   # optional: really switch the light off
                    if not is_off:
                        if write_task:
                            await write_task
                        await self.power(False)
                        is_off = True
                        dark_sent = True
                        sent = cur
                        last_send = loop.time()
                    await asyncio.sleep(0.1)
                    continue

                # skip tiny changes (less Bluetooth traffic), but never leave a faint glow in black scenes
                glow = (black and sent is not None and any(sent)) or wake > 0
                if sent is not None and not glow and \
                        max(abs(x - y) for x, y in zip(cur, sent)) < self.amb["dead"]:
                    await asyncio.sleep(0.03)
                    continue
                # let the previous write finish, then send this one while we grab the next frame
                if write_task:
                    await write_task
                sent = cur
                last_send = loop.time()
                if black:
                    dark_sent = True
                if wake:
                    wake -= 1
                write_task = loop.create_task(self.set_rgb(*cur))
        finally:
            if write_task and not write_task.done():
                write_task.cancel()

    def _fps(self):
        return self.auto_fps if self.auto else self.amb["fps"]

    # ---- typing reactive ----
    def _on_key(self, _key):
        self.key_count += 1          # only counts presses, never records which key

    async def fx_typing(self):
        lst = pynput_kb.Listener(on_press=self._on_key)
        lst.start()
        loop = asyncio.get_running_loop()
        seen, hue, level, rate, last = self.key_count, 0.6, 0.15, 0.0, loop.time()
        try:
            while True:
                now = loop.time()
                dt = max(0.01, now - last)
                last = now
                new, seen = self.key_count - seen, self.key_count
                rate = rate * 0.92 + (new / dt) * 0.08            # smoothed keys per second
                if self.typing["style"] == "Speed colour":
                    hue = 0.62 - 0.62 * min(1.0, rate / 7.0)      # slow = blue, fast = red
                    level = 0.25 + 0.75 * min(1.0, rate / 5.0)
                else:
                    if new:
                        level, hue = 1.0, hue + 0.03 * new
                    else:
                        level = max(0.12, level * 0.8)
                await self.set_rgb(*hsv255(hue, 1.0, level))
                await asyncio.sleep(0.05)
        finally:
            lst.stop()

    # ---- PC meter ----
    def _read_metric(self, name):
        if name == "CPU":
            return psutil.cpu_percent(None)
        if name == "RAM":
            return psutil.virtual_memory().percent
        if name == "Disk space":
            return psutil.disk_usage(os.path.abspath(os.sep)).percent
        if name == "Battery":
            b = psutil.sensors_battery()
            return None if b is None else b.percent
        if name == "GPU (NVIDIA)":
            flags = 0x08000000 if os.name == "nt" else 0
            out = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu",
                                  "--format=csv,noheader,nounits"], capture_output=True,
                                 text=True, timeout=3, creationflags=flags).stdout
            return float(out.strip().splitlines()[0])
        return None

    async def fx_meter(self):
        loop = asyncio.get_running_loop()
        psutil.cpu_percent(None)
        cur, tick = None, 0
        while True:
            name = self.meter["metric"]
            try:
                v = await loop.run_in_executor(None, self._read_metric, name)
            except Exception:
                v = None
            if v is None:
                self.status(f"{name}: not available on this PC", "#e74c3c")
                return
            self.meter_value = v
            frac = 100 - v if name == "Battery" else v        # low battery = red
            cur = frac if cur is None else cur + (frac - cur) * 0.5
            tick += 1
            level = 1.0 if cur < 90 or tick % 2 else 0.55     # pulse when above 90%
            await self.set_rgb(*hsv255(0.33 * (1 - min(100.0, cur) / 100), 1.0, level))
            await asyncio.sleep(0.5)

    def _audio_thread(self):
        try:
            if self.music["source"] == "Microphone":
                mic = sc.default_microphone()
            else:
                mic = sc.get_microphone(sc.default_speaker().name, include_loopback=True)
            f = np.fft.rfftfreq(1024, 1 / 44100)
            lo, mi, hi = (f >= 20) & (f < 150), (f >= 150) & (f < 2000), (f >= 2000) & (f < 12000)
            win = np.hanning(1024)
            with mic.recorder(samplerate=44100, channels=1, blocksize=1024) as rec:
                while self._audio_run:
                    d = rec.record(numframes=1024)[:, 0]
                    sp = np.abs(np.fft.rfft(d * win))
                    self.audio = (float(sp[lo].sum()), float(sp[mi].sum()), float(sp[hi].sum()))
        except Exception as e:  # noqa
            self.audio_err = str(e)[:70] or "audio error"
        self._audio_run = False

    async def fx_music(self):
        self.audio_err = ""
        self.audio = (0.0, 0.0, 0.0)
        self._audio_run = True
        threading.Thread(target=self._audio_thread, daemon=True).start()
        peak = [1e-6, 1e-6, 1e-6]
        avg = level = hue = last_beat = 0.0
        loop = asyncio.get_running_loop()
        try:
            while self._audio_run:
                t0 = loop.time()
                peak = [max(p * 0.998, x, 1e-6) for p, x in zip(peak, self.audio)]
                nb, nm, nh = (min(1.0, x / p) for x, p in zip(self.audio, peak))
                sens = self.music["sens"]
                level = max(min(1.0, (nb ** 1.5) * sens), level * 0.82)
                if nb > avg * 1.35 + 0.12 and t0 - last_beat > 0.18:
                    last_beat = t0
                    hue += 0.11
                avg = avg * 0.9 + nb * 0.1
                style = self.music["style"]
                if style == "Pulse":
                    c = tuple(v * level for v in self.base)
                elif style == "Spectrum":
                    c = (255 * min(1, nb * sens), 255 * min(1, nm * sens), 255 * min(1, nh * sens))
                else:
                    c = tuple(v * (0.15 + 0.85 * level) for v in hsv255(hue))
                await self.set_rgb(*c)
                await asyncio.sleep(max(0.01, 0.05 - (loop.time() - t0)))
            if self.audio_err:
                self.status(f"Audio error: {self.audio_err}", "#e74c3c")
        finally:
            self._audio_run = False

    async def scan(self):
        self.scan_result = None
        rows = []
        try:
            found = await BleakScanner.discover(timeout=6.0, return_adv=True)
            rows = [(d.name or a.local_name or "Unknown", addr, a.rssi if a.rssi is not None else -100)
                    for addr, (d, a) in found.items()]
        except TypeError:       # older bleak without return_adv
            try:
                devs = await BleakScanner.discover(timeout=6.0)
                rows = [(d.name or "Unknown", d.address, getattr(d, "rssi", None) or -100) for d in devs]
            except Exception as e:  # noqa
                self.status(f"Scan failed: {str(e)[:50]}", "#e74c3c")
        except Exception as e:  # noqa
            self.status(f"Scan failed: {str(e)[:50]}", "#e74c3c")
        rows.sort(key=lambda r: (not looks_like_light(r[0]), -r[2]))
        self.scan_result = rows

    async def send_hex(self, hex_str):
        h = "".join(hex_str.replace("0x", "").split())
        try:
            data = bytes.fromhex(h)
        except ValueError:
            self.status("Invalid hex", "#e74c3c")
            return
        await self._write(data)

    async def read_info(self):
        if not self.connected:
            self.info_text = "Not connected - press Connect first."
            return
        lines = []
        for svc in self.client.services:
            lines.append(f"SERVICE {svc.uuid}")
            for ch in svc.characteristics:
                lines.append(f"    {ch.uuid}  [{', '.join(ch.properties)}]")
        self.info_text = "\n".join(lines)

    async def fx_random(self):
        prev = self.current
        while True:
            nxt = hsv255(random.random())
            await self.fade(prev, nxt, 1.5)
            prev = nxt


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------
EFFECTS = [
    ("🌈  Rainbow", "fx_rainbow"),
    ("🫁  Breathing", "fx_breathing"),
    ("🔥  Fire", "fx_fire"),
    ("🚨  Police", "fx_police"),
    ("⚡  Strobe", "fx_strobe"),
    ("🌊  Ocean", "fx_ocean"),
    ("🎲  Random Fade", "fx_random"),
]


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")
        self.title("Neon Studio")
        self.configure(fg_color=BG)
        self.geometry("1180x800")
        self.minsize(980, 640)

        self.q = queue.Queue()
        self.cmd_q = queue.Queue()   # actions from tray / voice threads
        self.clap = None
        self.lights_on = True
        self.tray = None
        self.ble = Lights(lambda text, color: self.q.put((text, color)))
        self.mode = "static"
        self._pending = False
        self.rows = []
        self.saved = self.load_saved()
        self._pay_win = None
        # Free: on/off, colours, brightness.  Premium (licence key): everything else.
        self.premium = licensing.check_key(CFG.get("license_email"), CFG.get("license_key"))

        self.build_sidebar()
        self.build_tabs()
        self.build_locks()
        self.set_color(self.ble.base, send=False)
        self.add_step()
        self.add_step()
        self.refresh_pattern_menu()

        self.setup_tray()
        self.after(100, self.poll)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.restyle(self)
        self.sidebar.configure(fg_color="#0e0e1c", corner_radius=0, border_width=0)
        if CFG.get("brightness"):
            self.bright.set(CFG["brightness"])
            self.ble.brightness = float(CFG["brightness"])
        if CFG.get("color"):
            self.set_color(CFG["color"], send=False)
        self.build_setup()
        if self.ble.mac:
            self.ble.connect()
        else:
            self.show_setup()

    # ---- helpers ----------------------------------------------------------
    def poll(self):
        try:
            while True:
                text, color = self.q.get_nowait()
                self.status_label.configure(text="●  " + text, text_color=color)
                self.connect_btn.configure(
                    text="Disconnect" if text in ("Connected", "Reconnected") else "Connect")
        except queue.Empty:
            pass
        try:
            while True:
                self.cmd_q.get_nowait()()
        except queue.Empty:
            pass
        except Exception as e:  # noqa
            print("action error:", e)
        if hasattr(self, "auto_lbl"):
            self.auto_lbl.configure(
                text=f"auto: {self.ble.auto_fps:.0f} updates/s (write ~{self.ble.ema_ms:.0f} ms)" if self.ble.auto else "")
        if self.clap:
            self.clap_bar.set(min(1.0, self.clap.level * 3))
            if self.clap.err:
                self.clap_lbl.configure(text="Mic error: " + self.clap.err)
        if hasattr(self, "meter_lbl") and self.tabs.get() == "Tools":
            self.meter_lbl.configure(text=f"{self.ble.meter['metric']}: {self.ble.meter_value:.0f}%")
        if hasattr(self, "music_preview") and self.tabs.get() == "Music":
            self.music_preview.configure(fg_color=hexc(self.ble.current))
        if self.ble.info_text and self.ble.info_text != self._info_shown:
            self._info_shown = self.ble.info_text
            self.info_box.delete("1.0", "end")
            self.info_box.insert("1.0", self.ble.info_text)
        if self.ble.connected:
            self.lat_label.configure(text=f"last write: {self.ble.last_ms} ms  ·  drops: {self.ble.drops}")
        else:
            self.lat_label.configure(text=f"drops: {self.ble.drops}" if self.ble.drops else "")
        if AMBIENT_OK and hasattr(self, "amb_preview") and self.tabs.get() == "Screen":
            self.amb_preview.configure(fg_color=hexc(self.ble.amb_color))
        self.after(100, self.poll)

    def on_close(self):
        self.save_settings()
        if self.tray:
            self.withdraw()      # hide to tray; use tray menu -> Quit to exit
        else:
            self.quit_app()

    def show_window(self):
        self.deiconify()
        self.lift()

    def quit_app(self):
        self.save_settings()
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        try:
            self.ble.disconnect().result(timeout=2)
        except Exception:
            pass
        self.destroy()

    # ---- sidebar ----------------------------------------------------------
    def build_sidebar(self):
        sb = ctk.CTkFrame(self, width=230, corner_radius=0)
        self.sidebar = sb
        sb.pack(side="left", fill="y")
        sb.pack_propagate(False)

        ctk.CTkLabel(sb, text="NEON\nSTUDIO", font=ctk.CTkFont(size=30, weight="bold"),
                     text_color="#ff2bd6").pack(pady=(24, 4))
        self.status_label = ctk.CTkLabel(sb, text="●  Starting…", text_color="gray")
        self.status_label.pack(pady=(0, 2))
        self.lat_label = ctk.CTkLabel(sb, text="", text_color="gray", font=ctk.CTkFont(size=11))
        self.lat_label.pack(pady=(0, 8))

        self.connect_btn = ctk.CTkButton(sb, text="Connect", command=self.toggle_connection)
        self.connect_btn.pack(padx=20, pady=4, fill="x")
        ctk.CTkButton(sb, text="Change light…", fg_color="transparent", border_width=1, border_color=EDGE,
                      text_color="gray", command=self.show_setup).pack(padx=20, pady=(2, 4), fill="x")

        row = ctk.CTkFrame(sb, fg_color="transparent")
        row.pack(padx=20, pady=(14, 4), fill="x")
        ctk.CTkButton(row, text="ON", width=80, fg_color="#1e8e4e", hover_color="#27ae60",
                      command=self.power_on).pack(side="left", expand=True, padx=(0, 4))
        ctk.CTkButton(row, text="OFF", width=80, fg_color="#a93226", hover_color="#c0392b",
                      command=self.power_off).pack(side="left", expand=True, padx=(4, 0))

        ctk.CTkLabel(sb, text="Brightness").pack(pady=(24, 0))
        self.bright = ctk.CTkSlider(sb, from_=0.02, to=1.0, command=self.on_brightness)
        self.bright.set(1.0)
        self.bright.pack(padx=20, pady=4, fill="x")

        ctk.CTkLabel(sb, text="Effect speed").pack(pady=(16, 0))
        self.speed = ctk.CTkSlider(sb, from_=0.25, to=4.0, command=self.on_speed)
        self.speed.set(1.0)
        self.speed.pack(padx=20, pady=4, fill="x")
        self.speed_lbl = ctk.CTkLabel(sb, text="1.0x", text_color="gray")
        self.speed_lbl.pack()

        self.paced_switch = ctk.CTkSwitch(sb, text="Paced writes (less lag)",
                                          command=lambda: setattr(self.ble, "paced", bool(self.paced_switch.get())))
        self.paced_switch.select()
        self.paced_switch.pack(padx=20, pady=(22, 0), anchor="w")
        self.auto_switch = ctk.CTkSwitch(sb, text="Auto-tune update rate",
                                         command=lambda: setattr(self.ble, "auto", bool(self.auto_switch.get())))
        self.auto_switch.select()
        self.auto_switch.pack(padx=20, pady=(8, 0), anchor="w")
        self.auto_lbl = ctk.CTkLabel(sb, text="", text_color="gray", font=ctk.CTkFont(size=11))
        self.auto_lbl.pack(padx=20, anchor="w")

        ctk.CTkButton(sb, text="■  Stop effect", fg_color="#444", hover_color="#555",
                      command=self.stop_effect).pack(padx=20, pady=(14, 4), fill="x")

        ctk.CTkLabel(sb, text="Effects are drawn by this app,\nso keep it open while they run.",
                     text_color="gray", font=ctk.CTkFont(size=11)).pack(side="bottom", pady=14)

        self.premium_box = ctk.CTkFrame(sb, fg_color="transparent")
        self.premium_box.pack(side="bottom", fill="x", padx=20, pady=(0, 4))
        self.refresh_premium_ui()

    def toggle_connection(self):
        if self.ble.connected:
            self.ble.disconnect()
        else:
            self.ble.connect()

    def power_on(self):
        self.lights_on = True
        self.mode = "static"
        self.ble.stop()
        self.ble.submit(self.ble.power(True))

    def power_off(self):
        self.lights_on = False
        self.mode = "static"
        self.ble.stop()
        self.ble.submit(self.ble.power(False))

    def stop_effect(self):
        self.ble.stop()
        self.mode = "static"

    def on_brightness(self, v):
        self.ble.brightness = float(v)
        self.f_sent = None   # focus timer re-sends its colour with the new brightness
        if self.mode == "static":
            self.schedule_send()

    def on_speed(self, v):
        self.ble.speed = float(v)
        self.speed_lbl.configure(text=f"{float(v):.1f}x")

    # ---- premium / licence ------------------------------------------------
    LOCKED_TABS = ("Effects", "Screen", "Music", "Tools")

    def price(self):
        return licensing.load_payment_config().get("price_inr", 129)

    def require_premium(self, feature="This feature"):
        """Call at the top of every premium action. Shows the buy window and returns False when locked."""
        if self.premium:
            return True
        self.show_window()
        self.open_premium_dialog(feature)
        return False

    def refresh_premium_ui(self):
        for w in self.premium_box.winfo_children():
            w.destroy()
        if self.premium:
            ctk.CTkLabel(self.premium_box, text="⭐  Premium unlocked", text_color="#f59e0b",
                         font=ctk.CTkFont(size=13, weight="bold")).pack(pady=4)
        else:
            ctk.CTkButton(self.premium_box, text=f"⭐  Buy Premium (₹{self.price()})", height=38,
                          fg_color="#b9770e", hover_color="#d68910",
                          command=lambda: self.open_premium_dialog()).pack(fill="x", pady=(0, 4))
            ctk.CTkButton(self.premium_box, text="Enter licence key", height=28, fg_color="transparent",
                          border_width=1, border_color=EDGE, text_color="gray",
                          command=lambda: self.open_premium_dialog()).pack(fill="x")
        self.restyle(self.premium_box)

    def build_locks(self):
        """Cover each premium tab with a lock screen until a valid licence is entered."""
        self.locks = []
        for name in self.LOCKED_TABS:
            cover = ctk.CTkFrame(self.tabs.tab(name), fg_color=GLASS, corner_radius=16)
            cover.place(relx=0, rely=0, relwidth=1, relheight=1)
            box = ctk.CTkFrame(cover, fg_color="transparent")
            box.place(relx=0.5, rely=0.5, anchor="center")
            ctk.CTkLabel(box, text="🔒", font=ctk.CTkFont(size=48)).pack()
            ctk.CTkLabel(box, text=f"{name} is a Premium feature",
                         font=ctk.CTkFont(size=20, weight="bold")).pack(pady=(6, 2))
            ctk.CTkLabel(box, justify="center", text_color="gray",
                         text="Free: lights on/off, colours and brightness.\n"
                              "Premium: effects, screen sync, music, PC meter, focus timer,\n"
                              "pattern studio, voice/text commands and more.").pack(pady=(0, 14))
            ctk.CTkButton(box, text=f"⭐  Buy Premium (₹{self.price()})", height=44, width=260,
                          fg_color="#b9770e", hover_color="#d68910",
                          command=lambda n=name: self.open_premium_dialog(n)).pack()
            ctk.CTkButton(box, text="I have a licence key", width=260, fg_color="transparent",
                          border_width=1, border_color=EDGE, text_color="gray",
                          command=lambda n=name: self.open_premium_dialog(n)).pack(pady=(8, 0))
            cover.lift()
            self.locks.append(cover)
        if self.premium:
            self.remove_locks()

    def remove_locks(self):
        for c in getattr(self, "locks", []):
            c.place_forget()

    def activate_license(self, email, key):
        """Returns (ok, message). Saves the licence on success."""
        if not licensing.PUBLIC_KEY_HEX:
            return False, "Licensing isn't set up in this build (no public key)."
        if not licensing.EMAIL_RE.match(licensing.normalise_email(email)):
            return False, "Enter the e-mail address you paid with."
        if not licensing.check_key(email, key):
            return False, "That key doesn't match this e-mail. Check for typos."
        CFG["license_email"] = licensing.normalise_email(email)
        CFG["license_key"] = key.strip()
        save_cfg(CFG)
        self.premium = True
        self.remove_locks()
        self.refresh_premium_ui()
        return True, "Premium unlocked - thank you!"

    def _qr_image(self, pay, size=230):
        """Your own QR screenshot (payment_config.json -> qr_image) or, failing that, one drawn from the UPI id."""
        try:
            from PIL import Image
        except Exception:
            return None
        img = None
        path = Path(pay.get("qr_image") or "")
        if not path.is_absolute():
            path = licensing.HERE / path
        try:
            if path.is_file():
                img = Image.open(path).convert("RGB")
            else:
                uri = licensing.upi_uri(pay)
                if uri:
                    import qrcode            # optional: pip install qrcode
                    img = qrcode.make(uri).convert("RGB")
        except Exception:
            img = None
        if img is None:
            return None
        w, h = img.size
        k = size / max(w, h)
        return ctk.CTkImage(light_image=img, dark_image=img, size=(int(w * k), int(h * k)))

    def open_premium_dialog(self, feature=None):
        try:
            if self._pay_win is not None and self._pay_win.winfo_exists():
                self._pay_win.lift()
                self._pay_win.focus_force()
                return
        except Exception:
            pass
        pay = licensing.load_payment_config()
        price = pay.get("price_inr", 129)
        win = ctk.CTkToplevel(self)
        self._pay_win = win
        win.title("Neon Studio Premium")
        win.geometry("540x860")
        win.minsize(480, 560)
        win.configure(fg_color=BG)
        win.transient(self)
        body = ctk.CTkScrollableFrame(win, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=8, pady=8)

        def card(title):
            c = glass(body)
            c.pack(fill="x", padx=8, pady=8)
            ctk.CTkLabel(c, text=title, font=ctk.CTkFont(size=15, weight="bold"),
                         anchor="w").pack(fill="x", padx=16, pady=(12, 4))
            return c

        ctk.CTkLabel(body, text="⭐  Neon Studio Premium", font=ctk.CTkFont(size=26, weight="bold"),
                     text_color="#f59e0b").pack(pady=(10, 0))
        ctk.CTkLabel(body, text=f"One-time ₹{price}  ·  lifetime licence", text_color="gray").pack()
        if feature:
            ctk.CTkLabel(body, text=f"“{feature}” needs Premium.", text_color="#fb7185").pack(pady=(6, 0))
        ctk.CTkLabel(body, justify="left", text_color="gray",
                     text="Unlocks effects & preset palettes, screen ambient sync, music mode,\n"
                          "PC meter, typing light, double-clap, focus timer, pattern studio,\n"
                          "voice/text commands and the explorer.").pack(pady=(8, 2))

        # 1 - pay
        c = card(f"1   Pay ₹{price} with any UPI app")
        qr = self._qr_image(pay)
        if qr is not None:
            win._qr = qr  # keep a reference or Tk drops the image
            ctk.CTkLabel(c, text="", image=qr).pack(pady=4)
        else:
            ctk.CTkLabel(c, text="(QR not set up yet - see payment_config.json)", text_color="gray").pack(pady=4)
        if pay.get("upi_id"):
            row = ctk.CTkFrame(c, fg_color="transparent")
            row.pack(pady=(0, 4))
            ctk.CTkLabel(row, text=f"UPI ID:  {pay['upi_id']}").pack(side="left", padx=(0, 8))

            def copy_upi():
                self.clipboard_clear()
                self.clipboard_append(pay["upi_id"])
                copy_btn.configure(text="Copied ✓")
            copy_btn = ctk.CTkButton(row, text="Copy", width=70, command=copy_upi)
            copy_btn.pack(side="left")
        ctk.CTkLabel(c, text=f"Amount: exactly ₹{price}", text_color="gray").pack(pady=(0, 12))

        # 2 - tell us who paid (in-app form -> ready-written message to the seller)
        c = card("2   Send your Gmail and UTR")
        ctk.CTkLabel(c, justify="left", wraplength=440, text_color="gray",
                     text="After paying, enter the Gmail address to send the key to and the 12-digit UPI "
                          "Transaction ID (UTR) from your UPI app's payment details.").pack(padx=16, anchor="w")
        g_e = ctk.CTkEntry(c, placeholder_text="Your Gmail address")
        g_e.pack(fill="x", padx=16, pady=(8, 4))
        if CFG.get("license_email"):
            g_e.insert(0, CFG["license_email"])
        u_e = ctk.CTkEntry(c, placeholder_text="UPI Transaction ID (UTR) - 12 digits")
        u_e.pack(fill="x", padx=16, pady=4)
        send_msg = ctk.CTkLabel(c, text="", wraplength=440)
        send_msg.pack(padx=16, pady=2)

        def details():
            """Validated (gmail, utr, text) or None."""
            gm = licensing.normalise_email(g_e.get())
            utr = re.sub(r"\s+", "", u_e.get())
            if not licensing.EMAIL_RE.match(gm):
                send_msg.configure(text="Enter a valid Gmail address.", text_color="#fb7185")
                return None
            if not re.fullmatch(r"\d{12}", utr):
                send_msg.configure(text="The UTR is the 12-digit number in your UPI payment details.",
                                   text_color="#fb7185")
                return None
            text = (f"Neon Studio Premium payment\nAmount: Rs.{price}\nGmail: {gm}\nUTR: {utr}")
            return gm, utr, text

        def via_gmail():
            d = details()
            if not d:
                return
            if not pay.get("support_email"):
                send_msg.configure(text="Seller e-mail isn't set (payment_config.json).", text_color="#fb7185")
                return
            webbrowser.open("https://mail.google.com/mail/?view=cm&fs=1&to=%s&su=%s&body=%s" % (
                quote(pay["support_email"]), quote("Neon Studio Premium payment"), quote(d[2])))
            send_msg.configure(text="Gmail opened - press Send there. Your key is e-mailed once the "
                                    "payment is verified.", text_color="#34d399")

        def via_whatsapp():
            d = details()
            if d:
                webbrowser.open("https://wa.me/%s?text=%s" % (pay["whatsapp_number"], quote(d[2])))
                send_msg.configure(text="WhatsApp opened - press Send there.", text_color="#34d399")

        def copy_details():
            d = details()
            if d:
                self.clipboard_clear()
                self.clipboard_append(d[2])
                send_msg.configure(text="Copied. Send it to: " + (pay.get("support_email") or "the seller"),
                                   text_color="#34d399")
        row = ctk.CTkFrame(c, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(4, 14))
        ctk.CTkButton(row, text="✉  Send via Gmail", command=via_gmail).pack(side="left", padx=(0, 6))
        if pay.get("whatsapp_number"):
            ctk.CTkButton(row, text="WhatsApp", width=100, command=via_whatsapp).pack(side="left", padx=(0, 6))
        ctk.CTkButton(row, text="Copy", width=70, fg_color="transparent", border_width=1, border_color=EDGE,
                      command=copy_details).pack(side="left")

        # 3 - licence key
        c = card("3   Enter your licence key")
        ctk.CTkLabel(c, justify="left", wraplength=440, text_color="gray",
                     text="Once your payment is verified, your key is emailed to you. "
                          "Use the same Gmail address you submitted.").pack(padx=16, anchor="w")
        email_e = ctk.CTkEntry(c, placeholder_text="Your Gmail address")
        email_e.pack(fill="x", padx=16, pady=(8, 4))
        if CFG.get("license_email"):
            email_e.insert(0, CFG["license_email"])
        key_e = ctk.CTkEntry(c, placeholder_text="NS1-XXXXXXXX-XXXXXXXX-…")
        key_e.pack(fill="x", padx=16, pady=4)
        status = ctk.CTkLabel(c, text="", wraplength=440)
        status.pack(padx=16, pady=2)

        def activate():
            ok, msg = self.activate_license(email_e.get(), key_e.get())
            status.configure(text=msg, text_color="#34d399" if ok else "#fb7185")
            if ok:
                win.after(1400, win.destroy)
        ctk.CTkButton(c, text="Activate", height=38, command=activate).pack(padx=16, pady=(4, 14), anchor="w")
        if pay.get("support_email"):
            ctk.CTkLabel(body, text=f"Problems? {pay['support_email']}", text_color="gray").pack(pady=(0, 10))

        self.restyle(win)
        win.after(200, lambda: (win.lift(), win.focus_force()))

    # ---- tabs -------------------------------------------------------------
    def build_tabs(self):
        self.tabs = ctk.CTkTabview(self)
        self.tabs.pack(side="left", fill="both", expand=True, padx=14, pady=12)
        for name in ("Home", "Effects", "Screen", "Music", "Tools"):
            self.tabs.add(name)
        self.build_colors_tab(self.tabs.tab("Home"))
        self.build_effects_tab(self.tabs.tab("Effects"))
        self.build_ambient_tab(self.tabs.tab("Screen"))
        self.build_music_tab(self.tabs.tab("Music"))
        self.tools = ctk.CTkTabview(self.tabs.tab("Tools"))
        self.tools.pack(fill="both", expand=True)
        for name in ("Live", "Focus", "Studio", "Commands", "Explorer"):
            self.tools.add(name)
        self.build_live_tab(self.tools.tab("Live"))
        self.build_focus_tab(self.tools.tab("Focus"))
        self.build_studio_tab(self.tools.tab("Studio"))
        self.build_commands_tab(self.tools.tab("Commands"))
        self.build_explorer_tab(self.tools.tab("Explorer"))

    # ---- Colors tab -------------------------------------------------------
    def build_colors_tab(self, tab):
        self.preview = ctk.CTkFrame(tab, height=110, corner_radius=16)
        self.preview.pack(fill="x", padx=10, pady=10)

        self.sliders = []
        for name, col in (("R", "#e74c3c"), ("G", "#2ecc71"), ("B", "#3498db")):
            f = ctk.CTkFrame(tab, fg_color="transparent")
            f.pack(fill="x", padx=10, pady=2)
            ctk.CTkLabel(f, text=name, width=24).pack(side="left")
            s = ctk.CTkSlider(f, from_=0, to=255, number_of_steps=255,
                              progress_color=col, command=lambda _v: self.on_rgb_slider())
            s.pack(side="left", fill="x", expand=True, padx=8)
            self.sliders.append(s)

        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.pack(fill="x", padx=10, pady=10)
        self.hex_entry = ctk.CTkEntry(bar, width=110, placeholder_text="#RRGGBB")
        self.hex_entry.pack(side="left")
        self.hex_entry.bind("<Return>", lambda _e: self.on_hex())
        ctk.CTkButton(bar, text="Apply hex", width=90, command=self.on_hex).pack(side="left", padx=6)
        ctk.CTkButton(bar, text="🎨 Color wheel…", command=self.open_wheel).pack(side="left", padx=6)

        ctk.CTkLabel(tab, text="Quick colors", anchor="w").pack(fill="x", padx=12, pady=(10, 2))
        grid = ctk.CTkFrame(tab, fg_color="transparent")
        grid.pack(padx=10, pady=4, anchor="w")
        for i, rgb in enumerate(SWATCHES):
            ctk.CTkButton(grid, text="", width=54, height=40, fg_color=hexc(rgb),
                          hover_color=hexc(rgb), corner_radius=10,
                          command=lambda c=rgb: self.set_color(c)).grid(
                row=i // 8, column=i % 8, padx=4, pady=4)

    def set_color(self, rgb, send=True):
        rgb = tuple(int(v) for v in rgb)
        self.ble.base = rgb
        for s, v in zip(self.sliders, rgb):
            s.set(v)
        self.preview.configure(fg_color=hexc(rgb))
        self.hex_entry.delete(0, "end")
        self.hex_entry.insert(0, hexc(rgb))
        if send:
            self.mode = "static"
            self.schedule_send()

    def on_rgb_slider(self):
        self.set_color([s.get() for s in self.sliders])

    def on_hex(self):
        h = self.hex_entry.get().strip().lstrip("#")
        try:
            self.set_color([int(h[i:i + 2], 16) for i in (0, 2, 4)])
        except (ValueError, IndexError):
            messagebox.showerror("Invalid hex", "Use a format like #ff00aa")

    def open_wheel(self):
        rgb, _ = colorchooser.askcolor(color=hexc(self.ble.base), title="Pick a colour")
        if rgb:
            self.set_color(rgb)

    def schedule_send(self):
        """Throttle slider drags so we don't flood the Bluetooth link."""
        if self._pending:
            return
        self._pending = True
        self.after(30, self._flush)

    def _flush(self):
        self._pending = False
        self.ble.send_static(tuple(self.ble.base))

    # ---- Effects tab ------------------------------------------------------
    def build_effects_tab(self, tab):
        ctk.CTkLabel(tab, text="Built-in effects  (Breathing & Strobe use your current colour)",
                     anchor="w").pack(fill="x", padx=12, pady=(8, 4))
        grid = ctk.CTkFrame(tab, fg_color="transparent")
        grid.pack(fill="x", padx=8, pady=4)
        for i, (label, fn) in enumerate(EFFECTS):
            ctk.CTkButton(grid, text=label, height=52, font=ctk.CTkFont(size=15),
                          command=lambda f=fn: self.run_effect(f)).grid(
                row=i // 3, column=i % 3, padx=6, pady=6, sticky="ew")
        for c in range(3):
            grid.grid_columnconfigure(c, weight=1)

        ctk.CTkLabel(tab, text="Preset palettes", anchor="w").pack(fill="x", padx=12, pady=(18, 4))
        grid2 = ctk.CTkFrame(tab, fg_color="transparent")
        grid2.pack(fill="x", padx=8, pady=4)
        for i, name in enumerate(PRESETS):
            ctk.CTkButton(grid2, text=name, height=44, fg_color="#5b2a86", hover_color="#7a3bb0",
                          command=lambda n=name: self.run_preset(n)).grid(
                row=i // 3, column=i % 3, padx=6, pady=6, sticky="ew")
        for c in range(3):
            grid2.grid_columnconfigure(c, weight=1)

    def run_effect(self, fn_name):
        if not self.require_premium("Effects"):
            return
        self.mode = "pattern"
        self.ble.play(getattr(self.ble, fn_name))

    def run_preset(self, name):
        if not self.require_premium("Preset palettes"):
            return
        steps = PRESETS[name]
        self.mode = "pattern"
        self.ble.play(lambda: self.ble.play_steps(steps))

    # ---- Ambient tab ------------------------------------------------------
    def build_ambient_tab(self, tab):
        if not AMBIENT_OK:
            ctk.CTkLabel(tab, text="Ambient mode needs two extra packages.\n\n"
                         "Close the app and run:\n    py -m pip install mss pillow\n\nthen start it again.",
                         font=ctk.CTkFont(size=15), justify="left").pack(padx=20, pady=40, anchor="w")
            return
        ctk.CTkLabel(tab, text="Your lights follow the colours on your screen.",
                     anchor="w").pack(fill="x", padx=12, pady=(6, 4))
        self.amb_preview = ctk.CTkFrame(tab, height=70, corner_radius=16, fg_color="#111")
        self.amb_preview.pack(fill="x", padx=10, pady=8)

        with mss.mss() as sct:
            count = len(sct.monitors) - 1
        self.mon_map = {f"Monitor {i}": i for i in range(1, count + 1)}
        if count > 1:
            self.mon_map["All monitors"] = 0

        r1 = ctk.CTkFrame(tab, fg_color="transparent")
        r1.pack(fill="x", padx=10, pady=6)
        ctk.CTkLabel(r1, text="Screen", width=110, anchor="w").pack(side="left")
        ctk.CTkOptionMenu(r1, values=list(self.mon_map), width=160,
                          command=lambda v: self.ble.amb.update(monitor=self.mon_map[v])).pack(side="left")

        r2 = ctk.CTkFrame(tab, fg_color="transparent")
        r2.pack(fill="x", padx=10, pady=6)
        ctk.CTkLabel(r2, text="Colour mode", width=110, anchor="w").pack(side="left")
        seg = ctk.CTkSegmentedButton(r2, values=["Dominant", "Average"],
                                     command=lambda v: self.ble.amb.update(mode=v))
        seg.set("Dominant")
        seg.pack(side="left")
        ctk.CTkLabel(r2, text="Dominant = the main colour on screen (best). Average blends everything.",
                     text_color="gray", font=ctk.CTkFont(size=11)).pack(side="left", padx=10)

        pr = ctk.CTkFrame(tab, fg_color="transparent")
        pr.pack(fill="x", padx=10, pady=6)
        ctk.CTkLabel(pr, text="Presets", width=110, anchor="w").pack(side="left")
        for name in ("Smooth", "Balanced", "Instant"):
            ctk.CTkButton(pr, text=name, width=90, fg_color="#5b2a86", hover_color="#7a3bb0",
                          command=lambda n=name: self.amb_preset(n)).pack(side="left", padx=4)

        self.amb_sliders = {}
        for label, key, lo, hi, init in (("Reaction speed", "react", 0.1, 1.0, 0.5),
                                         ("Colour boost", "sat", 1.0, 2.5, 1.3),
                                         ("Max updates/sec", "fps", 5, 40, 15),
                                         ("Black cutoff", "cut", 0.0, 0.25, 0.06)):
            f = ctk.CTkFrame(tab, fg_color="transparent")
            f.pack(fill="x", padx=10, pady=6)
            ctk.CTkLabel(f, text=label, width=110, anchor="w").pack(side="left")
            sl = ctk.CTkSlider(f, from_=lo, to=hi,
                               command=lambda v, k=key: self.ble.amb.update({k: float(v)}))
            sl.set(init)
            sl.pack(side="left", fill="x", expand=True, padx=8)
            self.amb_sliders[key] = sl

        self.edge_switch = ctk.CTkSwitch(tab, text="Favour screen edges (Ambilight style)",
                                         command=lambda: self.ble.amb.update(edge=bool(self.edge_switch.get())))
        self.edge_switch.pack(padx=14, pady=6, anchor="w")
        self.off_switch = ctk.CTkSwitch(tab, text="Fully power the light OFF in black scenes",
                                        command=lambda: self.ble.amb.update(poweroff=bool(self.off_switch.get())))
        self.off_switch.pack(padx=14, pady=4, anchor="w")

        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.pack(fill="x", padx=10, pady=18)
        ctk.CTkButton(bar, text="▶  Start ambient", height=44, width=170, fg_color="#1e8e4e",
                      hover_color="#27ae60", command=self.start_ambient).pack(side="left", padx=4)
        ctk.CTkButton(bar, text="■  Stop", height=44, width=100, fg_color="#444",
                      hover_color="#555", command=self.stop_effect).pack(side="left", padx=4)
        ctk.CTkLabel(tab, text="Tip: the Brightness slider on the left still controls the overall level.\n"
                     "Some protected video (Netflix etc.) can show up black to screen capture.",
                     text_color="gray", justify="left").pack(padx=12, anchor="w")

    def amb_preset(self, name):
        # react, sat, fps, deadband, paced writes
        react, sat, fps, dead, paced = {
            "Smooth":   (0.3, 1.3, 10, 5, True),
            "Balanced": (0.5, 1.3, 15, 4, True),
            "Instant":  (1.0, 1.2, 30, 3, False),
        }[name]
        self.ble.amb.update(react=react, sat=sat, fps=fps, dead=dead)
        for k, v in (("react", react), ("sat", sat), ("fps", fps)):
            self.amb_sliders[k].set(v)
        self.ble.paced = paced
        if name == "Instant":
            self.ble.auto = False
            self.auto_switch.deselect()
        (self.paced_switch.select if paced else self.paced_switch.deselect)()

    def start_ambient(self):
        if not self.require_premium("Screen ambient sync"):
            return
        self.mode = "pattern"
        self.ble.play(self.ble.fx_ambient)

    # ---- Focus Timer tab --------------------------------------------------
    def build_focus_tab(self, tab):
        self.f_start = (255, 0, 0)
        self.f_end = (150, 0, 255)
        self.f_elapsed = 0.0
        self.f_total = 25 * 60.0
        self.f_state = "idle"      # idle | running | paused | done
        self.f_sent_fill = None
        self.f_last = 0.0
        self.f_sent = None
        self.f_job = None

        ctk.CTkLabel(tab, text="The light slowly blends from the start colour to the end colour "
                     "as your session goes by.", anchor="w").pack(fill="x", padx=12, pady=(6, 0))
        self.f_time_lbl = ctk.CTkLabel(tab, text="25:00", font=ctk.CTkFont(size=44, weight="bold"))
        self.f_time_lbl.pack(pady=(4, 0))
        self.f_bar = ctk.CTkProgressBar(tab, height=12)
        self.f_bar.set(0)
        self.f_bar.pack(fill="x", padx=30, pady=6)
        self.f_preview = ctk.CTkFrame(tab, height=36, corner_radius=14, fg_color="#222")
        self.f_preview.pack(fill="x", padx=30, pady=6)

        r1 = ctk.CTkFrame(tab, fg_color="transparent")
        r1.pack(fill="x", padx=10, pady=6)
        ctk.CTkLabel(r1, text="Minutes", width=90, anchor="w").pack(side="left")
        self.f_min = ctk.StringVar(value="25")
        ctk.CTkEntry(r1, width=70, textvariable=self.f_min).pack(side="left")
        for m in (15, 25, 30, 45, 60):
            ctk.CTkButton(r1, text=str(m), width=44, fg_color="#444", hover_color="#555",
                          command=lambda m=m: self.focus_set_minutes(m)).pack(side="left", padx=3)

        r2 = ctk.CTkFrame(tab, fg_color="transparent")
        r2.pack(fill="x", padx=10, pady=6)
        ctk.CTkLabel(r2, text="Colours", width=90, anchor="w").pack(side="left")
        self.f_start_btn = ctk.CTkButton(r2, text="Start colour", width=120, text_color="white",
                                         fg_color=hexc(self.f_start), hover=False,
                                         command=lambda: self.focus_pick("start"))
        self.f_start_btn.pack(side="left", padx=3)
        ctk.CTkLabel(r2, text="→").pack(side="left", padx=4)
        self.f_end_btn = ctk.CTkButton(r2, text="End colour", width=120, text_color="white",
                                       fg_color=hexc(self.f_end), hover=False,
                                       command=lambda: self.focus_pick("end"))
        self.f_end_btn.pack(side="left", padx=3)

        self.f_sb = self.focus_slider(tab, "Start brightness", 0.25)
        self.f_eb = self.focus_slider(tab, "End brightness", 1.0)

        r3 = ctk.CTkFrame(tab, fg_color="transparent")
        r3.pack(fill="x", padx=10, pady=4)
        ctk.CTkLabel(r3, text="Fill style", width=90, anchor="w").pack(side="left")
        self.f_style = ctk.CTkOptionMenu(r3, values=["Blend", "Segments (experimental)"], width=190)
        self.f_style.pack(side="left")
        ctk.CTkLabel(r3, text="Segments").pack(side="left", padx=(12, 2))
        self.f_segs = ctk.StringVar(value="10")
        ctk.CTkEntry(r3, width=44, textvariable=self.f_segs).pack(side="left")
        ctk.CTkButton(r3, text="Test 50% fill", width=100, fg_color="#444", hover_color="#555",
                      command=lambda: self.focus_send_segments(True, test=True)).pack(side="left", padx=8)
        self.f_tpl = ctk.CTkEntry(tab, placeholder_text="Segment command template")
        self.f_tpl.insert(0, "7e0007{fill:02x}{total:02x}{r1:02x}{g1:02x}{b1:02x}ef")
        self.f_tpl.pack(fill="x", padx=14, pady=2)
        ctk.CTkLabel(tab, text="Segments = fill with the END colour over the START colour. The template is a guess: "
                     "paste your app's real command here once known. Fields: fill total r1 g1 b1 r2 g2 b2",
                     text_color="gray", font=ctk.CTkFont(size=10), wraplength=640, justify="left"
                     ).pack(fill="x", padx=14)

        self.f_flash = ctk.CTkSwitch(tab, text="Flash the light when time is up")
        self.f_flash.select()
        self.f_flash.pack(padx=14, pady=8, anchor="w")

        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.pack(fill="x", padx=10, pady=8)
        self.f_main_btn = ctk.CTkButton(bar, text="▶  Start focus", height=44, width=170,
                                        fg_color="#1e8e4e", hover_color="#27ae60",
                                        command=self.focus_toggle)
        self.f_main_btn.pack(side="left", padx=4)
        ctk.CTkButton(bar, text="↺  Reset", height=44, width=100, fg_color="#444",
                      hover_color="#555", command=self.focus_reset).pack(side="left", padx=4)

    def focus_slider(self, tab, label, init):
        f = ctk.CTkFrame(tab, fg_color="transparent")
        f.pack(fill="x", padx=10, pady=4)
        ctk.CTkLabel(f, text=label, width=120, anchor="w").pack(side="left")
        sl = ctk.CTkSlider(f, from_=0.05, to=1.0, command=lambda _v: self.focus_refresh())
        sl.set(init)
        sl.pack(side="left", fill="x", expand=True, padx=8)
        return sl

    def focus_minutes(self):
        try:
            return max(0.1, min(600.0, float(self.f_min.get())))
        except ValueError:
            return 25.0

    def focus_set_minutes(self, m):
        self.f_min.set(str(m))
        if self.f_state in ("idle", "done"):
            self.f_time_lbl.configure(text=f"{m:02d}:00")

    def focus_pick(self, which):
        cur = self.f_start if which == "start" else self.f_end
        rgb, _ = colorchooser.askcolor(color=hexc(cur), title=f"Pick the {which} colour")
        if not rgb:
            return
        rgb = tuple(int(v) for v in rgb)
        if which == "start":
            self.f_start = rgb
            self.f_start_btn.configure(fg_color=hexc(rgb))
        else:
            self.f_end = rgb
            self.f_end_btn.configure(fg_color=hexc(rgb))
        self.focus_refresh()

    def focus_refresh(self):
        """Re-apply immediately when colours/brightness change mid-session."""
        if self.f_state in ("running", "paused"):
            self.f_sent = None
            if self.mode == "focus":
                self.focus_apply()

    def focus_apply(self, force=False):
        if not self.premium:
            return
        t = min(1.0, self.f_elapsed / self.f_total)
        col = [a + (b - a) * t for a, b in zip(self.f_start, self.f_end)]
        sb, eb = self.f_sb.get(), self.f_eb.get()
        br = sb + (eb - sb) * t
        out = tuple(v * br for v in col)
        left = max(0, int(math.ceil(self.f_total - self.f_elapsed)))
        self.f_time_lbl.configure(text=f"{left // 60:02d}:{left % 60:02d}")
        self.f_bar.set(t)
        self.f_preview.configure(fg_color=hexc(out))
        if self.f_style.get().startswith("Seg"):
            self.focus_send_segments(force or self.f_sent is None)
            self.f_sent = out
        elif force or self.f_sent is None or max(abs(x - y) for x, y in zip(out, self.f_sent)) >= 1:
            self.f_sent = out
            self.ble.send_static(out)

    def focus_toggle(self):
        if not self.require_premium("Focus timer"):
            return
        if self.f_state == "running":            # pause
            self.f_state = "paused"
            if self.f_job:
                self.after_cancel(self.f_job)
                self.f_job = None
            self.f_main_btn.configure(text="▶  Resume", fg_color="#1e8e4e")
            return
        if self.f_state in ("idle", "done"):     # fresh start
            self.f_total = self.focus_minutes() * 60
            self.f_elapsed = 0.0
        self.f_state = "running"                 # start or resume
        self.mode = "focus"
        self.f_last = time.monotonic()
        self.f_sent = None
        self.f_main_btn.configure(text="⏸  Pause", fg_color="#b9770e")
        if self.f_job:
            self.after_cancel(self.f_job)
        self.focus_tick()

    def focus_tick(self):
        self.f_job = None
        if self.f_state != "running":
            return
        if self.mode != "focus":                 # something else took over the lights
            self.f_state = "paused"
            self.f_main_btn.configure(text="▶  Resume", fg_color="#1e8e4e")
            return
        now = time.monotonic()
        self.f_elapsed += now - self.f_last
        self.f_last = now
        if self.f_elapsed >= self.f_total:
            self.f_elapsed = self.f_total
            self.focus_apply(force=True)
            self.focus_finish()
            return
        self.focus_apply()
        self.f_job = self.after(1000, self.focus_tick)

    def focus_finish(self):
        self.f_state = "done"
        self.f_time_lbl.configure(text="Done! 🎉")
        self.f_main_btn.configure(text="▶  Start focus", fg_color="#1e8e4e")
        if self.f_flash.get():
            end = self.f_end
            final = tuple(v * self.f_eb.get() for v in end)
            flash = [(end, 0.15, 0.1), ((0, 0, 0), 0.15, 0.1)] * 3 + [(final, 0.1, 0.3)]
            self.mode = "pattern"
            self.ble.play(lambda: self.ble.play_steps(flash, loop=False))

    def focus_reset(self):
        if self.f_job:
            self.after_cancel(self.f_job)
            self.f_job = None
        self.f_state = "idle"
        self.f_elapsed = 0.0
        self.f_sent = None
        if self.mode == "focus":
            self.mode = "static"
        m = self.focus_minutes()
        self.f_time_lbl.configure(text=f"{int(m):02d}:{int(round((m % 1) * 60)):02d}")
        self.f_bar.set(0)
        self.f_preview.configure(fg_color="#222")
        self.f_main_btn.configure(text="▶  Start focus", fg_color="#1e8e4e")

    # ---- Explorer tab -----------------------------------------------------
    def build_explorer_tab(self, tab):
        self._info_shown = ""
        ctk.CTkLabel(tab, text="Try the lights' built-in modes. Click a number and watch the lights. "
                     "If nothing changes, switch the format and try again.",
                     anchor="w", justify="left", wraplength=640).pack(fill="x", padx=12, pady=(6, 4))
        top = ctk.CTkFrame(tab, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=4)
        ctk.CTkLabel(top, text="Format", width=60, anchor="w").pack(side="left")
        self.fmt = ctk.CTkOptionMenu(top, values=["Format A", "Format B"], width=120)
        self.fmt.pack(side="left")

        grid = ctk.CTkFrame(tab, fg_color="transparent")
        grid.pack(padx=8, pady=6, anchor="w")
        for i, m in enumerate(range(0x80, 0x9D)):
            ctk.CTkButton(grid, text=f"{m:02X}", width=52, height=34, fg_color="#5b2a86",
                          hover_color="#7a3bb0",
                          command=lambda m=m: self.send_mode(m)).grid(row=i // 8, column=i % 8,
                                                                      padx=3, pady=3)

        raw = ctk.CTkFrame(tab, fg_color="transparent")
        raw.pack(fill="x", padx=10, pady=(10, 4))
        ctk.CTkLabel(raw, text="Raw hex", width=60, anchor="w").pack(side="left")
        self.raw_entry = ctk.CTkEntry(raw, width=330, placeholder_text="e.g. 7e 07 05 03 ff 00 00 00 ef")
        self.raw_entry.pack(side="left", padx=4)
        self.raw_entry.bind("<Return>", lambda _e: self.send_raw())
        ctk.CTkButton(raw, text="Send", width=70, command=self.send_raw).pack(side="left", padx=4)

        inf = ctk.CTkFrame(tab, fg_color="transparent")
        inf.pack(fill="x", padx=10, pady=(10, 2))
        ctk.CTkButton(inf, text="Show Bluetooth info", width=170, fg_color="#444", hover_color="#555",
                      command=self.show_info).pack(side="left")
        self.info_box = ctk.CTkTextbox(tab, height=140)
        self.info_box.pack(fill="both", expand=True, padx=10, pady=6)

    def send_mode(self, m):
        if not self.require_premium("Explorer"):
            return
        self.mode = "static"
        self.ble.stop()
        if self.fmt.get() == "Format A":
            h = f"7e0003{m:02x}03000000ef"
        else:
            h = f"7e0503{m:02x}06ff00ef"
        self.ble.submit(self.ble.send_hex(h))

    def send_raw(self):
        if not self.require_premium("Explorer"):
            return
        self.mode = "static"
        self.ble.stop()
        self.ble.submit(self.ble.send_hex(self.raw_entry.get()))

    def show_info(self):
        if not self.require_premium("Explorer"):
            return
        self.ble.info_text = "Reading..."
        self.ble.submit(self.ble.read_info())

    # ---- Focus: segment fill -------------------------------------------------
    def focus_send_segments(self, force=False, test=False):
        if not self.premium:
            return
        try:
            total = max(1, int(self.f_segs.get()))
        except ValueError:
            total = 10
        frac = 0.5 if test else min(1.0, self.f_elapsed / self.f_total)
        fill = int(round(frac * total))
        if fill == self.f_sent_fill and not force:
            return
        self.f_sent_fill = fill
        eb, sb = self.f_eb.get(), self.f_sb.get()
        r1, g1, b1 = (int(v * eb) for v in self.f_end)       # filled part
        r2, g2, b2 = (int(v * sb) for v in self.f_start)     # remaining part
        try:
            h = self.f_tpl.get().format(fill=fill, total=total, r1=r1, g1=g1, b1=b1, r2=r2, g2=g2, b2=b2)
        except Exception:
            self.q.put(("Bad segment template", "#e74c3c"))
            return
        self.mode = "focus"
        self.ble.stop()
        self.ble.submit(self.ble.send_hex(h))

    # ---- Music tab --------------------------------------------------------
    def build_music_tab(self, tab):
        if not AUDIO_OK:
            ctk.CTkLabel(tab, text="Music mode needs extra packages.\n\nClose the app and run:\n"
                         "    py -m pip install numpy soundcard\n\nthen start it again.",
                         font=ctk.CTkFont(size=15), justify="left").pack(padx=20, pady=40, anchor="w")
            return
        ctk.CTkLabel(tab, text="Lights dance to the music playing on your PC (or your microphone).",
                     anchor="w").pack(fill="x", padx=12, pady=(6, 4))
        self.music_preview = ctk.CTkFrame(tab, height=100, corner_radius=16, fg_color="#111")
        self.music_preview.pack(fill="x", padx=10, pady=8)
        for label, key, values in (("Style", "style", ["Beat colours", "Pulse", "Spectrum"]),
                                   ("Source", "source", ["System audio", "Microphone"])):
            f = ctk.CTkFrame(tab, fg_color="transparent")
            f.pack(fill="x", padx=10, pady=6)
            ctk.CTkLabel(f, text=label, width=110, anchor="w").pack(side="left")
            seg = ctk.CTkSegmentedButton(f, values=values,
                                         command=lambda v, k=key: self.ble.music.update({k: v}))
            seg.set(self.ble.music[key])
            seg.pack(side="left")
        f = ctk.CTkFrame(tab, fg_color="transparent")
        f.pack(fill="x", padx=10, pady=6)
        ctk.CTkLabel(f, text="Sensitivity", width=110, anchor="w").pack(side="left")
        sl = ctk.CTkSlider(f, from_=0.5, to=2.5, command=lambda v: self.ble.music.update(sens=float(v)))
        sl.set(1.0)
        sl.pack(side="left", fill="x", expand=True, padx=8)
        ctk.CTkLabel(tab, text="Beat colours: colour jumps on every beat.  Pulse: your current colour flashes "
                     "with the bass.\nSpectrum: bass = red, mids = green, highs = blue.  Pulse uses the colour "
                     "chosen on the Colors tab.", text_color="gray", justify="left").pack(padx=12, pady=6, anchor="w")
        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.pack(fill="x", padx=10, pady=14)
        ctk.CTkButton(bar, text="▶  Start music mode", height=44, width=190, fg_color="#1e8e4e",
                      hover_color="#27ae60", command=self.start_music).pack(side="left", padx=4)
        ctk.CTkButton(bar, text="■  Stop", height=44, width=100, fg_color="#444",
                      hover_color="#555", command=self.stop_effect).pack(side="left", padx=4)

    def start_music(self):
        if not self.require_premium("Music mode"):
            return
        self.mode = "pattern"
        self.ble.play(self.ble.fx_music)

    # ---- Commands tab (text + voice) ---------------------------------------
    def build_commands_tab(self, tab):
        ctk.CTkLabel(tab, text="Type a command (or press the mic and speak):", anchor="w"
                     ).pack(fill="x", padx=12, pady=(8, 4))
        row = ctk.CTkFrame(tab, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=4)
        self.cmd_entry = ctk.CTkEntry(row, width=380, placeholder_text="e.g.  purple   |   brightness 40   |   rainbow")
        self.cmd_entry.pack(side="left", padx=4)
        self.cmd_entry.bind("<Return>", lambda _e: self.run_typed())
        ctk.CTkButton(row, text="Send", width=70, command=self.run_typed).pack(side="left", padx=4)
        ctk.CTkButton(row, text="🎤 Listen (5s)", width=120, fg_color="#5b2a86", hover_color="#7a3bb0",
                      command=self.listen).pack(side="left", padx=4)
        self.lang = ctk.CTkOptionMenu(row, values=["en-IN", "en-US", "en-GB"], width=80)
        self.lang.pack(side="left", padx=4)
        self.cmd_out = ctk.CTkLabel(tab, text="", anchor="w", font=ctk.CTkFont(size=15))
        self.cmd_out.pack(fill="x", padx=14, pady=10)
        ctk.CTkLabel(tab, justify="left", anchor="w", text_color="gray", text=(
            "Things you can say or type:\n"
            "  on / off / stop\n"
            "  red, blue, purple, pink, orange, yellow, cyan, green, white, warm\n"
            "  brightness 40  /  dim  /  brighter\n"
            "  rainbow, breathing, fire, police, strobe, ocean, random\n"
            "  music  /  ambient\n"
            "  focus 25   (starts the focus timer for 25 minutes)\n"
            "  mode 9a    (built-in mode number from the Explorer tab)\n\n"
            "Voice uses Google's free speech service, so it needs internet and your 5-second clip is sent there.\n"
            "Needs:  py -m pip install SpeechRecognition numpy soundcard")).pack(fill="x", padx=14)

    def run_typed(self):
        text = self.cmd_entry.get()
        if text.strip():
            self.handle_command(text)
            self.cmd_entry.delete(0, "end")

    def listen(self):
        if not self.require_premium("Voice commands"):
            return
        if not VOICE_OK:
            self.cmd_out.configure(text="Voice needs: py -m pip install SpeechRecognition numpy soundcard")
            return
        self.cmd_out.configure(text="🎤 Listening for 5 seconds...")
        lang = self.lang.get()

        def work():
            try:
                text = listen_once(5, lang)
                self.cmd_q.put(lambda: self.handle_command(text, heard=True))
            except sr.UnknownValueError:
                self.cmd_q.put(lambda: self.cmd_out.configure(text="Didn't catch that, try again."))
            except Exception as e:  # noqa
                msg = str(e)[:80]
                self.cmd_q.put(lambda: self.cmd_out.configure(text=f"Voice error: {msg}"))
        threading.Thread(target=work, daemon=True).start()

    def handle_command(self, text, heard=False):
        t = text.lower().strip()
        nums = re.findall(r"\d+", t)
        n = int(nums[0]) if nums else None
        has = lambda *ws: any(w in t for w in ws)
        out = None
        mode_m = re.search(r"mode\s*([0-9a-f]{2})\b", t)
        if has("turn off", "lights off", "switch off") or t == "off":
            self.power_off()
            out = "Lights off"
        elif has("turn on", "lights on", "switch on") or t == "on":
            self.power_on()
            out = "Lights on"
        elif has("stop"):
            self.stop_effect()
            out = "Stopped"
        elif not self.premium and (
                has("focus", "pomodoro", "study", "cpu", "ram", "memory", "gpu", "battery", "typing",
                    "music", "party", "beat", "ambient", "ambilight", "screen",
                    "rainbow", "breath", "fire", "police", "strobe", "ocean", "random") or mode_m):
            self.require_premium("Commands for effects and tools")
            out = "🔒 Premium feature"
        elif has("focus", "pomodoro", "study"):
            self.f_min.set(str(n or 25))
            self.focus_reset()
            self.focus_toggle()
            out = f"Focus timer: {n or 25} min"
        elif PSUTIL_OK and has("cpu", "ram", "memory", "gpu", "battery"):
            name = ("CPU" if has("cpu") else "RAM" if has("ram", "memory")
                    else "GPU (NVIDIA)" if has("gpu") else "Battery")
            self.ble.meter["metric"] = name
            self.meter_menu.set(name)
            self.start_meter()
            out = f"{name} meter"
        elif PYNPUT_OK and has("typing"):
            self.start_typing()
            out = "Typing mode"
        elif has("music", "party", "beat"):
            self.start_music()
            out = "Music mode"
        elif has("ambient", "ambilight", "screen"):
            self.start_ambient()
            out = "Ambient mode"
        elif mode_m and 0x80 <= int(mode_m.group(1), 16) <= 0x9C:
            self.send_mode(int(mode_m.group(1), 16))
            out = f"Built-in mode {mode_m.group(1).upper()}"
        elif has("bright", "dim"):
            if n is not None:
                v = n / 100
            elif has("dim", "lower", "less"):
                v = self.bright.get() - 0.25
            else:
                v = self.bright.get() + 0.25
            v = max(0.02, min(1.0, v))
            self.bright.set(v)
            self.on_brightness(v)
            out = f"Brightness {int(v * 100)}%"
        else:
            for name, fn in (("rainbow", "fx_rainbow"), ("breath", "fx_breathing"), ("fire", "fx_fire"),
                             ("police", "fx_police"), ("strobe", "fx_strobe"), ("ocean", "fx_ocean"),
                             ("random", "fx_random")):
                if name in t:
                    self.run_effect(fn)
                    out = f"Effect: {name}"
                    break
            if out is None:
                for name, c in COLOURS.items():
                    if re.search(rf"\b{name}\b", t):
                        self.set_color(c)
                        out = f"Colour: {name}"
                        break
        prefix = f'Heard "{text}"  ->  ' if heard else ""
        self.cmd_out.configure(text=prefix + (out or "Didn't understand that. See the list below."))

    # ---- System tray --------------------------------------------------------
    def setup_tray(self):
        if not TRAY_OK:
            return
        try:
            img = Image.new("RGB", (64, 64), "#12001f")
            d = ImageDraw.Draw(img)
            d.ellipse((8, 8, 56, 56), outline="#ff2bd6", width=7)
            d.ellipse((24, 24, 40, 40), fill="#00e5ff")
            put = lambda fn: (lambda icon, item: self.cmd_q.put(fn))
            colours = pystray.Menu(*[pystray.MenuItem(n.title(), put(lambda c=c: self.set_color(c)))
                                     for n, c in COLOURS.items()])
            effects = pystray.Menu(*[pystray.MenuItem(lbl.split(None, 1)[-1], put(lambda f=fn: self.run_effect(f)))
                                     for lbl, fn in EFFECTS])
            menu = pystray.Menu(
                pystray.MenuItem("Show Neon Studio", put(self.show_window), default=True),
                pystray.MenuItem("Lights ON", put(self.power_on)),
                pystray.MenuItem("Lights OFF", put(self.power_off)),
                pystray.MenuItem("Colour", colours),
                pystray.MenuItem("Effect", effects),
                pystray.MenuItem("Ambient mode", put(self.start_ambient)),
                pystray.MenuItem("Music mode", put(self.start_music)),
                pystray.MenuItem("Quit", put(self.quit_app)))
            self.tray = pystray.Icon("neon_studio", img, "Neon Studio", menu)
            self.tray.run_detached()
        except Exception as e:  # noqa
            print("Tray unavailable:", e)
            self.tray = None

    # ---- Live tab: PC meter, typing, clap ----------------------------------
    def build_live_tab(self, tab):
        def section(title):
            f = ctk.CTkFrame(tab)
            f.pack(fill="x", padx=10, pady=6)
            ctk.CTkLabel(f, text=title, font=ctk.CTkFont(size=15, weight="bold"),
                         anchor="w").pack(fill="x", padx=12, pady=(8, 2))
            return f

        def need(f, pkgs):
            ctk.CTkLabel(f, text=f"Needs a package: close the app and run   py -m pip install {pkgs}",
                         text_color="gray").pack(padx=12, pady=8, anchor="w")

        def buttons(f, start):
            row = ctk.CTkFrame(f, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=(2, 8))
            ctk.CTkButton(row, text="▶ Start", width=90, fg_color="#1e8e4e", hover_color="#27ae60",
                          command=start).pack(side="left", padx=(0, 6))
            ctk.CTkButton(row, text="■ Stop", width=80, fg_color="#444", hover_color="#555",
                          command=self.stop_effect).pack(side="left")
            return row

        f = section("📊  PC Meter  (light shows how busy one part of your PC is)")
        if not PSUTIL_OK:
            need(f, "psutil")
        else:
            row = ctk.CTkFrame(f, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=4)
            ctk.CTkLabel(row, text="Show", width=50, anchor="w").pack(side="left")
            self.meter_menu = ctk.CTkOptionMenu(row, values=["CPU", "RAM", "GPU (NVIDIA)", "Disk space", "Battery"],
                                                width=150, command=lambda v: self.ble.meter.update(metric=v))
            self.meter_menu.pack(side="left")
            self.meter_lbl = ctk.CTkLabel(row, text="", font=ctk.CTkFont(size=15))
            self.meter_lbl.pack(side="left", padx=14)
            buttons(f, self.start_meter)
            ctk.CTkLabel(f, text="Green = idle, yellow = busy, red = maxed out (pulses above 90%). "
                         "Battery goes red when low.", text_color="gray",
                         font=ctk.CTkFont(size=11)).pack(padx=12, pady=(0, 8), anchor="w")

        f = section("⌨️  Typing reactive")
        if not PYNPUT_OK:
            need(f, "pynput")
        else:
            row = ctk.CTkFrame(f, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=4)
            seg = ctk.CTkSegmentedButton(row, values=["Flash on keys", "Speed colour"],
                                         command=lambda v: self.ble.typing.update(style=v))
            seg.set("Flash on keys")
            seg.pack(side="left")
            buttons(f, self.start_typing)
            ctk.CTkLabel(f, text="Flash: every key press flashes and shifts the colour.  Speed colour: blue when "
                         "slow, red when you type fast.\nIt only counts key presses - it never records what you type.",
                         text_color="gray", font=ctk.CTkFont(size=11), justify="left").pack(padx=12, pady=(0, 8), anchor="w")

        f = section("👏  Double-clap to toggle the lights")
        if not AUDIO_OK:
            need(f, "numpy soundcard")
        else:
            row = ctk.CTkFrame(f, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=4)
            self.clap_switch = ctk.CTkSwitch(row, text="Listen for claps", command=self.clap_toggle)
            self.clap_switch.pack(side="left")
            ctk.CTkLabel(row, text="Loudness needed").pack(side="left", padx=(18, 4))
            self.clap_sens = ctk.CTkSlider(row, from_=3, to=15, width=160)
            self.clap_sens.set(6)
            self.clap_sens.pack(side="left")
            self.clap_bar = ctk.CTkProgressBar(f, height=10)
            self.clap_bar.set(0)
            self.clap_bar.pack(fill="x", padx=12, pady=4)
            self.clap_lbl = ctk.CTkLabel(f, text="The bar shows mic loudness. Music or TV nearby can trigger it - "
                                         "raise 'Loudness needed' if it does.", text_color="gray",
                                         font=ctk.CTkFont(size=11))
            self.clap_lbl.pack(padx=12, pady=(0, 8), anchor="w")

    def start_meter(self):
        if not self.require_premium("PC meter"):
            return
        self.mode = "pattern"
        self.ble.play(self.ble.fx_meter)

    def start_typing(self):
        if not self.require_premium("Typing reactive light"):
            return
        self.mode = "pattern"
        self.ble.play(self.ble.fx_typing)

    def toggle_power(self):
        (self.power_off if self.lights_on else self.power_on)()

    def clap_toggle(self):
        if not self.premium:
            self.clap_switch.deselect()
            self.require_premium("Double-clap control")
            return
        if self.clap_switch.get():
            self.clap = ClapListener(lambda: self.cmd_q.put(self.toggle_power), lambda: self.clap_sens.get())
            self.clap.start()
        elif self.clap:
            self.clap.running = False
            self.clap = None
            self.clap_bar.set(0)

    # ---- styling ------------------------------------------------------------
    def restyle(self, root):
        """Give every widget under root the dark glass look (safe to call again on new widgets)."""
        MAP = {"#5b2a86": ACCENT, "#7a3bb0": "#a78bfa", "#444": GLASS2, "#555": EDGE,
               "#1e8e4e": "#10b981", "#27ae60": "#34d399", "#a93226": "#e11d48",
               "#c0392b": "#fb7185", "#b9770e": "#f59e0b"}
        rgb_bars = ("#e74c3c", "#2ecc71", "#3498db")

        def fix(c):
            if isinstance(c, ctk.CTkSegmentedButton):
                c.configure(fg_color="#0e0e1c", selected_color=ACCENT, selected_hover_color="#7c4de0",
                            unselected_color=GLASS2, unselected_hover_color=EDGE, corner_radius=12)
            elif isinstance(c, ctk.CTkTabview):
                c.configure(fg_color=GLASS, corner_radius=20, border_width=1, border_color=EDGE,
                            segmented_button_fg_color="#0e0e1c", segmented_button_selected_color=ACCENT,
                            segmented_button_selected_hover_color="#7c4de0",
                            segmented_button_unselected_color=GLASS2,
                            segmented_button_unselected_hover_color=EDGE)
            elif isinstance(c, ctk.CTkSlider):
                kw = {"button_color": ACCENT2, "button_hover_color": "#67e8f9", "fg_color": GLASS2}
                pc = c.cget("progress_color")
                if not (isinstance(pc, str) and pc.lower() in rgb_bars):
                    kw["progress_color"] = ACCENT
                c.configure(**kw)
            elif isinstance(c, ctk.CTkSwitch):
                c.configure(progress_color=ACCENT, button_color="#e5e7eb", button_hover_color="#ffffff",
                            fg_color=GLASS2)
            elif isinstance(c, ctk.CTkProgressBar):
                c.configure(progress_color=ACCENT2, fg_color=GLASS2)
            elif isinstance(c, ctk.CTkOptionMenu):
                c.configure(fg_color=GLASS2, button_color=ACCENT, button_hover_color="#7c4de0",
                            dropdown_fg_color=GLASS2, corner_radius=10)
            elif isinstance(c, ctk.CTkEntry):
                c.configure(fg_color="#0e0e1c", border_color=EDGE, corner_radius=10)
            elif isinstance(c, ctk.CTkTextbox):
                c.configure(fg_color="#0e0e1c", corner_radius=12)
            elif isinstance(c, ctk.CTkFrame):
                if isinstance(c.cget("fg_color"), (list, tuple)):
                    c.configure(fg_color=GLASS, corner_radius=16, border_width=1, border_color=EDGE)
            elif isinstance(c, ctk.CTkButton):
                kw = {"corner_radius": 12}
                fg, hv = c.cget("fg_color"), c.cget("hover_color")
                if isinstance(fg, (list, tuple)):
                    kw["fg_color"] = ACCENT
                elif isinstance(fg, str) and fg.lower() in MAP:
                    kw["fg_color"] = MAP[fg.lower()]
                if isinstance(hv, (list, tuple)):
                    kw["hover_color"] = "#7c4de0"
                elif isinstance(hv, str) and hv.lower() in MAP:
                    kw["hover_color"] = MAP[hv.lower()]
                c.configure(**kw)

        def walk(w):
            for c in w.winfo_children():
                try:
                    fix(c)
                except Exception:
                    pass
                walk(c)
        walk(root)

    def save_settings(self):
        try:
            CFG["brightness"] = round(float(self.bright.get()), 3)
            CFG["color"] = [int(v) for v in self.ble.base]
            save_cfg(CFG)
        except Exception:
            pass

    # ---- first-run setup: find / choose the light --------------------------------
    def build_setup(self):
        self.setup = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        card = glass(self.setup)
        card.place(relx=0.5, rely=0.5, anchor="center")
        ctk.CTkLabel(card, text="Neon Studio", font=ctk.CTkFont(size=36, weight="bold"),
                     text_color=ACCENT2).pack(padx=60, pady=(30, 0))
        ctk.CTkLabel(card, justify="center", text_color="gray",
                     text="Let's find your light.\nTurn it on, keep it near your PC, and close any phone app "
                          "that is connected to it.").pack(pady=(6, 14))
        self.scan_btn = ctk.CTkButton(card, text="🔍  Scan for lights", height=46, width=320,
                                      font=ctk.CTkFont(size=15, weight="bold"), command=self.start_scan)
        self.scan_btn.pack(pady=6)
        self.scan_lbl = ctk.CTkLabel(card, text="", text_color="gray", wraplength=480)
        self.scan_lbl.pack(pady=2)
        self.scan_list = ctk.CTkScrollableFrame(card, width=480, height=190, fg_color="transparent")
        self.scan_list.pack(padx=24, pady=6)
        ctk.CTkLabel(card, text="or type the address yourself", text_color="gray").pack(pady=(8, 2))
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(pady=(0, 4))
        self.mac_entry = ctk.CTkEntry(row, width=300, placeholder_text="AA:BB:CC:DD:EE:FF")
        self.mac_entry.pack(side="left", padx=4)
        ctk.CTkButton(row, text="Connect", width=100, command=self.use_manual).pack(side="left", padx=4)
        self.setup_err = ctk.CTkLabel(card, text="", text_color="#fb7185")
        self.setup_err.pack()
        ctk.CTkButton(card, text="Cancel", width=100, fg_color="transparent", border_width=1,
                      border_color=EDGE, text_color="gray", command=self.hide_setup).pack(pady=(4, 26))
        self.restyle(self.setup)

    def show_setup(self):
        self.mac_entry.delete(0, "end")
        if self.ble.mac:
            self.mac_entry.insert(0, self.ble.mac)
        self.setup_err.configure(text="")
        self.setup.place(x=0, y=0, relwidth=1, relheight=1)
        self.setup.lift()

    def hide_setup(self):
        self.setup.place_forget()

    def start_scan(self):
        self.scan_btn.configure(state="disabled", text="Scanning…")
        self.scan_lbl.configure(text="Looking for Bluetooth devices (about 6 seconds)…")
        for w in self.scan_list.winfo_children():
            w.destroy()
        self.ble.scan_result = None
        self.ble.submit(self.ble.scan())
        self.after(500, self.check_scan)

    def check_scan(self):
        rows = self.ble.scan_result
        if rows is None:
            self.after(400, self.check_scan)
            return
        self.scan_btn.configure(state="normal", text="🔍  Scan again")
        if not rows:
            self.scan_lbl.configure(text="No devices found. Is Bluetooth on, and is the light switched on and close by?")
            return
        self.scan_lbl.configure(text=f"Found {len(rows)} devices. ⭐ = looks like an LED controller. Pick yours:")
        for name, addr, rssi in rows:
            star = "⭐ " if looks_like_light(name) else ""
            ctk.CTkButton(self.scan_list, text=f"{star}{name}   ·   {addr}   ·   {rssi} dBm", anchor="w",
                          height=38, fg_color=GLASS2, hover_color=EDGE,
                          command=lambda a=addr: self.use_device(a)).pack(fill="x", pady=3)
        self.restyle(self.scan_list)

    def use_manual(self):
        raw = self.mac_entry.get().strip().upper()
        mac = raw.replace("-", ":")
        if re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", mac):
            self.use_device(mac)
        elif re.fullmatch(r"[0-9A-F]{8}(-[0-9A-F]{4}){3}-[0-9A-F]{12}", raw):   # macOS uses UUIDs
            self.use_device(raw)
        else:
            self.setup_err.configure(text="That doesn't look like an address. Example: AA:BB:CC:DD:EE:FF")

    def use_device(self, addr):
        self.ble.disconnect()
        self.ble.mac = addr
        CFG["mac"] = addr
        save_cfg(CFG)
        self.hide_setup()
        self.after(700, self.ble.connect)

    # ---- Pattern Studio tab ----------------------------------------------
    def build_studio_tab(self, tab):
        ctk.CTkLabel(tab, text="Design your own sequence. Each step fades to its colour, then holds.",
                     anchor="w").pack(fill="x", padx=12, pady=(6, 4))

        self.step_frame = ctk.CTkScrollableFrame(tab, height=270)
        self.step_frame.pack(fill="both", expand=True, padx=8, pady=4)

        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.pack(fill="x", padx=8, pady=4)
        ctk.CTkButton(bar, text="+ Add step", width=100, command=self.add_step).pack(side="left", padx=3)
        ctk.CTkButton(bar, text="🎲 Random palette", width=130, fg_color="#5b2a86",
                      hover_color="#7a3bb0", command=self.random_palette).pack(side="left", padx=3)
        ctk.CTkButton(bar, text="Clear", width=70, fg_color="#444", hover_color="#555",
                      command=self.clear_steps).pack(side="left", padx=3)
        self.loop_switch = ctk.CTkSwitch(bar, text="Loop")
        self.loop_switch.select()
        self.loop_switch.pack(side="left", padx=14)
        ctk.CTkButton(bar, text="▶  Play", width=100, fg_color="#1e8e4e", hover_color="#27ae60",
                      command=self.play_studio).pack(side="right", padx=3)

        save = ctk.CTkFrame(tab, fg_color="transparent")
        save.pack(fill="x", padx=8, pady=(8, 2))
        self.name_entry = ctk.CTkEntry(save, width=170, placeholder_text="Pattern name")
        self.name_entry.pack(side="left", padx=3)
        ctk.CTkButton(save, text="💾 Save", width=80, command=self.save_pattern).pack(side="left", padx=3)
        self.pattern_menu = ctk.CTkOptionMenu(save, values=["—"], width=190)
        self.pattern_menu.pack(side="left", padx=(16, 3))
        ctk.CTkButton(save, text="Load", width=70, command=self.load_pattern).pack(side="left", padx=3)
        ctk.CTkButton(save, text="Delete", width=70, fg_color="#a93226", hover_color="#c0392b",
                      command=self.delete_pattern).pack(side="left", padx=3)

    def add_step(self, color=None, hold=1.0, fade=1.0, render=True):
        if color is None:
            color = hsv255(random.random())
        self.rows.append({"color": tuple(int(v) for v in color),
                          "hold": ctk.StringVar(value=str(hold)),
                          "fade": ctk.StringVar(value=str(fade))})
        if render:
            self.render_steps()

    def clear_steps(self):
        self.rows = []
        self.render_steps()

    def random_palette(self):
        base = random.random()
        self.rows = []
        for i in range(random.randint(3, 5)):
            self.add_step(hsv255(base + i * random.uniform(0.12, 0.3)), 0.8, 1.5, render=False)
        self.render_steps()

    def render_steps(self):
        for w in self.step_frame.winfo_children():
            w.destroy()
        for i, row in enumerate(self.rows):
            f = ctk.CTkFrame(self.step_frame)
            f.pack(fill="x", pady=3, padx=4)
            ctk.CTkLabel(f, text=str(i + 1), width=26).pack(side="left", padx=(8, 2))
            ctk.CTkButton(f, text="", width=70, height=30, fg_color=hexc(row["color"]),
                          hover_color=hexc(row["color"]),
                          command=lambda i=i: self.pick_step_color(i)).pack(side="left", padx=8)
            ctk.CTkLabel(f, text="hold (s)").pack(side="left")
            ctk.CTkEntry(f, width=60, textvariable=row["hold"]).pack(side="left", padx=(4, 12))
            ctk.CTkLabel(f, text="fade (s)").pack(side="left")
            ctk.CTkEntry(f, width=60, textvariable=row["fade"]).pack(side="left", padx=4)
            ctk.CTkButton(f, text="✕", width=30, fg_color="#a93226", hover_color="#c0392b",
                          command=lambda i=i: self.del_step(i)).pack(side="right", padx=6)
            ctk.CTkButton(f, text="▼", width=30, fg_color="#444", hover_color="#555",
                          command=lambda i=i: self.move_step(i, 1)).pack(side="right")
            ctk.CTkButton(f, text="▲", width=30, fg_color="#444", hover_color="#555",
                          command=lambda i=i: self.move_step(i, -1)).pack(side="right", padx=2)

        self.restyle(self.step_frame)

    def pick_step_color(self, i):
        rgb, _ = colorchooser.askcolor(color=hexc(self.rows[i]["color"]), title=f"Step {i + 1} colour")
        if rgb:
            self.rows[i]["color"] = tuple(int(v) for v in rgb)
            self.render_steps()

    def del_step(self, i):
        del self.rows[i]
        self.render_steps()

    def move_step(self, i, d):
        j = i + d
        if 0 <= j < len(self.rows):
            self.rows[i], self.rows[j] = self.rows[j], self.rows[i]
            self.render_steps()

    def get_steps(self):
        def num(var, default):
            try:
                return max(0.0, min(60.0, float(var.get())))
            except ValueError:
                return default
        return [(r["color"], num(r["hold"], 1.0), num(r["fade"], 1.0)) for r in self.rows]

    def play_studio(self):
        if not self.require_premium("Pattern studio"):
            return
        steps = self.get_steps()
        if not steps:
            messagebox.showinfo("Empty pattern", "Add at least one step first.")
            return
        loop = bool(self.loop_switch.get())
        self.mode = "pattern"
        self.ble.play(lambda: self.ble.play_steps(steps, loop))

    # ---- saving / loading -------------------------------------------------
    def load_saved(self):
        try:
            return json.loads(PATTERN_FILE.read_text())
        except Exception:
            return {}

    def write_saved(self):
        PATTERN_FILE.write_text(json.dumps(self.saved, indent=2))

    def refresh_pattern_menu(self):
        names = ["★ " + n for n in PRESETS] + list(self.saved)
        self.pattern_menu.configure(values=names)
        self.pattern_menu.set(names[0])

    def save_pattern(self):
        name = self.name_entry.get().strip()
        if not name:
            messagebox.showinfo("Name needed", "Type a name for your pattern first.")
            return
        self.saved[name] = [[list(c), h, f] for c, h, f in self.get_steps()]
        self.write_saved()
        self.refresh_pattern_menu()
        self.pattern_menu.set(name)

    def load_pattern(self):
        choice = self.pattern_menu.get()
        if choice.startswith("★ "):
            steps = PRESETS.get(choice[2:])
        else:
            steps = self.saved.get(choice)
        if not steps:
            return
        self.rows = []
        for c, h, f in steps:
            self.add_step(c, h, f, render=False)
        self.render_steps()
        if not choice.startswith("★ "):
            self.name_entry.delete(0, "end")
            self.name_entry.insert(0, choice)

    def delete_pattern(self):
        choice = self.pattern_menu.get()
        if choice in self.saved and messagebox.askyesno("Delete", f"Delete '{choice}'?"):
            del self.saved[choice]
            self.write_saved()
            self.refresh_pattern_menu()


if __name__ == "__main__":
    import os
    import subprocess
    import sys
    import traceback

    # On Windows: relaunch without the black console window, so closing it can't kill the app
    if os.name == "nt" and "--child" not in sys.argv and sys.executable.lower().endswith("python.exe"):
        pyw = sys.executable[:-10] + "pythonw.exe"
        if os.path.exists(pyw):
            DETACHED_PROCESS, NEW_GROUP = 0x00000008, 0x00000200
            subprocess.Popen([pyw, os.path.abspath(__file__), "--child"],
                             creationflags=DETACHED_PROCESS | NEW_GROUP, close_fds=True)
            sys.exit(0)

    try:
        App().mainloop()
    except Exception:
        # no console to show errors in, so save them next to the script
        Path(__file__).with_name("neon_studio_error.log").write_text(traceback.format_exc())
        raise
