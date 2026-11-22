"""Home "Getvingo Yard" B1 (22/11/2026, specifiche getvingo-dashboard-B1-specifiche.md dell'utente).
Tutti i numeri della Home da computer, della barra laterale e del telefono del personale, presi dal database.
Regola: ogni numero dice cosa conta e su quale periodo; niente analisi per singola persona (il registro
"Chi ha fatto cosa" è un elenco di azioni, senza tempi, medie o confronti)."""
from collections import Counter, OrderedDict
from datetime import datetime, time, timedelta

from flask import g, url_for

from . import servizi
from .models import Azione, Ddt, Movimento, Preparazione, Sede, Veicolo, adesso

GIORNI = ("lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica")
GIORNI_BREVI = ("lun", "mar", "mer", "gio", "ven", "sab", "dom")
MESI = ("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre",
        "novembre", "dicembre")

# stati della barra laterale: (chiave, nome, nota sotto il nome, filtro fase dello Stock)
STATI = (("in_viaggio", "In viaggio", "su strada con DDT aperto", "in_viaggio"),
         ("lavorazione", "Dai fornitori", "in lavorazione esterna", "lavorazione"),
         ("da_ritirare", "Da ritirare", "DDT da ritirare o lavoro finito", "da_ritirare"),
         ("preparazione", "In preparazione", "in preparazione in sede", "preparazione"))
# Home gestori completa: amministratore e responsabile commerciale; gli altri partono da "Solo le mie"
RUOLI_TUTTO = ("admin",)  # 22/11/2026: "Solo le mie" di base per tutte le mansioni; "Tutte" di base solo all'amministratore


def _giorno(d):
    return datetime.combine(d, time.min), datetime.combine(d + timedelta(days=1), time.min)


def data_lunga(d):
    return f"{GIORNI[d.weekday()].capitalize()} {d.day} {MESI[d.month - 1]} {d.year}"


def attive(sede_id=None):
    q = Veicolo.query.filter(Veicolo.stato != "consegnato")
    return [v for v in q.all() if not sede_id or v.sede_id == sede_id
            or (v.movimento_aperto and sede_id in (v.movimento_aperto.da_sede_id, v.movimento_aperto.a_sede_id))]


def stati_vetture():
    """Barra laterale (ogni pagina da computer): vetture per stato + consegnate oggi e nel mese.
    Conteggi veloci: niente calcolo della fase auto per auto su tutto lo stock."""
    in_viaggio = Veicolo.query.filter_by(stato="in_viaggio").count()
    # 22/11/2026: gli stessi conteggi delle pagine che si aprono (Lavorazioni, Da ritirare)
    lavorazione = 0
    for sede, elenco in servizi.presso_fornitori():
        if not sede.esterna:
            continue
        for v, m in elenco:
            if m and m.con_lavorazione and not m.lavorazione_completata_at:
                lavorazione += 1
    n_ritirare = len(da_ritirare())
    preparazione = (Preparazione.query.join(Veicolo, Preparazione.veicolo_id == Veicolo.id)
                    .filter(Preparazione.completata_at.is_(None), Veicolo.stato == "in_sede")
                    .with_entities(Preparazione.veicolo_id).distinct().count())
    conta = {"in_viaggio": in_viaggio, "lavorazione": lavorazione, "da_ritirare": n_ritirare,
             "preparazione": preparazione}
    oggi = adesso().date()
    dal, al = _giorno(oggi)
    inizio_mese = datetime(oggi.year, oggi.month, 1)
    consegnate = [v for v in Veicolo.query.filter(Veicolo.stato == "consegnato", Veicolo.consegnato_at >= inizio_mese)
                  .with_entities(Veicolo.consegnato_at, Veicolo.trasferita_a).all() if not v.trasferita_a]
    # 22/11/2026: in viaggio e da ritirare hanno la loro pagina; dai fornitori → Lavorazioni; in preparazione → Preparazioni
    dove = {"in_viaggio": url_for("main.stato_vetture", chiave="in-viaggio"), "lavorazione": url_for("main.lavorazioni"),
            "da_ritirare": url_for("main.stato_vetture", chiave="da-ritirare"), "preparazione": url_for("main.preparazione")}
    righe = [{"chiave": k, "nome": n, "nota": nota, "n": conta[k], "url": dove[k]} for k, n, nota, f in STATI]
    righe.append({"chiave": "consegnato", "nome": "Consegnate", "nota": "calendario delle consegne", "n": None,
                  "url": url_for("main.stato_vetture", chiave="consegnate")})
    return righe


# ---------------------------------------------------------------- agenda: movimentazioni e preparazioni

def _attivita(m):
    """Nome dell'attività di un movimento, come lo dice chi lavora in piazzale."""
    da, a = m.da_sede, m.a_sede
    if m.causale in ("Consegna a cliente",) or (a and a.tipo == "cliente"):
        return "Consegna al cliente"
    if m.causale == "Vendita":
        return "Vendita"
    if da is not None and da.tipo not in ("propria", "cliente") and a is not None and a.tipo == "propria":
        return "Ritiro dal fornitore"
    if a is not None and a.esterna:
        return "Verso il fornitore"
    if da is not None and a is not None and da.tipo == "propria" and a.tipo == "propria":
        return "Spostamento"
    return "Trasporto"


def _chi(m):
    if m.assegnato_a is not None:
        return m.assegnato_a.nome
    if m.conducente:
        return m.conducente
    return "—"


def _mio_movimento(m, io):
    """Mio = lo ritiro io, l'ho portato o lo porto io, ho creato il DDT, ho confermato l'arrivo o è un'auto che vendo."""
    return io in (m.assegnato_a_id, m.partenza_utente_id, m.ddt.creato_da_id if m.ddt else None,
                  m.arrivo_utente_id, m.veicolo.venditore_id if m.veicolo else None)


def agenda(dal, al, sede_id=None, solo_mie=False):
    """Movimentazioni e preparazioni tra due date (incluse), in ordine di ora.
    stato: "fatto" · "corso" · "dafare"."""
    inizio, _ = _giorno(dal)
    _, fine = _giorno(al)
    io, ruolo = g.utente.id, g.utente.ruolo
    voci = []

    def tiene(m):
        return not sede_id or sede_id in (m.da_sede_id, m.a_sede_id)

    movimenti = (Movimento.query.join(Ddt, Movimento.ddt_id == Ddt.id).filter(Ddt.annullato_at.is_(None))
                 .filter(((Movimento.partenza_at >= inizio) & (Movimento.partenza_at < fine))
                         | ((Movimento.arrivo_at >= inizio) & (Movimento.arrivo_at < fine))
                         # ritiri: quelli del periodo; i ritiri in ritardo solo se il periodo comprende oggi
                         | ((Movimento.stato == "predisposto") & (Movimento.ritiro_entro_at >= inizio) & (Movimento.ritiro_entro_at < fine))
                         | ((Movimento.stato == "predisposto") & (Movimento.ritiro_entro_at < inizio) & (dal <= adesso().date()))
                         | ((Movimento.stato == "aperto") & (Movimento.partenza_at < inizio)
                            & (dal <= adesso().date())))
                 .all())
    # 22/11/2026: un DDT multiplo è una riga sola ("con altre N auto"); "Solo le mie" = solo le mie, per persona
    visti = set()
    for m in movimenti:
        if not tiene(m) or (solo_mie and not _mio_movimento(m, io)) or m.ddt_id in visti:
            continue
        visti.add(m.ddt_id)
        altre = max(0, len(m.ddt.movimenti) - 1) if m.ddt and m.ddt.multiplo else 0
        # 22/11/2026: l'orario mostrato è quello vero, con scritto cosa indica (partita, arrivata, entro…)
        if m.stato == "predisposto":
            quando, stato, att = m.ritiro_entro_at or m.ddt.emesso_at, "dafare", "Ritiro da fare"
            etichetta = "in ritardo · andava ritirata entro" if m.oltre_orario else "da ritirare entro"
        elif m.partenza_at and m.partenza_at >= inizio:
            quando, stato, att = m.partenza_at, "fatto" if m.stato == "chiuso" else "corso", _attivita(m)
            etichetta = f"partita · arrivata alle {m.arrivo_at:%H:%M}" if m.arrivo_at else "partita"
        elif m.stato == "aperto":  # partito prima e ancora in viaggio
            quando, stato, att, etichetta = m.partenza_at, "corso", "In viaggio", "partita"
        else:  # partito prima, arrivato nel periodo
            quando, stato, att, etichetta = m.arrivo_at, "fatto", "Arrivo", "arrivata"
        # viaggio oltre un'ora dopo l'arrivo atteso: si scrive sotto l'attività (come il vecchio "Da fare")
        nota = ""
        ritardo = servizi.viaggio_in_ritardo(m)
        if ritardo is not None and ritardo > 60:
            _, stimato = servizi.arrivo_atteso(m)
            nota = "arrivo in ritardo" + ("" if stimato else " · arrivo non stimabile")
        voci.append({"at": quando, "attivita": att, "ddt": m.ddt.sigla if m.ddt else "", "v": m.veicolo,
                     "da": m.da_sede.nome if m.da_sede else "", "a": m.sede_arrivo_effettiva.nome if m.a_sede else "",
                     "chi": _chi(m), "stato": stato, "tipo": "movimento", "nota": nota, "quando": etichetta, "altre": altre,
                     "url": url_for("main.ddt_dettaglio", ddt_id=m.ddt_id)})
    # consegne al cliente confermate (senza DDT verso il cliente: la consegna in sede)
    for v in Veicolo.query.filter(Veicolo.stato == "consegnato", Veicolo.consegnato_at >= inizio,
                                  Veicolo.consegnato_at < fine).all():
        if v.trasferita_a or (sede_id and v.sede_id != sede_id):
            continue
        if solo_mie and v.venditore_id != io:
            continue
        voci.append({"at": v.consegnato_at, "attivita": "Consegna al cliente", "ddt": "", "v": v,
                     "da": v.sede.nome if v.sede else "", "a": "", "chi": "—", "stato": "fatto", "tipo": "consegna", "quando": "consegnata",
                     "url": url_for("main.veicolo", veicolo_id=v.id)})
    # preparazioni: l'orario fissato per essere pronta
    for p in Preparazione.query.filter(Preparazione.pronta_entro_at >= inizio, Preparazione.pronta_entro_at < fine).all():
        v = p.veicolo
        if sede_id and v.sede_id != sede_id:
            continue
        if solo_mie and v.venditore_id != io and not any(f.utente_id == io for f in p.fasi):
            continue
        fatte = len(p.fasi)
        stato = "fatto" if p.completata_at else ("corso" if fatte else "dafare")
        passo = "pronta" if p.completata_at else p.prossima_fase.lower()
        ultimo = p.fasi[-1].utente.nome if p.fasi and p.fasi[-1].utente else "—"
        voci.append({"at": p.pronta_entro_at, "attivita": "Preparazione in sede", "ddt": "", "v": v,
                     "da": v.sede.nome if v.sede else "", "a": "", "chi": ultimo, "stato": stato, "tipo": "preparazione",
                     "quando": "pronta entro" + ("" if p.completata_at else f" · ora: {passo}"),
                     "url": url_for("main.veicolo", veicolo_id=v.id) + "#preparazione"})
    voci.sort(key=lambda x: x["at"])
    return voci


def giornata(sede_id=None):
    """Riquadri "La giornata in tre numeri"."""
    oggi = adesso().date()
    voci = agenda(oggi, oggi, sede_id)
    mov = [x for x in voci if x["tipo"] != "preparazione"]
    prep = [x for x in voci if x["tipo"] == "preparazione"]

    def conta(lista):
        c = Counter(x["stato"] for x in lista)
        return {"totale": len(lista), "fatto": c["fatto"], "corso": c["corso"], "dafare": c["dafare"]}

    # lavorazioni che finiscono nei prossimi 3 giorni (dal fornitore, lavoro non ancora finito)
    per_giorno, ritardo = OrderedDict(), 0
    giorni = [oggi + timedelta(days=k) for k in range(1, 4)]
    for d in giorni:
        per_giorno[d] = 0
    for _, elenco in servizi.presso_fornitori():
        for v, m in elenco:
            if not (m and m.con_lavorazione) or m.lavorazione_completata_at or not m.lavorazione_prevista_at:
                continue
            if sede_id and v.sede_id != sede_id:
                continue
            if m.giorni_ritardo:
                ritardo += 1
            elif m.lavorazione_prevista_at.date() in per_giorno:
                per_giorno[m.lavorazione_prevista_at.date()] += 1
    return {"mov": conta(mov), "prep": conta(prep),
            "lav": {"totale": sum(per_giorno.values()), "ritardo": ritardo,
                    "giorni": [(GIORNI[d.weekday()], n) for d, n in per_giorno.items()]}}


# ---------------------------------------------------------------- stock

def gruppo(v):
    """Per le barre: in sede (anche pronte), in preparazione, dai fornitori (in lavorazione e da ritirare), in viaggio."""
    f = v.fase
    if f == "in_viaggio":
        return "viaggio"
    if f in ("lavorazione", "da_ritirare"):
        return "fornitori"
    if f == "preparazione":
        return "preparazione"
    return "sede"


def stock_per_marca(veicoli, quante=6):
    per = {}
    for v in veicoli:
        marca = v.marca or "Marca non indicata"
        r = per.setdefault(marca, {"marca": marca, "totale": 0, "sede": 0, "preparazione": 0, "fornitori": 0, "viaggio": 0})
        r["totale"] += 1
        r[gruppo(v)] += 1
    righe = sorted(per.values(), key=lambda r: -r["totale"])
    return righe[:quante], len(righe)


def piazzali(veicoli):
    """Sedi e piazzali della concessionaria (ed esposizioni con auto): vetture presenti e posti."""
    presenti = Counter(v.sede_id for v in veicoli if v.stato == "in_sede" and v.sede_id)
    out = []
    for s in Sede.query.filter_by(attiva=True).order_by(Sede.nome).all():
        if s.tipo != "propria" and not (s.tipo == "esposizione" and presenti.get(s.id)):
            continue
        if s.sede_legale:  # la sede legale è un indirizzo, non un piazzale (scelta dell'utente 22/11/2026)
            continue
        n = presenti.get(s.id, 0)
        pct = round(100 * n / s.posti) if s.posti else None
        out.append({"sede": s, "n": n, "posti": s.posti, "pct": pct})
    return out


# ---------------------------------------------------------------- registro e fornitori

def _frase(a):
    v = a.veicolo.descrizione if a.veicolo else "auto"
    d = a.ddt
    m = d.movimento if d else None
    dove_a = m.sede_arrivo_effettiva.nome if m and m.a_sede else ""
    dove_da = m.da_sede.nome if m and m.da_sede else ""
    return {
        "partenza": f"ha emesso il DDT {d.sigla if d else ''}: {v}" + (f" verso {dove_a}" if dove_a else ""),
        "predisposizione": f"ha predisposto il DDT {d.sigla if d else ''}: {v}",
        "presa_in_carico": f"ha preso in carico il ritiro: {v}" + (f" da {dove_da}" if dove_da else ""),
        "arrivo": f"ha confermato l'arrivo: {v}" + (f" a {dove_a}" if dove_a else ""),
        "lavorazione_completata": f"ha confermato la fine dei lavori: {v}",
        "preparazione_fase": f"ha chiuso un passo di preparazione: {v}",
        "consegna": f"ha consegnato al cliente: {v}",
        "annullamento": f"ha cancellato il DDT {d.sigla if d else ''}: {v}",
        "danno": f"ha segnalato un danno: {v}",
        "danno_risolto": f"ha segnato risolto un danno: {v}",
    }.get(a.tipo, a.nome if hasattr(a, "nome") else a.tipo)


def registro_oggi(quante=8):
    dal, _ = _giorno(adesso().date())
    azioni = (Azione.query.filter(Azione.at >= dal, Azione.ripristinata_at.is_(None))
              .order_by(Azione.at.desc()).limit(quante).all())
    return [{"at": a.at, "chi": a.utente.nome if a.utente else "—", "frase": _frase(a),
             "url": url_for("main.veicolo", veicolo_id=a.veicolo_id)} for a in azioni]


def fornitori_30():
    """Fornitori esterni, ultimi 30 giorni: auto da loro ora, lavori finiti, finiti entro la fine prevista,
    ritardo medio (giorni lavorativi oltre la fine prevista), in ritardo ora."""
    dal = adesso() - timedelta(days=30)
    righe = {}

    def riga(sede):
        return righe.setdefault(sede.id, {"nome": sede.nome, "ora": 0, "finiti": 0, "entro": 0, "ritardi": [],
                                          "ritardo_ora": 0})
    for sede, elenco in servizi.presso_fornitori():
        r = riga(sede)
        for v, m in elenco:
            r["ora"] += 1
            if m and m.con_lavorazione and not m.lavorazione_completata_at and m.giorni_ritardo:
                r["ritardo_ora"] += 1
    finiti = Movimento.query.filter(Movimento.lavorazione_completata_at >= dal).all()
    for m in finiti:
        if not m.con_lavorazione or not m.lavorazione_prevista_at:
            continue
        r = riga(m.sede_arrivo_effettiva)
        r["finiti"] += 1
        if m.lavorazione_completata_at <= m.lavorazione_prevista_at:
            r["entro"] += 1
            r["ritardi"].append(0)
        else:
            r["ritardi"].append(max(1, servizi.giorni_lavorativi_tra(m.lavorazione_prevista_at, m.lavorazione_completata_at)))
    out = []
    for r in righe.values():
        r["pct"] = round(100 * r["entro"] / r["finiti"]) if r["finiti"] else None
        r["ritardo_medio"] = (sum(r["ritardi"]) / len(r["ritardi"])) if r["ritardi"] else None
        out.append(r)
    out.sort(key=lambda r: (-r["ora"], -r["finiti"], r["nome"]))
    return out


# ---------------------------------------------------------------- telefono del personale

def compiti_telefono():
    """"Da fare per te": ogni riga dice l'azione con la destinazione, l'auto e dove si trova adesso o quando."""
    io, ruolo = g.utente.id, g.utente.ruolo
    out = []
    base = Movimento.query.join(Ddt, Movimento.ddt_id == Ddt.id).filter(Ddt.annullato_at.is_(None))
    for m in base.filter(Movimento.stato == "predisposto", Movimento.assegnato_a_id == io).order_by(Movimento.ritiro_entro_at):
        out.append({"tipo": "ritiro", "v": m.veicolo, "azione": f"Porta a {m.a_sede.nome}",
                    "dove": (f"Ora è a {m.da_sede.nome}" if m.da_sede else "")
                    + (f" · ritiro entro {m.ritiro_entro_at:%d/%m %H:%M}" if m.ritiro_entro_at else ""),
                    "url": url_for("main.tocco_scelta", veicolo_id=m.veicolo_id)})
    for m in base.filter(Movimento.stato == "aperto", Movimento.partenza_utente_id == io).order_by(Movimento.partenza_at):
        out.append({"tipo": "viaggio", "v": m.veicolo, "azione": f"Arriva a {m.a_sede.nome}",
                    "dove": f"Partita alle {m.partenza_at:%H:%M} da {m.da_sede.nome if m.da_sede else '—'} · all'arrivo tocca l'adesivo",
                    "url": url_for("main.arrivo_da_tap", ddt_id=m.ddt_id)})
    if ruolo in ("preparatore", "consegne", "admin"):
        for p in (Preparazione.query.filter_by(completata_at=None)
                  .order_by(Preparazione.pronta_entro_at.is_(None), Preparazione.pronta_entro_at).limit(10)):
            v = p.veicolo
            out.append({"tipo": "preparazione", "v": v, "azione": p.prossima_fase if p.prossima_fase != "Pronta" else "Segna come pronta",
                        "dove": (f"In {v.sede.nome}" if v.sede else "")
                        + (f" · pronta entro {p.pronta_entro_at:%d/%m alle %H:%M}" if p.pronta_entro_at else ""),
                        "url": url_for("main.tocco_scelta", veicolo_id=v.id)})
    if ruolo in ("consegne", "admin"):
        for v in Veicolo.query.filter(Veicolo.stato == "in_sede").all():
            if v.fase == "pronta" and v.consegna_sede and v.sede_id != v.consegna_sede_id and not v.verso_consegna:
                out.append({"tipo": "consegna", "v": v, "azione": f"Porta a {v.consegna_sede.nome}",
                            "dove": f"Ora è in {v.sede.nome if v.sede else '—'} · ultimo DDT prima della consegna",
                            "url": url_for("main.tocco_scelta", veicolo_id=v.id)})
    return out[:15]


# ---------------------------------------------------------------- pagine dedicate della barra laterale (22/11/2026)

def in_viaggio():
    """Auto su strada con DDT aperto: tratta, chi la porta, partenza, arrivo previsto o ritardo."""
    out = []
    base = Movimento.query.join(Ddt, Movimento.ddt_id == Ddt.id).filter(Ddt.annullato_at.is_(None), Movimento.stato == "aperto")
    for m in base.order_by(Movimento.partenza_at).all():
        atteso, stimato = servizi.arrivo_atteso(m)
        ritardo = servizi.viaggio_in_ritardo(m) or 0
        out.append({"m": m, "v": m.veicolo, "atteso": atteso, "stimato": stimato, "ritardo_min": ritardo if ritardo > 60 else 0})
    out.sort(key=lambda r: (-r["ritardo_min"], r["m"].partenza_at))
    return out


def da_ritirare():
    """Lavoro finito dal fornitore e DDT da ritirare: dove sono, da quando, chi le riporta, entro quando."""
    out, visti = [], set()
    base = Movimento.query.join(Ddt, Movimento.ddt_id == Ddt.id).filter(Ddt.annullato_at.is_(None))
    for m in base.filter(Movimento.stato == "predisposto").order_by(Movimento.ritiro_entro_at).all():
        visti.add(m.veicolo_id)
        out.append({"v": m.veicolo, "dove": m.da_sede.nome if m.da_sede else "—", "verso": m.a_sede.nome if m.a_sede else "—",
                    "ddt": m.ddt, "entro": m.ritiro_entro_at, "scaduto": m.oltre_orario, "chi": m.assegnato_a.nome if m.assegnato_a else "",
                    "finita": None, "url": url_for("main.ddt_dettaglio", ddt_id=m.ddt_id)})
    for sede, elenco in servizi.presso_fornitori():
        for v, m in elenco:
            if v.id in visti or not (m and m.lavorazione_completata_at):
                continue
            out.append({"v": v, "dove": sede.nome, "verso": "", "ddt": None, "entro": None, "scaduto": False, "chi": "",
                        "finita": m.lavorazione_completata_at, "lavorazione": m.lavorazione,
                        "url": url_for("main.veicolo", veicolo_id=v.id) + "#predisponi"})
    out.sort(key=lambda r: (not r["scaduto"], r["ddt"] is not None, r["entro"] or r["finita"] or adesso()))
    return out


def consegnate(mese=None):
    """Consegne al cliente confermate: oggi, ultimi 7 giorni, mese; elenco del mese scelto."""
    oggi = adesso().date()
    tutte = [v for v in Veicolo.query.filter(Veicolo.stato == "consegnato", Veicolo.consegnato_at.isnot(None))
             .order_by(Veicolo.consegnato_at.desc()).all() if not v.trasferita_a]
    dal_oggi, _ = _giorno(oggi)
    settimana = datetime.combine(oggi - timedelta(days=6), time.min)
    inizio_mese = datetime(oggi.year, oggi.month, 1)
    return tutte, {"oggi": sum(1 for v in tutte if v.consegnato_at >= dal_oggi),
                   "settimana": sum(1 for v in tutte if v.consegnato_at >= settimana),
                   "mese": sum(1 for v in tutte if v.consegnato_at >= inizio_mese)}
