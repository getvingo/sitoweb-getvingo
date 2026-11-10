"""Analisi dei tempi (11/11/2026): dalla vendita alla consegna, fase per fase, fornitori e tratte.

Usa solo dati che il portale registra già: partenza e arrivo dei DDT, fine lavorazione, passi della
preparazione, consegna, più i tre dati aggiunti per le analisi (data di vendita, prima stima della lavorazione,
stima del viaggio fissata alla partenza). Niente analisi per singola persona: vedi CLAUDE.md.
"""
from datetime import datetime, timedelta

from .models import Movimento, Preparazione, Veicolo, adesso

PERIODI = {"30": ("30 giorni", 30), "90": ("90 giorni", 90), "365": ("12 mesi", 365), "tutto": ("Tutto", None)}


def _ore(delta):
    return delta.total_seconds() / 3600


def media(valori):
    return sum(valori) / len(valori) if valori else None


def durata(ore):
    """Testo leggibile: minuti, ore o giorni (con una cifra se serve)."""
    if ore is None:
        return "—"
    if ore < 1:
        return f"{max(1, round(ore * 60))} min"
    if ore < 24:
        return f"{ore:.1f} h".replace(".0 h", " h").replace(".", ",")
    giorni = ore / 24
    return (f"{giorni:.1f} giorni" if giorni < 10 else f"{round(giorni)} giorni").replace(".", ",")


def _validi(movimenti):
    return [m for m in movimenti if not (m.ddt and m.ddt.annullato_at)]


def calcola(periodo="90"):
    giorni = PERIODI.get(periodo, PERIODI["90"])[1]
    dal = adesso() - timedelta(days=giorni) if giorni else datetime(2000, 1, 1)
    movimenti = _validi(Movimento.query.filter(Movimento.arrivo_at.isnot(None)).all())

    # --- viaggi: partenza -> arrivo, con la stima fissata alla partenza
    viaggi = [m for m in movimenti if m.partenza_at and m.arrivo_at >= dal and m.arrivo_at > m.partenza_at]
    # --- lavorazioni dai fornitori: arrivo -> fine lavorazione, poi attesa del ritiro
    lavori = [m for m in movimenti if m.con_lavorazione and m.lavorazione_completata_at
              and m.lavorazione_completata_at >= dal and m.lavorazione_completata_at > m.arrivo_at]
    partenze = {}
    for m in Movimento.query.filter(Movimento.partenza_at.isnot(None)).all():
        partenze.setdefault((m.veicolo_id, m.da_sede_id), []).append(m.partenza_at)

    def attesa_ritiro(m):
        dopo = [p for p in partenze.get((m.veicolo_id, m.a_sede_id), []) if p > m.lavorazione_completata_at]
        return _ore(min(dopo) - m.lavorazione_completata_at) if dopo else None

    # --- preparazione: avvio -> "Pronta"; poi pronta -> consegnata
    prep_ore, attesa_consegna = [], []
    for p in Preparazione.query.all():
        pronta = next((f.at for f in p.fasi if f.indice == len(Preparazione.FASI) - 1), None)
        if pronta and pronta >= dal and pronta > p.creata_at:
            prep_ore.append(_ore(pronta - p.creata_at))
        v = p.veicolo
        if pronta and v.consegnato_at and v.consegnato_at >= dal and v.consegnato_at > pronta:
            attesa_consegna.append(_ore(v.consegnato_at - pronta))
    # --- dalla vendita alla consegna
    vendute = [v for v in Veicolo.query.filter(Veicolo.stato == "consegnato", Veicolo.venduta_il.isnot(None)).all()
               if v.consegnato_at and v.consegnato_at >= dal]
    vendita_consegna = [max(0, (v.consegnato_at.date() - v.venduta_il).days) * 24 for v in vendute]

    attese = [a for a in (attesa_ritiro(m) for m in lavori) if a is not None]
    fasi = [
        {"nome": "Viaggio (DDT)", "ore": media([_ore(m.arrivo_at - m.partenza_at) for m in viaggi]), "n": len(viaggi),
         "cosa": "dalla partenza all'arrivo confermato"},
        {"nome": "Lavorazione dal fornitore", "ore": media([_ore(m.lavorazione_completata_at - m.arrivo_at) for m in lavori]),
         "n": len(lavori), "cosa": "dall'arrivo alla fine segnata dal fornitore"},
        {"nome": "Attesa del ritiro", "ore": media(attese), "n": len(attese),
         "cosa": "da lavorazione finita alla partenza del rientro", "attesa": True},
        {"nome": "Preparazione", "ore": media(prep_ore), "n": len(prep_ore), "cosa": "dall'avvio a “Pronta”"},
        {"nome": "Pronta, in attesa di consegna", "ore": media(attesa_consegna), "n": len(attesa_consegna),
         "cosa": "da “Pronta” alla consegna al cliente", "attesa": True},
    ]
    massimo = max([f["ore"] for f in fasi if f["ore"]] or [1])
    for f in fasi:
        f["quota"] = round(100 * f["ore"] / massimo) if f["ore"] else 0
        f["testo"] = durata(f["ore"])
    con_dati = [f for f in fasi if f["ore"]]
    piu_lunga = max(con_dati, key=lambda f: f["ore"])["nome"] if con_dati else None

    # --- fornitori: tempi, rispetto della prima stima, attesa del ritiro, auto ancora da loro
    fornitori = {}
    for m in lavori:
        sede = m.sede_arrivo_effettiva
        r = fornitori.setdefault(sede.id, {"nome": sede.nome, "tipo": sede.TIPI.get(sede.tipo, ""), "giorni": [],
                                           "entro": 0, "ritardi": [], "attese": []})
        giorni_veri = _ore(m.lavorazione_completata_at - m.arrivo_at) / 24
        promessa = m.lavorazione_stima_iniziale or m.stima_giorni
        r["giorni"].append(giorni_veri)
        if giorni_veri <= promessa + 0.5:  # mezza giornata di tolleranza
            r["entro"] += 1
        else:
            r["ritardi"].append(giorni_veri - promessa)
        a = attesa_ritiro(m)
        if a is not None:
            r["attese"].append(a)
    righe_f = []
    for r in fornitori.values():
        n = len(r["giorni"])
        righe_f.append({"nome": r["nome"], "tipo": r["tipo"], "n": n, "giorni": media(r["giorni"]),
                        "entro_pct": round(100 * r["entro"] / n), "ritardo": media(r["ritardi"]),
                        "attesa": media(r["attese"])})
    righe_f.sort(key=lambda r: -r["n"])
    max_g = max([r["giorni"] for r in righe_f] or [1])
    for r in righe_f:
        r["quota"] = round(100 * r["giorni"] / max_g) if max_g else 0

    # --- tratte: tempo vero e stimato alla partenza
    tratte = {}
    for m in viaggi:
        chiave = (m.da_sede.nome if m.da_sede else "—", m.sede_arrivo_effettiva.nome)
        t = tratte.setdefault(chiave, {"veri": [], "stime": [], "coppie": []})
        vero = _ore(m.arrivo_at - m.partenza_at) * 60
        t["veri"].append(vero)
        if m.stima_viaggio_min:
            t["stime"].append(m.stima_viaggio_min)
            t["coppie"].append((vero, m.stima_viaggio_min))
    righe_t = []
    for (da, a), t in tratte.items():
        scarto = None
        if t["coppie"]:
            scarto = round(100 * (sum(v for v, _ in t["coppie"]) / sum(s for _, s in t["coppie"]) - 1))
        righe_t.append({"da": da, "a": a, "n": len(t["veri"]), "vero": durata(media(t["veri"]) / 60),
                        "stima": durata(media(t["stime"]) / 60) if t["stime"] else None, "scarto": scarto})
    righe_t.sort(key=lambda r: -r["n"])

    return {"periodo": periodo if periodo in PERIODI else "90", "fasi": fasi, "piu_lunga": piu_lunga,
            "vendita": {"ore": media(vendita_consegna), "testo": durata(media(vendita_consegna)), "n": len(vendute)},
            "fornitori": righe_f, "tratte": righe_t[:15],
            "con_stima_viaggio": sum(1 for m in viaggi if m.stima_viaggio_min), "viaggi": len(viaggi)}
