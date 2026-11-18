---
name: print-run
description: Print a mixed batch of documents on your own printer with the right paper, settings and stack order, e.g. "print 40 postcards and 10 flyers" or "print 6 copies of the program". Works out how many sheets of each paper you need and the order to stack them in the tray, prints two-sided and folded booklets where a piece needs it, waits until you say the paper is loaded, then tells you when the printer is done. Use for any request to print things you print often, and to set up a new printable piece. macOS or Linux with CUPS.
---

# Print run

A home or office printer has one tray and a dozen settings. Every time you
print something you've printed before, you have to remember which paper it
goes on, whether it's borderless or two-sided, how many fit on a sheet, and
which file is the current one. This skill remembers that for you. When a run
mixes papers, it stacks them in one tray in the right order: the tray feeds
from the top, so the first job's paper goes on top.

Everything runs through `scripts/printrun.py`. It needs Python 3.10+ and
`pypdf`. Run it with `uv run scripts/printrun.py ...` (uv installs pypdf for
you) or `pip install pypdf` once and use `python3`. Settings live in
`~/.config/print-run/config.json` (or `$PRINT_RUN_CONFIG`).

## First run: set up (once)

If the config file doesn't exist, set it up before printing anything. Keep
it conversational: a few questions, sensible defaults offered, nothing
asked twice.

1. **Ask the printer what it can do.** Run `printrun.py detect`. It lists the
   printers, the default, the paper types the driver knows (`media_types`),
   whether borderless Letter exists, whether it can print two-sided, and, if
   the printer answers over IPP, which paper types it refuses to print
   two-sided (`no_duplex_media`). If `ipp` says the printer didn't answer, ask
   them to wake it up, then run it again.
2. **Which printer?** Say which one is the default, and confirm it if
   there's more than one.
3. **Where do your print files live?** One or more folders. Ask them for the
   path, or search for likely ones (Dropbox, Documents, a design folder) and
   confirm. Point the config at files **where they live**. Don't copy them
   into a print folder: a copy goes stale the moment the design changes.
4. **What do you print, and on what paper?** Offer this as a starting point
   and let them change it. Most people print a handful of kinds:

   | kind | paper | sides |
   |---|---|---|
   | flyer, sign, poster | glossy photo | one-sided |
   | postcard | cardstock | two-sided if the file has 2 pages |
   | booklet | cardstock | two-sided |
   | program (folded 8.5x5.5) | cardstock | folded booklet |
   | document, worksheet, coloring page | plain | one-sided |

   Ask whether they print borderless (full bleed). If the printer supports it
   (`borderless_letter`), most people who print designed pieces want it on.
5. **Map each paper to the printer's paper setting** from `media_types`:
   plain → `stationery`; glossy → a `photographic-glossy` / `*glossy*` type;
   cardstock → a heavy matte type (`*matte-brochure*`, `*heavyweight*`,
   `*cardstock*`; on HP, `com.hp-matte-brochure`). Use `High` quality for
   photo and cardstock papers and `Normal` for plain. Mark any paper whose type
   is in `no_duplex_media` with `"one_sided_only": true`.
6. **Add their pieces.** For each thing they print often, find the final
   print-ready PDF in their folders. Check its page count and size, look at
   it, and ask about anything unclear (is it 4-up? a front and back?). Give
   it an id, a label, a kind and a few aliases. Pieces that come several to a
   sheet get `per_sheet`. A note to show when the run finishes (cutting,
   folding) goes in `after`.
7. Write the config (see `references/config-example.json` for the shape), run
   `printrun.py list` and `printrun.py plan <piece>=1`, and show them the result.
8. **Suggest one test sheet** of anything two-sided or folded, so they can check
   the backs line up and that their cardstock goes through the duplexer.

## Every print request

1. **Match each piece.** Run `printrun.py list`. If their words fit more than
   one piece, ask which ("postcards: the show ones or the class ones?").
   `printrun.py` refuses to guess.
2. **Get the quantity in pieces.** If they said "a few", ask for a number.
3. **Show the plan** with `printrun.py plan <piece=qty ...>`: what's printing,
   how many sheets of each paper, and the order to load them. Add
   `piece:paper=plain` to change a piece's paper for one run.
4. **Wait for the paper.** Ask them to load it as the plan says and tell you
   when it's in. Don't print before they say so; nothing else can check that
   the right paper is in.
5. **Print** with `printrun.py print <same items> --yes --wait`. Run it in the
   background: it exits when the printer's queue empties, raises a desktop
   notification, and prints any `after` notes. Then tell them it's done.

If something goes wrong: a two-sided piece on paper the printer can't print
two-sided on gets refused before anything prints. A missing file names the
path, which usually means it was renamed or moved, so find the new one and
update the config. If the queue hasn't emptied after an hour, they should
check the printer for a jam or an empty tray.

## Adding a piece later

Same as setup step 6. Folded programs work best exported as plain pages in
reading order at 5.5x8.5in: `printrun.py` arranges them on the sheet so the
printed stack folds straight into a booklet. Files exported with bleed get the
bleed trimmed so they print at 100%. A full Letter page going into a booklet
is scaled down, not cropped.

## Going further

The plan-then-confirm shape works from anywhere a person can answer:
a chat bot posting the plan with a "paper's in, print" button, a task card
that asks for the print run, a phone. The rule stays the same: anything can
*ask* for a print run, but only a person who just loaded the paper starts it.
