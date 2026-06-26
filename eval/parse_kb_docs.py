"""
parse_kb_docs.py — extract structured fields from the KB .docx files for eval.

Reads every .docx in eval/kb_docs/, pulls kb_id, title, guidance_troubleshoot,
environment, and the symptom lines, and writes eval/kb_meta.json. This is the
ground-truth catalogue the gold set and the evaluators are built from.

Standalone — does not import or modify the service code.
"""
import json
import os
import re
from io import BytesIO

import docx as python_docx

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(HERE, "kb_docs")
OUT = os.path.join(HERE, "kb_meta.json")

_SYM_HEADERS = ("questions / symptoms", "user experience / symptoms", "symptoms",
                "user experience")
_STOP_HEADERS = ("cause", "resolution", "steps", "option", "note", "environment")


def parse(path):
    doc = python_docx.Document(path)
    paras = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    full = "\n".join(paras)

    kb_id = ""
    m = re.search(r"KB\s*ID\s*:\s*(KB\d+)", full, re.IGNORECASE)
    if m:
        kb_id = m.group(1).upper()
    if not kb_id:
        m2 = re.search(r"(KB\d{5,})", os.path.basename(path), re.IGNORECASE)
        kb_id = m2.group(1).upper() if m2 else os.path.splitext(os.path.basename(path))[0]

    title = paras[0] if paras else ""

    gt = None
    mg = re.search(r"Guidance\s*Troubleshoot\s*:\s*(true|false)", full, re.IGNORECASE)
    if mg:
        gt = mg.group(1).lower() == "true"

    env = ""
    me = re.search(r"Environment\s*:\s*(.+)", full)
    if me:
        env = me.group(1).strip()

    # collect symptom lines: those under a symptoms header until the next section
    symptoms = []
    collecting = False
    for line in paras:
        low = line.lower()
        if any(low.startswith(h) for h in _SYM_HEADERS):
            collecting = True
            # header line may itself contain a trailing symptom after the colon
            after = line.split(":", 1)[1].strip() if ":" in line else ""
            if after:
                symptoms.append(after)
            continue
        if collecting:
            if any(low.startswith(h) for h in _STOP_HEADERS) or re.match(r"^kb\s*id", low):
                collecting = False
                continue
            # skip metadata-ish lines
            if low.startswith(("guidance troubleshoot", "environment", "kb id")):
                continue
            symptoms.append(line)

    # de-dup, trim
    seen, syms = set(), []
    for s in symptoms:
        s = s.strip(" -•\t")
        if s and s.lower() not in seen and len(s) > 3:
            seen.add(s.lower())
            syms.append(s)

    return {
        "kb_id": kb_id,
        "title": title,
        "guidance_troubleshoot": gt,
        "environment": env,
        "symptoms": syms,
        "source_file": os.path.basename(path),
    }


def main():
    metas = []
    for f in sorted(os.listdir(DOCS_DIR)):
        if f.lower().endswith(".docx") and not f.startswith("~"):
            metas.append(parse(os.path.join(DOCS_DIR, f)))
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(metas, fh, indent=2, ensure_ascii=False)
    print(f"parsed {len(metas)} docs -> {OUT}\n")
    for m in metas:
        print(f"{m['kb_id']}  gt={m['guidance_troubleshoot']}  | {m['title'][:70]}")
        for s in m["symptoms"]:
            print(f"      - {s[:90]}")
        print()


if __name__ == "__main__":
    main()
