#!/usr/bin/env python3
"""GrousBot launcher for FalixNodes free plan.

Falix's free plan runs a single startup file (set PY_FILE to
falix-launcher.py) with no custom startup command. This launcher downloads
the bot code on first run, then supervises both bots (Twitch + Discord,
sharing one overlay server) under the Hellhound watch (added 2026-11-01):
the hound knows the smell of a dead token, refuses to crash-loop, keeps a
restart budget with growing backoff, writes a live heartbeat status file,
and — when something is structurally wrong — parks the pack and BARKS
(a banner in this console plus HELLHOUND-ALERT.txt) instead of spinning
silently until the Creator happens to look.
"""
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

try:
    # Chat lines can carry any Unicode; never let an unprintable byte kill
    # the launcher. _say below is the second layer: a failed print is
    # dropped, never raised.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _say(msg, **kw):
    """Best-effort console line. The launcher must never die because its
    console died (broken pipe, unprintable bytes): a failed print is
    dropped, the pack keeps running, and the status/alert files still
    record state."""
    try:
        print(msg, **kw)
    except Exception:
        pass

# 2026-10-13 ~08:25 EDT: rebuilt ZIP with Nightbot importer (bot.py +
# nightbot_import.py). tmpfiles link — use within ~1 hour, then re-upload.
ZIP_URL = "https://tmpfiles.org/dl/1791894518.eed2ef1a1aac8213/wjADKQ74gYE5/grous-bot.zip"
ZIP_PATH = os.path.join(HERE, "grous-bot.zip")


def _early_park(title, body, watch_files=()):
    """Park before the Hellhound exists: banner + alert file + wait.

    Used by the startup checks (missing code / missing files), which run
    before the hound is built. A missing file must park the launcher
    visibly instead of killing the process with sys.exit. Returns when a
    watched file appears or config.yaml changes, so the caller can retry;
    never returns while nothing has changed, so the process cannot spin.
    """
    bar = "=" * 68
    _say(f"\n{bar}", flush=True)
    _say(f"[HELLHOUND] {title}", flush=True)
    for line in body.splitlines():
        _say(f"[HELLHOUND] {line}", flush=True)
    _say(f"{bar}\n", flush=True)
    try:
        with open(os.path.join(HERE, "HELLHOUND-ALERT.txt"), "w") as f:
            f.write(f"{title}\n\n{body}\n")
    except OSError:
        pass
    try:
        cfg_mtime = os.path.getmtime(os.path.join(HERE, "config.yaml"))
    except OSError:
        cfg_mtime = None
    waited = 0
    while True:
        time.sleep(30)
        waited += 30
        if any(os.path.isfile(os.path.join(HERE, p)) for p in watch_files):
            _say("[hellhound] missing file appeared — retrying startup.",
                  flush=True)
            return
        try:
            m = os.path.getmtime(os.path.join(HERE, "config.yaml"))
        except OSError:
            m = None
        if (cfg_mtime is None and m is not None) or (
                m is not None and cfg_mtime is not None and m != cfg_mtime):
            _say("[hellhound] config.yaml changed — retrying startup.",
                  flush=True)
            return
        if waited % 300 == 0:
            _say(f"[hellhound] still parked ({title}). Fix per "
                  "HELLHOUND-ALERT.txt, or restart the server.", flush=True)


def ensure_code():
    # Missing code never kills the launcher: park visibly and retry when
    # bot.py appears (pasted in Falix) or config.yaml changes.
    while True:
        if os.path.isfile(os.path.join(HERE, "bot.py")):
            return
        _say("[falix] downloading bot code...", flush=True)
        ok = False
        for attempt in range(4):
            try:
                req = urllib.request.Request(
                    ZIP_URL,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                )
                with urllib.request.urlopen(req, timeout=120) as r, open(ZIP_PATH, "wb") as f:
                    shutil.copyfileobj(r, f)
                ok = True
                break
            except Exception as e:
                _say(f"[falix] download attempt {attempt + 1} failed: {e}", flush=True)
                time.sleep(3)
        if not ok:
            _early_park(
                "BOT CODE MISSING — the pack is parked, NOT dead.",
                "bot.py is not on this server and the code download failed "
                "(the link has probably expired).\n"
                "The fix is to paste bot.py (and the other pack files) into "
                "Falix, or ask Donnie for a fresh download link.\n"
                "The launcher watches for bot.py — paste it and the pack "
                "starts on its own.",
                watch_files=("bot.py",))
            continue
        if not zipfile.is_zipfile(ZIP_PATH):
            _early_park(
                "BOT CODE MISSING — the pack is parked, NOT dead.",
                "The downloaded code wasn't a valid zip (the link has "
                "probably expired).\n"
                "The fix is to paste bot.py (and the other pack files) into "
                "Falix, or ask Donnie for a fresh download link.\n"
                "The launcher watches for bot.py — paste it and the pack "
                "starts on its own.",
                watch_files=("bot.py",))
            continue
        with zipfile.ZipFile(ZIP_PATH) as z:
            z.extractall(HERE)
        pkg = os.path.join(HERE, "grous-bot")
        if os.path.isdir(pkg):
            for item in os.listdir(pkg):
                # Never clobber an existing config.yaml: the one on the server
                # already holds the user's real tokens; the ZIP only has
                # placeholders. (Fresh servers without a config still get the
                # placeholder copy below.)
                if item == "config.yaml" and os.path.isfile(os.path.join(HERE, "config.yaml")):
                    _say("[falix] keeping your existing config.yaml (tokens preserved).", flush=True)
                    continue
                s = os.path.join(pkg, item)
                d = os.path.join(HERE, item)
                if os.path.isdir(s):
                    shutil.copytree(s, d, dirs_exist_ok=True)
                else:
                    shutil.copy2(s, d)
            shutil.rmtree(pkg)
        os.remove(ZIP_PATH)
        _say("[falix] code ready.", flush=True)
        return


def ensure_deps():
    """Install requirements.txt on first run if the bot's packages are missing.

    Falix's free plan may or may not install requirements.txt before running
    the startup file; rather than guess, check the imports the bots need and
    pip-install when they're absent. Non-fatal: if pip fails, the bots start
    anyway so the real error shows in the console.
    """
    try:
        import yaml  # noqa: F401
        import twitchio  # noqa: F401
        import discord  # noqa: F401
        import flask  # noqa: F401
        import streamlink  # noqa: F401 — voice listening needs it
        import imageio_ffmpeg  # noqa: F401 — voice listening needs it
        return
    except ImportError:
        pass
    _say("[falix] installing Python packages (first run only, ~1 minute)...", flush=True)
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q",
             "-r", os.path.join(HERE, "requirements.txt")],
            timeout=300, check=False)
        _say("[falix] packages installed.", flush=True)
    except Exception as e:
        _say(f"[falix] pip install had trouble ({e}) — starting bots anyway.", flush=True)


def _read_overlay_port():
    """Read `overlay_port` from config.yaml without PyYAML (default 5000).

    Missing file or key means the default port. Never raises.
    """
    try:
        with open(os.path.join(HERE, "config.yaml")) as f:
            lines = f.read().splitlines()
    except OSError:
        return 5000
    for raw in lines:
        # strip comments (values never contain '#')
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip() or line[:1] in (" ", "\t"):
            continue  # top-level keys only
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        if key.strip() == "overlay_port":
            try:
                return int(value.strip().strip("'\""))
            except ValueError:
                return 5000
    return 5000


ensure_code()
ensure_deps()

NEEDED = ("bot.py", "discord_bot.py", "shared.py", "hub.py",
          "overlay_server.py", "config.yaml")
# A missing pack file never kills the launcher: park visibly and retry
# when the file is pasted in Falix (or config.yaml changes).
while True:
    missing = [f for f in NEEDED if not os.path.isfile(os.path.join(HERE, f))]
    if not missing:
        break
    _early_park(
        "MISSING FILES — the pack is parked, NOT dead.",
        "These pack files are not on this server: %s\n"
        "The fix is to paste them into Falix (same folder as this "
        "launcher).\n"
        "The launcher watches for them — paste one and it re-checks on "
        "its own." % ", ".join(missing),
        watch_files=tuple(missing))

_say("[falix] starting GrousBot (Twitch + Discord)...", flush=True)

# --- Cloudflare quick tunnel (no account needed) ---
# This gives the overlay a public URL in seconds via trycloudflare.com.
# The URL changes on each restart; the bot posts it in chat (see below).
CF_URL = ("https://github.com/cloudflare/cloudflared/releases/latest/"
          "download/cloudflared-linux-amd64")
CF_BIN = os.path.join(HERE, ".bin", "cloudflared")


def _ensure_cloudflared_binary():
    """Download the cloudflared CLI on first run. Returns path or None."""
    if os.path.isfile(CF_BIN):
        return CF_BIN
    _say("[falix] downloading Cloudflare tunnel client (one-time)...", flush=True)
    try:
        os.makedirs(os.path.dirname(CF_BIN), exist_ok=True)
        urllib.request.urlretrieve(CF_URL, CF_BIN)
        os.chmod(CF_BIN, 0o755)
        _say("[falix] Cloudflare tunnel client ready.", flush=True)
        return CF_BIN
    except Exception as e:
        _say(f"[falix] Cloudflare download failed ({e}).", flush=True)
        return None


def _cloudflared_supervisor(cf_bin, port):
    """Keep `cloudflared tunnel --url` alive; print the public URL."""
    url_re = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.IGNORECASE)
    failures = 0
    announced = False
    _say("[falix] Cloudflare quick tunnel starting...", flush=True)
    while True:
        if failures:
            wait = min(10 * (2 ** min(failures, 5)), 300)
            time.sleep(wait)
        try:
            p = subprocess.Popen(
                [cf_bin, "tunnel", "--url", f"http://127.0.0.1:{port}"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1)
        except Exception as e:
            _say(f"[falix] couldn't start Cloudflare tunnel ({e})", flush=True)
            failures += 1
            continue

        found = []

        def drain():
            try:
                for line in p.stdout:
                    m = url_re.search(line)
                    if m and not found:
                        found.append(m.group(0))
            except Exception:
                pass

        threading.Thread(target=drain, daemon=True).start()

        deadline = time.time() + 60
        while time.time() < deadline:
            if found and not announced:
                _say(f"[falix] overlay public URL: {found[0]}/overlay", flush=True)
                # Save for the bot to announce in chat.
                try:
                    with open(os.path.join(HERE, ".public-url"), "w") as f:
                        f.write(found[0] + "/overlay")
                except OSError:
                    pass
                announced = True
                break
            if p.poll() is not None:
                break
            time.sleep(5)

        # Keep the tunnel process alive; if it dies, loop retries.
        while p.poll() is None:
            time.sleep(10)
        if not announced:
            _say("[falix] Cloudflare tunnel exited before URL came up — retrying.", flush=True)
        failures += 1
        announced = False


# Public overlay tunnel: Cloudflare quick tunnel, started directly.
# Best-effort: if anything here fails, the bots below still start
# and run exactly as before.
cf = _ensure_cloudflared_binary()
if cf:
    threading.Thread(target=_cloudflared_supervisor,
                     args=(cf, _read_overlay_port()),
                     daemon=True, name="cf-tunnel").start()
else:
    _say("[falix] tunnel not started — bots continue without a public overlay URL.", flush=True)

# --- Hellhound watch -------------------------------------------------
# The old supervision restarted the pack forever, every 10 seconds, no
# matter why it died. That is how an expired token became an all-night
# crash loop on 2026-10-30 that nobody saw until the Creator opened the
# console. The hound fixes the shape:
#
# - NOSE: it reads the Twitch bot's output and knows the signatures of
#   a dead token. An auth death is never restartable — new credentials
#   are the only fix — so the hound does not loop on it.
# - BUDGET: ordinary crashes get a restart budget inside a window, with
#   growing backoff. Exhaust the budget and the fault is structural.
# - HEARTBEAT: hellhound-status.json is rewritten every few seconds, so
#   the pack's state is a file, not a guess.
# - BARK: on auth death or budget exhaustion the hound parks the pack,
#   prints a banner that cannot be missed, and writes HELLHOUND-ALERT.txt
#   saying what is wrong and the one fix. While parked it watches
#   config.yaml: if the Creator pastes fresh values, the pack retries.
import json as _json

HOUND_MAX_RESTARTS = int(os.environ.get("HELLHOUND_MAX_RESTARTS", "5"))
HOUND_WINDOW_S = float(os.environ.get("HELLHOUND_WINDOW_S", "600"))
HOUND_BACKOFF_S = (10, 30, 60, 120, 300)
AUTH_SIGNATURES = (
    "AuthenticationError",
    "token invalid and refresh unavailable",
    "Login failure",
    "invalid access token",
)
STATUS_PATH = os.path.join(HERE, "hellhound-status.json")
ALERT_PATH = os.path.join(HERE, "HELLHOUND-ALERT.txt")


class _Hound:
    def __init__(self):
        self.state = "starting"
        self.since = time.time()
        self.restarts = []
        self.last_event = "launcher boot"
        self.auth_dead = False
        self.pack_started = None
        self.procs = []
        # Generation of the currently running pack. The output-drain thread
        # only trusts lines from its own generation: a stale drain thread
        # from a dead pack must never flag the new pack as auth-dead.
        self.pack_gen = 0


def _hound_write_status(hound):
    url = None
    try:
        with open(os.path.join(HERE, ".public-url")) as f:
            url = f.read().strip() or None
    except OSError:
        pass
    now = time.time()
    hound.restarts = [t for t in hound.restarts if now - t <= HOUND_WINDOW_S]
    data = {
        "state": hound.state,
        "since": hound.since,
        "updated_at": now,
        "restarts_in_window": len(hound.restarts),
        "restart_budget": HOUND_MAX_RESTARTS,
        "last_event": hound.last_event,
        "overlay_url": url,
    }
    try:
        with open(STATUS_PATH, "w") as f:
            _json.dump(data, f, indent=1)
    except OSError:
        pass


def _hound_heartbeat(hound):
    # A single failed status write must never kill the heartbeat thread:
    # a stale status file would make a live pack look dead.
    while True:
        try:
            _hound_write_status(hound)
        except Exception:
            pass
        time.sleep(5)


def _hound_bark(hound, title, body):
    hound.last_event = title
    bar = "=" * 68
    _say(f"\n{bar}", flush=True)
    _say(f"[HELLHOUND] {title}", flush=True)
    for line in body.splitlines():
        _say(f"[HELLHOUND] {line}", flush=True)
    _say(f"{bar}\n", flush=True)
    try:
        with open(ALERT_PATH, "w") as f:
            f.write(f"{title}\n\n{body}\n")
    except OSError:
        pass
    _hound_write_status(hound)


def _hound_park(hound, state):
    """Stop the pack and wait. Watches config.yaml: new values = retry."""
    hound.state = state
    hound.since = time.time()
    _hound_write_status(hound)
    try:
        cfg_mtime = os.path.getmtime(os.path.join(HERE, "config.yaml"))
    except OSError:
        cfg_mtime = None
    waited = 0
    while True:
        time.sleep(30)
        waited += 30
        try:
            m = os.path.getmtime(os.path.join(HERE, "config.yaml"))
        except OSError:
            m = None
        if (cfg_mtime is None and m is not None) or (
                m is not None and cfg_mtime is not None and m != cfg_mtime):
            # config.yaml appeared (it was missing when we parked) or changed.
            _say("[hellhound] config.yaml changed — retrying the pack.",
                  flush=True)
            hound.last_event = "config changed while parked; retrying"
            return
        if waited % 300 == 0:
            _say(f"[hellhound] still parked ({state}). "
                  "Fix per HELLHOUND-ALERT.txt, or edit config.yaml "
                  "and the pack retries on its own.", flush=True)


def _drain_bot_output(stream, hound, gen):
    """Print the Twitch bot's lines and sniff for a dead token.

    Only lines from this drain's own pack generation count: a stale drain
    thread still holding the previous pack's pipe must never flag the new
    pack as auth-dead.
    """
    try:
        for line in stream:
            _say(line, end="", flush=True)
            if (hound.pack_gen == gen
                    and any(sig in line for sig in AUTH_SIGNATURES)):
                hound.auth_dead = True
    except Exception:
        pass


hound = _Hound()
threading.Thread(target=_hound_heartbeat, args=(hound,), daemon=True,
                 name="hellhound").start()


def _park_or_sleep(hound, state):
    """Park via the hound; if parking itself faults, stay up and visible
    rather than letting the launcher process die. A stop request
    (KeyboardInterrupt) still propagates so the panel can stop the server."""
    try:
        _hound_park(hound, state)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        _say(f"[hellhound] park failed ({exc!r}) — holding instead of "
              "exiting.", flush=True)
        while True:
            time.sleep(60)
            _say(f"[hellhound] still parked ({state}). Fix per "
                  "HELLHOUND-ALERT.txt, or restart the server.", flush=True)


# The supervision loop never exits on its own: a pack crash is a restart
# (budget + backoff) or a visible park, a launcher fault is counted
# against the same budget, and only a deliberate stop (KeyboardInterrupt)
# ends the process — cleanly, without a traceback.
try:
    while True:
        hound.state = "running"
        hound.since = time.time()
        hound.pack_started = time.time()
        hound.pack_gen += 1
        hound.auth_dead = False
        hound.procs = []
        launcher_fault = None
        p_bot = p_disc = None
        procs = []
        try:
            p_bot = subprocess.Popen([sys.executable, "-u", "bot.py"],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, bufsize=1)
            procs = [p_bot]
            hound.procs = procs
            p_disc = subprocess.Popen([sys.executable, "-u", "discord_bot.py"])
            procs.append(p_disc)
            threading.Thread(target=_drain_bot_output,
                             args=(p_bot.stdout, hound, hound.pack_gen),
                             daemon=True, name="hound-nose").start()
            while True:
                time.sleep(2)
                if any(p.poll() is not None for p in procs):
                    break
        except KeyboardInterrupt:
            for p in procs:
                if p.poll() is None:
                    p.terminate()
            break
        except Exception as exc:
            # A launcher fault (spawn failure, thread failure, ...) is a
            # pack crash like any other: it counts against the restart
            # budget and either backs off or parks — it never kills the
            # launcher process with a traceback.
            launcher_fault = exc
        if launcher_fault is not None:
            hound.last_event = "launcher fault: %r" % (launcher_fault,)
            _say(f"[hellhound] launcher fault: {launcher_fault!r} — "
                  "counting it against the restart budget.", flush=True)
        else:
            hound.last_event = "pack exited (bot rc=%s, discord rc=%s)" % (
                p_bot.poll() if p_bot is not None else None,
                p_disc.poll() if p_disc is not None else None)
        for p in procs:
            try:
                if p.poll() is None:
                    p.terminate()
            except Exception:
                pass
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    p.kill()
                except Exception:
                    pass
            except Exception:
                pass

        if hound.auth_dead:
            _hound_bark(
                hound,
                "AUTH DEAD — the pack is parked, NOT crash-looping.",
                "Twitch refused the bot's token and restarting cannot fix "
                "that.\n"
                "The fix is one fresh pair from twitchtokengenerator.com "
                "(authorize as grousbot):\n"
                "paste the new access token + refresh token into config.yaml "
                "in Falix.\n"
                "The hound watches config.yaml — save it and the pack "
                "retries on its own.")
            _park_or_sleep(hound, "parked_auth_dead")
            hound.restarts.clear()
            continue

        now = time.time()
        hound.restarts = [t for t in hound.restarts if now - t <= HOUND_WINDOW_S]
        hound.restarts.append(now)
        if len(hound.restarts) > HOUND_MAX_RESTARTS:
            _hound_bark(
                hound,
                "RESTART BUDGET EXHAUSTED — the pack is parked.",
                "The bots crashed %d times in %d minutes. That is a "
                "structural fault,\nnot bad luck, and looping would only "
                "hide it.\n"
                "Read the last errors above this banner, fix the cause, "
                "then restart the\nserver — or edit config.yaml and the "
                "hound retries the pack once." % (
                    len(hound.restarts), int(HOUND_WINDOW_S // 60)))
            _park_or_sleep(hound, "parked_budget")
            hound.restarts.clear()
            continue

        wait = HOUND_BACKOFF_S[min(len(hound.restarts) - 1,
                                   len(HOUND_BACKOFF_S) - 1)]
        _say(f"[hellhound] pack exited — restart "
              f"{len(hound.restarts)}/{HOUND_MAX_RESTARTS} in {wait}s...",
              flush=True)
        hound.state = "backing_off"
        hound.since = time.time()
        _hound_write_status(hound)
        time.sleep(wait)
except KeyboardInterrupt:
    # Deliberate stop (panel / Ctrl-C) at any point — backoff, park, or
    # spawn: terminate anything still running and exit cleanly.
    for p in hound.procs:
        try:
            if p.poll() is None:
                p.terminate()
        except Exception:
            pass
