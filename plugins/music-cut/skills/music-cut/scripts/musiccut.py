#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy>=1.24", "pillow>=10"]
# ///
"""musiccut: cut a piece of music to a time range so it sounds like it was
meant to end there.

"I need 1:23 to 2:37 of this" sounds like a ten-second job, until the cut
lands mid-note, the fade fights a chord that was still ringing, or the next
phrase pokes through the fade-out. This script does the listening part:

  * Snaps each cut point to the music. An end on a ringing chord waits for it
    to ring out; an end in running music stops just before the next attack;
    a start moves to just before the nearest attack, unless it's already in a
    quiet gap (a count-in may start there) or at 0:00.
  * Takes sections out of the middle (--remove) and joins the two sides with
    a short equal-power crossfade.
  * Checks every long fade-out for leaks: the phrase lands, or something new
    comes in, while the fade is still loud enough to hear it. Loudness alone
    can't show this, so it follows the harmony. When it finds one it prints
    the end time or fade curve that fixes it.
  * Draws a picture of every cut point (--png) so a person can check by eye.
  * Writes "<name> vN.mp3" (or .m4a/.wav) with the next free N and never
    overwrites anything. The source file and final ranges go in the comment
    tag, so every cut records what it was made from.

Needs ffmpeg on PATH, Python 3.10+, numpy and pillow (`uv run` installs those).
Settings: ~/.config/music-cut/config.json, or $MUSIC_CUT_CONFIG.

  musiccut.py setup [--out-dir DIR] [--format mp3|m4a|wav] [--keep-silence] [--show]
  musiccut.py info FILE
  musiccut.py cut FILE --start 1:23 [--end 2:37] [--name "Opening"]
  musiccut.py cut FILE --remove 2:16-5:06 [--remove ...] [--start/--end] [--crossfade S]
              [--png cuts.png] [--exact] [--fade-in S] [--fade-out S] [--fade-curve tri|squ|cub]
              [--offset S] [--keep-silence] [--format F] [--out-dir DIR] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

FORMATS = {
    "mp3": ["-ar", "48000", "-ac", "2", "-c:a", "libmp3lame", "-b:a", "320k", "-id3v2_version", "3"],
    "m4a": ["-ar", "48000", "-ac", "2", "-c:a", "aac", "-b:a", "256k"],
    "wav": ["-ar", "48000", "-ac", "2", "-c:a", "pcm_s24le"],
}
DEFAULTS = {"out_dir": None, "format": "mp3", "trim_trailing_silence": True}


def config_path() -> Path:
    return Path(os.environ.get("MUSIC_CUT_CONFIG", Path.home() / ".config/music-cut/config.json")).expanduser()


def load_config() -> dict:
    p = config_path()
    cfg = dict(DEFAULTS)
    if p.is_file():
        cfg.update(json.loads(p.read_text()))
    if cfg["format"] not in FORMATS:
        raise SystemExit(f"{p}: format must be one of {', '.join(FORMATS)}")
    return cfg


def _fmt(ms: float) -> str:
    s = ms / 1000
    return f"{int(s // 60)}:{s % 60:05.2f}"


# ---------------------------------------------------------------- audio

SR, HOP, WIN = 8000, 80, 512     # 10 ms frames


def pcm(path_or_url: str | Path) -> np.ndarray:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path_or_url), "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
        capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32)


# ---------------------------------------------------------------- snapping
#
# Players show whole seconds, so "2:37" means somewhere in 2:37.00-2:37.99.
# Snapping moves each cut to what the audio is doing there. Measured on the
# first real cut: at "2:37" the last chord was still ringing at -33 dB and
# reached the room floor at 2:37.85; a 2 s fade then fought the chord's own
# decay. Rules:
#   end on a ringing note  -> extend to where it rings out; short fade
#   end in running music   -> end just before the next attack; normal fade
#   start mid-note         -> just before the nearest strong attack
#   start in a quiet gap   -> leave it (a count-in may start in the silence)
#   start at 0:00          -> leave it (a full-track pull keeps its first note)
#   end at the track's end -> trim trailing silence (unless told to keep it)

FRAME_S = HOP / SR            # 10 ms analysis frames
QUIET_DB = 8                  # within this of the track's floor counts as quiet
QUIET_MAX = -40.0             # and never louder than this (dBFS)
PRE_ROLL = 0.03               # start this far ahead of an attack
RING_FADE = 0.25              # fade over a natural ring-out
TAIL_DB = -60.0               # a track's last note is over once it stays below this
TAIL_MIN = 0.5                # trailing silence shorter than this is left alone
TOP_OF_TRACK = 0.05           # a start this early is the whole track's opening


@dataclass
class Snap:
    requested: float
    time: float
    reason: str
    natural_end: bool = False   # cut lands where the music has rung out


def _features(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per-10ms loudness (dB, 50 ms smoothed) and onset strength."""
    frames = np.lib.stride_tricks.sliding_window_view(x, WIN)[::HOP]
    # Clamp at -90 dBFS: digital silence would otherwise drag the "floor"
    # far below any real recording's noise.
    rms = np.maximum(-90.0, 20 * np.log10(np.sqrt((frames ** 2).mean(1)) + 1e-9))
    rms = np.convolve(rms, np.ones(5) / 5, "same")
    spec = np.log1p(100 * np.abs(np.fft.rfft(frames * np.hanning(WIN), axis=1)))
    flux = np.r_[0, np.maximum(0, np.diff(spec, axis=0)).sum(1)]
    return rms, flux


def _onsets(flux: np.ndarray, rms: np.ndarray, lo: int, hi: int, ctx: slice) -> list[tuple[int, float]]:
    """Attacks in frames [lo, hi): local flux peaks well above the local norm
    that also make the sound louder (>= 3 dB over 50 ms) — spectral noise in a
    fading note peaks too, but never gets louder. As (frame, strength)."""
    ref = flux[ctx]
    mu, sd = ref.mean(), ref.std() + 1e-9
    out = []
    for i in range(max(lo, 5), min(hi, len(flux) - 6)):
        z = (flux[i] - mu) / sd
        if z > 1.5 and flux[i] == flux[i - 3:i + 4].max() and rms[i + 5] - rms[i - 3] >= 3:
            out.append((i, float(z)))
    return out


def snap_cut(x: np.ndarray, t: float, kind: str, track_end: float, trim_tail: bool = True) -> Snap:
    rms, flux = _features(x)
    n = len(rms)
    f = lambda s: min(n - 1, max(0, int(round(s / FRAME_S))))  # noqa: E731
    ctx = slice(f(t - 10), f(t + 10))
    # Quiet is judged against the whole track's floor: a local floor inside
    # continuous music is just softer music (a soft dip in a celesta solo
    # read as a "gap" that way).
    quiet = rms <= min(np.percentile(rms, 1) + QUIET_DB, QUIET_MAX)
    i = f(t)

    if kind == "start":
        # Frames 0-1 read loud: the 5-frame dB smoothing averages in zeros at
        # the array's edge, so a silent intro looks like music at 0:00.
        if quiet[max(i, 2)]:
            hold = int(0.1 / FRAME_S)
            enter = next((j for j in range(i, f(t + 3)) if not quiet[j:j + hold].any()), None)
            note = f"; music enters at {_fmt(enter * FRAME_S * 1000)}" if enter else ""
            return Snap(t, t, "already in a quiet gap" + note)
        # 0:00 means the piece from its first note, and there is no earlier
        # audio to cut into. Snapping here once jumped +0.75 s to the first
        # loud attack and lost the quiet notes before it.
        if t <= TOP_OF_TRACK:
            return Snap(t, t, "top of track; start left alone")
        ons = _onsets(flux, rms, f(t - 0.15), f(t + 1.0), ctx)
        if not ons:
            return _softest(rms, t, f, "start")
        # Strongest attack, mildly preferring ones near the requested time.
        j, _ = max(ons, key=lambda o: o[1] / (1 + abs(o[0] * FRAME_S - t)))
        s = max(0.0, j * FRAME_S - PRE_ROLL)
        return Snap(t, s, "just before the attack")

    if t >= track_end - 0.05:
        # Gapless albums: the next movement's first attack can sit in the
        # last few frames (one file ended on 70 ms of the next movement).
        end, reason = track_end, "end of track"
        for j in range(f(track_end - 0.3), n):
            if not quiet[j] and quiet[max(0, j - 30):j].all():
                end, reason = j * FRAME_S - PRE_ROLL, "end of track; stops before the next track bleeds in"
                break
        # Trailing silence goes by default: whoever plays the cut sets the gap
        # before the next one, so every cut ends where its music does. One
        # file carried 6 s of it. trim_tail=False keeps it.
        k = f(end) - 1
        while k > 0 and rms[k] < TAIL_DB:
            k -= 1
        tail = end - (k + 1) * FRAME_S
        if trim_tail and tail >= TAIL_MIN:
            return Snap(t, (k + 1) * FRAME_S + 0.05,
                        f"end of track; trailing silence trimmed ({tail:.1f}s)", natural_end=True)
        return Snap(t, end, reason, natural_end=True)
    if quiet[i]:
        return Snap(t, t, "already in a quiet gap", natural_end=True)
    hold = int(0.1 / FRAME_S)                  # quiet for 100 ms = rung out
    for j in range(i, f(min(t + 1.5, track_end))):
        if quiet[j:j + hold].all():
            ons = [k for k, _ in _onsets(flux, rms, i, j, ctx)]
            if not ons:                         # a decay, not a new phrase
                return Snap(t, j * FRAME_S + 0.05, "last note rings out", natural_end=True)
            break
    ons = _onsets(flux, rms, i, f(t + 1.0), ctx)
    if ons:
        j = ons[0][0]
        return Snap(t, j * FRAME_S - PRE_ROLL, "just before the next attack")
    return _softest(rms, t, f, "end")


def _softest(rms: np.ndarray, t: float, f, kind: str) -> Snap:
    """Legato music (strings, sustained winds) has no sharp attack to cut
    before; the breath between phrases is the softest moment nearby. Only
    move if it is clearly softer than the requested point. The window covers
    the whole requested second, like the attack search: one waltz's
    "4:27" breath sat at 4:27.75, 9 dB down, and a 0.6 s window cut mid-note."""
    lo, hi = f(t - 0.15), f(t + 1.0)
    j = lo + int(np.argmin(rms[lo:hi]))
    if rms[f(t)] - rms[j] >= 3:
        return Snap(t, j * FRAME_S, "at the softest point nearby (legato, no sharp attack)")
    return Snap(t, t, "no clear attack or breath nearby; kept")


# ---------------------------------------------------------------- fade leaks
#
# The case that found this: "fade out at 1:58" became a 4 s linear fade
# from 1:54, and the listener still heard "the slightest bit of the next
# passage". The
# overall loudness was flat (-26 to -31 dB) through the whole fade, so the
# snap and the waveform showed nothing. The harmony did: the phrase landed
# (B -> E) at 1:56.3, and what followed was the next passage, only ~7 dB
# down under the fade. So: find where the harmony lands or something new
# enters inside the fade, and warn when the fade hasn't buried what follows.

LEAK_DB = -20.0          # under the fade, anything louder than this is heard
LEAK_RING = 0.5          # let a landing chord sound this long before judging
CHROMA_HOP = 0.05        # harmony analysis step
CHROMA_SIDE = 0.5        # compare the half-second before and after each step
CHROMA_MIN = 0.12        # a change smaller than this is the same harmony
MIN_LEAK_FADE = 1.0      # short de-click and ring-out fades are left alone
STABLE_S = 1.0           # no harmony change for this long = a held chord
FADE_CURVES = ("tri", "squ", "cub")   # ffmpeg afade curves we can model exactly


def fade_gain_db(curve: str, remaining: float) -> float:
    """ffmpeg afade's fade-out gain with `remaining` (0-1) of the fade left:
    tri (ffmpeg's default) is linear, squ squared, cub cubed. The faster
    curves drop most in the first half, which buries a late entry."""
    r = min(1.0, max(0.0, remaining))
    g = {"tri": r, "squ": r ** 2, "cub": r ** 3}[curve]
    return 20 * np.log10(g) if g > 0 else -120.0


def harmonic_changes(x: np.ndarray, t0: float, t1: float) -> list[tuple[float, float]]:
    """Points in [t0, t1) where the harmony changes, as (time, strength 0-1).
    Pitch-class energy (100-2000 Hz) over the half-second before each step is
    compared with the half-second after; local peaks of that difference are
    chord changes and phrase entries. Loudness alone misses them in legato
    orchestral music."""
    n = 2048 if SR <= 11025 else 4096      # ~0.2 s: fine enough for low notes
    hop = int(CHROMA_HOP * SR)
    lo = max(0, int((t0 - CHROMA_SIDE) * SR) - n // 2)
    hi = min(len(x), int((t1 + CHROMA_SIDE) * SR) + n)
    f = np.fft.rfftfreq(n, 1 / SR)
    band = (f > 100) & (f < 2000)
    pc = np.round(69 + 12 * np.log2(f[band] / 440)).astype(int) % 12
    times, frames = [], []
    for i in range(lo, hi - n, hop):
        spec = np.abs(np.fft.rfft(x[i:i + n] * np.hanning(n)))[band] ** 2
        c = np.bincount(pc, weights=spec, minlength=12)
        frames.append(c / (c.sum() + 1e-12))
        times.append((i + n / 2) / SR)
    if not frames:
        return []
    C, k = np.array(frames), int(round(CHROMA_SIDE / CHROMA_HOP))
    nov = np.zeros(len(C))
    for j in range(k, len(C) - k):
        a, b = C[j - k:j].mean(0), C[j:j + k].mean(0)
        nov[j] = 1 - a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12)
    out = []
    for j in range(len(C)):
        if (t0 <= times[j] < t1 and nov[j] >= CHROMA_MIN
                and nov[j] == nov[max(0, j - 6):j + 7].max()):
            out.append((float(times[j]), float(nov[j])))
    return out


def fade_leak(x: np.ndarray, end: float, fade: float, curve: str = "tri") -> dict | None:
    """Is the next passage audible under the fade-out [end - fade, end]?

    Harmony changes come in two kinds that matter at an ending. A *landing*
    follows chord motion and is followed by a held chord (B -> E at
    1:56.3): the phrase is over, so after LEAK_RING the old phrase is done
    and anything still loud is the next passage creeping in. An *entry*
    follows a held chord (E -> G at 1:58.0): something new starts right
    there. Changes in the middle of chord motion are the phrase itself
    moving, which the fade is meant to carry, so they don't count.

    Only the last landing or entry inside the fade is judged, because it is
    what the ending sounds like. Returns {time, kind, strength, gain_db,
    fix_end} when its music is above LEAK_DB under the fade, else None."""
    if fade < MIN_LEAK_FADE:
        return None
    start = end - fade
    ch = harmonic_changes(x, start - 3 * STABLE_S, end + 3 * STABLE_S)
    last = None
    for k, (t, s) in enumerate(ch):
        if not start <= t < end:
            continue
        prev_gap = t - ch[k - 1][0] if k else np.inf
        next_gap = ch[k + 1][0] - t if k + 1 < len(ch) else np.inf
        if prev_gap >= STABLE_S:
            last = {"time": t, "kind": "entry", "strength": s, "judge_at": t, "fix_end": t}
        elif next_gap >= STABLE_S:
            last = {"time": t, "kind": "landing", "strength": s,
                    "judge_at": t + LEAK_RING, "fix_end": t + LEAK_RING}
    if last is None or last["judge_at"] >= end:
        return None
    last["gain_db"] = fade_gain_db(curve, (end - last["judge_at"]) / fade)
    return last if last["gain_db"] > LEAK_DB else None


def leak_advice(leak: dict | None, end: float, fade: float, curve: str) -> list[str]:
    """What cut prints about a leak: where, how loud, and two ways out."""
    if not leak:
        return []
    t, g = leak["time"], leak["gain_db"]
    what = (f"the phrase lands at {_fmt(t * 1000)}, but {LEAK_RING:g}s later the fade is only {-g:.0f} dB "
            f"down, so whatever comes next is heard" if leak["kind"] == "landing" else
            f"something new starts at {_fmt(t * 1000)} and the fade is only {-g:.0f} dB down there")
    lines = [f"  ⚠ fade leak (harmony change {leak['strength']:.2f}): {what}.",
             f"    fix: finish the fade there, e.g. --end {_fmt(leak['fix_end'] * 1000)} --exact "
             f"--fade-out {min(fade, 4.0):g}, then listen"]
    for c in FADE_CURVES[FADE_CURVES.index(curve) + 1:]:
        gc = fade_gain_db(c, (end - leak["judge_at"]) / fade)
        if gc <= LEAK_DB:
            lines.append(f"    or keep the end and use --fade-curve {c} ({-gc:.0f} dB down there)")
            break
    return lines


def render_cut_png(x: np.ndarray, panels: list[tuple[str, Snap, bool, float]],
                   out: Path, title: str, span: float = 3.0) -> None:
    """One panel per cut point, as (label, snap, keep_right, fade): loudness
    over ±span s, the requested time (grey), the snapped cut (red), the fade
    or crossfade (shaded) and the kept side. A plain cut is two panels (start,
    end); each --remove adds the two sides of its splice."""
    from PIL import Image, ImageDraw
    rms, _ = _features(x)
    W, H, pad = 900, 200, 24
    img = Image.new("RGB", (W, len(panels) * (H + pad) + pad), "white")
    d = ImageDraw.Draw(img)
    d.text((pad, 4), title, fill="black")
    lo_db = np.percentile(rms, 2)
    hi_db = rms.max()
    for k, (label, sn, keep_right, fade) in enumerate(panels):
        top = pad + k * (H + pad)
        t0 = sn.requested - span
        # Clamped: a snap can land outside the window (trailing silence
        # longer than `span` moves the end that far back).
        xpix = lambda t: min(W, max(0, int((t - t0) / (2 * span) * W)))  # noqa: E731
        kx = xpix(sn.time)
        keep = (kx, top, W, top + H) if keep_right else (0, top, kx, top + H)
        d.rectangle(keep, fill=(235, 245, 235))
        fx = (kx, xpix(sn.time + fade)) if keep_right else (xpix(sn.time - fade), kx)
        d.rectangle((min(fx), top, max(fx), top + H), fill=(255, 228, 196))
        for px in range(W):
            i = int((t0 + px / W * 2 * span) / FRAME_S)
            if 0 <= i < len(rms):
                h = (rms[i] - lo_db) / (hi_db - lo_db + 1e-9) * H
                d.line((px, top + H, px, top + H - max(0, h)), fill=(60, 90, 140))
        rx = xpix(sn.requested)
        for y in range(top, top + H, 8):
            d.line((rx, y, rx, y + 4), fill=(120, 120, 120))
        d.line((kx, top, kx, top + H), fill=(210, 30, 30), width=2)
        d.text((6, top + 4), f"{label} {_fmt(sn.requested * 1000)} -> {_fmt(sn.time * 1000)} - {sn.reason}",
               fill="black")
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)


# ---------------------------------------------------------------- cutting

def parse_time(s: str) -> float:
    """'1:23' / '01:23.5' / '83' -> seconds."""
    parts = s.strip().split(":")
    if not 1 <= len(parts) <= 3 or not all(re.fullmatch(r"\d+(\.\d+)?", p) for p in parts):
        raise ValueError(f"bad time: {s!r}")
    secs = 0.0
    for p in parts:
        secs = secs * 60 + float(p)
    return secs



def next_version_path(folder: Path, name: str, ext: str = "mp3") -> Path:
    """'<name> vN.<ext>' with the next free N. Earlier versions of any
    format count, so a WAV after two MP3s is v3."""
    pat = re.compile(rf"^{re.escape(name)} v(\d+)\.(?:{'|'.join(FORMATS)})$", re.I)
    taken = [int(m.group(1)) for p in folder.glob("*") if (m := pat.match(p.name))]
    return folder / f"{name} v{max(taken, default=0) + 1}.{ext}"


def _curve(curve: str) -> str:
    return "" if curve == "tri" else f":curve={curve}"


def _encode(fmt: str) -> list[str]:
    return FORMATS[fmt]


def ffmpeg_cut_cmd(src: Path, dst: Path, start: float, end: float, fade_in: float,
                   fade_out: float, tags: dict, curve: str = "tri", fmt: str = "mp3") -> list[str]:
    length = end - start
    filters = []
    if fade_in > 0:
        filters.append(f"afade=t=in:st=0:d={fade_in}")
    if fade_out > 0:
        filters.append(f"afade=t=out:st={max(0.0, length - fade_out):.3f}:d={fade_out}{_curve(curve)}")
    cmd = ["ffmpeg", "-v", "error", "-n", "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(src),
           "-map", "0:a:0", *_encode(fmt), "-map_metadata", "-1"]
    if filters:
        cmd += ["-af", ",".join(filters)]
    for k, v in tags.items():
        cmd += ["-metadata", f"{k}={v}"]
    return cmd + [str(dst)]


def parse_range(s: str) -> tuple[float, float]:
    """'2:16-5:06' -> (136.0, 306.0)."""
    a, sep, b = s.partition("-")
    if not sep:
        raise ValueError(f"bad range: {s!r} (want m:ss-m:ss)")
    lo, hi = parse_time(a), parse_time(b)
    if not lo < hi:
        raise ValueError(f"bad range: {s!r} (end must come after start)")
    return lo, hi


def kept_ranges(start: float, end: float, removes: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """The ranges left after taking each removed gap out of start..end, in
    order. Gaps must sit strictly inside the range and not touch each other:
    a gap at the very start or end is a --start/--end, not a splice."""
    out, at = [], start
    for lo, hi in sorted(removes):
        if not at < lo < hi < end:
            raise ValueError(f"removed range {_fmt(lo * 1000)}-{_fmt(hi * 1000)} must sit inside "
                             f"{_fmt(at * 1000)}-{_fmt(end * 1000)} and not overlap another")
        out.append((at, lo))
        at = hi
    return out + [(at, end)]


XFADE = 0.4      # splice crossfade: equal-power, short enough to keep the beat


def ffmpeg_splice_cmd(src: Path, dst: Path, keep: list[tuple[float, float]], xfade: float,
                      fade_in: float, fade_out: float, tags: dict, curve: str = "tri",
                      fmt: str = "mp3") -> list[str]:
    """Like ffmpeg_cut_cmd, for several kept ranges joined end to end with an
    equal-power (qsin) crossfade at each splice. Each crossfade overlaps the
    two sides, so the result is sum(ranges) - xfade per splice long."""
    graph = [f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[k{i}]"
             for i, (a, b) in enumerate(keep)]
    prev = "k0"
    for i in range(1, len(keep)):
        graph.append(f"[{prev}][k{i}]acrossfade=d={xfade}:c1=qsin:c2=qsin[x{i}]")
        prev = f"x{i}"
    length = sum(b - a for a, b in keep) - xfade * (len(keep) - 1)
    tail = []
    if fade_in > 0:
        tail.append(f"afade=t=in:st=0:d={fade_in}")
    if fade_out > 0:
        tail.append(f"afade=t=out:st={max(0.0, length - fade_out):.3f}:d={fade_out}{_curve(curve)}")
    graph.append(f"[{prev}]" + (",".join(tail) or "anull") + "[out]")
    cmd = ["ffmpeg", "-v", "error", "-n", "-i", str(src), "-filter_complex", ";".join(graph),
           "-map", "[out]", *_encode(fmt), "-map_metadata", "-1"]
    for k, v in tags.items():
        cmd += ["-metadata", f"{k}={v}"]
    return cmd + [str(dst)]




# ---------------------------------------------------------------- commands

def cmd_setup(args) -> int:
    p = config_path()
    cfg = load_config()
    if args.show:
        print(f"{p}{'' if p.is_file() else ' (not created yet: defaults)'}\n{json.dumps(cfg, indent=2)}")
        return 0
    if args.out_dir:
        cfg["out_dir"] = str(Path(args.out_dir).expanduser())
    if args.format:
        cfg["format"] = args.format
    if args.keep_silence is not None:
        cfg["trim_trailing_silence"] = not args.keep_silence
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"Wrote {p}\n{json.dumps(cfg, indent=2)}")
    return 0


def describe(x: np.ndarray) -> dict:
    """Where the music starts and stops, and how loud it is (dBFS)."""
    rms, _ = _features(x)
    quiet = rms <= min(np.percentile(rms, 1) + QUIET_DB, QUIET_MAX)
    # The 5-frame dB smoothing averages in zeros at both edges of the array,
    # so the first and last two frames read loud even in silence.
    loud = np.flatnonzero(~quiet[2:]) + 2
    above = np.flatnonzero(rms[:-2] > TAIL_DB)
    return {
        "length": len(x) / SR,
        "music_starts": float(loud[0] * FRAME_S) if len(loud) else None,
        "music_ends": float((above[-1] + 1) * FRAME_S) if len(above) else None,
        "peak_db": float(20 * np.log10(np.abs(x).max() + 1e-9)),
        "floor_db": float(np.percentile(rms, 1)),
    }


def cmd_info(args) -> int:
    src = Path(args.audio).expanduser()
    if not src.is_file():
        raise SystemExit(f"no such file: {src}")
    d = describe(pcm(src))
    tail = d["length"] - (d["music_ends"] or d["length"])
    print(f"{src.name}\n  length {_fmt(d['length'] * 1000)}  peak {d['peak_db']:.1f} dBFS  floor {d['floor_db']:.0f} dB")
    if d["music_starts"] is not None:
        print(f"  music starts {_fmt(d['music_starts'] * 1000)}, last sound {_fmt(d['music_ends'] * 1000)}"
              + (f" ({tail:.1f}s of silence after it)" if tail >= TAIL_MIN else ""))
    return 0


def cmd_cut(args) -> int:
    cfg = load_config()
    src = Path(args.audio).expanduser()
    if not src.is_file():
        raise SystemExit(f"no such file: {src}")
    if args.start is None and not args.remove:
        raise SystemExit("cut needs --start (or --remove to take a section out of the whole track)")
    fmt = args.format or cfg["format"]
    trim_tail = cfg["trim_trailing_silence"] and not args.keep_silence
    audio = pcm(src)
    track_end = len(audio) / SR
    # --offset: the times came from another copy (a streaming app, a video)
    # whose timeline runs ahead of or behind this file by a fixed amount.
    off = args.offset
    try:
        start = (parse_time(args.start) if args.start else 0.0) + (off if args.start else 0.0)
        end = parse_time(args.end) + off if args.end else track_end
        removes = [(a + off, b + off) for a, b in (parse_range(r) for r in args.remove or [])]
        if not 0 <= start < end <= track_end + 0.5:
            raise ValueError(f"range {_fmt(start * 1000)}-{_fmt(end * 1000)} is outside the file "
                             f"({_fmt(track_end * 1000)} long)")
        end = min(end, track_end)
        ranges = kept_ranges(start, end, removes)
    except ValueError as e:
        raise SystemExit(str(e))

    # Every kept range is cut like a plain cut: its start snaps as a start,
    # its end as an end. A splice is a kept range's end (the head runs out
    # just before the next attack) meeting the next one's start.
    snaps = []
    for a, b in ranges:
        if args.exact:
            pair = (Snap(a, a, "exact"), Snap(b, b, "exact", natural_end=b == end and not args.end))
        else:
            pair = (snap_cut(audio, a, "start", track_end, trim_tail),
                    snap_cut(audio, b, "end", track_end, trim_tail))
        snaps.append(pair)
    last = len(snaps) - 1
    for k, (s, e) in enumerate(snaps):
        for label, sn in ((f"splice {k} in" if k else "start", s), ("end" if k == last else f"splice {k + 1} out", e)):
            moved = f"{sn.time - sn.requested:+.2f}s" if abs(sn.time - sn.requested) >= 0.01 else "unchanged"
            print(f"  {label}: {_fmt(sn.requested * 1000)} → {_fmt(sn.time * 1000)} ({moved}) — {sn.reason}")
    keep = [(s.time, e.time) for s, e in snaps]
    xfade = args.crossfade
    for a, b in keep:
        if b - a <= 2 * xfade:
            raise SystemExit(f"kept piece {_fmt(a * 1000)}-{_fmt(b * 1000)} is too short to crossfade "
                             f"({xfade}s each side); recheck the --remove ranges")
    e_snap = snaps[-1][1]
    if args.fade_out is not None:
        fade_out = args.fade_out
    elif e_snap.natural_end:
        fade_out = 0.05 if e_snap.time >= track_end - 0.05 else RING_FADE
    else:
        fade_out = 2.0
    # The fade runs over the last kept piece's tail; in a splice that piece
    # starts later in the file, but the fade's place in the source is the same.
    leak = fade_leak(audio, e_snap.time, fade_out, args.fade_curve)
    for line in leak_advice(leak, e_snap.time, fade_out, args.fade_curve):
        print(line)
    if leak is None and fade_out >= MIN_LEAK_FADE:
        print(f"  fade check: clean (nothing new audible under the {fade_out:g}s {args.fade_curve} fade)")
    name = args.name or src.stem
    if args.png:
        panels = [("start", snaps[0][0], True, args.fade_in)]
        for k in range(1, len(snaps)):
            panels += [(f"splice {k} out", snaps[k - 1][1], False, xfade),
                       (f"splice {k} in", snaps[k][0], True, xfade)]
        panels.append(("end", e_snap, False, fade_out))
        render_cut_png(audio, panels, Path(args.png).expanduser(), name)
        print(f"  waveform: {args.png}")

    out_dir = Path(args.out_dir or cfg["out_dir"] or src.parent).expanduser()
    dst = next_version_path(out_dir, name, fmt)
    stamp = " + ".join(f"{_fmt(a * 1000)}-{_fmt(b * 1000)}" for a, b in keep)
    comment = f"src={src.name} {stamp}"
    if len(keep) > 1:
        gaps = ", ".join(f"{_fmt(keep[k][1] * 1000)}-{_fmt(keep[k + 1][0] * 1000)}" for k in range(len(keep) - 1))
        comment += f" removed {gaps} xfade {xfade}s qsin"
    tags = {"title": f"{name} ({dst.stem.rsplit(' ', 1)[-1]})", "comment": comment}
    curve = args.fade_curve
    if len(keep) == 1:
        cmd = ffmpeg_cut_cmd(src, dst, *keep[0], args.fade_in, fade_out, tags, curve, fmt)
    else:
        cmd = ffmpeg_splice_cmd(src, dst, keep, xfade, args.fade_in, fade_out, tags, curve, fmt)
    length = sum(b - a for a, b in keep) - xfade * (len(keep) - 1)
    if args.dry_run:
        print(f"Would write {dst}  ({_fmt(length * 1000)} long)\n ", " ".join(cmd))
        return 0
    out_dir.mkdir(parents=True, exist_ok=True)
    if dst.exists():  # ffmpeg -n declines to overwrite but still exits 0
        raise SystemExit(f"{dst} already exists")
    subprocess.run(cmd, check=True)
    joins = f", {len(keep) - 1} splice(s) crossfaded {xfade}s" if len(keep) > 1 else ""
    print(f"Wrote {dst}  ({stamp}, {_fmt(length * 1000)} long, fade in {args.fade_in}s / out "
          f"{fade_out}s{'' if curve == 'tri' else ' ' + curve}{joins})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="musiccut.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", help="where cuts go, which format, trailing silence")
    s.add_argument("--out-dir", help="folder for cuts (default: next to the source file)")
    s.add_argument("--format", choices=FORMATS)
    s.add_argument("--keep-silence", action=argparse.BooleanOptionalAction, default=None,
                   help="keep silence after the last note when a cut runs to the end of the file")
    s.add_argument("--show", action="store_true", help="print the current settings")

    i = sub.add_parser("info", help="length, where the music starts and stops, loudness")
    i.add_argument("audio")

    c = sub.add_parser("cut", help="cut a file to a range, snapping to the music")
    c.add_argument("audio", help="any file ffmpeg can read")
    c.add_argument("--start", help="m:ss; with --remove, default 0:00")
    c.add_argument("--end", help="m:ss; omit for 'to the end'")
    c.add_argument("--remove", action="append", metavar="M:SS-M:SS",
                   help="take this section out and crossfade the join; repeatable")
    c.add_argument("--crossfade", type=float, default=XFADE, help=f"splice crossfade, seconds (default {XFADE})")
    c.add_argument("--name", help="output name (default: the source file's name); written as '<name> vN.<ext>'")
    c.add_argument("--fade-in", type=float, default=0.05, help="seconds (default 0.05: de-click only)")
    c.add_argument("--fade-out", type=float,
                   help="seconds (default: 2.0 in running music, 0.25 over a natural ring-out)")
    c.add_argument("--fade-curve", choices=FADE_CURVES, default="tri",
                   help="tri = linear (default); squ and cub drop sooner and bury a late entry")
    c.add_argument("--exact", action="store_true", help="cut at the given times; skip snapping")
    c.add_argument("--offset", type=float, default=0.0,
                   help="add this many seconds to every given time (times from a copy that runs ahead/behind)")
    c.add_argument("--keep-silence", action="store_true", help="don't trim silence after the last note")
    c.add_argument("--format", choices=FORMATS, help="mp3 (320k), m4a (AAC 256k) or wav (24-bit)")
    c.add_argument("--png", help="write a picture of every cut point here")
    c.add_argument("--out-dir", help="default: the setup folder, else next to the source")
    c.add_argument("--dry-run", action="store_true", help="snap, check and print; write nothing")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return {"setup": cmd_setup, "info": cmd_info, "cut": cmd_cut}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
