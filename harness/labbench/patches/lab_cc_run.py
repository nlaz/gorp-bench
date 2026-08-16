#!/usr/bin/env python3
"""Run one LAB task with the Claude Agent SDK — the subscription-funded loop.

The lab-cc-* family. Everything task-shaped is upstream's, imported from the
vendored tree this file is copied into: `load_task`, the system-prompt
preamble + skills, the podman `Sandbox`, and the very same
`ToolExecutor`/`ArmToolExecutor` the lab-* family runs (`lab_arms.build`).
What changes is ONE thing — who drives the loop. Instead of upstream's
`agent_loop.py` over the Anthropic SDK (API key), the loop is the Claude
Agent SDK, which authenticates as the operator's Claude Code login. No
token is extracted or forwarded anywhere: the SDK spawns the `claude` CLI,
which holds its own credentials. That is the supported way to fund a run
from a subscription, and the same auth path swexplore's arms already use.

Tool surface: the executor's seven tools are exposed as in-process MCP
tools (`mcp__lab__bash` … `mcp__lab__gorp`) and the CLI's built-ins are
disabled (`tools=[]`), so the agent cannot Read/Bash outside the sandbox
contract — the tool UNIVERSE is restricted, which is stronger than an
allowlist (`--allowedTools` does not bind under permissive modes; §27's
lesson). `setting_sources=[]` + `strict_mcp_config` keep the operator's own
MCP servers and settings out of the experiment (the 36-tool contamination
swexplore's first smoke caught).

Artifacts mirror `harness/run.py` so the driver, triage and judge read one
shape: results/<run-id>/{config.json, transcript.jsonl, metrics.json,
telemetry.json, workspace/, output/}. metrics.json carries the same keys
plus `family: "cc"` and `cc_subtype` (the SDK's terminal subtype);
`input_tokens` sums fresh + cache-creation + cache-read tokens so it means
"context volume" — comparable within the family, NOT across families.

    LABBENCH_ARM=lab-cc-gorp uv run --with claude-agent-sdk python \
        lab_cc_run.py --task <area>/<slug> --run-id <id> --model sonnet
"""

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from harness.run import (BENCH_ROOT, DEFAULT_SKILLS, SYSTEM_PROMPT_PREAMBLE,
                         load_skills, load_task, setup_skill_scripts)
from sandbox.sandbox import DEFAULT_IMAGE, Sandbox

import lab_arms

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions,
                              ResultMessage, create_sdk_mcp_server, query,
                              tool)

TRANSCRIPT_TEXT_CAP = 500       # upstream agent_loop's own truncations
TRANSCRIPT_RESULT_CAP = 1000


def sdk_tools_for(executor, tool_defs):
    """Wrap the executor's tools as SDK MCP tools, definitions verbatim.

    The handler runs `executor.execute` off the event loop — a sandboxed
    bash call can hold the shell timeout, and the MCP server must keep
    serving the CLI meanwhile.
    """
    wrapped = []
    for t in tool_defs:
        name = t["name"]

        def make(name):
            async def handler(args):
                out = await asyncio.to_thread(executor.execute, name, args)
                return {"content": [{"type": "text", "text": out}]}
            return handler

        wrapped.append(tool(name, t["description"], t["parameters"])(make(name)))
    return wrapped


async def run_cc_agent(options, user_prompt, transcript_path):
    turn = 0
    result = None
    with open(transcript_path, "w") as tf:
        async for msg in query(prompt=user_prompt, options=options):
            if isinstance(msg, AssistantMessage):
                turn += 1
                text, calls = [], []
                for b in msg.content:
                    if getattr(b, "text", None):
                        text.append(b.text)
                    if getattr(b, "name", None):
                        calls.append({"name": b.name,
                                      "arguments": getattr(b, "input", {})})
                tf.write(json.dumps({
                    "turn": turn, "role": "assistant",
                    "text": " ".join(text)[:TRANSCRIPT_TEXT_CAP],
                    "tool_calls": calls}) + "\n")
                tf.flush()
            elif isinstance(msg, ResultMessage):
                result = msg
                tf.write(json.dumps({
                    "turn": turn, "role": "result", "subtype": msg.subtype,
                    "num_turns": msg.num_turns, "is_error": msg.is_error,
                    "total_cost_usd": msg.total_cost_usd,
                    "result_preview": (msg.result or "")[:TRANSCRIPT_RESULT_CAP],
                }) + "\n")
                tf.flush()
    return result


def main(args):
    if not lab_arms.ARM.startswith("lab-cc-"):
        raise SystemExit(
            f"LABBENCH_ARM={lab_arms.ARM!r}: this entry point runs the "
            f"lab-cc-* family only; the lab-* family runs harness.run")

    task = load_task(task_name=args.task)
    results_dir = BENCH_ROOT / "results" / args.run_id
    output_dir = results_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir = results_dir / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    skill_names = DEFAULT_SKILLS if args.skills is None else args.skills

    sandbox = Sandbox(
        documents_dir=Path(task["docs_dir"]),
        output_dir=output_dir,
        workspace_dir=workspace_dir,
        image=args.sandbox_image,
        default_timeout=args.shell_timeout,
    )
    sandbox.start()

    (results_dir / "config.json").write_text(json.dumps({
        "family": "cc", "model": args.model, "task": args.task,
        "run_id": args.run_id, "max_turns": args.max_turns,
        "shell_timeout": args.shell_timeout, "skills": skill_names,
        "sandbox_image": args.sandbox_image,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2))

    tool_executor, tool_defs = lab_arms.build(
        sandbox=sandbox, shell_timeout=args.shell_timeout)

    system_prompt = SYSTEM_PROMPT_PREAMBLE
    if skill_names:
        system_prompt += load_skills(skill_names)
        setup_skill_scripts(skill_names, workspace_dir)
    system_prompt = lab_arms.amend_system_prompt(system_prompt)

    server = create_sdk_mcp_server(
        "lab", tools=sdk_tools_for(tool_executor, tool_defs))
    options = ClaudeAgentOptions(
        tools=[],  # no built-ins: the executor's tools are the universe
        allowed_tools=[f"mcp__lab__{t['name']}" for t in tool_defs],
        mcp_servers={"lab": server},
        strict_mcp_config=True,
        setting_sources=[],
        system_prompt=system_prompt,
        permission_mode="bypassPermissions",
        max_turns=args.max_turns,
        model=args.model,
        cwd=str(workspace_dir),
    )

    print(f"lab-cc agent: {lab_arms.ARM} · {args.model} · "
          f"{len(tool_defs)} MCP tools · max {args.max_turns} turns")
    t0 = time.time()
    try:
        result = asyncio.run(run_cc_agent(
            options, task["instructions"],
            results_dir / "transcript.jsonl"))
    finally:
        sandbox.stop()
    wall = round(time.time() - t0, 2)

    if result is None:
        raise SystemExit("no ResultMessage from the SDK — the run produced "
                         "nothing scoreable")

    usage = result.usage or {}
    input_tokens = sum(usage.get(k) or 0 for k in (
        "input_tokens", "cache_creation_input_tokens",
        "cache_read_input_tokens"))
    output_tokens = usage.get("output_tokens") or 0
    metrics = {
        **tool_executor.get_metrics(),
        "family": "cc",
        "model": args.model,
        "task": args.task,
        "run_id": args.run_id,
        "turn_count": result.num_turns,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "usage_raw": usage,
        "wall_clock_seconds": wall,
        "finished_cleanly": result.subtype == "success" and not result.is_error,
        "context_overflow": False,  # the CLI auto-compacts; see cc_subtype
        "cc_subtype": result.subtype,
        "total_cost_usd": result.total_cost_usd,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    (results_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))
    lab_arms.write_telemetry(results_dir, tool_executor)
    print(f"lab-cc done: {result.subtype} · {result.num_turns} turns · "
          f"{wall:.0f}s · searches={getattr(tool_executor, 'lab_search_count', 0)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--max-turns", type=int, default=200)
    ap.add_argument("--shell-timeout", type=int, default=60)
    ap.add_argument("--skills", nargs="*", default=None)
    ap.add_argument("--sandbox-image", default=DEFAULT_IMAGE)
    main(ap.parse_args())
