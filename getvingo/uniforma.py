"""Vetture e veicoli commerciali caricati tutti allo stesso modo (11/11/2026).

Al salvataggio marca, modello, versione e colore si correggono da soli:
1. spazi in più tolti;
2. se esiste già lo stesso valore scritto in un altro modo ("golf", "GOLF", "Golf ") si usa quello già
   presente, il più usato (modello cercato tra le auto della stessa marca, versione tra quelle dello stesso
   modello, colore tra tutte);
3. la marca prende il nome ufficiale se è tra quelle note (VW → Volkswagen, mercedes → Mercedes-Benz);
4. altrimenti: iniziale maiuscola per parola ("grigio moonstone" → "Grigio Moonstone"); le sigle restano
   maiuscole (GTI, TDI, BMW), le parole con cifre in maiuscolo (a3 → A3, cx-30 → CX-30) e quelle già
   scritte a lettere miste restano come sono (eTSI, iX)."""
import unicodedata
from collections import Counter

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


def formatta(testo):
    return " ".join(_parola(w) for w in (testo or "").split())


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
    if campo == "marca" and testo.lower() in MARCHE:
        return MARCHE[testo.lower()]
    filtri = {"marca": marca} if campo == "modello" else (
        {"marca": marca, "modello": modello} if campo == "versione" else {})
    return _gia_usato(campo, testo, escludi_id, **filtri) or formatta(testo)


def auto(v):
    """Corregge sul posto marca, modello, versione e colore dell'auto (da chiamare prima del salvataggio)."""
    v.marca = valore("marca", v.marca, v.id)
    v.modello = valore("modello", v.modello, v.id, marca=v.marca)
    v.versione = valore("versione", v.versione, v.id, marca=v.marca, modello=v.modello)
    v.colore = valore("colore", v.colore, v.id)
    return v


# ---------------------------------------------------------------- correzione una tantum delle auto già presenti

CAMPI = (("marca", "Marca"), ("modello", "Modello"), ("versione", "Versione"), ("colore", "Colore"))


def _scelta(scritture):
    """Fra le scritture dello stesso valore: la più usata, poi con le regole di scrittura."""
    return formatta(scritture.most_common(1)[0][0])


def proposte():
    """Correzioni per le auto già presenti, senza salvarle: [(veicolo, campo, etichetta, prima, dopo)].
    Stesse regole del salvataggio; fra più scritture dello stesso valore vince la più usata."""
    from .models import Veicolo
    auto_ = Veicolo.query.order_by(Veicolo.marca, Veicolo.modello, Veicolo.id).all()
    nuova = {v.id: {} for v in auto_}

    def pulito(t):
        return " ".join((t or "").split())

    def uniforma_campo(campo, contesto, ufficiale=None):
        gruppi = {}
        for v in auto_:
            t = pulito(getattr(v, campo))
            if t:
                gruppi.setdefault((contesto(v), chiave(t)), Counter())[t] += 1
        scelte = {k: _scelta(c) for k, c in gruppi.items()}
        for v in auto_:
            t = pulito(getattr(v, campo))
            if not t:
                nuova[v.id][campo] = getattr(v, campo)
            else:
                nuova[v.id][campo] = (ufficiale or {}).get(t.lower()) or scelte[(contesto(v), chiave(t))]

    uniforma_campo("marca", lambda v: "", MARCHE)
    uniforma_campo("modello", lambda v: chiave(nuova[v.id]["marca"]))
    uniforma_campo("versione", lambda v: (chiave(nuova[v.id]["marca"]), chiave(nuova[v.id]["modello"])))
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
