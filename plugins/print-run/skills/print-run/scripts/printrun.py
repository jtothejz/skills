#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pypdf>=4"]
# ///
"""printrun: print a mixed run of documents on a home or office printer with
the right paper, settings and stack order.

"Print 40 postcards and 10 flyers" turns into a paper plan: how many sheets
of each paper, the order to stack them in the one tray (it feeds from the
top, so the first job's paper goes on top), two-sided where the piece needs
it, folded booklets imposed for you. It waits for a person to say the paper
is in, sends the jobs in stack order, and tells you when the printer is done.

Works with any CUPS printer (macOS, Linux). Settings live in one JSON file,
~/.config/print-run/config.json, or $PRINT_RUN_CONFIG.

  printrun.py detect                    # printers, paper types, borderless, duplex
  printrun.py list                      # the pieces you've set up
  printrun.py plan  flyers=10 postcards=40 [flyers:paper=plain]
  printrun.py print flyers=10 postcards=40 --yes [--wait] [--dry-run]

Quantities are in pieces, not sheets: 40 postcards at 4 per sheet is 10
sheets. Without --yes, `print` only shows the plan.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

CONFIG = Path(os.environ.get("PRINT_RUN_CONFIG", "~/.config/print-run/config.json")).expanduser()
LETTER = (612.0, 792.0)   # points
HALF = (396.0, 612.0)     # 5.5 x 8.5in, one booklet page
BLEED_MAX = 36.0          # up to 0.25in a side counts as bleed; more is a different size


def _pypdf():
    try:
        import pypdf  # noqa: F401
        return pypdf
    except ImportError:
        raise SystemExit("needs pypdf: `pip install pypdf` (or run with `uv run printrun.py ...`)")


# --- detect -------------------------------------------------------------------

def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def detect() -> dict:
    """What CUPS and the printer itself say: printers, the default, media
    types, whether borderless Letter exists, and which media types the
    printer refuses to print two-sided (from IPP job-constraints)."""
    if not shutil.which("lpstat"):
        raise SystemExit("no CUPS here (lpstat not found); this skill needs macOS or Linux with CUPS")
    printers = re.findall(r"^printer (\S+)", _run(["lpstat", "-p"]), re.M)
    default = (re.search(r"destination: (\S+)", _run(["lpstat", "-d"])) or [None, None])[1]
    out = {"default": default, "printers": {}}
    for p in printers:
        opts = _run(["lpoptions", "-p", p, "-l"])
        line = lambda key: next((l for l in opts.splitlines() if l.startswith(key + "/")), "")  # noqa: E731
        vals = lambda key: [v.lstrip("*") for v in line(key).split(":", 1)[-1].split()]  # noqa: E731
        info = {
            "media_types": vals("MediaType"),
            "borderless_letter": "Letter.Fullbleed" in vals("PageSize"),
            "duplex": [v for v in vals("Duplex") if v != "None"],
            "quality": vals("cupsPrintQuality"),
        }
        uri = (re.search(r"device-uri=(ipps?://\S+)", _run(["lpoptions", "-p", p])) or [None, None])[1]
        if uri and shutil.which("ipptool"):
            info.update(parse_ipp(_run(["ipptool", "-tv", uri, "get-printer-attributes.test"])))
        else:
            info["ipp"] = "not an IPP printer; duplex limits unknown"
        out["printers"][p] = info
    return out


def parse_ipp(attrs: str) -> dict:
    """The printer's own answer to get-printer-attributes: its model, and the
    media types its job-constraints say can't go two-sided."""
    if "printer-state" not in attrs:
        return {"ipp": "printer didn't answer (asleep or off the network?); "
                       "wake it and run detect again to learn its duplex limits"}
    no_duplex = []
    for m in re.finditer(r"resolver-name=duplex-types[^}]*?media-type=([\w.,-]+)", attrs):
        no_duplex += m[1].split(",")
    make = re.search(r"printer-make-and-model \S+ = (.+)", attrs)
    return {"ipp": "ok", "make": make[1].strip() if make else "", "no_duplex_media": no_duplex}


# --- config -------------------------------------------------------------------

def load_config(path: Path = CONFIG) -> dict:
    if not path.is_file():
        raise SystemExit(f"no config yet at {path}. Run the skill's first-time setup "
                         "(or copy references/config-example.json there and edit it).")
    cfg = json.loads(path.read_text())
    for key in ("printer", "papers", "kinds", "pieces"):
        if key not in cfg:
            raise SystemExit(f"config is missing {key!r}")
    return cfg


def _norm(s: str) -> str:
    return re.sub(r"[\s_-]+", " ", s.strip().lower())


def resolve(cfg: dict, name: str) -> str:
    """An id or alias exactly; else the one piece whose names contain every
    word. More than one match is a question, not a guess."""
    key = _norm(name)
    names = {pid: [_norm(pid), _norm(p.get("label", pid))] + [_norm(a) for a in p.get("aliases", [])]
             for pid, p in cfg["pieces"].items()}
    for pid, ns in names.items():
        if key == ns[0] or key in ns[2:]:
            return pid
    words = [w.rstrip("s") for w in key.split()]
    hits = [pid for pid, ns in names.items() if all(any(w in n for n in ns) for w in words)]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise SystemExit(f"{name!r} could be: " + ", ".join(hits) + ". Which one?")
    raise SystemExit(f"no piece called {name!r}; `printrun.py list` shows what's set up")


def spec(cfg: dict, pid: str) -> dict:
    p = cfg["pieces"][pid]
    base = {"per_sheet": 1, "borderless": cfg.get("borderless", True), "sides": "one", "fold": None,
            "label": pid}
    return {**base, **cfg["kinds"].get(p.get("kind", ""), {}), **p}


_PAGES: dict[str, int] = {}


def page_count(path: Path) -> int:
    key = str(path)
    if key not in _PAGES:
        if not path.is_file():
            raise SystemExit(f"print file is missing (moved or renamed?): {path}")
        _PAGES[key] = len(_pypdf().PdfReader(key).pages)
    return _PAGES[key]


def layout(cfg: dict, pid: str) -> dict:
    """Sheets per copy and duplex. sides "auto" = two-sided when the file has
    more than one page; fold "booklet" = half-letter pages imposed two per
    side and folded."""
    s = spec(cfg, pid)
    pages = page_count(Path(s["file"]).expanduser())
    if s["fold"] == "booklet":
        return {"pages": pages, "sheets": math.ceil(pages / 4), "sides": "two-sided-short-edge"}
    if s["sides"] in ("auto", "two") and (pages > 1 or s["sides"] == "two"):
        return {"pages": pages, "sheets": math.ceil(pages / 2), "sides": "two-sided-long-edge"}
    return {"pages": pages, "sheets": pages, "sides": "one-sided"}


@dataclass
class Line:
    piece: str
    qty: int
    paper: str

    def copies(self, cfg: dict) -> int:
        return math.ceil(self.qty / spec(cfg, self.piece)["per_sheet"])

    def sheets(self, cfg: dict) -> int:
        return self.copies(cfg) * layout(cfg, self.piece)["sheets"]


def parse_request(cfg: dict, args: list[str]) -> list[Line]:
    lines: dict[str, Line] = {}
    papers: dict[str, str] = {}
    for a in args:
        m = re.fullmatch(r"(.+?):paper=([\w-]+)", a)
        if m:
            papers[resolve(cfg, m[1])] = m[2].lower()
            continue
        m = re.fullmatch(r"(.+?)=(\d+)", a)
        if not m or int(m[2]) <= 0:
            raise SystemExit(f"can't read {a!r}; write piece=quantity, e.g. flyers=10")
        pid = resolve(cfg, m[1])
        lines[pid] = Line(pid, int(m[2]), spec(cfg, pid)["paper"])
    for pid, paper in papers.items():
        if paper not in cfg["papers"]:
            raise SystemExit(f"unknown paper {paper!r}; one of {', '.join(cfg['papers'])}")
        if pid not in lines:
            raise SystemExit(f"paper given for {pid} but no quantity")
        lines[pid].paper = paper
    if not lines:
        raise SystemExit("nothing to print")
    for ln in lines.values():
        if layout(cfg, ln.piece)["sides"] != "one-sided" and cfg["papers"][ln.paper].get("one_sided_only"):
            raise SystemExit(f"{spec(cfg, ln.piece)['label']} is two-sided, and the printer won't print "
                             f"two-sided on {cfg['papers'][ln.paper]['label']}. Pick another paper.")
    order = list(dict.fromkeys(ln.paper for ln in lines.values()))
    return sorted(lines.values(), key=lambda ln: order.index(ln.paper))


def _n(n: int) -> str:
    return f"{n} sheet{'s' if n != 1 else ''}"


def plan_text(cfg: dict, lines: list[Line]) -> str:
    papers = cfg["papers"]
    out, folds = ["To print:"], []
    for ln in lines:
        p, lay = spec(cfg, ln.piece), layout(cfg, ln.piece)
        made = ln.copies(cfg) * p["per_sheet"]
        bonus = f" (makes {made})" if made != ln.qty else ""
        per = f", {p['per_sheet']} per sheet" if p["per_sheet"] > 1 else ""
        two = ", two-sided" if lay["sides"] != "one-sided" else ""
        out.append(f"  • {ln.qty} {p['label']}{bonus}: {_n(ln.sheets(cfg))} of "
                   f"{papers[ln.paper]['label']}{per}{two}")
        if p["fold"] == "booklet":
            folds.append(f"{p['label']}: every {_n(lay['sheets'])} = one booklet; fold them in half together.")
    out.append("")
    blocks: list[list] = []
    for ln in lines:
        if blocks and blocks[-1][0] == ln.paper:
            blocks[-1][1] += ln.sheets(cfg)
        else:
            blocks.append([ln.paper, ln.sheets(cfg)])
    side = cfg.get("load_side", "print side down")
    if len(blocks) == 1:
        out.append(f"Load {_n(blocks[0][1])} of {papers[blocks[0][0]]['label']}, {side}.")
    else:
        out.append(f"Load the tray in this order, {side} (it feeds from the top):")
        for i, (paper, n) in enumerate(reversed(blocks), 1):
            where = "first, at the bottom" if i == 1 else ("last, on top" if i == len(blocks) else "next")
            out.append(f"  {i}. {_n(n)} of {papers[paper]['label']} ({where})")
    out.append(f"Total: {_n(sum(n for _, n in blocks))}.")
    out.extend(folds)
    return "\n".join(out)


# --- files --------------------------------------------------------------------

def _fit(page, size):
    """Crop a bleed export to `size`, centred. Bigger-than-bleed pages are
    left alone to be scaled."""
    mb = page.mediabox
    dw, dh = float(mb.width) - size[0], float(mb.height) - size[1]
    if (dw, dh) == (0, 0) or not (0 <= dw <= BLEED_MAX and 0 <= dh <= BLEED_MAX):
        return page, False
    x0, y0 = float(mb.left) + dw / 2, float(mb.bottom) + dh / 2
    for box in (page.mediabox, page.cropbox, page.trimbox, page.bleedbox):
        box.lower_left = (x0, y0)
        box.upper_right = (x0 + size[0], y0 + size[1])
    return page, True


def trimmed_pdf(src: Path, tmp: Path) -> Path:
    pypdf = _pypdf()
    w, changed = pypdf.PdfWriter(), False
    for page in pypdf.PdfReader(str(src)).pages:
        page, c = _fit(page, LETTER)
        changed |= c
        w.add_page(page)
    if not changed:
        return src
    out = tmp / f"{src.stem} (trimmed).pdf"
    with out.open("wb") as fh:
        w.write(fh)
    return out


def booklet_order(n: int) -> list[tuple]:
    """(left, right) page indexes per side for a folded booklet, sheet by
    sheet, front then back. None = blank padding page."""
    total = math.ceil(n / 4) * 4
    idx = lambda i: i if i < n else None  # noqa: E731
    sides = []
    for s in range(total // 4):
        sides += [(idx(total - 1 - 2 * s), idx(2 * s)), (idx(2 * s + 1), idx(total - 2 - 2 * s))]
    return sides


def imposed_pdf(src: Path, tmp: Path) -> Path:
    pypdf = _pypdf()
    pages = [_fit(p, HALF)[0] for p in pypdf.PdfReader(str(src)).pages]
    w = pypdf.PdfWriter()
    for pair in booklet_order(len(pages)):
        sheet = pypdf.PageObject.create_blank_page(width=LETTER[1], height=LETTER[0])
        for slot, i in enumerate(pair):
            if i is None:
                continue
            mb = pages[i].mediabox
            pw, ph = float(mb.width), float(mb.height)
            k = min(HALF[0] / pw, HALF[1] / ph)
            tx = slot * HALF[0] + (HALF[0] - pw * k) / 2 - float(mb.left) * k
            ty = (HALF[1] - ph * k) / 2 - float(mb.bottom) * k
            sheet.merge_transformed_page(pages[i], pypdf.Transformation().scale(k).translate(tx, ty))
        w.add_page(sheet)
    out = tmp / f"{src.stem} (booklet).pdf"
    with out.open("wb") as fh:
        w.write(fh)
    return out


# --- printing -----------------------------------------------------------------

def lp_command(cfg: dict, ln: Line, file: Path) -> list[str]:
    p, paper, lay = spec(cfg, ln.piece), cfg["papers"][ln.paper], layout(cfg, ln.piece)
    size = cfg.get("page_size", "Letter")
    size = f"{size}.Fullbleed" if p["borderless"] else size
    duplex = {"one-sided": "None", "two-sided-long-edge": "DuplexNoTumble",
              "two-sided-short-edge": "DuplexTumble"}[lay["sides"]]
    cmd = ["lp", "-d", cfg["printer"], "-n", str(ln.copies(cfg)), "-t", f"{p['label']} x{ln.qty}",
           "-o", f"PageSize={size}", "-o", f"media={size}", "-o", f"sides={lay['sides']}",
           "-o", f"Duplex={duplex}", "-o", f"Collate={'True' if lay['sheets'] > 1 else 'False'}",
           "-o", "print-scaling=none"]
    if paper.get("media"):
        cmd += ["-o", f"MediaType={paper['media']}"]
    if paper.get("quality"):
        cmd += ["-o", f"cupsPrintQuality={paper['quality']}"]
    return cmd + [str(file)]


def submit(cfg: dict, lines: list[Line], tmp: Path, dry: bool = False) -> list[str]:
    ids = []
    for ln in lines:
        s = spec(cfg, ln.piece)
        src = Path(s["file"]).expanduser()
        ready = imposed_pdf(src, tmp) if s["fold"] == "booklet" else trimmed_pdf(src, tmp)
        cmd = lp_command(cfg, ln, ready)
        if dry:
            print(" ".join(f"'{c}'" if " " in c else c for c in cmd))
            continue
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
        ids.append((re.search(r"request id is (\S+)", out) or [None, out.strip()])[1])
    return ids


def wait(ids: list[str], timeout_s: int = 3600) -> bool:
    end = time.time() + timeout_s
    while time.time() < end:
        if not any(i in _run(["lpstat", "-o"]) for i in ids):
            return True
        time.sleep(10)
    return False


def notify(msg: str) -> None:
    if sys.platform == "darwin":
        _run(["osascript", "-e", f'display notification "{msg}" with title "Printer" sound name "Glass"'])
    elif shutil.which("notify-send"):
        _run(["notify-send", "Printer", msg])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["detect", "list", "plan", "print"])
    ap.add_argument("items", nargs="*")
    ap.add_argument("--yes", action="store_true", help="the paper is loaded; send the jobs")
    ap.add_argument("--wait", action="store_true", help="block until the printer finishes")
    ap.add_argument("--dry-run", action="store_true", help="show the lp commands only")
    a = ap.parse_args(argv)

    if a.cmd == "detect":
        print(json.dumps(detect(), indent=2))
        return 0
    cfg = load_config()
    if a.cmd == "list":
        for pid in cfg["pieces"]:
            s = spec(cfg, pid)
            print(f"{pid}: {s['label']} [{s.get('kind', '-')}] {s['per_sheet']}/sheet on {s['paper']}"
                  f"{', folded booklet' if s['fold'] else ''}{', borderless' if s['borderless'] else ''}")
        return 0
    lines = parse_request(cfg, a.items)
    print(plan_text(cfg, lines))
    if a.cmd == "plan" or not (a.yes or a.dry_run):
        if a.cmd == "print":
            print("\nNot sent. Load the paper, then run again with --yes.")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        ids = submit(cfg, lines, Path(tmp), dry=a.dry_run)
        if a.dry_run:
            return 0
        print("\nSent: " + ", ".join(ids))
        if a.wait:
            done = wait(ids)
            notes = " ".join(spec(cfg, ln.piece).get("after", "") for ln in lines).strip()
            msg = (f"Print run finished. {notes}" if done
                   else "Print run still going after an hour; check for a jam or empty tray.").strip()
            print(msg)
            notify(msg)
            return 0 if done else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
