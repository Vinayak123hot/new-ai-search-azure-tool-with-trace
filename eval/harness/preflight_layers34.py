"""
preflight_layers34.py — verify EVERYTHING needed for layers 3 & 4 is ready,
WITHOUT calling the real LLM (no Azure OpenAI quota spent).

It does two things:
  1. Static readiness checks (config, prompt file, dataset, SDK, env, endpoint).
  2. A full offline dry-run of the agent loop + layer-3 + layer-4 scoring using a
     MOCKED LLM client and a MOCKED tool — proving the plumbing (tool calls,
     clarification simulator, classification capture, metric computation) works.

Run:  python eval/harness/preflight_layers34.py
Exit 0 = ready to run layers 3 & 4 for real (just set AZURE_OPENAI_KEY and go).
"""
import json
import os
import sys
import types
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import layer3_clarification, layer4_end_to_end  # noqa: E402

OK, WARN, BAD = "  [OK]  ", "  [WARN]", "  [FAIL]"


# ── 1. static checks ────────────────────────────────────────────────
def static_checks(cfg, gold, kb_meta):
    problems = 0
    print("STATIC READINESS")

    a = cfg.get("agent", {})
    print(f"{OK} config.agent: deployment={a.get('deployment')} "
          f"endpoint={a.get('azure_openai_endpoint')} api_version={a.get('api_version')}")

    instr = os.path.join(common.EVAL_DIR, a.get("instructions_file", ""))
    if os.path.isfile(instr) and os.path.getsize(instr) > 1000:
        print(f"{OK} prompt file: {a['instructions_file']} ({os.path.getsize(instr):,} bytes)")
    else:
        print(f"{BAD} prompt file missing/too small: {instr}"); problems += 1

    amb = [c for c in gold if c["tier"] == "ambiguous" and c.get("dialog")]
    print(f"{OK} dataset: {len(gold)} cases, {len(amb)} ambiguous-with-dialog (layer 3), "
          f"{sum(1 for c in gold if c.get('expected_kb'))} in-KB / "
          f"{sum(1 for c in gold if not c.get('expected_kb'))} out-of-KB (layer 4)")
    if not amb:
        print(f"{WARN} no ambiguous dialog cases -> layer 3 will have nothing to score")

    try:
        import openai
        print(f"{OK} openai SDK importable (v{openai.__version__})")
    except Exception as e:
        print(f"{BAD} openai SDK not importable: {e}"); problems += 1

    print(f"{OK} TOOL_API_KEY set" if os.environ.get("TOOL_API_KEY")
          else f"{WARN} TOOL_API_KEY not set (needed for the real run)")
    print(f"{OK} AZURE_OPENAI_KEY set — ready for real run" if os.environ.get("AZURE_OPENAI_KEY")
          else f"{WARN} AZURE_OPENAI_KEY NOT set — set this when the LLM is ready, then run run_all.py")

    # endpoint reachability (no LLM)
    try:
        urllib.request.urlopen(cfg["endpoint"].rstrip("/") + "/docs", timeout=20)
        print(f"{OK} tool endpoint reachable: {cfg['endpoint']}")
    except Exception as e:
        print(f"{WARN} tool endpoint probe failed ({e}) — verify before the real run")
    return problems


# ── 2. mocked agent loop (no network) ───────────────────────────────
def _msg(content=None, tool_calls=None):
    return types.SimpleNamespace(content=content, tool_calls=tool_calls)


def _tc(call_id, name, args):
    return types.SimpleNamespace(id=call_id, type="function",
                                 function=types.SimpleNamespace(name=name, arguments=json.dumps(args)))


class FakeChat:
    """Self-consistent fake: candidates -> (one clarification if spread high) -> guidance -> classify."""
    def __init__(self):
        self.chat = self          # client.chat ...
        self.completions = self   # ... .completions.create

    def create(self, model, messages, tools, **kw):
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assistant_texts = [m for m in messages if m.get("role") == "assistant" and m.get("content")]
        # most recent candidates payload
        last_cand = None
        called_guidance = False
        for m in messages:
            if m.get("role") == "tool":
                try:
                    body = json.loads(m["content"])
                except Exception:
                    body = {}
                if "candidates" in body:
                    last_cand = body
            if m.get("role") == "assistant" and m.get("tool_calls"):
                for t in m["tool_calls"]:
                    if t["function"]["name"] == "get_kb_guidance":
                        called_guidance = True

        if last_cand is None:
            user = next(m["content"] for m in reversed(messages) if m.get("role") == "user")
            return _r(_msg(tool_calls=[_tc("c1", "get_kb_candidates", {"description": user})]))

        cands = last_cand.get("candidates") or []
        spread = last_cand.get("spread")
        disc = last_cand.get("discriminating_symptoms") or []
        asked = len(assistant_texts) > 0
        if spread == "high" and not asked:
            q = "To narrow it down: " + (disc[0] if disc else "can you describe the symptom?")
            return _r(_msg(content=q))
        if cands:
            top = cands[0]
            if top.get("guidance_troubleshoot") and not called_guidance:
                return _r(_msg(tool_calls=[_tc("g1", "get_kb_guidance", {"kb_id": top["kb_id"]})]))
            return _r(_msg(content=f"KB_Article_name: {top['kb_id']}\nMatching Score: "
                                   f"{top.get('score')}\nCall Orchestrator: True"))
        return _r(_msg(content="No valid document found."))


def _r(message):
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


class FakeTool:
    """Returns a deterministic candidates/guidance payload; top candidate is the
    case's expected_kb so the mocked agent lands on it (validates routing + scoring)."""
    def __init__(self, kb_meta, gold):
        self.kb_meta = kb_meta
        self.by_query = {c["query"]: c for c in gold}
        self._round = {}

    def candidates(self, description):
        # find a case whose query is a prefix of the (cumulative) description
        case = next((c for q, c in self.by_query.items() if description.startswith(q)), None)
        exp = (case or {}).get("expected_kb")
        if not exp:                                    # out-of-KB -> empty (rejected)
            return {"candidates": [], "spread": "low", "top_score": 1.2,
                    "discriminating_symptoms": [], "any_meets_display_threshold": False}
        tier = (case or {}).get("tier")
        n = self._round.get(description[:20], 0); self._round[description[:20]] = n + 1
        spread = "high" if (tier == "ambiguous" and n == 0) else "low"
        gt = bool(self.kb_meta.get(exp, {}).get("guidance_troubleshoot"))
        syms = self.kb_meta.get(exp, {}).get("symptoms", []) or ["the reported symptom"]
        return {"candidates": [{"kb_id": exp, "score": 3.2, "guidance_troubleshoot": gt,
                                "meets_display_threshold": True, "key_symptoms": syms}],
                "spread": spread, "top_score": 3.2,
                "discriminating_symptoms": syms[:2], "any_meets_display_threshold": True}

    def guidance(self, kb_id):
        return {"kb_id": kb_id, "title": "mock", "sections": [{"heading": "Resolution Steps",
                "steps": [{"text": "step", "level": 0}]}], "document_url": "https://x"}


def mock_dryrun(cfg, gold, kb_meta):
    print("\nMOCK DRY-RUN (no real LLM)")
    os.environ.setdefault("AZURE_OPENAI_KEY", "preflight-dummy")     # lets AgentRunner construct
    tool = FakeTool(kb_meta, gold)
    runner = common.AgentRunner(cfg, kb_meta, tool)
    runner.client = FakeChat()                                       # swap real client for fake

    # validate one ambiguous case loop
    amb = next((c for c in gold if c["tier"] == "ambiguous"), None)
    if amb:
        res = runner.run(amb)
        assert isinstance(res, dict) and "predicted" in res and "tool_events" in res
        print(f"{OK} agent loop runs: {amb['id']} -> predicted={res['predicted']} "
              f"rounds={res['rounds']} followups={len(res['followups'])} "
              f"guidance={res['called_guidance']} tool_calls={len(res['tool_events'])}")

    # validate layer 3 + layer 4 scoring end-to-end on mocks
    l3 = layer3_clarification.run(cfg, gold, kb_meta, tool, runner, verbose=False)
    print(f"{OK} layer 3 scoring runs: {json.dumps(l3['metrics'])}")
    l4 = layer4_end_to_end.run(cfg, gold, kb_meta, tool, runner, verbose=False)
    print(f"{OK} layer 4 scoring runs: {json.dumps(l4['metrics'])}")
    print("       (numbers above are MOCK — they only prove the code paths execute)")


def main():
    cfg, gold, kb_meta = common.load_config(), common.load_gold(), common.load_kb_meta()
    problems = static_checks(cfg, gold, kb_meta)
    try:
        mock_dryrun(cfg, gold, kb_meta)
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"{BAD} mock dry-run raised: {e}"); problems += 1

    print("\n" + "=" * 60)
    if problems == 0:
        print("PREFLIGHT PASSED — layers 3 & 4 are ready.")
        print("When the LLM is ready:  export AZURE_OPENAI_KEY=... ; python eval/harness/run_all.py")
        print("Cheap first check:       python eval/harness/run_all.py --smoke 2")
    else:
        print(f"PREFLIGHT FOUND {problems} blocking issue(s) — fix before running for real.")
    sys.exit(0 if problems == 0 else 1)


if __name__ == "__main__":
    main()
