"""Audit UA vs RU names/descriptions in the Rozetka feed.

Layer 1: deterministic checks (numbers mismatch, Russian letters in UA, length ratio).
Layer 2: two independent free LLMs review each pair and return JSON issues.
Output: llm/out/audit_<tag>.json  (Claude then reviews every flagged item).

Usage: python llm/audit_ua.py --n 50 --seed 1 [--keys path.env] [--out tag]
"""
import argparse, html, json, os, random, re, sys, time, urllib.error
import xml.etree.ElementTree as ET
sys.path.insert(0, os.path.dirname(__file__))
from client import chat

HERE = os.path.dirname(os.path.abspath(__file__))
FEED = os.path.join(HERE, "..", "feed", "rozetka_feed.xml")
RU_ONLY = re.compile(r"[ыэъЫЭЪёЁ]")
NUM = re.compile(r"\d+(?:[.,]\d+)?")

REVIEWERS = [
    ("groq", "openai/gpt-oss-120b"),
    ("groq", "qwen/qwen3.8-27b"),
]
BACKUP = [("openrouter", "google/gemma-4-31b-it:free"), ("openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free"),
          ("openrouter", "openrouter/free")]

SYSTEM = (
    "Ти суворий редактор інтернет-магазину спортивного харчування. Порівняй російський оригінал і український "
    "переклад картки товару. Шукай ТІЛЬКИ реальні помилки: 1) факт розходиться (смак, вага/об'єм, кількість порцій, "
    "розмір, колір, склад, цифри); 2) пропущений або доданий зміст; 3) суржик, русизми, російські слова чи літери; "
    "4) граматичні помилки; 5) неправильний переклад терміна. Стилістичні дрібниці, які не є помилкою, НЕ зазначай. "
    "Відповідай ЛИШЕ JSON без markdown: {\"ok\": true|false, \"issues\": [{\"type\": \"fact|missing|surzhyk|grammar|term\", "
    "\"ua\": \"точна цитата з перекладу\", \"fix\": \"як має бути\", \"why\": \"коротко\"}]}"
)


def clean(s, limit):
    s = html.unescape(s or "")
    s = re.sub(r"<li[^>]*>", "\n- ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*", "\n", s).strip()
    return s[:limit]


def det_checks(o):
    issues = []
    ru = o["name"] + "\n" + o["desc"]
    ua = o["name_ua"] + "\n" + o["desc_ua"]
    for field, text in (("name_ua", o["name_ua"]), ("description_ua", o["desc_ua"])):
        for m in RU_ONLY.finditer(text):
            a = max(0, m.start() - 25)
            issues.append({"type": "ru_letter", "field": field, "ua": text[a:m.end() + 25]})
            break
    nr = sorted(NUM.findall(o["name"].replace(",", ".")))
    nu = sorted(NUM.findall(o["name_ua"].replace(",", ".")))
    if nr != nu:
        issues.append({"type": "name_numbers", "ru": nr, "ua": nu})
    dr, du = set(NUM.findall(o["desc"])), set(NUM.findall(o["desc_ua"]))
    if dr - du:
        issues.append({"type": "desc_numbers_missing_in_ua", "nums": sorted(dr - du)[:15]})
    if du - dr:
        issues.append({"type": "desc_numbers_extra_in_ua", "nums": sorted(du - dr)[:15]})
    if o["desc"] and len(o["desc_ua"]) < 0.75 * len(o["desc"]):
        issues.append({"type": "ua_much_shorter", "ru_len": len(o["desc"]), "ua_len": len(o["desc_ua"])})
    return issues


def parse_json(text):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0)) if m else None


def ask(provider, model, msgs):
    for attempt in range(4):
        try:
            text, sec = chat(provider, model, msgs, temperature=0.1, max_tokens=2500)
            res = parse_json(text)
            return res, sec, (None if res is not None else "no_json")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = float(e.headers.get("retry-after") or 0) or 15 * (attempt + 1)
                time.sleep(min(wait, 60))
                continue
            return None, 0, f"HTTP {e.code}"
        except json.JSONDecodeError:
            return None, 0, "bad_json"
        except Exception as e:  # noqa
            return None, 0, type(e).__name__
    return None, 0, "rate_limited"


def review(o, reviewer):
    user = (f"RU назва: {o['name']}\nUA назва: {o['name_ua']}\n\nRU опис:\n{o['desc']}\n\nUA опис:\n{o['desc_ua']}")
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
    for prov, model in [reviewer] + BACKUP:
        res, sec, err = ask(prov, model, msgs)
        if res is not None:
            return {"model": model, "sec": sec, **res}
    return {"model": None, "error": err}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--keys")
    ap.add_argument("--out", default="sample")
    ap.add_argument("--ids", help="comma-separated offer ids")
    a = ap.parse_args()
    if a.keys:
        from load_keys import load
        load(a.keys)
    root = ET.parse(FEED).getroot()
    offers = []
    for e in root.iter("offer"):
        offers.append({"id": e.get("id"), "cat": e.findtext("categoryId"),
                       "name": (e.findtext("name") or "").strip(), "name_ua": (e.findtext("name_ua") or "").strip(),
                       "desc": clean(e.findtext("description"), 3500), "desc_ua": clean(e.findtext("description_ua"), 3500)})
    if a.ids:
        want = set(a.ids.split(","))
        sample = [o for o in offers if o["id"] in want]
    else:
        random.seed(a.seed)
        sample = random.sample(offers, min(a.n, len(offers)))
    os.makedirs(os.path.join(HERE, "out"), exist_ok=True)
    path = os.path.join(HERE, "out", f"audit_{a.out}.json")
    results = json.load(open(path)) if os.path.exists(path) else []
    done = {r["id"] for r in results}
    for i, o in enumerate(sample, 1):
        if o["id"] in done:
            continue
        r = {"id": o["id"], "name": o["name"], "name_ua": o["name_ua"], "det": det_checks(o), "llm": []}
        for rev in REVIEWERS:
            r["llm"].append(review(o, rev))
        flagged = bool(r["det"]) or any(x.get("issues") for x in r["llm"])
        r["flagged"] = flagged
        results.append(r)
        json.dump(results, open(path, "w"), ensure_ascii=False, indent=1)
        print(f"[{i}/{len(sample)}] {o['id']} flagged={flagged} "
              + " | ".join(f"{(x.get('model') or 'ERR').split('/')[-1]}:{len(x.get('issues') or [])}" for x in r["llm"]),
              flush=True)
    print("saved", path, "flagged", sum(r["flagged"] for r in results), "/", len(results))


if __name__ == "__main__":
    main()
