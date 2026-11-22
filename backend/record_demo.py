"""
Turn your latest live run into the offline demo backup.

    python record_demo.py            # uses the most recent run in runs/live.jsonl
    then set DEMO_MODE=1 in .env and restart the server

It saves: the request, every question card (with suggestions), every plan version,
the draft preview and the Pangram voice result. A copy of the old demo is kept as
demo_cache.backup.json.
"""
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "runs" / "live.jsonl"
OUT = ROOT / "demo_cache.json"

rows = [json.loads(l) for l in LOG.read_text().splitlines() if l.strip()]
plans = [r for r in rows if r["kind"] == "plan"]
if not plans:
    raise SystemExit("No plan found in runs/live.jsonl. Do one full live run first.")

request = plans[-1]["request"]
# the session = everything after the last first-question for this request
start = max(i for i, r in enumerate(rows)
            if r["kind"] == "question" and r.get("request") == request and not r.get("history"))
session = rows[start:]

questions = [r["out"] for r in session if r["kind"] == "question" and r.get("request") == request and not r["out"].get("done")]
plan_versions = [r["out"] for r in session if r["kind"] == "plan" and r.get("request") == request]
previews = [r for r in session if r["kind"] == "preview" and r.get("markdown")]
voices = [r["out"] for r in session if r["kind"] == "voice"]

old = json.loads(OUT.read_text()) if OUT.exists() else {}
if OUT.exists():
    shutil.copy(OUT, ROOT / "demo_cache.backup.json")

demo = {
    "persona": "Recorded live run",
    "request": request,
    "upload_text": old.get("upload_text", ""),
    "questions": [{k: q.get(k, "") for k in ("summary", "field", "question", "why", "suggestions")} for q in questions],
    "plans": [{k: v for k, v in p.items() if not k.startswith("_")} for p in plan_versions],
    "preview": previews[-1]["markdown"] if previews else old.get("preview", ""),
    "voice_check": voices[-1] if voices else old.get("voice_check", {}),
}
if not demo["plans"]:
    raise SystemExit("That run has no plan. Finish the flow to the plan screen and try again.")
OUT.write_text(json.dumps(demo, indent=2, ensure_ascii=False))
print(f"Saved demo for: {request!r}")
print(f"  {len(demo['questions'])} questions, {len(demo['plans'])} plan version(s), "
      f"preview: {'yes' if previews else 'kept old'}, Pangram voice: {'yes' if voices else 'kept old'}")
print("Now set DEMO_MODE=1 in .env and restart the server to replay it offline.")
