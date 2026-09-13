"""
Veille emploi via les API publiques des ATS.

  python veille_ats.py detect    -> identifie l'ATS derriere chaque site carrieres
  python veille_ats.py collect   -> une collecte, ecrit offres.json
  python veille_ats.py serve     -> collecte toutes les heures + sert l'app de swipe
                                    sur http://localhost:8765

Dependance unique : pip install requests
Place swipe-offres.html dans le meme dossier que ce fichier.
"""

import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

import requests

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
TIMEOUT = 20
PORT = 8765
INTERVALLE = 3600          # secondes entre deux collectes
FICHIER_VUES = "deja_vues.json"
FICHIER_STOCK = "offres.json"          # le flux que l'app vient lire
MAX_STOCK = 200

# ---------------------------------------------------------------- detection

SIGNATURES = {
    "workday":        [r"myworkdayjobs\.com", r"wday/cxs"],
    "smartrecruiters": [r"smartrecruiters\.com", r"jobs\.smartrecruiters"],
    "greenhouse":     [r"greenhouse\.io", r"boards\.greenhouse"],
    "lever":          [r"jobs\.lever\.co", r"api\.lever\.co"],
    "ashby":          [r"jobs\.ashbyhq\.com", r"ashbyhq\.com"],
    "workable":       [r"workable\.com"],
    "recruitee":      [r"recruitee\.com"],
    "personio":       [r"personio\.(de|com)"],
    "successfactors": [r"successfactors\.(com|eu)", r"career\d?\.sap"],
    "taleo":          [r"taleo\.net"],
    "icims":          [r"icims\.com"],
    "avature":        [r"avature\.net"],
    "teamtailor":     [r"teamtailor\.com"],
    "phenom":         [r"phenompeople\.com"],
    "eightfold":      [r"eightfold\.ai"],
}

FICHIER_SOCIETES = "societes.csv"
FICHIER_CIBLES = "cibles.json"

CHEMINS_CARRIERES = ["/careers", "/en/careers", "/carrieres", "/fr/carrieres",
                     "/jobs", "/en/jobs", "/about/careers", "/careers/jobs",
                     "/en/about-us/careers", "/nous-rejoindre", "/recrutement", ""]


def slugs(nom, domaine):
    """Variantes de nom sous lesquelles un tableau d'offres peut exister."""
    base = re.sub(r"[^a-z0-9]", "", nom.lower())
    court = re.sub(r"[^a-z0-9]", "", nom.split()[0].lower())
    dom = domaine.split(".")[0].replace("-", "")
    tiret = re.sub(r"[^a-z0-9]+", "-", nom.lower()).strip("-")
    return list(dict.fromkeys([base, dom, court, tiret]))


SONDES = {
    "greenhouse": lambda s: f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs",
    "lever": lambda s: f"https://api.lever.co/v0/postings/{s}?mode=json",
    "ashby": lambda s: f"https://api.ashbyhq.com/posting-api/job-board/{s}",
    "smartrecruiters": lambda s: f"https://api.smartrecruiters.com/v1/companies/{s}/postings?limit=1",
}


def lire_page(url):
    try:
        r = requests.get(url, headers=UA, timeout=12, allow_redirects=True)
        if r.status_code < 400:
            return r.url + "\n" + r.text[:400_000]
    except Exception:
        pass
    return ""


def signature(corpus):
    for ats, pats in SIGNATURES.items():
        if any(re.search(p, corpus, re.I) for p in pats):
            return ats
    return ""


def extraire(ats, corpus, societe):
    if ats == "workday":
        m = re.search(r"https?://([\w-]+)\.(wd\d)\.myworkdayjobs\.com/(?:[\w-]+/)?([\w-]+)",
                      corpus, re.I)
        if m:
            return {"societe": societe, "ats": "workday", "tenant": m.group(1),
                    "host": m.group(2), "site": m.group(3)}
    if ats == "smartrecruiters":
        m = re.search(r"smartrecruiters\.com/(?:companies/)?([\w-]+)", corpus, re.I)
        if m:
            return {"societe": societe, "ats": "smartrecruiters", "company": m.group(1)}
    if ats == "greenhouse":
        m = re.search(r"greenhouse\.io/(?:embed/job_board\?for=|boards/|v1/boards/)?([\w-]+)",
                      corpus, re.I)
        if m:
            return {"societe": societe, "ats": "greenhouse", "token": m.group(1)}
    if ats == "lever":
        m = re.search(r"lever\.co/(?:postings/)?([\w-]+)", corpus, re.I)
        if m:
            return {"societe": societe, "ats": "lever", "company": m.group(1)}
    if ats == "ashby":
        m = re.search(r"ashbyhq\.com/(?:job-board/)?([\w-]+)", corpus, re.I)
        if m:
            return {"societe": societe, "ats": "ashby", "company": m.group(1)}
    return None


def sonder(nom, domaine):
    """Demande a chaque ATS si un tableau existe sous ce nom."""
    for s in slugs(nom, domaine):
        for ats, faire_url in SONDES.items():
            try:
                r = requests.get(faire_url(s), headers=UA, timeout=10)
            except Exception:
                continue
            if r.status_code != 200:
                continue
            try:
                d = r.json()
            except Exception:
                continue
            vide = (isinstance(d, list) and not d) or \
                   (isinstance(d, dict) and not (d.get("jobs") or d.get("content")))
            if vide:
                continue
            cle_champ = {"greenhouse": "token", "lever": "company",
                         "ashby": "company", "smartrecruiters": "company"}[ats]
            return {"societe": nom, "ats": ats, cle_champ: s}
    return None


def detecter_societe(nom, domaine):
    for chemin in CHEMINS_CARRIERES:
        corpus = lire_page(f"https://{domaine}{chemin}")
        if not corpus:
            continue
        ats = signature(corpus)
        if ats:
            info = extraire(ats, corpus, nom)
            if info:
                info["source"] = f"page {chemin or '/'}"
                return info
            return {"societe": nom, "ats": ats, "incomplet": True,
                    "source": f"page {chemin or '/'}"}
    trouve = sonder(nom, domaine)
    if trouve:
        trouve["source"] = "sonde directe"
        return trouve
    return {"societe": nom, "ats": "inconnu", "domaine": domaine}


def lancer_detection():
    categorie = sys.argv[2] if len(sys.argv) > 2 else ""
    if not os.path.exists(FICHIER_SOCIETES):
        print(f"{FICHIER_SOCIETES} introuvable.")
        return
    lignes = []
    with open(FICHIER_SOCIETES, encoding="utf-8") as f:
        next(f)
        for l in f:
            p = [c.strip() for c in l.strip().split(";")]
            if len(p) >= 3 and (not categorie or p[2] == categorie):
                lignes.append(p)
    print(f"{len(lignes)} societe(s) a analyser"
          f"{' (categorie ' + categorie + ')' if categorie else ''}\n")

    cibles = lire(FICHIER_CIBLES, [])
    connues = {c["societe"] for c in cibles}
    rates = []
    for i, (nom, domaine, cat) in enumerate(lignes, 1):
        if nom in connues:
            print(f"{i:>3}. {nom:<28} deja dans cibles.json")
            continue
        r = detecter_societe(nom, domaine)
        if r["ats"] in ("inconnu",) or r.get("incomplet"):
            rates.append(r)
            print(f"{i:>3}. {nom:<28} {r['ats']:<16} a faire dans Chrome")
        else:
            cibles.append({k: v for k, v in r.items() if k != "source"})
            print(f"{i:>3}. {nom:<28} {r['ats']:<16} {r.get('source','')}")
            ecrire(FICHIER_CIBLES, cibles)
        time.sleep(0.4)

    ecrire("non_detectees.json", rates)
    print(f"\n{len(cibles)} cible(s) exploitables dans {FICHIER_CIBLES}")
    print(f"{len(rates)} societe(s) sans API, listees dans non_detectees.json")


# ---------------------------------------------------------------- collecte

def depuis_greenhouse(token, societe):
    u = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    for j in requests.get(u, headers=UA, timeout=TIMEOUT).json().get("jobs", []):
        yield {"societe": societe, "poste": j.get("title", ""),
               "ville": (j.get("location") or {}).get("name", ""), "pays": "",
               "contrat": "", "publie": (j.get("updated_at") or "")[:10],
               "ref": str(j.get("id", "")), "lien": j.get("absolute_url", ""),
               "texte": nettoyer_html(j.get("content", ""))}


def depuis_lever(company, societe):
    u = f"https://api.lever.co/v0/postings/{company}?mode=json"
    for j in requests.get(u, headers=UA, timeout=TIMEOUT).json():
        cat = j.get("categories") or {}
        yield {"societe": societe, "poste": j.get("text", ""),
               "ville": cat.get("location", ""), "pays": "",
               "contrat": cat.get("commitment", ""),
               "publie": datetime.fromtimestamp(j.get("createdAt", 0) / 1000,
                                                timezone.utc).strftime("%Y-%m-%d"),
               "ref": j.get("id", ""), "lien": j.get("hostedUrl", ""),
               "texte": nettoyer_html(j.get("descriptionPlain") or j.get("description", ""))}


def depuis_ashby(company, societe):
    u = f"https://api.ashbyhq.com/posting-api/job-board/{company}?includeCompensation=true"
    for j in requests.get(u, headers=UA, timeout=TIMEOUT).json().get("jobs", []):
        yield {"societe": societe, "poste": j.get("title", ""),
               "ville": j.get("location", ""), "pays": "",
               "contrat": j.get("employmentType", ""),
               "publie": (j.get("publishedAt") or "")[:10],
               "ref": j.get("id", ""), "lien": j.get("jobUrl", ""),
               "texte": nettoyer_html(j.get("descriptionHtml", ""))}


def depuis_smartrecruiters(company, societe, mot_cle=""):
    u = f"https://api.smartrecruiters.com/v1/companies/{company}/postings"
    d = requests.get(u, headers=UA, timeout=TIMEOUT,
                     params={"q": mot_cle, "limit": 100}).json()
    for j in d.get("content", []):
        loc = j.get("location") or {}
        det = requests.get(f"{u}/{j['id']}", headers=UA, timeout=TIMEOUT).json()
        sec = ((det.get("jobAd") or {}).get("sections") or {})
        texte = "\n\n".join(nettoyer_html((sec.get(k) or {}).get("text", ""))
                            for k in ("companyDescription", "jobDescription",
                                      "qualifications", "additionalInformation"))
        yield {"societe": societe, "poste": j.get("name", ""),
               "ville": loc.get("city", ""), "pays": loc.get("country", ""),
               "contrat": (j.get("typeOfEmployment") or {}).get("label", ""),
               "publie": (j.get("releasedDate") or "")[:10], "ref": j.get("id", ""),
               "lien": j.get("ref") or f"https://jobs.smartrecruiters.com/{company}/{j['id']}",
               "texte": texte}
        time.sleep(0.3)


def depuis_workday(tenant, host, site, societe, mot_cle=""):
    base = f"https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
    offset = 0
    while offset < 100:
        d = requests.post(f"{base}/jobs",
                          headers={**UA, "Content-Type": "application/json"},
                          json={"appliedFacets": {}, "limit": 20, "offset": offset,
                                "searchText": mot_cle}, timeout=TIMEOUT).json()
        postes = d.get("jobPostings", [])
        if not postes:
            return
        for j in postes:
            det = requests.get(f"{base}{j.get('externalPath','')}",
                               headers=UA, timeout=TIMEOUT).json()
            info = det.get("jobPostingInfo", {})
            yield {"societe": societe, "poste": j.get("title", ""),
                   "ville": j.get("locationsText", "") or info.get("location", ""),
                   "pays": "", "contrat": info.get("timeType", ""),
                   "publie": (info.get("startDate") or "")[:10],
                   "ref": info.get("jobReqId", ""), "lien": info.get("externalUrl", ""),
                   "texte": nettoyer_html(info.get("jobDescription", ""))}
            time.sleep(0.3)
        offset += 20


# Rempli automatiquement par la commande detect, dans cibles.json.
def cibles():
    return lire(FICHIER_CIBLES, [])

MOTS_CLES = ["Investment Specialist", "Product Specialist", "Investor Relations"]


# ---------------------------------------------------------------- filtres

REJET_CONTRAT = re.compile(
    r"\b(stage|stagiaire|internship|intern|apprentissage|alternance|apprenti|"
    r"working student|werkstudent)\b", re.I)
REJET_METIER = re.compile(
    r"\b(developer|developpeur|d[ée]veloppeur|engineer|ing[ée]nieur|software|devops|cloud|"
    r"cyber|data scientist|data engineer|actuaire|actuarial|comptab|accountant|audit|"
    r"juridique|legal|counsel|ressources humaines|human resources|recruiter|trade finance|"
    r"credit risk|risque de cr[ée]dit|compliance|conformit[ée]|regulatory reporting|"
    r"trader|trading|marketing manager)\b", re.I)
REJET_ASSURANCE = re.compile(
    r"\b(iard|construction|sant[ée]|pr[ée]voyance|sinistre|claims|underwriting|dommages)\b", re.I)
REJET_BACKOFFICE = re.compile(
    r"\b(back[- ]office|saisie|data entry|gestion administrative des contrats)\b", re.I)
ANNEES = re.compile(r"(\d{1,2})\s*(?:\+|à|-|to)?\s*(\d{1,2})?\s*(?:ans|years)", re.I)
IDF = re.compile(
    r"\b(paris|la d[ée]fense|courbevoie|puteaux|nanterre|levallois|neuilly|boulogne|issy|"
    r"montrouge|saint-?ouen|clichy|[îi]le-?de-?france|9[2-5]\d{3}|7[5-8]\d{3})\b", re.I)
PAYS_OK = re.compile(
    r"\b(france|luxembourg|belgi|suisse|switzerland|schweiz|geneva|gen[èe]ve|zurich|"
    r"deutschland|germany|munich|m[üu]nchen|frankfurt|nederland|netherlands|amsterdam|"
    r"ireland|irlande|dublin|espa|madrid|italia|italie|milano|milan|bruxelles|brussels)\b", re.I)


def filtrer(o, jours=7):
    blob = f"{o['poste']} {o['contrat']} {o['texte'][:6000]}"
    lieu = f"{o['ville']} {o['pays']}"
    if REJET_CONTRAT.search(f"{o['poste']} {o['contrat']}") or \
       REJET_CONTRAT.search(o["texte"][:1500]):
        return "contrat : stage ou alternance"
    if "france" in lieu.lower() and not IDF.search(lieu):
        return f"lieu hors Ile-de-France ({lieu.strip()})"
    if lieu.strip() and not IDF.search(lieu) and not PAYS_OK.search(lieu):
        return f"pays hors perimetre ({lieu.strip()})"
    if REJET_METIER.search(o["poste"]):
        return "metier hors perimetre"
    if REJET_ASSURANCE.search(o["poste"]):
        return "assurance non epargne"
    if REJET_BACKOFFICE.search(o["poste"]):
        return "back-office ou saisie"
    m = ANNEES.search(blob)
    if m and int(m.group(1)) > 6:
        return f"seniorite : {m.group(1)} ans exiges"
    if o["publie"]:
        try:
            if datetime.strptime(o["publie"], "%Y-%m-%d") < datetime.now() - timedelta(days=jours):
                return f"publiee le {o['publie']}"
        except ValueError:
            pass
    return None


def nettoyer_html(h):
    h = re.sub(r"<br\s*/?>|</p>|</li>|</div>|</h\d>", "\n", h or "")
    h = re.sub(r"<[^>]+>", "", h)
    for a, b in (("&amp;", "&"), ("&nbsp;", " "), ("&#39;", "'"), ("&quot;", '"'),
                 ("&lt;", "<"), ("&gt;", ">"), ("&rsquo;", "'")):
        h = h.replace(a, b)
    return re.sub(r"\n{3,}", "\n\n", h).strip()


def completer(o):
    lignes = [l.strip("•-– \t") for l in o["texte"].split("\n") if 40 < len(l.strip()) < 300]
    o.setdefault("visa", "non mentionne")
    o["missions"] = lignes[:3]
    o["exigences"] = lignes[-2:] if len(lignes) > 4 else []
    return o


def cle(o):
    return f"{o.get('societe','')}|{o.get('poste','')}|{o.get('ville','')}".lower()


# ---------------------------------------------------------------- memoire

def lire(fichier, defaut):
    if os.path.exists(fichier):
        try:
            with open(fichier, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return defaut


def ecrire(fichier, data):
    with open(fichier, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def un_tour(verbeux=True):
    """Collecte, filtre, et renvoie uniquement les offres jamais vues."""
    vues = set(lire(FICHIER_VUES, []))
    neuves, rejets = [], []
    for c in cibles():
        ats = c["ats"]
        try:
            for mot in (MOTS_CLES if ats in ("workday", "smartrecruiters") else [""]):
                if ats == "greenhouse":
                    flux = depuis_greenhouse(c["token"], c["societe"])
                elif ats == "lever":
                    flux = depuis_lever(c["company"], c["societe"])
                elif ats == "ashby":
                    flux = depuis_ashby(c["company"], c["societe"])
                elif ats == "smartrecruiters":
                    flux = depuis_smartrecruiters(c["company"], c["societe"], mot)
                elif ats == "workday":
                    flux = depuis_workday(c["tenant"], c.get("host", "wd3"),
                                          c["site"], c["societe"], mot)
                else:
                    if verbeux:
                        print(f"   ATS non gere : {ats} ({c['societe']})")
                    break
                for o in flux:
                    k = cle(o)
                    if k in vues:
                        continue
                    vues.add(k)
                    motif = filtrer(o)
                    if motif:
                        rejets.append(f"{o['societe']} - {o['poste']} — {motif}")
                    else:
                        neuves.append(completer(o))
        except Exception as e:
            if verbeux:
                print(f"   echec {c['societe']} : {str(e)[:120]}")
    ecrire(FICHIER_VUES, sorted(vues))
    if verbeux:
        print(f"   {len(neuves)} nouvelle(s) retenue(s), {len(rejets)} rejetee(s)")
        for r in rejets[:20]:
            print("   x", r)
    return neuves


# ---------------------------------------------------------------- serveur

class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/offres"):
            data = json.dumps(lire(FICHIER_STOCK, []), ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path in ("/", "/index.html"):
            self.path = "/swipe-offres.html"
        return super().do_GET()

    def log_message(self, *a):
        pass


def boucle_collecte():
    while True:
        print(f"[{datetime.now():%H:%M}] collecte...")
        neuves = un_tour()
        if neuves:
            stock = neuves + lire(FICHIER_STOCK, [])
            ecrire(FICHIER_STOCK, stock[:MAX_STOCK])
            print(f"[{datetime.now():%H:%M}] {len(neuves)} offre(s) envoyee(s) vers l'app")
        time.sleep(INTERVALLE)


def lancer_serveur():
    if not cibles():
        print("Aucune cible. Lance d'abord : python veille_ats.py detect")
        return
    if not os.path.exists("swipe-offres.html"):
        print("Place swipe-offres.html dans ce dossier avant de lancer serve.")
        return
    threading.Thread(target=boucle_collecte, daemon=True).start()
    print(f"App de swipe : http://localhost:{PORT}")
    print("Depuis le telephone sur le meme wifi : http://<ip-de-ton-ordi>:%d" % PORT)
    print("Ctrl+C pour arreter.")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


# ---------------------------------------------------------------- pilotage

def lancer_collecte():
    """Un tour. Empile les nouvelles offres dans offres.json, que l'app va lire."""
    neuves = un_tour()
    stock = neuves + lire(FICHIER_STOCK, [])
    ecrire(FICHIER_STOCK, stock[:MAX_STOCK])
    print(f"\n{len(neuves)} nouvelle(s) offre(s). offres.json en contient {len(stock[:MAX_STOCK])}.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "detect"
    {"detect": lancer_detection, "collect": lancer_collecte,
     "serve": lancer_serveur}.get(cmd, lancer_detection)()
