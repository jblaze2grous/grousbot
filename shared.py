"""Shared helpers for GrousBot — used by both the Twitch and Discord bots.

Pure functions only: no TwitchIO or discord.py imports here, so either
bot can use them.
"""
import json
import re
import urllib.parse
import urllib.request

# Built-in patterns for obfuscated slurs (leet-speak, spaced out, etc.).
# Always blocked — never sent to the AI.
_SLUR_PATTERNS = [
    # f-slur and variations: fag(s), faggot(s), f4g, f@gg0t, ...
    re.compile(r"\bf[\W_]*[@a4][\W_]*[gq69](?:[\W_]*[gq69][\W_]*[o0][\W_]*t)?s?\b", re.IGNORECASE),
    # n-word hard-r and variations: n1gger, nigg3r, n i g g e r, niggers, ...
    re.compile(r"\bn[\W_]*[1i!][\W_]*[gq69][\W_]*[gq69][\W_]*[e3][\W_]*rs?\b", re.IGNORECASE),
]


def contains_blocked(text: str, blocked_words) -> bool:
    """True if text hits the custom blocklist or built-in slur patterns."""
    if not text:
        return False
    lowered = text.lower()
    for word in (blocked_words or []):
        if re.search(r"\b" + re.escape(str(word).lower()) + r"\b", lowered):
            return True
    return any(p.search(text) for p in _SLUR_PATTERNS)


def make_ai_client(api_key):
    """Build an OpenAI client, or None if no key / library missing / env broken."""
    try:
        from openai import OpenAI
    except ImportError:
        return None
    api_key = (api_key or "").strip()
    if not api_key:
        return None
    try:
        return OpenAI(api_key=api_key)
    except Exception as e:
        # e.g. malformed proxy env vars — AI stays off, bot keeps running
        print(f"[ai] client init failed ({e}); !ask/!roast disabled")
        return None


def is_hateful(ai_client, text: str) -> bool:
    """Check text with OpenAI's free moderation endpoint. True = hateful."""
    if ai_client is None or not text:
        return False
    try:
        result = ai_client.moderations.create(input=text[:2000])
        return bool(result.results[0].flagged)
    except Exception as e:
        print(f"[moderation] error: {e}")
        return False


def ask_ai(ai_client, question: str, system_prompt: str) -> str | None:
    """Send a prompt to OpenAI. Returns reply text or None on failure."""
    if ai_client is None or not system_prompt:
        return None
    try:
        resp = ai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question[:300]},
            ],
            max_tokens=120,
        )
        answer = (resp.choices[0].message.content or "").strip()
        if len(answer) > 450:
            answer = answer[:447] + "..."
        return answer or None
    except Exception as e:
        print(f"[AI] error: {e}")
        return None


def giphy_search(api_key: str, term: str) -> str | None:
    """Return the top GIPHY GIF url for a search term, or None."""
    api_key = (api_key or "").strip()
    if not api_key or "PASTE" in api_key or not term:
        return None
    try:
        url = ("https://api.giphy.com/v1/gifs/search?api_key=" + api_key
               + "&q=" + urllib.parse.quote(term) + "&limit=1&rating=g")
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        results = data.get("data") or []
        if not results:
            return None
        images = results[0].get("images") or {}
        return (images.get("original") or {}).get("url")
    except Exception as e:
        print(f"[giphy] error: {e}")
        return None


EIGHTBALL_ANSWERS = [
    "It is certain.", "Without a doubt.", "Yes definitely.", "Most likely.",
    "Signs point to yes.", "As I see it, yes.", "Reply hazy, try again.",
    "Ask again later.", "Cannot predict now.", "Concentrate and ask again.",
    "Don't count on it.", "My reply is no.", "Outlook not so good.",
    "Very doubtful.", "Bet.", "Absolutely not.", "Yeah, run it.",
    "The vibe says yes.", "Nah fam.", "Trust.",
]


# --- Twitch Helix helpers (for !uptime, !followage) ---
import urllib.error as _urlerror
import datetime as _dt


def get_app_token(client_id: str, client_secret: str):
    """Client-credentials app token, or None."""
    try:
        data = urllib.parse.urlencode({
            "client_id": client_id, "client_secret": client_secret,
            "grant_type": "client_credentials"}).encode()
        req = urllib.request.Request("https://id.twitch.tv/oauth2/token", data=data)
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode()).get("access_token")
    except Exception:
        return None


def _helix(client_id: str, token: str, path: str):
    req = urllib.request.Request(
        "https://api.twitch.tv/helix/" + path,
        headers={"Client-ID": client_id, "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def _helix_post(client_id: str, token: str, path: str):
    req = urllib.request.Request(
        "https://api.twitch.tv/helix/" + path,
        data=b"",
        headers={"Client-ID": client_id, "Authorization": f"Bearer {token}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def _helix_patch(client_id: str, token: str, path: str, payload: dict):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        "https://api.twitch.tv/helix/" + path,
        data=body,
        headers={"Client-ID": client_id,
                 "Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        method="PATCH")
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read().decode().strip()  # 204 No Content on success
        return json.loads(raw) if raw else {}


def _clean_token(token: str) -> str:
    t = (token or "").strip()
    if t.lower().startswith("oauth:"):
        t = t[6:]
    return t


def create_clip(bot_token: str, client_id: str, broadcaster_login: str):
    """Create a Twitch clip of the broadcaster's live stream.

    Needs the bot's user token with the clips:edit scope.
    Returns the clip URL (https://clips.twitch.tv/<id>) or None.
    Returns "NEED_SCOPE" if the token lacks the clips:edit scope.
    """
    token = _clean_token(bot_token)
    if not token or not client_id or not broadcaster_login:
        return None
    try:
        u = _helix(client_id, token,
                   f"users?login={urllib.parse.quote(broadcaster_login)}")
        users = u.get("data") or []
        if not users:
            return None
        broadcaster_id = users[0]["id"]
        c = _helix_post(client_id, token, f"clips?broadcaster_id={broadcaster_id}")
        clips = c.get("data") or []
        if not clips:
            return None
        clip_id = clips[0].get("id")
        return f"https://clips.twitch.tv/{clip_id}" if clip_id else None
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return "NEED_SCOPE"
        return None
    except Exception:
        return None


def set_channel_title(bot_token: str, client_id: str, broadcaster_login: str,
                      title: str):
    """Update the broadcaster's stream title (Helix Modify Channel Info).

    Needs the bot's user token with the channel:manage:broadcast scope —
    same token family as clips:edit, granted by the broadcaster.
    Returns the final title string on success, "NEED_SCOPE" on 401/403,
    or None on any other failure.
    """
    token = _clean_token(bot_token)
    title = (title or "").strip()[:140]  # Twitch title max length
    if not token or not client_id or not broadcaster_login or not title:
        return None
    try:
        u = _helix(client_id, token,
                   f"users?login={urllib.parse.quote(broadcaster_login)}")
        users = u.get("data") or []
        if not users:
            return None
        broadcaster_id = users[0]["id"]
        _helix_patch(client_id, token,
                     f"channels?broadcaster_id={broadcaster_id}",
                     {"title": title})
        return title
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return "NEED_SCOPE"
        return None
    except Exception:
        return None


def stream_uptime(client_id: str, client_secret: str, login: str):
    """Returns the stream's started_at datetime, or None if offline/unavailable."""
    token = get_app_token(client_id, client_secret)
    if not token:
        return None
    try:
        data = _helix(client_id, token, f"streams?user_login={urllib.parse.quote(login)}")
        arr = data.get("data") or []
        if not arr:
            return None
        return _dt.datetime.fromisoformat(arr[0]["started_at"].replace("Z", "+00:00"))
    except Exception:
        return None


def stream_info(client_id: str, client_secret: str, login: str):
    """One-shot stream vitals: game, title, viewers, started_at. None if offline."""
    token = get_app_token(client_id, client_secret)
    if not token:
        return None
    try:
        data = _helix(client_id, token, f"streams?user_login={urllib.parse.quote(login)}")
        arr = data.get("data") or []
        if not arr:
            return None
        s = arr[0]
        return {"game": s.get("game_name") or "Just Chatting",
                "title": s.get("title") or "",
                "viewers": s.get("viewer_count", 0),
                "started_at": _dt.datetime.fromisoformat(
                    s["started_at"].replace("Z", "+00:00"))}
    except Exception:
        return None


def format_duration(started_at) -> str:
    """Human duration between a past datetime and now: '2h 14m'."""
    delta = _dt.datetime.now(_dt.timezone.utc) - started_at
    s = max(0, int(delta.total_seconds()))
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def follow_date(bot_token: str, client_id: str, broadcaster_login: str, user_login: str):
    """Returns followed_at datetime, 'NEED_SCOPE', or None (not following / error).

    Needs the bot's token to carry moderator:read:followers.
    """
    try:
        bu = _helix(client_id, bot_token,
                    f"users?login={urllib.parse.quote(broadcaster_login)}")
        uu = _helix(client_id, bot_token,
                    f"users?login={urllib.parse.quote(user_login)}")
        bd = (bu.get("data") or [None])[0]
        ud = (uu.get("data") or [None])[0]
        if not bd or not ud:
            return None
        f = _helix(client_id, bot_token,
                   f"channels/followers?broadcaster_id={bd['id']}&user_id={ud['id']}")
        arr = f.get("data") or []
        if not arr:
            return None
        return _dt.datetime.fromisoformat(arr[0]["followed_at"].replace("Z", "+00:00"))
    except _urlerror.HTTPError as e:
        if e.code in (401, 403):
            return "NEED_SCOPE"
        return None
    except Exception:
        return None


def format_poll(res) -> str:
    """One-line poll results for chat."""
    if not res or not res.get("results"):
        return "📊 Poll closed — no votes cast."
    top = res["results"][0]
    parts = ", ".join(f"{r['text']} ({r['votes']})" for r in res["results"])
    return (f"📊 POLL RESULTS: {res['question'][:80]} — {parts} "
            f"— winner: {top['text']}!")


def parse_amount(s) -> int | None:
    """Positive integer or None."""
    try:
        n = int(s)
        return n if n >= 1 else None
    except (TypeError, ValueError):
        return None


def bet_error(err: str) -> str:
    return {"bad_amount": "bet a real number of points 😅",
            "broke": "not enough points! Chat to earn more 💰"}.get(err, "bet failed 😅")
