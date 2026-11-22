"""
Ask Me First: backend (FastAPI).

Local:
    cd backend
    pip install -r requirements.txt
    # edit .env (never re-copy .env.example over it)
    uvicorn main:app --reload --port 8000
    open http://localhost:8000

Deploy (Modal):  modal deploy modal_app.py

Design:
- Keys live only on the server. The browser never sees them.
- AI providers are tried in order: Hermes (Nous Portal or OpenRouter), then OpenAI.
  If one fails (no credit, timeout, bad JSON) the next is used automatically.
- If every provider fails, domain-aware fallbacks keep the flow relevant
  (questions and suggestions for resumes differ from those for images, etc.).
- The server enforces the interview limits; endpoints never return a 500.
- DEMO_MODE=1 replays demo_cache.json.
"""

import asyncio
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from openai import AsyncOpenAI, NotFoundError
from pydantic import BaseModel

from fallbacks import DELIVERABLE, DRAFT_STEP, categorize, fallback_question, your_thing

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=True)

# ---------------- config ----------------
NOUS_API_KEY = os.getenv("NOUS_API_KEY", "")
NOUS_BASE_URL = os.getenv("NOUS_BASE_URL", "https://inference-api.nousresearch.com/v1")
# "auto" = ask the provider which Hermes models are live and pick the best one.
INTERVIEW_MODEL = os.getenv("HERMES_INTERVIEW_MODEL", "auto")
PLAN_MODEL = os.getenv("HERMES_PLAN_MODEL", "auto")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-5-5")
ANTHROPIC_VERSION = os.getenv("ANTHROPIC_VERSION", "2026-10-21")
PANGRAM_API_KEY = os.getenv("PANGRAM_API_KEY", "")
REDUCTO_API_KEY = os.getenv("REDUCTO_API_KEY", "")
DEMO_MODE = os.getenv("DEMO_MODE", "0") == "1"
DEMO_DELAY = float(os.getenv("DEMO_DELAY", "1.0"))
Q_TIMEOUT = float(os.getenv("QUESTION_TIMEOUT", "25"))
PLAN_TIMEOUT = float(os.getenv("PLAN_TIMEOUT", "55"))
MAX_QUESTIONS = 5
MIN_QUESTIONS = 3
PANGRAM_MIN_WORDS = 50
HERMES_PREFERENCE = ["hermes-4.3-36b", "hermes-4-405b", "hermes-4-70b"]

SLOTS = {
    "audience": "who will receive or use the result, in concrete terms",
    "purpose": "what the result must achieve; what success looks like",
    "key_details": "specific facts, content or experience only this person knows (names, dates, places, numbers, history)",
    "style": "how it should look, sound or feel",
    "avoid": "anything the result must never say, show or do",
    "clarify": "a short follow-up that makes the vaguest earlier answer specific (choose ONLY if an earlier answer is too vague to act on, e.g. 'event', 'stuff', 'normal')",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("askmefirst")

# ---------------- AI providers (Hermes first, OpenAI as backup) ----------------
class Provider:
    def __init__(self, name, client, configured_model, json_mode, native_anthropic=False):
        self.name, self.client, self.configured, self.json_mode = name, client, configured_model, json_mode
        self.native_anthropic = native_anthropic
        self.resolved = None if configured_model.lower() == "auto" else configured_model

    async def model(self) -> str:
        if self.resolved:
            return self.resolved
        page = await self.client.models.list()
        ids = [m.id for m in page.data]
        lower = {i.lower(): i for i in ids}
        pick = next((lower[p] for p in HERMES_PREFERENCE if p in lower), None)
        if not pick:
            hermes = sorted([i for i in ids if "hermes" in i.lower()], reverse=True)
            pick = hermes[0] if hermes else ids[0]
        log.info("%s: available models %s -> using %s", self.name, ids[:20], pick)
        self.resolved = pick
        return pick


def make_providers(kind: str) -> list[Provider]:
    out = []
    if NOUS_API_KEY:
        c = AsyncOpenAI(api_key=NOUS_API_KEY, base_url=NOUS_BASE_URL, timeout=PLAN_TIMEOUT, max_retries=0)
        out.append(Provider("hermes", c, INTERVIEW_MODEL if kind == "q" else PLAN_MODEL, json_mode=False))
    if OPENAI_API_KEY:
        # Any OpenAI-compatible backup: OpenAI (default), Claude (https://api.anthropic.com/v1/), OpenRouter...
        base = os.getenv("OPENAI_BASE_URL") or None
        c = AsyncOpenAI(api_key=OPENAI_API_KEY, base_url=base, timeout=PLAN_TIMEOUT, max_retries=0)
        name = "claude" if base and "anthropic" in base else "openai" if not base else "backup"
        out.append(Provider(name, c, OPENAI_MODEL, json_mode=base is None))
    if ANTHROPIC_API_KEY:
        # Claude via its own Messages API (works with every Anthropic key type)
        out.append(Provider("claude", None, ANTHROPIC_MODEL, json_mode=False, native_anthropic=True))
    return out


async def call_anthropic(model: str, system: str, user: str, max_tokens: int) -> str:
    import httpx
    async with httpx.AsyncClient(timeout=PLAN_TIMEOUT) as c:
        r = await c.post("https://api.anthropic.com/v1/messages",
                         headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": ANTHROPIC_VERSION,
                                  "content-type": "application/json"},
                         json={"model": model, "max_tokens": max_tokens, "system": system,
                               "messages": [{"role": "user", "content": user}]})
    if r.status_code >= 400:
        raise RuntimeError(f"Claude HTTP {r.status_code}: {r.text[:300]}")
    j = r.json()
    text = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
    if not text.strip() or j.get("stop_reason") == "max_tokens":
        kinds = [b.get("type") for b in j.get("content", [])]
        raise RuntimeError(f"Claude reply incomplete (stop_reason={j.get('stop_reason')}, blocks={kinds}): {text[:120]!r}")
    return text


PROVIDERS = {"q": make_providers("q"), "plan": make_providers("plan")}
# share resolved Hermes model between the two lists
if PROVIDERS["q"] and PROVIDERS["plan"] and PROVIDERS["q"][0].name == "hermes" and PLAN_MODEL == INTERVIEW_MODEL:
    PROVIDERS["plan"][0] = PROVIDERS["q"][0]


def extract_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M)
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        raise ValueError(f"no JSON in output: {text[:200]!r}")
    return json.loads(m.group(0), strict=False)


async def call_provider(p: Provider, system: str, user: str, max_tokens: int, validate) -> dict:
    parse = parse_plan_text if system is PLAN_SYSTEM else extract_json
    if p.native_anthropic:
        data = parse(await call_anthropic(p.configured, system, user, max_tokens))
        validate(data)
        return data
    kwargs = dict(temperature=0.6, max_tokens=max_tokens,
                  messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    if p.json_mode and system is not PLAN_SYSTEM:
        kwargs["response_format"] = {"type": "json_object"}
    try:
        r = await p.client.chat.completions.create(model=await p.model(), **kwargs)
    except NotFoundError as e:
        # model retired or wrong name: rediscover once
        log.warning("%s model not found (%s); rediscovering", p.name, e)
        if p.name != "hermes":
            raise
        p.resolved = None
        p.configured = "auto"
        r = await p.client.chat.completions.create(model=await p.model(), **kwargs)
    data = parse(r.choices[0].message.content)
    validate(data)
    return data


async def llm_json(kind: str, system: str, user: str, budget: float, max_tokens: int, validate) -> tuple[dict, str]:
    """Try each provider in order within one overall time budget."""
    providers = PROVIDERS[kind]
    if not providers:
        raise RuntimeError("no AI provider configured")
    deadline = time.monotonic() + budget
    last = None
    for i, p in enumerate(providers):
        remaining = deadline - time.monotonic()
        if remaining < 3:
            break
        share = remaining * (0.62 if i < len(providers) - 1 else 1.0)
        for attempt in range(2):
            left = min(share, deadline - time.monotonic())
            if left < 3:
                break
            try:
                data = await asyncio.wait_for(call_provider(p, system, user, max_tokens, validate), timeout=left)
                return data, p.name
            except asyncio.TimeoutError as e:
                last = e
                log.warning("%s timed out", p.name)
                break
            except Exception as e:  # noqa: BLE001
                last = e
                log.warning("%s attempt %d failed: %s", p.name, attempt + 1, e)
                if attempt == 0:
                    await asyncio.sleep(0.8)
    raise last or RuntimeError("all providers failed")


IDK = {"i don't know", "i dont know", "idk", "not sure", "i'm not sure", "skip", "skipped", ""}


def is_idk(a: str) -> bool:
    return a.strip().lower().rstrip(".!?") in IDK


# ---------------- schemas ----------------
class Turn(BaseModel):
    field: str
    question: str
    answer: str


class NextQuestionIn(BaseModel):
    request: str
    history: list[Turn] = []
    upload_text: Optional[str] = None


class NextQuestionOut(BaseModel):
    summary: str = ""
    question: str = ""
    why: str = ""
    field: str = ""
    suggestions: list[str] = []
    done: bool = False
    source: str = "ai"
    error: str = ""


class PlanIn(BaseModel):
    request: str
    history: list[Turn] = []
    upload_text: Optional[str] = None
    previous_plan: Optional[dict] = None
    change_request: Optional[str] = None


class VoiceIn(BaseModel):
    text: str


class PreviewIn(BaseModel):
    request: str
    brief: str


# ---------------- shared helpers ----------------
def context_block(request: str, history: list[Turn], upload_text: Optional[str]) -> str:
    lines = [f'What the person wants (their exact words): "{request.strip()}"',
             f"Type of request: {categorize(request)}"]
    if upload_text:
        lines.append("Excerpt from a document they attached. Use its specific facts and refer to it naturally:\n"
                     + upload_text[:2500])
    if history:
        lines.append("Their answers so far, in order:")
        for t in history:
            lines.append(f"- [{t.field}] Q: {t.question}\n  A: {t.answer}")
    return "\n".join(lines)


def log_run(kind: str, payload: dict) -> None:
    try:
        d = ROOT / "runs"
        d.mkdir(exist_ok=True)
        with open(d / "live.jsonl", "a") as f:
            f.write(json.dumps({"t": time.time(), "kind": kind, **payload}) + "\n")
    except Exception:  # noqa: BLE001
        pass


def load_demo() -> dict:
    p = ROOT / "demo_cache.json"
    return json.loads(p.read_text()) if p.exists() else {}


# ---------------- interview ----------------
Q_SYSTEM = """You are an experienced, warm consultant helping a person with no AI experience get exactly what they want.
You ask ONE short question at a time.

Their request defines everything. Stay inside their domain (for example a bakery, a job search, a family budget,
a school lesson). Never drift to other topics.

Choose the single most useful remaining topic for THIS request, then write:
- "question": plain professional English, under 20 words. It MUST name their specific thing in natural words
  (write "Who will see your bakery's weekend sale poster?", never "Who is this for?").
  When an earlier answer is relevant, build on it explicitly (e.g. "Since it is for first-time buyers, ...").
  If they attached a document, refer to a specific detail from it when it fits.
- "why": one sentence, under 18 words, on how the answer improves THEIR result.
- "suggestions": exactly 3 realistic answers this specific person might give, each under 10 words,
  specific to their domain and consistent with their earlier answers. Make them concrete and different
  from each other. Never generic placeholders.
- "field": the topic key you chose (must be one of the available topics).
- "done": true only if at least 3 questions are answered and you have enough for an excellent result.
- "summary": ONLY when no questions have been answered yet: one plain sentence, max 16 words, starting with
  "You want", restating what they want to create and for what purpose. Otherwise an empty string.
If an earlier answer is vague or ambiguous and the "clarify" topic is available, prefer it: quote their vague
word and ask for something concrete.
Never use the words "prompt", "LLM" or "AI model". No exclamation marks.
Return ONLY JSON: {"summary": "", "field": "...", "question": "...", "why": "...", "suggestions": ["...","...","..."], "done": false}"""


def validate_q(remaining: list[str]):
    def v(d: dict):
        if not str(d.get("question", "")).strip() or not str(d.get("why", "")).strip():
            raise ValueError("missing question/why")
        if re.search(r"\bprompt", json.dumps(d), re.I):
            raise ValueError("used the word prompt")
        if d.get("field") not in remaining:
            d["field"] = remaining[0]
        s = [str(x).strip() for x in (d.get("suggestions") or []) if str(x).strip()]
        if len(s) < 3:
            raise ValueError("need 3 suggestions")
        d["suggestions"] = s[:3]
    return v


FALLBACK_ORDER = ["audience", "purpose", "key_details", "style", "avoid"]



# ---------------- plan ----------------
PLAN_SYSTEM = """You are a senior consultant. Turn a non-technical person's answers into
(1) a visual plan they understand at a glance, and
(2) complete, expert-quality instructions they will paste into ChatGPT or Claude.
Everything must be specific to THEIR request and domain. Nothing generic.

Return ONLY JSON with exactly these keys:
{
  "title": "short professional title of the deliverable, max 8 words, in Title Case",
  "deliverable": "one or two words, e.g. Poster, Resume, Budget, Lesson plan, Research summary, Letter",
  "best_tool": "chatgpt" or "claude",
  "tool_reason": "one short sentence. Choose chatgpt when the result needs image generation; otherwise choose claude for writing, planning and analysis",
  "steps": [ {"id": "s1", "label": "max 5 words", "detail": "one sentence, max 14 words, specific to them", "owner": "you" or "ai"} ],
  "choices": [ {"label": "Audience|Goal|Key details|Style|Avoid|Document", "value": "their choice in their own words, max 14 words"} ],
  "checks": ["3 to 5 concrete quality checks drawn from their answers, each max 10 words"],
  "changes": ["only when revising: each change made, max 12 words; otherwise an empty list"]
}

Steps: exactly 5 or 6, in order. Begin with 1 or 2 "you" steps naming their real decisions
(e.g. "Families in Park Slope chosen"), then 2 or 3 "ai" steps describing the real work for THIS deliverable
(e.g. "AI sorts expenses into categories", "AI designs the poster layout"), then "Check against your rules" (ai),
and end with a "you" step for review and use.

Brief (the most important field):
- Addressed to the AI assistant, second person, plain professional English, 180 to 320 words.
- Short headings: Goal, Audience, Key details, Style, Must avoid, Deliverable format, Before you finish.
  Add "Reference material" with the relevant facts if a document excerpt was provided.
- Use the person's exact facts. Never invent names, dates, prices or credentials. Where something essential is
  missing, instruct the assistant to ask one question before starting.
- Deliverable format states exactly what to produce (image, table, one-page document, slide outline, etc.).
- Before you finish: check the work against each Must avoid item and report the result.
- Do not mention this app. Do not use the word "prompt".

When a previous plan and a change request are provided, apply the change, keep everything else stable,
and list the changes.

Inside JSON string values never use double quotation marks; use single quotes instead.

OUTPUT FORMAT (strict): first the JSON object above (WITHOUT a brief key), then on a new line the full brief
wrapped exactly like this:
<brief>
...the instructions...
</brief>"""


def parse_plan_text(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    m = re.search(r"<brief>(.*?)(</brief>|$)", text, flags=re.S)
    brief = m.group(1).strip() if m else ""
    head = text[:m.start()] if m else text
    d = extract_json(head)
    if brief:
        d["brief"] = brief
    return d


def validate_plan(d: dict):
    for k in ["title", "deliverable", "steps", "choices", "checks", "brief"]:
        if not d.get(k):
            raise ValueError(f"plan missing {k}")
    if d.get("best_tool") not in ("chatgpt", "claude"):
        d["best_tool"] = "claude"
    d.setdefault("tool_reason", "")
    d.setdefault("changes", [])
    d["steps"] = d["steps"][:6]
    for i, s in enumerate(d["steps"]):
        s["id"] = s.get("id") or f"s{i+1}"
        s["owner"] = "you" if s.get("owner") == "you" else "ai"
        s.setdefault("detail", "")
    if len(d["brief"].split()) < 60:
        raise ValueError("brief too short")


LABEL_FOR = {"audience": "Audience", "purpose": "Goal", "key_details": "Key details", "style": "Style", "avoid": "Avoid"}


def fallback_plan(body: PlanIn) -> dict:
    """Deterministic plan from the person's raw answers, used only if no AI provider answers."""
    cat = categorize(body.request)
    thing = your_thing(body.request)
    ans = {t.field: t.answer for t in body.history if not is_idk(t.answer)}
    choices = [{"label": LABEL_FOR.get(f, f.title()), "value": a} for f, a in ans.items()]
    if body.upload_text:
        choices.append({"label": "Document", "value": "Your attached document is used as reference"})
    sec = lambda f, d: ans.get(f, d)  # noqa: E731
    draft_label, draft_detail = DRAFT_STEP[cat]
    visual = cat == "visual" or bool(re.search(r"\b(image|picture|logo|illustration)\b", body.request, re.I))
    extra = f"\n\nAdditional instruction from me: {body.change_request.strip()}" if body.change_request else ""
    ref = f"\n\nReference material\n{body.upload_text[:1200]}" if body.upload_text else ""
    brief = (f"I would like your help with the following: {body.request.strip()}.\n\n"
             f"Goal\n{sec('purpose', 'Make it clear, useful and effective for the people it is for.')}\n\n"
             f"Audience\n{sec('audience', 'Ask me who this is for before you begin.')}\n\n"
             f"Key details\n{sec('key_details', 'Ask me for any facts you need. Do not invent names, dates, prices or credentials.')}\n\n"
             f"Style\n{sec('style', 'Clear, warm and professional.')}\n\n"
             f"Must avoid\n{sec('avoid', 'Jargon, exaggerated claims and generic filler.')}{ref}\n\n"
             f"Deliverable format\nProduce {DELIVERABLE[cat]}.\n\n"
             "Before you finish\nCheck your result against every item under \"Must avoid\" and tell me, item by item, "
             "whether it passes. Use only the facts I have given you. If something essential is missing, ask me one "
             f"question before you start.{extra}")
    title = re.sub(r"^your\s+", "", thing).strip().title()[:60]
    return {
        "title": title or "Your Plan",
        "deliverable": cat.title() if cat != "general" else "Result",
        "best_tool": "chatgpt" if visual else "claude",
        "tool_reason": "ChatGPT can create images directly." if visual else "Claude is strong at careful writing and planning.",
        "steps": [
            {"id": "s1", "label": "Your audience chosen", "detail": (sec("audience", "Who this is for"))[:90], "owner": "you"},
            {"id": "s2", "label": "Your details and limits", "detail": (sec("key_details", "Facts only you can provide"))[:90], "owner": "you"},
            {"id": "s3", "label": draft_label, "detail": draft_detail, "owner": "ai"},
            {"id": "s4", "label": "Check against your rules", "detail": ("Avoids: " + sec("avoid", "anything you ruled out"))[:90], "owner": "ai"},
            {"id": "s5", "label": "You review and use it", "detail": "You approve the final result before anyone sees it.", "owner": "you"},
        ],
        "choices": choices,
        "checks": ([f"Avoids {ans['avoid']}"[:60]] if "avoid" in ans else []) + ["Uses only the facts you gave", "Matches your chosen style"],
        "brief": brief,
        "changes": [f"Applied your request: {body.change_request.strip()}"] if body.change_request else [],
    }


async def llm_text(system: str, user: str, budget: float, max_tokens: int) -> tuple[str, str]:
    """Plain-text call through the same provider chain (used for the draft preview)."""
    deadline = time.monotonic() + budget
    last = None
    for p in PROVIDERS["plan"]:
        left = deadline - time.monotonic()
        if left < 3:
            break
        try:
            if p.native_anthropic:
                txt = await asyncio.wait_for(call_anthropic(p.configured, system, user, max_tokens), timeout=left)
            else:
                r = await asyncio.wait_for(p.client.chat.completions.create(
                    model=await p.model(), temperature=0.6, max_tokens=max_tokens,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}]), timeout=left)
                txt = r.choices[0].message.content or ""
            txt = re.sub(r"<think>.*?</think>", "", txt, flags=re.S).strip()
            if txt:
                return txt, p.name
        except Exception as e:  # noqa: BLE001
            last = e
            log.warning("%s preview failed: %s", p.name, e)
    raise last or RuntimeError("no provider")


PREVIEW_SYSTEM = """You are the assistant receiving the instructions below. Produce a short FIRST DRAFT preview of the
deliverable so a non-technical person can see what they will get.
- Markdown only: a title (#), short sections (##), bullet lists, and a table if the deliverable is tabular.
- Maximum 300 words. Do not ask questions. Where a needed fact is missing, write [to be added].
- If the deliverable is an image or visual design, give the exact text that would appear on it and a
  short "Layout" section describing placement, colors and imagery.
- Never invent names, prices, dates or credentials that were not given."""


# ---------------- app ----------------
app = FastAPI(title="Ask Me First")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/api/health")
async def health():
    return {"ok": True, "demo_mode": DEMO_MODE,
            "providers": [{"name": p.name, "model": p.resolved or p.configured} for p in PROVIDERS["q"]],
            "has_pangram": bool(PANGRAM_API_KEY), "has_reducto": bool(REDUCTO_API_KEY)}


@app.post("/api/next_question", response_model=NextQuestionOut)
async def next_question(body: NextQuestionIn):
    if DEMO_MODE:
        await asyncio.sleep(DEMO_DELAY)
        qs = load_demo().get("questions", [])
        i = len(body.history)
        return NextQuestionOut(done=True, source="demo") if i >= len(qs) else NextQuestionOut(**qs[i], source="demo")

    asked = {t.field for t in body.history}
    remaining = [s for s in FALLBACK_ORDER if s not in asked]
    if len(body.history) >= MAX_QUESTIONS or not remaining:
        return NextQuestionOut(done=True, source="plan")
    offered = remaining + (["clarify"] if len(body.history) >= 2 and "clarify" not in asked else [])

    user = context_block(body.request, body.history, body.upload_text)
    user += f"\n\nQuestions answered so far: {len(body.history)} (maximum {MAX_QUESTIONS})."
    user += "\nAvailable topics:\n" + "\n".join(f"- {k}: {SLOTS[k]}" for k in offered)
    t0 = time.time()
    try:
        d, src = await llm_json("q", Q_SYSTEM, user, Q_TIMEOUT, 1200, validate_q(offered))
        done = bool(d.get("done")) and len(body.history) >= MIN_QUESTIONS
        summary = str(d.get("summary") or "").strip() if not body.history else ""
        out = NextQuestionOut(summary=summary, question=d["question"].strip(), why=d["why"].strip(), field=d["field"],
                              suggestions=d["suggestions"], done=done, source=src)
    except Exception as e:  # noqa: BLE001
        log.error("question fallback: %s", e)
        slot = remaining[0]
        q, w, s = fallback_question(body.request, slot)
        out = NextQuestionOut(question=q, why=w, field=slot, suggestions=s, source="fallback", error=str(e)[:300])
    log.info("next_question %s via %s in %.1fs", out.field, out.source, time.time() - t0)
    log_run("question", {"request": body.request, "history": [t.model_dump() for t in body.history], "out": out.model_dump()})
    return out


@app.post("/api/plan")
async def plan(body: PlanIn):
    if DEMO_MODE:
        await asyncio.sleep(DEMO_DELAY * 2)
        versions = load_demo().get("plans", [])
        idx = 0 if not body.previous_plan else min(int(body.previous_plan.get("_version", 1)), len(versions) - 1)
        p = dict(versions[idx])
        p.update(_version=idx + 1, _source="demo")
        return p

    user = context_block(body.request, body.history, body.upload_text)
    if body.previous_plan:
        prev = {k: v for k, v in body.previous_plan.items() if not k.startswith("_")}
        user += "\n\nPrevious plan:\n" + json.dumps(prev, ensure_ascii=False)
        user += f'\n\nChange requested by the person: "{(body.change_request or "").strip()}"'
    version = int((body.previous_plan or {}).get("_version", 0)) + 1
    t0 = time.time()
    try:
        p, src = await llm_json("plan", PLAN_SYSTEM, user, PLAN_TIMEOUT, 4000, validate_plan)
        p["_source"] = src
    except Exception as e:  # noqa: BLE001
        log.error("plan fallback: %s", e)
        p = fallback_plan(body)
        p["_source"] = "fallback"
        p["_error"] = str(e)[:300]
    p["_version"] = version
    log.info("plan v%d via %s in %.1fs", version, p["_source"], time.time() - t0)
    log_run("plan", {"request": body.request, "change": body.change_request, "out": p})
    return p


@app.post("/api/preview")
async def preview(body: PreviewIn):
    if DEMO_MODE:
        await asyncio.sleep(DEMO_DELAY * 1.5)
        return {"markdown": load_demo().get("preview", "# Preview\n\nA preview is not available in demo mode."), "source": "demo"}
    try:
        md, src = await llm_text(PREVIEW_SYSTEM, f"Original request: {body.request}\n\nInstructions:\n{body.brief}", 40, 1500)
        log_run("preview", {"request": body.request, "source": src})
        return {"markdown": md, "source": src}
    except Exception as e:  # noqa: BLE001
        log.error("preview failed: %s", e)
        return JSONResponse({"error": "A preview could not be created right now. You can still open the instructions in ChatGPT or Claude."}, status_code=502)


@app.post("/api/voice_check")
async def voice_check(body: VoiceIn):
    """Pangram reads the person's OWN combined answers, never the AI output, only at 50+ words.
    The UI never blocks on it."""
    words = len(body.text.split())
    if words < PANGRAM_MIN_WORDS:
        return {"eligible": False, "words": words}
    if DEMO_MODE:
        await asyncio.sleep(0.6)
        return load_demo().get("voice_check", {"eligible": True, "ai_like": False, "label": "Human"})
    if not PANGRAM_API_KEY:
        return {"eligible": False, "words": words, "error": "no key"}

    def run():
        from pangram import Pangram
        c = Pangram(api_key=PANGRAM_API_KEY)
        try:
            return c.predict(body.text, model="pangram-4")
        except TypeError:
            return c.predict(body.text)

    try:
        r = await asyncio.wait_for(asyncio.to_thread(run), timeout=20)
        get = (lambda k: r.get(k)) if isinstance(r, dict) else (lambda k: getattr(r, k, None))
        short = str(get("prediction_short") or "")
        headline = str(get("headline") or "")
        ai_like = short.lower() in ("ai", "mixed") or ("ai" in headline.lower() and "human" not in headline.lower())
        return {"eligible": True, "words": words, "label": short, "headline": headline,
                "fraction_human": get("fraction_human"), "ai_like": ai_like}
    except Exception as e:  # noqa: BLE001
        log.error("pangram failed: %s", e)
        return {"eligible": False, "words": words, "error": str(e)[:200]}


@app.post("/api/parse_upload")
async def parse_upload(file: UploadFile = File(...)):
    """Reducto reads an existing document so the questions and instructions build on it."""
    if DEMO_MODE:
        await asyncio.sleep(1.2)
        t = load_demo().get("upload_text", "")
        return {"text": t, "preview": t[:220], "source": "demo"}
    if not REDUCTO_API_KEY:
        return JSONResponse({"error": "Document reading is not configured."}, status_code=503)
    suffix = Path(file.filename or "upload").suffix or ".pdf"
    data = await file.read()

    def run():
        from reducto import Reducto
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            path = Path(tmp.name)
        c = Reducto(api_key=REDUCTO_API_KEY)
        up = c.upload(file=path)
        try:
            res = c.parse.run(input=getattr(up, "file_id", up))
        except TypeError:
            res = c.parse.run(document_url=up)
        chunks = getattr(getattr(res, "result", None), "chunks", None) or []
        return "\n\n".join(getattr(ch, "content", "") for ch in chunks).strip()

    try:
        text = await asyncio.wait_for(asyncio.to_thread(run), timeout=45)
        if not text:
            return JSONResponse({"error": "No readable text was found in this document."}, status_code=422)
        preview = re.sub(r"\s+", " ", text)[:220]
        return {"text": text[:6000], "preview": preview, "source": "reducto"}
    except Exception as e:  # noqa: BLE001
        log.error("reducto failed: %s", e)
        return JSONResponse({"error": "The document could not be read. You can continue without it."}, status_code=502)


def _frontend() -> Optional[Path]:
    for p in [ROOT.parent / "frontend" / "index.html", ROOT / "frontend" / "index.html"]:
        if p.exists():
            return p
    return None


@app.get("/")
async def index():
    p = _frontend()
    return FileResponse(p) if p else JSONResponse({"error": "frontend/index.html not found"}, status_code=404)
