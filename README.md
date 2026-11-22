# Ask Me First

Most people do not know how to talk to AI. Ask Me First interviews you with a few
short question cards, shows you a visual plan of how the AI will make what you want,
lets you revise it, and then opens ChatGPT or Claude with expert instructions built
from your own words.

Built for the Non-Slop Hackathon (Pangram, Nous Research, Reducto, Modal).

## Run locally
    cd backend
    pip install -r requirements.txt
    cp .env.example .env        # add NOUS_API_KEY, PANGRAM_API_KEY, REDUCTO_API_KEY
    python smoke_test.py        # checks all three APIs, prints Hermes model IDs
    uvicorn main:app --reload --port 8000
    open http://localhost:8000

Offline/stage backup: set DEMO_MODE=1 in .env (replays a recorded run).

## Deploy (Modal)
    modal setup
    modal deploy backend/modal_app.py

## How each tool is used
- Hermes 4 (Nous Portal API): writes each question, the three tailored suggestions,
  the visual plan, and the final instructions (strict JSON).
- Pangram: reads only the person's own combined answers (50+ words) and shows a quiet
  "in your own words" badge or a polite note. Never blocks, never scores the AI output.
- Reducto: parses an existing document (old flyer, resume, form) so questions build on it.
- Modal: hosts the app.
