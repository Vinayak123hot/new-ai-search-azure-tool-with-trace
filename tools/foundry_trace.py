"""
foundry_trace.py — readable dump of an Azure AI Foundry agent conversation.

The local trace (logs/trace_*.log from main_traced.py) shows everything that
happens INSIDE the tool service. This script shows the OTHER half — what the
agent did in Foundry: every user message, every follow-up question the agent
asked, and every tool call it made (name + arguments + raw output), in order.

Setup (one-time):
    pip install azure-ai-projects azure-identity
    az login                       # DefaultAzureCredential — no keys needed
    set FOUNDRY_PROJECT_ENDPOINT=https://<your-foundry-resource>.services.ai.azure.com/api/projects/<project-name>

Usage:
    python tools/foundry_trace.py                  # list the 10 newest threads
    python tools/foundry_trace.py <thread_id>      # full readable trace of one thread
    python tools/foundry_trace.py <thread_id> -o trace.txt
"""

import argparse
import json
import os
import sys

from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient

RULE = "═" * 100
LINE = "─" * 100


def out(f, text=""):
    print(text, file=f)


def pretty(value, indent="        "):
    """Pretty-print possibly-JSON strings with an indent."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return indent + value
    return "\n".join(indent + ln for ln in
                     json.dumps(value, indent=2, ensure_ascii=False, default=str).splitlines())


def get_client():
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").strip()
    if not endpoint:
        sys.exit("Set FOUNDRY_PROJECT_ENDPOINT first (Foundry portal → your project → "
                 "Overview → 'Azure AI Foundry project endpoint').")
    return AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())


def list_threads(client, f):
    out(f, RULE)
    out(f, "NEWEST THREADS (pass a thread_id to this script for the full trace)")
    out(f, RULE)
    threads = client.agents.threads.list(limit=10)
    for th in threads:
        created = getattr(th, "created_at", "")
        out(f, f"  {th.id}    created={created}")


def dump_thread(client, thread_id: str, f):
    agents = client.agents

    out(f, RULE)
    out(f, f"FOUNDRY AGENT TRACE — thread {thread_id}")
    out(f, RULE)

    # ── 1. Conversation messages in chronological order ───────────
    out(f, "")
    out(f, "CONVERSATION (user ↔ agent)")
    out(f, LINE)
    try:
        from azure.ai.agents.models import ListSortOrder
        messages = agents.messages.list(thread_id=thread_id, order=ListSortOrder.ASCENDING)
    except Exception:
        messages = agents.messages.list(thread_id=thread_id)

    for m in messages:
        role = str(getattr(m, "role", "?")).upper()
        when = getattr(m, "created_at", "")
        texts = []
        for c in getattr(m, "content", []) or []:
            txt = getattr(getattr(c, "text", None), "value", None)
            if txt:
                texts.append(txt)
        out(f, f"\n[{when}] {role}:")
        for txt in texts:
            for ln in txt.splitlines():
                out(f, "    " + ln)

    # ── 2. Runs + run steps: the agent's tool calls ────────────────
    out(f, "")
    out(f, "RUNS & TOOL CALLS (what the agent executed, in order)")
    out(f, LINE)
    runs = list(agents.runs.list(thread_id=thread_id))
    runs.sort(key=lambda r: getattr(r, "created_at", 0) or 0)

    for run in runs:
        out(f, f"\nRUN {run.id}  status={getattr(run, 'status', '?')}  "
               f"created={getattr(run, 'created_at', '')}")
        usage = getattr(run, "usage", None)
        if usage:
            out(f, f"    tokens: prompt={getattr(usage, 'prompt_tokens', '?')} "
                   f"completion={getattr(usage, 'completion_tokens', '?')}")

        try:
            steps = list(agents.run_steps.list(thread_id=thread_id, run_id=run.id))
        except Exception as e:
            out(f, f"    (could not list run steps: {e})")
            continue
        steps.sort(key=lambda s: getattr(s, "created_at", 0) or 0)

        for step in steps:
            stype = str(getattr(step, "type", "?"))
            details = getattr(step, "step_details", None)
            if "message" in stype:
                out(f, f"    step {step.id}: agent produced a message "
                       f"(see CONVERSATION above)")
                continue

            tool_calls = getattr(details, "tool_calls", None) or []
            for tc in tool_calls:
                tc_type = getattr(tc, "type", "?")
                out(f, f"    step {step.id}: TOOL CALL ({tc_type})")

                # function-style tools (incl. OpenAPI tools) expose name/arguments/output
                fn = getattr(tc, "function", None)
                if fn is not None:
                    out(f, f"      tool name : {getattr(fn, 'name', '?')}")
                    args = getattr(fn, "arguments", None)
                    if args:
                        out(f, "      arguments :")
                        out(f, pretty(args))
                    output = getattr(fn, "output", None)
                    if output:
                        out(f, "      output    :")
                        out(f, pretty(output))
                else:
                    # fall back to raw dict dump for other tool-call shapes
                    try:
                        out(f, pretty(tc.as_dict() if hasattr(tc, "as_dict") else str(tc)))
                    except Exception:
                        out(f, f"      {tc}")

    out(f, "")
    out(f, RULE)
    out(f, "TIP: match these tool calls with the local pipeline traces in logs/trace_*.log "
           "by timestamp and by the 'description' argument.")


def main():
    ap = argparse.ArgumentParser(description="Readable Azure AI Foundry agent trace")
    ap.add_argument("thread_id", nargs="?", help="thread to dump; omit to list threads")
    ap.add_argument("-o", "--output", help="write to file instead of stdout")
    args = ap.parse_args()

    client = get_client()
    f = open(args.output, "w", encoding="utf-8") if args.output else sys.stdout
    try:
        if args.thread_id:
            dump_thread(client, args.thread_id, f)
        else:
            list_threads(client, f)
    finally:
        if args.output:
            f.close()
            print(f"written to {args.output}")


if __name__ == "__main__":
    main()
