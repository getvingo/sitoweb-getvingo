"""Vetture e veicoli commerciali caricati tutti allo stesso modo (11/11/2026).

Al salvataggio marca, modello, versione e colore si correggono da soli:
1. spazi in più tolti;
2. se esiste già lo stesso valore scritto in un altro modo ("golf", "GOLF", "Golf ") si usa quello già
   presente, il più usato (modello cercato tra le auto della stessa marca, versione tra quelle dello stesso
   modello, colore tra tutte);
3. la marca prende il nome ufficiale se è tra quelle note (VW → Volkswagen, mercedes → Mercedes-Benz);
4. altrimenti: iniziale maiuscola per parola ("grigio moonstone" → "Grigio Moonstone"); le sigle restano
   maiuscole (GTI, TDI, BMW), le parole con cifre in maiuscolo (a3 → A3, cx-30 → CX-30) e quelle già
   scritte a lettere miste restano come sono (eTSI, iX).
Prima di tutto (15/11/2026) vale il **catalogo delle vetture** caricato da Gestione (CatalogoAuto): se marca,
modello o versione corrispondono a una sua voce (stessa chiave), si usa la scrittura del catalogo così com'è."""
import csv
import io
import re
import unicodedata
import zipfile
from collections import Counter
from xml.etree import ElementTree

from flask import g, has_app_context

from .vin import WMI

MARCHE = {m.lower(): m for m in WMI.values()}
MARCHE.update({"vw": "Volkswagen", "mercedes": "Mercedes-Benz", "mercedes benz": "Mercedes-Benz",
               "alfa": "Alfa Romeo", "alfaromeo": "Alfa Romeo", "land rover": "Land Rover", "landrover": "Land Rover",
               "mini": "Mini", "ds": "DS", "mg": "MG", "byd": "BYD", "kia": "Kia", "seat": "Seat", "cupra": "Cupra",
               "skoda": "Škoda", "citroen": "Citroën"})  # come nell'elenco dei costruttori (vin.WMI)
SIGLE = {"gti", "gtd", "gte", "gt", "tdi", "tsi", "tfsi", "dci", "hdi", "crdi", "jtd", "suv", "amg", "rs", "st",
         "cng", "lpg", "gpl", "phev", "hev", "mhev", "bev", "ev", "awd", "4wd", "fwd", "rwd", "bmw", "dsg", "cv", "kw"}
PAROLE_FISSE = {"e-tron": "e-tron", "etsi": "eTSI", "etfsi": "eTFSI", "ehybrid": "eHybrid", "e-hybrid": "e-Hybrid",
                "xdrive": "xDrive", "sdrive": "sDrive", "4motion": "4Motion", "bluehdi": "BlueHDi", "puretech": "PureTech",
                "ecoblue": "EcoBlue", "ecoboost": "EcoBoost", "multijet": "MultiJet", "tce": "TCe", "dci": "dCi"}


def chiave(testo):
    """Forma per confrontare due scritture: minuscolo, senza accenti, spazi, trattini e punti."""
    t = unicodedata.normalize("NFKD", (testo or "").lower())
    return "".join(c for c in t if c.isalnum())


def _pezzo(w):
    if w.lower() in PAROLE_FISSE:
        return PAROLE_FISSE[w.lower()]
    if any(c.isdigit() for c in w):
        # codici: a3 → A3, x3 → X3, id.4 → ID.4; quelli che iniziano con una cifra restano (118i, 600e, 4xe)
        return w.upper() if w[:1].isalpha() and w == w.lower() else w
    if w.lower() in SIGLE:
        return w.upper()
    if w.isupper() and len(w) <= 3:  # sigle brevi scritte in maiuscolo: VZ, LX, HR, DS
        return w
    if w not in (w.lower(), w.upper()):  # lettere miste scritte apposta: eTSI, iX
        return w
    return w[:1].upper() + w[1:].lower()


def _parola(w):
    if w.lower() in PAROLE_FISSE:
        return PAROLE_FISSE[w.lower()]
    if any(c.isdigit() for c in w) and w[:1].isalpha() and w == w.lower():
        return w.upper()  # cx-30 → CX-30
    pezzi = w.split("-")
    # "e-" minuscola davanti al trattino è voluta: e-Power, e-tron, e-Hybrid
    return "-".join(p if i == 0 and p == "e" and len(pezzi) > 1 else _pezzo(p) for i, p in enumerate(pezzi))


def _colore(w):
    # colori: sempre iniziale maiuscola e il resto minuscolo (BLU → Blu, bIANCO → Bianco), anche col trattino
    return "-".join(p[:1].upper() + p[1:].lower() for p in w.split("-"))


def formatta(testo, campo=None):
    parola = _colore if campo == "colore" else _parola
    return " ".join(parola(w) for w in (testo or "").split())


def _gia_usato(campo, valore, escludi_id=None, **filtri):
    """Lo stesso valore già presente in Getvingo scritto in un altro modo (il più usato), o None."""
    from .models import Veicolo, db
    k = chiave(valore)
    if not k:
        return None
    colonna = getattr(Veicolo, campo)
    with db.session.no_autoflush:
        q = Veicolo.query.with_entities(colonna).filter(colonna.isnot(None), colonna != "")
        for nome, v in filtri.items():
            q = q.filter(getattr(Veicolo, nome) == v)
        if escludi_id:
            q = q.filter(Veicolo.id != escludi_id)
        conta = Counter(r[0] for r in q if chiave(r[0]) == k)
    return conta.most_common(1)[0][0] if conta else None


def valore(campo, testo, escludi_id=None, marca=None, modello=None):
    testo = " ".join((testo or "").split())
    if not testo:
        return testo
    dal_catalogo = da_catalogo(campo, testo, marca, modello)
    if dal_catalogo:
        return dal_catalogo
    if campo == "marca" and testo.lower() in MARCHE:
        return MARCHE[testo.lower()]
    filtri = {"marca": marca} if campo == "modello" else (
        {"marca": marca, "modello": modello} if campo == "versione" else {})
    # anche il valore già presente passa dalle regole: se era scritto male (es. "bianco perla") non si ricopia
    return formatta(_gia_usato(campo, testo, escludi_id, **filtri) or testo, campo)


def auto(v):
    """Corregge sul posto marca, modello, versione e colore dell'auto (da chiamare prima del salvataggio)."""
    v.marca = valore("marca", v.marca, v.id)
    v.modello = valore("modello", v.modello, v.id, marca=v.marca)
    v.versione = valore("versione", v.versione, v.id, marca=v.marca, modello=v.modello)
    v.colore = valore("colore", v.colore, v.id)
    return v


# ---------------------------------------------------------------- correzione una tantum delle auto già presenti

CAMPI = (("marca", "Marca"), ("modello", "Modello"), ("versione", "Versione"), ("colore", "Colore"))


def _scelta(scritture, campo=None):
    """Fra le scritture dello stesso valore: la più usata, poi con le regole di scrittura."""
    return formatta(scritture.most_common(1)[0][0], campo)


def proposte():
    """Correzioni per le auto già presenti, senza salvarle: [(veicolo, campo, etichetta, prima, dopo)].
    Stesse regole del salvataggio; fra più scritture dello stesso valore vince la più usata."""
    from .models import Veicolo
    auto_ = Veicolo.query.order_by(Veicolo.marca, Veicolo.modello, Veicolo.id).all()
    nuova = {v.id: {} for v in auto_}

    def pulito(t):
        return " ".join((t or "").split())

    def uniforma_campo(campo, contesto, ufficiale=None, catalogo_=lambda v, t: None):
        gruppi = {}
        for v in auto_:
            t = pulito(getattr(v, campo))
            if t:
                gruppi.setdefault((contesto(v), chiave(t)), Counter())[t] += 1
        scelte = {k: _scelta(c, campo) for k, c in gruppi.items()}
        for v in auto_:
            t = pulito(getattr(v, campo))
            if not t:
                nuova[v.id][campo] = getattr(v, campo)
            else:
                nuova[v.id][campo] = catalogo_(v, t) or (ufficiale or {}).get(t.lower()) \
                    or scelte[(contesto(v), chiave(t))]

    uniforma_campo("marca", lambda v: "", MARCHE, lambda v, t: da_catalogo("marca", t))
    uniforma_campo("modello", lambda v: chiave(nuova[v.id]["marca"]),
                   catalogo_=lambda v, t: da_catalogo("modello", t, nuova[v.id]["marca"]))
    uniforma_campo("versione", lambda v: (chiave(nuova[v.id]["marca"]), chiave(nuova[v.id]["modello"])),
                   catalogo_=lambda v, t: da_catalogo("versione", t, nuova[v.id]["marca"], nuova[v.id]["modello"]))
    uniforma_campo("colore", lambda v: "")
    elenco = []
    for v in auto_:
        for campo, etichetta in CAMPI:
            prima, dopo = getattr(v, campo), nuova[v.id][campo]
            if (prima or "") != (dopo or ""):
                elenco.append((v, campo, etichetta, prima, dopo))
    return elenco


def applica():
    """Applica le correzioni proposte (le ricalcola al momento). Ritorna quante modifiche ha fatto."""
    elenco = proposte()
    for v, campo, _, _, dopo in elenco:
        setattr(v, campo, dopo)
    return len(elenco)


# ---------------------------------------------------------------- catalogo delle vetture (Gestione, 15/11/2026)

def catalogo():
    """Il catalogo della concessionaria pronto per i confronti (una lettura per richiesta):
    {"marche": {k: Marca}, "modelli": {k_marca: {k: Modello}}, "versioni": {(k_marca, k_modello): {k: Versione}}}."""
    from .models import CatalogoAuto, org_attiva
    tieni = has_app_context()
    if tieni and g.get("_catalogo_auto") is not None and g.get("_catalogo_org") == org_attiva():
        return g._catalogo_auto
    c = {"marche": {}, "modelli": {}, "versioni": {}}
    for r in CatalogoAuto.query.order_by(CatalogoAuto.id):
        km, kmod = chiave(r.marca), chiave(r.modello)
        c["marche"].setdefault(km, r.marca)
        c["modelli"].setdefault(km, {}).setdefault(kmod, r.modello)
        if r.versione:
            c["versioni"].setdefault((km, kmod), {}).setdefault(chiave(r.versione), r.versione)
    if tieni:
        g._catalogo_auto, g._catalogo_org = c, org_attiva()
    return c


def svuota_cache_catalogo():
    if has_app_context():
        g.pop("_catalogo_auto", None)


def da_catalogo(campo, testo, marca=None, modello=None):
    """La scrittura del catalogo per questo valore (stessa chiave), o None se il catalogo non lo ha."""
    k = chiave(testo)
    if not k:
        return None
    c = catalogo()
    km = chiave(c["marche"].get(chiave(marca), marca)) if marca else ""
    if campo == "marca":
        return c["marche"].get(k)
    if campo == "modello":
        return c["modelli"].get(km, {}).get(k)
    if campo == "versione":
        return c["versioni"].get((km, chiave(modello)), {}).get(k)
    return None


def opzioni_catalogo(marca=None, modello=None):
    """Marche, modelli della marca e versioni del modello del catalogo, nell'ordine del file."""
    c = catalogo()
    km, kmod = chiave(marca), chiave(modello)
    return {"marche": list(c["marche"].values()),
            "modelli": list(c["modelli"].get(km, {}).values()) if km else [],
            "versioni": list(c["versioni"].get((km, kmod), {}).values()) if km and kmod else []}


def _righe_xlsx(dati):
    """Righe del primo foglio di un file Excel .xlsx (solo i valori, senza librerie esterne)."""
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(dati)) as z:
        condivise = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ElementTree.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns):
                condivise.append("".join(t.text or "" for t in si.iter("{%s}t" % ns["m"])))
        fogli = sorted(n for n in z.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
        if not fogli:
            return []
        foglio = "xl/worksheets/sheet1.xml" if "xl/worksheets/sheet1.xml" in fogli else fogli[0]
        righe = []
        for riga in ElementTree.fromstring(z.read(foglio)).iter("{%s}row" % ns["m"]):
            celle = {}
            for c in riga.findall("m:c", ns):
                lettere = re.match(r"[A-Z]+", c.get("r", "")).group(0) if c.get("r") else None
                col = 0
                for ch in lettere or "":
                    col = col * 26 + ord(ch) - 64
                col = col - 1 if lettere else len(celle)
                t, v = c.get("t"), c.find("m:v", ns)
                if t == "s" and v is not None:
                    testo = condivise[int(v.text)]
                elif t == "inlineStr":
                    testo = "".join(x.text or "" for x in c.iter("{%s}t" % ns["m"]))
                else:
                    testo = v.text if v is not None and v.text else ""
                    if re.fullmatch(r"-?\d+\.0", testo):
                        testo = testo[:-2]  # numeri interi salvati come 2.0
                celle[col] = testo
            if celle:
                righe.append([celle.get(i, "") for i in range(max(celle) + 1)])
        return righe


def leggi_file_catalogo(nome, dati):
    """Voci (marca, modello, versione) da un file CSV (; , o tab) o Excel .xlsx.
    Colonne riconosciute dall'intestazione (marca/brand, modello/model, versione/allestimento); senza intestazione
    valgono nell'ordine marca, modello, versione. Ritorna (voci, righe_scartate)."""
    if (nome or "").lower().endswith(".xlsx") or dati[:2] == b"PK":
        righe = _righe_xlsx(dati)
    else:
        testo = dati.decode("utf-8-sig", errors="replace")
        if "\ufffd" in testo:
            testo = dati.decode("cp1252", errors="replace")  # CSV salvato da Excel per Windows
        prima = next((r for r in testo.splitlines() if r.strip()), "")
        sep = max(";,\t", key=prima.count) if prima else ";"
        righe = list(csv.reader(io.StringIO(testo), delimiter=sep))
    righe = [[(x or "").strip() for x in r] for r in righe if any((x or "").strip() for x in r)]
    if not righe:
        return [], []
    nomi = {"marca": ("marca", "brand", "marchio", "costruttore", "make"),
            "modello": ("modello", "model", "modelli"),
            "versione": ("versione", "versioni", "allestimento", "version", "variante", "trim")}
    testa = [chiave(x) for x in righe[0]]
    pos = {campo: next((i for i, t in enumerate(testa) if t in alias), None) for campo, alias in nomi.items()}
    inizio = 1
    if pos["marca"] is not None and pos["modello"] is not None:
        righe, inizio = righe[1:], 2
    else:
        pos = {"marca": 0, "modello": 1, "versione": 2}
    voci, scartate, viste = [], [], set()
    for n, r in enumerate(righe, start=inizio):
        prendi = lambda campo: " ".join(r[pos[campo]].split()) if pos[campo] is not None and pos[campo] < len(r) else ""  # noqa: E731
        marca, modello, versione = prendi("marca")[:80], prendi("modello")[:120], prendi("versione")[:120]
        if not marca or not modello:
            scartate.append(n)
            continue
        k = (chiave(marca), chiave(modello), chiave(versione))
        if k in viste:
            continue
        viste.add(k)
        voci.append((marca, modello, versione))
    return voci, scartate
