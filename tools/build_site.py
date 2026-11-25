#!/usr/bin/env python3
"""Build the combined flex-pse documentation site.

    python tools/build_site.py --out _site
    python -m http.server --directory _site 8000

Steps, in order:

1. **Fetch** each repo in ``sites.yml`` (shallow clone of ``ref``), or use a local
   checkout given with ``--local ID=PATH``.
2. **Build** each repo's docs with its own command. A repo's
   ``[tool.flexdocs]`` table in pyproject.toml overrides the manifest, so the
   command can live next to the code it builds.
3. **Mount** each build's output at ``/<mount>/`` and add the shared nav bar.
4. **Graph**: derive the dependency graph from the checkouts (``depgraph.py``).
5. **Render** the landing page and write ``depgraph.json``.
6. **Verify** every mount has an index.html and every landing-page link resolves.

``--prebuilt ID=DIR`` skips step 2 for one repo and mounts DIR instead (useful
for previews and for working on the landing page without heavy builds).
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import venv
from html.parser import HTMLParser
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

sys.path.insert(0, str(Path(__file__).resolve().parent))
import depgraph  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
NAV_MARK = "data-flexpse-nav"


def log(msg: str) -> None:
    print(f"[site] {msg}", flush=True)


def run(cmd: str, cwd: Path, env: dict | None = None) -> None:
    log(f"$ {cmd}  (in {cwd.name})")
    subprocess.run(cmd, shell=True, cwd=cwd, env=env, check=True)


def pairs(values: list[str], flag: str) -> dict[str, Path]:
    out = {}
    for v in values:
        if "=" not in v:
            sys.exit(f"{flag} expects ID=PATH, got {v!r}")
        k, p = v.split("=", 1)
        out[k] = Path(p).resolve()
    return out


# ------------------------------------------------------------------- fetch

def fetch(site: dict, work: Path, local: dict[str, Path]) -> tuple[Path, str]:
    if site["id"] in local:
        path = local[site["id"]]
    else:
        path = work / "src" / site["id"]
        if path.exists():
            shutil.rmtree(path)
        run(f"git clone --depth 1 --branch {site.get('ref', 'main')} "
            f"https://github.com/{site['repo']}.git {path}", cwd=work)
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True,
                             text=True, check=True).stdout.strip()
    except subprocess.CalledProcessError:
        sha = ""
    return path, sha


# ------------------------------------------------------------------- build

def build_config(site: dict, checkout: Path) -> dict:
    cfg = dict(site.get("build", {}))
    own = depgraph.read_pyproject(checkout).get("tool", {}).get("flexdocs")
    if own:
        log(f"{site['id']}: using [tool.flexdocs] from its pyproject.toml")
        cfg.update(own)
    for key in ("setup", "build"):
        if isinstance(cfg.get(key), str):
            cfg[key] = [cfg[key]]
    if "out" not in cfg or not cfg.get("build"):
        sys.exit(f"{site['id']}: build config needs `build` and `out`")
    return cfg


def build(site: dict, checkout: Path, work: Path) -> Path:
    cfg = build_config(site, checkout)
    env = dict(os.environ)
    if cfg.get("python", "venv") == "venv":
        vdir = work / "venvs" / site["id"]
        if not vdir.exists():
            venv.create(vdir, with_pip=True)
        bindir = vdir / ("Scripts" if os.name == "nt" else "bin")
        env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
        env["VIRTUAL_ENV"] = str(vdir)
        env.pop("PYTHONHOME", None)
    for cmd in cfg.get("setup", []) + cfg["build"]:
        run(cmd, cwd=checkout, env=env)
    return checkout / cfg["out"]


# -------------------------------------------------------------------- logo

def make_mark(src: Path, assets: Path) -> bool:
    """Write assets/mark.png and assets/favicon.png from the logo.

    The flex-pse logo stacks the app-icon mark over the wordmark and a tagline.
    Shrunk to nav or hero size the lettering becomes illegible, so the pages use
    only the mark and set the wordmark as live text. The mark is the first band
    of opaque rows from the top of the image.
    """
    try:
        from PIL import Image
    except ImportError:
        log("Pillow not installed; using the full logo as the mark")
        shutil.copy(src, assets / "mark.png")
        return True
    im = Image.open(src).convert("RGBA")
    alpha = im.getchannel("A")
    w, h = im.size
    rows = [alpha.crop((0, y, w, y + 1)).getbbox() is not None for y in range(h)]
    top = rows.index(True) if True in rows else 0
    bottom = next((y for y in range(top, h) if not rows[y]), h)
    mark = im.crop((0, top, w, bottom))
    mark = mark.crop(mark.getbbox())
    side = max(mark.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(mark, ((side - mark.width) // 2, (side - mark.height) // 2))
    square.resize((256, 256), Image.LANCZOS).save(assets / "mark.png", optimize=True)
    square.resize((64, 64), Image.LANCZOS).save(assets / "favicon.png", optimize=True)
    return True


# ------------------------------------------------------------------- mount

def mount(src: Path, dest: Path) -> None:
    if not (src / "index.html").exists():
        sys.exit(f"build output {src} has no index.html")
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".buildinfo", ".doctrees"))


def inject(out: Path, mount_dir: str, snippet: str, exclude: list[str]) -> int:
    body = re.compile(r"(<body\b[^>]*>)", re.I)
    n = 0
    for f in (out / mount_dir).rglob("*.html"):
        rel = f.relative_to(out).as_posix()
        if any(fnmatch.fnmatch(rel, pat) for pat in exclude):
            continue
        html = f.read_text(encoding="utf-8", errors="surrogateescape")
        if NAV_MARK in html:
            continue
        new, k = body.subn(lambda m: m.group(1) + snippet, html, count=1)
        if k:
            f.write_text(new, encoding="utf-8", errors="surrogateescape")
            n += 1
    return n


# ------------------------------------------------------------------ verify

class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        for k, v in attrs:
            if k in ("href", "src", "xlink:href") and v:
                self.links.append(v)


def verify(out: Path, base: str, mounts: list[str]) -> list[str]:
    errors = [f"/{m}/index.html is missing" for m in mounts
              if not (out / m / "index.html").exists()]
    parser = _Links()
    parser.feed((out / "index.html").read_text(encoding="utf-8"))
    for link in parser.links:
        if re.match(r"^[a-z]+:|^//|^#", link):
            continue
        path = link.split("#")[0].split("?")[0]
        if path.startswith(base):
            path = path[len(base):]
        target = out / path.lstrip("/")
        if target.is_dir():
            target = target / "index.html"
        if not target.exists():
            errors.append(f"index.html links to {link}, which does not exist")
    return errors


# -------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=ROOT / "_site")
    ap.add_argument("--manifest", type=Path, default=ROOT / "sites.yml")
    ap.add_argument("--work", type=Path, default=ROOT / ".work")
    ap.add_argument("--local", action="append", default=[], metavar="ID=PATH",
                    help="use a local checkout instead of cloning")
    ap.add_argument("--prebuilt", action="append", default=[], metavar="ID=DIR",
                    help="mount an existing build output instead of building")
    ap.add_argument("--strict", action="store_true",
                    help="fail if the dependency graph has warnings")
    args = ap.parse_args()

    manifest = yaml.safe_load(args.manifest.read_text(encoding="utf-8"))
    cfg, sites, org = manifest["site"], manifest["sites"], manifest["org"]
    base = cfg.get("base_path", "/").rstrip("/") + "/"
    local, prebuilt = pairs(args.local, "--local"), pairs(args.prebuilt, "--prebuilt")
    unknown = (set(local) | set(prebuilt)) - {s["id"] for s in sites}
    if unknown:
        sys.exit(f"unknown site id(s): {', '.join(sorted(unknown))}")

    out, work = args.out.resolve(), args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    if out.exists():
        shutil.rmtree(out)
    (out / "assets").mkdir(parents=True)

    checkouts, shas = {}, {}
    for s in sites:
        checkouts[s["id"]], shas[s["id"]] = fetch(s, work, local)

    env = Environment(loader=FileSystemLoader(ROOT / "templates"),
                      autoescape=select_autoescape(["html", "j2"]))
    shutil.copy(ROOT / "static" / "site.css", out / "assets" / "site.css")
    logo = cfg.get("logo")
    has_logo = False
    if logo and (checkouts[logo["site"]] / logo["path"]).exists():
        has_logo = make_mark(checkouts[logo["site"]] / logo["path"], out / "assets")

    for s in sites:
        src = prebuilt.get(s["id"]) or build(s, checkouts[s["id"]], work)
        mount(src, out / s["mount"])
        snippet = env.get_template("nav.html.j2").render(
            base=base, sites=sites, current=s["id"], has_logo=has_logo, cfg=cfg,
            org=org, mark=NAV_MARK)
        n = inject(out, s["mount"], snippet.strip(), cfg.get("inject_exclude", []))
        log(f"{s['id']}: mounted at {base}{s['mount']}/, nav added to {n} pages")

    repos = [depgraph.Repo(id=s["id"], title=s["title"], mount=s["mount"],
                           path=checkouts[s["id"]], github=s["repo"]) for s in sites]
    graph = depgraph.analyze(repos, org)
    for c in graph.checks:
        log(f"graph {c['level']}: {c['text']}")
    (out / "depgraph.json").write_text(json.dumps(graph.to_json(), indent=2) + "\n")

    html = env.get_template("index.html.j2").render(
        cfg=cfg, base=base, org=org, sites=sites, shas=shas, has_logo=has_logo,
        versions=graph.versions, graph=graph,
        flow=depgraph.flow(graph, cfg.get("foundations", {}),
                           {s["id"]: s.get("modules", {}) for s in sites}),
        site_by_id={s["id"]: s for s in sites},
        built=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    (out / "index.html").write_text(html, encoding="utf-8")
    (out / ".nojekyll").touch()

    errors = verify(out, base, [s["mount"] for s in sites])
    if args.strict:
        errors += [f"graph: {c['text']}" for c in graph.checks if c["level"] == "warn"]
    for e in errors:
        log(f"ERROR {e}")
    log(f"done: {out}" if not errors else f"{len(errors)} error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
