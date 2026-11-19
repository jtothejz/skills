# music-cut

A Claude skill for cutting songs down to the part you need.

Tell Claude "I need 1:23 to 2:37 of this." It finds the file, moves each
cut to where the music allows it, checks that the fade-out doesn't let the
next phrase through, shows you a picture of every cut, and writes
`Opening v1.mp3` next to your other cuts. Ask for "half a second longer" and
you get v2. v1 stays.

```
  start: 1:23.00 → 1:23.00 (unchanged) — already in a quiet gap; music enters at 1:24.79
  end: 2:37.00 → 2:37.81 (+0.81s) — last note rings out
  waveform: cuts.png
Wrote ~/Music/Cuts/Opening v1.mp3  (1:23.00-2:37.81, 1:14.81 long, fade in 0.05s / out 0.25s)
```

(A real run, on the Grandfather's Dance from Tchaikovsky's *Nutcracker*.)

## What it handles

- **Ringing endings.** A cut at "2:37" often lands while the last chord is
  still sounding. It waits for the chord to ring out and uses a short fade
  instead of fighting the decay.
- **Running music.** It ends in the gap just before the next note, not on
  top of it.
- **Starts.** It moves to just before the nearest attack. A start that's
  already in silence stays put, because the count-in may be there. 0:00
  never moves.
- **Taking a section out** ("cut 2:16 to 5:06"). It joins the two sides
  with an equal-power crossfade and asks you to listen to the join.
- **The next phrase under the fade.** This is the one you can't see in a
  waveform. Orchestral music often stays the same loudness across a phrase
  boundary, so a 4-second fade can still let the first bar of the next
  section through. The check follows the harmony, finds where the phrase
  lands, and tells you how loud the fade still is there. If it's too loud,
  it gives you two fixes: end the fade on the landing, or keep your end time
  with a steeper fade curve.
- **Trailing silence** when a cut runs to the end of the file, so whoever
  plays it sets the gap.
- **Versions.** `<name> vN` with the next free number. It never overwrites
  anything, and the comment tag records the source file and exact ranges.

## First run

The first time you use it, Claude asks where cuts should go (one folder, or
next to each source) and which format you want (mp3, m4a or wav). The answers
go in `~/.config/music-cut/config.json`. See
[`config-example.json`](skills/music-cut/references/config-example.json).

## Install

**Claude Code**

```
/plugin marketplace add jtothejz/skills
/plugin install music-cut@jtothez-skills
```

**Claude apps (claude.ai, Desktop):** download `music-cut.skill` from the
[releases](https://github.com/jtothejz/skills/releases)
and upload it under Settings → Capabilities → Skills.

You need [ffmpeg](https://ffmpeg.org) on your PATH (`brew install ffmpeg`),
Python 3.10+, and [`uv`](https://docs.astral.sh/uv/) or
`pip install numpy pillow`.

## Using the script directly

```bash
uv run skills/music-cut/scripts/musiccut.py info song.m4a
uv run skills/music-cut/scripts/musiccut.py cut song.m4a --start 1:23 --end 2:37 --name Opening --png cuts.png
uv run skills/music-cut/scripts/musiccut.py cut song.m4a --remove 2:16-5:06 --name "Short version"
uv run skills/music-cut/scripts/musiccut.py cut song.m4a --start 0:00 --end 1:58 --fade-out 4 --fade-curve squ
```

`--dry-run` snaps, checks and draws without writing. `--exact` turns snapping
off. Tests: `uv run --with numpy --with pillow python skills/music-cut/scripts/test_musiccut.py`
(offline, synthetic audio, needs ffmpeg).

## You bring the file

It cuts files you have: m4a, mp3, wav, aiff, flac. Streaming apps don't give
you one, so buy or download the track first. If your timestamps came from a
streaming app, check the lengths match. A different recording or a remaster
won't line up, and `--offset` fixes a copy that's only shifted.

## Status

Built for a dance studio's recital and class music, mostly orchestral and
musical-theatre tracks, and in use there. The fade check is tuned on that
music. On dense pop and electronic tracks it fires less often, because the
harmony rarely holds still long enough to call a landing. Listen to every
cut before it goes out.

MIT license (see the repo root).
