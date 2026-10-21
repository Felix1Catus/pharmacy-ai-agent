# -*- coding: utf-8 -*-
"""Active-ingredient index: find the same medicine under other brand names.

Every product's active ingredient(s) come from the SFDA registry (sfda_scientific_name, e.g.
"AMOXICILLIN,CLAVULANIC ACID") or our own field (x_ingredient_en, "Amoxicillin+Clavulanate").
They are normalised into an ingredient SET, so "Augmentin 1g" (sample data) and "أوجمنتین 625
ملجم" / "JULMENTIN 375 MG TAB" (SFDA) are recognised as the same active ingredients, while plain
"AMOXICILLIN" stays a different set from the amoxicillin+clavulanic acid combination.

Used by Gateway.search_product to
  - understand ingredient queries ("amoxicillin", "اموكسيسيلين", "amoxicillin 1mg tablet"),
  - rank single-ingredient products before combinations, then by dosage form and strength,
  - return other brands with the same active ingredient(s) as `alternatives`,
and by prescriptions, so a verified prescription also unlocks same-ingredient brands.
"""
from __future__ import annotations

import re
import time

from rapidfuzz import fuzz

from fuzzy_search import normalize, sound_key

# spelling variants of the same ingredient across our data sources
_SYNONYMS = {
    "clavulanate": "clavulanic acid", "clavulanate potassium": "clavulanic acid",
    "potassium clavulanate": "clavulanic acid", "acetaminophen": "paracetamol",
    "vitd": "vitamin d", "vit d": "vitamin d", "vitamin d3": "vitamin d", "cholecalciferol": "vitamin d",
    "acetylsalicylic acid": "aspirin", "amoxycillin": "amoxicillin",
    "amoxicillin trihydrate": "amoxicillin", "salbutamol sulfate": "salbutamol",
    "salbutamol sulphate": "salbutamol", "albuterol": "salbutamol",
}
_FORMS = {   # query/product words -> dosage-form group
    "tab": "tablet", "tabs": "tablet", "tablet": "tablet", "tablets": "tablet", "قرص": "tablet",
    "اقراص": "tablet", "حبوب": "tablet", "حبه": "tablet", "cap": "capsule", "caps": "capsule",
    "capsule": "capsule", "capsules": "capsule", "كبسول": "capsule", "كبسوله": "capsule",
    "كبسولات": "capsule", "syrup": "liquid", "susp": "liquid", "suspension": "liquid",
    "شراب": "liquid", "معلق": "liquid", "drops": "drops", "قطره": "drops", "نقط": "drops",
    "inj": "injection", "injection": "injection", "vial": "injection", "حقن": "injection",
    "حقنه": "injection", "ابره": "injection", "cream": "topical", "ointment": "topical",
    "gel": "topical", "كريم": "topical", "مرهم": "topical", "جل": "topical",
    "inhaler": "inhaler", "بخاخ": "inhaler", "spray": "spray",
}
_UNIT_MG = {"mg": 1, "ملجم": 1, "ملغ": 1, "مجم": 1, "ملغم": 1, "g": 1000, "gm": 1000, "gram": 1000,
            "جم": 1000, "جرام": 1000, "غ": 1000, "mcg": 0.001, "ميكروجرام": 0.001}
_STRENGTH = re.compile(r"(\d+(?:[.,]\d+)?)\s*(mg|mcg|gm|gram|g|ملجم|ملغم|ملغ|مجم|ميكروجرام|جرام|جم|غ)(?![a-z])")
_DIG = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
# first words shared by many ingredients: never an ingredient on their own
_GENERIC_FIRST = {"sodium", "potassium", "calcium", "magnesium", "vitamin", "ferrous", "zinc",
                  "hydrochloride", "sulfate", "sulphate", "chloride", "acetate", "citrate", "purified",
                  "human", "insulin", "liquid", "light", "white", "yellow", "hydrogen", "dried"}


# salt / hydrate words: "METFORMIN HYDROCHLORIDE" and "ATORVASTATIN CALCIUM" are what a
# prescription calls "Metformin" and "Atorvastatin" (only stripped when a name remains)
_SALTS = {"hydrochloride", "hcl", "hydrobromide", "sodium", "potassium", "calcium", "magnesium",
          "trihydrate", "dihydrate", "monohydrate", "hemihydrate", "anhydrous", "sulfate",
          "sulphate", "maleate", "besylate", "besilate", "mesylate", "mesilate", "succinate",
          "tartrate", "fumarate", "citrate", "acetate", "phosphate", "dipropionate", "propionate",
          "hyclate", "bitartrate", "disodium", "dihydrochloride", "medoxomil", "base", "as"}


def _base_name(p: str) -> str:
    words = p.split()
    core = [w for w in words if w not in _SALTS]
    return " ".join(core) if core else p


def ingredient_set(raw: str | None) -> frozenset[str]:
    """'AMOXICILLIN,CLAVULANIC ACID' / 'Amoxicillin+Clavulanate' -> {'amoxicillin','clavulanic acid'};
    'METFORMIN HYDROCHLORIDE' -> {'metformin'}."""
    if not raw:
        return frozenset()
    parts = re.split(r"\s*(?:,|\+|/|;|&|\band\b|\bwith\b)\s*", raw.lower())
    out = set()
    for p in parts:
        p = re.sub(r"\(.*?\)", "", p)                       # "(as trihydrate)"
        p = re.sub(r"\s+", " ", p).strip(" .-\"'")
        if p:
            p = _SYNONYMS.get(p, p)
            out.add(_SYNONYMS.get(_base_name(p), _base_name(p)))
    return frozenset(out)


_RX_ITEM_LINE = re.compile(r"^\s*(\d+\s*[.)\-]|[-•*▪])")


def prescription_items(text: str, index: "IngredientIndex") -> list[dict]:
    """Medicine lines of a prescription -> [{"set", "strength_mg", "line"}]. Only lines that
    look like an Rx item (a list number/bullet or a strength) are read, so headers such as
    'Physician' or 'Diagnosis' can never be mistaken for an ingredient."""
    items, seen = [], set()
    for line in (text or "").splitlines():
        if not (_RX_ITEM_LINE.match(line) or _STRENGTH.search(normalize(line).translate(_DIG))):
            continue
        if re.match(r"\s*(sig|qty|quantity|instructions?)\b", line, re.I):
            continue
        s = index.resolve_name(line)
        if not s:
            continue
        key = (s, strength_mg(line))
        if key not in seen:
            seen.add(key)
            items.append({"set": s, "strength_mg": key[1], "line": line.strip()})
    return items


def strength_mg(text: str) -> float | None:
    m = _STRENGTH.search(normalize(text or "").translate(_DIG))
    if not m:
        return None
    return float(m.group(1).replace(",", ".")) * _UNIT_MG[m.group(2)]


def dosage_form(text: str) -> str | None:
    for w in re.findall(r"[a-z؀-ۿ]+", normalize(text or "")):
        if w in _FORMS:
            return _FORMS[w]
    return None


def parse_query(q: str) -> dict:
    """Split a customer query into the name part and optional strength / dosage form."""
    n = normalize(q).translate(_DIG)
    core = _STRENGTH.sub(" ", n)
    core = " ".join(w for w in re.findall(r"[a-z؀-ۿ]+", core) if w not in _FORMS)
    return {"core": core.strip(), "strength_mg": strength_mg(q), "form": dosage_form(q)}


def fmt_mg(mg: float) -> str:
    return f"{mg / 1000:g} g" if mg >= 1000 else f"{mg:g} mg"


class IngredientIndex:
    REFRESH_S = 600

    def __init__(self, client):
        self.c = client
        self.built = 0.0
        self.products: dict[int, dict] = {}                 # template id -> info
        self.by_set: dict[frozenset, list[int]] = {}
        self.names: dict[str, set[str]] = {}                # ingredient -> sound keys (ar/en)

    def ensure(self):
        if self.products and time.time() - self.built < self.REFRESH_S:
            return
        rows = self.c.search_read("product.template", [["sale_ok", "=", True]],
                                  ["name", "x_name_ar", "sfda_scientific_name", "x_ingredient_en",
                                   "x_ingredient_ar"])
        products, by_set, names = {}, {}, {}
        for r in rows:
            s = ingredient_set(r.get("sfda_scientific_name") or r.get("x_ingredient_en"))
            if not s:
                continue
            products[r["id"]] = {"set": s, "name": r["name"] or "",
                                 "strength_mg": strength_mg(r["name"] or ""),
                                 "form": dosage_form(r["name"] or "")}
            by_set.setdefault(s, []).append(r["id"])
            for ing in s:
                keys = names.setdefault(ing, set())
                keys.add(sound_key(ing.replace(" ", "")))
                first = ing.split(" ")[0]          # "clavulanic acid" also answers to "clavulanic"
                if " " in ing and len(first) >= 6 and first not in _GENERIC_FIRST:
                    keys.add(sound_key(first))
            # Arabic ingredient names from our own data ("اموكسيسيلين") -> same ingredient keys
            ar = r.get("x_ingredient_ar")
            if ar and len(s) == 1:
                names[next(iter(s))].add(sound_key(normalize(ar).replace(" ", "")))
        self.products, self.by_set, self.names, self.built = products, by_set, names, time.time()

    def resolve_name(self, text: str) -> frozenset[str]:
        """Ingredient(s) a query NAMES directly: 'amoxicillin', 'اموكسيسيلين', 'paracetamol'.
        Each query word is compared by sound key with every known ingredient name."""
        self.ensure()
        found = set()
        words = [w for w in re.findall(r"[a-z؀-ۿ]+", normalize(text)) if len(w) >= 5]
        for w in words + ([" ".join(words)] if len(words) > 1 else []):
            wl = _SYNONYMS.get(w, w)
            k = sound_key(wl.replace(" ", ""))
            if len(k) < 4:
                continue
            best, ing = 0.0, None
            for name, keys in self.names.items():
                if wl == name:
                    best, ing = 100.0, name
                    break
                for nk in keys:
                    if abs(len(nk) - len(k)) <= 2:
                        s = fuzz.ratio(k, nk)
                        if s > best:
                            best, ing = s, name
            # short sound keys are ambiguous ("amoxil" ~ "amoxicillin"): demand a near-exact match
            if ing and best >= (88 if len(k) >= 6 else 95):
                found.add(ing)
        return frozenset(found)

    def set_of(self, template_id: int) -> frozenset[str]:
        self.ensure()
        p = self.products.get(template_id)
        return p["set"] if p else frozenset()

    def rank(self, ids, want_set, strength=None, form=None):
        """Order template ids: exact ingredient set first, then same form, closest strength."""
        def key(tid):
            p = self.products[tid]
            exact = p["set"] == want_set
            form_ok = form is None or p["form"] == form
            dist = abs((p["strength_mg"] or 0) - strength) if strength and p["strength_mg"] else 1e9
            return (not exact, not form_ok, dist, p["name"])
        return sorted((t for t in ids if t in self.products), key=key)

    def same_ingredients(self, want_set: frozenset, include_combos: bool = False) -> list[int]:
        """Templates with exactly these active ingredients (optionally also combinations that
        contain all of them)."""
        self.ensure()
        out = list(self.by_set.get(want_set, []))
        if include_combos:
            out += [t for s, ids in self.by_set.items() if s != want_set and want_set < s for t in ids]
        return out

    def strengths(self, ids, form=None) -> list[float]:
        """All strengths (mg) we stock for these products - the FULL list (callers decide how
        many to show; cutting it first made '1 g' look unavailable)."""
        return sorted({self.products[t]["strength_mg"] for t in ids if t in self.products
                       and self.products[t]["strength_mg"] and (form is None or self.products[t]["form"] == form)})

    @staticmethod
    def nearest(mgs: list[float], want: float, n: int = 8) -> list[str]:
        """The n stocked strengths closest to the requested one, in ascending order."""
        return [fmt_mg(m) for m in sorted(sorted(mgs, key=lambda m: abs(m - want))[:n])]
