---
name: music-cut
description: Cut a piece of music to a time range so it ends like it was meant to, e.g. "I need 1:23 to 2:37 of this song", "start at 1:07", "cut out 2:16-5:06", or "make it half a second longer". Snaps each cut to the music (waits for a ringing chord, stops before the next attack, keeps a count-in), crossfades sections taken out of the middle, checks the fade-out for the next phrase poking through, draws a picture of every cut, and writes a new versioned file without overwriting anything. Use for any request to trim, shorten or edit a song for a routine, class, video, presentation, ringtone or show, and for re-cuts of an earlier version.
---

# Music cut

Someone needs 1:23 to 2:37 of a song: for a dance routine, a fitness class,
a video, a walk-on. It sounds like ten seconds in an audio editor, but a cut
at the exact second lands mid-note, the fade fights a chord that was still
ringing, and sometimes the first bar of the next phrase pokes through the
fade-out. This skill does the listening part. You never place a cut by ear
or guess one: `scripts/musiccut.py` snaps the cuts and checks the fade, and
you read what it printed and look at the picture.

It needs `ffmpeg` on the PATH and Python 3.10+. Run it with
`uv run scripts/musiccut.py ...` (uv installs numpy and pillow), or
`pip install numpy pillow` once and use `python3`. Settings live in
`~/.config/music-cut/config.json` (or `$MUSIC_CUT_CONFIG`).

```bash
uv run scripts/musiccut.py info "<file>"                       # length, where the music starts and stops
uv run scripts/musiccut.py cut "<file>" --start 1:23 --end 2:37 --name "Opening" \
   --png /tmp/opening.png                                      # snap, check, write "Opening vN.mp3"
```

## First run: set up (once)

If the config file doesn't exist, ask two things, offer the defaults, and
save the answers with `musiccut.py setup`:

1. **Where should cuts go?** One folder for all of them, or next to each
   source file (the default). A synced folder (Dropbox, iCloud, Google
   Drive) makes it easy to share a cut afterwards.
2. **Which format?** `mp3` (320 kbps, plays everywhere; the default), `m4a`
   (AAC) or `wav` (for further editing).

There's a third setting most people never need: when a cut runs to the end
of the file, the silence after the last note is trimmed, so the person
playing it sets the gap before the next track. Mention that once. If they
want to keep it, use `--keep-silence`.

```bash
uv run scripts/musiccut.py setup --out-dir "~/Music/Cuts" --format mp3
uv run scripts/musiccut.py setup --show
```

## Getting the file

The script works on a file the person owns: any format ffmpeg reads (m4a,
mp3, wav, aiff, flac). Ask for its path, or look in their downloads and
music folders and confirm which one. Streaming apps don't give you a file,
so if all they have is a streaming link, they need to buy or download the
track first. Buying is theirs to do.

Timestamps from a streaming app usually match the same recording bought
from a store. A different recording, a remaster, or a "radio edit" won't.
Run `info` and compare the length with what they see in their app. If it's
off by more than a second, say so before cutting: their times may not line
up. If the whole track is shifted by a fixed amount (a longer intro, a video
that starts early), `--offset S` adds S seconds to every time they gave.

## Reading the request

| They say | Flags |
|---|---|
| "1:23-2:37" / "from 1:23 to 2:37" | `--start 1:23 --end 2:37` |
| "start at 1:07" / "1:07 to the end" | `--start 1:07` (no `--end`) |
| "cut out 2:16-5:06" (keep the rest) | `--remove 2:16-5:06` (no `--start`/`--end` means the whole track minus that gap). Repeat `--remove` for several gaps. The sides are joined with a 0.4 s equal-power crossfade; the fade-out stays at the true end. |
| "half a second longer at the end" | the same range again with `--end` 0.5 s later. The new file becomes v2 on its own. |
| "fade out at 1:58" / "a long fade" | `--end 1:58 --fade-out 4` (then read the fade check) |
| "exactly at 1:23, don't move it" | add `--exact` |
| a name for it ("the opener") | `--name "the opener"`. Without one, the source file's name is used. |

One file and one range make one cut. Several in one message mean several
cuts. If a range or name is ambiguous, ask: a wrong cut costs someone a
rehearsal.

## Every cut

1. Run `cut` with `--dry-run --png <somewhere temporary>` first. It snaps,
   runs the fade check, draws the picture and writes nothing.
2. **Read every snap line.** Each cut point says what it did:
   - `last note rings out`: the end moved later to where the chord fades
     naturally, with a short 0.25 s fade.
   - `just before the next attack`: running music, so the end goes in the
     gap before the next note, with a 2 s fade.
   - `at the softest point nearby`: legato music with no sharp attack; it
     cuts at the breath.
   - `already in a quiet gap; music enters at X`: the start is left alone,
     because a count-in may start in the silence. Tell them the entry time.
   - `top of track; start left alone`: a cut from 0:00 never moves, so quiet
     opening notes survive.
   - `stops before the next track bleeds in`: on a gapless album the next
     track's first note was in the file's last moment; it's trimmed.
   - `trailing silence trimmed (Ns)`: the cut runs to the end and the silent
     tail is gone.
   A move of more than about a second is suspicious. Look at the picture,
   and recut with `--exact` if the snap is wrong.
3. **Look at the picture** (read the PNG). One panel per cut point: grey
   dashes are the time they asked for, red is the cut, the shaded band is the
   fade or crossfade, green is the part that's kept.
4. **The fade check.** Any fade of 1 s or more prints `fade check: clean` or
   a `⚠ fade leak` line. A leak means the harmony lands, or something new
   comes in, while the fade is still less than 20 dB down, so the next
   passage is heard under the fade. A waveform can't show this: orchestral and
   ambient music often stay the same loudness across a phrase boundary. The
   check follows the chords instead. On a leak, it prints two ways out:
   - `--end <landing> --exact` finishes the fade where the phrase ends.
   - `--fade-curve squ` or `cub` keeps their end time with a fade that drops
     sooner.
   If they asked for a specific end time ("fade out at 1:58"), use the curve
   so you keep their time, and mention the landing as the other option. Say
   which one you used.
5. **Splices** (`--remove`) snap to attacks, not to the beat, and the
   crossfade shortens the bar it sits in. In music with a steady pulse the
   beat can stumble at the join even when both points look clean. When you
   deliver one, ask them to listen across the join. If it stumbles, try
   moving one side by a beat, or `--crossfade 0.2`.
6. Run it again without `--dry-run`. It writes `<name> vN.<ext>` with the
   next free N and refuses to overwrite anything. The comment tag records the
   source file and the final ranges.
7. **Deliver.** Give them the path and the snap report in a sentence or two
   ("the end waited 0.85 s for the last chord to ring out"). If the session
   can show files, show the audio so they can play it. When it's going to
   someone else on a phone, a link to the file in a synced folder works
   better than an attachment: messaging apps often re-encode audio.

## Re-cuts

A re-cut is a new version, never an edit of the old one. Use the same name and
the new range, and the next version number comes on its own. If they don't
remember the old range, read it from the earlier file's comment tag:
`ffprobe -v error -show_entries format_tags=comment -of default=nw=1:nk=1 "<file>"`.

## Going further

The script only reads the source and writes new files, so it's safe to run
from a chat bot, a task card or a scheduled job. Getting the file (buying it)
and sending the cut to people stay with a person. One way to set it up: a
request comes in, the cut gets made, and a person listens before it goes
anywhere.
