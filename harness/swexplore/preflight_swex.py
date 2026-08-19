#!/usr/bin/env python3
"""The money gate for SWE-Explore. `harness/locbench/preflight.py` adapted, not
forked — the way `triage_swex.py` adapts `triage.py`.

CLAUDE.md's one rule is that every gate runs before the expensive thing, and
until now swexplore had no gate at all: the README pointed at locbench's
preflight, which knows nothing about these arms, and `sg_arms`'s own
`tools_lines_track_descv9()` documented itself as "Run by preflight.py" while
being called by nothing. The §16.10 campaign spent $361 measuring a tool whose
searches returned nothing 47% of the time. These checks are what would have
caught that, plus the ones the repo split broke.

    python3 harness/swexplore/preflight_swex.py
    python3 harness/swexplore/preflight_swex.py --arms cc,cc-gorp --model claude-sonnet-4-5-20250929

Exits nonzero with a list. `--probe-model` is the only check that spends, and
it spends cents.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "harness"))
from common import gorp_repo as common  # noqa: E402

HERE = Path(__file__).parent
DATA = common.DATA / "swexplore"
UPSTREAM = DATA / "upstream"
SHIM = common.BENCH / "harness" / "common" / "shim.py"
# gorp's own fixture corpus, the same one locbench's preflight searches.
FIXTURE = common.REPO / "tests" / "corpus"

# The registered vendored delta: one patched file, three copied in.
EXPECTED_DELTA = {"M eval_runner.py", "?? _sg_repos.py",
                  "?? explorers/sg_arms.py", "?? explorers/sg_static.py"}


def _run(argv, **kw):
    return subprocess.run(argv, capture_output=True, text=True, timeout=120, **kw)


def _arms_module():
    """Import the DEPLOYED copy, not the checked-in one.

    They are supposed to be byte-identical — fetch.sh copies one to the other —
    but the deployed file is what a campaign actually runs, so that is the one
    a gate must inspect. check_vendoring below asserts they match.
    """
    sys.path.insert(0, str(UPSTREAM))
    import explorers.sg_arms as m  # noqa: E402
    return m


# ---------------------------------------------------------------- 1. binary
def check_binary():
    """The binary exists, is not stale relative to the checkout, and is clean.

    §36 found the release binary two commits behind `../gorp` HEAD, including a
    CLI behaviour change. Nothing compared them, and a campaign measuring a
    binary nobody can name afterwards is not a measurement.
    """
    fails = []
    if not common.BIN.exists():
        return [f"no gorp binary at {common.BIN} — (cd {common.REPO} && cargo build --release)"]
    ver = _run([str(common.BIN), "--version"]).stdout
    commit = ""
    for line in ver.splitlines():
        if line.startswith("commit:"):
            commit = line.split(":", 1)[1].strip()
    if "(dirty)" in commit:
        fails.append(f"binary built from a dirty tree ({commit}) — it corresponds to no commit")
    commit = commit.replace("(dirty)", "").strip()
    head = _run(["git", "-C", str(common.REPO), "rev-parse", "--short=7", "HEAD"]).stdout.strip()
    if commit and head and not (commit.startswith(head) or head.startswith(commit)):
        fails.append(
            f"binary is stale: built at {commit}, but {common.REPO} HEAD is {head} — "
            f"rebuild, or the campaign measures neither")
    print(f"  binary {commit or '?'} vs HEAD {head or '?'}")
    return fails


# ------------------------------------------------------------- 2. vendoring
def check_vendoring():
    """Upstream is at its pinned SHA with exactly the registered delta, and the
    deployed overlay is byte-identical to `patches/`."""
    fails = []
    if not (UPSTREAM / ".git").is_dir():
        return [f"no vendored upstream at {UPSTREAM} — run harness/swexplore/fetch.sh"]
    pin = ""
    for line in (HERE / "fetch.sh").read_text().splitlines():
        if line.startswith("UPSTREAM_SHA="):
            pin = line.split("=", 1)[1].strip()
    head = _run(["git", "-C", str(UPSTREAM), "rev-parse", "HEAD"]).stdout.strip()
    if pin and head != pin:
        fails.append(f"vendored upstream at {head[:12]}, pinned at {pin[:12]}")
    delta = {l.strip() for l in _run(
        ["git", "-C", str(UPSTREAM), "status", "--porcelain"]).stdout.splitlines() if l.strip()}
    if delta != EXPECTED_DELTA:
        fails.append(f"vendored delta is {sorted(delta)}, registered is {sorted(EXPECTED_DELTA)}")
    for name, dst in (("sg_arms.py", UPSTREAM / "explorers" / "sg_arms.py"),
                      ("sg_static.py", UPSTREAM / "explorers" / "sg_static.py"),
                      ("_sg_repos.py", UPSTREAM / "_sg_repos.py")):
        src = HERE / "patches" / name
        if not dst.exists() or src.read_bytes() != dst.read_bytes():
            fails.append(f"deployed {name} differs from patches/{name} — re-run fetch.sh")
    print(f"  upstream@{head[:7]} delta={len(delta)} files")
    return fails


# ---------------------------------------------------------------- 3. paths
def check_paths(m):
    """Every cross-repo path the arms resolve actually exists.

    This is the §36 split check. `BENCH_ROOT` was computed one level too high
    after the repo split, which made `SHIM` a path that does not exist — so
    every shim wrapper would have exec'd a missing file and every search in
    every arm would have failed. Rows still get written; the arm reads as a
    clean null. Nothing else in the harness looks at these.
    """
    fails = []
    for label, p in (("BENCH_ROOT", m.BENCH_ROOT), ("SHIM", m.SHIM),
                     ("LOCBENCH", m.LOCBENCH), ("GORP_REPO", m.GORP_REPO),
                     ("GORP_BIN", m.GORP_BIN)):
        if not Path(p).exists():
            fails.append(f"{label} resolves to {p}, which does not exist")
    if m.BENCH_ROOT != common.BENCH:
        fails.append(f"sg_arms BENCH_ROOT ({m.BENCH_ROOT}) != gorp_repo BENCH ({common.BENCH})")
    print(f"  BENCH_ROOT={m.BENCH_ROOT}")
    return fails


# ------------------------------------------------------------ 4. registries
def check_registries(m, arms):
    """All six arm registries agree.

    Adding `sub-sgb` to one registry at a time cost three consecutive fix
    commits (42c192a, d2e2b90, 27f3c08) — each one finding the next copy the
    last had missed. A campaign whose arm is unknown to `analyze.py` dies after
    the money is spent: the gate passes, the numbers never print.
    """
    fails = []
    # By explicit path, NOT by name: triage_swex puts harness/locbench on
    # sys.path[0] when it loads, and locbench has a viewer.py too — so a bare
    # `import viewer` silently audits the wrong file's registry.
    import importlib.util

    def _load(stem):
        spec = importlib.util.spec_from_file_location(
            f"_pf_{stem}", HERE / f"{stem}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    registries = {"sg_arms.ARMS": set(m.ARMS)}
    for stem in ("triage_swex", "analyze", "viewer"):
        registries[f"{stem}.ALL_ARM_TOOL"] = set(_load(stem).ALL_ARM_TOOL)
    patch = (HERE / "patches" / "0001-eval_runner-arms-cost-rolling.patch").read_text()
    for arm in sorted(arms):
        for name, reg in registries.items():
            if arm not in reg:
                fails.append(f"{arm} missing from {name}")
        # The runner's own two registries live inside the patch text.
        if f'"{arm}": lambda rec: _sg_arm_method(rec, "{arm}")' not in patch:
            fails.append(f"{arm} missing from eval_runner METHOD_MAP (the patch)")
        if f'"{arm}"' not in patch.split("SG_ARMS = {")[1].split("}")[0]:
            fails.append(f"{arm} missing from eval_runner SG_ARMS (the patch)")
    print(f"  {len(registries) + 2} registries x {len(arms)} arms")
    return fails


# ------------------------------------------------------------------ 5. arms
def check_arm_wiring(m, arms):
    """Each arm's tool has a shim, an allowlist entry and a block message, and
    the arm's own tool is the only one bound."""
    fails = []
    for arm in sorted(arms):
        if arm not in m.ARMS:
            continue                      # already reported by check_registries
        tools, allowed, sysline = m.ARMS[arm]
        tool = m.ARM_TOOL.get(arm)
        if tool is None:
            # Two kinds of engine-less arm. The pristine control (cc) must
            # carry nothing — it is the calibration anchor against the
            # published row. A grep-open shell control (§37.2 cc-bash) may
            # allowlist shell grep and nothing else, and still carries no
            # description: its purity IS the decomposition it exists for.
            if arm in getattr(m, "GREP_OPEN_ARMS", ()):
                if sysline:
                    fails.append(f"{arm}: shell control carries a description line")
                if [a for a in allowed if not a.startswith("Bash(grep")]:
                    fails.append(f"{arm}: shell control allowlists more than grep")
            elif allowed or sysline:
                fails.append(f"{arm}: control arm carries an allowlist or a tool line")
            continue
        if tool not in m.SEARCH_TOOLS:
            fails.append(f"{arm}: tool {tool!r} is not in SEARCH_TOOLS, so it gets no shim "
                         f"and would reach the real binary on PATH")
        if not any(a.startswith(f"Bash({tool} ") for a in allowed):
            fails.append(f"{arm}: no Bash({tool} *) in --allowedTools")
        if f"`{tool}`" not in sysline:
            fails.append(f"{arm}: the appended tool line never names `{tool}`")
        if m.ARM_CLAUSE.get(arm) and f"`{tool}`" not in m.ARM_CLAUSE[arm]:
            fails.append(f"{arm}: the amended prompt clause never names `{tool}`")
        if "Bash" not in tools:
            fails.append(f"{arm}: has a shell tool but Bash is not in --tools")
    print(f"  {len(arms)} arms wired")
    return fails


# ---------------------------------------------------- 6. descriptions
def check_descriptions(m):
    """The orphaned gate, finally called — plus the v11->v12 rename assertion.

    §19.9 is the cautionary case: desc-v9 changed the tool's name *and* a
    clause, and could attribute neither. v12 must be v11 and the rename, with
    nothing else moved, and that has to be mechanically true rather than
    carefully proofread.
    """
    import re
    fails = []
    track = m.tools_lines_track_descv9()
    if not track["ok"]:
        fails.append(f"the sg line no longer carries desc-v9's clauses: {track['missing']}")
    if m.SG_LINE_V12 != re.sub(r"\bsg\b", "gorp", m.SG_LINE_V11):
        fails.append("SG_LINE_V12 is not SG_LINE_V11 with the tool renamed — "
                     "two variables moved and neither can be attributed")
    if re.search(r"\bsg\b", m.SG_LINE_V12):
        fails.append("SG_LINE_V12 still names `sg`")
    print(f"  desc-v9 clauses intact; v12 == v11 + rename")
    return fails


# ------------------------------------------------------- 7. search shapes
def check_shapes(query):
    """Real invocation shapes return real hits.

    THE §16.10 check: that campaign spent $361 on an arm whose searches came
    back empty 47% of the time and nothing noticed. Three scopes x three modes
    against gorp's own fixture corpus; a zero-hit ranked search is a failure,
    not a curiosity.
    """
    fails = []
    if not FIXTURE.is_dir():
        return [f"no fixture corpus at {FIXTURE}"]
    scopes = [FIXTURE]
    subs = sorted(p for p in FIXTURE.iterdir() if p.is_dir())
    files = sorted(p for p in FIXTURE.rglob("*.py"))[:1] or sorted(
        p for p in FIXTURE.rglob("*") if p.is_file())[:1]
    if subs:
        scopes.append(subs[0])
    if files:
        scopes.append(files[0])
    env = {**os.environ, "GORP_NO_HINTS": "1"}
    n = 0
    for scope in scopes:
        for mode in (["--json"], ["--json", "-k", "20"]):
            p = _run([str(common.BIN), *mode, query, str(scope)], env=env)
            n += 1
            if p.returncode == 2:
                fails.append(f"gorp exited 2 on {' '.join(mode)} {scope.name}: {p.stderr[:120]}")
                continue
            hits, bad = [], False
            for line in p.stdout.splitlines():
                if not line.strip():
                    continue
                try:
                    hits.append(json.loads(line))
                except json.JSONDecodeError:
                    bad = True
            if bad:
                fails.append(f"gorp emitted non-JSON for {' '.join(mode)} {scope.name}")
                continue
            if not hits:
                fails.append(f"ranked search returned NOTHING for {' '.join(mode)} {scope.name} "
                             f"— this is the §16.10 shape")
            elif any(not h.get("path") for h in hits if isinstance(h, dict)):
                fails.append(f"a hit carries no path for {' '.join(mode)} {scope.name}")
    print(f"  {n} invocation shapes, all non-empty" if not fails else f"  {n} shapes, {len(fails)} bad")
    return fails


# --------------------------------------------------------------- 8. shims
def check_shims(m):
    """Materialise the real shims and assert both halves of the contract:
    a bound tool passes bytes through, an unbound one blocks on BOTH streams.

    stdout as well as stderr, because stderr-only meant agents piping
    `2>/dev/null` saw silence and read it as "no matches".
    """
    fails = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        bin_dir = td / "bin"
        bin_dir.mkdir()
        for tool in (*m.SEARCH_TOOLS, "grep", "egrep", "fgrep", "git"):
            w = bin_dir / tool
            w.write_text(f'#!/bin/sh\nexec /usr/bin/env python3 "{SHIM}" {tool} "$@"\n')
            w.chmod(0o755)
        base = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "LOCBENCH_SHIM_LOG": str(td / "log.jsonl"),
                "LOCBENCH_STDOUT_DIR": str(td / "out"),
                "GORP_NO_HINTS": "1"}
        # blocked
        for tool in ("grep", "git", "rg"):
            env = {k: v for k, v in base.items() if not k.startswith("LOCBENCH_REAL_")}
            env[f"LOCBENCH_BLOCKMSG_{tool.upper()}"] = f"{tool}: unavailable in this environment"
            p = _run([str(bin_dir / tool), "x"], env=env)
            if p.returncode != 2 or "unavailable" not in p.stdout or "unavailable" not in p.stderr:
                fails.append(f"blocked {tool}: rc={p.returncode} "
                             f"stdout={p.stdout[:40]!r} stderr={p.stderr[:40]!r}")
        # bound: shimmed output must be byte-identical to calling the binary
        env = {**base, "LOCBENCH_REAL_GORP": str(common.BIN)}
        direct = _run([str(common.BIN), "retry backoff", str(FIXTURE)],
                      env={**os.environ, "GORP_NO_HINTS": "1"})
        through = _run([str(bin_dir / "gorp"), "retry backoff", str(FIXTURE)], env=env)
        if direct.stdout != through.stdout:
            fails.append("shimmed gorp stdout differs from the direct call — not byte-exact")
        if not (td / "log.jsonl").exists():
            fails.append("the shim wrote no log")
    print("  shims block on both streams; bound output byte-exact")
    return fails


# ------------------------------------------------------------ 9. the model
def check_model(model):
    """One ~1-token call, to prove the CLI accepts this model id and resolves
    to it. Costs cents; the alternative is finding out 300 sessions in.

    Pinning matters because the alias drifted silently once already: s27..s33
    all recorded `"model": "sonnet"` and all in fact ran claude-sonnet-5.
    """
    p = subprocess.run(
        ["claude", "-p", "--output-format", "stream-json", "--verbose",
         "--model", model, "--strict-mcp-config", "--setting-sources", "",
         "--no-session-persistence", "--tools", ""],
        input="say ok", capture_output=True, text=True, timeout=180)
    if p.returncode != 0:
        return [f"claude rejected --model {model}: {(p.stderr or p.stdout)[:300]}"]
    resolved = None
    for line in p.stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            resolved = ev.get("model")
    if resolved is None:
        return ["claude produced no init event — cannot verify the model resolution"]
    print(f"  --model {model} -> {resolved}")
    if resolved != model:
        return [f"--model {model} resolved to {resolved} — pin the resolved id instead"]
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default="cc,cc-gorp")
    ap.add_argument("--model", default="claude-sonnet-4-5-20250929")
    ap.add_argument("--query", default="compute the retry backoff delay")
    ap.add_argument("--skip-model", action="store_true",
                    help="skip the one check that spends money")
    a = ap.parse_args()
    arms = {x.strip() for x in a.arms.split(",") if x.strip()}

    m = _arms_module()
    steps = [
        ("binary", lambda: check_binary()),
        ("vendoring", lambda: check_vendoring()),
        ("paths", lambda: check_paths(m)),
        ("registries", lambda: check_registries(m, arms)),
        ("arm wiring", lambda: check_arm_wiring(m, arms)),
        ("descriptions", lambda: check_descriptions(m)),
        ("search shapes", lambda: check_shapes(a.query)),
        ("shims", lambda: check_shims(m)),
    ]
    if not a.skip_model:
        steps.append(("model", lambda: check_model(a.model)))

    failures = []
    for i, (name, fn) in enumerate(steps, 1):
        print(f"[{i}/{len(steps)}] {name}")
        try:
            failures += [f"{name}: {f}" for f in fn()]
        except Exception as e:  # noqa: BLE001
            failures.append(f"{name}: check itself raised — {type(e).__name__}: {e}")
    print()
    if failures:
        print(f"PREFLIGHT FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("preflight passed — safe to spend")
    return 0


if __name__ == "__main__":
    sys.exit(main())
