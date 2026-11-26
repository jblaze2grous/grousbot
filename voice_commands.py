"""Wake-word + voice command parsing for GrousBot — pure functions, no
Twitch/OpenAI imports, so this module is unit-testable on its own.

How voice commands work now (2026-11-19 fix):
  * Nothing is a command unless the streamer says the wake word first:
    "GrousBot" — then the command. General chatting never fires anything.
  * Whisper mishears the name a lot ("grows bot", "gross bot", ...), so
    a small alias list is matched on word windows, not on exact text.
  * After the wake word, the words that follow are matched against the
    voice-safe command table below — the same actions the bot already
    does in chat, minus the ones that need a chatter's identity or a
    multi-turn chat context (polls, votes, points, fights, giveaways).
  * A command split across two audio chunks still fires: if a chunk ends
    with the first word of a two-word wake alias ("grows ..."), the tail
    is carried into the next chunk. See `pending_wake_tail`.
"""

import re

# Word-window aliases for the wake word, most likely first. Whisper hears
# "GrousBot" as these variants; each alias is a tuple of words.
WAKE_WORD_ALIASES = (
    ("grousbot",),
    ("growsbot",),
    ("grossbot",),
    ("grous", "bot"),
    ("grows", "bot"),
    ("gross", "bot"),
    ("grouse", "bot"),
    ("grows", "bought"),
    ("gross", "bought"),
    ("grous", "bought"),
    ("grouse", "bought"),
    ("growth", "bot"),
)

# First words of the two-word aliases — a chunk ending on one of these
# may be a wake word split across the chunk boundary.
WAKE_FIRST_WORDS = frozenset(a[0] for a in WAKE_WORD_ALIASES if len(a) == 2)

# Per-action cooldowns in seconds (defaults; config `voice_listen:
# cooldowns:` overrides per action). The old code had ONE 60s cooldown
# shared by every trigger, so a second command inside a minute was
# silently dropped no matter what it was — that was half of the
# "halfway working" feeling. Each action now cools down on its own.
DEFAULT_COOLDOWNS = {
    "clip": 30,
    "say_hi": 60,
    "shoutout": 30,
    "set_title": 30,
    "uptime": 15,
    "info": 15,
    "eightball": 15,
    "queue": 15,
    "skip": 15,
}

# Info commands: spoken phrase -> the chat command whose reply text is
# reused (bot.py builds the exact same message as !music etc.).
INFO_COMMANDS = {
    ("music",): "music",
    ("my", "music"): "music",
    ("tip",): "tip",
    ("beats",): "beats",
    ("shop",): "shop",
    ("merch",): "shop",
    ("socials",): "socials",
    ("schedule",): "schedule",
    ("commands",): "commands",
    ("arena",): "arena",
}


def tokenize(text: str):
    """Lowercase words; digits/underscores kept so usernames and titles
    survive intact."""
    return re.findall(r"[a-z0-9_']+", (text or "").lower())


def find_wake_word(words):
    """Index just AFTER the wake word in `words`, or -1 if none.

    Matches any alias on a word window. The first word of an alias may
    carry a verb ending ("growsbot" vs "growsbought" is NOT matched —
    only a plain prefix on the alias's first word, same trick the old
    trigger matcher used for "clip"/"clipped")."""
    for i in range(len(words)):
        for alias in WAKE_WORD_ALIASES:
            if i + len(alias) > len(words):
                continue
            window = words[i:i + len(alias)]
            if len(alias) == 1:
                if window[0] == alias[0] or window[0].startswith(alias[0]):
                    return i + 1
            else:
                if window[0] == alias[0] and window[1] == alias[1]:
                    return i + 2
    return -1


def pending_wake_tail(words):
    """Trailing words of a chunk that might be the start of a wake word
    split across the chunk boundary. Returns them (usually [] or one
    word) so the caller can prepend them to the next chunk."""
    if not words:
        return []
    if words[-1] in WAKE_FIRST_WORDS:
        return [words[-1]]
    return []


def _match(after, *phrases):
    """If `after` starts with one of the phrases, return (len, rest).
    Longest phrase wins. Otherwise (0, after)."""
    best_len, best_rest = 0, after
    for phrase in phrases:
        if len(phrase) <= len(after) and list(after[:len(phrase)]) == list(phrase):
            if len(phrase) > best_len:
                best_len, best_rest = len(phrase), after[len(phrase):]
    return best_len, best_rest


def parse_voice_command(text: str):
    """Parse one transcript. Returns (action, args) or None.

    action is one of: clip, say_hi, shoutout, set_title, uptime,
    info, eightball, queue, skip. args is a list of words after the
    command phrase (username for shoutout, title words for set_title,
    question words for eightball, nothing for the rest). info actions
    return ("info", [command_name]) — the name is args[0].
    """
    words = tokenize(text)
    wake_end = find_wake_word(words)
    if wake_end < 0:
        return None
    after = words[wake_end:]
    if not after:
        return None  # wake word alone, no command after it

    n, rest = _match(after, ("clip", "that"), ("clip", "it"),
                     ("make", "a", "clip"), ("clip",))
    if n:
        return ("clip", rest)
    n, rest = _match(after, ("say", "hello"), ("say", "hi"),
                     ("say", "whats", "up"), ("say", "what's", "up"))
    if n:
        return ("say_hi", rest)
    n, rest = _match(after, ("shout", "out"), ("shoutout",))
    if n:
        return ("shoutout", rest)
    n, rest = _match(after, ("set", "title"), ("change", "the", "title"),
                     ("change", "title"), ("new", "title"), ("title",))
    if n:
        return ("set_title", rest)
    n, rest = _match(after, ("how", "long", "have", "i", "been", "live"),
                     ("how", "long", "live"), ("uptime",))
    if n:
        return ("uptime", rest)
    n, rest = _match(after, ("eight", "ball"), ("8ball",),
                     ("magic", "eight", "ball"), ("magic", "8", "ball"))
    if n:
        return ("eightball", rest)
    n, rest = _match(after, ("show", "the", "queue"), ("show", "queue"),
                     ("whats", "in", "the", "queue"), ("queue",))
    if n:
        return ("queue", rest)
    n, rest = _match(after, ("skip", "the", "song"), ("skip", "song"),
                     ("skip",))
    if n:
        return ("skip", rest)
    for phrase, name in INFO_COMMANDS.items():
        n, rest = _match(after, phrase)
        if n:
            return ("info", [name] + rest)
    return None
