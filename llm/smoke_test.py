"""Connectivity test: checks both keys and a few free models. Writes llm/out/smoke.json."""
import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from client import key_for, free_models, chat
import urllib.error

PROMPT = [{"role": "user", "content": "Переведи на украинский одним предложением: 'Сывороточный протеин со вкусом шоколада, 1 кг.' Ответь только переводом."}]
report = {}
for p in ("openrouter", "groq"):
    r = {"key_present": bool(key_for(p))}
    try:
        models = free_models(p)
        r["free_models_count"] = len(models)
        r["free_models"] = models[:25]
        tests = []
        for m in models[:4]:
            try:
                text, sec = chat(p, m, PROMPT, max_tokens=200)
                tests.append({"model": m, "ok": True, "sec": sec, "answer": text.strip()[:300]})
            except urllib.error.HTTPError as e:
                tests.append({"model": m, "ok": False, "error": f"HTTP {e.code}: {e.read()[:200].decode('utf-8','replace')}"})
            except Exception as e:
                tests.append({"model": m, "ok": False, "error": type(e).__name__})
        r["tests"] = tests
    except urllib.error.HTTPError as e:
        r["error"] = f"HTTP {e.code}"
    except Exception as e:
        r["error"] = type(e).__name__
    report[p] = r

os.makedirs("llm/out", exist_ok=True)
json.dump(report, open("llm/out/smoke.json", "w"), ensure_ascii=False, indent=2)
print(json.dumps(report, ensure_ascii=False, indent=2))
