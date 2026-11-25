"""Derive the repository and package dependency graph from source checkouts.

Two kinds of evidence are combined, so the graph stays correct without anyone
editing it by hand:

* **Declared**: the requirements in each repo's ``pyproject.toml`` (runtime
  dependencies and optional extras; dev/docs/test extras are ignored).
* **Observed**: ``import`` statements found by parsing the repo's Python files.
  Tests, docs and build output are skipped.

A declared edge with no matching import, or an import with no declared
dependency, is reported as a check on the landing page.

``flow()`` arranges the result into tiers for the landing-page flowchart.
"""

from __future__ import annotations

import ast
import re
import tomllib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "_build", "_site", "build", "dist",
    "tests", "test", "docs", "__pycache__", ".tox", ".mypy_cache",
}
SKIP_EXTRAS = {"dev", "docs", "test", "tests", "testing", "lint", "typing", "all"}
MAX_PY_BYTES = 2_000_000

REQ = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(?:@\s*(\S+))?\s*([^;@]*)"
)
GH_URL = re.compile(
    r"github\.com[/:]([^/\s]+)/([^/@#\s]+?)(?:\.git)?(?:@([^#\s]+))?(?:#.*)?$"
)


def norm(name: str) -> str:
    """Normalise a distribution or repo name so ``flex-pse``, ``flexPSE`` and
    ``flex_pse`` all compare equal."""
    return re.sub(r"[-_.]+", "", name).lower()


@dataclass
class Repo:
    id: str            # short id, also the manifest id
    title: str
    mount: str         # URL path the docs are served under
    path: Path         # local checkout
    github: str        # owner/name


@dataclass
class Graph:
    repos: list[Repo]
    repo_edges: list[dict] = field(default_factory=list)
    externals: dict[str, str] = field(default_factory=dict)   # name -> spec
    packages: dict[str, list[str]] = field(default_factory=dict)  # repo id -> pkgs
    pkg_edges: list[dict] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    versions: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "repos": [r.id for r in self.repos],
            "repo_edges": self.repo_edges,
            "externals": self.externals,
            "packages": self.packages,
            "package_edges": self.pkg_edges,
            "checks": self.checks,
        }


def read_pyproject(path: Path) -> dict:
    f = path / "pyproject.toml"
    return tomllib.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def _packages(path: Path, pp: dict) -> list[str]:
    pkgs: set[str] = set()
    src = path / "src"
    if src.is_dir():
        pkgs |= {p.name for p in src.iterdir()
                 if p.is_dir() and p.name.isidentifier() and not p.name.startswith("_")}
    listed = pp.get("tool", {}).get("setuptools", {}).get("packages")
    if isinstance(listed, list):
        pkgs |= {p.split(".")[0] for p in listed}
    return sorted(pkgs)


def _python_files(root: Path):
    for p in root.rglob("*.py"):
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts[:-1]):
            continue
        try:
            if p.stat().st_size <= MAX_PY_BYTES:
                yield p
        except OSError:
            continue


def _imports(file: Path) -> set[str]:
    try:
        tree = ast.parse(file.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, ValueError):
        return set()
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.add(node.module.split(".")[0])
    return out


def _requirements(pp: dict):
    """Yield (kind, requirement string) for runtime deps and non-dev extras."""
    proj = pp.get("project", {})
    for req in proj.get("dependencies", []):
        yield "runtime", req
    for extra, reqs in proj.get("optional-dependencies", {}).items():
        if extra.lower() in SKIP_EXTRAS:
            continue
        for req in reqs:
            yield f"extra:{extra}", req


def analyze(repos: list[Repo], org: str) -> Graph:
    g = Graph(repos=repos)
    meta = {r.id: read_pyproject(r.path) for r in repos}

    names: dict[str, str] = {}
    for r in repos:
        names[norm(r.id)] = r.id
        names[norm(meta[r.id].get("project", {}).get("name", r.id))] = r.id
        g.versions[r.id] = meta[r.id].get("project", {}).get("version", "")
        g.packages[r.id] = _packages(r.path, meta[r.id])

    pkg_owner = {p: rid for rid, ps in g.packages.items() for p in ps}

    # ---- declared -------------------------------------------------------
    edges: dict[tuple[str, str], dict] = {}
    for r in repos:
        for kind, req in _requirements(meta[r.id]):
            m = REQ.match(req)
            if not m:
                continue
            name, url, spec = m.group(1), m.group(3) or "", (m.group(4) or "").strip()
            target = names.get(norm(name))
            gh = GH_URL.search(url) if url else None
            if gh and gh.group(1).lower() == org.lower():
                target = target or names.get(norm(gh.group(2)))
                if target == r.id:
                    continue
                if target and gh.group(2) != canonical_of(repos, target):
                    g.checks.append({
                        "level": "warn",
                        "text": (f"{r.id} depends on {gh.group(1)}/{gh.group(2)}, but the "
                                 f"repository is named {canonical_of(repos, target)}. GitHub "
                                 "redirects the old name today; update the URL so it keeps "
                                 "working if the name is ever reused."),
                    })
                spec = f"@{gh.group(3)}" if gh.group(3) else spec
            if target == r.id:
                continue          # extras that refer back to the package itself
            if target:
                e = edges.setdefault((r.id, target), {
                    "src": r.id, "dst": target, "kind": kind, "spec": spec,
                    "declared": True, "observed": []})
                if kind == "runtime":
                    e["kind"] = "runtime"
            else:
                key = name.lower()
                g.externals.setdefault(key, spec)
                e = edges.setdefault((r.id, key), {
                    "src": r.id, "dst": key, "kind": kind, "spec": spec,
                    "declared": True, "observed": [], "external": True})
                if kind == "runtime":
                    e["kind"] = "runtime"

    # ---- observed -------------------------------------------------------
    pkg_counts: Counter[tuple[str, str]] = Counter()
    for r in repos:
        primary = next((p for p in g.packages[r.id] if norm(p) == norm(r.id)),
                       g.packages[r.id][0] if g.packages[r.id] else r.id)
        for f in _python_files(r.path):
            rel = f.relative_to(r.path).parts
            own = rel[1] if rel[0] == "src" and len(rel) > 2 else primary
            for mod in _imports(f):
                owner = pkg_owner.get(mod)
                if owner is None or mod == own:
                    continue
                pkg_counts[(f"{r.id}:{own}", f"{owner}:{mod}")] += 1
                if owner != r.id:
                    e = edges.setdefault((r.id, owner), {
                        "src": r.id, "dst": owner, "kind": "observed", "spec": "",
                        "declared": False, "observed": []})
                    if mod not in e["observed"]:
                        e["observed"].append(mod)

    g.repo_edges = sorted(edges.values(), key=lambda e: (e["src"], e["dst"]))
    for e in g.repo_edges:
        e["observed"].sort()
    g.pkg_edges = [{"src": s, "dst": d, "count": n}
                   for (s, d), n in sorted(pkg_counts.items())]

    # ---- reconcile ------------------------------------------------------
    for e in g.repo_edges:
        if e.get("external"):
            continue
        if e["observed"] and not e["declared"]:
            g.checks.append({"level": "warn", "text": (
                f"{e['src']} imports {', '.join(e['observed'])} from {e['dst']} but does "
                "not declare it in pyproject.toml.")})
        elif e["declared"] and not e["observed"]:
            g.checks.append({"level": "info", "text": (
                f"{e['src']} declares {e['dst']} but no non-test import of it was found "
                "(it may be used only through the command line, tests or notebooks "
                "outside the scanned folders).")})
    return g


def canonical_of(repos: list[Repo], rid: str) -> str:
    return next(r.github.split("/")[-1] for r in repos if r.id == rid)


# ------------------------------------------------------------- flowchart

def flow(g: Graph, foundations: dict[str, str], module_labels: dict[str, dict]) -> dict:
    """Arrange the graph as tiers for the landing-page flowchart.

    Repos that use others sit on the left, the repos they build on to the right,
    and a final "Foundations" tier lists the external packages named in
    ``foundations`` (in that order) that any repo declares.
    """
    ids = [r.id for r in g.repos]
    deps: dict[str, set[str]] = {rid: set() for rid in ids}
    for e in g.repo_edges:
        if not e.get("external"):
            deps[e["src"]].add(e["dst"])

    memo: dict[str, int] = {}

    def depth(rid: str, seen: tuple = ()) -> int:
        if rid in memo:
            return memo[rid]
        if rid in seen:                      # a cycle: stop rather than recurse
            return 0
        memo[rid] = 1 + max((depth(d, seen + (rid,)) for d in deps[rid]), default=-1)
        return memo[rid]

    top = max((depth(r) for r in ids), default=0)
    columns: list[list[str]] = [[] for _ in range(top + 1)]
    for rid in ids:
        columns[top - depth(rid)].append(rid)

    links = []
    for left, right in zip(columns, columns[1:]):
        es = [e for e in g.repo_edges if not e.get("external")
              and e["src"] in left and e["dst"] in right]
        links.append({
            "label": "builds on",
            "detail": sorted({p for e in es for p in e["observed"]}),
            "warn": any(not e["declared"] for e in es),
        })

    col_of = {rid: i for i, col in enumerate(columns) for rid in col}
    also = {rid: sorted(d for d in deps[rid] if col_of[d] != col_of[rid] + 1) for rid in ids}

    used = {e["dst"] for e in g.repo_edges if e.get("external")}
    found = [{"label": label, "pkg": pkg, "spec": g.externals.get(pkg, "")}
             for pkg, label in foundations.items() if pkg.lower() in used]

    modules = {}
    for rid in ids:
        pk = g.packages.get(rid, [])
        if len(pk) < 2:                      # a single package says nothing new
            modules[rid] = []
            continue
        labels = module_labels.get(rid) or {}
        ordered = [p for p in labels if p in pk] + [p for p in pk if p not in labels]
        modules[rid] = [{"pkg": p, "label": labels.get(p, p)} for p in ordered]

    return {
        "columns": columns,
        "links": links,
        "also": also,
        "foundations": found,
        "more": len(used) - len(found),
        "modules": modules,
    }
