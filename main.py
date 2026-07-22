# -*- coding: utf-8 -*-
"""
Fast Download Manager - Android (Kivy), single-file build
===========================================================
Everything (download engine + UI) lives in this one file on purpose, so
there is nothing else to import and nothing else that can go missing when
this gets built into an APK on GitHub Actions.

Fixes vs the previous Kivy version:
 1) UI bug (rows squashed / buttons overlapping / nothing clickable):
    the row widget's KV rule was missing `orientation: 'vertical'`, so
    Kivy stacked the header, progress bar, detail text and buttons
    HORIZONTALLY on top of each other instead of vertically. That is
    what made everything look broken and unusable. Fixed below, plus
    the whole UI was rebuilt with bigger touch targets and readable
    font sizes.
 2) "Download finishes but the app never shows Completed, and the file
    is not on the phone":
       a) Files were being saved inside the app's PRIVATE storage
          folder, which does exist but is invisible in the phone's
          normal Downloads / file manager. Now the app saves into the
          real public Downloads folder (Download/FastDownloadManager)
          on Android 11+ using the "All files access" permission, and
          asks for that permission on first launch.
       b) For YouTube, the engine was requesting separate video+audio
          streams merged into mp4 with ffmpeg. Most Android builds do
          not bundle an ffmpeg binary, so the merge step silently
          failed after the download reached 100%, and the task never
          became Completed. The engine now checks whether ffmpeg is
          actually available and, if not, picks an already-muxed
          "progressive" format instead so no merge step is needed.
"""
import os
import sys
import json
import time
import shutil
import subprocess
import threading
import traceback
from urllib.parse import urlparse, unquote

from kivy.app import App
from kivy.lang import Builder
from kivy.clock import Clock
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.popup import Popup
from kivy.properties import StringProperty, NumericProperty, ListProperty
from kivy.core.clipboard import Clipboard
from kivy.core.window import Window
from kivy.utils import platform

try:
    import requests
    from requests.adapters import HTTPAdapter
    try:
        from urllib3.util.retry import Retry
    except Exception:
        from requests.packages.urllib3.util.retry import Retry
    HAS_REQUESTS = True
except Exception:
    HAS_REQUESTS = False

try:
    import yt_dlp
    HAS_YTDLP = True
except Exception:
    HAS_YTDLP = False

try:
    import gallery_dl  # noqa: F401
    HAS_GALLERYDL = True
except Exception:
    HAS_GALLERYDL = False

HAS_FFMPEG = shutil.which("ffmpeg") is not None


# ============================================================================
#  Small helpers
# ============================================================================
def human_size(n):
    if not n or n <= 0:
        return "0 B"
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


def human_speed(bps):
    return human_size(bps) + "/s" if bps and bps > 0 else "-"


def human_time(sec):
    if not sec or sec <= 0:
        return "-"
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def safe_filename(name):
    keep = " .-_()[]{}"
    name = "".join(c for c in name if c.isalnum() or c in keep or ord(c) > 127)
    return name.strip() or "download"


def is_media_site(url):
    u = url.lower()
    hosts = ("youtube.com", "youtu.be", "instagram.com", "pinterest.", "pin.it",
             "tiktok.com", "facebook.com", "fb.watch", "twitter.com", "x.com",
             "vimeo.com", "dailymotion.com", "soundcloud.com", "twitch.tv")
    return any(h in u for h in hosts)


def site_of(url):
    u = url.lower()
    table = (
        ("youtube.com", "YouTube"), ("youtu.be", "YouTube"),
        ("instagram.com", "Instagram"), ("tiktok.com", "TikTok"),
        ("twitter.com", "X"), ("x.com", "X"),
        ("facebook.com", "Facebook"), ("fb.watch", "Facebook"),
        ("vimeo.com", "Vimeo"),
        ("pinterest.", "Pinterest"), ("pin.it", "Pinterest"),
        ("soundcloud.com", "SoundCloud"), ("twitch.tv", "Twitch"),
    )
    for key, name in table:
        if key in u:
            return name
    return None


class Status:
    QUEUED = "queued"; DOWNLOADING = "downloading"; PAUSED = "paused"
    COMPLETED = "completed"; ERROR = "error"; CANCELED = "canceled"


# ============================================================================
#  Android storage helpers
# ============================================================================
def request_android_permissions():
    """Ask for storage access on first launch. On Android 11+ the only way
    to reliably write into the public Download folder is the special
    "All files access" permission, which cannot be granted through the
    normal runtime-permission popup and instead opens a system settings
    screen. Everything here is best-effort and silently ignored on
    desktop or if anything is unavailable."""
    if platform != "android":
        return
    try:
        from android.permissions import request_permissions, Permission
        request_permissions([Permission.INTERNET,
                              Permission.WRITE_EXTERNAL_STORAGE,
                              Permission.READ_EXTERNAL_STORAGE])
    except Exception:
        pass
    try:
        from jnius import autoclass
        Environment = autoclass("android.os.Environment")
        if not Environment.isExternalStorageManager():
            Intent = autoclass("android.content.Intent")
            Settings = autoclass("android.provider.Settings")
            Uri = autoclass("android.net.Uri")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            activity = PythonActivity.mActivity
            intent = Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION)
            intent.setData(Uri.fromParts("package", activity.getPackageName(), None))
            activity.startActivity(intent)
    except Exception:
        pass


def public_downloads_dir():
    """Best real Downloads folder we can find. Falls back to app-private
    storage (always writable, no permission needed) if the public one is
    not reachable yet (e.g. permission not granted)."""
    if platform == "android":
        try:
            from jnius import autoclass
            Environment = autoclass("android.os.Environment")
            base = Environment.getExternalStoragePublicDirectory(
                Environment.DIRECTORY_DOWNLOADS).getAbsolutePath()
            d = os.path.join(base, "FastDownloadManager")
            os.makedirs(d, exist_ok=True)
            # quick write test - if this fails, fall through to private dir
            test = os.path.join(d, ".wtest")
            with open(test, "w") as f:
                f.write("1")
            os.remove(test)
            return d
        except Exception:
            pass
    return None


def scan_file(path):
    """Tell Android's media scanner about a new file so it shows up
    immediately in the Files app / gallery instead of after a reboot."""
    if platform != "android" or not path or not os.path.exists(path):
        return
    try:
        from jnius import autoclass
        MediaScannerConnection = autoclass("android.media.MediaScannerConnection")
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        MediaScannerConnection.scanFile(PythonActivity.mActivity, [path], None, None)
    except Exception:
        pass


# ============================================================================
#  Task model
# ============================================================================
class DownloadTask:
    _counter = 0

    def __init__(self, url, save_dir, quality="best", tid=None,
                 filename="", kind=None):
        DownloadTask._counter += 1
        self.id = tid or f"{int(time.time()*1000)}_{DownloadTask._counter}"
        self.url = url
        self.save_dir = save_dir
        self.quality = quality
        self.kind = kind or ("media" if is_media_site(url) else "direct")
        self.filename = filename
        self.filepath = ""
        self.status = Status.QUEUED
        self.total = 0
        self.downloaded = 0
        self.speed = 0
        self.eta = 0
        self.error = ""
        self.created = time.time()
        self._pause = threading.Event(); self._pause.set()
        self._cancel = False
        self._thread = None

    def to_dict(self):
        return {k: getattr(self, k) for k in
                ("id", "url", "save_dir", "quality", "kind", "filename",
                 "filepath", "status", "total", "downloaded", "error", "created")}

    @staticmethod
    def from_dict(d):
        t = DownloadTask(d["url"], d.get("save_dir", ""),
                         d.get("quality", "best"), tid=d.get("id"),
                         filename=d.get("filename", ""), kind=d.get("kind"))
        t.filepath = d.get("filepath", "")
        t.total = d.get("total", 0)
        t.downloaded = d.get("downloaded", 0)
        t.error = d.get("error", "")
        t.created = d.get("created", time.time())
        st = d.get("status", Status.QUEUED)
        if st in (Status.DOWNLOADING, Status.QUEUED):
            st = Status.PAUSED
        t.status = st
        return t

    @property
    def progress(self):
        return min(self.downloaded / self.total, 1.0) if self.total > 0 else 0.0


# ============================================================================
#  Engine
# ============================================================================
class Engine:
    def __init__(self, config, on_update):
        self.config = config
        self.on_update = on_update
        self._sem = threading.Semaphore(config.get("max_concurrent", 3))
        self.session = requests.Session() if HAS_REQUESTS else None
        if self.session is not None:
            retry = Retry(total=3, backoff_factor=0.5,
                          status_forcelist=[429, 500, 502, 503, 504])
            adapter = HTTPAdapter(pool_connections=32, pool_maxsize=32,
                                  max_retries=retry)
            self.session.mount("http://", adapter)
            self.session.mount("https://", adapter)

    def set_concurrency(self, n):
        self._sem = threading.Semaphore(max(1, int(n)))

    def start(self, task):
        if task.status == Status.DOWNLOADING:
            return
        task._cancel = False
        task._pause.set()
        task.error = ""
        task.status = Status.QUEUED
        self.on_update(task)
        task._thread = threading.Thread(target=self._run, args=(task,), daemon=True)
        task._thread.start()

    def pause(self, task):
        if task.status == Status.DOWNLOADING:
            task._pause.clear()
            task.status = Status.PAUSED
            self.on_update(task)

    def resume(self, task):
        self.start(task)

    def cancel(self, task):
        task._cancel = True
        task._pause.set()
        task.status = Status.CANCELED
        self.on_update(task)

    def _run(self, task):
        with self._sem:
            if task._cancel:
                return
            try:
                task.status = Status.DOWNLOADING
                self.on_update(task)
                if task.kind == "media":
                    self._media(task)
                else:
                    try:
                        self._direct(task)
                    except Exception as de:
                        if HAS_YTDLP and not task._cancel:
                            task.kind = "media"
                            task.error = ""
                            self._media(task)
                        else:
                            raise de
            except Exception as e:
                if not task._cancel:
                    task.status = Status.ERROR
                    task.error = str(e) or e.__class__.__name__
                    traceback.print_exc()
            finally:
                self.on_update(task)

    # ---- direct (non-media) links ----
    def _direct(self, task):
        if not HAS_REQUESTS:
            raise RuntimeError("requests not installed")
        headers_ua = {"User-Agent": "Mozilla/5.0 (PyDownloader)"}
        head = None
        try:
            head = self.session.head(task.url, allow_redirects=True, timeout=15,
                                     headers=headers_ua)
        except Exception:
            pass

        if not task.filename:
            task.filename = self._name(task.url, head)
        task.filepath = os.path.join(task.save_dir, task.filename)
        os.makedirs(task.save_dir, exist_ok=True)

        total, ranges_ok = 0, False
        if head is not None and head.status_code < 400:
            total = int(head.headers.get("Content-Length", 0) or 0)
            ranges_ok = head.headers.get("Accept-Ranges", "").lower() == "bytes"
            ctype = head.headers.get("Content-Type", "")
            if "text/html" in ctype and is_media_site(task.url):
                raise RuntimeError("html page -> media")
        task.total = total
        self.on_update(task)

        conns = int(self.config.get("connections_per_file", 8))
        if ranges_ok and total > 512 * 1024 and conns > 1:
            self._segmented(task, total, conns, headers_ua)
        else:
            self._single(task, headers_ua)

    def _name(self, url, head):
        if head is not None:
            cd = head.headers.get("Content-Disposition", "")
            if "filename=" in cd:
                nm = cd.split("filename=")[-1].strip('"; ')
                if nm:
                    return safe_filename(unquote(nm))
        nm = os.path.basename(urlparse(url).path)
        return safe_filename(unquote(nm) or "download.bin")

    def _single(self, task, ua):
        part = task.filepath + ".part"
        resume_from = os.path.getsize(part) if os.path.exists(part) else 0
        headers = dict(ua)
        mode = "wb"
        if resume_from > 0:
            headers["Range"] = f"bytes={resume_from}-"
            mode = "ab"
        task.downloaded = resume_from
        with self.session.get(task.url, stream=True, headers=headers, timeout=30) as r:
            r.raise_for_status()
            if not task.total:
                task.total = int(r.headers.get("Content-Length", 0) or 0) + resume_from
            last, last_b = time.time(), task.downloaded
            with open(part, mode) as f:
                for chunk in r.iter_content(1024 * 256):
                    if task._cancel:
                        return
                    if not task._pause.is_set():
                        return
                    if chunk:
                        f.write(chunk)
                        task.downloaded += len(chunk)
                        now = time.time()
                        if now - last >= 0.4:
                            task.speed = (task.downloaded - last_b) / (now - last)
                            task.eta = ((task.total - task.downloaded) / task.speed
                                        if task.speed and task.total else 0)
                            last, last_b = now, task.downloaded
                            self.on_update(task)
        self._finalize(task, part)

    def _segmented(self, task, total, conns, ua):
        part = task.filepath + ".part"
        seg = total // conns
        ranges = [(i * seg, total - 1 if i == conns - 1 else (i + 1) * seg - 1)
                  for i in range(conns)]
        with open(part, "wb") as f:
            f.truncate(total)
        lock = threading.Lock()
        task.downloaded = 0
        errors = []

        def worker(start, end):
            try:
                h = dict(ua); h["Range"] = f"bytes={start}-{end}"
                with self.session.get(task.url, stream=True, headers=h, timeout=30) as r:
                    r.raise_for_status()
                    pos = start
                    with open(part, "r+b") as f:
                        for chunk in r.iter_content(1024 * 256):
                            if task._cancel or not task._pause.is_set():
                                return
                            if chunk:
                                f.seek(pos); f.write(chunk); pos += len(chunk)
                                with lock:
                                    task.downloaded += len(chunk)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=r, daemon=True) for r in ranges]
        for t in threads:
            t.start()
        last, last_b = time.time(), 0
        while any(t.is_alive() for t in threads):
            time.sleep(0.4)
            now = time.time()
            task.speed = (task.downloaded - last_b) / max(now - last, 1e-6)
            task.eta = ((task.total - task.downloaded) / task.speed
                        if task.speed and task.total else 0)
            last, last_b = now, task.downloaded
            self.on_update(task)
        for t in threads:
            t.join()
        if task._cancel or not task._pause.is_set():
            return
        if errors:
            raise errors[0]
        self._finalize(task, part)

    def _finalize(self, task, part):
        if task._cancel or not task._pause.is_set():
            return
        try:
            if os.path.exists(task.filepath):
                os.remove(task.filepath)
            shutil.move(part, task.filepath)
        except Exception:
            pass
        task.downloaded = task.total or task.downloaded
        task.speed = task.eta = 0
        task.status = Status.COMPLETED
        self.on_update(task)
        scan_file(task.filepath)

    # ---- media (yt-dlp / gallery-dl) ----
    def _media(self, task):
        if not HAS_YTDLP:
            raise RuntimeError("yt-dlp not installed")
        os.makedirs(task.save_dir, exist_ok=True)
        outtmpl = os.path.join(task.save_dir, "%(title).80s.%(ext)s")
        q = task.quality
        last = [time.time()]

        def hook(d):
            if task._cancel:
                raise yt_dlp.utils.DownloadCancelled()
            task._pause.wait()
            if d["status"] == "downloading":
                task.status = Status.DOWNLOADING
                task.total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                task.downloaded = d.get("downloaded_bytes", 0)
                task.speed = d.get("speed") or 0
                task.eta = d.get("eta") or 0
                fn = d.get("filename")
                if fn:
                    task.filename = os.path.basename(fn)
                now = time.time()
                if now - last[0] >= 0.4:
                    self.on_update(task); last[0] = now
            elif d["status"] == "finished":
                task.filepath = d.get("filename", "")
                self.on_update(task)

        opts = {
            "outtmpl": outtmpl, "progress_hooks": [hook],
            "continuedl": True, "noprogress": True, "quiet": True,
            "no_warnings": True, "retries": 5, "fragment_retries": 5,
            "concurrent_fragment_downloads":
                max(1, int(self.config.get("connections_per_file", 8))),
            "http_headers": {"User-Agent": "Mozilla/5.0 (PyDownloader)"},
        }

        if HAS_FFMPEG:
            # ffmpeg is available (typically desktop): we can request the
            # true best video+audio and merge/convert as needed.
            if q == "audio":
                opts["format"] = "bestaudio/best"
                opts["postprocessors"] = [{"key": "FFmpegExtractAudio",
                                           "preferredcodec": "mp3",
                                           "preferredquality": "192"}]
            elif q in ("1080", "720", "480"):
                opts["format"] = (f"bestvideo[height<={q}]+bestaudio/"
                                   f"best[height<={q}]/best")
                opts["merge_output_format"] = "mp4"
            else:
                opts["format"] = "bestvideo+bestaudio/best"
                opts["merge_output_format"] = "mp4"
        else:
            # No ffmpeg on this device (typical on Android): only pick
            # formats that are already a single muxed file, so no merge
            # or audio-conversion step is ever needed - this is what was
            # causing downloads to reach 100% and then never finish.
            if q == "audio":
                opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
            elif q in ("1080", "720", "480"):
                opts["format"] = (f"best[height<={q}][ext=mp4]/"
                                   f"best[height<={q}]/best")
            else:
                opts["format"] = "best[ext=mp4]/best"

        try:
            info = self._extract_with_fallback(task.url, opts)
            if not task.filename and info:
                task.filename = info.get("title", "media")
        except Exception as yt_err:
            u = task.url.lower()
            if (not task._cancel and HAS_GALLERYDL and
                    ("instagram.com" in u or "pinterest." in u
                     or "pin.it" in u)):
                try:
                    self._gallery_dl(task)
                    return
                except Exception as gd_err:
                    yt_err = gd_err
            msg = str(yt_err).lower()
            if not task._cancel and ("sign in" in msg or "not a bot" in msg):
                raise RuntimeError(
                    "YouTube is asking to confirm you're not a bot. Set a "
                    "cookies file in Settings, then retry.") from None
            raise yt_err
        if not task._cancel and task._pause.is_set():
            task.status = Status.COMPLETED
            task.downloaded = task.total or task.downloaded
            task.speed = task.eta = 0
            self.on_update(task)
            scan_file(task.filepath)

    def _extract_with_fallback(self, url, opts):
        attempts = [{}, {"player_client": ["android"]}, {"player_client": ["ios"]}]
        cookies_file = self.config.get("cookies_file", "").strip()
        last_err = None
        for i, extra in enumerate(attempts):
            o = dict(opts)
            if extra:
                o["extractor_args"] = {"youtube": extra}
            if i == len(attempts) - 1 and cookies_file and os.path.exists(cookies_file):
                o["cookiefile"] = cookies_file
            try:
                with yt_dlp.YoutubeDL(o) as ydl:
                    return ydl.extract_info(url, download=True)
            except Exception as e:
                last_err = e
                continue
        raise last_err

    def _gallery_dl(self, task):
        os.makedirs(task.save_dir, exist_ok=True)
        task.status = Status.DOWNLOADING
        self.on_update(task)
        cmd = [sys.executable, "-m", "gallery_dl", "-D", task.save_dir]
        cookies_file = self.config.get("cookies_file", "").strip()
        if cookies_file and os.path.exists(cookies_file):
            cmd += ["--cookies", cookies_file]
        cmd.append(task.url)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if task._cancel:
            return
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or
                                "gallery-dl failed")[:300])
        task.status = Status.COMPLETED
        task.downloaded = task.total or task.downloaded
        task.speed = task.eta = 0
        self.on_update(task)
        scan_file(task.filepath)


# ============================================================================
#  UI
# ============================================================================
KV = """
#:import dp kivy.metrics.dp

<Chip@Label>:
    size_hint: None, None
    size: self.texture_size[0] + dp(18), dp(26)
    padding: dp(9), dp(2)
    color: 0.95, 0.95, 1, 1
    font_size: '12sp'
    bold: True
    canvas.before:
        Color:
            rgba: 0.42, 0.3, 0.94, 1
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(13)]

<RowWidget>:
    orientation: 'vertical'
    size_hint_y: None
    height: self.minimum_height
    padding: dp(14)
    spacing: dp(8)
    canvas.before:
        Color:
            rgba: 0.11, 0.10, 0.18, 1
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(14)]

    BoxLayout:
        size_hint_y: None
        height: dp(26)
        spacing: dp(8)
        Chip:
            text: root.site_name or ""
            opacity: 1 if root.site_name else 0
            size_hint_x: None
        Label:
            text: root.filename
            color: 0.95, 0.94, 1, 1
            font_size: '15sp'
            halign: 'left'
            valign: 'middle'
            text_size: self.size
            shorten: True
            shorten_from: 'right'
        Label:
            text: root.status_text
            color: root.status_color
            font_size: '13sp'
            bold: True
            size_hint_x: None
            width: dp(104)
            halign: 'right'
            valign: 'middle'
            text_size: self.size

    ProgressBar:
        max: 1
        value: root.progress
        size_hint_y: None
        height: dp(10)

    Label:
        text: root.detail_text
        size_hint_y: None
        height: dp(20)
        color: 0.68, 0.66, 0.8, 1
        font_size: '12sp'
        halign: 'left'
        valign: 'middle'
        text_size: self.size
        shorten: True

    BoxLayout:
        size_hint_y: None
        height: dp(46)
        spacing: dp(8)
        Button:
            text: root.action_label
            font_size: '14sp'
            bold: True
            background_normal: ''
            background_down: ''
            background_color: 0.42, 0.3, 0.94, 1
            on_release: root.on_action()
        Button:
            text: 'Remove'
            font_size: '14sp'
            background_normal: ''
            background_down: ''
            background_color: 0.32, 0.29, 0.42, 1
            on_release: root.on_remove()

<SettingsPopupContent>:
    orientation: 'vertical'
    spacing: dp(14)
    padding: dp(6)

    Label:
        text: 'Max simultaneous downloads: %d' % int(conc_slider.value)
        color: 0.93, 0.92, 1, 1
        font_size: '14sp'
        size_hint_y: None
        height: dp(24)
    Slider:
        id: conc_slider
        min: 1
        max: 8
        step: 1
        value: root.max_concurrent
        size_hint_y: None
        height: dp(36)

    Label:
        text: 'Connections per file: %d' % int(conn_slider.value)
        color: 0.93, 0.92, 1, 1
        font_size: '14sp'
        size_hint_y: None
        height: dp(24)
    Slider:
        id: conn_slider
        min: 1
        max: 32
        step: 1
        value: root.connections_per_file
        size_hint_y: None
        height: dp(36)

    Label:
        text: 'Cookies file path (optional, fixes some YouTube errors)'
        color: 0.93, 0.92, 1, 1
        font_size: '13sp'
        size_hint_y: None
        height: dp(20)
        halign: 'left'
        text_size: self.size
    TextInput:
        id: cookies_input
        text: root.cookies_file
        multiline: False
        size_hint_y: None
        height: dp(44)
        background_color: 0.15, 0.14, 0.22, 1
        foreground_color: 0.93, 0.92, 1, 1
        padding: dp(10), dp(10)

    Widget:
        size_hint_y: 1

    Button:
        text: 'Save'
        size_hint_y: None
        height: dp(48)
        font_size: '15sp'
        bold: True
        background_normal: ''
        background_down: ''
        background_color: 0.42, 0.3, 0.94, 1
        on_release: root.on_save(int(conc_slider.value), int(conn_slider.value), cookies_input.text)

RootLayout:
    orientation: 'vertical'
    canvas.before:
        Color:
            rgba: 0.075, 0.067, 0.125, 1
        Rectangle:
            pos: self.pos
            size: self.size
    padding: dp(14)
    spacing: dp(12)

    BoxLayout:
        size_hint_y: None
        height: dp(44)
        Label:
            text: 'Fast Download Manager'
            font_size: '20sp'
            bold: True
            color: 0.95, 0.94, 1, 1
            halign: 'left'
            valign: 'middle'
            text_size: self.size
        Button:
            text: 'Settings'
            size_hint_x: None
            width: dp(110)
            font_size: '14sp'
            background_normal: ''
            background_down: ''
            background_color: 0.22, 0.2, 0.32, 1
            on_release: app.open_settings()

    BoxLayout:
        size_hint_y: None
        height: dp(50)
        spacing: dp(8)
        TextInput:
            id: url_input
            hint_text: 'Paste a YouTube / Instagram / Pinterest / any link...'
            multiline: False
            font_size: '14sp'
            background_color: 0.15, 0.14, 0.22, 1
            foreground_color: 0.95, 0.94, 1, 1
            hint_text_color: 0.55, 0.53, 0.65, 1
            padding: dp(12), dp(14)
        Button:
            text: 'Paste'
            size_hint_x: None
            width: dp(76)
            font_size: '13sp'
            background_normal: ''
            background_down: ''
            background_color: 0.22, 0.2, 0.32, 1
            on_release: url_input.text = app.paste_clipboard()

    BoxLayout:
        size_hint_y: None
        height: dp(50)
        spacing: dp(8)
        Spinner:
            id: quality
            text: 'best'
            values: ['best', '1080', '720', '480', 'audio']
            font_size: '14sp'
            background_normal: ''
            background_color: 0.18, 0.16, 0.26, 1
        Button:
            text: 'Download'
            font_size: '15sp'
            bold: True
            background_normal: ''
            background_down: ''
            background_color: 0.42, 0.3, 0.94, 1
            on_release: app.add_download(url_input.text, quality.text); url_input.text = ''

    ScrollView:
        do_scroll_x: False
        BoxLayout:
            id: rows_box
            orientation: 'vertical'
            size_hint_y: None
            height: self.minimum_height
            spacing: dp(10)
"""


STATUS_COLORS = {
    Status.QUEUED: (0.6, 0.6, 0.7, 1),
    Status.DOWNLOADING: (0.48, 0.65, 1, 1),
    Status.PAUSED: (1, 0.75, 0.38, 1),
    Status.COMPLETED: (0.24, 0.86, 0.6, 1),
    Status.ERROR: (1, 0.48, 0.52, 1),
    Status.CANCELED: (0.6, 0.6, 0.7, 1),
}


class RootLayout(BoxLayout):
    pass


class SettingsPopupContent(BoxLayout):
    max_concurrent = NumericProperty(3)
    connections_per_file = NumericProperty(8)
    cookies_file = StringProperty("")

    def __init__(self, app, popup, **kw):
        self._app = app
        self._popup = popup
        super().__init__(**kw)

    def on_save(self, max_concurrent, connections_per_file, cookies_file):
        self._app.save_settings(max_concurrent, connections_per_file, cookies_file)
        self._popup.dismiss()


class RowWidget(BoxLayout):
    filename = StringProperty("")
    status_text = StringProperty("")
    detail_text = StringProperty("")
    action_label = StringProperty("")
    progress = NumericProperty(0)
    site_name = StringProperty("")
    status_color = ListProperty(list(STATUS_COLORS[Status.QUEUED]))

    def __init__(self, task, app, **kw):
        super().__init__(**kw)
        self.task = task
        self.app = app
        self.site_name = site_of(task.url) or ""
        self.refresh()

    def refresh(self):
        t = self.task
        self.filename = t.filename or t.url
        self.status_text = t.status
        self.status_color = list(STATUS_COLORS.get(t.status, (1, 1, 1, 1)))
        self.progress = t.progress
        if t.status == Status.ERROR:
            self.detail_text = t.error[:90]
        elif t.status == Status.DOWNLOADING:
            self.detail_text = (f"{human_size(t.downloaded)} / {human_size(t.total)}  "
                                 f"{human_speed(t.speed)}  ETA {human_time(t.eta)}")
        elif t.status == Status.COMPLETED:
            self.detail_text = "Saved: " + human_size(t.total or t.downloaded)
        else:
            self.detail_text = ""
        self.action_label = ("Pause" if t.status == Status.DOWNLOADING
                              else "Retry" if t.status == Status.ERROR
                              else "Resume")

    def on_action(self):
        t = self.task
        if t.status == Status.DOWNLOADING:
            self.app.engine.pause(t)
        else:
            self.app.engine.start(t)

    def on_remove(self):
        self.app.remove_task(self.task)


class DownloaderApp(App):
    def build(self):
        self.title = "Fast Download Manager"
        Window.clearcolor = (0.075, 0.067, 0.125, 1)
        self.config_data = self._load_config()
        self.tasks = []
        self.rows = {}
        self.engine = Engine(self.config_data, self._on_engine_update)
        root = Builder.load_string(KV)
        self.rows_box = root.ids.rows_box
        self._load_history()
        Clock.schedule_once(lambda dt: request_android_permissions(), 0.5)
        return root

    # ---- storage paths ----
    def _base_dir(self):
        return self.user_data_dir

    def _download_dir(self):
        custom = (self.config_data.get("download_dir") or "").strip()
        if custom:
            os.makedirs(custom, exist_ok=True)
            return custom
        pub = public_downloads_dir()
        if pub:
            return pub
        d = os.path.join(self._base_dir(), "Downloads")
        os.makedirs(d, exist_ok=True)
        return d

    def _config_path(self):
        return os.path.join(self._base_dir(), "config.json")

    def _history_path(self):
        return os.path.join(self._base_dir(), "history.json")

    def _load_config(self):
        default = {
            "download_dir": "", "max_concurrent": 3,
            "connections_per_file": 8, "cookies_file": "",
        }
        try:
            with open(self._config_path(), "r", encoding="utf-8") as f:
                default.update(json.load(f))
        except Exception:
            pass
        return default

    def _save_config(self):
        try:
            with open(self._config_path(), "w", encoding="utf-8") as f:
                json.dump(self.config_data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _load_history(self):
        try:
            with open(self._history_path(), "r", encoding="utf-8") as f:
                items = json.load(f)
        except Exception:
            items = []
        for d in items:
            t = DownloadTask.from_dict(d)
            self.tasks.append(t)
            self._add_row(t)

    def _save_history(self):
        try:
            with open(self._history_path(), "w", encoding="utf-8") as f:
                json.dump([t.to_dict() for t in self.tasks], f,
                          ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ---- actions ----
    def paste_clipboard(self):
        try:
            return Clipboard.paste()
        except Exception:
            return ""

    def add_download(self, url, quality):
        url = (url or "").strip()
        if not url:
            return
        task = DownloadTask(url, self._download_dir(), quality=quality)
        self.tasks.append(task)
        self._add_row(task)
        self.engine.start(task)
        self._save_history()

    def _add_row(self, task):
        row = RowWidget(task, self)
        self.rows[task.id] = row
        self.rows_box.add_widget(row)

    def remove_task(self, task):
        self.engine.cancel(task)
        row = self.rows.pop(task.id, None)
        if row:
            self.rows_box.remove_widget(row)
        if task in self.tasks:
            self.tasks.remove(task)
        self._save_history()

    def _on_engine_update(self, task):
        Clock.schedule_once(lambda dt: self._update_row(task))

    def _update_row(self, task):
        row = self.rows.get(task.id)
        if row:
            row.refresh()
        self._save_history()

    def open_settings(self):
        popup = Popup(title="Settings", size_hint=(0.92, 0.7),
                       title_color=(0.95, 0.94, 1, 1),
                       separator_color=(0.42, 0.3, 0.94, 1))
        content = SettingsPopupContent(self, popup)
        content.max_concurrent = self.config_data.get("max_concurrent", 3)
        content.connections_per_file = self.config_data.get("connections_per_file", 8)
        content.cookies_file = self.config_data.get("cookies_file", "")
        popup.content = content
        popup.open()

    def save_settings(self, max_concurrent, connections_per_file, cookies_file):
        self.config_data["max_concurrent"] = max_concurrent
        self.config_data["connections_per_file"] = connections_per_file
        self.config_data["cookies_file"] = cookies_file.strip()
        self.engine.set_concurrency(max_concurrent)
        self._save_config()

    def on_stop(self):
        self._save_history()
        self._save_config()


if __name__ == "__main__":
    DownloaderApp().run()
