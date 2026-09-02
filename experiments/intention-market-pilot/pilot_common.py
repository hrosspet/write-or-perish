"""Shared helpers for the intention-market pilot (lab.loore.org, 2026-09-02).

Runs inside the lab checkout with the app's env loaded:

    cd ~/write-or-perish && set -a && . ./.env.production && set +a
    PYTHONPATH=. python ~/lab-scripts/pilot_select.py

Outputs live outside git in PILOT_OUT (default ~/lab-out). Nothing with
content is committed — see the privacy note in the research summary.
"""
import json
import os
import pathlib
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(os.environ.get("PILOT_ROOT", os.path.expanduser("~/write-or-perish")))
OUT = pathlib.Path(os.environ.get("PILOT_OUT", os.path.expanduser("~/lab-out")))
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
# Intentions "frame": which extraction prompt produced the lists. Each frame
# keeps its own raw/agg/match dirs so runs never collide; the spend ledger and
# accounts.json are shared.
FRAME = os.environ.get("PILOT_FRAME", "default")
_sfx = "" if FRAME == "default" else f"_{FRAME}"
RAW_DIR = OUT / f"raw{_sfx}"
AGG_DIR = OUT / f"agg{_sfx}"
MATCH_DIR = OUT / f"match{_sfx}"

LUNA = "gpt-5.6-luna"
OPUS = "claude-opus-4.8"
# $/MTok (input, output). luna bills 2x/1.5x above 272K input tokens.
PRICE = {LUNA: (0.20, 1.20), OPUS: (5.00, 25.00)}
# Stop below the agreed caps ($50 OpenAI / $20 Anthropic for the night).
LIMITS = {"openai": 45.0, "anthropic": 18.0}


def app():
    from backend.app import create_app
    return create_app()


def api_keys(flask_app):
    from backend.utils.api_keys import get_api_keys_for_usage
    from backend.utils.llm_batch import apply_batch_key_override
    return apply_batch_key_override(
        get_api_keys_for_usage(flask_app.config, "chat"), flask_app.config)


def provider_of(model):
    return "openai" if model.startswith("gpt") else "anthropic"


def jload(path, default=None):
    p = pathlib.Path(path)
    return json.loads(p.read_text()) if p.exists() else default


def jdump(path, obj):
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str))
    tmp.replace(p)


def append_jsonl(path, obj):
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(obj, default=str) + "\n")


_enc = None


def o200k_len(text):
    global _enc
    if _enc is None:
        import tiktoken
        _enc = tiktoken.get_encoding("o200k_base")
    return len(_enc.encode_ordinary(text))


# ---- spend ledger (USD per provider) ---------------------------------------
_lock = threading.Lock()


def record_spend(model, in_tok, out_tok, note="", batch=False):
    pi, po = PRICE[model]
    if model == LUNA and (in_tok or 0) > 272_000:
        pi, po = pi * 2, po * 1.5
    usd = ((in_tok or 0) * pi + (out_tok or 0) * po) / 1e6
    if batch:
        usd *= 0.5
    with _lock:
        led = jload(OUT / "spend.json", {"openai": 0.0, "anthropic": 0.0, "calls": 0})
        led[provider_of(model)] = round(led[provider_of(model)] + usd, 5)
        led["calls"] += 1
        jdump(OUT / "spend.json", led)
        append_jsonl(OUT / "spend.jsonl", {
            "t": time.time(), "model": model, "in": in_tok, "out": out_tok,
            "usd": round(usd, 5), "batch": batch, "note": note})
    return usd, led


def spend_ok(model):
    led = jload(OUT / "spend.json", {"openai": 0.0, "anthropic": 0.0})
    return led[provider_of(model)] < LIMITS[provider_of(model)]


def complete(flask_app, keys, model, prompt, max_tokens, note="", retries=2):
    """One sync completion through the app's provider layer.
    Returns the provider dict plus elapsed seconds; records spend."""
    import backend.llm_providers as lp
    from backend.llm_providers import LLMProvider
    # The app clamps output to its DEFAULT_MAX_OUTPUT_TOKENS (10K); exhaustive
    # matching needs more, and a silent clamp is exactly the anchor we want to
    # avoid. Lift the module constant for this process only.
    if max_tokens and max_tokens > getattr(lp, "DEFAULT_MAX_OUTPUT_TOKENS", 0):
        lp.DEFAULT_MAX_OUTPUT_TOKENS = max_tokens
    if not spend_ok(model):
        raise RuntimeError(f"spend cap reached for {provider_of(model)} — stopping")
    messages = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
    last = None
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            # Worker threads have no Flask context; the provider layer reads
            # current_app.config, so push one per call.
            with flask_app.app_context():
                r = LLMProvider.get_completion(model, messages, keys, max_tokens=max_tokens)
            r["elapsed"] = round(time.time() - t0, 1)
            record_spend(model, r.get("input_tokens", 0), r.get("output_tokens", 0), note)
            return r
        except Exception as e:  # noqa: BLE001 — retry anything transient
            last = e
            print(f"  !! {note}: {type(e).__name__}: {str(e)[:160]} (attempt {attempt + 1})",
                  flush=True)
            time.sleep(5 * (attempt + 1))
    raise last


def pmap(fn, items, workers=4):
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, items))


# ---- intentions markdown parser -------------------------------------------
def parse_blocks(text):
    """'# Endorsed' / '# Inferred' sections holding '## title' blocks →
    [{group, title, status, body, text}]."""
    blocks, group, cur = [], None, None
    for line in (text or "").splitlines():
        s = line.rstrip()
        if s.startswith("# "):
            group = s[2:].strip().rstrip(":")
            cur = None
            continue
        if s.startswith("## "):
            cur = {"group": group, "title": s[3:].strip(), "status": "",
                   "body": [], "text": [s]}
            blocks.append(cur)
            continue
        if cur is None:
            continue
        cur["text"].append(s)
        st = s.strip()
        if not st:
            continue
        if not cur["status"] and st.startswith("*") and st.endswith("*"):
            cur["status"] = st.strip("*").strip()
            continue
        cur["body"].append(st)
    for b in blocks:
        b["text"] = "\n".join(b["text"]).strip()
        b["body"] = "\n".join(b["body"]).strip()
    return blocks


def accounts():
    return jload(OUT / "accounts.json", [])


def user_for(handle):
    from backend.models import User
    return User.query.filter_by(prefilled_handle=handle).first()
