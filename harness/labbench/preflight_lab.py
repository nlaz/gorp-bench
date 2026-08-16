#!/usr/bin/env python3
"""Does the LAB harness actually deliver its treatment? Run before spending.

The §16.10 lesson transplanted to a legal corpus: the expensive thing here
is a 200-turn agent run plus one judge call per rubric criterion, so every
check runs first and costs no API calls. The checks that exist, exist
because a specific silent failure would otherwise read as a result:

  1. vendoring    the pin, the exact two-file delta, py_compile — a drifted
                  upstream is a different benchmark under the same name
  2. frame        reproduces from seed; upstream's own validate_task_config
                  accepts every frame task (via upstream's venv)
  3. corpus       manifest matches the pin, frame docroots covered, no
                  originals left behind, spot-check the naming round trip —
                  a hit that cites a missing file is a tool that lies
  4. shapes       gorp and rg against real converted docroots with canned
                  legal queries — §16.10 was 47% empty searches that nothing
                  noticed until the money was spent
  5. shim         blocks the unbound name on both streams, passes bytes
                  through when bound, logs a row
  6. arm wiring   per-arm import inside the vendored tree: lab-base's tool
                  list byte-equal to upstream, treatments exactly +1 tool,
                  descriptions still track desc-v9, prompt anchor present
  7. runtime      podman, uv, pandoc, key presence (existence only)

    python3 harness/labbench/preflight_lab.py
    python3 harness/labbench/preflight_lab.py --skip-podman --skip-keys --skip-corpus

CI runs the skip form: it has no podman, no keys, no data/.
"""

import argparse
import json
import os
import posixpath
import random
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import gorp_repo as common  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent / "patches"))
import lab_arms  # noqa: E402

HERE = Path(__file__).parent
DATA = common.DATA / "labbench"
UPSTREAM = DATA / "upstream"
UPSTREAM_SHA = "7be41d57fd5a6e97b5f246a029e810f83d09cd96"
RG_BIN = os.environ.get("RG_BIN", "/opt/homebrew/bin/rg")

# Canned legal queries for the shape check: phrases a lawyer-agent will
# actually issue, spanning the frame's practice areas. The gate is on the
# EMPTY share, not per-query: any one phrase may legitimately miss a given
# corpus, but a tool blind to most of these is §16.10 again.
LEGAL_QUERIES = [
    "indemnification cap", "termination for convenience",
    "change of control consent", "governing law and venue",
    "limitation of liability", "confidentiality obligations survive",
    "purchase price adjustment", "representations and warranties",
    "material adverse effect", "non-compete restrictive covenant",
    "assignment requires consent", "force majeure",
    "intellectual property ownership", "audit rights",
    "notice period for breach", "escrow release conditions",
    "employee benefit plans", "environmental compliance",
    "insurance coverage requirements", "dispute resolution arbitration",
]
MAX_EMPTY_SHARE = 0.10
RG_LITERALS = ["Agreement", "indemnif", "[Tt]ermination", "consent", "liability"]

FAILURES = []


def fail(check, detail):
    FAILURES.append((check, detail))
    print(f"  FAIL  {check}: {detail}")


def ok(check, detail=""):
    print(f"  ok    {check}{(' — ' + detail) if detail else ''}")


def run(argv, env_extra=None, cwd=None, timeout=120):
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run([str(a) for a in argv], capture_output=True,
                          env=env, cwd=cwd, timeout=timeout)


# ------------------------------------------------------------ 1. vendoring

def check_vendoring():
    head = run(["git", "-C", UPSTREAM, "rev-parse", "HEAD"]).stdout.decode().strip()
    if head != UPSTREAM_SHA:
        return fail("vendoring", f"upstream at {head[:7]}, pin is {UPSTREAM_SHA[:7]}")
    # -z: corpus filenames carry spaces, which newline-terminated porcelain
    # C-quotes — a quoted line never endswith(".md") and reads as drift.
    entries = [e for e in run(["git", "-C", UPSTREAM, "status",
                               "--porcelain", "-z"])
               .stdout.decode().split("\0") if e]
    porcelain = [e for e in entries
                 # untracked .md files under tasks/ are the corpus, not drift
                 if not (e.startswith("?? tasks/") and e.endswith(".md"))]
    overlays = ("lab_arms.py", "lab_cc_run.py", "lab_cc_judge.py")
    expect = {" M harness/run.py"} | {f"?? {f}" for f in overlays}
    got = set(porcelain)
    if got != expect:
        return fail("vendoring", f"unexpected delta: {sorted(got ^ expect)[:5]}")
    p = run([sys.executable, "-m", "py_compile", UPSTREAM / "harness/run.py",
             *(UPSTREAM / f for f in overlays)])
    if p.returncode != 0:
        return fail("vendoring", p.stderr.decode()[-200:])
    for f in overlays:
        if (UPSTREAM / f).read_bytes() != (HERE / "patches" / f).read_bytes():
            return fail("vendoring", f"deployed {f} != checked-in copy "
                                     f"(re-run fetch.sh)")
    ok("vendoring", f"upstream@{head[:7]} + {len(overlays)} files + 1 patch")


# ---------------------------------------------------------------- 2. frame

def check_frame():
    p = run([sys.executable, HERE / "lab_frame.py", "--check"])
    if p.returncode != 0:
        return fail("frame", p.stdout.decode()[-200:])
    frame = [json.loads(l) for l in (DATA / "frame.jsonl").read_text().splitlines()]
    # Upstream's own validator, in upstream's venv, over every frame task.
    prog = (
        "import json,sys; from pathlib import Path;"
        "from evaluation.run_eval import validate_task_config;"
        "tasks=json.loads(sys.argv[1]);"
        "[validate_task_config(config=json.loads((Path('tasks')/t/'task.json')"
        ".read_text()), task_path=Path('tasks')/t/'task.json') for t in tasks];"
        "print(len(tasks))"
    )
    p = run(["uv", "run", "python", "-c", prog,
             json.dumps([r["task"] for r in frame])], cwd=UPSTREAM, timeout=300)
    if p.returncode != 0:
        return fail("frame", f"validate_task_config: {p.stderr.decode()[-300:]}")
    ok("frame", f"{len(frame)} tasks reproduce from seed and validate upstream")


# --------------------------------------------------------------- 3. corpus

def check_corpus(sample=25):
    manifest = json.loads((DATA / "corpus-manifest.json").read_text())
    head = run(["git", "-C", UPSTREAM, "rev-parse", "HEAD"]).stdout.decode().strip()
    if manifest.get("upstream_sha") != head:
        return fail("corpus", "manifest built against a different upstream sha")
    if manifest.get("corpus") != lab_arms.CORPUS:
        return fail("corpus", f"manifest corpus {manifest.get('corpus')!r} != "
                              f"{lab_arms.CORPUS!r}")
    frame = [json.loads(l) for l in (DATA / "frame.jsonl").read_text().splitlines()]
    roots = set()
    for r in frame:
        if r["shared_corpus"]:
            roots.add("tasks/" + posixpath.normpath(f"{r['task']}/{r['shared_corpus']}"))
        else:
            roots.add(f"tasks/{r['task']}/documents")
    missing = [r for r in roots if manifest["docroots"].get(r) != "done"]
    if missing:
        return fail("corpus", f"{len(missing)} frame docroots unbuilt, "
                              f"first: {missing[:2]}")
    files = manifest["files"]
    n_err = sum(1 for e in files.values() if e["status"] == "error")
    if files and n_err / len(files) > 1 - 0.98:
        return fail("corpus", f"coverage {(1 - n_err / len(files)):.1%} < 98%")
    docx_err = [r for r, e in files.items()
                if e["status"] == "error" and r.endswith(".docx")]
    if docx_err:
        return fail("corpus", f"{len(docx_err)} .docx failed extraction — the "
                              f"format this harness exists to unlock; first: "
                              f"{docx_err[0]}")
    rng = random.Random(0)
    oks = [r for r, e in files.items() if e["status"] == "ok"]
    for rel in rng.sample(oks, min(sample, len(oks))):
        md = UPSTREAM / lab_arms.md_name(rel)
        if not md.exists():
            return fail("corpus", f"manifest ok but no corpus file: {rel}")
        if (UPSTREAM / rel).exists():
            return fail("corpus", f"original still on disk: {rel} — the "
                                  f"sparse drop did not happen")
    ok("corpus", f"{len(files) - n_err}/{len(files)} converted, "
                 f"{len(roots)} frame docroots done, round-trip sampled")


# --------------------------------------------------------------- 4. shapes

def _docroots_for_shapes():
    """One ordinary frame docroot and, if in frame, the shared dms."""
    frame = [json.loads(l) for l in (DATA / "frame.jsonl").read_text().splitlines()]
    roots = []
    for r in frame:
        if not r["shared_corpus"]:
            roots.append(UPSTREAM / "tasks" / r["task"] / "documents")
            break
    for r in frame:
        if r["shared_corpus"]:
            roots.append(UPSTREAM / ("tasks/" + posixpath.normpath(
                f"{r['task']}/{r['shared_corpus']}")))
            break
    return roots


def check_shapes():
    common.require_bin()
    for root in _docroots_for_shapes():
        label = root.name if root.name != "documents" else root.parent.name
        empties, slow = 0, 0.0
        for q in LEGAL_QUERIES:
            t0 = time.time()
            p = run([common.BIN, "--json", "-k", "5", q, "."], cwd=root,
                    timeout=600, env_extra={"GORP_NO_HINTS": "1"})
            slow = max(slow, time.time() - t0)
            hits = [l for l in p.stdout.decode().splitlines() if l.strip()]
            if p.returncode not in (0, 1) :
                return fail("shapes", f"{label}: gorp exit {p.returncode} on "
                                      f"{q!r}: {p.stderr.decode()[-150:]}")
            if not hits:
                empties += 1
                continue
            h = json.loads(hits[0])
            target = root / h["path"]
            if not target.exists():
                return fail("shapes", f"{label}: hit cites missing file "
                                      f"{h['path']!r} for {q!r}")
            if not h["path"].endswith(".md"):
                return fail("shapes", f"{label}: hit outside the corpus "
                                      f"naming rule: {h['path']!r}")
        share = empties / len(LEGAL_QUERIES)
        if share > MAX_EMPTY_SHARE:
            return fail("shapes", f"{label}: {share:.0%} of canned queries "
                                  f"empty (> {MAX_EMPTY_SHARE:.0%}) — §16.10")
        # file scope: the shape that was broken in §16.10
        first_md = next(root.rglob("*.md"), None)
        p = run([common.BIN, "--json", "-k", "3", LEGAL_QUERIES[0],
                 first_md.relative_to(root)], cwd=root, timeout=600)
        if p.returncode not in (0, 1):
            return fail("shapes", f"{label}: file-as-scope exits {p.returncode}")
        for lit in RG_LITERALS:
            p = run([RG_BIN, "-n", "--no-heading", "--with-filename", lit, "."],
                    cwd=root, timeout=120)
            if p.returncode not in (0, 1):
                return fail("shapes", f"{label}: rg exit {p.returncode} on {lit!r}")
        note = f"{label}: {empties}/{len(LEGAL_QUERIES)} empty"
        if slow > 30:
            note += f", slowest query {slow:.0f}s (watch the dms warm cost)"
        ok("shapes", note)


# ----------------------------------------------------------------- 5. shim

def check_shim():
    shim = common.BENCH / "harness" / "common" / "shim.py"
    root = _docroots_for_shapes()[0]
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "shim_log.jsonl"
        base = {"LOCBENCH_SHIM_LOG": str(log), "LOCBENCH_STDOUT_DIR": td}
        # unset entirely for the block test — unset, not empty, is the
        # shim's blocked condition
        os_env = dict(os.environ)
        os_env.pop("LOCBENCH_REAL_GORP", None)
        blocked = subprocess.run(
            [sys.executable, str(shim), "gorp", "--json", "q", "."],
            cwd=root, capture_output=True, env={**os_env, **base})
        if blocked.returncode != 2 or not blocked.stdout or not blocked.stderr:
            return fail("shim", "unbound gorp did not block on both streams "
                                f"(exit {blocked.returncode})")
        bound = run([sys.executable, shim, "gorp", "--json", "-k", "3",
                     LEGAL_QUERIES[0], "."], cwd=root,
                    env_extra={**base, "LOCBENCH_REAL_GORP": str(common.BIN),
                               "GORP_NO_HINTS": "1"}, timeout=600)
        direct = run([common.BIN, "--json", "-k", "3", LEGAL_QUERIES[0], "."],
                     cwd=root, env_extra={"GORP_NO_HINTS": "1"}, timeout=600)
        if bound.stdout != direct.stdout:
            return fail("shim", "shimmed stdout != direct stdout")
        rows = [json.loads(l) for l in log.read_text().splitlines()]
        if not any(not r["blocked"] and r["tool"] == "gorp" for r in rows):
            return fail("shim", "no live gorp row in the shim log")
    ok("shim", "blocks unbound on both streams; byte-exact when bound; logs")


# ----------------------------------------------------------- 6. arm wiring

def check_arm_wiring():
    prog = (
        "import json, lab_arms, harness.run as hr\n"
        "from harness.tools import get_all_tool_definitions\n"
        "names = [t['name'] for t in get_all_tool_definitions()]\n"
        "import inspect\n"
        "amended = lab_arms.amend_system_prompt(hr.SYSTEM_PROMPT_PREAMBLE)\n"
        "print(json.dumps({'arm': lab_arms.ARM,\n"
        "  'upstream_names': names,\n"
        "  'extra': (lab_arms.ARMS[lab_arms.ARM] or {}).get('name'),\n"
        "  'track': lab_arms.tools_lines_track_descv9(),\n"
        "  'amended': amended != hr.SYSTEM_PROMPT_PREAMBLE}))\n"
    )
    for arm in lab_arms.ARMS:
        p = run(["uv", "run", "python", "-c", prog], cwd=UPSTREAM,
                env_extra={"LABBENCH_ARM": arm}, timeout=300)
        if p.returncode != 0:
            return fail("arm-wiring", f"{arm}: {p.stderr.decode()[-300:]}")
        r = json.loads(p.stdout.decode().splitlines()[-1])
        expect_extra = lab_arms.ARM_TOOL.get(arm)
        if r["extra"] != expect_extra:
            return fail("arm-wiring", f"{arm}: extra tool {r['extra']!r}, "
                                      f"expected {expect_extra!r}")
        if r["amended"] != (lab_arms.ARM_CLAUSE[arm] is not None):
            return fail("arm-wiring", f"{arm}: prompt amended={r['amended']}")
        if not r["track"]["ok"]:
            return fail("arm-wiring", f"desc drift: {r['track']['missing']}")
        if set(r["upstream_names"]) != {"bash", "read", "write", "edit",
                                        "glob", "grep"}:
            return fail("arm-wiring", f"upstream tool surface changed: "
                                      f"{r['upstream_names']}")
    ok("arm-wiring", f"{len(lab_arms.ARMS)} arms across 2 families; base "
                     f"untouched; treatments +1 tool; descs track")


# ------------------------------------------------------------ auth probe

def check_auth_probe(model="claude-sonnet-4-6"):
    """One minimal API call through upstream's own venv and .env loading.

    Opt-in (--probe-auth) because preflight is no-API-calls by default —
    but a credential that the Messages API rejects (an expired key, or an
    OAuth token scoped to something narrower than the API) otherwise
    surfaces one 200-turn agent run into the smoke. Costs ~a token.
    """
    prog = (
        "from harness.run import _load_env; _load_env()\n"
        "import anthropic\n"
        f"r = anthropic.Anthropic(max_retries=1).messages.create(\n"
        f"    model={model!r}, max_tokens=1,\n"
        "    messages=[{'role': 'user', 'content': 'ping'}])\n"
        "print('auth-ok', r.usage.input_tokens)\n"
    )
    p = run(["uv", "run", "python", "-c", prog], cwd=UPSTREAM, timeout=120)
    if p.returncode != 0:
        tail = p.stderr.decode(errors="ignore").strip().splitlines()
        return fail("auth-probe", tail[-1][-220:] if tail else "no stderr")
    ok("auth-probe", f"credential accepted by the Messages API ({model})")


# -------------------------------------------------------------- 7. runtime

def check_runtime(skip_podman, skip_keys):
    for cmd, name in ((["uv", "--version"], "uv"),
                      (["pandoc", "--version"], "pandoc")):
        try:
            if run(cmd).returncode != 0:
                return fail("runtime", f"{name} not working")
        except FileNotFoundError:
            return fail("runtime", f"{name} not on PATH")
    if not Path(RG_BIN).exists():
        fail("runtime", f"no ripgrep at {RG_BIN} (set RG_BIN)")
    if not skip_podman:
        p = run(["podman", "info"], timeout=60)
        if p.returncode != 0:
            fail("runtime", "podman info failed — is the machine started?")
        else:
            ok("runtime", "podman up")
    if not skip_keys:
        # Two credential paths, one per family. lab-*: the SDK sends
        # ANTHROPIC_API_KEY as x-api-key or ANTHROPIC_AUTH_TOKEN as a Bearer
        # header (both upstream clients are constructed bare, so env decides).
        # lab-cc-*: the `claude` CLI's own subscription login — no token
        # ever crosses into this harness. Fail only when NEITHER family
        # could run; name what each present credential unlocks.
        names = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
        dotenv = ((UPSTREAM / ".env").read_text()
                  if (UPSTREAM / ".env").exists() else "")
        api_cred = [n for n in names if n in os.environ or n in dotenv]
        import shutil as _sh
        cli = _sh.which("claude")
        if api_cred:
            ok("runtime", f"lab-* runnable ({api_cred[0]})")
        if cli:
            ok("runtime", "lab-cc-* runnable (claude CLI subscription login)")
        if not api_cred and not cli:
            fail("runtime", "no credential for either family: need an "
                            "ANTHROPIC_API_KEY/AUTH_TOKEN (lab-*) or the "
                            "claude CLI (lab-cc-*)")
    ok("runtime", "uv + pandoc + rg present")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-podman", action="store_true")
    ap.add_argument("--skip-keys", action="store_true")
    ap.add_argument("--skip-corpus", action="store_true",
                    help="CI: no data/, skip corpus/shape/shim/wiring checks")
    ap.add_argument("--probe-auth", action="store_true",
                    help="one ~1-token API call to validate the credential "
                         "(the only check here that spends anything)")
    args = ap.parse_args()

    print("labbench preflight:")
    if not UPSTREAM.exists():
        if args.skip_corpus:
            print("  ok    (no vendored upstream; corpus checks skipped for CI)")
            check_runtime(args.skip_podman, args.skip_keys)
            sys.exit(1 if FAILURES else 0)
        sys.exit("no vendored upstream — run fetch.sh")
    check_vendoring()
    check_frame()
    if not args.skip_corpus:
        check_corpus()
        check_shapes()
        check_shim()
    check_arm_wiring()  # needs the vendored tree, not the corpus
    check_runtime(args.skip_podman, args.skip_keys)
    if args.probe_auth:
        check_auth_probe()
    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s) — do not spend")
        sys.exit(1)
    print("\nall gates pass")


if __name__ == "__main__":
    main()
