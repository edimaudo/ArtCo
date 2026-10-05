# ArtCo

An agentic art discovery & Concierge System built using Qloo & Gemini.

## Product model

Six user jobs share one concierge:

- Discover
- Find
- Develop my taste
- Curate
- Buy
- Keep discovering

The user gives a compact brief. Qloo resolves broader cultural inputs and returns artist affinities. The LangGraph workflow converts that context into search directions. Artwork providers then supply actual works.
For purchasing requests, commercial providers are searched first. Institutional sources are used for fallback discovery and clearly labeled **NOT FOR SALE**.

## Stack

- FastAPI
- Jinja2
- Vanilla CSS + JS
- Qloo API
- Optional Gemini SDK


## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# set QLOO_API_KEY
uvicorn main:app --reload
```

Open http://127.0.0.1:8000

