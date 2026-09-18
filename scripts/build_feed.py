#!/usr/bin/env python3
"""
Rozetka feed builder.

Pulls the seller's own Prom.ua YML feed + the supplier's YML feed,
re-prices everything for Rozetka's real (progressive, bracket-based)
commission schedule, cleans descriptions, maps categories, and writes
a single Rozetka-compatible YML file.

Run manually:   python build_feed.py
Run in CI:      see .github/workflows/update-feed.yml
Config:         all URLs/secrets come from environment variables (see
                README.md) so nothing sensitive lives in this file.
"""
import os
import re
import io
import math
import json
import html
from datetime import datetime
from urllib.request import Request, urlopen

from lxml import etree

# ---------------------------------------------------------------
# Код_товару -> Ідентифікатор_товару translation table. The seller's
# live Prom YML feed uses vendorCode = zero-padded "Код_товару" (Prom's
# own internal id), while the supplier feed keys on "Ідентифікатор_
# товару" (the hyphenated article, e.g. "00945-01") - two different ID
# spaces that don't convert into each other mathematically. This table
# bridges them; rebuild it (see references/README) whenever it goes
# stale (new products added after the last xlsx export won't resolve
# until the table is refreshed).
# ---------------------------------------------------------------
_MAPPING_PATH = os.path.join(os.path.dirname(__file__), "..", "references", "code_to_article.json")
try:
    with open(_MAPPING_PATH, encoding="utf-8") as _f:
        CODE_TO_ARTICLE = json.load(_f)
except FileNotFoundError:
    CODE_TO_ARTICLE = {}

# ---------------------------------------------------------------
# Config (from environment / GitHub Actions secrets)
# ---------------------------------------------------------------
PROM_FEED_URL = os.environ.get("PROM_FEED_URL", "")
SUPPLIER_FEED_URL = os.environ.get("SUPPLIER_FEED_URL", "")
OUTPUT_PATH = os.environ.get("OUTPUT_PATH", "feed/rozetka_feed.xml")
SHOP_NAME = os.environ.get("SHOP_NAME", "ProteinPlus")
SHOP_URL = os.environ.get("SHOP_URL", "https://proteinpro.prom.ua/")

TARGET_MARGIN = 0.05
K_PROM = (1 + TARGET_MARGIN) / ((1 - 0.16) * (1 - 0.1889))  # for back-solving implied cost from a Prom price

# ---------------------------------------------------------------
# Rozetka's real commission brackets (already VAT-inclusive), from
# the seller's official tariff sheet. Confirmed 2026-09-13. Re-verify
# periodically - Rozetka can change these without much notice.
# ---------------------------------------------------------------
BADY_BRACKETS = [(0, 499, 0.27), (500, 999, 0.2376), (1000, 1999, 0.1944),
                 (2000, 3999, 0.1404), (4000, 5999, 0.108), (6000, 10**9, 0.0864)]
ACC_BRACKETS = [(0, 999, 0.27), (1000, 2999, 0.1944), (3000, 9999, 0.1296), (10000, 10**9, 0.0756)]
FIT_BRACKETS = [(0, 1999, 0.2268), (2000, 4999, 0.1944), (5000, 9999, 0.1296), (10000, 10**9, 0.0756)]

# ---------------------------------------------------------------
# Category mapping: Prom category name (as it appears in the seller's
# own Prom YML <categories> block) -> Rozetka leaf category id.
# Extend this dict whenever a new Prom category needs a home.
# ---------------------------------------------------------------
DIRECT_CATEGORY_MAP = {
    "Креатин": "273293",
    "Протеины": "273294",
    "Протеиновые, энергетические батончики, зерновые мюсли": "273294",
    "Жиросжигатели": "273296",
    "Гейнеры": "273297",
    "Витамины и Минералы": "274789",
    "Аминокислоты": "273295",
    "Bcaa": "273295",
    "Энергетики и изотоники": "299356",
    "No, предтренировочники": "341460",
    "Для повышение тестостерона": "4653731",
    "Заменители питания": "4653703",
    # Intentionally NOT mapped (confirmed with seller 2026-09-15):
    # "Уценка спортивного питания", "Спортивное питание на развес",
    # "Hi-tech pharma", "Cla - конъюгированная линолевая к-та",
    # "Глютамин", "Препараты для суставов и связок", "Карбо (углеводы)",
    # "Постренировочные комплексы и специальные препараты" - seller
    # confirmed these 6 categories are not needed at all (likely empty
    # shell categories with 0 real products; no offer using their
    # categoryIds was found in the portion of the live feed that could
    # be inspected).
    # "Американские PURCHASEPEPTIDES", "Zhengzhou Pharmaceutical Co Ltd",
    #   "Пептиды (инъекционная форма)", "Фактор роста" - injectable
    #   peptides/research chemicals, seller does not sell these on
    #   Rozetka at all (likely separate regulatory requirements from
    #   ordinary dietary supplements). Do not add without an explicit
    #   decision to do so.
}
BADY_CATS = {"273293", "273294", "273295", "273296", "273297", "274390", "274789",
             "299356", "341460", "4653703", "4653717", "4653724", "4653731"}
ACCESSORY_CATS = {"4624914", "4627638", "4653745", "4653773", "4656294",
                   "4669267", "4669333", "4669351", "4670126"}
FITNESS_CATS = {"159461"}

ROZETKA_CATEGORY_NAMES = {
    "273293": "Креатин", "273294": "Протеїн", "273295": "Амінокислоти",
    "273296": "Жироспалювачі", "273297": "Гейнери", "274390": "Натуральні добавки і екстракти",
    "274789": "Вітаміни та мінерали", "299356": "Ізотоніки", "341460": "Передтренувальні комплекси",
    "4653703": "Замінники харчування", "4653717": "Риб'ячий жир та омега",
    "4653724": "Пробіотики та пребіотики", "4653731": "Стимулятори тестостерону",
    "4624914": "Пояси та рукавички", "4627638": "Термоси", "4653745": "Пляшки для води",
    "4653773": "Таблетниці", "4656294": "Спортивні шейкери", "4669267": "Еспандери",
    "4669333": "Лямки, гачки, накладки", "4669351": "Спортивна магнезія", "4670126": "Бинти",
    "159461": "Йога",
}


def classify_active_longevity(name):
    n = name.lower()
    if re.search(r"омега|omega|рыбий жир|риб.ячий жир|fish oil", n):
        return "4653717"
    if re.search(r"пробиотик|пробіотик|prebiot|пребиотик|пребіотик|lactobac|bifidobac", n):
        return "4653724"
    if re.search(r"витамин|вітамін|vitamin|мультивитамин|мультивітамін|цинк|zinc|магни|магні|"
                 r"кальций|кальцій|calcium|железо|залізо|iron\b", n):
        return "274789"
    return "274390"


def classify_accessory(name):
    n = name.lower()
    if re.search(r"бутыл|пляшк|gallon|галлон", n):
        return "4653745"
    if re.search(r"шейкер|shaker", n):
        return "4656294"
    if re.search(r"рукавич|перчат|glove|пояс тяжелоатлет|belt\b", n):
        return "4624914"
    if re.search(r"бинт", n):
        return "4670126"
    if re.search(r"лямк|гачк|hook|манжет|тяга для шеи|петл[іи] береш", n):
        return "4669333"
    if re.search(r"магнези|магнезі|chalk", n):
        return "4669351"
    if re.search(r"эспандер|еспандер|резинка для фитнеса|power band|loop band|fitness band", n):
        return "4669267"
    if re.search(r"таблетниц|pillbox|pill box", n):
        return "4653773"
    if re.search(r"термофляг|термос|thermo|food storage|контейнер для еды", n):
        return "4627638"
    if re.search(r"йога|yoga|массажный мяч|массажный ролик|коврик|чехол для коврика", n):
        return "159461"
    return None


def assign_rozetka_category(prom_category_name, product_name):
    if prom_category_name in DIRECT_CATEGORY_MAP:
        return DIRECT_CATEGORY_MAP[prom_category_name]
    if "долголет" in prom_category_name.lower():
        return classify_active_longevity(product_name)
    if "аксессуар" in prom_category_name.lower() or "перчат" in prom_category_name.lower():
        return classify_accessory(product_name)
    return None


# ---------------------------------------------------------------
# Characteristic inference (real Rozetka filter values where we've
# verified them; generic fallbacks otherwise)
# ---------------------------------------------------------------
FORM_PATTERNS = [
    (re.compile(r"жевательн|жувальн", re.I), "Жувальні таблетки"),
    (re.compile(r"шипуч", re.I), "Шипучі таблетки"),
    (re.compile(r"капсул", re.I), "Капсули"),
    (re.compile(r"таблет", re.I), "Таблетки"),
    (re.compile(r"\bshot\b|гель|\bмл\b|\bл\b(?!\w)", re.I), "Рідина"),
    (re.compile(r"батончик", re.I), "Батончик"),
    (re.compile(r"порошок|\bг\b|\bкг\b", re.I), "Порошок"),
]
CREATINE_TYPE_PATTERNS = [
    (re.compile(r"гидрохлорид|гідрохлорид|\bHCL\b|\bHCl\b", re.I), "Креатин гідрохлорид"),
    (re.compile(r"малат|\bTCM\b|tri.?creatine", re.I), "Трикреатинмалат"),
    (re.compile(r"ph.?x|буфериз|kre.?alkalyn", re.I), "Буферний креатин"),
    (re.compile(r"комплекс|kick|xplode|crea.?bomb|crea.?zero|effervescent|sport\b|creaport|gummies|chews", re.I), "Креатинова матриця"),
    (re.compile(r"моногидрат|моногідрат|monohydrate|creapure", re.I), "Креатин моногідрат"),
]
PROTEIN_TYPE_PATTERNS = [
    (re.compile(r"мицелляр|міцелярн|micellar", re.I), "Міцелярний казеїн"),
    (re.compile(r"казеин|казеїн|casein", re.I), "Казеїновий"),
    (re.compile(r"соев|соєв|\bsoy\b", re.I), "Соєвий"),
    (re.compile(r"яичн|яєчн|\begg\b", re.I), "Яєчний"),
    (re.compile(r"говяж|яловичий|\bbeef\b", re.I), "Яловичий"),
    (re.compile(r"горохов|\bpea\b", re.I), "Гороховий"),
    (re.compile(r"рисов|\brice\b", re.I), "Рисовий"),
    (re.compile(r"конопл|hemp", re.I), "Конопляний"),
    (re.compile(r"сывороточ|сироватков|\bwhey\b", re.I), "Сироватковий"),
]
PROTEIN_PURITY_PATTERNS = [
    (re.compile(r"гидролизат|гідролізат|hydrolysate|hydrolyzed", re.I), "Гідролізат"),
    (re.compile(r"изолят|ізолят|isolate", re.I), "Ізолят"),
    (re.compile(r"концентрат|concentrate", re.I), "Концентрат"),
    (re.compile(r"комплекс|matrix|power|blend|combo", re.I), "Комбінована"),
]
PROTEIN_ORIGIN_ANIMAL = re.compile(
    r"сывороточ|сироватков|whey|казеин|казеїн|casein|говяж|яловичий|beef|яичн|яєчн|egg", re.I)
PROTEIN_ORIGIN_PLANT = re.compile(
    r"веган|vegan|соев|соєв|soy|горохов|pea|рисов|rice|конопл|hemp", re.I)
GAINER_TYPE_PATTERNS = [
    (re.compile(r"carbo|waxy|dextrin|instant oats|овсянк", re.I), "Високовуглеводні гейнери"),
]
FATBURNER_TYPE_PATTERNS = [
    (re.compile(r"l-carnitine|л-карнитин|карнітин", re.I), "L-карнітин"),
    (re.compile(r"\bcla\b|конъюгирован|кон'югован", re.I), "Кон'югована лінолева кислота"),
    (re.compile(r"термоген|thermogenic|caffeine.*fat|fat.*burn", re.I), "Термогеніки"),
    (re.compile(r"диуретик|діуретик|diuretic|water\s?loss", re.I), "Діуретики"),
]
MAGNESIA_FORM_PATTERNS = [
    (re.compile(r"брикет|block", re.I), "Брикети"),
    (re.compile(r"жидк|рідк|liquid", re.I), "Рідка"),
    (re.compile(r"шар|ball|куля", re.I), "Куля"),
    (re.compile(r"гель|gel", re.I), "Гель"),
    (re.compile(r"порошок|рассыпн|розсипн|powder", re.I), "Порошок"),
]
EXPANDER_TYPE_PATTERNS = [
    (re.compile(r"резинка для фитнеса|фітнес.?гумка|power band|loop band|fitness band|resistance band", re.I), "Стрічкові"),
    (re.compile(r"кистевой эспандер|кистьовий еспандер|hand grip|силикон|силікон", re.I), "Гелеві"),
    (re.compile(r"пружин|spring", re.I), "Пружинні"),
    (re.compile(r"трубчаст|tube", re.I), "Трубчасті"),
]
FEMALE_RE = re.compile(r"жіноч|женск|for women|lady|femme", re.I)

# Rozetka requires the name to start with the product TYPE, not the brand
# (name formula: Тип товару + Бренд + Модель + ...). Some Prom listings in
# these categories start directly with a Latin brand/model name instead -
# flagged by a Rozetka consultant reviewing an actual import (2026-09-15).
# Fix: prepend a category type word whenever the name doesn't already start
# with a Cyrillic word (a decent proxy for "already has a type descriptor",
# since a real type noun in this catalog is always Cyrillic).
NAME_TYPE_PREFIX = {
    "273296": ("Жиросжигатель", "Жироспалювач"),      # Жироспалювачі
    "4653731": ("Стимулятор тестостерона", "Стимулятор тестостерону"),  # Стимулятори тестостерону
}
STARTS_CYRILLIC_RE = re.compile(r"^[А-Яа-яІіЇїЄєҐґ]")


def apply_name_type_prefix(name, name_ua, category_id):
    prefix = NAME_TYPE_PREFIX.get(category_id)
    if not prefix or STARTS_CYRILLIC_RE.match(name):
        return name, name_ua
    ru_prefix, ua_prefix = prefix
    new_name = f"{ru_prefix} {name}" if not name.lower().startswith(ru_prefix.lower()) else name
    new_name_ua = f"{ua_prefix} {name_ua}" if not name_ua.lower().startswith(ua_prefix.lower()) else name_ua
    return new_name, new_name_ua
COLOR_WORDS = {
    "black": "Чорний", "white": "Білий", "red": "Червоний", "blue": "Синій",
    "green": "Зелений", "yellow": "Жовтий", "grey": "Сірий", "gray": "Сірий",
    "pink": "Рожевий", "purple": "Фіолетовий", "orange": "Помаранчевий",
    "camo": "Камуфляж", "navy": "Темно-синій", "beige": "Бежевий",
    "brown": "Коричневий", "silver": "Срібний", "gold": "Золотий", "turquoise": "Бірюзовий",
    "черный": "Чорний", "белый": "Білий", "красный": "Червоний", "синий": "Синій",
    "зеленый": "Зелений", "желтый": "Жовтий", "серый": "Сірий", "розовый": "Рожевий",
}
SIZE_RE = re.compile(r"\b(XXXL|XXL|XL|XS|S|M|L)\b")


def extract_color(name):
    for t in re.findall(r"[A-Za-zА-Яа-яІіЇїЄєҐґ']+", name):
        if t.lower() in COLOR_WORDS:
            return COLOR_WORDS[t.lower()]
    return None


def extract_size(name):
    m = SIZE_RE.search(name)
    return m.group(1) if m else None


def match_first(patterns, name):
    for pat, val in patterns:
        if pat.search(name):
            return val
    return None


def build_params(name, category_id, prom_params):
    """prom_params: list of (name, value) already present on the Prom offer."""
    params = list(prom_params)
    names_lower = {p[0].lower() for p in params}

    if category_id == "273293":
        t = match_first(CREATINE_TYPE_PATTERNS, name)
        if t and "тип" not in names_lower:
            params.append(("Тип", t))
            names_lower.add("тип")

    if category_id == "273294":
        pt = match_first(PROTEIN_TYPE_PATTERNS, name)
        if pt and "вид білка" not in names_lower:
            params.append(("Вид білка", pt))
            names_lower.add("вид білка")
        pp = match_first(PROTEIN_PURITY_PATTERNS, name)
        if pp and "ступінь очищення" not in names_lower:
            params.append(("Ступінь очищення", pp))
            names_lower.add("ступінь очищення")
        if "різновид за походженням" not in names_lower:
            if PROTEIN_ORIGIN_ANIMAL.search(name):
                params.append(("Різновид за походженням", "Тваринний"))
                names_lower.add("різновид за походженням")
            elif PROTEIN_ORIGIN_PLANT.search(name):
                params.append(("Різновид за походженням", "Рослинний"))
                names_lower.add("різновид за походженням")

    if category_id == "273297":
        gt = match_first(GAINER_TYPE_PATTERNS, name)
        if gt and "тип" not in names_lower:
            params.append(("Тип", gt))
            names_lower.add("тип")

    if category_id == "273296":
        fb = match_first(FATBURNER_TYPE_PATTERNS, name)
        if fb and "різновид" not in names_lower:
            params.append(("Різновид", fb))
            names_lower.add("різновид")

    if category_id in BADY_CATS:
        form = match_first(FORM_PATTERNS, name)
        if form and "форма випуску" not in names_lower:
            params.append(("Форма випуску", form))
            names_lower.add("форма випуску")
        if "стать" not in names_lower:
            gender = "Жіночий" if FEMALE_RE.search(name) else "Чоловічий, Жіночий"
            params.append(("Стать", gender))
            names_lower.add("стать")
        if "вікова група" not in names_lower:
            params.append(("Вікова група", "Від 18 років"))
            names_lower.add("вікова група")

    if category_id == "4669351":
        mf = match_first(MAGNESIA_FORM_PATTERNS, name)
        if mf and "форма випуску" not in names_lower:
            params.append(("Форма випуску", mf))
            names_lower.add("форма випуску")

    if category_id == "4669267":
        et = match_first(EXPANDER_TYPE_PATTERNS, name)
        if et and "тип" not in names_lower:
            params.append(("Тип", et))
            names_lower.add("тип")

    if category_id == "4653745" and "тип" not in names_lower:
        params.append(("Тип", "Спортивна пляшка"))
        names_lower.add("тип")

    if category_id == "4627638" and "тип" not in names_lower:
        if re.search(r"контейнер|food storage|для еды|для їжі", name, re.I):
            params.append(("Тип", "Термос для їжі"))
        else:
            params.append(("Тип", "Термос класичний"))
        names_lower.add("тип")

    if category_id in ACCESSORY_CATS or category_id in FITNESS_CATS:
        color = extract_color(name)
        if color and "колір" not in names_lower:
            params.append(("Колір", color))
            names_lower.add("колір")
        size = extract_size(name)
        if size and "розмір" not in names_lower:
            params.append(("Розмір", size))
            names_lower.add("розмір")

    if len(params) < 3 and "стан" not in names_lower:
        params.append(("Стан", "Новий"))

    return params


# ---------------------------------------------------------------
# Description cleaning (strip the Prom SEO boilerplate that violates
# Rozetka's "no delivery/payment/warranty info in description" rule)
# ---------------------------------------------------------------
DESC_CUT_RE = re.compile(r"\s*(Купити з доставкою|Купить с доставкой).*$", re.IGNORECASE | re.DOTALL)


def clean_description(text):
    if not text:
        return ""
    return DESC_CUT_RE.sub("", text).strip()


# ---------------------------------------------------------------
# Pricing: back-solve an implied cost from the Prom price (using the
# Prom margin formula), then solve for the Rozetka price that clears
# 5% margin under Rozetka's *actual* progressive commission bracket
# (iterative, since the bracket depends on the price itself).
# ---------------------------------------------------------------
def get_brackets(category_id):
    if category_id in BADY_CATS:
        return BADY_BRACKETS
    if category_id in ACCESSORY_CATS:
        return ACC_BRACKETS
    if category_id in FITNESS_CATS:
        return FIT_BRACKETS
    return BADY_BRACKETS  # safest default if a category isn't classified yet


def rate_for(price, brackets):
    for lo, hi, rate in brackets:
        if lo <= price <= hi:
            return rate
    return brackets[-1][2]


def compute_prices(prom_price, rrp_uah, category_id):
    """Returns (base_price, promo_price) both already >= supplier RRP as a safety floor."""
    if prom_price is None:
        return None, None

    if rrp_uah:
        rrp_anchored = math.ceil(rrp_uah / (1 - 0.16))
        is_rrp_branch = abs(prom_price - rrp_anchored) <= max(2, 0.01 * rrp_anchored)
    else:
        is_rrp_branch = False

    if is_rrp_branch:
        assumed_cost = rrp_uah * (1 - 0.16) / K_PROM
    else:
        assumed_cost = prom_price / K_PROM

    brackets = get_brackets(category_id)
    price = assumed_cost * (1 + TARGET_MARGIN) / (1 - brackets[0][2])
    for _ in range(6):
        rate = rate_for(price, brackets)
        new_price = assumed_cost * (1 + TARGET_MARGIN) / (1 - rate)
        if abs(new_price - price) < 0.5:
            price = new_price
            break
        price = new_price
    promo_price = math.ceil(price)

    if rrp_uah:
        promo_price = max(promo_price, math.ceil(rrp_uah))

    base_price = math.ceil(promo_price / 0.84)
    return base_price, promo_price


# ---------------------------------------------------------------
# Feed I/O
# ---------------------------------------------------------------
def fetch_xml(url):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=120) as resp:
        return resp.read()


def parse_supplier_feed(raw_bytes):
    """vendorCode -> {available, rrp_uah, picture, country}"""
    feed = {}
    context = etree.iterparse(io.BytesIO(raw_bytes), events=("end",), tag="offer")
    for _, elem in context:
        vc = elem.findtext("vendorCode")
        if vc:
            price = elem.findtext("price")
            feed[vc] = {
                "available": elem.get("available") == "true",
                "rrp_uah": float(price) if price else None,
                "picture": elem.findtext("picture"),
                "country": elem.findtext("country"),
            }
        elem.clear()
    return feed


def s(v):
    return (v or "").strip()


def dedupe_names(offers):
    from collections import Counter
    counts = Counter(o["name"] for o in offers)
    for o in offers:
        if counts[o["name"]] > 1:
            tag = o["article"] or o["id"]
            o["name"] = f'{o["name"]} ({tag})'


def build_rozetka_feed(prom_offers, supplier_feed):
    root = etree.Element("yml_catalog", date=datetime.now().strftime("%Y-%m-%d %H:%M"))
    shop = etree.SubElement(root, "shop")
    etree.SubElement(shop, "name").text = SHOP_NAME
    etree.SubElement(shop, "company").text = SHOP_NAME
    etree.SubElement(shop, "url").text = SHOP_URL
    currencies = etree.SubElement(shop, "currencies")
    etree.SubElement(currencies, "currency", id="UAH", rate="1")

    categories_el = etree.SubElement(shop, "categories")
    for cat_id in sorted(set(o["category_id"] for o in prom_offers if o["category_id"])):
        etree.SubElement(categories_el, "category", id=cat_id).text = \
            ROZETKA_CATEGORY_NAMES.get(cat_id, cat_id)

    offers_el = etree.SubElement(shop, "offers")

    resolved = []
    for o in prom_offers:
        if not o["category_id"]:
            continue
        # o["article"] here is the Prom vendorCode (Код_товару-style in this
        # feed) - translate to the supplier's Ідентифікатор_товару before
        # looking it up in the supplier feed. Try a few formats since the
        # feed isn't perfectly consistent about zero-padding.
        raw_code = o["article"]
        supplier_article = (
            CODE_TO_ARTICLE.get(raw_code)
            or CODE_TO_ARTICLE.get(raw_code.zfill(9))
            or CODE_TO_ARTICLE.get(str(int(raw_code)).zfill(9) if raw_code.isdigit() else "")
        )
        fe = supplier_feed.get(supplier_article) if supplier_article else None
        available = fe["available"] if fe else o["available"]
        picture = (fe["picture"] if fe and fe.get("picture") else o["picture"])
        country = (fe["country"] if fe and fe.get("country") else o["country"])
        rrp = fe["rrp_uah"] if fe else None
        base_price, promo_price = compute_prices(o["price"], rrp, o["category_id"])
        if base_price is None or not picture:
            continue
        fixed_name, fixed_name_ua = apply_name_type_prefix(
            o["name"], o["name_ua"] or o["name"], o["category_id"])
        resolved.append({
            "id": re.sub(r"[^A-Za-z0-9]", "", supplier_article or raw_code) or o["id"],
            "article": supplier_article or raw_code,
            "name": fixed_name,
            "name_ua": fixed_name_ua,
            "vendor": o["vendor"] or "Без бренду",
            "available": available,
            "base_price": base_price,
            "promo_price": promo_price,
            "picture": picture,
            "country": country,
            "description": clean_description(o["description"]),
            "description_ua": clean_description(o["description_ua"]) or clean_description(o["description"]),
            "category_id": o["category_id"],
            "params": build_params(o["name"], o["category_id"], o["params"]),
        })

    dedupe_names(resolved)

    for r in resolved:
        offer = etree.SubElement(offers_el, "offer", id=r["id"],
                                  available="true" if r["available"] else "false")
        etree.SubElement(offer, "price").text = str(r["base_price"])
        etree.SubElement(offer, "promo_price").text = str(r["promo_price"])
        etree.SubElement(offer, "currencyId").text = "UAH"
        etree.SubElement(offer, "categoryId").text = r["category_id"]
        etree.SubElement(offer, "picture").text = r["picture"]
        etree.SubElement(offer, "vendor").text = r["vendor"]
        if r["article"]:
            etree.SubElement(offer, "article").text = r["article"]
        etree.SubElement(offer, "stock_quantity").text = "100" if r["available"] else "0"
        etree.SubElement(offer, "name").text = r["name"]
        etree.SubElement(offer, "name_ua").text = r["name_ua"]
        etree.SubElement(offer, "description").text = etree.CDATA(r["description"])
        etree.SubElement(offer, "description_ua").text = etree.CDATA(r["description_ua"])
        if r["country"]:
            etree.SubElement(offer, "param", name="Країна-виробник товару").text = r["country"]
        for pname, pval in r["params"]:
            etree.SubElement(offer, "param", name=pname).text = pval

    return root, len(resolved), len(prom_offers)


def parse_prom_feed(raw_bytes):
    """Standard Prom.ua YML export -> list of offer dicts."""
    root = etree.fromstring(raw_bytes)
    cat_names = {c.get("id"): c.text for c in root.findall(".//categories/category")}
    offers = []
    for o in root.findall(".//offers/offer"):
        cat_id = o.findtext("categoryId")
        prom_cat_name = cat_names.get(cat_id, "")
        name = s(o.findtext("name"))
        params = [(p.get("name"), s(p.text)) for p in o.findall("param") if p.get("name") and s(p.text)]
        price_text = o.findtext("price")
        offers.append({
            "id": o.get("id"),
            "article": s(o.findtext("vendorCode") or o.findtext("article")),
            "name": name,
            "name_ua": s(o.findtext("name_ua")),
            "vendor": s(o.findtext("vendor")),
            "price": float(price_text) if price_text else None,
            "available": o.get("available") == "true",
            "picture": s(o.findtext("picture")),
            "country": s(o.findtext("country_of_origin") or o.findtext("country")),
            "description": s(o.findtext("description")),
            "description_ua": s(o.findtext("description_ua")),
            "params": params,
            "category_id": assign_rozetka_category(prom_cat_name, name),
        })
    return offers


def main():
    if not PROM_FEED_URL:
        raise SystemExit("PROM_FEED_URL is not set (see README.md)")
    if not SUPPLIER_FEED_URL:
        raise SystemExit("SUPPLIER_FEED_URL is not set (see README.md)")

    print("Fetching Prom feed...")
    prom_offers = parse_prom_feed(fetch_xml(PROM_FEED_URL))
    print(f"  {len(prom_offers)} offers parsed")

    print("Fetching supplier feed...")
    supplier_feed = parse_supplier_feed(fetch_xml(SUPPLIER_FEED_URL))
    print(f"  {len(supplier_feed)} supplier entries parsed")

    print("Building Rozetka feed...")
    root, n_written, n_total = build_rozetka_feed(prom_offers, supplier_feed)

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    etree.ElementTree(root).write(OUTPUT_PATH, xml_declaration=True, encoding="UTF-8", pretty_print=True)
    print(f"Wrote {n_written}/{n_total} offers to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
