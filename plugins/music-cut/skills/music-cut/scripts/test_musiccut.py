#!/usr/bin/env python3
"""Offline checks for musiccut.py, on synthetic audio: snapping, splices,
the fade-leak check, version numbers, formats and the end-to-end cut. Needs
ffmpeg, numpy and pillow. Run: `uv run --with numpy --with pillow python
test_musiccut.py` (or plain python3 if those are installed)."""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import musiccut as me  # noqa: E402

def music(seconds, seed):
    """Stand-in for a recording: decaying tone bursts at random times/pitches."""
    rng = np.random.default_rng(seed)
    x = np.zeros(int(seconds * me.SR), np.float32)
    t = np.arange(int(0.4 * me.SR)) / me.SR
    for start in np.sort(rng.uniform(0, seconds - 0.5, int(seconds * 3))):
        i = int(start * me.SR)
        burst = np.sin(2 * np.pi * rng.uniform(150, 1500) * t) * np.exp(-t * 8)
        x[i:i + len(t)] += burst.astype(np.float32)
    return x + rng.normal(0, 0.01, len(x)).astype(np.float32)



def tone(seconds, f=440.0, decay=0.0, amp=0.3):
    t = np.arange(int(seconds * me.SR)) / me.SR
    return (amp * np.sin(2 * np.pi * f * t) * np.exp(-t * decay)).astype(np.float32)


def silence(seconds):
    return np.random.default_rng(0).normal(0, 1e-4, int(seconds * me.SR)).astype(np.float32)


def test_snap_end_waits_for_ring_out():
    # Steady chord to 10 s, then it decays (~0.6 s to the floor), then silence.
    x = np.r_[tone(10), tone(3, decay=8), silence(10)] + silence(23)
    snap = me.snap_cut(x, 10.1, "end", len(x) / me.SR)
    assert snap.natural_end and 10.4 < snap.time < 11.4, snap


def test_snap_start_in_gap_is_kept_and_reports_entry():
    x = np.r_[music(10, seed=8), silence(2), music(10, seed=9)]
    snap = me.snap_cut(x, 10.5, "start", len(x) / me.SR)
    assert snap.time == 10.5 and re.search(r"enters at 0:1(1\.9|2\.0)", snap.reason), snap


def test_snap_start_mid_music_moves_before_attack():
    x = np.r_[tone(10, f=220, amp=0.1), tone(10, f=660, decay=0.5, amp=0.6)]
    snap = me.snap_cut(x, 9.6, "start", len(x) / me.SR)
    assert 9.9 < snap.time < 10.0, snap          # 30 ms ahead of the 10.0 s attack


def test_snap_start_at_top_of_track_stays_put():
    # Quiet opening notes, then a loud attack 0.75 s in. Marzipan's snap jumped
    # to an attack like this one and lost everything before it.
    x = np.r_[tone(0.75, f=220, amp=0.05), tone(10, f=660, decay=0.5, amp=0.6)]
    snap = me.snap_cut(x, 0.0, "start", len(x) / me.SR)
    assert snap.time == 0.0 and "top of track" in snap.reason, snap


def test_snap_start_at_top_of_track_still_reports_a_silent_intro():
    x = np.r_[silence(2), music(20, seed=10)]
    snap = me.snap_cut(x, 0.0, "start", len(x) / me.SR)
    assert snap.time == 0.0 and "enters at 0:0" in snap.reason, snap


def test_softest_start_searches_the_whole_requested_second():
    # Legato loudness at -33 dB with its breath (-42 dB) 0.75 s after the
    # requested time: Waltz of the Flowers at "4:27" was cut mid-note when the
    # search stopped at +0.6 s.
    rms = np.full(2000, -33.0)
    rms[1070:1080] = -42.0
    f = lambda s: int(round(s / me.FRAME_S))  # noqa: E731
    snap = me._softest(rms, 10.0, f, "start")
    assert 10.65 < snap.time < 10.85 and "softest" in snap.reason, snap


def test_snap_end_in_running_music_stops_before_next_attack():
    x = np.r_[tone(10, f=220, amp=0.1), tone(10, f=660, decay=0.5, amp=0.6)]
    snap = me.snap_cut(x, 9.6, "end", len(x) / me.SR)
    assert not snap.natural_end and 9.9 < snap.time < 10.0, snap


def test_snap_end_of_track_drops_next_track_bleed():
    # A ring-out to silence, then 70 ms of the next movement at the very end.
    x = np.r_[tone(5, decay=3), silence(2), tone(0.07)] + silence(7.07)
    end = len(x) / me.SR
    snap = me.snap_cut(x, end, "end", end)
    # The bleed goes, and so does the 2 s of silence before it.
    assert snap.natural_end and 1.0 < snap.time < 2.5 and "trimmed" in snap.reason, (snap, end)


def test_snap_end_of_track_trims_trailing_silence():
    # Music, a final chord ringing out, then 6 s of near-silence (Snowflakes).
    x = np.r_[music(20, seed=11), tone(2, decay=2), silence(6)]
    end = len(x) / me.SR
    snap = me.snap_cut(x, end, "end", end)
    assert snap.natural_end and 21.0 < snap.time < 22.5 and "trimmed" in snap.reason, snap


def test_snap_end_of_track_keeps_a_short_tail():
    x = np.r_[music(20, seed=12), silence(0.2)]
    end = len(x) / me.SR
    assert me.snap_cut(x, end, "end", end).time == end


def test_snap_end_of_track():
    x = music(30, seed=10)
    snap = me.snap_cut(x, 30.0, "end", 30.0)
    assert snap.time == 30.0 and snap.natural_end


def test_parse_time():
    assert me.parse_time("1:23") == 83
    assert me.parse_time("01:07.5") == 67.5
    assert me.parse_time("95") == 95
    for bad in ("", "1:", "a:10", "1:2:3:4"):
        try:
            me.parse_time(bad)
        except ValueError:
            continue
        raise AssertionError(bad)



def test_next_version():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        assert me.next_version_path(d, "9 to 5").name == "9 to 5 v1.mp3"
        for n in (1, 2, 9):
            (d / f"9 to 5 v{n}.mp3").touch()
        (d / "9 to 5 extended v40.mp3").touch()
        assert me.next_version_path(d, "9 to 5").name == "9 to 5 v10.mp3"


def test_cut_length_and_format():
    with tempfile.TemporaryDirectory() as d:
        src, dst = Path(d) / "src.m4a", Path(d) / "out v1.mp3"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=20:sample_rate=44100",
                        "-c:a", "aac", str(src)], check=True)
        cmd = me.ffmpeg_cut_cmd(src, dst, 3.0, 13.0, 0.05, 2.0, {"title": "t", "comment": "c"})
        subprocess.run(cmd, check=True)
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                              "stream=sample_rate,channels:format=duration,bit_rate",
                              "-of", "default=nw=1", str(dst)], capture_output=True, text=True).stdout
        info = dict(line.split("=") for line in out.split())
        assert abs(float(info["duration"]) - 10.0) < 0.1, info
        assert info["sample_rate"] == "48000" and info["channels"] == "2", info
        # -n: a second run must leave the existing file alone.
        before = dst.read_bytes()
        subprocess.run(cmd + [], capture_output=True)
        assert dst.read_bytes() == before


def test_parse_range_and_kept_ranges():
    assert me.parse_range("2:16-5:06") == (136, 306)
    for bad in ("2:16", "5:06-2:16", "2:16-x"):
        try:
            me.parse_range(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    assert me.kept_ranges(0, 381, [(136, 306)]) == [(0, 136), (306, 381)]
    # Several gaps, given out of order, come back in track order.
    assert me.kept_ranges(10, 100, [(60, 70), (20, 30)]) == [(10, 20), (30, 60), (70, 100)]
    # A gap at the edge, outside the range, or overlapping another is refused.
    for gaps in ([(0, 30)], [(50, 120)], [(20, 40), (35, 50)]):
        try:
            me.kept_ranges(0, 100, gaps)
        except ValueError:
            continue
        raise AssertionError(gaps)


def test_splice_length_and_crossfade():
    # Head: 440 Hz for 0-10 s, removed: 880 Hz for 10-20 s, tail: 440 Hz again.
    with tempfile.TemporaryDirectory() as d:
        src, dst = Path(d) / "src.wav", Path(d) / "out v1.mp3"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "aevalsrc='0.3*sin(2*PI*if(between(t,10,20),880,440)*t)':d=30:s=44100",
                        str(src)], check=True)
        keep = [(1.0, 10.0), (20.0, 29.0)]
        cmd = me.ffmpeg_splice_cmd(src, dst, keep, 0.4, 0.05, 2.0, {"comment": "c"})
        subprocess.run(cmd, check=True)
        x = me.pcm(dst)
        assert abs(len(x) / me.SR - (18.0 - 0.4)) < 0.1, len(x) / me.SR
        # No 880 Hz from the removed section anywhere in the result.
        spec = np.abs(np.fft.rfft(x))
        hz = np.fft.rfftfreq(len(x), 1 / me.SR)
        assert spec[np.abs(hz - 880) < 5].max() < 0.01 * spec[np.abs(hz - 440) < 5].max()
        # Equal-power: no dip at the splice (8.6-9.0 s in the output).
        rms = lambda a, b: np.sqrt((x[int(a * me.SR):int(b * me.SR)] ** 2).mean())  # noqa: E731
        assert rms(8.7, 8.9) > 0.8 * rms(5, 6), (rms(8.7, 8.9), rms(5, 6))
        info = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=sample_rate,channels",
                               "-of", "default=nw=1", str(dst)], capture_output=True, text=True).stdout
        assert "sample_rate=48000" in info and "channels=2" in info, info


def test_render_png_draws_every_splice_panel():
    x = music(30, seed=11)
    snaps = [me.Snap(t, t, "test") for t in (2, 10, 20, 28)]
    panels = [("start", snaps[0], True, 0.05), ("splice 1 out", snaps[1], False, 0.4),
              ("splice 1 in", snaps[2], True, 0.4), ("end", snaps[3], False, 2.0)]
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "cuts.png"
        me.render_cut_png(x, panels, out, "t")
        from PIL import Image
        assert Image.open(out).height == 4 * 224 + 24


# ---------------------------------------------------------------- fade leaks

PC_HZ = {"E": 164.81, "G": 196.00, "A": 220.00, "B": 246.94}
TRIADS = {"E": ("E", "G#", "B"), "A": ("A", "C#", "E"), "G": ("G", "B", "D")}
NOTE = {"C#": 277.18, "D": 293.66, "G#": 207.65, **PC_HZ}


def chords(plan):
    """[(chord, seconds), ...] as sustained triads: harmony moves, loudness doesn't."""
    out = []
    for name, secs in plan:
        t = np.arange(int(secs * me.SR)) / me.SR
        out.append(sum(0.1 * np.sin(2 * np.pi * NOTE[n] * t) for n in TRIADS[name]))
    return np.concatenate(out).astype(np.float32) + silence(sum(s for _, s in plan))


# Held E to 4.0, a cadence E-A-G-E (changes at 4.0, 4.6, 5.2), held E to 7.0,
# then something new (G) at 7.0, held to 10.
PIECE = [("E", 4.0), ("A", 0.6), ("G", 0.6), ("E", 1.8), ("G", 3.0)]


def test_fade_gain_matches_ffmpeg_curves():
    for curve, want in (("tri", -6.02), ("squ", -12.04), ("cub", -18.06)):
        assert abs(me.fade_gain_db(curve, 0.5) - want) <= 0.05, curve
    assert me.fade_gain_db("tri", 0.0) <= -100


def test_harmonic_changes_find_chord_moves_in_steady_loudness():
    times = [t for t, _ in me.harmonic_changes(chords(PIECE), 1, 9.5)]
    for want in (4.0, 4.6, 5.2, 7.0):
        assert any(abs(t - want) <= 0.15 for t in times), (want, times)


def test_fade_leak_flags_audible_music_after_the_landing():
    # The case that found it: a 4 s linear fade past the landing at 5.2.
    lk = me.fade_leak(chords(PIECE), 7.0, 4.0)
    assert lk and lk["kind"] == "landing" and abs(lk["time"] - 5.2) <= 0.15, lk
    assert abs(lk["fix_end"] - 5.7) <= 0.15 and lk["gain_db"] > me.LEAK_DB
    advice = "\n".join(me.leak_advice(lk, 7.0, 4.0, "tri"))
    assert "--end 0:05.7" in advice and "--fade-curve" in advice, advice


def test_fade_leak_clean_when_fade_ends_at_the_landing_or_curve_buries_it():
    x = chords(PIECE)
    assert me.fade_leak(x, 5.7, 2.0) is None            # the suggested fix
    assert me.fade_leak(x, 7.0, 4.0, "cub") is None     # same end, faster curve


def test_fade_leak_flags_a_fade_that_starts_as_something_new_enters():
    # A short fade right as a new passage (G at 7.0) begins.
    lk = me.fade_leak(chords(PIECE), 8.9, 2.0)
    assert lk and lk["kind"] == "entry" and abs(lk["time"] - 7.0) <= 0.15, lk


def test_fade_leak_ignores_short_fades_and_held_chords():
    x = chords(PIECE)
    assert me.fade_leak(x, 7.0, me.RING_FADE) is None
    assert me.fade_leak(chords([("E", 10.0)]), 8.0, 4.0) is None


def test_fade_curve_reaches_ffmpeg_only_when_not_linear():
    tags = {"title": "t"}
    lin = me.ffmpeg_cut_cmd(Path("a.m4a"), Path("b.mp3"), 0, 10, 0.05, 4, tags)
    cub = me.ffmpeg_cut_cmd(Path("a.m4a"), Path("b.mp3"), 0, 10, 0.05, 4, tags, "cub")
    assert "afade=t=out:st=6.000:d=4" in " ".join(lin) and "curve" not in " ".join(lin)
    assert "afade=t=out:st=6.000:d=4:curve=cub" in " ".join(cub)



# ---------------------------------------------------------------- new in the shareable version

def test_snap_end_of_track_keeps_trailing_silence_when_asked():
    x = np.r_[tone(10), tone(2, decay=8), silence(5)]
    end = len(x) / me.SR
    snap = me.snap_cut(x, end, "end", end, trim_tail=False)
    assert abs(snap.time - end) < 0.05 and "trimmed" not in snap.reason, snap


def test_next_version_counts_every_format():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "Opening v1.mp3").touch()
        (d / "Opening v2.wav").touch()
        assert me.next_version_path(d, "Opening", "m4a").name == "Opening v3.m4a"


def test_every_format_encodes():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "src.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=6", str(src)], check=True)
        for fmt, codec in (("mp3", "mp3"), ("m4a", "aac"), ("wav", "pcm_s24le")):
            dst = Path(d) / f"out v1.{fmt}"
            subprocess.run(me.ffmpeg_cut_cmd(src, dst, 1, 5, 0.05, 1, {"comment": "c"}, fmt=fmt), check=True)
            got = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name",
                                  "-of", "default=nw=1:nk=1", str(dst)], capture_output=True, text=True).stdout
            assert got.strip() == codec, (fmt, got)


def run(*argv, env=None):
    """musiccut.py in a subprocess, with its own config file."""
    script = Path(__file__).resolve().parent / "musiccut.py"
    return subprocess.run([sys.executable, str(script), *argv], capture_output=True, text=True,
                          env={**os.environ, **(env or {})})


def test_setup_writes_and_reads_the_config():
    with tempfile.TemporaryDirectory() as d:
        env = {"MUSIC_CUT_CONFIG": str(Path(d) / "c.json")}
        assert "not created yet" in run("setup", "--show", env=env).stdout
        r = run("setup", "--out-dir", d, "--format", "wav", "--keep-silence", env=env)
        assert r.returncode == 0, r.stderr
        cfg = json.loads((Path(d) / "c.json").read_text())
        assert cfg == {"out_dir": d, "format": "wav", "trim_trailing_silence": False}, cfg


def test_cut_end_to_end_writes_a_new_version_and_never_overwrites():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        src = d / "song.wav"
        # 0.5 s of silence, a 440 Hz tone to 9 s that decays, then 4 s of silence.
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "aevalsrc='if(lt(t,0.5),0,0.3*sin(2*PI*440*t)*if(gt(t,9),exp(-8*(t-9)),1))':d=13:s=44100",
                        str(src)], check=True)
        env = {"MUSIC_CUT_CONFIG": str(d / "none.json")}
        info = run("info", str(src), env=env).stdout
        assert "music starts 0:00.4" in info and "silence after it" in info, info
        r = run("cut", str(src), "--start", "0:00", "--name", "Song", "--png", str(d / "p.png"), env=env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "trailing silence trimmed" in r.stdout and (d / "Song v1.mp3").is_file(), r.stdout
        assert (d / "p.png").is_file()
        length = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                       "-of", "default=nw=1:nk=1", str(d / "Song v1.mp3")],
                                      capture_output=True, text=True).stdout)
        assert 9.3 < length < 11.5, length
        tag = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags=comment",
                              "-of", "default=nw=1:nk=1", str(d / "Song v1.mp3")],
                             capture_output=True, text=True).stdout
        assert tag.startswith("src=song.wav 0:00.00-"), tag
        r2 = run("cut", str(src), "--start", "0:02", "--end", "0:06", "--offset", "1", "--exact",
                 "--name", "Song", "--dry-run", env=env)
        assert "0:03.00 → 0:03.00" in r2.stdout and "Song v2.mp3" in r2.stdout, r2.stdout
        assert not (d / "Song v2.mp3").exists()


def test_cut_refuses_a_range_outside_the_file():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "s.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=d=5", str(src)], check=True)
        r = run("cut", str(src), "--start", "0:02", "--end", "0:09", env={"MUSIC_CUT_CONFIG": str(Path(d) / "n")})
        assert r.returncode != 0 and "outside the file" in r.stderr, r.stderr


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
