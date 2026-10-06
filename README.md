# ArtCo

An art discovery & Concierge.

## Product Design

Six user jobs share one concierge:

- Discover
- Find
- Develop my taste
- Curate
- Buy
- Keep discovering

The user gives a compact brief. Qloo resolves broader cultural inputs and returns artist affinities. 
For purchasing requests, commercial providers are searched first. Institutional sources are used for fallback discovery and clearly labeled **NOT FOR SALE**.

## Stack

- **Backend**: FastAPI
- **Front-end**: Jinja2 + Vanilla CSS + JS
- **AI*: Qloo API + Gemini SDK


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

