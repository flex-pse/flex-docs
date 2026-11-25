# flex-docs

One documentation site for every public flex-pse repository, served at
<https://flex-pse.github.io/flex-docs/>.

Each repo keeps its docs in its own tree and builds them with its own tool
(Sphinx for `flex-pse`, a marimo/WebAssembly site for `flex-pse-examples`).
This repo clones them, runs those builds, mounts each result under its old URL,
adds a shared nav bar, and generates a landing page with a flowchart of how the
repos depend on each other.

| Path | Served from |
| --- | --- |
| `/flex-docs/` | landing page and flowchart (this repo) |
| `/flex-docs/flex-pse/` | `flex-pse/flex-pse` → `docs/` (Sphinx) |
| `/flex-docs/flex-pse-examples/` | `flex-pse/flex-pse-examples` → `tools/site/build.py` |
| `/flex-docs/depgraph.json` | full dependency data behind the flowchart |

## URLs

Because this repo is named `flex-docs` (not `flex-pse.github.io`), GitHub serves
it as a project site under `/flex-docs/`. The docs therefore move:

| Old | New |
| --- | --- |
| `flex-pse.github.io/flex-pse/` | `flex-pse.github.io/flex-docs/flex-pse/` |
| `flex-pse.github.io/flex-pse-examples/` | `flex-pse.github.io/flex-docs/flex-pse-examples/` |

Update `project.urls.Documentation` in each repo's `pyproject.toml` and any README
links. To keep old links working, leave each repo's Pages on and have it deploy
a one-file redirect instead of its full docs (`templates/redirect.html`, with
`TARGET` replaced).

To serve from the root instead, give the site a custom domain (for example
`docs.flex-pse.org`): add `static/CNAME`, set the domain in Settings → Pages,
and set `base_path: /` in `sites.yml`.

## The flowchart

Nobody edits the flowchart by hand. Every build regenerates it from two sources:

* **Declared** dependencies in each repo's `pyproject.toml` (runtime
  dependencies and extras; `dev`, `docs` and `test` extras are ignored).
* **Observed** imports, found by parsing each repo's Python files (tests, docs
  and build output are skipped).

Repos are laid out in tiers from left to right: what you start with, what it
builds on, and finally the open-source foundations. The "builds on" arrow lists
the packages actually imported across that boundary.

Only the display text is curated, in `sites.yml`:

* `role`, `icon`, `blurb` and `highlights` for each card
* `modules`: friendly names for a repo's packages (shown when it has 2 or more)
* `foundations`: the external packages to show, with friendly labels. Only
  packages that some repo actually declares appear. The rest are counted as
  "+N more".

The **For maintainers** section of the landing page lists mismatches: an import
with no declared dependency (the arrow turns amber and dashed), a declared repo
that is never imported, or a git URL that still uses a repo's old name. Pass
`--strict` to make warnings fail the build.

## Build locally

```bash
pip install -r requirements.txt
python tools/build_site.py --out _site      # clones and builds everything
python -m http.server --directory _site 8000
```

When you only want to work on the landing page, reuse local checkouts and skip
the heavy builds:

```bash
python tools/build_site.py \
  --local flex-pse=../flex-pse --local flex-pse-examples=../flex-pse-examples \
  --prebuilt flex-pse=../flex-pse/docs/_build \
  --prebuilt flex-pse-examples=../flex-pse-examples/_site
```

Tests: `pytest tests`.

## Enrolling a repo

1. **Add it to `sites.yml`** with `id`, `title`, `blurb`, `role`, `repo`,
   `ref`, `mount`, and a `build` block (`setup` and `build` commands, `out`
   folder). Its build output must contain `index.html`, and its internal links
   must be relative (both current repos already enforce this).
2. **Optional, recommended: move the build command into the repo**, so it
   lives next to the code it builds and can't drift from it:

   ```toml
   # pyproject.toml
   [tool.flexdocs]
   setup = ['pip install -e ".[dev,docs]"']
   build = ["sphinx-build -W --keep-going -b html docs docs/_build"]
   out   = "docs/_build"
   ```

   When this table exists it overrides the manifest.
3. **Trigger rebuilds.** Copy `templates/notify-docs.yml` to the repo's
   `.github/workflows/` and add a `DOCS_DISPATCH_TOKEN` secret: a fine-grained
   token with *Contents: read and write* on `flex-pse/flex-docs` only.
   A weekly scheduled build catches anything a dispatch misses.
4. **Stop the repo publishing its full docs.** Keep its docs build on pull
   requests (that's how contributors catch broken docs). Replace what its
   `deploy` job uploads with the redirect page (see "URLs"), or remove the job
   and turn off Pages if old links don't matter.

## Switching over from per-repo Pages (one time)

1. Create `flex-pse/flex-docs` from this folder. In Settings → Pages,
   set Source to *GitHub Actions*. Run the `site` workflow and check
   `https://flex-pse.github.io/flex-docs/`.
2. In `flex-pse`: make the `deploy` job in `.github/workflows/docs.yml` publish
   the redirect page, add `notify-docs.yml`, and point
   `project.urls.Documentation` at the new URL.
3. In `flex-pse-examples`: the same in `.github/workflows/pages.yml`, and fix its
   flex-pse dependency URL (`flexPSE` → `flex-pse`).
4. Re-run the `site` workflow and check the old URLs redirect.

## Layout

```
sites.yml                  manifest: repos, build commands, display text
tools/build_site.py        fetch → build → mount → nav → graph → landing → verify
tools/depgraph.py          dependency analysis and flowchart tiers
templates/index.html.j2    landing page
templates/nav.html.j2      shared nav bar injected into every docs page
templates/notify-docs.yml  workflow to copy into enrolled repos
templates/redirect.html    redirect page for old per-repo URLs
static/site.css            shared styles (flex-pse design tokens)
.github/workflows/site.yml build, test, deploy
```
