"""Statistiche (22/11/2026, richiesta dell'utente: "Report" diventa "Statistiche" con dati su tutto: movimentazioni,
stock, persone, tempi, preparazioni, lavorazioni, danni). Si aggiungono ai tempi di analisi.py (fasi, fornitori,
tratte). Tutto per il periodo scelto (30/90 giorni, 12 mesi, tutto); lo stock è sempre la situazione di adesso.
Nota: i dati per persona sono conteggi di azioni registrate; per usarli per valutare il personale serve prima il
parere del consulente del lavoro (art. 4 Statuto dei lavoratori)."""
from collections import Counter, OrderedDict, defaultdict
from datetime import datetime, timedelta

from . import servizi
from .analisi import PERIODI, durata
from .models import Azione, Danno, Ddt, Evento, Movimento, Preparazione, Utente, Veicolo, adesso, db

MESI = ("gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic")


def _quota(righe, campo="n"):
    """Larghezza della barra di ogni riga rispetto alla più grande (0–100)."""
    m = max([r[campo] for r in righe] or [0]) or 1
    for r in righe:
        r["quota"] = round(100 * r[campo] / m)
    return righe


def _conteggio(cnt, chiavi=None):
    righe = [{"nome": k or "—", "n": n} for k, n in (cnt.most_common() if chiavi is None else ((k, cnt.get(k, 0)) for k in chiavi))]
    return _quota(righe)


def calcola(periodo="90"):
    giorni = PERIODI.get(periodo, PERIODI["90"])[1]
    ora = adesso()
    dal = ora - timedelta(days=giorni) if giorni else datetime(2000, 1, 1)

    # ---- movimentazioni
    ddt = [d for d in Ddt.query.filter(Ddt.emesso_at >= dal, Ddt.annullato_at.is_(None)).all()]
    movimenti = [m for d in ddt for m in (d.movimenti or [d.movimento])]
    per_mese = OrderedDict()
    for d in sorted(ddt, key=lambda d: d.emesso_at):
        k = f"{MESI[d.emesso_at.month - 1]} {d.emesso_at.year}"
        per_mese[k] = per_mese.get(k, 0) + 1
    km = sum(m.stima_viaggio_km or 0 for m in movimenti)
    movimentazioni = {
        "ddt": len(ddt), "auto": len(movimenti), "km": round(km),
        "per_mese": _quota([{"nome": k, "n": n} for k, n in per_mese.items()]),
        "causale": _conteggio(Counter(m.causale for m in movimenti)),
        "mezzo": _conteggio(Counter(m.mezzo or "non indicata" for m in movimenti)),
        "vettore": _conteggio(Counter(m.vettore.nome if m.vettore else "—" for m in movimenti)),
        "destinazione": _conteggio(Counter(m.a_sede.nome if m.a_sede else "—" for m in movimenti))[:10],
        "multipli": sum(1 for d in ddt if d.multiplo),
    }

    # ---- stock (adesso)
    attive = Veicolo.query.filter(Veicolo.stato != "consegnato").all()
    from .models import Veicolo as V
    fasi = Counter(v.fase for v in attive)
    eta = Counter()
    giacenze = []
    for v in attive:
        g = (ora - v.creato_at).days if v.creato_at else 0
        giacenze.append(g)
        eta["meno di 30 giorni" if g < 30 else "30–60 giorni" if g < 60 else "60–90 giorni" if g < 90 else "oltre 90 giorni"] += 1
    stock = {
        "totale": len(attive),
        "fase": _conteggio(Counter({V.FASI.get(k, k): n for k, n in fasi.items()})),
        "sede": _conteggio(Counter(v.sede.nome if v.sede else "in viaggio" for v in attive))[:12],
        "marca": _conteggio(Counter(v.marca or "—" for v in attive))[:12],
        "condizione": _conteggio(Counter({"nuovo": "Nuove", "usato": "Usate"}.get(v.condizione, "Da indicare") for v in attive)),
        "tipologia": _conteggio(Counter("Commerciali" if v.tipologia == "commerciale" else "Vetture" for v in attive)),
        "alimentazione": _conteggio(Counter(v.alimentazione or "Da indicare" for v in attive)),
        "eta": _conteggio(eta, ["meno di 30 giorni", "30–60 giorni", "60–90 giorni", "oltre 90 giorni"]),
        "giacenza_media": round(sum(giacenze) / len(giacenze)) if giacenze else 0,
        "entrate": Veicolo.query.filter(Veicolo.creato_at >= dal).count(),
    }

    # ---- consegne
    consegnate = [v for v in Veicolo.query.filter(Veicolo.stato == "consegnato", Veicolo.consegnato_at >= dal).all()
                  if not v.trasferita_a]
    cons_mese = OrderedDict()
    for v in sorted(consegnate, key=lambda v: v.consegnato_at):
        k = f"{MESI[v.consegnato_at.month - 1]} {v.consegnato_at.year}"
        cons_mese[k] = cons_mese.get(k, 0) + 1
    consegne = {"n": len(consegnate), "per_mese": _quota([{"nome": k, "n": n} for k, n in cons_mese.items()]),
                "sede": _conteggio(Counter(v.sede.nome if v.sede else "—" for v in consegnate))}

    # ---- preparazioni
    finite = Preparazione.query.filter(Preparazione.completata_at >= dal).all()
    durate = [(p.completata_at - p.creata_at).total_seconds() / 3600 for p in finite if p.completata_at > p.creata_at]
    con_orario = [p for p in finite if p.pronta_entro_at]
    puntuali = [p for p in con_orario if p.completata_at <= p.pronta_entro_at]
    in_corso = Preparazione.query.filter(Preparazione.completata_at.is_(None)).all()
    preparazioni = {
        "finite": len(finite), "in_corso": len(in_corso),
        "durata": durata(sum(durate) / len(durate), lavorativa=False) if durate else "—",
        "puntuali_pct": round(100 * len(puntuali) / len(con_orario)) if con_orario else None,
        "in_ritardo": sum(1 for p in in_corso if p.pronta_entro_at and p.pronta_entro_at < ora),
        "attivita": _conteggio(Counter(p.prossima_fase for p in in_corso), list(Preparazione.FASI)),
    }

    # ---- lavorazioni (dai fornitori)
    lav_finite = [m for m in Movimento.query.filter(Movimento.lavorazione_completata_at >= dal).all() if m.con_lavorazione]
    per_tipo = defaultdict(lambda: {"n": 0, "giorni": [], "nei_tempi": 0})
    for m in lav_finite:
        r = per_tipo[m.lavorazione]
        r["n"] += 1
        if m.arrivo_at:
            r["giorni"].append(servizi.giorni_lavorativi_tra(m.arrivo_at, m.lavorazione_completata_at))
        if m.lavorazione_prevista_at and m.lavorazione_completata_at <= m.lavorazione_prevista_at:
            r["nei_tempi"] += 1
    tipi = _quota([{"nome": k, "n": r["n"], "giorni": round(sum(r["giorni"]) / len(r["giorni"]), 1) if r["giorni"] else None,
                    "pct": round(100 * r["nei_tempi"] / r["n"])} for k, r in sorted(per_tipo.items(), key=lambda x: -x[1]["n"])])
    lavorazioni = {"finite": len(lav_finite), "tipi": tipi}

    # ---- persone: conteggi di azioni registrate nel periodo (non valutazioni)
    nomi = {"partenza": "DDT e partenze", "presa_in_carico": "Prese in carico", "arrivo": "Arrivi confermati",
            "lavorazione_completata": "Fine lavori", "preparazione_fase": "Attività di preparazione",
            "consegna": "Consegne", "danno": "Danni segnalati", "predisposizione": "DDT predisposti"}
    per_utente = defaultdict(Counter)
    for a in Azione.query.filter(Azione.at >= dal, Azione.ripristinata_at.is_(None)).all():
        if a.tipo in nomi:
            per_utente[a.utente_id][a.tipo] += 1
    for e in Evento.query.filter(Evento.tipo == "abbinamento", Evento.at >= dal).all():
        per_utente[e.utente_id]["abbinamento"] += 1
    persone = []
    for uid, cnt in per_utente.items():
        u = db.session.get(Utente, uid)
        if u is None:
            continue
        persone.append({"nome": u.nome, "ruolo": u.RUOLI.get(u.ruolo, u.ruolo), "conteggi": cnt, "totale": sum(cnt.values())})
    persone.sort(key=lambda r: -r["totale"])
    colonne_persone = [("abbinamento", "Auto caricate")] + list(nomi.items())

    # ---- danni
    danni_periodo = Danno.query.filter(Danno.segnalato_at >= dal, Danno.rimosso.is_(False)).all()
    risolti = [d for d in danni_periodo if d.risolto_at]
    tempi_ris = [(d.risolto_at - d.segnalato_at).total_seconds() / 3600 for d in risolti if d.risolto_at > d.segnalato_at]
    danni = {"segnalati": len(danni_periodo), "risolti": len(risolti),
             "aperti": Danno.query.filter(Danno.risolto_at.is_(None), Danno.rimosso.is_(False)).count(),
             "tempo": durata(sum(tempi_ris) / len(tempi_ris), lavorativa=False) if tempi_ris else "—",
             "zona": _conteggio(Counter(d.zona for d in danni_periodo))}

    return {"movimentazioni": movimentazioni, "stock": stock, "consegne": consegne, "preparazioni": preparazioni,
            "lavorazioni": lavorazioni, "persone": persone, "colonne_persone": colonne_persone, "danni": danni}
