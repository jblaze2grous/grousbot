"""
GrousBot - Phase 1: Chat commands + random engagement for Twitch
Built by Donnie for Grous.

Setup:
  1. Copy config.example.yaml -> config.yaml and fill in your values
  2. pip install -r requirements.txt
  3. python bot.py
"""

import json
import random
import re
import time
import asyncio
import os

import yaml
from twitchio.ext import commands

import shared
from shared import contains_blocked, make_ai_client, is_hateful, ask_ai, giphy_search, create_clip

try:
    import nightbot_import
    NIGHTBOT_IMPORT_AVAILABLE = True
except Exception as e:
    # Nightbot import is optional: a broken nightbot_import.py (e.g. a
    # bad paste) must not kill the bot — custom commands just stay off.
    print(f"[nightbot] import failed ({e}) — custom commands disabled",
          flush=True)
    NIGHTBOT_IMPORT_AVAILABLE = False

try:
    import voice_listen
    import voice_commands
    VOICE_AVAILABLE = True
except Exception as e:
    # Voice is optional: a broken voice module (bad paste, SyntaxError,
    # ...) must not kill the bot — voice listening just stays off.
    print(f"[voice] voice modules failed to import ({e}) — "
          "voice listening disabled", flush=True)
    VOICE_AVAILABLE = False

try:
    from overlay_server import ensure_overlay  # only the server starter is local
    import hub  # everything else goes through the shared hub over HTTP
    from hub import (
        show_gif, approve_next, deny_next, set_giphy_key,
        arena_join, arena_move, arena_reset, arena_round, arena_status, arena_tick,
        queue_add, queue_vote, queue_remove, queue_skip, queue_clear, queue_list,
        poll_start, poll_vote, poll_end, poll_tick, poll_status,
        points_get, points_add, points_top,
        gamble, slots, roulette,
        heist_start, heist_join, heist_tick, heist_status,
        quote_add, quote_get, quote_del,
        counter_get, counter_bump,
        giveaway_start, giveaway_enter, giveaway_tick, giveaway_end, giveaway_status,
        bridge_post, bridge_get,
    )
    OVERLAY_AVAILABLE = True
except Exception as e:
    # Overlay is optional: a broken overlay_server/hub (bad paste,
    # SyntaxError, missing flask, ...) must not kill the bot — overlay
    # features just stay off.
    print(f"[overlay] overlay modules failed to import ({e}) — "
          "overlay features disabled", flush=True)
    OVERLAY_AVAILABLE = False  # pip install flask to enable hub features

# (slur patterns moved to shared.py — imported above)


DEFAULT_RANDOM_MESSAGES = [
    "What y'all listening to tonight? 👀",
    "Drop a 🔥 if this beat is hitting",
    "Anyone in chat make music? What DAW y'all on?",
    "If you're new here say what's up — we're cooking live 🎹",
    "Type !music to check the SoundCloud 🎵",
    "What's the vibe tonight — chill or hype?",
    "Shoutout to the night crew, y'all the real ones 🌙",
]


def load_config(path="config.yaml"):
    # Resolve relative to this file, not the working directory, so the
    # bot starts no matter where the launcher runs it from.
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    try:
        with open(path, "r") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        print(f"[config] FATAL: no config.yaml at {path} — copy "
              "config.example.yaml to config.yaml and fill in your tokens",
              flush=True)
        raise
    except Exception as e:
        print(f"[config] FATAL: could not read {path} ({e})", flush=True)
        raise


def _require_config_keys(config):
    """Fail LOUD on missing must-haves instead of a bare KeyError inside
    GrousBot.__init__ — the console should name the missing key."""
    for _key in ("twitch_token", "channel"):
        if not config.get(_key):
            print(f"[boot] FATAL: config.yaml is missing '{_key}' — the bot "
                  "cannot connect without it", flush=True)
            raise SystemExit(2)


def ensure_fresh_token(config, config_path="config.yaml"):
    """Validate the Twitch access token at boot; refresh it if expired.

    Twitch access tokens die after a few hours, and a dead one used to put
    the bot in an AuthenticationError crash loop. If config.yaml also has
    twitch_refresh_token + twitch_client_id + twitch_client_secret, exchange
    the refresh token for a fresh pair and write the new values back into
    config.yaml (line-based edit: only the two token lines change; Twitch
    rotates the refresh token, so both are saved). Stdlib only. Never
    raises — any failure logs a warning and boot continues with the
    existing token, exactly as before.
    """
    import urllib.parse
    import urllib.request

    try:
        raw = str(config.get("twitch_token") or "")
        access = raw[6:] if raw.lower().startswith("oauth:") else raw
        if not access:
            return config
        req = urllib.request.Request(
            "https://id.twitch.tv/oauth2/validate",
            headers={"Authorization": f"OAuth {access}"})
        try:
            with urllib.request.urlopen(req, timeout=15):
                return config  # token still valid — nothing to do
        except Exception:
            pass  # expired/rejected (or network hiccup) — try a refresh

        refresh = str(config.get("twitch_refresh_token") or "").strip()
        cid = str(config.get("twitch_client_id") or "").strip()
        csec = str(config.get("twitch_client_secret") or "").strip()
        if not (refresh and cid and csec):
            print("[auth] token invalid and refresh unavailable — mint a fresh token",
                  flush=True)
            return config

        form = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": cid,
            "client_secret": csec,
        }).encode()
        req = urllib.request.Request(
            "https://id.twitch.tv/oauth2/token", data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
        new_access = data.get("access_token")
        if not new_access:
            print("[auth] token invalid and refresh unavailable — mint a fresh token",
                  flush=True)
            return config
        new_refresh = data.get("refresh_token") or refresh

        # Write the new pair back so the NEXT boot starts fresh too.
        # Line-based: every other line of config.yaml stays byte-for-byte.
        try:
            with open(config_path) as f:
                lines = f.read().splitlines(keepends=True)
            seen_token = seen_refresh = False
            for i, line in enumerate(lines):
                stripped = line.lstrip()
                indent = line[:len(line) - len(stripped)]
                if stripped.startswith("twitch_refresh_token:"):
                    lines[i] = f'{indent}twitch_refresh_token: "{new_refresh}"\n'
                    seen_refresh = True
                elif stripped.startswith("twitch_token:"):
                    lines[i] = f'{indent}twitch_token: "oauth:{new_access}"\n'
                    seen_token = True
            if seen_token and not seen_refresh:
                for i, line in enumerate(lines):
                    if line.lstrip().startswith("twitch_token:"):
                        indent = line[:len(line) - len(line.lstrip())]
                        lines.insert(i + 1,
                                     f'{indent}twitch_refresh_token: "{new_refresh}"\n')
                        break
            if seen_token:
                with open(config_path, "w") as f:
                    f.writelines(lines)
        except OSError as e:
            print(f"[auth] refreshed but couldn't save config.yaml ({e}) — "
                  "new token works for this run only", flush=True)

        config["twitch_token"] = f"oauth:{new_access}"
        config["twitch_refresh_token"] = new_refresh
        print("[auth] access token refreshed at boot", flush=True)
        return config
    except Exception as e:
        print(f"[auth] token check failed ({e}) — continuing with existing token",
              flush=True)
        return config


class GrousBot(commands.Bot):
    def __init__(self, config):
        self.cfg = config
        super().__init__(
            token=config["twitch_token"],  # e.g. "oauth:xxxx" from twitchtokengenerator.com
            prefix=config.get("prefix", "!"),
            initial_channels=[config["channel"]],
        )
        self.links = config.get("links", {})
        self.channel_name = config["channel"].lower()

        # Random engagement settings
        rc = config.get("random_chat", {}) or {}
        self.random_chat_enabled = rc.get("enabled", True)
        self.msg_threshold = rc.get("messages_per_trigger", 25)
        self.random_chance = rc.get("chance", 0.35)
        self.cooldown_secs = rc.get("cooldown_minutes", 10) * 60
        self.greet_new = rc.get("greet_new_chatters", True)
        self.random_messages = rc.get("messages") or DEFAULT_RANDOM_MESSAGES

        self.seen_chatters = set()
        self._msg_count = 0
        self._last_random_ts = 0.0

        # Twitch <-> Discord chat bridge + go-live watcher (via the hub)
        bc = config.get("bridge", {}) or {}
        self.bridge_enabled = bool(bc.get("enabled", True))
        self._bridge_since = time.time()
        self._was_live = None  # unknown until the first Helix check
        self._golive_check_seconds = int(bc.get("golive_check_seconds", 60) or 60)

        # AI replies — needs an OpenAI API key (shared helper)
        self.ai_client = make_ai_client(config.get("openai_api_key"))
        self.ai_cooldown = config.get("ai_cooldown_seconds", 30)
        self._last_ai_ts = 0.0
        # Custom instant-timeout words from config (case-insensitive)
        self.blocked_words = [str(w).lower() for w in (config.get("blocked_words") or [])]
        self.ai_system_prompt = config.get("ai_system_prompt") or (
            "You are GrousBot, the hype assistant for Twitch streamer Grous, "
            "who makes music live on stream (late-night 12am-3am sessions). "
            "Reply in Twitch chat style: short, fun, max 2 sentences, no hashtags. "
            "Never produce racist, hateful, harassing, or sexual content. "
            "If asked for such content, refuse briefly and move on."
        )

        # Per-command per-user cooldowns: {(cmd, user): timestamp}
        self._cmd_cooldowns = {}
        # Points earn cooldowns: {user_lower: timestamp}
        self._earn_cooldowns = {}
        # Cached giveaway status (refreshed by the watchdog)
        self._giveaway_cache = None

        # Auto-clip: hype detection -> Twitch clip creation
        ac = config.get("autoclip", {}) or {}
        self.autoclip_enabled = bool(ac.get("enabled", True))
        self._clip_window = int(ac.get("window_seconds", 30) or 30)
        self._clip_threshold = int(ac.get("message_threshold", 8) or 8)
        self._clip_cooldown = int(ac.get("cooldown_minutes", 5) or 5) * 60
        self._clip_announce = bool(ac.get("announce", True))
        self._chat_timestamps = []  # rolling window of message times
        self._last_clip_ts = 0.0
        self._clips_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "clips.json")

        # Voice listening: hear the stream like a chatter would.
        # Wake-word mode (2026-11-19): nothing is a command unless the
        # streamer says "GrousBot" first — general chatting never fires
        # anything. The old `triggers:` config (any "clip that" anywhere
        # = clip) is ignored now; see voice_commands.py.
        vc = config.get("voice_listen", {}) or {}
        self.voice_enabled = bool(vc.get("enabled", False)) and VOICE_AVAILABLE
        self._voice_chunk = int(vc.get("chunk_seconds", 6) or 6)
        self._voice_model = str(vc.get("transcribe_model") or
                               (voice_listen.DEFAULT_TRANSCRIBE_MODEL
                                if VOICE_AVAILABLE
                                else "gpt-4o-mini-transcribe"))
        self._voice_cooldowns = dict(
            voice_commands.DEFAULT_COOLDOWNS if VOICE_AVAILABLE else {})
        for k, v in ((vc.get("cooldowns") or {}).items()):
            try:
                self._voice_cooldowns[str(k)] = int(v)
            except (TypeError, ValueError):
                pass
        self._voice_say_hi = str(
            vc.get("say_hi_text") or
            "Yo what's good chat \U0001F5A4 it's ya boy Grous, live on the sticks")
        self._voice_last_action = {}  # action -> last-fired timestamp
        self._voice_pending_tail = []  # wake fragment split across chunks
        self._voice_last_fired = ("", "", 0.0)  # (action, args, ts) dedupe
        self._voice_task_started = False
        if vc.get("triggers"):
            print("[voice] NOTE: old voice_listen.triggers config is "
                  "ignored — wake-word mode is on now (say 'GrousBot' "
                  "then the command)")

        # Imported Nightbot custom commands (one-bot takeover): trigger -> entry.
        # Checked in event_message BEFORE built-in commands.
        self.custom_commands = {}
        if NIGHTBOT_IMPORT_AVAILABLE:
            try:
                self.custom_commands = nightbot_import.load_commands(
                    nightbot_import.COMMANDS_FILE)
                if self.custom_commands:
                    print("[nightbot] loaded %d imported custom commands"
                          % len(self.custom_commands))
            except Exception as e:
                print("[nightbot] couldn't load custom_commands.json: %s" % e)
        self._custom_cooldowns = {}  # trigger -> last-used timestamp (Nightbot: global per command)

        # Stream overlay web server for !gif / !photos / arena (OBS browser sources).
        # The overlay is OPTIONAL: if it fails to start (port already in
        # use, Flask error, ...), the Twitch bot must keep running
        # degraded instead of dying inside __init__ — overlay-gated
        # commands already check OVERLAY_AVAILABLE, so switching it off
        # here degrades them cleanly.
        global OVERLAY_AVAILABLE
        if OVERLAY_AVAILABLE:
            try:
                port = int(config.get("overlay_port", 5000) or 5000)
                ensure_overlay(port)
                hub.configure(port)
                print(f"[overlay] OBS browser sources: http://localhost:{port}/overlay and /arena")
                giphy_key = str(config.get("giphy_api_key") or "")
                if giphy_key and "PASTE" not in giphy_key:
                    set_giphy_key(giphy_key)
            except Exception as e:
                OVERLAY_AVAILABLE = False
                print(f"[overlay] FAILED to start overlay server ({e}) — "
                      "overlay features disabled, Twitch bot continues",
                      flush=True)

    async def event_ready(self):
        print(f"GrousBot connected to #{self.cfg['channel']}")
        if not getattr(self, "_watchdog_started", False):
            self._watchdog_started = True
            asyncio.create_task(self._arena_watchdog())
        if self.voice_enabled and not self._voice_task_started:
            self._voice_task_started = True
            asyncio.create_task(self._start_voice_listener())
            print("[voice] voice listening enabled")

    def _voice_is_live(self) -> bool:
        """Sync check: is the channel currently live?"""
        try:
            return shared.stream_uptime(
                str(self.cfg.get("twitch_client_id") or ""),
                str(self.cfg.get("twitch_client_secret") or ""),
                self.channel_name) is not None
        except Exception:
            return False

    async def _start_voice_listener(self):
        api_key = str(self.cfg.get("openai_api_key") or "")
        if not api_key or "PASTE" in api_key:
            print("[voice] no OpenAI key — voice listening disabled")
            return
        await voice_listen.listen_loop(
            self.channel_name, api_key, self._voice_chunk,
            self._voice_is_live, self._on_voice_transcript,
            model=self._voice_model)

    @staticmethod
    def _extract_shoutout_target(args):
        """Pull a Twitch username out of the words spoken after 'shoutout'.

        Skips filler ("to", "for", "my boy", ...) and any @; returns a
        valid Twitch login or None.
        """
        filler = {"to", "for", "my", "boy", "girl", "man", "the", "a", "an",
                  "bro", "sis", "homie"}
        for w in args:
            w = w.lstrip("@")
            if w in filler:
                continue
            w = re.sub(r"[^a-z0-9_]", "", w.lower())
            if 2 <= len(w) <= 25:
                return w
            return None  # first real word wasn't a usable username
        return None

    async def _on_voice_transcript(self, text: str):
        """Bot heard the streamer say something — wake-word gate, then route.

        Only words spoken AFTER "GrousBot" (or a Whisper mishearing of
        it) are a command. General chatting, game audio, and a bare
        "clip that" with no wake word are ignored. One command per
        transcript. Cooldowns are per action, not one global timer.
        """
        now = time.time()
        words = voice_commands.tokenize(text)
        if self._voice_pending_tail:
            # Wake word split across the previous chunk boundary.
            words = self._voice_pending_tail + words
            self._voice_pending_tail = []
        parsed = voice_commands.parse_voice_command(" ".join(words))
        if parsed is None:
            # No command — hold a trailing wake fragment for next chunk.
            self._voice_pending_tail = voice_commands.pending_wake_tail(words)
            self._voice_bootstrap_once()
            return
        action, args = parsed
        args_key = " ".join(args)
        # Dedupe: the same command re-parsed from a carried-over tail
        # must not fire twice.
        if (self._voice_last_fired[0] == action
                and self._voice_last_fired[1] == args_key
                and now - self._voice_last_fired[2] < 10):
            return
        last = self._voice_last_action.get(action, 0.0)
        cd = self._voice_cooldowns.get(action, 30)
        if now - last < cd:
            print(f"[voice] '{action}' on cooldown "
                  f"({cd - (now - last):.0f}s left), skipping")
            return
        self._voice_last_action[action] = now
        self._voice_last_fired = (action, args_key, now)
        print(f"[voice] wake-word command: {action} {args_key[:60]}")
        chan = self.connected_channels[0] if self.connected_channels else None
        if not chan:
            return
        if action == "clip":
            url = await asyncio.to_thread(
                create_clip,
                str(self.cfg.get("twitch_token") or ""),
                str(self.cfg.get("twitch_client_id") or ""),
                self.channel_name)
            if url == "NEED_SCOPE":
                # Same reporting as !clip / autoclip — never fail silent.
                print("[voice] clip FAILED: Twitch refused the token "
                      "(401/403) — clips:edit scope missing, or the "
                      "token was minted for a different client ID")
                try:
                    await chan.send(
                        "Heard you — but Twitch refused the clip "
                        "(bot token is missing clip permission) 🔧")
                except Exception:
                    pass
            elif not url:
                print("[voice] clip FAILED: create_clip returned "
                      "nothing (API error or channel lookup failed)")
            else:
                self._save_clip(url, "voice: GrousBot clip that")
                print(f"[voice] clip created: {url}")
                try:
                    await chan.send(f"📎 Clipped that! {url}")
                except Exception:
                    pass
        elif action == "say_hi":
            try:
                await chan.send(self._voice_say_hi)
                print(f"[voice] said hi: {self._voice_say_hi[:60]}")
            except Exception as e:
                print(f"[voice] say_hi send failed: {e}")
        elif action == "shoutout":
            target = self._extract_shoutout_target(args)
            if not target:
                print("[voice] shoutout heard but no username caught "
                      f"in: {' '.join(args)[:60]} — skipping")
            else:
                msg = (f"Big shoutout to @{target} — go show them some "
                       f"love! https://twitch.tv/{target}")
                try:
                    await chan.send(msg)
                    print(f"[voice] shouted out @{target}")
                except Exception as e:
                    print(f"[voice] shoutout send failed: {e}")
        elif action == "set_title":
            new_title = " ".join(args).strip()
            if not new_title:
                print("[voice] set_title heard but no title caught — "
                      "skipping")
            else:
                result = await asyncio.to_thread(
                    shared.set_channel_title,
                    str(self.cfg.get("twitch_token") or ""),
                    str(self.cfg.get("twitch_client_id") or ""),
                    self.channel_name, new_title)
                if result == "NEED_SCOPE":
                    print("[voice] title FAILED: Twitch refused the "
                          "token (401/403) — channel:manage:broadcast "
                          "scope missing on the bot token")
                    try:
                        await chan.send(
                            "Heard you — but my token can't change the "
                            "title (needs the channel:manage:broadcast "
                            "permission) 🔧")
                    except Exception:
                        pass
                elif not result:
                    print("[voice] title FAILED: create failed "
                          "(API error or channel lookup failed)")
                else:
                    print(f"[voice] title set: {result}")
                    try:
                        await chan.send(f"Title updated: {result} ✅")
                    except Exception:
                        pass
        elif action == "uptime":
            if not self._helix_ready():
                await chan.send("Uptime isn't hooked up yet 🔧")
            else:
                started = await asyncio.to_thread(
                    shared.stream_uptime,
                    str(self.cfg.get("twitch_client_id") or ""),
                    str(self.cfg.get("twitch_client_secret") or ""),
                    self.channel_name)
                if not started:
                    await chan.send("Not live right now 😴")
                else:
                    await chan.send(
                        f"🔴 Live for {shared.format_duration(started)}!")
        elif action == "info":
            await self._voice_send_info(chan, args[0] if args else "")
        elif action == "eightball":
            await chan.send(f"🎱 {random.choice(shared.EIGHTBALL_ANSWERS)}")
        elif action == "queue":
            if not OVERLAY_AVAILABLE:
                return
            items = queue_list()
            if not items:
                await chan.send("🎵 Queue's empty — !sr <song> to request one!")
            else:
                msg = "🎵 UP NEXT: " + " | ".join(
                    f"#{i['n']} \"{i['query'][:28]}\" ({i['votes']}❤)"
                    for i in items[:5])
                if len(items) > 5:
                    msg += f" (+{len(items) - 5} more)"
                await chan.send(msg[:450])
        elif action == "skip":
            # Voice is always the streamer, so skip is allowed like a mod.
            if not OVERLAY_AVAILABLE:
                return
            top = queue_skip()
            if top:
                await chan.send(
                    f"⏭️ Skipped \"{top['query'][:40]}\" "
                    f"(requested by @{top['user']})")
            else:
                await chan.send("Queue's empty 📭")
        else:
            print(f"[voice] unknown action '{action}' — check voice_commands")

    def _voice_bootstrap_once(self):
        """One-time starters that used to ride on the first non-matching
        transcript: bridge watchdog + Nightbot import. Still runs once,
        now from the no-command path instead of the old trigger loop."""
        if not getattr(self, "_bridge_started", False):
            self._bridge_started = True
            asyncio.create_task(self._bridge_watchdog())
        # One-time Nightbot import (runs only when configured + not yet imported)
        if NIGHTBOT_IMPORT_AVAILABLE and not getattr(self, "_nightbot_import_started", False):
            self._nightbot_import_started = True
            asyncio.create_task(self._nightbot_import_once())

    async def _voice_send_info(self, chan, name: str):
        """Send the same reply as the matching !command, for voice."""
        if name == "music":
            url = self.links.get("soundcloud", "")
            await chan.send(f"🎵 My music: {url}" if url else
                            "🎵 SoundCloud link coming soon!")
        elif name == "tip":
            url = self.links.get("tip", "")
            await chan.send(f"💰 Tip the stream: {url}" if url else
                            "💰 Tip link coming soon!")
        elif name == "beats":
            url = self.links.get("beats", "")
            await chan.send(f"🎹 Beats/services: {url}" if url else
                            "🎹 Beat store coming soon!")
        elif name == "shop":
            url = self.links.get("shop", "")
            await chan.send(f"🛒 Merch: {url}" if url else "🛒 Shop coming soon!")
        elif name == "socials":
            parts = [f"{k}: {v}" for k, v in self.links.items() if v]
            await chan.send(" | ".join(parts) if parts else "Socials coming soon!")
        elif name == "schedule":
            sched = self.cfg.get("schedule", "")
            await chan.send(f"📅 Schedule: {sched}" if sched else
                            "📅 I stream when I'm cooking up — follow to get notified!")
        elif name == "commands":
            await chan.send(
                "🎛️ !sr !queue !vote !poll !pvote !8ball !ask !roast !gif "
                "!photos !fight !punch !kick !block !special !fighters "
                "!points !gamble !slots !roulette !heist !quote !counter "
                "!giveaway !uptime !followage !stats !music !tip !socials "
                "!schedule")
        elif name == "arena":
            base = self._public_base_url()
            if base:
                await chan.send(
                    f"🥊 Watch the arena live: {base}/arena | "
                    f"Stream overlay: {base}/overlay")
            else:
                await chan.send("🥊 Arena link coming soon — tunnel's still starting!")
        else:
            print(f"[voice] unknown info command '{name}'")

    async def _nightbot_import_once(self):
        """Pull Nightbot custom commands once, translate, save. Never crashes the bot."""
        cfg = self.cfg
        try:
            if (os.path.exists(nightbot_import.COMMANDS_FILE)
                    and not cfg.get("nightbot_reimport")):
                return  # already imported; one-time by design
            token = str(cfg.get("nightbot_api_token") or "")
            cid = str(cfg.get("nightbot_client_id") or "")
            csec = str(cfg.get("nightbot_client_secret") or "")
            has_token = bool(token) and not nightbot_import.is_placeholder(token)
            has_creds = (bool(cid) and bool(csec)
                         and not nightbot_import.is_placeholder(cid)
                         and not nightbot_import.is_placeholder(csec))
            if not has_token and not has_creds:
                return  # not configured; stay quiet
            print("[nightbot] importing custom commands from Nightbot API...")
            # Don't shadow GrousBot built-ins: Nightbot names carry the "!" but
            # twitchio command names don't, so skip both forms.
            builtin = {str(n).lower() for n in self.commands.keys()}
            skip = builtin | {"!" + n for n in builtin}
            report = await asyncio.to_thread(
                nightbot_import.run_import,
                token=token if has_token else None,
                client_id=cid if has_creds else None,
                client_secret=csec if has_creds else None,
                skip_names=skip,
            )
            nightbot_import.save_commands(nightbot_import.COMMANDS_FILE,
                                          report["commands"])
            self.custom_commands = report["commands"]
            nightbot_import.print_report(report)
        except Exception as e:
            # Bad/expired token, no network, API hiccup — bot keeps running.
            print("[nightbot] import failed, bot continues normally: %s" % e)

    def _custom_userlevel_ok(self, level, message):
        """Nightbot userlevel gate against twitchio author flags."""
        level = str(level or "everyone").lower()
        if level in ("everyone", "subscriber", "regular", "twitch_vip", "soop_vip"):
            return True
        author = message.author
        name = str(getattr(author, "name", "") or "").lower()
        is_owner = (name == self.channel_name
                    or bool(getattr(author, "is_broadcaster", False)))
        if level == "owner":
            return is_owner
        if level == "moderator":
            return is_owner or bool(getattr(author, "is_mod", False))
        return True  # unknown level: allow rather than break chat

    def _save_custom_commands(self):
        if NIGHTBOT_IMPORT_AVAILABLE:
            nightbot_import.save_commands(nightbot_import.COMMANDS_FILE,
                                          self.custom_commands or {})

    async def _handle_custom_command(self, message):
        """Imported Nightbot commands. Returns True if the message was handled."""
        if not self.custom_commands:
            return False
        content = str(message.content or "")
        content_l = content.lower()
        matched = None
        for trigger in sorted(self.custom_commands.keys(), key=len, reverse=True):
            if content_l == trigger or content_l.startswith(trigger + " "):
                matched = trigger
                break
        if not matched:
            return False
        entry = self.custom_commands[matched]
        if entry.get("disabled"):
            return False  # flagged for manual review; don't answer
        if not self._custom_userlevel_ok(entry.get("userlevel"), message):
            return True  # gated: stay silent like Nightbot does
        cd = int(entry.get("cooldown") or 0)
        now = time.time()
        if cd and self._custom_cooldowns.get(matched, 0) + cd > now:
            return True  # cooling down: swallow quietly
        self._custom_cooldowns[matched] = now

        author = message.author
        user = (getattr(author, "display_name", None)
                or getattr(author, "name", "") or "")
        rest = content[len(matched):].strip()
        query = rest
        touser = query.split(None, 1)[0].lstrip("@") if query else ""
        if not touser:
            touser = user
        count = int(entry.get("count") or 0)
        try:
            reply = await asyncio.to_thread(
                nightbot_import.render_template, entry.get("template", ""),
                user=user, touser=touser, query=query, count=count)
        except Exception as e:
            print("[nightbot] render failed for %s: %s" % (matched, e))
            return True
        # Bump the persistent per-command counter (Nightbot $(count) semantics).
        try:
            entry["count"] = count + 1
            self._save_custom_commands()
        except Exception:
            pass
        if reply:
            try:
                await message.channel.send(reply[:500])
            except Exception as e:
                print("[nightbot] send failed for %s: %s" % (matched, e))
        return True

    async def event_message(self, message):
        # Ignore the bot's own messages
        if message.echo:
            return

        # Imported Nightbot custom commands take priority (one-bot takeover)
        if NIGHTBOT_IMPORT_AVAILABLE and await self._handle_custom_command(message):
            return

        is_command = message.content.startswith(self.cfg.get("prefix", "!"))
        await self.handle_commands(message)

        # Respond when @mentioned by name (like a chatter would tag someone)
        if not is_command and not message.echo:
            await self._maybe_reply_to_mention(message)

        # Random engagement only on regular chat, not commands
        if self.random_chat_enabled and not is_command:
            await self._maybe_random_response(message)

        # Loyalty points: earn for chatting (once per 60s per user)
        if not is_command and OVERLAY_AVAILABLE and message.author:
            await self._earn_for_chat(message.author.name)
            # Giveaway keyword entries
            await self._giveaway_keyword(message)

        # Chat bridge: relay regular Twitch chat to Discord via the hub
        if (not is_command and OVERLAY_AVAILABLE and self.bridge_enabled
                and message.author):
            await asyncio.to_thread(
                bridge_post, "twitch", message.author.name,
                message.content or "")

        # Auto-clip: hype detection on every chat message
        if self.autoclip_enabled and not is_command:
            await self._maybe_autoclip(message)

    def _load_clips(self):
        try:
            with open(self._clips_file, "r") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            return []

    def _save_clip(self, url: str, trigger: str):
        clips = self._load_clips()
        clips.append({
            "url": url,
            "trigger": trigger,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        # Keep the last 200 clips
        clips = clips[-200:]
        try:
            with open(self._clips_file, "w") as f:
                json.dump(clips, f, indent=1)
        except Exception as e:
            print(f"[autoclip] couldn't save clips.json: {e}")

    async def _maybe_autoclip(self, message):
        now = time.time()
        # Rolling window of recent message timestamps
        self._chat_timestamps.append(now)
        cutoff = now - self._clip_window
        self._chat_timestamps = [t for t in self._chat_timestamps if t > cutoff]

        # Need the threshold + cooldown respected
        if len(self._chat_timestamps) < self._clip_threshold:
            return
        if now - self._last_clip_ts < self._clip_cooldown:
            return
        if not self._helix_ready():
            return

        self._last_clip_ts = now
        count = len(self._chat_timestamps)
        # Reset the window so one hype spike = one clip
        self._chat_timestamps = []

        url = await asyncio.to_thread(
            create_clip,
            str(self.cfg.get("twitch_token") or ""),
            str(self.cfg.get("twitch_client_id") or ""),
            self.channel_name)
        if url == "NEED_SCOPE":
            print("[autoclip] token needs the clips:edit scope — "
                  "regenerate it at twitchtokengenerator.com")
            return
        if not url:
            print("[autoclip] clip creation failed (offline or API error)")
            return
        self._save_clip(url, f"hype spike ({count} msgs/{self._clip_window}s)")
        print(f"[autoclip] created: {url}")
        if self._clip_announce:
            try:
                await message.channel.send(f"📎 Clipped that moment! {url}")
            except Exception:
                pass

    async def _maybe_reply_to_mention(self, message):
        """If a chatter @s the bot by name, reply with AI (not just commands)."""
        if self.ai_client is None:
            return
        bot_name = (getattr(self, "nick", "") or "").lower()
        if not bot_name:
            return
        content = message.content or ""
        # Match @botname anywhere in the message (case-insensitive)
        if f"@{bot_name}" not in content.lower():
            return
        author = (message.author.name if message.author else "") or ""
        if author.lower() == bot_name or author.lower() == self.channel_name:
            return  # don't reply to self or the broadcaster's own tags
        now = time.time()
        if now - self._last_ai_ts < self.ai_cooldown:
            return  # stay quiet on cooldown — no spam
        # Strip the mention, keep the actual message
        question = content.lower().replace(f"@{bot_name}", "").strip(" ,:;-")
        if not question:
            question = "say hi"
        if self._blocked(question) or self._hateful(question):
            return  # silently ignore hateful mentions
        self._last_ai_ts = now
        answer = await asyncio.to_thread(self._ai, question)
        if not answer:
            return
        if self._blocked(answer) or self._hateful(answer):
            return
        try:
            await message.channel.send(f"@{author} {answer}")
        except Exception:
            pass

    async def _earn_for_chat(self, username: str):
        now = time.time()
        key = (username or "").lower()
        if not key or now - self._earn_cooldowns.get(key, 0) < 60:
            return
        self._earn_cooldowns[key] = now
        await asyncio.to_thread(points_add, username, "twitch", 2)

    async def _giveaway_keyword(self, message):
        g = self._giveaway_cache
        if not g:
            return
        if message.content.strip().lower() == g["keyword"]:
            await asyncio.to_thread(giveaway_enter, message.author.name, "twitch")

    async def _maybe_random_response(self, message):
        now = time.time()
        author = (message.author.name if message.author else "") or ""
        author_lower = author.lower()

        # Skip the broadcaster themself
        if author_lower == self.channel_name:
            return

        # Greet first-time chatters (respects cooldown so it never spams)
        if self.greet_new and author and author_lower not in self.seen_chatters:
            self.seen_chatters.add(author_lower)
            if now - self._last_random_ts >= self.cooldown_secs:
                self._last_random_ts = now
                await message.channel.send(
                    f"Welcome in @{author}! We're making music live tonight 🎶"
                )
            return

        # Every N messages, roll the dice on a random engagement message
        self._msg_count += 1
        threshold = random.randint(
            max(5, self.msg_threshold - 5), self.msg_threshold + 5
        )
        if self._msg_count >= threshold:
            self._msg_count = 0
            if (
                now - self._last_random_ts >= self.cooldown_secs
                and random.random() < self.random_chance
            ):
                self._last_random_ts = now
                await message.channel.send(random.choice(self.random_messages))




    @commands.command(name="music")
    async def cmd_music(self, ctx: commands.Context):
        url = self.links.get("soundcloud", "")
        await ctx.send(f"🎵 My music: {url}" if url else "🎵 SoundCloud link coming soon!")

    @commands.command(name="tip")
    async def cmd_tip(self, ctx: commands.Context):
        url = self.links.get("tip", "")
        await ctx.send(f"💰 Tip the stream: {url}" if url else "💰 Tip link coming soon!")

    @commands.command(name="beats")
    async def cmd_beats(self, ctx: commands.Context):
        url = self.links.get("beats", "")
        await ctx.send(f"🎹 Beats/services: {url}" if url else "🎹 Beat store coming soon!")

    @commands.command(name="shop")
    async def cmd_shop(self, ctx: commands.Context):
        url = self.links.get("shop", "")
        await ctx.send(f"🛒 Merch: {url}" if url else "🛒 Shop coming soon!")

    @commands.command(name="socials")
    async def cmd_socials(self, ctx: commands.Context):
        parts = [f"{k}: {v}" for k, v in self.links.items() if v]
        await ctx.send(" | ".join(parts) if parts else "Socials coming soon!")

    @commands.command(name="schedule")
    async def cmd_schedule(self, ctx: commands.Context):
        sched = self.cfg.get("schedule", "")
        await ctx.send(f"📅 Schedule: {sched}" if sched else "📅 I stream when I'm cooking up — follow to get notified!")

    def _public_base_url(self) -> str:
        """Public tunnel base URL — auto-updated by the launcher every startup.

        Reads .public-url (written by falix-launcher on each boot), falling
        back to the public_url config value. Strips any trailing /overlay.
        """
        url = ""
        try:
            with open(".public-url") as f:
                url = f.read().strip()
        except OSError:
            pass
        if not url:
            url = str(self.cfg.get("public_url") or "").strip()
        # .public-url ends with /overlay — strip it for the base
        if url.endswith("/overlay"):
            url = url[: -len("/overlay")]
        return url.rstrip("/")

    @commands.command(name="arena")
    async def cmd_arena(self, ctx: commands.Context):
        """!arena — post the public arena URLs."""
        base = self._public_base_url()
        if base:
            await ctx.send(
                f"🥊 Watch the arena live: {base}/arena | Stream overlay: {base}/overlay")
        else:
            await ctx.send("🥊 Arena link coming soon — tunnel's still starting!")

    @commands.command(name="sr")
    async def cmd_sr(self, ctx: commands.Context):
        """!sr <song> — request it. One active request per chatter."""
        if not OVERLAY_AVAILABLE:
            return
        parts = ctx.message.content.split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            await ctx.send(f"@{ctx.author.name} usage: !sr <song name or link>")
            return
        if self._on_cooldown("sr", ctx.author.name, 30):
            return
        res = queue_add(ctx.author.name, "twitch", parts[1])
        if res.get("error") == "already":
            await ctx.send(f"@{ctx.author.name} you already have one in the queue! !remove to change it 🎵")
        elif res.get("error") == "full":
            await ctx.send("Queue's full right now — try again later 🎵")
        else:
            await ctx.send(f"🎵 @{ctx.author.name} requested \"{parts[1].strip()[:60]}\" — #{res['position']} in queue! Chat: !vote to push it up")

    @commands.command(name="queue")
    async def cmd_queue(self, ctx: commands.Context):
        """Show the ranked request queue."""
        if not OVERLAY_AVAILABLE:
            return
        items = queue_list()
        if not items:
            await ctx.send("🎵 Queue's empty — !sr <song> to request one!")
            return
        msg = "🎵 UP NEXT: " + " | ".join(
            f"#{i['n']} \"{i['query'][:28]}\" ({i['votes']}❤)" for i in items[:5])
        if len(items) > 5:
            msg += f" (+{len(items) - 5} more)"
        await ctx.send(msg[:450])

    @commands.command(name="vote")
    async def cmd_vote(self, ctx: commands.Context):
        """!vote <number> — push a request up the queue. One active vote each."""
        if not OVERLAY_AVAILABLE:
            return
        parts = ctx.message.content.split()
        if len(parts) < 2 or not parts[1].isdigit():
            await ctx.send(f"@{ctx.author.name} usage: !vote <number> — check !queue for numbers")
            return
        res = queue_vote(ctx.author.name, "twitch", int(parts[1]))
        if res.get("error"):
            await ctx.send(f"@{ctx.author.name} that number's not in the queue 😅")
        else:
            await ctx.send(f"❤ @{ctx.author.name} voted for \"{res['query'][:40]}\" ({res['votes']} votes)")

    @commands.command(name="remove")
    async def cmd_remove(self, ctx: commands.Context):
        """Remove your own request from the queue."""
        if not OVERLAY_AVAILABLE:
            return
        res = queue_remove(ctx.author.name, "twitch")
        if res.get("ok"):
            await ctx.send(f"🗑️ Removed your request \"{res['query'][:40]}\"")
        else:
            await ctx.send(f"@{ctx.author.name} you don't have one in the queue")

    @commands.command(name="skip")
    async def cmd_skip(self, ctx: commands.Context):
        """Mod only: drop the top request and move on."""
        if not OVERLAY_AVAILABLE or not self._is_mod(ctx):
            return
        top = queue_skip()
        if top:
            await ctx.send(f"⏭️ Skipped \"{top['query'][:40]}\" (requested by @{top['user']})")
        else:
            await ctx.send("Queue's empty 📭")

    @commands.command(name="clearqueue")
    async def cmd_clearqueue(self, ctx: commands.Context):
        """Mod only: wipe the request queue."""
        if not OVERLAY_AVAILABLE or not self._is_mod(ctx):
            return
        queue_clear()
        await ctx.send("🧹 Queue cleared!")

    # ---------- Nightbot basics ----------

    @commands.command(name="commands")
    async def cmd_commands(self, ctx: commands.Context):
        await ctx.send(
            "🎛️ !sr !queue !vote !poll !pvote !8ball !ask !roast !gif !photos "
            "!fight !punch !kick !block !special !fighters !points !gamble "
            "!slots !roulette !heist !quote !counter !giveaway !uptime !followage "
            "!stats !music !tip !socials !schedule")

    @commands.command(name="8ball")
    async def cmd_8ball(self, ctx: commands.Context):
        parts = ctx.message.content.split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            await ctx.send(f"@{ctx.author.name} ask the 8ball! !8ball <question> 🎱")
            return
        await ctx.send(f"🎱 {random.choice(shared.EIGHTBALL_ANSWERS)}")

    @commands.command(name="poll")
    async def cmd_poll(self, ctx: commands.Context):
        """!poll question | option1 | option2 — 90s vote, shared across Twitch+Discord."""
        if not OVERLAY_AVAILABLE:
            return
        if self._on_cooldown("poll", ctx.author.name, 60):
            return
        parts = ctx.message.content.split(None, 1)
        if len(parts) < 2:
            await ctx.send(f"@{ctx.author.name} usage: !poll best beat? | A | B")
            return
        bits = [b.strip() for b in parts[1].split("|")]
        if len(bits) < 3:
            await ctx.send(f"@{ctx.author.name} need a question + 2 options! !poll best beat? | A | B")
            return
        res = poll_start(bits[0], bits[1:], by=ctx.author.name)
        if res.get("error"):
            await ctx.send("A poll's already running — vote !pvote <number> 📊")
            return
        opts = "  ".join(f"{i + 1}. {o}" for i, o in enumerate(res["options"]))
        await ctx.send(f"📊 POLL: {bits[0][:100]} — {opts} — !pvote <n> (90s)")

    @commands.command(name="pvote")
    async def cmd_pvote(self, ctx: commands.Context):
        if not OVERLAY_AVAILABLE:
            return
        parts = ctx.message.content.split()
        if len(parts) < 2 or not parts[1].isdigit():
            await ctx.send(f"@{ctx.author.name} usage: !pvote <number>")
            return
        res = poll_vote(ctx.author.name, "twitch", int(parts[1]))
        if res.get("error"):
            await ctx.send(f"@{ctx.author.name} no active poll or bad number 😅")
        else:
            await ctx.send(f"🗳️ @{ctx.author.name} voted!")

    @commands.command(name="pollend")
    async def cmd_pollend(self, ctx: commands.Context):
        """Mod only: end the poll now and post results."""
        if not OVERLAY_AVAILABLE or not self._is_mod(ctx):
            return
        res = poll_end()
        await ctx.send(shared.format_poll(res) if res else "No poll running 📭")

    def _helix_ready(self) -> bool:
        cid = str(self.cfg.get("twitch_client_id") or "")
        return bool(cid and "PASTE" not in cid)

    @commands.command(name="uptime")
    async def cmd_uptime(self, ctx: commands.Context):
        if not self._helix_ready():
            await ctx.send("Uptime isn't hooked up yet 🔧")
            return
        started = await asyncio.to_thread(
            shared.stream_uptime,
            str(self.cfg.get("twitch_client_id") or ""),
            str(self.cfg.get("twitch_client_secret") or ""),
            self.channel_name)
        if not started:
            await ctx.send("Not live right now 😴")
            return
        await ctx.send(f"🔴 Live for {shared.format_duration(started)}!")

    @commands.command(name="followage")
    async def cmd_followage(self, ctx: commands.Context):
        """!followage [@user] — how long they've followed."""
        parts = ctx.message.content.split()
        target = parts[1].lstrip("@")[:25] if len(parts) > 1 else ctx.author.name
        if not self._helix_ready():
            await ctx.send("Followage isn't hooked up yet 🔧")
            return
        res = await asyncio.to_thread(
            shared.follow_date,
            str(self.cfg.get("twitch_token") or ""),
            str(self.cfg.get("twitch_client_id") or ""),
            self.channel_name, target)
        if res == "NEED_SCOPE":
            await ctx.send("Grous needs to add the follows scope to the bot token 🔧")
        elif not res:
            await ctx.send(f"@{target} isn't following (yet!) 👀")
        else:
            await ctx.send(f"@{target} has followed for {shared.format_duration(res)}! 💜")

    @commands.command(name="stats")
    async def cmd_stats(self, ctx: commands.Context):
        """One line: game | title | uptime | viewers."""
        if not self._helix_ready():
            await ctx.send("Stats aren't hooked up yet 🔧")
            return
        info = await asyncio.to_thread(
            shared.stream_info,
            str(self.cfg.get("twitch_client_id") or ""),
            str(self.cfg.get("twitch_client_secret") or ""),
            self.channel_name)
        if not info:
            await ctx.send("⚫ Offline — but the vibes are still on 🎵")
            return
        await ctx.send(
            f"🔴 LIVE | {info['game']} | {info['title'][:60]} | "
            f"{shared.format_duration(info['started_at'])} | {info['viewers']} watching")

    @commands.command(name="clip")
    async def cmd_clip(self, ctx: commands.Context):
        """!clip — manually create a Twitch clip of this moment."""
        if not self._helix_ready():
            await ctx.send("Clips aren't hooked up yet 🔧")
            return
        url = await asyncio.to_thread(
            create_clip,
            str(self.cfg.get("twitch_token") or ""),
            str(self.cfg.get("twitch_client_id") or ""),
            self.channel_name)
        if url == "NEED_SCOPE":
            await ctx.send("Grous needs to add the clips:edit scope to the bot token 🔧")
        elif not url:
            await ctx.send("Couldn't clip that — stream might be offline 😴")
        else:
            self._save_clip(url, f"manual by @{ctx.author.name}")
            await ctx.send(f"📎 Clipped! {url}")

    @commands.command(name="clips")
    async def cmd_clips(self, ctx: commands.Context):
        """!clips — list today's auto-clips."""
        clips = self._load_clips()
        today = time.strftime("%Y-%m-%d")
        todays = [c for c in clips
                  if str(c.get("created_at", "")).startswith(today)]
        if not todays:
            await ctx.send("No clips today yet — get chat hyped! 🔥")
            return
        # Show the last 3 to keep chat tidy
        lines = [c["url"] for c in todays[-3:]]
        await ctx.send(f"📎 Today's clips ({len(todays)}): " + " | ".join(lines))

    # ---------- Loyalty points + minigames ----------

    @commands.command(name="points")
    async def cmd_points(self, ctx: commands.Context):
        parts = ctx.message.content.split()
        target = parts[1].lstrip("@")[:25] if len(parts) > 1 else ctx.author.name
        bal = await asyncio.to_thread(points_get, target, "twitch")
        await ctx.send(f"💰 @{target}: {bal} points")

    @commands.command(name="toppoints")
    async def cmd_toppoints(self, ctx: commands.Context):
        top = await asyncio.to_thread(points_top, 5)
        if not top:
            await ctx.send("No points yet — chat to earn! 💰")
            return
        line = "  ".join(f"{i + 1}. @{p['user']} ({p['balance']})"
                         for i, p in enumerate(top))
        await ctx.send(f"🏆 Top points: {line}")

    @commands.command(name="give")
    async def cmd_give(self, ctx: commands.Context):
        """!give @user <amount> — gift your points."""
        parts = ctx.message.content.split()
        if len(parts) < 3:
            await ctx.send(f"@{ctx.author.name} usage: !give @user <amount>")
            return
        amt = shared.parse_amount(parts[2])
        target = parts[1].lstrip("@")[:25]
        if not amt or not target or target.lower() == ctx.author.name.lower():
            await ctx.send(f"@{ctx.author.name} usage: !give @user <amount>")
            return
        if await asyncio.to_thread(points_get, ctx.author.name, "twitch") < amt:
            await ctx.send(f"@{ctx.author.name} not enough points! 💸")
            return
        await asyncio.to_thread(points_add, ctx.author.name, "twitch", -amt)
        new_bal = await asyncio.to_thread(points_add, target, "twitch", amt)
        await ctx.send(f"🎁 @{ctx.author.name} gave {amt} points to @{target}! (now {new_bal})")

    @commands.command(name="addpoints")
    async def cmd_addpoints(self, ctx: commands.Context):
        """Mod: grant points."""
        if not self._is_mod(ctx):
            return
        parts = ctx.message.content.split()
        if len(parts) < 3:
            return
        amt = shared.parse_amount(parts[2])
        if not amt:
            return
        target = parts[1].lstrip("@")[:25]
        bal = await asyncio.to_thread(points_add, target, "twitch", amt)
        await ctx.send(f"✨ @{target} got {amt} points! (now {bal})")

    @commands.command(name="removepoints")
    async def cmd_removepoints(self, ctx: commands.Context):
        """Mod: take points."""
        if not self._is_mod(ctx):
            return
        parts = ctx.message.content.split()
        if len(parts) < 3:
            return
        amt = shared.parse_amount(parts[2])
        if not amt:
            return
        target = parts[1].lstrip("@")[:25]
        bal = await asyncio.to_thread(points_add, target, "twitch", -amt)
        await ctx.send(f"💸 @{target} lost {amt} points. (now {bal})")

    @commands.command(name="gamble")
    async def cmd_gamble(self, ctx: commands.Context):
        """!gamble <amount> — 50/50 double or nothing."""
        if self._on_cooldown("gamble", ctx.author.name, 10):
            return
        parts = ctx.message.content.split()
        amt = shared.parse_amount(parts[1]) if len(parts) > 1 else None
        if not amt:
            await ctx.send(f"@{ctx.author.name} usage: !gamble <amount>")
            return
        res = await asyncio.to_thread(gamble, ctx.author.name, "twitch", amt)
        if res.get("error"):
            await ctx.send(f"@{ctx.author.name} {shared.bet_error(res['error'])}")
            return
        if res["win"]:
            await ctx.send(f"🎰 @{ctx.author.name} WON {amt * 2} points! (now {res['balance']}) 🤑")
        else:
            await ctx.send(f"🎰 @{ctx.author.name} lost {amt} points... (now {res['balance']}) 💀")

    @commands.command(name="don")
    async def cmd_don(self, ctx: commands.Context):
        """!don <amount> — coin flip, double or nothing."""
        if self._on_cooldown("don", ctx.author.name, 10):
            return
        parts = ctx.message.content.split()
        amt = shared.parse_amount(parts[1]) if len(parts) > 1 else None
        if not amt:
            await ctx.send(f"@{ctx.author.name} usage: !don <amount>")
            return
        res = await asyncio.to_thread(gamble, ctx.author.name, "twitch", amt)
        if res.get("error"):
            await ctx.send(f"@{ctx.author.name} {shared.bet_error(res['error'])}")
            return
        coin = "HEADS" if res["win"] else "TAILS"
        if res["win"]:
            await ctx.send(f"🪙 {coin}! @{ctx.author.name} doubles to {amt * 2}! (now {res['balance']})")
        else:
            await ctx.send(f"🪙 {coin}! @{ctx.author.name} loses {amt}... (now {res['balance']})")

    @commands.command(name="slots")
    async def cmd_slots(self, ctx: commands.Context):
        """!slots [bet] — spin the slot machine."""
        if self._on_cooldown("slots", ctx.author.name, 10):
            return
        parts = ctx.message.content.split()
        bet = shared.parse_amount(parts[1]) if len(parts) > 1 else 10
        if not bet:
            await ctx.send(f"@{ctx.author.name} usage: !slots [bet]")
            return
        res = await asyncio.to_thread(slots, ctx.author.name, "twitch", bet)
        if res.get("error"):
            await ctx.send(f"@{ctx.author.name} {shared.bet_error(res['error'])}")
            return
        reels = "".join(res["reels"])
        if res["mult"]:
            await ctx.send(f"🎰 {reels} @{ctx.author.name} wins {bet * res['mult']}! ({res['mult']}x — now {res['balance']}) 🤑")
        else:
            await ctx.send(f"🎰 {reels} @{ctx.author.name} lost {bet}... (now {res['balance']})")

    @commands.command(name="roulette")
    async def cmd_roulette(self, ctx: commands.Context):
        """!roulette <amount> <red|black|green> — green pays 14x."""
        if self._on_cooldown("roulette", ctx.author.name, 10):
            return
        parts = ctx.message.content.split()
        if len(parts) < 3:
            await ctx.send(f"@{ctx.author.name} usage: !roulette <amount> <red|black|green>")
            return
        amt = shared.parse_amount(parts[1])
        if not amt:
            await ctx.send(f"@{ctx.author.name} usage: !roulette <amount> <red|black|green>")
            return
        res = await asyncio.to_thread(roulette, ctx.author.name, "twitch", amt, parts[2])
        if res.get("error") == "bad_color":
            await ctx.send(f"@{ctx.author.name} pick red, black, or green 🎡")
            return
        if res.get("error"):
            await ctx.send(f"@{ctx.author.name} {shared.bet_error(res['error'])}")
            return
        if res["win"]:
            await ctx.send(f"🎡 {res['spin']} {res['hit']}! @{ctx.author.name} wins {amt * res['mult']}! ({res['mult']}x — now {res['balance']}) 🤑")
        else:
            await ctx.send(f"🎡 {res['spin']} {res['hit']}. @{ctx.author.name} lost {amt}... (now {res['balance']})")

    @commands.command(name="heist")
    async def cmd_heist(self, ctx: commands.Context):
        """!heist <amount> — start/join a 60s crew heist. 55% success, winners get 2.5x."""
        parts = ctx.message.content.split()
        amt = shared.parse_amount(parts[1]) if len(parts) > 1 else None
        if not amt:
            await ctx.send(f"@{ctx.author.name} usage: !heist <amount> (60s to join!)")
            return
        res = await asyncio.to_thread(heist_start, ctx.author.name, "twitch", amt)
        if res.get("error") == "active":
            res = await asyncio.to_thread(heist_join, ctx.author.name, "twitch", amt)
            if res.get("error") == "already":
                await ctx.send(f"@{ctx.author.name} you're already in the heist! 🕶️")
                return
            if res.get("error"):
                await ctx.send(f"@{ctx.author.name} {shared.bet_error(res.get('error', ''))}")
                return
            await ctx.send(f"🕶️ @{ctx.author.name} joined the heist with {amt}! Crew: {res['crew']} — type !heist <amount> to join!")
            return
        if res.get("error"):
            await ctx.send(f"@{ctx.author.name} {shared.bet_error(res['error'])}")
            return
        await ctx.send(f"🚨 HEIST STARTED by @{ctx.author.name}! Type !heist <amount> to join — 60 seconds! 🕶️")

    # ---------- Quotes & counters ----------

    @commands.command(name="quote")
    async def cmd_quote(self, ctx: commands.Context):
        """!quote | !quote add <text> | !quote <n>"""
        if not OVERLAY_AVAILABLE:
            return
        parts = ctx.message.content.split(None, 2)
        if len(parts) >= 3 and parts[1].lower() == "add":
            if len(parts[2]) > 200:
                return
            res = await asyncio.to_thread(quote_add, ctx.author.name, parts[2])
            if res.get("error"):
                await ctx.send("Quote book is full! 📕")
                return
            await ctx.send(f"📝 Quote #{res['id']} saved!")
            return
        n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        q = await asyncio.to_thread(quote_get, n)
        if not q:
            await ctx.send("No quotes yet — !quote add <text> 📝")
            return
        await ctx.send(f"💬 #{q['id']}: \"{q['text']}\" — {q['user']}")

    @commands.command(name="quotedel")
    async def cmd_quotedel(self, ctx: commands.Context):
        """Mod: delete a quote."""
        if not self._is_mod(ctx):
            return
        parts = ctx.message.content.split()
        if len(parts) < 2 or not parts[1].isdigit():
            return
        ok = await asyncio.to_thread(quote_del, int(parts[1]))
        await ctx.send(f"🗑️ Quote #{parts[1]} deleted." if ok else "Quote not found 🤷")

    @commands.command(name="counter")
    async def cmd_counter(self, ctx: commands.Context):
        """!counter <name> | !counter <name> +1 (mods)"""
        parts = ctx.message.content.split()
        if len(parts) < 2:
            await ctx.send(f"@{ctx.author.name} usage: !counter <name>")
            return
        name = parts[1][:30]
        if len(parts) > 2:
            if not self._is_mod(ctx):
                return
            delta = parts[2]
            if delta.startswith("=") and delta[1:].isdigit():
                val = await asyncio.to_thread(counter_bump, name,
                                              int(delta[1:]) - await asyncio.to_thread(counter_get, name))
            elif (delta.startswith("+") or delta.startswith("-")) and delta[1:].isdigit():
                val = await asyncio.to_thread(counter_bump, name, int(delta))
            else:
                return
            await ctx.send(f"🔢 {name}: {val}")
            return
        val = await asyncio.to_thread(counter_get, name)
        await ctx.send(f"🔢 {name}: {val}")

    # ---------- Giveaways ----------

    @commands.command(name="giveaway")
    async def cmd_giveaway(self, ctx: commands.Context):
        """Mod: !giveaway <keyword> [seconds] — chatters type the keyword to enter."""
        if not self._is_mod(ctx):
            return
        parts = ctx.message.content.split()
        if len(parts) < 2:
            await ctx.send("Usage: !giveaway <keyword> [seconds]")
            return
        secs = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 120
        res = await asyncio.to_thread(giveaway_start, parts[1], secs, ctx.author.name)
        if res.get("error"):
            await ctx.send("A giveaway's already running! 🎁")
            return
        self._giveaway_cache = await asyncio.to_thread(giveaway_status)
        await ctx.send(f"🎁 GIVEAWAY! Type \"{res['keyword']}\" to enter — drawing in {secs}s!")

    @commands.command(name="giveawayend")
    async def cmd_giveawayend(self, ctx: commands.Context):
        """Mod: draw the giveaway now."""
        if not self._is_mod(ctx):
            return
        res = await asyncio.to_thread(giveaway_end)
        self._giveaway_cache = None
        if not res:
            await ctx.send("No giveaway running 📭")
            return
        if res["winner"]:
            await ctx.send(f"🎉 GIVEAWAY WINNER: @{res['winner']}! ({res['entries']} entries) 🎉")
        else:
            await ctx.send("🎁 Giveaway ended — no entries 😅")

    # Moderation + AI helpers live in shared.py (used by Twitch + Discord bots).

    async def _timeout_for_hate(self, ctx: commands.Context):
        """Time out the chatter who triggered the hate filter."""
        duration = self.cfg.get("moderation_timeout_seconds", 600)
        try:
            await ctx.send(
                f"/timeout {ctx.author.name} {duration} Hateful content is not welcome here"
            )
        except Exception as e:
            print(f"[timeout] error: {e}")
        await ctx.send(
            f"@{ctx.author.name} timed out for {duration // 60} minutes — keep it respectful."
        )

    def _on_cooldown(self, cmd: str, user: str, secs: int) -> bool:
        """Per-command per-user cooldown. True = still cooling down."""
        now = time.time()
        key = (cmd, (user or "").lower())
        if self._cmd_cooldowns.get(key, 0) + secs > now:
            return True
        self._cmd_cooldowns[key] = now
        return False

    def _is_mod(self, ctx: commands.Context) -> bool:
        author = ctx.author
        return bool(getattr(author, "is_mod", False)
                    or getattr(author, "is_broadcaster", False)
                    or (author.name or "").lower() == self.channel_name)

    def _blocked(self, text: str) -> bool:
        return contains_blocked(text, self.blocked_words)

    def _hateful(self, text: str) -> bool:
        return is_hateful(self.ai_client, text)

    def _ai(self, question: str, system_prompt: str = None) -> str | None:
        return ask_ai(self.ai_client, question, system_prompt or self.ai_system_prompt)

    @commands.command(name="ask")
    async def cmd_ask(self, ctx: commands.Context, *, question: str = None):
        """AI-powered replies. Needs openai_api_key in config.yaml."""
        if self.ai_client is None:
            await ctx.send(
                "AI chat isn't hooked up yet — add an OpenAI key to config.yaml to unlock it 🤖"
            )
            return
        if not question:
            await ctx.send(f"@{ctx.author.name} ask me something! Usage: !ask <your question>")
            return
        now = time.time()
        if now - self._last_ai_ts < self.ai_cooldown:
            await ctx.send(f"@{ctx.author.name} one sec, let me finish this thought ⏳")
            return
        self._last_ai_ts = now
        # Custom blocklist first — those words are never sent to the AI at all.
        # Then OpenAI moderation for everything else hateful/racist.
        # NOTE: the bot account must be a moderator in your channel for
        # timeouts to work. In your Twitch chat (as groustvv), type: /mod grousbot
        if self._blocked(question) or self._hateful(question):
            await self._timeout_for_hate(ctx)
            return
        answer = self._ai(question)
        if answer is None:
            await ctx.send("Brain fart — try again in a sec 🤖")
            return
        # Don't send AI output that trips the filter either
        if self._blocked(answer) or self._hateful(answer):
            await ctx.send("Nah, not going there. Ask something else.")
            return
        await ctx.send(f"@{ctx.author.name} {answer}")

    @commands.command(name="gif")
    async def cmd_gif(self, ctx: commands.Context):
        """!gif [search] — no text = GIF picker link (like the IG keyboard);
        with text = instant top result on stream."""
        if not OVERLAY_AVAILABLE:
            return
        term = ctx.message.content[len("!gif"):].strip()
        key = str(self.cfg.get("giphy_api_key") or "")
        if not key or "PASTE" in key:
            await ctx.send("GIFs aren't hooked up yet — Grous needs to add a GIPHY key 🔧")
            return
        public_url = self._public_base_url()
        if not term:
            # Picker mode — visual search grid on their phone
            if not public_url or "PASTE" in public_url:
                await ctx.send("Usage: !gif <search> — e.g. !gif hype")
                return
            await ctx.send(f"@{ctx.author.name} pick your GIF 🎬👉 {public_url}/gif")
            return
        if self._on_cooldown("gif", ctx.author.name, 30):
            return
        gif_url = giphy_search(key, term)
        if not gif_url:
            await ctx.send(f"No GIFs for '{term[:40]}' 😅 try another word")
            return
        show_gif(gif_url, seconds=8)
        await ctx.send(f"🎬 @{ctx.author.name} dropped a GIF: {term[:40]}")

    @commands.command(name="roast")
    async def cmd_roast(self, ctx: commands.Context):
        """!roast [@user] — AI roast, all love. No target = you get roasted."""
        if self.ai_client is None:
            await ctx.send("Roasts aren't hooked up yet — Grous needs to add an OpenAI key 🔧")
            return
        if self._on_cooldown("roast", ctx.author.name, 30):
            return
        parts = ctx.message.content.split()
        target = parts[1].lstrip("@")[:25] if len(parts) > 1 else ctx.author.name
        prompt = (
            f"Write a playful 1-2 sentence roast of Twitch chatter '{target}'. "
            f"Comedy-roast-battle energy between friends: funny, never cruel. "
            f"Joke about chatter habits (lurking, spamming emotes, bad takes, being broke). "
            f"NEVER use slurs or attack race, gender, sexuality, disability, or religion."
        )
        answer = self._ai(
            prompt,
            system_prompt="You are a comedy roast writer for a Twitch stream. Funny, playful, never hateful.",
        )
        if answer is None:
            await ctx.send("Roast machine broke, try again in a sec 🔧")
            return
        if self._blocked(answer) or self._hateful(answer):
            await ctx.send("That roast went too far — even I have limits 😅")
            return
        await ctx.send(f"🔥 @{target} — {answer}")

    @commands.command(name="photos")
    async def cmd_photos(self, ctx: commands.Context):
        """!photos — get the upload link so your pics/vids can hit the stream."""
        if not OVERLAY_AVAILABLE:
            return
        public_url = self._public_base_url()
        if not public_url or "PASTE" in public_url:
            await ctx.send("Photo uploads aren't live yet — Grous is setting it up 📸")
            return
        await ctx.send(
            f"@{ctx.author.name} upload your pic/clip here — best ones hit the stream! "
            f"👉 {public_url}/upload"
        )

    @commands.command(name="approve")
    async def cmd_approve(self, ctx: commands.Context):
        """Mod only: approve the oldest pending upload -> plays on stream."""
        if not OVERLAY_AVAILABLE or not self._is_mod(ctx):
            return
        item = approve_next()
        if item:
            await ctx.send(f"✅ On stream now: {item['user']}'s upload!")
        else:
            await ctx.send("Queue's empty 📭")

    @commands.command(name="deny")
    async def cmd_deny(self, ctx: commands.Context):
        """Mod only: delete the oldest pending upload."""
        if not OVERLAY_AVAILABLE or not self._is_mod(ctx):
            return
        item = deny_next()
        if item:
            await ctx.send(f"❌ Denied {item['user']}'s upload")
        else:
            await ctx.send("Queue's empty 📭")

    # ---------- King of the Hill arena ----------

    @commands.command(name="fight")
    async def cmd_fight(self, ctx: commands.Context):
        """!fight — claim the hill, challenge the champ, or join the line."""
        if not OVERLAY_AVAILABLE:
            return
        res = arena_join(ctx.author.name, "twitch")
        status = res["status"]
        if status == "already":
            await ctx.send(f"@{ctx.author.name} you're already in! 🥊")
        elif status == "champ":
            await ctx.send(f"👑 @{ctx.author.name} takes the hill! Who's stepping up? Type !fight!")
        elif status == "fight":
            await ctx.send(f"🥊 @{ctx.author.name} challenges @{res['opponent']}! FIGHT!")
        elif status == "queued":
            await ctx.send(f"@{ctx.author.name} you're #{res['position']} in line! 🥊")
        elif status == "full":
            await ctx.send("The line's full — wait for the next round 🥊")

    async def _arena_move(self, ctx: commands.Context, move: str):
        if not OVERLAY_AVAILABLE:
            return
        res = arena_move(ctx.author.name, "twitch", move)
        err = res.get("error")
        if err:
            if err == "not_in":
                await ctx.send(f"@{ctx.author.name} you're not in this fight — !fight to join 🥊")
            elif err == "no_fight":
                await ctx.send(f"@{ctx.author.name} no fight running yet — !fight to start one 🥊")
            elif err == "not_ready":
                await ctx.send(f"@{ctx.author.name} !special only unlocks under 40% HP! 🩸")
            # countdown + cooldown stay silent to avoid chat spam
            return
        if res["move"] == "block":
            await ctx.send(f"🛡️ @{ctx.author.name} blocks!")
            return
        msg = (f"💥 @{res['by']} {move}s @{res['target']} for {res['damage']}"
               f"{' (blocked!)' if res['blocked'] else ''} — {res['target']}: {res['target_hp']}HP")
        if res.get("ko"):
            msg += f" ☠️ K.O.! 🏆 @{res['winner']} keeps the hill!"
        await ctx.send(msg)

    @commands.command(name="punch")
    async def cmd_punch(self, ctx: commands.Context):
        """Quick jab: 8-14 dmg, 3s cooldown."""
        await self._arena_move(ctx, "punch")

    @commands.command(name="kick")
    async def cmd_kick(self, ctx: commands.Context):
        """Heavy kick: 14-22 dmg, 6s cooldown."""
        await self._arena_move(ctx, "kick")

    @commands.command(name="block")
    async def cmd_block(self, ctx: commands.Context):
        """Block: next hits do 75% less for 6s. 10s cooldown."""
        await self._arena_move(ctx, "block")

    @commands.command(name="special")
    async def cmd_special(self, ctx: commands.Context):
        """Comeback move: 25-40 dmg, only under 40% HP. 15s cooldown."""
        await self._arena_move(ctx, "special")

    @commands.command(name="fighters")
    async def cmd_fighters(self, ctx: commands.Context):
        """Who's champ, who's fighting, who's in line."""
        if not OVERLAY_AVAILABLE:
            return
        s = arena_status()
        if not s["champ"]:
            await ctx.send("🥊 The hill is empty — type !fight to claim it!")
            return
        msg = f"👑 Champ: @{s['champ']}"
        if s["challenger"]:
            msg += f"  vs  🥊 @{s['challenger']} ({s['challenger_hp']}HP)"
        else:
            msg += " (waiting for a challenger...)"
        if s["queue"]:
            msg += f" | Line: {', '.join('@' + q for q in s['queue'][:5])}"
        await ctx.send(msg)

    @commands.command(name="fightreset")
    async def cmd_fightreset(self, ctx: commands.Context):
        """Mod only: clear the arena."""
        if not OVERLAY_AVAILABLE or not self._is_mod(ctx):
            return
        arena_reset()
        await ctx.send("🥊 Arena cleared! Type !fight to claim the hill!")




    async def _arena_watchdog(self):
        """Background: stalled fights, expired polls/heists/giveaways, giveaway cache."""
        while True:
            await asyncio.sleep(10)
            if not OVERLAY_AVAILABLE:
                continue
            try:
                channel = self.get_channel(self.channel_name)
                ev = arena_tick()
                if ev and channel is not None:
                    if ev["event"] == "stall":
                        await channel.send(f"⏱️ Fight stalled — 👑 @{ev['champ']} keeps the hill! Type !fight!")
                    elif ev["event"] == "stall_next":
                        await channel.send(f"⏱️ Fight stalled! 👑 @{ev['champ']} keeps the hill — 🥊 @{ev['next']} steps up! FIGHT!")
                pres = poll_tick()
                if pres and channel is not None:
                    await channel.send(shared.format_poll(pres))
                hres = heist_tick()
                if hres and channel is not None:
                    crew = ", ".join(f"@{c['user']} ({c['stake']})" for c in hres["crew"])
                    if hres["success"]:
                        await channel.send(f"💰 HEIST SUCCESS! The crew splits the loot: {crew} — everyone gets 2.5x! 🤑")
                    else:
                        await channel.send(f"🚔 HEIST BUSTED! {crew} lost their stakes... 💀")
                gres = giveaway_tick()
                if gres and channel is not None:
                    if gres["winner"]:
                        await channel.send(f"🎉 GIVEAWAY WINNER: @{gres['winner']}! ({gres['entries']} entries) 🎉")
                    else:
                        await channel.send("🎁 Giveaway ended — no entries 😅")
                self._giveaway_cache = giveaway_status()
            except Exception as e:
                print(f"[arena-watchdog] {e}")

    async def _bridge_watchdog(self):
        """Relay Discord chat into Twitch + watch for the channel going live."""
        tick = 0
        golive_every = max(1, self._golive_check_seconds // 5)
        while True:
            await asyncio.sleep(5)
            if not OVERLAY_AVAILABLE or not self.bridge_enabled:
                continue
            try:
                channel = self.get_channel(self.channel_name)
                msgs = await asyncio.to_thread(bridge_get, self._bridge_since)
                for m in msgs or []:
                    self._bridge_since = max(self._bridge_since, m.get("ts", 0))
                    if m.get("from") == "discord" and channel is not None:
                        user = str(m.get("user", "?"))[:25]
                        text = str(m.get("text", ""))
                        if text:
                            await channel.send(f"[Discord] {user}: {text}")
                            await asyncio.sleep(1)  # stay gentle on rate limits
                tick += 1
                if tick >= golive_every:
                    tick = 0
                    await self._golive_check()
            except Exception as e:
                print(f"[bridge-watchdog] {e}")

    async def _golive_check(self):
        """Post a bridge alert when the channel transitions offline -> live."""
        cid = str(self.cfg.get("twitch_client_id") or "")
        csec = str(self.cfg.get("twitch_client_secret") or "")
        if not cid or "PASTE" in cid or not csec or "PASTE" in csec:
            return  # needs the same Helix creds as !uptime
        try:
            info = await asyncio.to_thread(
                shared.stream_info, cid, csec, self.channel_name)
        except Exception as e:
            print(f"[golive] helix check failed: {e}")
            return
        is_live = bool(info)
        if self._was_live is None:  # first check just sets the baseline
            self._was_live = is_live
            return
        if is_live and not self._was_live:
            title = (info.get("title") or "").strip()
            game = (info.get("game") or "").strip()
            text = f"🔴 {self.channel_name} is LIVE"
            if game:
                text += f" playing {game}"
            if title:
                text += f" — {title}"
            await asyncio.to_thread(bridge_post, "system", "golive", text)
            print(f"[golive] {text}")
        self._was_live = is_live


if __name__ == "__main__":
    config = load_config()
    config = ensure_fresh_token(config)
    _require_config_keys(config)
    bot = GrousBot(config)
    try:
        bot.run()
    except Exception as e:
        # A loud one-line diagnosis for the console. The exception is
        # re-raised so the traceback — and the hellhound's auth sniffing
        # on the bot's output — still reaches the log unchanged.
        _detail = f"{type(e).__name__}: {e}".lower()
        if any(s in _detail for s in ("loginfailure", "authenticationerror",
                                      "invalid access token", "unauthorized")):
            print("[twitch] Login failure — Twitch refused the token. "
                  "Mint a fresh pair and paste the access + refresh tokens "
                  "into config.yaml, then restart.", flush=True)
        else:
            print(f"[twitch] bot exited with error: {type(e).__name__}: {e}",
                  flush=True)
        raise
