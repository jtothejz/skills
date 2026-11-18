# print-run

A Claude skill for the printer in your house or office.

Tell Claude "print 40 postcards and 10 flyers." It finds the files, works out
how many sheets of each paper you need, and tells you how to stack them in the
tray. The tray feeds from the top, so the first job's paper goes on top. It
waits for you to say the paper is in, prints everything in that order with the
right settings (borderless, two-sided, folded booklets), and tells you when the
printer is done.

```
To print:
  • 40 Show postcards: 10 sheets of cardstock, 4 per sheet
  • 10 Show flyers: 10 sheets of glossy photo paper

Load the tray in this order, print side down (it feeds from the top):
  1. 10 sheets of glossy photo paper (first, at the bottom)
  2. 10 sheets of cardstock (last, on top)
Total: 20 sheets.
```

## What it handles

- **Paper per kind of thing.** Flyers on glossy, postcards on cardstock,
  worksheets on plain, or whatever your rules are. Set them once.
- **Mixed runs in one tray**, stacked in print order.
- **Several to a sheet.** Ask for 42 postcards at 4-up and it prints 11 sheets
  (and tells you that makes 44).
- **Two-sided**, automatically for files with a front and back. It refuses
  paper your printer can't print two-sided on, before wasting a sheet.
- **Folded booklets.** Give it a program's pages in reading order at
  5.5x8.5in. It arranges them on the sheets so the printed stack folds
  straight into a booklet.
- **Borderless.** Files exported with bleed get the bleed trimmed so they
  print at 100%.
- **Ambiguous names.** If you have show postcards and class postcards,
  "print postcards" gets a "which one?" instead of a guess.

## First run

The first time you use it, Claude sets it up with you:

1. It asks your printer what it can do: paper types, borderless, two-sided,
   and which papers it won't print two-sided (over IPP, when the printer
   answers).
2. It asks where your print files live. Files stay where they are; a copy in
   a "print" folder goes stale when the design changes.
3. It asks what paper each kind of thing goes on, starting from sensible
   defaults.
4. It adds the pieces you print often, after looking at each file.

Everything goes in `~/.config/print-run/config.json`. See
[`references/config-example.json`](references/config-example.json).

## Install

**Claude Code**

```bash
git clone https://github.com/jtothejz/print-run ~/.claude/skills/print-run
```

**Claude apps (claude.ai, Desktop):** download `print-run.skill` from the
[latest release](https://github.com/jtothejz/print-run/releases/latest) and
upload it under Settings → Capabilities → Skills.

You need macOS or Linux with CUPS (any printer your computer can already print
to), Python 3.10+, and [`uv`](https://docs.astral.sh/uv/) or `pip install pypdf`.

## Using the script directly

```bash
uv run scripts/printrun.py detect                     # what your printer supports
uv run scripts/printrun.py list                       # your pieces
uv run scripts/printrun.py plan flyers=10 postcards=40
uv run scripts/printrun.py print flyers=10 postcards=40 --yes --wait
```

`--dry-run` shows the exact `lp` commands without printing. Tests:
`python3 scripts/test_printrun.py` (offline, no printer needed).

## Why a person still presses go

Only a person can put paper in the tray, so nothing prints until someone says
it's loaded. If you hook this up to a chat bot or a task board, keep that
rule: anything can ask for a print run, but only a person who just loaded
the paper starts it.

## Status

Built for a dance studio's HP Smart Tank and in use there. Paper names are
tested on HP's driver. Other brands name their paper types differently;
first-run setup maps them from what your printer reports. Two-sided borderless
cardstock depends on your printer's duplexer, so print one test sheet first.

MIT license.
