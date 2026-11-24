"""Minimal client for free LLM tiers (OpenRouter + Groq) with automatic fallback.

Keys come only from env vars OPENROUTER_API_KEY / GROQ_API_KEY (GitHub Secrets).
Keys are never printed.
"""
import json, os, time, urllib.request, urllib.error

PROVIDERS = {
    "openrouter": {"base": "https://openrouter.ai/api/v1", "env": "OPENROUTER_API_KEY"},
    "groq": {"base": "https://api.groq.com/openai/v1", "env": "GROQ_API_KEY"},
}
GROQ_PREFERRED = ["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "moonshotai/kimi-k2-instruct"]


def _req(url, key=None, body=None, timeout=90):
    headers = {"Content-Type": "application/json", "User-Agent": "proteinpro-llm-worker"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read())


def key_for(provider):
    return os.environ.get(PROVIDERS[provider]["env"], "").strip()


def free_models(provider):
    """Return list of free model ids for a provider (best first)."""
    key = key_for(provider)
    base = PROVIDERS[provider]["base"]
    if provider == "openrouter":
        data = _req(f"{base}/models")["data"]
        free = [m for m in data
                if str(m.get("pricing", {}).get("prompt")) == "0"
                and str(m.get("pricing", {}).get("completion")) == "0"
                and "text" in (m.get("architecture", {}).get("output_modalities") or ["text"])]
        free.sort(key=lambda m: -(m.get("context_length") or 0))
        return [m["id"] for m in free]
    if provider == "groq":
        ids = [m["id"] for m in _req(f"{base}/models", key)["data"]]
        pref = [m for m in GROQ_PREFERRED if m in ids]
        return pref + [m for m in ids if m not in pref and not any(x in m for x in ("whisper", "tts", "guard", "playai"))]
    return []


def chat(provider, model, messages, temperature=0.3, max_tokens=1500):
    key = key_for(provider)
    body = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    t = time.time()
    out = _req(f"{PROVIDERS[provider]['base']}/chat/completions", key, body)
    return out["choices"][0]["message"]["content"], round(time.time() - t, 1)


def chat_fallback(chain, messages, **kw):
    """chain = [(provider, model), ...]; tries each until one succeeds."""
    errors = []
    for provider, model in chain:
        try:
            text, sec = chat(provider, model, messages, **kw)
            if text and text.strip():
                return {"provider": provider, "model": model, "text": text, "sec": sec, "errors": errors}
            errors.append(f"{provider}/{model}: empty")
        except urllib.error.HTTPError as e:
            errors.append(f"{provider}/{model}: HTTP {e.code}")
        except Exception as e:  # noqa
            errors.append(f"{provider}/{model}: {type(e).__name__}")
    return {"provider": None, "model": None, "text": None, "errors": errors}
