#!/usr/bin/env python3
"""Offline checks for printrun.py. No printer needed: run with
`python3 test_printrun.py` (needs pypdf)."""
import copy
import json
import sys
import tempfile
from pathlib import Path

from pypdf import PdfReader, PdfWriter

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import printrun as pr  # noqa: E402

EXAMPLE = json.loads((HERE.parent / "references" / "config-example.json").read_text())


def pdf(tmp, name, pages, size=(612, 792)):
    f = Path(tmp) / f"{name}.pdf"
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(*size)
    with f.open("wb") as fh:
        w.write(fh)
    return str(f)


def cfg(tmp):
    c = copy.deepcopy(EXAMPLE)
    c["pieces"]["show-postcards"]["file"] = pdf(tmp, "pc", 1)
    c["pieces"]["show-flyer"]["file"] = pdf(tmp, "fl", 1, (630, 810))   # with bleed
    c["pieces"]["show-program"]["file"] = pdf(tmp, "pg", 7, (414, 630))
    c["pieces"]["class-postcards"] = {"kind": "postcard", "label": "Class postcards",
                                      "file": pdf(tmp, "pc2", 2), "per_sheet": 4}
    return c


def test_example_config_loads():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "c.json"
        f.write_text(json.dumps(EXAMPLE))
        assert set(pr.load_config(f)["pieces"]) == {"show-postcards", "show-flyer", "show-program"}


def test_ambiguous_name_asks():
    with tempfile.TemporaryDirectory() as d:
        c = cfg(d)
        assert pr.resolve(c, "programs") == "show-program"
        assert pr.resolve(c, "class") == "class-postcards"
        try:
            pr.resolve(c, "postcards")
        except SystemExit as e:
            assert "Which one?" in str(e)
        else:
            raise AssertionError("guessed")


def test_plan_stack_and_sheets():
    with tempfile.TemporaryDirectory() as d:
        c = cfg(d)
        lines = pr.parse_request(c, ["show-flyer=10", "show-postcards=42", "class-postcards=8", "programs=5"])
        # Same paper grouped: glossy first (asked first), then all the cardstock.
        assert [l.piece for l in lines] == ["show-flyer", "show-postcards", "class-postcards", "show-program"]
        assert [l.sheets(c) for l in lines] == [10, 11, 2, 10]
        text = pr.plan_text(c, lines)
        assert text.index("23 sheets of cardstock (first, at the bottom)") < \
            text.index("10 sheets of glossy photo paper (last, on top)"), text
        assert "makes 44" in text and "two-sided" in text and "fold them in half" in text


def test_glossy_cant_go_two_sided():
    with tempfile.TemporaryDirectory() as d:
        try:
            pr.parse_request(cfg(d), ["class-postcards=4", "class-postcards:paper=glossy"])
        except SystemExit as e:
            assert "two-sided" in str(e)
        else:
            raise AssertionError("accepted")


def test_lp_flags():
    with tempfile.TemporaryDirectory() as d:
        c = cfg(d)
        fl, pc2, prog = pr.parse_request(c, ["show-flyer=3", "class-postcards=8", "programs=2"])
        f = pr.lp_command(c, fl, Path("x.pdf"))
        assert "PageSize=Letter.Fullbleed" in f and "sides=one-sided" in f and "MediaType=photographic-glossy" in f
        assert "sides=two-sided-long-edge" in pr.lp_command(c, pc2, Path("x.pdf"))
        p = pr.lp_command(c, prog, Path("x.pdf"))
        assert "sides=two-sided-short-edge" in p and "Collate=True" in p and p[p.index("-n") + 1] == "2"


def test_bleed_trim_and_booklet():
    with tempfile.TemporaryDirectory() as d:
        t = pr.trimmed_pdf(Path(pdf(d, "b", 1, (630, 810))), Path(d))
        assert float(PdfReader(str(t)).pages[0].mediabox.width) == 612
        assert pr._fit(PdfReader(pdf(d, "l", 1)).pages[0], pr.HALF)[1] is False  # scale, don't crop
        assert pr.booklet_order(8) == [(7, 0), (1, 6), (5, 2), (3, 4)]
        out = PdfReader(str(pr.imposed_pdf(Path(pdf(d, "p", 7, (414, 630))), Path(d))))
        assert len(out.pages) == 4 and float(out.pages[0].mediabox.width) == 792


def test_parse_ipp():
    reply = ("printer-state (enum) = idle\n"
             "printer-make-and-model (textWithoutLanguage) = HP Smart Tank 7000 series\n"
             "job-constraints-supported (1setOf collection) = {resolver-name=duplex-types "
             "sides=two-sided-short-edge,two-sided-long-edge media=na_letter_8.5x11in "
             "media-type=photographic-glossy,com.hp-photographic-inkjet}\n")
    got = pr.parse_ipp(reply)
    assert got["make"] == "HP Smart Tank 7000 series"
    assert got["no_duplex_media"] == ["photographic-glossy", "com.hp-photographic-inkjet"]
    assert "didn't answer" in pr.parse_ipp("")["ipp"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
