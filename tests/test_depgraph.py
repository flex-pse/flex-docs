"""Tests for the dependency graph and flowchart tiers."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import depgraph  # noqa: E402


def make_repo(root: Path, name: str, pyproject: str, files: dict[str, str]) -> Path:
    path = root / name
    path.mkdir()
    (path / "pyproject.toml").write_text(pyproject)
    for rel, body in files.items():
        f = path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
    return path


def two_repos(tmp_path, dep_line, example_code="import corepkg\n"):
    core = make_repo(tmp_path, "core", """
[project]
name = "core-lib"
version = "1.2.0"
dependencies = ["pyomo==6.10.1", "pandas>=2"]
[project.optional-dependencies]
solvers = ["highspy"]
dev = ["pytest"]
""", {
        "src/corepkg/__init__.py": "",
        "src/coreops/__init__.py": "from corepkg import x\n",
        "src/core_lib.egg-info/PKG-INFO": "",              # not a package
        "tests/test_x.py": "import examplespkg\n",   # tests are ignored
    })
    ex = make_repo(tmp_path, "ex", f"""
[project]
name = "examples"
dependencies = [{dep_line}]
[tool.setuptools]
packages = ["examples_pkg"]
""", {"examples_pkg/__init__.py": example_code})
    return [
        depgraph.Repo("core-lib", "Core", "core-lib", core, "acme/core-lib"),
        depgraph.Repo("examples", "Examples", "examples", ex, "acme/examples"),
    ]


def test_declared_and_observed_edge(tmp_path):
    repos = two_repos(tmp_path, '"core-lib>=1"')
    g = depgraph.analyze(repos, "acme")
    edge = next(e for e in g.repo_edges if e["dst"] == "core-lib")
    assert edge["declared"] and edge["observed"] == ["corepkg"]
    assert "pytest" not in g.externals                 # dev extra skipped
    assert g.externals["pyomo"] == "==6.10.1"
    assert not [c for c in g.checks if c["level"] == "warn"]
    # internal package edge, tests excluded
    assert {"src": "core-lib:coreops", "dst": "core-lib:corepkg", "count": 1} in g.pkg_edges


def test_git_url_under_old_name_warns(tmp_path):
    repos = two_repos(tmp_path, '"core-lib @ git+https://github.com/acme/CoreLib.git@main"')
    g = depgraph.analyze(repos, "acme")
    edge = next(e for e in g.repo_edges if e["dst"] == "core-lib")
    assert edge["spec"] == "@main"
    assert any("CoreLib" in c["text"] for c in g.checks)


def test_undeclared_import_warns(tmp_path):
    repos = two_repos(tmp_path, '"numpy"')
    g = depgraph.analyze(repos, "acme")
    edge = next(e for e in g.repo_edges if e["dst"] == "core-lib")
    assert not edge["declared"]
    assert any(c["level"] == "warn" and "does not declare" in c["text"] for c in g.checks)


def test_flow_tiers_and_foundations(tmp_path):
    repos = two_repos(tmp_path, '"core-lib>=1"')
    g = depgraph.analyze(repos, "acme")
    f = depgraph.flow(g, {"pyomo": "Pyomo", "highspy": "HiGHS", "gurobipy": "Gurobi"}, {})
    assert f["columns"] == [["examples"], ["core-lib"]]
    assert f["links"][0]["detail"] == ["corepkg"]
    assert [x["label"] for x in f["foundations"]] == ["Pyomo", "HiGHS"]   # gurobipy not used
    assert f["more"] == 1                                                  # pandas
    assert [m["pkg"] for m in f["modules"]["core-lib"]] == ["coreops", "corepkg"]
    assert f["modules"]["examples"] == []


def test_cycle_does_not_recurse_forever(tmp_path):
    repos = two_repos(tmp_path, '"core-lib"')
    (repos[0].path / "src/corepkg/__init__.py").write_text("import examples_pkg\n")
    g = depgraph.analyze(repos, "acme")
    f = depgraph.flow(g, {}, {})
    assert sorted(sum(f["columns"], [])) == ["core-lib", "examples"]
