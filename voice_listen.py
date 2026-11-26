"""Voice listening for GrousBot — hears the stream like a chatter would.

How it works:
  1. Resolves the channel's HLS audio feed via streamlink (same feed viewers get)
  2. Records audio in short chunks and sends them to OpenAI's speech-to-text API
  3. Calls the provided callback with transcript text

Transcription model (2026-11-19 fix): `gpt-4o-mini-transcribe` — the
current fast model, noticeably quicker and more accurate on short
commands/names than the old `whisper-1`, at a lower per-minute cost.
If the model ever errors (e.g. not enabled on the key), it falls back
to `whisper-1` once per chunk instead of failing the chunk. Latency
is dominated by chunk length: with 6s chunks a spoken command fires
~8s after it is said, vs ~20s on the old 15s chunks.

Two recording paths:
  - ffmpeg (preferred): whichever ffmpeg on the host passes a 1-second
    self-test transcode — hosts differ, and a bundled binary that
    segfaults on one host works fine on another, so we probe, not assume.
  - raw (fallback): no ffmpeg at all. Downloads the newest HLS segments
    and extracts the ADTS AAC audio in pure Python; Whisper accepts it.

Runs as a background asyncio task. Only listens while the channel is live.
Costs ~$0.006/min of audio via Whisper API (~$1 per 3-hour stream).

Requires (pip): streamlink, imageio-ffmpeg (ffmpeg itself optional)
"""

import asyncio
import os
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import json


# ---------------------------------------------------------------- ffmpeg ---

def _ffmpeg_candidates():
    """Possible ffmpeg executables, most-preferred first, deduped."""
    cands = []
    try:
        import imageio_ffmpeg
        cands.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        pass
    cands.append("ffmpeg")  # system install, if the host has one
    seen, out = set(), []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


async def _probe_ffmpeg(exe: str):
    """True if `exe` can actually transcode 1s of silence on this host.
    Catches missing binaries AND binaries that crash (e.g. SIGSEGV)."""
    probe = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            probe = tf.name
        proc = await asyncio.create_subprocess_exec(
            exe, "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
            "-t", "1", probe,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE)
        _, err = await asyncio.wait_for(proc.communicate(), timeout=20)
        ok = (proc.returncode == 0 and os.path.exists(probe)
              and os.path.getsize(probe) > 1000)
        detail = f"rc={proc.returncode}"
        if not ok and err:
            detail += " " + err.decode(errors="replace").strip()[:120]
        return ok, detail
    except Exception as e:
        return False, str(e)[:120]
    finally:
        if probe:
            try:
                os.unlink(probe)
            except Exception:
                pass


async def _select_ffmpeg():
    """Probe candidates, return the first working ffmpeg (or None)."""
    for exe in _ffmpeg_candidates():
        ok, detail = await _probe_ffmpeg(exe)
        print(f"[voice] ffmpeg probe {exe}: {'OK' if ok else 'FAILED ' + detail}")
        if ok:
            return exe
    return None


async def _record_chunk_ffmpeg(ffmpeg: str, audio_url: str,
                               chunk_seconds: int, wav_path: str):
    """Record one chunk with ffmpeg. Returns (ok, detail) — never hangs:
    hard timeout at chunk_seconds + 45, process killed on expiry."""
    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-y", "-loglevel", "error",
            "-i", audio_url,
            "-t", str(chunk_seconds),
            "-ac", "1", "-ar", "16000",
            wav_path,
            stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(
                proc.communicate(), timeout=chunk_seconds + 45)
            rc = proc.returncode
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return False, f"ffmpeg timed out after {chunk_seconds + 45}s"
        size = os.path.getsize(wav_path) if os.path.exists(wav_path) else 0
        if rc == 0 and size > 1000:
            return True, f"{size // 1024}KB"
        tail = (err or b"").decode(errors="replace").strip().split("\n")
        return False, (f"ffmpeg rc={rc} size={size}B: "
                       + (tail[-1][:160] if tail and tail[-1] else "no stderr"))
    except Exception as e:
        return False, f"record error: {e}"


# ------------------------------------------------------- raw HLS capture ---

def _fetch_playlist_segments(playlist_url: str):
    """Parse an HLS media playlist -> [(duration_seconds, segment_url)]."""
    req = urllib.request.Request(
        playlist_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        text = r.read().decode(errors="replace")
    segs, dur = [], 0.0
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            try:
                dur = float(line.split(":", 1)[1].split(",")[0])
            except Exception:
                dur = 0.0
        elif line and not line.startswith("#"):
            segs.append((dur, urllib.parse.urljoin(playlist_url, line)))
            dur = 0.0
    return segs


def _extract_adts(ts: bytes) -> bytes:
    """Extract ADTS AAC audio from MPEG-TS bytes (pure Python).

    Walks PAT -> PMT to find the AAC audio PID, strips PES headers,
    then re-walks the ADTS frames by their length fields so the output
    starts exactly on a frame boundary with no partial frames."""
    def packets(data):
        # Resync to the 188-byte packet grid in case the buffer doesn't
        # start exactly on a boundary.
        base = 0
        for cand in range(0, 188):
            if (cand + 376 < len(data) and data[cand] == 0x47
                    and data[cand + 188] == 0x47
                    and data[cand + 376] == 0x47):
                base = cand
                break
        for off in range(base, len(data) - 187, 188):
            if data[off] != 0x47:
                continue
            b1, b2, b3 = data[off + 1], data[off + 2], data[off + 3]
            pid = ((b1 & 0x1F) << 8) | b2
            pusi = bool(b1 & 0x40)
            afc = (b3 >> 4) & 0x3
            start = off + 4
            if afc in (2, 3):  # adaptation field present
                start += 1 + data[start]
            if start <= off + 188:
                yield pid, pusi, data[start:off + 188]

    pmt_pid, audio_pid = None, None
    for pid, pusi, payload in packets(ts):
        if pid == 0 and pusi and payload:
            body = payload[1 + payload[0]:]  # skip pointer field
            if len(body) >= 12 and body[0] == 0x00:
                for i in range(8, len(body) - 3, 4):
                    prog = (body[i] << 8) | body[i + 1]
                    if prog != 0:
                        pmt_pid = ((body[i + 2] & 0x1F) << 8) | body[i + 3]
                        break
            if pmt_pid:
                break
    if pmt_pid:
        for pid, pusi, payload in packets(ts):
            if pid == pmt_pid and pusi and payload:
                body = payload[1 + payload[0]:]
                if len(body) >= 12:
                    prog_info_len = ((body[10] & 0x0F) << 8) | body[11]
                    i = 12 + prog_info_len
                    while i + 4 < len(body):
                        stype = body[i]
                        epid = ((body[i + 1] & 0x1F) << 8) | body[i + 2]
                        es_len = ((body[i + 3] & 0x0F) << 8) | body[i + 4] \
                            if i + 4 < len(body) else 0
                        if stype in (0x0F, 0x11, 0x1B, 0x24):
                            # prefer AAC (0x0F ADTS / 0x11 LATM); keep looking
                            if stype in (0x0F, 0x11):
                                audio_pid = epid
                                break
                            if audio_pid is None:
                                audio_pid = epid
                        i += 5 + es_len
                if audio_pid:
                    break
    if not audio_pid:
        return b""

    raw = bytearray()
    for pid, pusi, payload in packets(ts):
        if pid != audio_pid or not payload:
            continue
        if pusi:
            # PES packet: 00 00 01 <id> <len2> <flags> <flags> <hdrlen> ...
            if len(payload) >= 9 and payload[:3] == b"\x00\x00\x01":
                payload = payload[9 + payload[8]:]
            else:
                continue
        raw += payload

    # Re-walk ADTS frames from the first sync word for a clean stream.
    out = bytearray()
    i = 0
    while i + 7 <= len(raw):
        if raw[i] == 0xFF and (raw[i + 1] & 0xF0) == 0xF0:
            flen = ((raw[i + 3] & 0x03) << 11) | (raw[i + 4] << 3) \
                | ((raw[i + 5] >> 5) & 0x07)
            if 7 <= flen and i + flen <= len(raw):
                out += raw[i:i + flen]
                i += flen
                continue
        i += 1
    return bytes(out)


_SAMPLE_RATES = [96000, 88200, 64000, 48000, 44100, 32000, 24000,
                 22050, 16000, 12000, 11025, 8000, 7350]


def _adts_frames(aac: bytes):
    """Walk ADTS frames -> (frame_sizes, profile, sr_index, channels)."""
    sizes, i = [], 0
    profile = sr_index = channels = None
    while i + 7 <= len(aac):
        if aac[i] == 0xFF and (aac[i + 1] & 0xF0) == 0xF0:
            flen = ((aac[i + 3] & 0x03) << 11) | (aac[i + 4] << 3) \
                | ((aac[i + 5] >> 5) & 0x07)
            if 7 <= flen and i + flen <= len(aac):
                if profile is None:
                    profile = (aac[i + 2] >> 6) & 0x03
                    sr_index = (aac[i + 2] >> 2) & 0x0F
                    channels = ((aac[i + 2] & 0x01) << 2) \
                        | ((aac[i + 3] >> 6) & 0x03)
                sizes.append(flen)
                i += flen
                continue
        i += 1
    return sizes, profile, sr_index, channels


def _box(typ: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + typ + payload


def _desc(tag: int, payload: bytes) -> bytes:
    # All descriptor payloads here are < 128 bytes -> 1-byte length.
    return bytes([tag, len(payload)]) + payload


def _wrap_m4a(aac: bytes):
    """Wrap ADTS AAC in a minimal ISO-BMFF (M4A) container, pure Python.

    Whisper's API accepts .m4a but not raw .aac/ADTS, and hosts where
    ffmpeg can't run leave no other muxer — so we build the boxes by
    hand: ftyp, mdat (the AAC frames), moov (mvhd/trak/mdia with the
    sample tables and the esds carrying AudioSpecificConfig)."""
    sizes, profile, sr_index, channels = _adts_frames(aac)
    if not sizes or profile is None or not sr_index \
            or sr_index >= len(_SAMPLE_RATES):
        return None
    sr = _SAMPLE_RATES[sr_index]
    n = len(sizes)
    duration = n * 1024  # samples; every AAC frame decodes to 1024

    ftyp = _box(b"ftyp", b"M4A " + struct.pack(">I", 0) + b"M4A mp42isom")
    mdat_header = 8
    stco_value = len(ftyp) + mdat_header

    # esds: ES_Descriptor -> DecoderConfig -> DecoderSpecificInfo (ASC)
    asc = ((profile + 1) << 11) | (sr_index << 7) | ((channels or 2) << 3)
    dec_specific = _desc(0x05, struct.pack(">H", asc))
    dec_config = _desc(0x04, bytes([0x40, 0x15]) + b"\x00\x00\x00"
                       + struct.pack(">II", 0, 0) + dec_specific)
    es_desc = _desc(0x03, struct.pack(">H", 0) + b"\x00" + dec_config
                    + _desc(0x06, b"\x02"))
    esds = _box(b"esds", b"\x00\x00\x00\x00" + es_desc)

    mp4a = (b"\x00" * 6 + struct.pack(">H", 1)          # reserved + ref idx
            + struct.pack(">HHHI", 0, 0, 0, 0)          # ver/rev/vendor
            + struct.pack(">HHHH", channels or 2, 16, 0, 0)
            + struct.pack(">I", sr << 16) + esds)
    stsd = _box(b"stsd", b"\x00\x00\x00\x00" + struct.pack(">I", 1)
                + _box(b"mp4a", mp4a))
    stts = _box(b"stts", b"\x00\x00\x00\x00"
                + struct.pack(">III", 1, n, 1024))
    stsc = _box(b"stsc", b"\x00\x00\x00\x00"
                + struct.pack(">IIII", 1, 1, n, 1))
    stsz = _box(b"stsz", b"\x00\x00\x00\x00" + struct.pack(">II", 0, n)
                + b"".join(struct.pack(">I", s) for s in sizes))
    stco = _box(b"stco", b"\x00\x00\x00\x00"
                + struct.pack(">II", 1, stco_value))
    stbl = _box(b"stbl", stsd + stts + stsc + stsz + stco)

    dref = _box(b"dref", b"\x00\x00\x00\x00" + struct.pack(">I", 1)
                + _box(b"url ", b"\x00\x00\x00\x01"))
    minf = _box(b"minf", _box(b"smhd", b"\x00\x00\x00\x00\x00\x00\x00\x00")
                + _box(b"dinf", dref) + stbl)
    hdlr = _box(b"hdlr", b"\x00\x00\x00\x00" + b"\x00\x00\x00\x00"
                + b"soun" + b"\x00" * 12 + b"SoundHandler\x00")
    mdhd = _box(b"mdhd", b"\x00\x00\x00\x00" + struct.pack(">II", 0, 0)
                + struct.pack(">II", sr, duration)
                + struct.pack(">HH", 0x55C4, 0))
    mdia = _box(b"mdia", mdhd + hdlr + minf)
    tkhd = _box(b"tkhd", b"\x00\x00\x00\x07" + struct.pack(">II", 0, 0)
                + struct.pack(">I", 1) + b"\x00\x00\x00\x00"
                + struct.pack(">I", duration) + b"\x00" * 8
                + struct.pack(">hhhh", 0, 0, 0x0100, 0)
                + struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0,
                              0x40000000)
                + struct.pack(">II", 0, 0))
    trak = _box(b"trak", tkhd + mdia)
    mvhd = _box(b"mvhd", b"\x00\x00\x00\x00" + struct.pack(">II", 0, 0)
                + struct.pack(">II", sr, duration)
                + struct.pack(">I", 0x00010000)
                + struct.pack(">H", 0x0100) + b"\x00\x00" + b"\x00" * 8
                + struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0,
                              0x40000000)
                + b"\x00" * 24 + struct.pack(">I", 2))
    moov = _box(b"moov", mvhd + trak)
    return ftyp + _box(b"mdat", aac) + moov


def _record_chunk_raw_sync(playlist_url: str, chunk_seconds: int,
                           out_path: str):
    """Download the newest HLS segments, extract AAC, wrap as M4A.
    Blocking HTTP work — call via asyncio.to_thread."""
    segs = _fetch_playlist_segments(playlist_url)
    if not segs:
        return False, "no segments in playlist"
    picked, total = [], 0.0
    for dur, url in reversed(segs):
        picked.append(url)
        total += dur or 4.0
        if total >= chunk_seconds:
            break
    picked.reverse()
    buf = bytearray()
    for url in picked:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            buf += r.read()
    aac = _extract_adts(bytes(buf))
    if len(aac) < 1000:
        return False, f"adts extract too small ({len(aac)}B of {len(buf)}B ts)"
    m4a = _wrap_m4a(aac)
    if m4a is None:
        return False, "m4a wrap failed (bad ADTS headers)"
    with open(out_path, "wb") as f:
        f.write(m4a)
    return True, f"{len(m4a) // 1024}KB m4a (raw)"


# ------------------------------------------------------------- Whisper -----

def get_audio_url(channel: str) -> str | None:
    """Resolve the channel's audio-only HLS stream URL via streamlink.

    Tries the `streamlink` CLI first, then `python -m streamlink` — on
    hosts like Falix the pip package is installed but its CLI script
    isn't on PATH, while the module entry point always works.
    """
    args = [f"https://www.twitch.tv/{channel}", "audio_only", "--stream-url"]
    last_err = ""
    for cmd in (["streamlink"], [sys.executable, "-m", "streamlink"]):
        try:
            out = subprocess.run(cmd + args, capture_output=True, text=True,
                                 timeout=30)
            url = (out.stdout or "").strip().split("\n")[0].strip()
            if url.startswith("http"):
                return url
            last_err = (out.stderr or "").strip()[:200] or f"exit {out.returncode}"
        except Exception as e:
            last_err = str(e)
    print(f"[voice] streamlink failed: {last_err}")
    return None


# Default transcription model — see module docstring. whisper-1 is the
# fallback if this one errors on the key.
DEFAULT_TRANSCRIBE_MODEL = "gpt-4o-mini-transcribe"
FALLBACK_TRANSCRIBE_MODEL = "whisper-1"


def transcribe_chunk(audio_path: str, api_key: str,
                     model: str = DEFAULT_TRANSCRIBE_MODEL) -> str:
    """Send an audio file (wav/m4a/...) to OpenAI speech-to-text, return
    transcript. Tries `model` first; on a 400/404 (model not available
    on this key) falls back to whisper-1 once instead of dropping the
    chunk."""
    ext = os.path.splitext(audio_path)[1].lstrip(".") or "wav"
    ctype = {"wav": "audio/wav", "m4a": "audio/m4a", "aac": "audio/aac",
             "mp3": "audio/mpeg"}.get(ext, "application/octet-stream")
    try:
        with open(audio_path, "rb") as f:
            audio_data = f.read()
    except Exception as e:
        print(f"[voice] transcription failed: can't read chunk: {e}")
        return ""
    for attempt_model in (model, FALLBACK_TRANSCRIBE_MODEL):
        boundary = "----voicboundary8f2c41"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; '
            f'filename="chunk.{ext}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n"
        ).encode() + audio_data + (
            f"\r\n--{boundary}\r\n"
            f'Content-Disposition: form-data; name="model"\r\n\r\n'
            f"{attempt_model}\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        try:
            req = urllib.request.Request(
                "https://api.openai.com/v1/audio/transcriptions",
                data=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                })
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read().decode())
            return (data.get("text") or "").strip()
        except urllib.error.HTTPError as e:
            # Log OpenAI's own error text — "HTTP 400" alone hides the cause.
            try:
                msg = e.read().decode(errors="replace")[:220]
            except Exception:
                msg = ""
            print(f"[voice] transcription failed ({attempt_model}): "
                  f"HTTP {e.code}: {msg}")
            if attempt_model != model or e.code not in (400, 404):
                return ""
            print("[voice] falling back to whisper-1 for this chunk")
        except Exception as e:
            print(f"[voice] transcription failed ({attempt_model}): {e}")
            return ""
    return ""


# ---------------------------------------------------------------- loop -----

async def listen_loop(channel: str, api_key: str, chunk_seconds: int,
                      is_live_fn, on_transcript_fn,
                      model: str = DEFAULT_TRANSCRIBE_MODEL):
    """Background loop: record -> transcribe -> callback. Runs forever.

    is_live_fn: sync callable returning True if the channel is live.
    on_transcript_fn: async callable taking transcript text.

    Every chunk logs its outcome (heard / no speech / failed) so the
    console always shows whether the listener is actually hearing.
    After 3 failed chunks the audio URL is re-resolved (Twitch's signed
    HLS links go stale) and the recorder choice is re-probed.
    """
    print("[voice] listener started")
    ffmpeg = await _select_ffmpeg()
    if ffmpeg:
        print(f"[voice] recording via ffmpeg: {ffmpeg}")
    else:
        print("[voice] no working ffmpeg — using raw HLS capture instead")
    # NOTE: a passing probe only proves ffmpeg runs on LOCAL input.
    # Some hosts crash it on NETWORK (HLS) input instead — so real chunk
    # failures get a vote too: 3 straight ffmpeg misses and we switch
    # to raw capture for the rest of the session.
    while True:
        try:
            if not is_live_fn():
                await asyncio.sleep(60)
                continue

            audio_url = await asyncio.to_thread(get_audio_url, channel)
            if not audio_url:
                print("[voice] couldn't resolve stream audio, retrying in 60s")
                await asyncio.sleep(60)
                continue

            print("[voice] locked onto stream audio")
            misses = 0
            while is_live_fn():
                iter_start = time.time()
                suffix = ".wav" if ffmpeg else ".m4a"
                with tempfile.NamedTemporaryFile(
                        suffix=suffix, delete=False) as tf:
                    chunk_path = tf.name
                try:
                    if ffmpeg:
                        ok, detail = await _record_chunk_ffmpeg(
                            ffmpeg, audio_url, chunk_seconds, chunk_path)
                    else:
                        ok, detail = await asyncio.to_thread(
                            _record_chunk_raw_sync, audio_url,
                            chunk_seconds, chunk_path)
                    if ok:
                        misses = 0
                        text = await asyncio.to_thread(
                            transcribe_chunk, chunk_path, api_key, model)
                        if text and len(text) > 3:
                            print(f"[voice] heard: {text[:80]}")
                            await on_transcript_fn(text)
                        else:
                            print(f"[voice] chunk ok ({detail}), no speech")
                    else:
                        misses += 1
                        print(f"[voice] chunk failed: {detail} "
                              f"(miss {misses}/3)")
                        if misses >= 3:
                            if ffmpeg:
                                print("[voice] ffmpeg keeps failing on the "
                                      "stream — switching to raw capture")
                                ffmpeg = None
                            else:
                                print("[voice] refreshing stream audio URL")
                            break  # outer loop re-resolves the URL
                finally:
                    try:
                        os.unlink(chunk_path)
                    except Exception:
                        pass

                if ffmpeg:
                    # Small breather between chunks
                    await asyncio.sleep(2)
                else:
                    # Raw capture assembles a chunk almost instantly from
                    # the newest segments — pace it to ~real time so
                    # consecutive chunks sit back-to-back on the stream
                    # instead of re-transcribing the same 15s repeatedly.
                    await asyncio.sleep(
                        max(2.0, chunk_seconds - (time.time() - iter_start)))
        except asyncio.CancelledError:
            print("[voice] listener stopped")
            break
        except Exception as e:
            print(f"[voice] loop error: {e}")
            await asyncio.sleep(30)
