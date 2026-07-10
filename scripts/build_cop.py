#!/usr/bin/env python3
"""Build the Web COP's distribution copies from the single source of truth.

Source:   site/index.html                          (full page; deployed to GitHub Pages)
Outputs:  cuas-cop-demo.html                       (identical standalone copy)
          services/c2-core/app/static/cop.html     (inner content; c2-core wraps it
                                                    and injects the LIVE-mode flag)

The build also stamps every copy with an 8-hex content hash (COP_BUILD) computed
over the source with the stamp field zeroed, so the deployed page can report
exactly which build a viewer is looking at. Running the build twice is a no-op.

Usage:
  python3 scripts/build_cop.py            # stamp + regenerate copies
  python3 scripts/build_cop.py --check    # verify no drift (CI); exit 1 on drift
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "site" / "index.html"
DEMO = ROOT / "cuas-cop-demo.html"
SERVED = ROOT / "services" / "c2-core" / "app" / "static" / "cop.html"

STAMP_RE = re.compile(r'(const COP_BUILD = ")[0-9a-f]{8}(";)')
FAVICON = '<link rel="icon" href="data:,"></head><body>'


def stamped_source() -> str:
    src = SOURCE.read_text()
    if not STAMP_RE.search(src):
        sys.exit("build_cop: COP_BUILD stamp line not found in site/index.html")
    normalized = STAMP_RE.sub(r"\g<1>00000000\g<2>", src)
    digest = hashlib.sha256(normalized.encode()).hexdigest()[:8]
    return STAMP_RE.sub(rf"\g<1>{digest}\g<2>", src)


def strip_wrapper(full: str) -> str:
    """Inner content for the c2-core-served copy (it provides its own wrapper)."""
    lines = full.split("\n")
    assert lines[0] == "<!doctype html>", "unexpected source prologue"
    i = lines[1].index("<title>")
    title_line = lines[1][i:]
    line3 = lines[2].replace(FAVICON, "")
    body = "\n".join(lines[3:]).rstrip("\n")
    assert body.endswith("</body></html>"), "unexpected source epilogue"
    body = body[: -len("</body></html>")].rstrip("\n")
    return title_line + "\n" + line3 + "\n" + body + "\n"


def main() -> int:
    check = "--check" in sys.argv
    full = stamped_source()
    outputs = {SOURCE: full, DEMO: full, SERVED: strip_wrapper(full)}
    drift = [
        str(p.relative_to(ROOT))
        for p, want in outputs.items()
        if not p.exists() or p.read_text() != want
    ]
    if check:
        if drift:
            print("COP build drift in: " + ", ".join(drift))
            print("run `make build-cop` and commit the result")
            return 1
        print("COP copies in sync (build " + STAMP_RE.search(full).group(0)[19:27] + ")")
        return 0
    for p, want in outputs.items():
        p.write_text(want)
    print("built COP " + STAMP_RE.search(full).group(0)[19:27] + " -> " +
          ", ".join(str(p.relative_to(ROOT)) for p in outputs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
