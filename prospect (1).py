#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PROSPECT-FR : outil 100 % gratuit de prospection "création de site web" pour toute la France.

CE QUE FAIT L'OUTIL
  1. Récupère les entreprises (tous secteurs) dans OpenStreetMap, avec leurs vrais contacts renseignés.
  2. Les rapproche du registre officiel (API Annuaire des Entreprises) : entreprise active, SIREN, effectif, dirigeant.
  3. Audite leur site web (HTTPS, mobile, vitesse, âge, site gratuit, site en construction...) ou constate l'absence de site.
  4. Extrait téléphone / email DEPUIS le site de l'entreprise et valide tout (format, numéros fictifs, domaine email réel).
  5. Calcule un score de besoin (0-100) et une accroche d'appel personnalisée.
  6. Exporte CSV + Excel triés par priorité.

JAMAIS de numéro ou d'email inventé : si rien n'est trouvé, la case reste vide.

INSTALLATION (une seule fois) :
    pip install requests beautifulsoup4 dnspython openpyxl

UTILISATION :
    python prospect.py --selftest                 -> vérifie que tout fonctionne (sans internet)
    python prospect.py --deps 42                  -> test sur la Loire
    python prospect.py --deps 42,69,75            -> plusieurs départements
    python prospect.py --deps all                 -> toute la France métropolitaine (long : laisse tourner)
    python prospect.py --deps 42 --min-score 60   -> élargit aux sites moyens
Les résultats arrivent dans le dossier "resultats/".
"""
import argparse
import csv
import difflib
import ipaddress
import json
import os
import re
import socket
import sys
import threading
import time
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse, quote_plus

import requests
import urllib3
from bs4 import BeautifulSoup

try:
    import dns.resolver
    HAS_DNS = True
except ImportError:
    HAS_DNS = False
try:
    import openpyxl
    from openpyxl.styles import Font
    HAS_XLSX = True
except ImportError:
    HAS_XLSX = False

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# =============================== CONFIGURATION ===============================
MON_PRENOM = "[TON PRÉNOM]"          # <-- remplace par ton prénom (utilisé dans l'accroche d'appel)
MON_OFFRE = "des sites web professionnels"
UA = "Mozilla/5.0 (compatible; ProspectFR/1.0; prospection B2B)"
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
SIRENE_URL = "https://recherche-entreprises.api.gouv.fr/search"
OUT_DIR = "resultats"
CACHE_DIR = "cache"
DEPARTEMENTS = [f"{i:02d}" for i in range(1, 96) if i != 20] + ["2A", "2B"]

SOCIAL = ("facebook.com", "fb.com", "instagram.com", "linkedin.com", "tiktok.com", "twitter.com", "x.com",
          "youtube.com", "pagesjaunes.fr", "tripadvisor.fr", "tripadvisor.com", "google.com", "g.page",
          "linktr.ee", "ubereats.com", "thefork.fr", "lafourchette.com", "planity.com", "doctolib.fr")
FREE_BUILDERS = ("wixsite.com", "jimdofree.com", "jimdosite.com", "webnode.fr", "webnode.com", "e-monsite.com",
                 "over-blog.com", "business.site", "site123.me", "weebly.com", "blogspot.com", "wordpress.com",
                 "000webhostapp.com", "sitew.com", "sitew.fr", "monsite-orange.fr", "perso.wanadoo.fr",
                 "free.fr", "yolasite.com", "webself.net", "strikingly.com", "godaddysites.com")
FREEMAIL = ("gmail.com", "orange.fr", "wanadoo.fr", "free.fr", "hotmail.com", "hotmail.fr", "outlook.com",
            "outlook.fr", "yahoo.fr", "yahoo.com", "sfr.fr", "neuf.fr", "laposte.net", "live.fr", "icloud.com",
            "bbox.fr", "gmx.fr")
GENERIC_LOCAL = {"contact", "info", "infos", "accueil", "hello", "bonjour", "commercial", "reservation",
                 "reservations", "secretariat", "admin", "mail", "agence", "direction", "boutique", "cabinet",
                 "atelier", "devis", "rdv", "courrier", "bonjour"}
AMENITY_EXCLUDE = {"townhall", "school", "kindergarten", "place_of_worship", "library", "post_office",
                   "police", "fire_station", "college", "university", "courthouse", "community_centre"}
FR_LABEL = {
    "hairdresser": "coiffeurs", "restaurant": "restaurateurs", "cafe": "cafés", "bar": "bars", "fast_food": "restaurateurs",
    "car_repair": "garagistes", "bakery": "boulangers", "butcher": "bouchers", "florist": "fleuristes",
    "plumber": "plombiers", "electrician": "électriciens", "carpenter": "menuisiers", "painter": "peintres",
    "hvac": "chauffagistes", "roofer": "couvreurs", "dentist": "dentistes", "doctors": "médecins",
    "veterinary": "vétérinaires", "beauty": "instituts de beauté", "clothes": "boutiques de mode",
    "hotel": "hôteliers", "guest_house": "gérants de chambres d'hôtes", "fitness_centre": "salles de sport",
    "estate_agent": "agents immobiliers", "lawyer": "avocats", "accountant": "comptables", "car": "concessionnaires",
    "mechanic": "garagistes", "gardener": "paysagistes", "locksmith": "serruriers", "tiler": "carreleurs",
}
OSM_PHONE_KEYS = ("phone", "contact:phone", "contact:mobile", "mobile")
OSM_WEB_KEYS = ("website", "contact:website", "url")
OSM_MAIL_KEYS = ("email", "contact:email")
EFFECTIF = {"NN": "non employeur", "00": "0 salarié", "01": "1-2", "02": "3-5", "03": "6-9", "11": "10-19",
            "12": "20-49", "21": "50-99", "22": "100-199", "31": "200-249", "32": "250-499", "41": "500-999"}
EFFECTIF_ELEVE = {"02", "03", "11", "12", "21", "22", "31", "32", "41"}
# Numéros fictifs réservés par l'ARCEP (ne sonneront jamais chez un vrai client)
FICTIF_PREFIXES = ("019900", "026191", "035301", "046571", "053649", "063998", "098767")

# ================================ OUTILS ====================================
def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


STOP = {"sarl", "sas", "sasu", "eurl", "sa", "snc", "ei", "et", "de", "du", "la", "le", "les", "des", "l", "d",
        "au", "aux", "chez", "the", "and", "ets", "etablissements"}


def norm_name(s):
    s = strip_accents((s or "").lower())
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(w for w in s.split() if w not in STOP)


def is_public_host(host):
    """Sécurité (serveur en ligne) : refuse les adresses internes/privées."""
    try:
        return all(ipaddress.ip_address(i[4][0]).is_global for i in socket.getaddrinfo(host, None))
    except Exception:
        return False


def host_of(url):
    h = urlparse(url).netloc.lower().split(":")[0]
    return h[4:] if h.startswith("www.") else h


def domain_match(host, domains):
    return any(host == d or host.endswith("." + d) for d in domains)


# ------------------------------ TÉLÉPHONE ----------------------------------
PHONE_RE = re.compile(r"(?<!\d)(?:(?:\+|00)\s?33[\s.\-]?(?:\(0\)[\s.\-]?)?|0)[1-9](?:[\s.\-]?\d{2}){4}(?!\d)")


def normalize_fr_phone(raw):
    """Retourne (numéro '0477123456', 'fixe'|'mobile') ou None si invalide / fictif / surtaxé."""
    if not raw:
        return None
    d = re.sub(r"[^\d+]", "", str(raw))
    if d.startswith("+33"):
        d = "0" + d[3:]
    elif d.startswith("0033"):
        d = "0" + d[4:]
    if not re.fullmatch(r"0[1-9]\d{8}", d):
        return None
    if d.startswith("08"):                      # numéros spéciaux / surtaxés : inutiles pour prospecter
        return None
    if len(set(d[1:])) <= 2:                    # 0600000000, 0611111111...
        return None
    if d[1:] in ("123456789", "234567890", "987654321"):
        return None
    if d[:6] in FICTIF_PREFIXES:
        return None
    return d, ("mobile" if d[1] in "67" else "fixe")


def fmt_phone(d):
    return " ".join(d[i:i + 2] for i in range(0, 10, 2)) if d else ""


def extract_phones(text):
    found = []
    for m in PHONE_RE.findall(text or ""):
        n = normalize_fr_phone(m)
        if n and n[0] not in [x[0] for x in found]:
            found.append(n)
    return found


# -------------------------------- EMAIL ------------------------------------
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,255}\.[A-Za-z]{2,}")
EMAIL_BAD = ("example", "sentry", "wixpress", "domain.", "votre", "votremail", "nom@", "email@", "exemple",
             "test@", "user@", "adresse@", "@2x", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", "noreply",
             "no-reply", "wordpress", "godaddy", "ovh.", "@sentry")
_mx_cache = {}


def domain_has_mail(domain):
    """Le domaine existe-t-il vraiment et peut-il recevoir des emails ?"""
    domain = domain.lower()
    if domain in _mx_cache:
        return _mx_cache[domain]
    ok = False
    try:
        if HAS_DNS:
            res = dns.resolver.Resolver()
            res.lifetime = 5
            try:
                ok = len(res.resolve(domain, "MX")) > 0
            except Exception:
                try:
                    ok = len(res.resolve(domain, "A")) > 0
                except Exception:
                    ok = False
        else:
            socket.setdefaulttimeout(5)
            socket.gethostbyname(domain)
            ok = True
    except Exception:
        ok = False
    _mx_cache[domain] = ok
    return ok


def clean_email(raw):
    """Retourne l'email nettoyé s'il a une forme valide et un domaine qui reçoit des emails, sinon None."""
    if not raw:
        return None
    e = raw.strip().strip(".,;:<>()[]\"'").lower()
    if e.startswith("mailto:"):
        e = e[7:].split("?")[0]
    if not EMAIL_RE.fullmatch(e):
        return None
    if any(b in e for b in EMAIL_BAD):
        return None
    if ".." in e or e.startswith(".") or e.split("@")[0].endswith("."):
        return None
    if not domain_has_mail(e.split("@")[1]):
        return None
    return e


def email_kind(e):
    return "générique" if e.split("@")[0] in GENERIC_LOCAL else "nominatif"


# ============================== SOURCE 1 : OSM ===============================
KEY_FILTER = '[~"^(contact:)?(phone|mobile|website|email)$"~"."]'
OSM_PARTS = {
    "shop": 'nwr(area.a)["name"]["shop"]' + KEY_FILTER + ';',
    "craft": 'nwr(area.a)["name"]["craft"]' + KEY_FILTER + ';',
    "office": 'nwr(area.a)["name"]["office"]' + KEY_FILTER + ';',
    "healthcare": 'nwr(area.a)["name"]["healthcare"]' + KEY_FILTER + ';',
    "amenity": 'nwr(area.a)["name"]["amenity"~"^(restaurant|cafe|bar|fast_food|pub|dentist|doctors|clinic|veterinary|pharmacy|driving_school|car_wash|car_rental|nightclub|coworking_space|childcare)$"]' + KEY_FILTER + ';',
    "tourism": 'nwr(area.a)["name"]["tourism"~"^(hotel|guest_house|hostel|motel|apartment|camp_site|gallery|museum|attraction)$"]' + KEY_FILTER + ';',
    "leisure": 'nwr(area.a)["name"]["leisure"~"^(fitness_centre|sports_centre|dance|swimming_pool|bowling_alley|escape_game|golf_course)$"]' + KEY_FILTER + ';',
}


def build_overpass_query(dep, part):
    return f"""[out:json][timeout:180][maxsize:536870912];
area["boundary"="administrative"]["admin_level"="6"]["ref:INSEE"="{dep}"]->.a;
(
  {OSM_PARTS[part]}
);
out center tags;"""


def overpass_request(q, tries=4):
    """Envoie une requête légère en essayant plusieurs serveurs, avec pauses si occupés."""
    last = ""
    for attempt in range(tries):
        for url in OVERPASS_URLS:
            try:
                r = requests.post(url, data={"data": q}, headers={"User-Agent": UA}, timeout=240)
                if r.status_code == 200:
                    data = r.json()
                    if "runtime error" in str(data.get("remark", "")).lower():
                        last = f"{url} -> délai dépassé"
                        continue
                    return data
                last = f"{url} -> HTTP {r.status_code}"
            except Exception as e:
                last = f"{url} -> {type(e).__name__}"
        wait = 15 * (attempt + 1)
        print(f"      serveur occupé ({last}), nouvel essai dans {wait}s...")
        time.sleep(wait)
    raise RuntimeError(last)


def fetch_osm(dep, force=False):
    """Télécharge le département par petits morceaux (un par catégorie), avec reprise si interruption."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    final = os.path.join(CACHE_DIR, f"osm_{dep}.json")
    if os.path.exists(final) and not force:
        with open(final, encoding="utf-8") as f:
            return json.load(f)
    elements, seen = [], set()
    for i, part in enumerate(OSM_PARTS, 1):
        ppath = os.path.join(CACHE_DIR, f"osm_{dep}_{part}.json")
        if os.path.exists(ppath) and not force:
            with open(ppath, encoding="utf-8") as f:
                data = json.load(f)
        else:
            print(f"   téléchargement {i}/{len(OSM_PARTS)} ({part})...")
            try:
                data = overpass_request(build_overpass_query(dep, part))
            except RuntimeError as e:
                raise RuntimeError(f"OpenStreetMap indisponible pour {dep}/{part} : {e}. "
                                   f"Relance la même commande plus tard : ce qui est déjà téléchargé est conservé.")
            with open(ppath, "w", encoding="utf-8") as f:
                json.dump(data, f)
            time.sleep(3)
        for el in data.get("elements", []):
            k = (el.get("type"), el.get("id"))
            if k not in seen:
                seen.add(k)
                elements.append(el)
    merged = {"elements": elements}
    with open(final, "w", encoding="utf-8") as f:
        json.dump(merged, f)
    return merged


def first_tag(tags, keys):
    for k in keys:
        if tags.get(k):
            return tags[k].strip()
    return ""


def parse_osm(data, dep):
    recs = []
    for el in data.get("elements", []):
        t = el.get("tags", {})
        name = (t.get("name") or "").strip()
        if not name:
            continue
        cat = sub = ""
        for k in ("shop", "craft", "office", "healthcare", "amenity", "tourism", "leisure"):
            if k in t:
                cat, sub = k, t[k]
                break
        if cat == "amenity" and sub in AMENITY_EXCLUDE:
            continue
        chain = "brand" in t or "brand:wikidata" in t
        phones = []
        for k in OSM_PHONE_KEYS:
            for p in re.split(r"[;,/]", t.get(k, "")):
                n = normalize_fr_phone(p)
                if n and n[0] not in [x[0] for x in phones]:
                    phones.append(n)
        website = first_tag(t, OSM_WEB_KEYS)
        email = first_tag(t, OSM_MAIL_KEYS).split(";")[0].strip()
        street = " ".join(x for x in (t.get("addr:housenumber", ""), t.get("addr:street", "")) if x)
        recs.append({
            "osm_id": f"{el.get('type')}/{el.get('id')}", "name": name, "cat": cat, "sub": sub,
            "chain": chain, "phone": phones[0][0] if phones else "", "phone_kind": phones[0][1] if phones else "",
            "website": website, "email": email, "city": t.get("addr:city", ""), "postcode": t.get("addr:postcode", ""),
            "address": street, "dep": dep,
        })
    return recs


def dedupe_and_filter_chains(recs):
    seen, out = set(), []
    for r in recs:
        key = r["phone"] or (norm_name(r["name"]) + "|" + r["city"].lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    # Franchises / chaînes : même nom dans plusieurs villes => pas de décision locale sur le site web
    cities = defaultdict(set)
    count = Counter()
    for r in out:
        n = norm_name(r["name"])
        count[n] += 1
        cities[n].add(r["city"].lower())
    return [r for r in out if not r["chain"] and not (count[norm_name(r["name"])] >= 5 and len(cities[norm_name(r["name"])]) >= 3)]


# ======================= SOURCE 2 : REGISTRE OFFICIEL ========================
_sirene_lock = threading.Lock()
_sirene_last = [0.0]


def sirene_get(params):
    with _sirene_lock:  # limite officielle ~7 requêtes/seconde
        wait = 0.16 - (time.time() - _sirene_last[0])
        if wait > 0:
            time.sleep(wait)
        _sirene_last[0] = time.time()
    for i in range(3):
        try:
            r = requests.get(SIRENE_URL, params=params, headers={"User-Agent": UA}, timeout=15)
            if r.status_code == 429:
                time.sleep(2 * (i + 1))
                continue
            return r.json() if r.status_code == 200 else None
        except Exception:
            time.sleep(1)
    return None


def sirene_match(rec):
    """Rapproche l'entreprise OSM d'une entreprise ACTIVE du registre. Retourne un dict ou None."""
    params = {"q": f"{rec['name']} {rec['city']}".strip(), "per_page": 5, "etat_administratif": "A",
              "departement": rec["dep"]}
    data = sirene_get(params)
    if not data:
        return None
    target = norm_name(rec["name"])
    best, best_ratio = None, 0.0
    for res in data.get("results", []):
        siege = res.get("siege") or {}
        names = [res.get("nom_complet"), res.get("nom_raison_sociale")] + list(siege.get("liste_enseignes") or [])
        ratio = max((difflib.SequenceMatcher(None, target, norm_name(n)).ratio() for n in names if n), default=0)
        cp_ok = bool(rec["postcode"]) and rec["postcode"] == siege.get("code_postal")
        city_ok = bool(rec["city"]) and norm_name(rec["city"]) == norm_name(siege.get("libelle_commune", ""))
        for et in res.get("matching_etablissements") or []:
            cp_ok = cp_ok or (bool(rec["postcode"]) and rec["postcode"] == et.get("code_postal"))
            city_ok = city_ok or (bool(rec["city"]) and norm_name(rec["city"]) == norm_name(et.get("libelle_commune", "")))
        good = (ratio >= 0.72 and (cp_ok or city_ok)) or ratio >= 0.88
        if good and ratio > best_ratio:
            best, best_ratio = res, ratio
    if not best:
        return None
    dirigeant = ""
    for d in best.get("dirigeants") or []:
        if d.get("nom") and d.get("prenoms"):  # personne physique seulement
            dirigeant = f"{d['prenoms'].split(',')[0].title()} {d['nom'].title()}"
            break
    code = best.get("tranche_effectif_salarie") or ""
    return {"siren": best.get("siren", ""), "nom_legal": best.get("nom_complet", ""), "effectif_code": code,
            "effectif": EFFECTIF.get(code, ""), "naf": best.get("activite_principale", ""),
            "dirigeant": dirigeant, "creation": best.get("date_creation", "")}


# ========================= SOURCE 3 : AUDIT DU SITE ==========================
def http_get(url, verify=True, timeout=12):
    return requests.get(url, headers={"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"},
                        timeout=timeout, allow_redirects=True, verify=verify)


def audit_site(raw_url):
    """Analyse un site. Retourne dict : etat, points (besoin), problemes, emails, phones, host."""
    url = raw_url.strip()
    if not re.match(r"https?://", url, re.I):
        url = "http://" + url
    host = host_of(url)
    out = {"url": url, "host": host, "etat": "", "points": 0, "problemes": [], "emails": [], "phones": []}

    if domain_match(host, SOCIAL):
        out.update(etat="reseau_social", points=88)
        out["problemes"].append("Pas de vrai site : seulement une page réseau social / annuaire")
        return out

    if not is_public_host(host):
        out.update(etat="injoignable", points=85)
        out["problemes"].append("Site injoignable (domaine inexistant ou serveur en panne)")
        return out

    t0, r, ssl_bad = time.time(), None, False
    for attempt_url in (url, url.replace("http://", "https://", 1) if url.startswith("http://") else url.replace("https://", "http://", 1)):
        try:
            r = http_get(attempt_url)
            break
        except requests.exceptions.SSLError:
            ssl_bad = True
            try:
                r = http_get(attempt_url, verify=False)
                break
            except Exception:
                continue
        except requests.exceptions.RequestException:
            continue
    elapsed = time.time() - t0

    if r is None:
        out.update(etat="injoignable", points=85)
        out["problemes"].append("Site injoignable (domaine inexistant ou serveur en panne)")
        return out
    if r.status_code in (401, 403, 429, 503) and len(r.text) < 3000:
        out.update(etat="inconnu", points=0)
        out["problemes"].append("Site protégé anti-robot : non analysable")
        return out
    if r.status_code >= 400:
        out.update(etat="injoignable", points=85)
        out["problemes"].append(f"Site en erreur (HTTP {r.status_code})")
        return out

    pts, pb = 0, []
    final_host = host_of(r.url)
    out["host"] = final_host
    if not is_public_host(final_host):
        out.update(etat="inconnu", points=0)
        return out
    if domain_match(final_host, SOCIAL):
        out.update(etat="reseau_social", points=88)
        out["problemes"].append("Le site redirige vers un réseau social / annuaire")
        return out
    if ssl_bad:
        pts += 25
        pb.append("Certificat HTTPS invalide (navigateur : « site non sécurisé »)")
    elif not r.url.startswith("https://"):
        pts += 20
        pb.append("Pas de HTTPS (navigateur : « non sécurisé »)")
    if elapsed > 4:
        pts += 10
        pb.append(f"Site lent ({elapsed:.1f}s)")
    elif elapsed > 2.5:
        pts += 5
        pb.append(f"Site assez lent ({elapsed:.1f}s)")

    html = r.text[:700000]
    soup = BeautifulSoup(html, "html.parser")
    text_low = soup.get_text(" ", strip=True).lower()

    if re.search(r"en construction|under construction|bient[oô]t disponible|coming soon|domaine (est )?[aà] vendre|this domain is for sale|site en maintenance|parked", text_low[:4000]):
        pts += 50
        pb.append("Site « en construction » / domaine parqué")
    if not soup.find("meta", attrs={"name": re.compile("^viewport$", re.I)}):
        pts += 25
        pb.append("Pas adapté au mobile (pas de balise viewport)")
    desc = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    if not desc or not (desc.get("content") or "").strip():
        pts += 8
        pb.append("Pas de description pour Google (SEO)")
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    if len(title) < 5 or title.lower() in ("accueil", "home", "index", "untitled", "bienvenue", "document"):
        pts += 5
        pb.append("Titre de page absent ou générique (SEO)")
    years = [int(y) for y in re.findall(r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?((?:19|20)\d{2})", html, re.I)]
    if years and max(years) <= datetime.now().year - 4:
        pts += 15
        pb.append(f"Site non mis à jour depuis {max(years)}")
    if domain_match(final_host, FREE_BUILDERS):
        pts += 30
        pb.append("Site sur un hébergement gratuit (image peu professionnelle)")
    if re.search(r"<frameset|<marquee|\.swf|shockwave|<blink", html, re.I) or len(soup.find_all("font")) > 5:
        pts += 15
        pb.append("Technologie dépassée (design d'une autre époque)")

    # --- contacts affichés sur le site (page d'accueil + contact + mentions légales) ---
    texts, mails, hrefs = [soup.get_text(" ", strip=True)], set(), []
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if h.lower().startswith("mailto:"):
            mails.add(h)
        elif h.lower().startswith("tel:"):
            texts.append(h[4:])
        else:
            blob = (a.get_text(" ", strip=True) + " " + h).lower()
            if any(k in blob for k in ("contact", "mention", "legal", "propos")):
                u = urljoin(r.url, h)
                if host_of(u) == final_host and u not in hrefs and u != r.url:
                    hrefs.append(u)
    for u in hrefs[:2]:
        try:
            r2 = http_get(u, timeout=8, verify=not ssl_bad)
            if r2.status_code < 400:
                s2 = BeautifulSoup(r2.text[:400000], "html.parser")
                texts.append(s2.get_text(" ", strip=True))
                for a in s2.find_all("a", href=True):
                    if a["href"].lower().startswith("mailto:"):
                        mails.add(a["href"])
                    elif a["href"].lower().startswith("tel:"):
                        texts.append(a["href"][4:])
        except Exception:
            pass
    alltext = " ".join(texts)
    for m in mails:
        alltext += " " + m
    out["phones"] = [p[0] for p in extract_phones(alltext)]
    emails = []
    for e in EMAIL_RE.findall(alltext):
        dom = e.split("@")[1].lower()
        # on garde uniquement les emails du domaine du site ou les messageries courantes (évite les emails d'agences web)
        if (dom == final_host or final_host.endswith("." + dom) or dom.endswith("." + final_host) or dom in FREEMAIL):
            ce = clean_email(e)
            if ce and ce not in emails:
                emails.append(ce)
    emails.sort(key=lambda e: (e.split("@")[1] in FREEMAIL, email_kind(e) != "générique"))
    out["emails"] = emails
    out.update(etat="ok", points=min(pts, 100), problemes=pb)
    return out


# =============================== SCORE / ACCROCHE =============================
def label_for(rec):
    return FR_LABEL.get(rec.get("sub", ""), "professionnels")


def build_pitch(rec, problems):
    nom, ville, lab = rec["name"], rec.get("city") or "votre région", label_for(rec)
    start = f"Bonjour, {MON_PRENOM} à l'appareil. "
    if rec["site_state"] == "aucun":
        body = (f"Je cherchais {nom} sur internet à {ville} et je n'ai pas trouvé de site : "
                f"les clients qui vous cherchent sur Google tombent chez vos concurrents. ")
    elif rec["site_state"] == "reseau_social":
        body = (f"J'ai vu que {nom} n'est présent que sur les réseaux sociaux : vous n'apparaissez pas bien sur Google "
                f"et vous dépendez d'une plateforme qui n'est pas la vôtre. ")
    elif rec["site_state"] == "injoignable":
        body = f"Je suis tombé sur le site de {nom} et il ne s'ouvre plus : vous perdez des clients sans le savoir. "
    else:
        body = f"J'ai regardé le site de {nom} : {problems[0].lower() if problems else 'il pourrait mieux vous servir'}. "
    return start + body + f"Je réalise {MON_OFFRE} pour les {lab} du coin : puis-je vous montrer une maquette en 5 minutes ?"


def enrich(rec, args):
    """Complète une fiche : registre officiel, audit du site, contacts validés, score."""
    rec = dict(rec)
    sir = sirene_match(rec) if args.sirene else None
    rec["sirene"] = sir
    if args.sirene and sir is None:
        rec["registre"] = "non rapproché (à vérifier)"
    elif sir:
        rec["registre"] = f"actif - SIREN {sir['siren']}"
    else:
        rec["registre"] = ""

    audit = None
    if rec["website"]:
        audit = audit_site(rec["website"])
        rec["site_state"] = audit["etat"]
        problems = audit["problemes"]
        score = audit["points"]
    else:
        rec["site_state"] = "aucun"
        problems = ["Aucun site web référencé (à confirmer par une recherche Google, lien fourni)"]
        score = 90
        if sir and sir["effectif_code"] in EFFECTIF_ELEVE:
            score = 95

    # --- Contacts validés ---
    phone, phone_src, phone_kind = rec["phone"], "OpenStreetMap", rec["phone_kind"]
    if audit and audit["phones"]:
        if phone and phone in audit["phones"]:
            phone_src = "confirmé (OpenStreetMap + site de l'entreprise)"
        elif not phone:
            phone, phone_src = audit["phones"][0], "site de l'entreprise"
            phone_kind = "mobile" if phone[1] in "67" else "fixe"
    email, email_src = "", ""
    if audit and audit["emails"]:
        email, email_src = audit["emails"][0], "site de l'entreprise"
    elif rec["email"]:
        ce = clean_email(rec["email"])
        if ce:
            email, email_src = ce, "OpenStreetMap"

    rec.update(phone=phone, phone_src=phone_src if phone else "", phone_kind=phone_kind if phone else "",
               email_ok=email, email_src=email_src, email_type=email_kind(email) if email else "",
               score=score, problems=problems, audit_state=rec["site_state"])
    rec["pitch"] = build_pitch(rec, problems)
    return rec


# ================================== EXPORT ===================================
COLUMNS = ["priorite", "score_besoin", "nom", "categorie", "ville", "code_postal", "departement", "telephone",
           "type_telephone", "fiabilite_telephone", "email", "type_email", "fiabilite_email", "site_web",
           "etat_site", "problemes_constates", "registre_officiel", "effectif", "dirigeant", "adresse",
           "recherche_google", "accroche_appel", "statut_appel", "notes"]


def to_row(r):
    sir = r.get("sirene") or {}
    prio = "A" if r["score"] >= 90 else ("B" if r["score"] >= 80 else "C")
    q = quote_plus(f"{r['name']} {r.get('city', '')}")
    return {
        "priorite": prio, "score_besoin": r["score"], "nom": r["name"], "categorie": r.get("sub") or r.get("cat"),
        "ville": r.get("city", ""), "code_postal": r.get("postcode", ""), "departement": r["dep"],
        "telephone": fmt_phone(r["phone"]), "type_telephone": r["phone_kind"], "fiabilite_telephone": r["phone_src"],
        "email": r["email_ok"], "type_email": r["email_type"],
        "fiabilite_email": (r["email_src"] + " + domaine email vérifié") if r["email_ok"] else "",
        "site_web": r["website"], "etat_site": r["audit_state"], "problemes_constates": " | ".join(r["problems"]),
        "registre_officiel": r["registre"], "effectif": sir.get("effectif", ""), "dirigeant": sir.get("dirigeant", ""),
        "adresse": r.get("address", ""), "recherche_google": f"https://www.google.com/search?q={q}",
        "accroche_appel": r["pitch"], "statut_appel": "", "notes": "",
    }


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, delimiter=";")
        w.writeheader()
        w.writerows(rows)


def write_xlsx(path, rows):
    if not HAS_XLSX:
        return False
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Prospects"
    ws.append(COLUMNS)
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in rows:
        ws.append([r[c] for c in COLUMNS])
    ws.freeze_panes = "D2"
    ws.auto_filter.ref = ws.dimensions
    for col, wdt in zip("ABCDEFGHIJKLMNOPQRSTUVWX", (8, 8, 30, 14, 18, 8, 6, 16, 9, 26, 28, 10, 22, 32, 12, 50, 24, 10, 18, 26, 36, 60, 14, 20)):
        ws.column_dimensions[col].width = wdt
    wb.save(path)
    return True


# ================================== PIPELINE =================================
def process_department(dep, args):
    print(f"\n=== Département {dep} ===")
    out_csv = os.path.join(OUT_DIR, f"prospects_{dep}.csv")
    if os.path.exists(out_csv) and not args.force:
        print("   déjà traité (utilise --force pour refaire) -> lecture du fichier existant")
        with open(out_csv, encoding="utf-8-sig") as f:
            return list(csv.DictReader(f, delimiter=";"))
    data = fetch_osm(dep, force=args.force)
    recs = dedupe_and_filter_chains(parse_osm(data, dep))
    # il faut au moins un moyen de contact potentiel (téléphone/email OSM ou un site à explorer)
    recs = [r for r in recs if r["phone"] or r["email"] or r["website"]]
    if args.limit:
        recs = recs[: args.limit]
    print(f"   {len(recs)} entreprises indépendantes à analyser")
    results, done = [], 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(enrich, r, args) for r in recs]
        for f in as_completed(futs):
            done += 1
            try:
                results.append(f.result())
            except Exception as e:
                print(f"   (fiche ignorée : {e})")
            if done % 50 == 0 or done == len(futs):
                print(f"   {done}/{len(futs)} analysées")
    kept = [r for r in results if r["score"] >= args.min_score and r["audit_state"] != "inconnu"
            and (r["phone"] or r["email_ok"])]
    kept.sort(key=lambda r: -r["score"])
    rows = [to_row(r) for r in kept]
    os.makedirs(OUT_DIR, exist_ok=True)
    write_csv(out_csv, rows)
    print(f"   -> {len(rows)} prospects retenus (score >= {args.min_score}) : {out_csv}")
    return rows


def main():
    ap = argparse.ArgumentParser(description="Prospection sites web - France (100 % gratuit)")
    ap.add_argument("--deps", default="42", help="ex: 42 | 42,69,75 | all (toute la France métropolitaine)")
    ap.add_argument("--min-score", type=int, default=80, help="score de besoin minimum 0-100 (défaut 80)")
    ap.add_argument("--workers", type=int, default=8, help="analyses en parallèle (défaut 8)")
    ap.add_argument("--limit", type=int, default=0, help="limite par département (pour tester)")
    ap.add_argument("--no-sirene", dest="sirene", action="store_false", help="ne pas rapprocher du registre officiel")
    ap.add_argument("--force", action="store_true", help="refaire même si déjà traité")
    ap.add_argument("--selftest", action="store_true", help="auto-test hors-ligne")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    deps = DEPARTEMENTS if args.deps.lower() == "all" else [d.strip().upper().zfill(2) if d.strip().isdigit() else d.strip().upper() for d in args.deps.split(",")]
    if not HAS_DNS:
        print("Astuce : pip install dnspython pour une vérification plus fiable des emails.")
    all_rows = []
    try:
        for dep in deps:
            try:
                all_rows += process_department(dep, args)
            except RuntimeError as e:
                print(f"   ERREUR : {e}")
    except KeyboardInterrupt:
        print("\nArrêt demandé : les départements déjà terminés sont sauvegardés.")
    if all_rows:
        all_rows.sort(key=lambda r: -int(r["score_besoin"]))
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        os.makedirs(OUT_DIR, exist_ok=True)
        p_csv = os.path.join(OUT_DIR, f"PROSPECTS_TOTAL_{stamp}.csv")
        write_csv(p_csv, all_rows)
        print(f"\nFichier final : {p_csv}  ({len(all_rows)} prospects)")
        if write_xlsx(os.path.join(OUT_DIR, f"PROSPECTS_TOTAL_{stamp}.xlsx"), all_rows):
            print("Version Excel créée dans le même dossier.")


# ================================== AUTO-TEST ================================
def selftest():
    ok = True

    def check(label, cond):
        nonlocal ok
        print(("  OK   " if cond else "  ECHEC ") + label)
        ok = ok and cond

    print("Auto-test hors-ligne :")
    check("fixe valide", normalize_fr_phone("04 77 12 34 56") == ("0477123456", "fixe"))
    check("+33 converti", normalize_fr_phone("+33 6 12 34 56 78") == ("0612345678", "mobile"))
    check("(0) toléré", PHONE_RE.search("+33 (0)4 77 12 34 56") is not None)
    check("0033 converti", normalize_fr_phone("0033477123456") == ("0477123456", "fixe"))
    check("surtaxé refusé", normalize_fr_phone("0899 12 34 56") is None and normalize_fr_phone("08 99 12 34 56") is None)
    check("faux évident refusé", normalize_fr_phone("06 00 00 00 00") is None and normalize_fr_phone("01 23 45 67 89") is None)
    check("fictif ARCEP refusé", normalize_fr_phone("06 39 98 12 34") is None)
    check("trop court refusé", normalize_fr_phone("04 77 12") is None)
    check("extraction texte", [p[0] for p in extract_phones("Appelez-nous au 04.77.12.34.56 ou 06 12 34 56 78")] == ["0477123456", "0612345678"])
    check("email format invalide refusé", clean_email("pas-un-email") is None)
    check("email factice refusé", clean_email("votre@email.com") is None and clean_email("test@example.com") is None)
    check("image refusée", clean_email("logo@2x.png") is None)
    check("type email", email_kind("contact@x.fr") == "générique" and email_kind("jean.dupont@x.fr") == "nominatif")
    check("nom normalisé", norm_name("SARL Le Café de l'Église") == "cafe eglise")
    fake = {"elements": [
        {"type": "node", "id": 1, "tags": {"name": "Salon Test", "shop": "hairdresser", "phone": "+33 4 77 12 34 56", "addr:city": "Saint-Étienne"}},
        {"type": "node", "id": 2, "tags": {"name": "Chaîne", "shop": "bakery", "brand": "Chaîne", "phone": "0477123457"}},
        {"type": "node", "id": 3, "tags": {"name": "Faux Tel", "shop": "clothes", "phone": "06 00 00 00 00", "website": "x.fr"}}]}
    recs = dedupe_and_filter_chains(parse_osm(fake, "42"))
    check("parse OSM + filtre chaînes", [r["name"] for r in recs] == ["Salon Test", "Faux Tel"] and recs[1]["phone"] == "")
    class A: sirene = False
    r = enrich(recs[0], A())
    check("score sans site = 90+", r["score"] >= 90 and "Aucun site" in r["problems"][0])
    check("accroche générée", "Salon Test" in r["pitch"] and "coiffeurs" in r["pitch"])
    check("ligne export complète", set(to_row(r).keys()) == set(COLUMNS))
    print("\nRÉSULTAT :", "tout est OK" if ok else "il y a un problème, copie-colle ce message")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main() or 0)
