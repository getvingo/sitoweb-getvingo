import os
import re
import time
import uuid
from datetime import date, datetime, timedelta
from functools import wraps
from itertools import groupby

from flask import (Blueprint, Response, abort, current_app, flash, g, jsonify, redirect,
                   render_template, request, send_from_directory, session, url_for)
from sqlalchemy import or_

from . import copie
from . import ddt as ddt_pdf_mod
from . import ia
from . import marchi
from . import servizi
from . import vin as vin_mod
from .auth import login_richiesto
from .models import (Azione, Danno, DannoFoto, Ddt, Movimento, Preparazione, Sede, Sollecito, Tag, Utente,
                     Veicolo, Vettore, adesso, db)
from .servizi import Vietato

bp = Blueprint("main", __name__)

ORE_RITIRO = [4, 8, 24, 48, 72]
GIORNI_SETTIMANA = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]


def solo_interni(view):
    """Pagine riservate al personale della concessionaria (non ai fornitori)."""
    @wraps(view)
    @login_richiesto
    def wrapper(*args, **kwargs):
        if not g.utente.interno:
            abort(403)
        return view(*args, **kwargs)
    return wrapper


# la posizione rilevata al tocco NFC vale per i passaggi che lo seguono (abbinamento, partenza, arrivo…)
VALIDITA_TOCCO_S = 15 * 60


def _posizione_tocco():
    """Posizione dell'ultimo tocco NFC di questo utente, se recente; altrimenti (None, None)."""
    t = session.get("posizione_tocco") or {}
    if t and time.time() - t.get("at", 0) <= VALIDITA_TOCCO_S:
        return t["lat"], t["lon"]
    return None, None


def _coord(usa_tocco=True):
    """Lat/lon inviati dal telefono; se il modulo non li ha (il GPS si chiede solo al tocco NFC)
    vale la posizione dell'ultimo tocco recente."""
    try:
        return float(request.form["lat"]), float(request.form["lon"])
    except (KeyError, ValueError):
        return _posizione_tocco() if usa_tocco else (None, None)


def _movimenti(stato):
    """Movimenti in un certo stato, esclusi quelli con DDT cancellato."""
    return (Movimento.query.join(Ddt, Movimento.ddt_id == Ddt.id).filter(Movimento.stato == stato, Ddt.annullato_at.is_(None)))


def _dati_movimento(conducente_obbligatorio=True, manca_conducente="Indica chi sposta il mezzo."):
    """Legge e valida i campi del modulo di partenza/modifica DDT.
    Restituisce (dati, errore)."""
    dati = {
        "a_sede_id": request.form.get("a_sede_id", type=int),
        "causale": request.form.get("causale", ""),
        "vettore_id": request.form.get("vettore_id", type=int),
        "conducente": request.form.get("conducente", "").strip()[:120],
        "lavorazione": request.form.get("lavorazione", ""),
        "lavorazione_note": request.form.get("lavorazione_note", "").strip()[:300],
        "mezzo": request.form.get("mezzo", ""),
    }
    if not dati["a_sede_id"] or not db.session.get(Sede, dati["a_sede_id"]):
        return None, "Scegli la destinazione."
    if dati["causale"] not in Movimento.CAUSALI + Movimento.CAUSALI_STORICHE:
        return None, "Scegli la causale."
    if not dati["vettore_id"] or not db.session.get(Vettore, dati["vettore_id"]):
        return None, "Scegli il vettore."
    if conducente_obbligatorio and not dati["conducente"]:
        return None, manca_conducente
    if dati["lavorazione"] not in Movimento.LAVORAZIONI:
        return None, "Scegli la lavorazione da fare."
    if dati["lavorazione"] != Movimento.NESSUNA and not dati["lavorazione_note"]:
        return None, "Scrivi i dettagli della lavorazione da fare."
    if dati["mezzo"] not in Movimento.MEZZI:
        return None, "Indica come viaggia l'auto (su strada, bisarca, carro attrezzi…)."
    return dati, None


def _contesto_modulo(escludi_sede_id=None):
    """Opzioni per il modulo movimento, con destinazioni raggruppate per tipo."""
    sedi = (Sede.query.filter(Sede.attiva.is_(True), Sede.id != (escludi_sede_id or 0))
            .order_by(Sede.tipo != "propria", Sede.tipo, Sede.nome).all())
    if g.utente.interno:
        nomi = [u.nome for u in Utente.query.filter_by(attivo=True, supporto=False).order_by(Utente.nome) if u.interno]
    else:
        nomi = [u.nome for u in Utente.query.filter_by(attivo=True, sede_id=g.utente.sede_id)]
    return {
        "gruppi_sedi": [(Sede.TIPI[t], list(s)) for t, s in groupby(sedi, key=lambda s: s.tipo)],
        "vettori": Vettore.query.filter_by(attivo=True).order_by(Vettore.nome).all(),
        "causali": Movimento.CAUSALI,
        "lavorazioni": Movimento.LAVORAZIONI,
        "mezzi": Movimento.MEZZI,
        "nomi_utenti": nomi,
    }


def _ddt_visibile(ddt_id):
    documento = db.get_or_404(Ddt, ddt_id)
    if not servizi.puo_vedere(documento, g.utente):
        abort(403)
    return documento


def _veicolo_visibile(veicolo_id):
    v = db.get_or_404(Veicolo, veicolo_id)
    if not servizi.puo_vedere_veicolo(v, g.utente):
        abort(403)
    return v


def _torna_al_ddt(documento):
    return redirect(url_for("main.ddt_dettaglio", ddt_id=documento.id))


def _da_consegnare():
    """Auto pronte, ferme o in viaggio con l'ultimo DDT: restano visibili fino alla consegna."""
    attive = Veicolo.query.filter(Veicolo.stato != "consegnato").order_by(Veicolo.id).all()
    return [v for v in attive if v.da_consegnare]


def _scelta_slot(occupati, giorni_lavorativi=15):
    """Giorni e slot di 30 minuti per scegliere un orario. occupati = {inizio: chi lo occupa}, oppure
    {inizio: {sede_id: chi}} quando l'occupazione dipende dalla sede scelta (consegne)."""
    return [{"data": giorno, "breve": GIORNI_SETTIMANA[giorno.weekday()][:3], "mese": MESI[giorno.month - 1][:3],
             "lungo": f"{GIORNI_SETTIMANA[giorno.weekday()]} {giorno.day} {MESI[giorno.month - 1]}",
             "slot": [{"at": s, "occupato": occupati.get(s)} for s in slot]}
            for giorno, slot in servizi.giorni_con_slot(giorni_lavorativi=giorni_lavorativi)]


def _per_fase(*fasi):
    """Veicoli (non consegnati) nelle fasi indicate, calcolate sul momento."""
    attivi = Veicolo.query.filter(Veicolo.stato != "consegnato").order_by(Veicolo.id).all()
    return [v for v in attivi if v.fase in fasi]


def _opzioni(valori, *, attuale=""):
    """Opzioni di un filtro: solo i valori davvero presenti (ordinati, senza vuoti).
    Il valore già scelto resta sempre selezionabile."""
    insieme = sorted({v for v in valori if v}, key=lambda x: str(x).lower())
    if attuale and attuale not in insieme:
        insieme.append(attuale)
    return insieme


def _filtra_veicoli(veicoli, f):
    """Applica i filtri comuni (tipologia, marca, modello, colore, stato, sede) a una lista."""
    out = []
    for v in veicoli:
        if f.get("tipologia") and v.tipologia != f["tipologia"]:
            continue
        if f.get("marca") and (v.marca or "").lower() != f["marca"].lower():
            continue
        if f.get("modello") and (v.modello or "").lower() != f["modello"].lower():
            continue
        if f.get("colore") and (v.colore or "").lower() != f["colore"].lower():
            continue
        if f.get("fase") and v.fase != f["fase"]:
            continue
        if f.get("sede") and str(v.sede_id) != f["sede"]:
            continue
        out.append(v)
    return out


def _filtri_veicoli(veicoli, f):
    """Opzioni presenti per ogni filtro, calcolate sui veicoli già filtrati dagli altri criteri."""
    def senza(chiave):
        return _filtra_veicoli(veicoli, {k: v for k, v in f.items() if k != chiave})
    return {
        "tipologia": [(k, Veicolo.TIPOLOGIE[k]) for k in _opzioni((v.tipologia for v in senza("tipologia")),
                                                                    attuale=f.get("tipologia"))],
        "marca": _opzioni((v.marca for v in senza("marca")), attuale=f.get("marca")),
        "modello": _opzioni((v.modello for v in senza("modello")), attuale=f.get("modello")),
        "colore": _opzioni((v.colore for v in senza("colore")), attuale=f.get("colore")),
        "fase": [(k, n) for k, n in Veicolo.FASI.items()
                 if k in {v.fase for v in senza("fase")} or k == f.get("fase")],
        "sede": sorted({(str(v.sede.id), v.sede.nome) for v in senza("sede") if v.sede}, key=lambda x: x[1]),
    }


CHIAVI_FILTRO = ("tipologia", "marca", "modello", "colore", "fase", "sede")


# ---------------------------------------------------------------- panoramica

@bp.route("/")
@login_richiesto
def home():
    if not g.utente.interno:
        return _home_fornitore()
    attivi = Veicolo.query.filter(Veicolo.stato != "consegnato").all()
    danni = Danno.query.filter(Danno.risolto_at.is_(None), Danno.rimosso.is_(False)) \
        .order_by(Danno.segnalato_at.desc()).all()

    # riquadri: ogni auto in un solo gruppo, contata come auto (non come DDT)
    gruppi = {k: [] for k in ("in_viaggio", "da_ritirare", "lavorazione", "preparazione", "pronta", "stock")}
    for v in attivi:
        fase = v.fase
        if fase == "in_viaggio":
            gruppi["in_viaggio"].append(v)
        elif fase == "da_ritirare" or v.movimento_predisposto is not None:
            gruppi["da_ritirare"].append(v)
        elif fase in ("lavorazione", "preparazione", "pronta"):
            gruppi[fase].append(v)
        elif v.sede is not None and v.sede.tipo == "propria":
            gruppi["stock"].append(v)
    conteggi = {k: len(lista) for k, lista in gruppi.items()}
    conteggi["ddt_in_viaggio"] = len({v.movimento_aperto.ddt_id for v in gruppi["in_viaggio"] if v.movimento_aperto})

    # da fare, in due gruppi: lavorazioni e ritiri / preparazione. Livello = colore (1 urgente, 2 presto,
    # 3 normale); l'etichetta dice cosa sta succedendo. Una voce per auto (la più grave).
    voci = {}

    def aggiungi(gruppo, livello, etichetta, v, testo, url):
        if v.id not in voci or livello < voci[v.id]["livello"]:
            voci[v.id] = {"gruppo": gruppo, "livello": livello, "etichetta": etichetta, "v": v, "testo": testo, "url": url}

    tra_un_giorno = adesso() + timedelta(hours=24)
    for m in _movimenti("predisposto").order_by(Movimento.ritiro_entro_at):
        url = url_for("main.ddt_dettaglio", ddt_id=m.ddt.id)
        if m.scaduto:
            aggiungi("lavorazioni", 1, "Urgente", m.veicolo,
                     f"DDT {m.ddt.sigla}: ritiro scaduto il {m.ritiro_entro_at:%d/%m alle %H:%M}, va rinnovato", url)
        elif m.ritiro_entro_at and m.ritiro_entro_at <= tra_un_giorno:
            aggiungi("lavorazioni", 2, "Entro oggi", m.veicolo,
                     f"DDT {m.ddt.sigla}: da ritirare entro le {m.ritiro_entro_at:%H:%M del %d/%m}", url)
        else:
            aggiungi("lavorazioni", 3, "Da ritirare", m.veicolo,
                     f"DDT {m.ddt.sigla}: da ritirare presso {m.da_sede.nome if m.da_sede else '—'}", url)
    for v in attivi:
        if v.fase == "da_ritirare" and v.movimento_predisposto is None:
            aggiungi("lavorazioni", 3, "Attesa DDT", v, f"Lavorazione finita presso {v.sede.nome}: manca il DDT di rientro",
                     url_for("main.veicolo", veicolo_id=v.id))
        elif v.fase == "lavorazione":
            giorni = v.ultimo_arrivo.giorni_lavorazione or 0
            if giorni >= 7:
                aggiungi("lavorazioni", 1 if giorni >= 14 else 2, "Urgente" if giorni >= 14 else "Da sollecitare", v,
                         f"Ferma presso {v.sede.nome} da {giorni} giorni",
                         url_for("main.ddt_dettaglio", ddt_id=v.ultimo_arrivo.ddt.id))
    for prep in Preparazione.query.filter_by(completata_at=None).order_by(Preparazione.creata_at):
        url = url_for("main.veicolo", veicolo_id=prep.veicolo_id)
        if servizi.bloccata_da_danni(prep):
            aggiungi("preparazione", 2, "Bloccata", prep.veicolo,
                     f"Ferma prima di “{prep.prossima_fase}”: c'è un danno da risolvere", url)
        else:
            fatte = len(prep.fasi)
            aggiungi("preparazione", 3, "Da completare", prep.veicolo,
                     f"Passo {fatte + 1} di {len(Preparazione.FASI)}: {prep.prossima_fase}", url)
    for prep in Preparazione.query.filter(Preparazione.completata_at.is_(None), Preparazione.pronta_entro_at.isnot(None)):
        url = url_for("main.veicolo", veicolo_id=prep.veicolo_id)
        if prep.pronta_entro_at < adesso():
            aggiungi("preparazione", 1, "In ritardo", prep.veicolo,
                     f"Doveva essere pronta il {prep.pronta_entro_at:%d/%m alle %H:%M} · ora: {prep.prossima_fase}", url)
        elif prep.pronta_entro_at.date() == adesso().date():
            aggiungi("preparazione", 2, "Entro oggi", prep.veicolo,
                     f"Pronta entro le {prep.pronta_entro_at:%H:%M} · ora: {prep.prossima_fase}", url)
    ordinate = sorted(voci.values(), key=lambda x: x["livello"])
    oggi_d = adesso().date()
    oggi = [e for e in _eventi_preparazione() if e["at"].date() == oggi_d or e["ritardo"]]
    da_fare = [("lavorazioni", "Lavorazioni e ritiri", "chiave", [x for x in ordinate if x["gruppo"] == "lavorazioni"]),
               ("preparazione", "Preparazione", "brilla", [x for x in ordinate if x["gruppo"] == "preparazione"])]
    return render_template("home.html", da_fare=da_fare, totale_da_fare=len(ordinate), danni=danni,
                           conteggi=conteggi, oggi=oggi, oggi_data=oggi_d, tipi_evento=TIPI_EVENTO,
                           ia_attiva=ia.disponibile(), chat_panoramica=CHAT_PANORAMICA and ia.disponibile())


@bp.route("/analisi")
@solo_interni
def analisi():
    """Tempi dalla vendita alla consegna, fase per fase; fornitori; tratte (stimato e reale)."""
    from . import analisi as analisi_mod
    dati = analisi_mod.calcola(request.args.get("periodo", "90"))
    return render_template("analisi.html", a=dati, periodi=analisi_mod.PERIODI, durata=analisi_mod.durata)


# 11/11/2026: per il momento la barra della Panoramica è solo "Trova" (auto, DDT, adesivi). La chat con
# l'assistente è pronta: basta rimettere True (serve ANTHROPIC_API_KEY)
CHAT_PANORAMICA = False


@bp.route("/api/trova")
@solo_interni
def api_trova():
    """Risultati mentre si scrive nella barra della Panoramica: auto (VIN anche solo le ultime cifre, targa,
    marca, modello, colore), DDT (numero, anche "12/2026"), adesivi NFC (codice). Al massimo 8 per gruppo."""
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return {"auto": [], "ddt": [], "adesivi": []}
    like = f"%{q}%"
    vin_q = vin_mod.normalizza(q)
    auto = Veicolo.query.filter(or_(
        Veicolo.vin.ilike(f"%{vin_q}%"), Veicolo.targa.ilike(f"%{q.replace(' ', '')}%"),
        Veicolo.modello.ilike(like), Veicolo.marca.ilike(like), Veicolo.colore.ilike(like),
        (Veicolo.marca + " " + Veicolo.modello).ilike(like)))
    # prima quelle ancora in stock, poi le consegnate
    auto = sorted(auto.limit(40).all(), key=lambda v: (v.stato == "consegnato", v.marca or "", v.modello or ""))[:8]
    ddt = []
    m = re.fullmatch(r"(?:ddt\s*)?(\d{1,6})(?:\s*/\s*(\d{4}))?", q, re.I)
    if m:
        cerca = Ddt.query.filter_by(numero=int(m.group(1)))
        if m.group(2):
            cerca = cerca.filter_by(anno=int(m.group(2)))
        ddt = cerca.order_by(Ddt.anno.desc()).limit(8).all()
    adesivi = Tag.query.filter(Tag.codice.ilike(like)).order_by(Tag.codice).limit(8).all()

    def voce_auto(v):
        _, stato = v.stato_per(g.utente)
        dove = v.sede.nome if v.sede and v.stato != "in_viaggio" else ""
        return {"url": url_for("main.veicolo", veicolo_id=v.id), "titolo": v.descrizione, "vin": v.vin,
                "targa": v.targa or "", "sotto": " · ".join(x for x in (stato, dove) if x), "fase": v.fase}

    def voce_ddt(d):
        mv = d.movimento
        auto_ddt = f"{len(d.movimenti)} auto" if d.multiplo else mv.veicolo.descrizione
        return {"url": url_for("main.ddt_dettaglio", ddt_id=d.id), "titolo": f"DDT {d.sigla}",
                "sotto": f"{auto_ddt} · {mv.da_sede.nome if mv.da_sede else '—'} → {mv.sede_arrivo_effettiva.nome}"}

    def voce_adesivo(t):
        if t.veicolo:
            return {"url": url_for("main.veicolo", veicolo_id=t.veicolo.id), "titolo": t.codice,
                    "sotto": f"Abbinato a {t.veicolo.descrizione}"}
        return {"url": url_for("admin.tag") if g.utente.ruolo == "admin" else "", "titolo": t.codice,
                "sotto": "Adesivo libero"}
    return {"auto": [voce_auto(v) for v in auto], "ddt": [voce_ddt(d) for d in ddt],
            "adesivi": [voce_adesivo(t) for t in adesivi]}


def _home_fornitore():
    sede = g.utente.sede
    if sede is None:
        abort(403, "Il tuo account non è collegato a nessuna sede: contatta la concessionaria.")
    in_arrivo = _movimenti("aperto").filter(Movimento.a_sede_id == sede.id).all()
    da_consegnare = _movimenti("predisposto").filter(Movimento.da_sede_id == sede.id).all()
    presenti = servizi.presso_fornitori(solo_sede_id=sede.id)
    presenti = presenti[0][1] if presenti else []
    solleciti = [m.sollecito_aperto for _, m in presenti if m and m.sollecito_aperto]
    return render_template("home_fornitore.html", sede=sede, in_arrivo=in_arrivo, solleciti=solleciti,
                           da_consegnare=da_consegnare, presenti=presenti)


@bp.route("/lavorazioni")
@solo_interni
def lavorazioni():
    """Auto dai fornitori per una lavorazione vera (il solo trasferimento non compare).
    Filtri: tipo di lavorazione (un pulsante per tipo) e vettura / veicolo commerciale."""
    tipo = request.args.get("lavorazione", "")
    tipologia = request.args.get("tipologia", "") if request.args.get("tipologia") in Veicolo.TIPOLOGIE else ""
    stato = request.args.get("stato", "") if request.args.get("stato") in ("corso", "ritardo", "finita") else ""
    tutte = [(sede, [(v, m) for v, m in voci if m and m.con_lavorazione]) for sede, voci in servizi.presso_fornitori()]
    voci_tutte = [(v, m) for _, voci in tutte for v, m in voci]
    # pulsanti: i tipi presenti con il numero di auto (contato con il filtro vettura/commerciale già applicato)
    conta = {}
    for v, m in voci_tutte:
        if not tipologia or v.tipologia == tipologia:
            conta[m.lavorazione] = conta.get(m.lavorazione, 0) + 1
    tipi = [(l, conta[l]) for l in Movimento.LAVORAZIONI if l in conta]
    fornitori = []
    for sede, voci in tutte:
        voci = [(v, m) for v, m in voci if (not tipo or m.lavorazione == tipo) and (not tipologia or v.tipologia == tipologia)]
        if voci:
            fornitori.append((sede, voci))
    visibili = [m for _, voci in fornitori for _, m in voci]
    # i 4 numeri si contano prima del filtro per stato (toccandoli si filtra)
    riepilogo = {k: sum(1 for m in visibili if stato_lavorazione(m) == k) for k in ("corso", "ritardo", "finita")}
    riepilogo["totale"] = len(visibili)
    if stato:
        fornitori = [(sede, [(v, m) for v, m in voci if stato_lavorazione(m) == stato]) for sede, voci in fornitori]
        fornitori = [(sede, voci) for sede, voci in fornitori if voci]
    modo = "calendario" if request.args.get("modo") == "calendario" else "elenco"
    calendario = None
    if modo == "calendario":  # il giorno in cui ogni lavorazione deve finire (o è finita)
        calendario = _calendario([_evento_lavorazione(v, m) for _, voci in fornitori for v, m in voci],
                                 request.args.get("mese"), verso_il_futuro=True)
    return render_template("lavorazioni.html", fornitori=fornitori, tipo=tipo, tipi=tipi, riepilogo=riepilogo,
                           totale=sum(conta.values()), tipologia=tipologia, stato=stato, modo=modo,
                           totale_visibili=sum(len(voci) for _, voci in fornitori),
                           calendario=calendario, tipi_evento=TIPI_EVENTO)


def _evento_lavorazione(v, m):
    """Evento del calendario delle lavorazioni esterne: fine prevista (o fine vera, se già finita)."""
    nota = f"{m.lavorazione} · {m.sede_arrivo_effettiva.nome}"
    if m.lavorazione_completata_at:
        return {"at": m.lavorazione_completata_at, "tipo": "lavoro_finita", "v": v, "nota": nota}
    return {"at": m.lavorazione_prevista_at, "tipo": "fine_prevista", "v": v, "nota": nota,
            "ritardo": bool(m.giorni_ritardo), "giornata": True}


def stato_lavorazione(m):
    """finita (da ritirare) · ritardo (oltre la stima) · corso (in tempo)."""
    return "finita" if m.lavorazione_completata_at else ("ritardo" if m.giorni_ritardo else "corso")


def _torna(predefinito):
    torna = request.form.get("torna", "")
    return redirect(torna if torna.startswith("/") and not torna.startswith("//") else predefinito)


@bp.route("/movimento/<int:mov_id>/stima", methods=["POST"])
@login_richiesto
def stima_lavorazione(mov_id):
    """Giorni di lavorazione stimati: da qui si calcolano avanzamento e ritardo."""
    mov = db.get_or_404(Movimento, mov_id)
    try:
        giorni = request.form.get("giorni", type=int)
        if request.form.get("fine"):  # fine prevista scelta sul calendario: diventa giorni dall'arrivo
            giorni = servizi.giorni_fino_a(mov, request.form.get("fine"))
        servizi.imposta_stima(mov, g.utente, giorni)
        db.session.commit()
        flash(f"{mov.veicolo.descrizione}: lavorazione stimata in {mov.stima_giorni} giorni, "
              f"pronta il {mov.lavorazione_prevista_at:%d/%m}.", "ok")
    except Vietato as e:
        db.session.rollback()
        flash(str(e), "errore")
    return _torna(url_for("main.lavorazioni"))


@bp.route("/movimento/<int:mov_id>/sollecito", methods=["POST"])
@solo_interni
def sollecito(mov_id):
    """Tempo stimato finito e nessuna fine lavorazione: si chiede al fornitore il motivo del ritardo."""
    mov = db.get_or_404(Movimento, mov_id)
    try:
        servizi.sollecita(mov, g.utente, request.form.get("messaggio", ""))
        db.session.commit()
        flash(f"Sollecito inviato a {mov.sede_arrivo_effettiva.nome}: lo vede appena entra in Getvingo.", "ok")
    except Vietato as e:
        db.session.rollback()
        flash(str(e), "errore")
    return _torna(url_for("main.lavorazioni"))


@bp.route("/sollecito/<int:sollecito_id>/risposta", methods=["POST"])
@login_richiesto
def risposta_sollecito(sollecito_id):
    s = db.get_or_404(Sollecito, sollecito_id)
    try:
        servizi.rispondi_sollecito(s, g.utente, request.form.get("risposta", ""),
                                   request.form.get("nuova_stima", type=int))
        db.session.commit()
        flash("Risposta inviata alla concessionaria.", "ok")
    except Vietato as e:
        db.session.rollback()
        flash(str(e), "errore")
    return _torna(url_for("main.home"))


@bp.route("/preparazione")
@solo_interni
def preparazione():
    vista = request.args.get("vista", "corso")
    tipologia = request.args.get("tipologia", "") if request.args.get("tipologia") in Veicolo.TIPOLOGIE else ""
    del_tipo = lambda v: not tipologia or v.tipologia == tipologia  # filtro vetture / commerciali
    preparazioni = [p for p in Preparazione.query.filter_by(completata_at=None).order_by(Preparazione.creata_at)
                    if del_tipo(p.veicolo)]
    in_ritardo = [p for p in preparazioni if p.pronta_entro_at and p.pronta_entro_at < adesso()]
    in_tempo = [p for p in preparazioni if p not in in_ritardo]
    # i numeri in cima filtrano: in tempo / in ritardo (toccandoli di nuovo si torna a tutte)
    stato_prep = request.args.get("stato") if request.args.get("stato") in ("ritardo", "tempo") else None
    pronte = [v for v in _da_consegnare() if del_tipo(v)]
    tutte_consegnate = [v for v in Veicolo.query.filter_by(stato="consegnato").order_by(Veicolo.consegnato_at.desc())
                        if del_tipo(v)]
    fc = {k: request.args.get(k, "").strip() for k in ("q", "sede", "dal", "al")}
    consegnate = _filtra_consegnate(tutte_consegnate, fc)
    conteggi = {"corso": len(preparazioni), "ritardo": len(in_ritardo), "tempo": len(in_tempo), "pronte": len(pronte),
                "consegnate": len(tutte_consegnate)}
    if vista not in ("corso", "pronte", "consegnate"):
        vista = "corso"
    if vista != "corso":
        stato_prep = None
    solo_ritardo = stato_prep == "ritardo"
    da_preparare = [v for v in Veicolo.query.filter_by(stato="in_sede").order_by(Veicolo.marca, Veicolo.modello)
                    if v.fase == "in_sede" and v.sede and v.sede.tipo == "propria" and v.movimento_predisposto is None]
    modo = "calendario" if request.args.get("modo") == "calendario" else "elenco"
    if modo != "calendario":
        calendario = None
    elif vista == "consegnate":
        calendario = _calendario([{"at": v.consegnato_at, "tipo": "consegnata", "v": v, "nota": v.sede.nome if v.sede else ""}
                                  for v in consegnate if v.consegnato_at], request.args.get("mese"))
    else:
        calendario = _calendario([e for e in _eventi_preparazione() if del_tipo(e["v"])], request.args.get("mese"),
                                 verso_il_futuro=True)
    return render_template("preparazione.html", vista=vista, conteggi=conteggi,
                           preparazioni={"ritardo": in_ritardo, "tempo": in_tempo}.get(stato_prep, preparazioni),
                           solo_ritardo=solo_ritardo, stato_prep=stato_prep,
                           slot_prep=_scelta_slot({s: p.veicolo.descrizione for s, p in
                                                   servizi.slot_occupati_preparazione().items()}, 20),
                           da_preparare=da_preparare, pronte=pronte, consegnate=consegnate[:500], fc=fc, modo=modo,
                           calendario=calendario, tipi_evento=TIPI_EVENTO, tipologia=tipologia,
                           sedi_consegnate=sorted({(str(v.sede_id), v.sede.nome) for v in tutte_consegnate if v.sede},
                                                  key=lambda x: x[1]),
                           fasi=Preparazione.FASI)


def _filtra_consegnate(veicoli, fc):
    q = fc["q"].lower()
    out = []
    for v in veicoli:
        if q and not (q in f"{v.marca} {v.modello} {v.colore} {v.targa or ''}".lower()
                      or vin_mod.normalizza(q).lower() in v.vin.lower()):
            continue
        if fc["sede"].isdigit() and v.sede_id != int(fc["sede"]):
            continue
        giorno = v.consegnato_at.date() if v.consegnato_at else None
        if fc["dal"] and (giorno is None or giorno < _giorno(fc["dal"], giorno)):
            continue
        if fc["al"] and (giorno is None or giorno > _giorno(fc["al"], giorno)):
            continue
        out.append(v)
    return out


def _giorno(testo, altrimenti):
    try:
        return datetime.strptime(testo, "%Y-%m-%d").date()
    except ValueError:
        return altrimenti


MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre",
        "ottobre", "novembre", "dicembre"]


# tipi di evento del calendario: etichetta e classe css
TIPI_EVENTO = {"consegnata": "Consegnata", "pronta_entro": "Pronta entro", "consegna_prevista": "Consegna prevista",
               "fine_prevista": "Lavorazione da finire", "lavoro_finita": "Lavorazione finita"}


def _eventi_preparazione():
    """Calendario della preparazione: solo gli orari entro cui le auto devono essere pronte.
    Gli appuntamenti di consegna al cliente non ci vanno (stanno nella sezione Consegna dell'auto)."""
    eventi = [{"at": p.pronta_entro_at, "tipo": "pronta_entro", "v": p.veicolo, "nota": p.prossima_fase,
               "ritardo": p.pronta_entro_at < adesso()}
              for p in Preparazione.query.filter(Preparazione.completata_at.is_(None),
                                                 Preparazione.pronta_entro_at.isnot(None))]
    return sorted(eventi, key=lambda e: e["at"])


def _calendario(eventi, mese, verso_il_futuro=False):
    """Mese da mostrare (YYYY-MM) in settimane da lunedì a domenica, ogni giorno con i suoi eventi
    in ordine di orario. Senza mese: il corrente se ci sono eventi futuri, altrimenti quello dell'ultimo."""
    import calendar
    try:
        anno, m = (int(x) for x in mese.split("-"))
        date(anno, m, 1)
    except (AttributeError, ValueError):
        rif = adesso() if verso_il_futuro or not eventi else max(e["at"] for e in eventi)
        anno, m = rif.year, rif.month
    per_giorno = {}
    for e in eventi:
        if (e["at"].year, e["at"].month) == (anno, m):
            per_giorno.setdefault(e["at"].day, []).append(e)
    for lista in per_giorno.values():
        lista.sort(key=lambda e: e["at"])
    oggi = adesso().date()
    settimane = [[{"giorno": d, "eventi": per_giorno.get(d, []) if d else [],
                   "oggi": d and date(anno, m, d) == oggi} for d in sett]
                 for sett in calendar.Calendar(firstweekday=0).monthdayscalendar(anno, m)]
    prima = date(anno, m, 1) - timedelta(days=1)
    dopo = date(anno, m, 28) + timedelta(days=7)
    return {"titolo": f"{MESI[m - 1].capitalize()} {anno}", "settimane": settimane,
            "prec": f"{prima.year}-{prima.month:02d}", "succ": f"{dopo.year}-{dopo.month:02d}",
            "totale": sum(len(x) for x in per_giorno.values())}


@bp.route("/piazzale")
def piazzale_vecchio():
    return redirect(url_for("main.piazzale", **request.args))


ZONE_STOCK = [("preparazione", "Zona preparazione", "brilla"), ("pronte", "Pronte per la consegna", "bandiera"),
              ("esterne", "Lavorazioni esterne", "chiave"), ("viaggio", "In viaggio", "camion")]


def _zona_stock(v):
    """In quale zona dello Stock compare l'auto: la sua sede propria, preparazione, fornitori, viaggio."""
    if v.fase == "preparazione":
        return "preparazione"
    if v.fase == "pronta":
        return "pronte"
    if v.fase in ("lavorazione", "da_ritirare"):
        return "esterne"
    if v.fase == "in_viaggio":
        return "viaggio"
    return f"sede-{v.sede_id}"


@bp.route("/stock")
@solo_interni
def piazzale():
    """Stock: ogni auto non ancora consegnata al cliente, anche se è in preparazione, da un fornitore,
    pronta o in viaggio (sempre con la sua posizione sulla mappa). Esce dallo stock alla consegna."""
    stock = Veicolo.query.filter(Veicolo.stato != "consegnato").order_by(Veicolo.marca, Veicolo.modello).all()
    f = {k: request.args.get(k, "").strip() for k in CHIAVI_FILTRO + ("q",)}
    # la sede si sceglie sulla mappa, senza ricaricare: qui non si filtra per sede
    risultati = _filtra_veicoli(stock, {k: v for k, v in f.items() if k != "sede"})
    if f["q"]:
        parole = f["q"].lower().split()
        risultati = [v for v in risultati if all(
            p in f"{v.marca} {v.modello} {v.colore} {v.vin} {v.targa or ''}".lower()
            or vin_mod.normalizza(p).lower() in v.vin.lower()
            for p in parole)]
    # zone: prima le sedi proprie (auto ferme), poi preparazione, lavorazioni esterne, viaggio
    per_zona = {}
    for v in risultati:
        per_zona.setdefault(_zona_stock(v), []).append(v)
    sedi_proprie = sorted({v.sede for v in risultati if _zona_stock(v).startswith("sede-")}, key=lambda x: x.nome)
    gruppi = [{"chiave": f"sede-{sd.id}", "titolo": sd.nome, "icona": "piazzale",
               "voci": per_zona[f"sede-{sd.id}"]} for sd in sedi_proprie]
    gruppi += [{"chiave": k, "titolo": t, "icona": ic, "voci": per_zona[k]} for k, t, ic in ZONE_STOCK if k in per_zona]
    # una sola mappa: da lontano un punto per luogo (sedi e fornitori), avvicinandosi le singole auto
    auto = [{"lat": v.lat, "lon": v.lon, "fase": v.fase, "stato": Veicolo.FASI[v.fase], "sede": v.sede_id,
             "titolo": v.descrizione, "sotto": v.targa or "VIN …" + v.vin[-8:],
             "url": url_for("main.veicolo", veicolo_id=v.id), "id": v.id} for v in risultati if v.lat is not None]
    piazzali = []
    # tutti i piazzali con la posizione, anche vuoti, più i luoghi dove ci sono auto ferme
    luoghi = {v.sede for v in risultati if v.sede is not None and v.stato == "in_sede"}
    luoghi |= set(Sede.query.filter(Sede.attiva.is_(True), Sede.tipo == "propria", Sede.lat.isnot(None)))
    luoghi = sorted(luoghi, key=lambda x: (x.tipo != "propria", x.nome))
    for sede in luoghi:
        voci = [v for v in risultati if v.sede is sede and v.stato == "in_sede"]
        coord = [(v.lat, v.lon) for v in voci if v.lat is not None]
        if sede.lat is not None:
            centro = (sede.lat, sede.lon)
        elif coord:
            centro = (sum(c[0] for c in coord) / len(coord), sum(c[1] for c in coord) / len(coord))
        else:
            continue
        piazzali.append({"id": sede.id, "nome": sede.nome, "n": len(voci), "lat": centro[0], "lon": centro[1],
                         "esterna": sede.esterna})
    senza_posizione = Sede.query.filter(Sede.attiva.is_(True), Sede.tipo == "propria", Sede.lat.is_(None)).count()
    return render_template("piazzale.html", gruppi=gruppi, totale=len(risultati), f=f,
                           opzioni=_filtri_veicoli(stock, f), auto=auto, piazzali=piazzali,
                           senza_posizione=senza_posizione,
                           filtri_attivi=[k for k in CHIAVI_FILTRO if f[k]])


@bp.route("/veicolo/<int:veicolo_id>/preparazione", methods=["POST"])
@solo_interni
def avvia_preparazione(veicolo_id):
    """Auto che non deve andare da fornitori esterni: direttamente in preparazione."""
    v = db.get_or_404(Veicolo, veicolo_id)
    session.pop("ultimo_tocco", None)  # il tocco è servito ad avviarla: il primo passo vuole un nuovo tocco
    try:
        prep = servizi.metti_in_preparazione(v, g.utente, _data_ora("pronta_entro"))
        db.session.commit()
        slot = prep.pronta_entro_at if prep else None
        flash(f"{v.descrizione} è in preparazione per la consegna"
              + (f": in calendario {GIORNI_SETTIMANA[slot.weekday()]} {slot:%d/%m} alle {slot:%H:%M}." if slot else "."), "ok")
    except Vietato as e:
        db.session.rollback()
        flash(str(e), "errore")
    return redirect(request.form.get("torna") or url_for("main.veicolo", veicolo_id=v.id))


def _data_ora(campo):
    """Valore di un campo datetime-local (YYYY-MM-DDTHH:MM), oppure None."""
    try:
        return datetime.strptime(request.form.get(campo, "").strip(), "%Y-%m-%dT%H:%M")
    except ValueError:
        return None


@bp.route("/preparazione/<int:prep_id>/scadenza", methods=["POST"])
@solo_interni
def scadenza_preparazione(prep_id):
    """Quando l'auto deve essere pronta: si fissa una volta sola (se manca), poi non si sposta."""
    prep = db.get_or_404(Preparazione, prep_id)
    try:
        servizi.fissa_scadenza(prep, _data_ora("pronta_entro"))
        db.session.commit()
        flash(f"{prep.veicolo.descrizione}: in calendario il {prep.pronta_entro_at:%d/%m alle %H:%M}.", "ok")
    except Vietato as e:
        db.session.rollback()
        flash(str(e), "errore")
    return redirect(request.form.get("torna") or url_for("main.preparazione"))


@bp.route("/veicolo/<int:veicolo_id>/consegna/sede", methods=["POST"])
@solo_interni
def sede_consegna(veicolo_id):
    """Auto pronta: in quale sede verrà consegnata al cliente. L'appuntamento con il cliente non si
    fissa qui: lo gestisce il portale delle consegne della concessionaria."""
    v = db.get_or_404(Veicolo, veicolo_id)
    try:
        servizi.imposta_consegna(v, g.utente, db.session.get(Sede, request.form.get("sede_id", type=int) or 0))
        db.session.commit()
        flash(f"Consegna presso {v.consegna_sede.nome}."
              + ("" if v.sede_id == v.consegna_sede_id else " Ora emetti l'ultimo DDT per portarla lì."), "ok")
    except Vietato as e:
        db.session.rollback()
        flash(str(e), "errore")
    return redirect(request.form.get("torna") or url_for("main.veicolo", veicolo_id=v.id) + "#consegna")


@bp.route("/veicolo/<int:veicolo_id>/consegna", methods=["POST"])
@solo_interni
def consegna(veicolo_id):
    v = db.get_or_404(Veicolo, veicolo_id)
    try:
        servizi.consegna_al_cliente(v, g.utente)
        db.session.commit()
        flash(f"{v.descrizione} consegnata: la trovi nell'archivio “Consegnate”.", "ok")
    except Vietato as e:
        flash(str(e), "errore")
    return redirect(request.form.get("torna") or url_for("main.veicolo", veicolo_id=v.id))


# ---------------------------------------------------------------- veicoli

@bp.route("/cerca")
@solo_interni
def cerca():
    f = {k: request.args.get(k, "").strip() for k in CHIAVI_FILTRO + ("q",)}
    base = Veicolo.query
    if f["fase"] != "consegnato":
        base = base.filter(Veicolo.stato != "consegnato")
    if f["q"]:
        like = f"%{f['q']}%"
        base = base.filter(or_(
            Veicolo.vin.ilike(f"%{vin_mod.normalizza(f['q'])}"),
            Veicolo.modello.ilike(like), Veicolo.marca.ilike(like), Veicolo.colore.ilike(like),
            Veicolo.targa.ilike(like.replace(" ", ""))))
    tutti = base.order_by(Veicolo.marca, Veicolo.modello, Veicolo.id).limit(500).all()
    return render_template("cerca.html", f=f, risultati=_filtra_veicoli(tutti, f),
                           opzioni=_filtri_veicoli(tutti, f), filtri_attivi=[k for k in CHIAVI_FILTRO if f[k]])


@bp.route("/t/<codice>", methods=["GET", "POST"])
@login_richiesto
def tap(codice):
    """Punto di ingresso di ogni tocco NFC. Prima si rileva la posizione del telefono (obbligatoria:
    senza non si prosegue) e la si salva sull'auto; poi si va dove serve."""
    tag = Tag.query.filter_by(codice=codice.upper()).first()
    if tag is None:
        abort(404, "Adesivo non registrato nel sistema.")
    lat, lon = _coord(usa_tocco=False)  # il tocco rileva sempre una posizione nuova
    if request.method == "POST" and lat is None and current_app.config["DEMO"]:
        lat, lon = _posizione_simulata(tag.veicolo)
    if request.method == "GET" or lat is None:
        return render_template("tocco.html", tag=tag, errore=request.method == "POST")
    session["posizione_tocco"] = {"lat": lat, "lon": lon, "at": time.time()}
    v = tag.veicolo
    if v is not None:  # "fatto" premuto subito dopo questo tocco vale come conferma (vedi tocco_passo)
        session["ultimo_tocco"] = {"veicolo": v.id, "at": time.time()}
    if v is not None and _tocco_aggiorna_posizione(v):
        servizi.registra_evento(v, "tocco", g.utente, lat, lon)
        db.session.commit()
    if _bozza_multiplo().get("raccolta"):
        categoria, messaggio = _aggiungi_a_multiplo(v)
        flash(messaggio, categoria)
        return redirect(url_for("main.ddt_multiplo"))
    if v is None:
        if not g.utente.interno:
            abort(403)
        return redirect(url_for("main.abbina", codice=tag.codice))
    # auto in viaggio: il tocco serve solo a confermare l'arrivo e chiudere il DDT
    if v.movimento_aperto:
        return redirect(url_for("main.arrivo_da_tap", ddt_id=v.movimento_aperto.ddt.id))
    if servizi.puo_vedere_veicolo(v, g.utente):
        return redirect(url_for("main.tocco_scelta", veicolo_id=v.id))
    for m in (v.movimento_predisposto, v.ultimo_arrivo):
        if m and servizi.puo_vedere(m.ddt, g.utente):
            return _torna_al_ddt(m.ddt)
    abort(403, "Questa auto non è in carico alla tua sede.")


@bp.route("/demo/tocco")
@login_richiesto
def demo_tocco():
    """Solo in demo: gli adesivi da "toccare" dal computer, senza NFC né GPS."""
    if not current_app.config["DEMO"]:
        abort(404)
    tag = Tag.query.order_by(Tag.codice).all()
    visibili = [t for t in tag if t.veicolo and t.veicolo.stato != "consegnato"
                and servizi.puo_vedere_veicolo(t.veicolo, g.utente)]
    gruppi = [("Ferme: “Inizia spostamento”", [t for t in visibili if t.veicolo.stato == "in_sede"]),
              ("In viaggio: “Arrivato a destinazione”", [t for t in visibili if t.veicolo.stato == "in_viaggio"])]
    if g.utente.interno:
        gruppi.append(("Adesivi liberi: abbina un'auto", [t for t in tag if t.veicolo is None]))
    return render_template("demo_tocco.html", gruppi=gruppi)


@bp.route("/veicolo/<int:veicolo_id>/tocco")
@login_richiesto
def tocco_scelta(veicolo_id):
    """Dopo il tocco su un'auto ferma: dati dell'auto in alto, poi le azioni. Se è in preparazione,
    l'avanzamento: ogni passo si aggiorna solo con un tocco NFC (un tocco = un passo)."""
    v = _veicolo_visibile(veicolo_id)
    ddt = sorted({m.ddt for m in v.movimenti if m.ddt and not m.ddt.annullato_at and servizi.puo_vedere(m.ddt, g.utente)},
                 key=lambda d: d.emesso_at, reverse=True)
    prep = v.preparazione_attiva
    fatto = request.args.get("fatto", type=int)  # passo appena chiuso: la grafica lo anima
    if prep is None and fatto is not None and v.fase == "pronta":
        prep = v.ultima_preparazione  # ultimo passo appena fatto: si mostra il 100%
    return render_template("tocco_scelta.html", v=v, ddt=ddt, prep=prep, fatto=fatto,
                           da=request.args.get("da", type=int), tocco_valido=_tocco_valido(v),
                           puo_spostare=servizi.puo_emettere(v, g.utente) or servizi.puo_emettere_ultimo_ddt(v, g.utente),
                           no_preparazione=servizi.motivo_no_preparazione(v, g.utente))


def _tocco_valido(v):
    """True se l'adesivo di quest'auto è stato appena toccato e il tocco non è ancora stato usato."""
    tocco = session.get("ultimo_tocco") or {}
    return (tocco.get("veicolo") == v.id
            and time.time() - tocco.get("at", 0) <= servizi.MINUTI_CONFERMA_TOCCO * 60)


def _dopo_il_passo(v, prep, da):
    """Torna alla schermata del tocco animando il passo appena chiuso (da = % prima del passo)."""
    return redirect(url_for("main.tocco_scelta", veicolo_id=v.id, fatto=len(prep.fasi) - 1, da=da))


@bp.route("/veicolo/<int:veicolo_id>/tocco/passo", methods=["POST"])
@solo_interni
def tocco_passo(veicolo_id):
    """Chiude il passo in corso della preparazione. Vale solo subito dopo un tocco dell'adesivo e il
    tocco si consuma: per il passo successivo serve un nuovo tocco."""
    v = _veicolo_visibile(veicolo_id)
    prep = v.preparazione_attiva
    if prep is None:
        return redirect(url_for("main.tocco_scelta", veicolo_id=v.id))
    if not _tocco_valido(v):
        flash("Tocca l'adesivo NFC dell'auto: ogni passo della preparazione si conferma con un tocco.", "errore")
        return redirect(url_for("main.tocco_scelta", veicolo_id=v.id))
    prima = prep.avanzamento
    try:
        servizi.avanza_preparazione(prep, g.utente)
        db.session.commit()
    except Vietato as e:
        db.session.rollback()
        flash(str(e), "errore")
        return redirect(url_for("main.tocco_scelta", veicolo_id=v.id))
    session.pop("ultimo_tocco", None)
    return _dopo_il_passo(v, prep, prima)


@bp.route("/veicolo/<int:veicolo_id>/sposta", methods=["GET", "POST"])
@login_richiesto
def sposta(veicolo_id):
    """Si sceglie solo la destinazione: vettore, modalità, conducente e firma vengono dal profilo di
    guida; il DDT si emette e l'auto parte con un tocco."""
    v = _veicolo_visibile(veicolo_id)
    if not (servizi.puo_emettere(v, g.utente) or servizi.puo_emettere_ultimo_ddt(v, g.utente)):
        flash("Quest'auto ora non si può spostare da qui.", "errore")
        return redirect(url_for("main.veicolo", veicolo_id=v.id))
    if servizi.profilo_guida_mancante(g.utente):
        return redirect(url_for("main.profilo_guida", poi=url_for("main.sposta", veicolo_id=v.id)))
    if request.method == "POST":
        nuova = request.form.get("nuova_destinazione") is not None
        # con una destinazione nuova lat/lon del modulo sono quelli del suo indirizzo, non del telefono
        lat, lon = _posizione_tocco() if nuova else _coord()
        if lat is None:  # il telefono non ha ancora risposto: vale la posizione appena rilevata al tocco
            lat, lon = v.lat, v.lon
        try:
            if nuova:
                sede = servizi.destinazione_scritta(request.form["nuova_destinazione"], request.form.get("tipo"))
            else:
                sede = db.session.get(Sede, request.form.get("a_sede_id", type=int) or 0)
            causale = None
            if request.form.get("a_sede_causale"):  # concessionario/rivenditore: luogo e causale insieme
                sid, _, causale = request.form["a_sede_causale"].partition("|")
                sede = db.session.get(Sede, int(sid) if sid.isdigit() else 0)
            documento = servizi.parti_col_tocco(v, sede, g.utente, lat, lon, causale=causale)
        except Vietato as e:
            db.session.rollback()
            flash(str(e), "errore")
            return redirect(url_for("main.sposta", veicolo_id=v.id))
        db.session.commit()
        m = documento.movimento
        flash(f"DDT n. {documento.sigla} emesso verso {m.a_sede.nome}. Buon viaggio."
              + ("" if m.lavorazione else " Indica la lavorazione prima dell'arrivo."), "ok")
        return _torna_al_ddt(documento)
    preferite, gruppi = servizi.destinazioni_rapide(v, g.utente)
    # partenza fissa: il luogo in cui l'auto è arrivata l'ultima volta, confrontato col tocco appena fatto
    distanza = None
    if v.sede is not None and v.sede.lat is not None and v.lat is not None:
        distanza = servizi.distanza_m(v.lat, v.lon, v.sede.lat, v.sede.lon)
    return render_template("sposta.html", v=v, preferite=preferite, gruppi=gruppi, distanza=distanza,
                           raggio=servizi.RAGGIO_ARRIVO_M, arrivo=v.ultimo_arrivo)


@bp.route("/profilo-guida", methods=["GET", "POST"])
@login_richiesto
def profilo_guida():
    """Vettore, modalità di trasporto e firma di chi guida: si impostano una volta e valgono per ogni
    partenza col tocco."""
    vettori = Vettore.query.filter_by(attivo=True).order_by(Vettore.nome).all()
    poi = request.values.get("poi", "")
    if not poi.startswith("/") or poi.startswith("//"):
        poi = ""
    if request.method == "POST":
        vettore = db.session.get(Vettore, request.form.get("vettore_id", type=int) or 0) \
            or servizi.vettore_predefinito(g.utente)
        mezzo = request.form.get("mezzo", "")
        firma = servizi.firma_valida(request.form.get("firma")) or g.utente.firma
        if vettore is None or not vettore.attivo:
            flash("Scegli il vettore.", "errore")
        elif mezzo not in Movimento.MEZZI:
            flash("Scegli come sposti di solito le auto.", "errore")
        elif not firma:
            flash("Disegna la tua firma nel riquadro.", "errore")
        else:
            g.utente.vettore, g.utente.mezzo, g.utente.firma = vettore, mezzo, firma
            db.session.commit()
            flash("Profilo di guida salvato.", "ok")
            return redirect(poi or url_for("main.home"))
    # il vettore viene dall'azienda per cui si lavora: la concessionaria o il proprio fornitore
    predefinito = servizi.vettore_predefinito(g.utente)
    db.session.commit()
    vettore_id = g.utente.vettore_id or predefinito.id
    vettori = Vettore.query.filter_by(attivo=True).order_by(Vettore.proprio.desc(), Vettore.nome).all()
    return render_template("profilo_guida.html", vettori=vettori, mezzi=Movimento.MEZZI, poi=poi,
                           vettore_id=vettore_id, mezzo=g.utente.mezzo or Movimento.MEZZI[0])


def _posizione_simulata(v):
    """Demo senza GPS: l'auto resta dov'era (o nella sua sede, o nella prima sede propria)."""
    if v is not None and v.lat is not None:
        return v.lat, v.lon
    sede = (v.sede if v is not None and v.sede and v.sede.lat is not None else
            Sede.query.filter(Sede.tipo == "propria", Sede.lat.isnot(None)).first())
    return (sede.lat, sede.lon) if sede else (None, None)


def _tocco_aggiorna_posizione(v):
    """Chi tocca l'adesivo aggiorna la posizione se l'auto è sua da gestire (o gli sta arrivando)."""
    if servizi.puo_vedere_veicolo(v, g.utente):
        return True
    m = v.movimento_aperto
    return m is not None and servizi.puo_vedere(m.ddt, g.utente)


@bp.route("/arrivo/<int:ddt_id>")
@login_richiesto
def arrivo_da_tap(ddt_id):
    """Schermata unica mostrata al tocco di un'auto in viaggio: si può solo confermare l'arrivo."""
    d = db.get_or_404(Ddt, ddt_id)
    m = d.movimento
    if m.stato != "aperto" or d.annullato_at:
        return _torna_al_ddt(d) if servizi.puo_vedere(d, g.utente) else redirect(url_for("main.home"))
    return render_template("arrivo.html", d=d, m=m, raggio=servizi.RAGGIO_ARRIVO_M, lavorazioni=Movimento.LAVORAZIONI,
                           puo_arrivo=servizi.puo_confermare_arrivo(m, g.utente),
                           dopo_tocco=_posizione_tocco()[0] is not None)


@bp.route("/abbina/<codice>", methods=["GET", "POST"])
@solo_interni
def abbina(codice):
    tag = Tag.query.filter_by(codice=codice).first_or_404()
    if tag.veicolo is not None:
        return redirect(url_for("main.veicolo", veicolo_id=tag.veicolo_id))
    sedi = Sede.query.filter_by(attiva=True, tipo="propria").order_by(Sede.nome).all()

    if request.method == "POST":
        vin = vin_mod.normalizza(request.form.get("vin"))
        if not vin_mod.valido(vin):
            flash("VIN non valido: servono 17 caratteri (lettere I, O, Q non ammesse).", "errore")
            return render_template("abbina.html", tag=tag, sedi=sedi, form=request.form)
        veicolo = Veicolo.query.filter_by(vin=vin).first()
        if veicolo and veicolo.tag:
            flash(f"Questo VIN è già abbinato all'adesivo {veicolo.tag.codice}.", "errore")
            return render_template("abbina.html", tag=tag, sedi=sedi, form=request.form)
        if veicolo is None:
            # se arriva dal gestionale (import) esiste già: lo si completa
            veicolo = Veicolo(vin=vin)
            db.session.add(veicolo)
        for campo in ("marca", "modello", "versione", "colore"):
            valore = " ".join(request.form.get(campo, "").split())
            if valore:
                setattr(veicolo, campo, valore)
        if not veicolo.marca:  # marca non scritta: quella del costruttore ricavata dal VIN
            veicolo.marca = vin_mod.marca_da_vin(vin)
        if request.form.get("tipologia") in Veicolo.TIPOLOGIE:
            veicolo.tipologia = request.form["tipologia"]
        if request.form.get("condizione") not in Veicolo.CONDIZIONI:
            flash("Indica se l'auto è nuova o usata.", "errore")
            return render_template("abbina.html", tag=tag, sedi=sedi, form=request.form)
        veicolo.condizione = request.form["condizione"]
        if _targa_insolita():
            flash(_targa_insolita(), "errore")
            return render_template("abbina.html", tag=tag, sedi=sedi, form=request.form)
        if _targa():
            veicolo.targa = _targa()
        veicolo.stato, veicolo.consegnato_at = "in_sede", None
        veicolo.sede_id = request.form.get("sede_id", type=int)
        tag.veicolo, tag.abbinato_at = veicolo, adesso()
        servizi.registra_evento(veicolo, "abbinamento", g.utente, *_coord())
        db.session.commit()
        marchi.assicura(veicolo.marca)  # marca nuova: il logo si scarica da solo
        flash("Adesivo abbinato.", "ok")
        return redirect(url_for("main.veicolo", veicolo_id=veicolo.id))

    return render_template("abbina.html", tag=tag, sedi=sedi, form={})


def _targa():
    return "".join(ch for ch in request.form.get("targa", "").upper() if ch.isalnum())[:12]


# targa italiana dal 1994: due lettere, tre cifre, due lettere (senza I, O, Q, U)
TARGA_ITALIANA = re.compile(r"[A-HJ-NPR-TV-Z]{2}[0-9]{3}[A-HJ-NPR-TV-Z]{2}")


def _targa_insolita():
    """Messaggio se la targa scritta non ha il formato italiano e nessuno ha confermato che è giusta così
    (targa estera o di vecchio formato). La targa non si cambia più: meglio un controllo in più."""
    t = _targa()
    if t and not TARGA_ITALIANA.fullmatch(t) and not request.form.get("targa_ok"):
        return (f"La targa {t} non ha il formato italiano (es. GH123AB): controllala oppure spunta "
                "“È giusta così” se è estera o di vecchio formato.")
    return None


@bp.route("/piazzale/nuova", methods=["GET", "POST"])
@solo_interni
def nuova_auto():
    """Auto nuova in stock: entra solo abbinata al suo adesivo NFC (scelto tra quelli liberi)."""
    sedi = Sede.query.filter_by(attiva=True, tipo="propria").order_by(Sede.nome).all()
    liberi = Tag.query.filter(Tag.veicolo_id.is_(None)).order_by(Tag.codice).all()
    if request.method == "POST":
        vin = vin_mod.normalizza(request.form.get("vin"))
        sede = db.session.get(Sede, request.form.get("sede_id", type=int) or 0)
        tag = Tag.query.filter_by(codice=(request.form.get("tag") or "").strip().upper(), veicolo_id=None).first()
        condizione = request.form.get("condizione", "")
        if condizione not in Veicolo.CONDIZIONI:
            flash("Indica se l'auto è nuova o usata.", "errore")
        elif tag is None:
            flash("Scegli l'adesivo NFC da attaccare all'auto: senza adesivo l'auto non entra in stock.", "errore")
        elif not vin_mod.valido(vin):
            flash("VIN non valido: servono 17 caratteri (lettere I, O, Q non ammesse).", "errore")
        elif Veicolo.query.filter_by(vin=vin).filter(Veicolo.sede_id.isnot(None)).first():
            flash("Questo VIN è già in Getvingo.", "errore")
        elif sede is None or sede.tipo != "propria":
            flash("Scegli in quale sede si trova.", "errore")
        elif _targa_insolita():
            flash(_targa_insolita(), "errore")
        else:
            # se arriva dal gestionale (import) l'anagrafica esiste già: la si completa
            v = Veicolo.query.filter_by(vin=vin).first() or Veicolo(vin=vin)
            db.session.add(v)
            v.sede, v.stato, v.consegnato_at = sede, "in_sede", None
            v.condizione = condizione
            v.targa = _targa() or v.targa
            if request.form.get("tipologia") in Veicolo.TIPOLOGIE:
                v.tipologia = request.form["tipologia"]
            for k in ("marca", "modello", "versione", "colore"):
                if request.form.get(k, "").strip():
                    setattr(v, k, " ".join(request.form[k].split()))
            if not v.marca:  # marca non scritta: quella del costruttore ricavata dal VIN
                v.marca = vin_mod.marca_da_vin(vin)
            tag.veicolo, tag.abbinato_at = v, adesso()
            servizi.registra_evento(v, "inserimento", g.utente, *_coord())
            db.session.commit()
            marchi.assicura(v.marca)  # marca nuova: il logo si scarica da solo
            flash(f"{v.descrizione} aggiunta allo stock con l'adesivo {tag.codice}.", "ok")
            return redirect(url_for("main.veicolo", veicolo_id=v.id))
        return render_template("abbina.html", tag=None, sedi=sedi, tag_liberi=liberi, form=request.form)
    return render_template("abbina.html", tag=None, sedi=sedi, tag_liberi=liberi, form={})


@bp.route("/veicolo/<int:veicolo_id>/modifica", methods=["GET", "POST"])
@solo_interni
def modifica_auto(veicolo_id):
    """Tutti i dati dell'auto, con gli stessi campi dell'ingresso in stock (tranne dove si trova:
    quello cambia solo con un DDT o con "Correggi la posizione" dell'amministratore)."""
    v = _veicolo_visibile(veicolo_id)
    puo_posizione = servizi.puo_correggere_posizione(v, g.utente)
    sedi = (Sede.query.filter(Sede.attiva.is_(True), Sede.tipo.in_(("propria",) + Sede.FORNITORI))
            .order_by(Sede.tipo != "propria", Sede.nome).all() if puo_posizione else [])

    def pagina(form):
        return render_template("veicolo_modifica.html", v=v, form=form, puo_posizione=puo_posizione, sedi=sedi)

    if request.method == "GET":
        form = {"sede_id": v.sede_id, "lat": v.lat if v.lat is not None else "",
                "lon": v.lon if v.lon is not None else "", "vin": v.vin, "condizione": v.condizione, "tipologia": v.tipologia, "targa": v.targa or "",
                "marca": v.marca or "", "modello": v.modello or "", "versione": v.versione or "",
                "colore": v.colore or "", "in_stock_dal": v.creato_at.date().isoformat() if v.creato_at else ""}
        return pagina(form)
    f = request.form
    vin = vin_mod.normalizza(f.get("vin"))
    pulito = {k: " ".join(f.get(k, "").split()) for k in ("marca", "modello", "versione", "colore")}
    try:
        giorno = date.fromisoformat(f.get("in_stock_dal", ""))
    except ValueError:
        giorno = None
    errore = None
    if not vin_mod.valido(vin):
        errore = "VIN non valido: servono 17 caratteri (lettere I, O, Q non ammesse)."
    elif Veicolo.query.filter(Veicolo.vin == vin, Veicolo.id != v.id).first():
        errore = "Questo VIN è già di un'altra auto in Getvingo."
    elif f.get("condizione") not in Veicolo.CONDIZIONI:
        errore = "Indica se l'auto è nuova o usata."
    elif not pulito["marca"] or not pulito["modello"]:
        errore = "Marca e modello sono obbligatori."
    elif giorno is None or giorno > adesso().date():
        errore = "In stock dal: scegli una data valida, non nel futuro."
    elif _targa_insolita():
        errore = _targa_insolita()
    elif not _targa() and v.stato == "consegnato":
        errore = "L'auto è consegnata: la targa non si può togliere."
    sposta = puo_posizione and f.get("sede_id")  # senza sede nel modulo la posizione non cambia
    sede = Sede.query.filter_by(id=f.get("sede_id", type=int)).first() if sposta else None
    if not errore and sposta and not servizi.luogo_ammesso(sede):
        errore = "Scegli una sede della concessionaria o un fornitore esterno."
    if errore:
        flash(errore, "errore")
        return pagina(f)
    if sposta:  # solo amministratore: niente DDT, niente traccia
        lat, lon = f.get("lat", type=float), f.get("lon", type=float)
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            lat = lon = None
        servizi.correggi_posizione(v, g.utente, sede, lat, lon)
    v.vin, v.condizione, v.targa = vin, f["condizione"], _targa()
    if f.get("tipologia") in Veicolo.TIPOLOGIE:
        v.tipologia = f["tipologia"]
    v.marca, v.modello = pulito["marca"], pulito["modello"]
    v.versione, v.colore = pulito["versione"][:120], pulito["colore"][:80]
    if v.creato_at is None or giorno != v.creato_at.date():
        v.creato_at = datetime.combine(giorno, v.creato_at.time() if v.creato_at else datetime.min.time())
    db.session.commit()
    marchi.assicura(v.marca)  # marca nuova: il logo si scarica da solo
    flash("Dati dell'auto aggiornati.", "ok")
    return redirect(url_for("main.veicolo", veicolo_id=v.id))


@bp.route("/veicolo/<int:veicolo_id>/dati", methods=["POST"])
@solo_interni
def modifica_dati_auto(veicolo_id):
    """Scorciatoie sul posto: nuova/usata se manca, targa se manca (sezione Consegna).
    Tutti i dati insieme si cambiano da "Modifica dati" (modifica_auto)."""
    v = db.get_or_404(Veicolo, veicolo_id)
    if request.form.get("condizione") in Veicolo.CONDIZIONI:  # nuova o usata: si indica una volta sola
        if not v.condizione:
            v.condizione = request.form["condizione"]
            db.session.commit()
            flash(f"{v.descrizione}: {Veicolo.CONDIZIONI[v.condizione].lower()}.", "ok")
        return redirect(url_for("main.veicolo", veicolo_id=v.id))
    if v.targa:
        flash("La targa è già stata inserita e non si può cambiare.", "errore")
    elif not _targa():
        flash("Scrivi la targa.", "errore")
    elif _targa_insolita():
        flash(_targa_insolita(), "errore")
    else:
        v.targa = _targa()
        db.session.commit()
        flash(f"Targa {v.targa} aggiunta.", "ok")
    return _torna(url_for("main.veicolo", veicolo_id=v.id))


@bp.route("/api/veicolo")
@solo_interni
def api_veicolo():
    """Dati già noti di un VIN (es. importato dal gestionale), per precompilare l'abbinamento;
    se l'auto non c'è ancora, almeno la marca ricavata dal VIN."""
    vin = vin_mod.normalizza(request.args.get("vin"))
    v = Veicolo.query.filter_by(vin=vin).first()
    if v is None:
        marca = vin_mod.marca_da_vin(vin)
        return ({"vin": vin, "marca": marca, "da_vin": True} if marca else ({}, 404))
    return {"vin": v.vin, "marca": v.marca, "modello": v.modello, "versione": v.versione, "colore": v.colore, "tipologia": v.tipologia,
            "targa": v.targa}


@bp.route("/veicolo/<int:veicolo_id>/note", methods=["POST"])
@solo_interni
def note_interne(veicolo_id):
    """Note interne della concessionaria sull'auto (cliente, venditore, appunti): non vanno sul DDT
    e i fornitori non le vedono."""
    v = _veicolo_visibile(veicolo_id)
    v.cliente_nome = " ".join(request.form.get("cliente_nome", "").split())[:200]
    vid = request.form.get("venditore_id", type=int)
    venditore = db.session.get(Utente, vid) if vid else None
    v.venditore = venditore if venditore is not None and venditore.interno else None
    v.note_interne = request.form.get("note_interne", "").strip()[:2000]
    venduta = request.form.get("venduta_il", "").strip()
    try:  # data di vendita: serve alle analisi (dalla vendita alla consegna)
        v.venduta_il = datetime.strptime(venduta, "%Y-%m-%d").date() if venduta else None
    except ValueError:
        pass
    db.session.commit()
    flash("Note interne salvate: restano nel portale, non vanno sul DDT.", "ok")
    return redirect(url_for("main.veicolo", veicolo_id=v.id) + "#note-interne")


@bp.route("/veicolo/<int:veicolo_id>")
@login_richiesto
def veicolo(veicolo_id):
    v = _veicolo_visibile(veicolo_id)
    mov = v.movimento_aperto
    ultimo = v.ultimo_arrivo
    valori = {"conducente": g.utente.nome if g.utente.ruolo != "admin" else "",
              "vettore_id": servizi.ultimo_vettore_id(g.utente)}
    if v.sede and v.sede.esterna and ultimo:
        valori.update(a_sede_id=ultimo.da_sede_id, causale="Reso lavorazione",
                      lavorazione=Movimento.NESSUNA)
    venditori = sorted((u for u in Utente.query.filter_by(attivo=True, supporto=False) if u.interno),
                       key=lambda u: (u.ruolo != "venditore", u.nome)) if g.utente.interno else []
    simili = Veicolo.query.filter(Veicolo.marca == v.marca, Veicolo.modello == v.modello,
                                  Veicolo.id != v.id, Veicolo.stato != "consegnato").count()
    return render_template(
        "veicolo.html", v=v, mov=mov, predisposto=v.movimento_predisposto, ultimo=ultimo, venditori=venditori,
        preparazione=v.preparazione_attiva, ultima_prep=v.ultima_preparazione, simili=simili,
        puo_emettere=servizi.puo_emettere(v, g.utente),
        puo_ultimo_ddt=servizi.puo_emettere_ultimo_ddt(v, g.utente),
        sedi_consegna=Sede.query.filter_by(attiva=True, tipo="propria").order_by(Sede.nome).all(),
        puo_predisporre=servizi.puo_predisporre(v, g.utente),
        puo_consegnare=servizi.puo_consegnare(v, g.utente),
        puo_lavorazione=ultimo is not None and servizi.puo_completare_lavorazione(ultimo, g.utente),
        puo_presa=v.movimento_predisposto is not None
        and servizi.puo_prendere_in_carico(v.movimento_predisposto, g.utente),
        storico=sorted((m for m in v.movimenti if m.ddt and not m.ddt.annullato_at
                        and servizi.puo_vedere(m.ddt, g.utente)),
                       key=lambda m: m.ddt.emesso_at, reverse=True),
        valori=valori, ore_ritiro=ORE_RITIRO, ia_attiva=ia.disponibile(),
        **_contesto_modulo(escludi_sede_id=v.sede_id),
    )


@bp.route("/veicolo/<int:veicolo_id>/partenza", methods=["POST"])
@login_richiesto
def partenza(veicolo_id):
    v = _veicolo_visibile(veicolo_id)
    predisponi = request.form.get("azione") == "predisponi"
    dati, errore = _dati_movimento(conducente_obbligatorio=not predisponi)
    if errore:
        flash(errore, "errore")
        return redirect(url_for("main.veicolo", veicolo_id=v.id))
    try:
        documento = servizi.crea_movimento(
            v, dati, g.utente, firma_conducente=servizi.firma_valida(request.form.get("firma")),
            predisponi_ore=request.form.get("ore_ritiro", 24, type=int) if predisponi else None,
            **dict(zip(("lat", "lon"), _coord())))
    except Vietato as e:
        db.session.rollback()
        flash(str(e), "errore")
        return redirect(url_for("main.veicolo", veicolo_id=v.id))
    db.session.commit()
    if predisponi:
        flash(f"DDT n. {documento.sigla} predisposto: va ritirato entro le "
              f"{documento.movimento.ritiro_entro_at:%H:%M del %d/%m}.", "ok")
    else:
        flash(f"DDT n. {documento.sigla} emesso. Buon viaggio.", "ok")
    return _torna_al_ddt(documento)


@bp.route("/veicolo/<int:veicolo_id>/riposiziona", methods=["POST"])
@solo_interni
def riposiziona(veicolo_id):
    v = db.get_or_404(Veicolo, veicolo_id)
    lat, lon = _coord()
    if lat is None:
        flash("Posizione GPS non disponibile: attiva la localizzazione e riprova.", "errore")
    else:
        servizi.registra_evento(v, "riposizionamento", g.utente, lat, lon)
        db.session.commit()
        flash("Posizione aggiornata.", "ok")
    return redirect(url_for("main.veicolo", veicolo_id=v.id))


# ---------------------------------------------------------------- DDT

# "In corso" = tutti tranne gli arrivati (che hanno la loro scheda) e i cancellati
FILTRI_STATO = {"tutti": "In corso", "predisposto": "Da ritirare", "aperto": "In viaggio",
                "chiuso": "Arrivati", "annullati": "Cancellati"}


def _data(campo):
    try:
        return datetime.strptime(request.args.get(campo, ""), "%Y-%m-%d")
    except ValueError:
        return None


@bp.route("/ddt")
@login_richiesto
def archivio_ddt():
    f = {k: request.args.get(k, "").strip() for k in
         ("q", "vin", "mezzo", "tipologia", "conducente", "creato_da", "destinazione", "lavorazione", "dal", "al")}
    stato = request.args.get("stato", "tutti")
    if stato not in FILTRI_STATO or (stato == "annullati" and g.utente.ruolo != "admin"):
        stato = "tutti"

    # niente DISTINCT sui DDT: Postgres non sa confrontare la colonna JSON dati_fissi.
    # I filtri scelgono gli id, i DDT si caricano a parte (una volta sola ciascuno)
    query = Ddt.query.join(Movimento, Movimento.ddt_id == Ddt.id).join(Veicolo)
    if not g.utente.interno:
        sid = g.utente.sede_id or -1
        query = query.filter(or_(Movimento.da_sede_id == sid, Movimento.a_sede_id == sid,
                                 Movimento.arrivo_sede_id == sid))
    visibili = query  # base per contare gli stati e calcolare le opzioni presenti
    if stato == "annullati":
        query = query.filter(Ddt.annullato_at.isnot(None))
    else:
        query = query.filter(Ddt.annullato_at.is_(None))
        if stato == "tutti":
            query = query.filter(Movimento.stato != "chiuso")
        else:
            query = query.filter(Movimento.stato == stato)
    if f["q"]:
        like = f"%{f['q']}%"
        query = query.filter(or_(
            Veicolo.vin.ilike(f"%{vin_mod.normalizza(f['q'])}"), Veicolo.modello.ilike(like),
            Veicolo.marca.ilike(like), Movimento.conducente.ilike(like),
            Ddt.numero == (int(f["q"]) if f["q"].isdigit() else -1)))
    if f["vin"]:
        query = query.filter(Veicolo.vin.ilike(f"%{vin_mod.normalizza(f['vin'])}%"))
    if f["mezzo"]:
        like = f"%{f['mezzo']}%"
        query = query.filter(or_(Veicolo.marca.ilike(like), Veicolo.modello.ilike(like),
                                 Veicolo.colore.ilike(like)))
    if f["tipologia"] in Veicolo.TIPOLOGIE:
        query = query.filter(Veicolo.tipologia == f["tipologia"])
    if f["conducente"]:
        query = query.filter(Movimento.conducente.ilike(f"%{f['conducente']}%"))
    if f["creato_da"].isdigit():
        query = query.filter(Ddt.creato_da_id == int(f["creato_da"]))
    if f["destinazione"].isdigit():
        query = query.filter(Movimento.a_sede_id == int(f["destinazione"]))
    if f["lavorazione"] in Movimento.LAVORAZIONI:
        query = query.filter(Movimento.lavorazione == f["lavorazione"])
    if _data("dal"):
        query = query.filter(Ddt.emesso_at >= _data("dal"))
    if _data("al"):
        query = query.filter(Ddt.emesso_at < _data("al") + timedelta(days=1))

    def _ddt_di(q):
        return Ddt.query.filter(Ddt.id.in_(q.with_entities(Ddt.id).statement))

    elenco = _ddt_di(query).order_by(Ddt.anno.desc(), Ddt.numero.desc()).limit(300).all()
    avanzati = [k for k in f if k != "q" and f[k]]
    tutti = _ddt_di(visibili).all()
    attivi = [d for d in tutti if not d.annullato_at]
    conteggi = {"tutti": sum(1 for d in attivi if d.movimento.stato != "chiuso"), "annullati": len(tutti) - len(attivi)}
    for d in attivi:
        conteggi[d.movimento.stato] = conteggi.get(d.movimento.stato, 0) + 1
    return render_template(
        "ddt_archivio.html", elenco=elenco, f=f, stato=stato, filtri_stato=FILTRI_STATO,
        f_attivi={k: v for k, v in f.items() if v}, avanzati=avanzati, conteggi=conteggi,
        autori=sorted({(str(d.creato_da.id), d.creato_da.nome) for d in tutti}, key=lambda x: x[1]),
        destinazioni=sorted({(str(d.movimento.a_sede.id), d.movimento.a_sede.nome) for d in tutti},
                            key=lambda x: x[1]),
        lavorazioni=_opzioni((d.movimento.lavorazione for d in tutti), attuale=f["lavorazione"]),
        tipologie=[(k, Veicolo.TIPOLOGIE[k]) for k in _opzioni((d.movimento.veicolo.tipologia for d in tutti),
                                                                 attuale=f["tipologia"])])


@bp.route("/ddt/<int:ddt_id>")
@login_richiesto
def ddt_dettaglio(ddt_id):
    d = _ddt_visibile(ddt_id)
    m = d.movimento
    attivo = d.annullato_at is None
    return render_template(
        "ddt_dettaglio.html", d=d, m=m, mittente=servizi.dati_ddt(d)[0],
        puo_modificare=servizi.puo_modificare(d, g.utente),
        puo_presa=attivo and not d.multiplo and servizi.puo_prendere_in_carico(m, g.utente),
        puo_arrivo=attivo and servizi.puo_confermare_arrivo(m, g.utente),
        puo_lavorazione=attivo and not d.multiplo and servizi.puo_completare_lavorazione(m, g.utente),
        puo_annullare=attivo and g.utente.ruolo == "admin",
        puo_rinnovare=attivo and m.stato == "predisposto" and servizi.puo_modificare(d, g.utente),
        nome_suggerito=m.conducente or g.utente.nome, raggio=servizi.RAGGIO_ARRIVO_M, lavorazioni=Movimento.LAVORAZIONI)


# ---------------------------------------------------------------- DDT multiplo

DURATA_BOZZA = 2 * 3600  # secondi: dopo, la raccolta con gli adesivi si spegne da sola


def _bozza_multiplo():
    """Bozza del DDT multiplo in sessione: {sede, auto, raccolta, at}. Scaduta = vuota."""
    bozza = session.get("multiplo") or {}
    if bozza and time.time() - bozza.get("at", 0) > DURATA_BOZZA:
        session.pop("multiplo", None)
        return {}
    return bozza


def _salva_bozza(**campi):
    bozza = dict(_bozza_multiplo(), **campi, at=time.time())
    session["multiplo"] = bozza
    return bozza


def _candidati_multiplo():
    """Auto che l'utente può far partire adesso, per sede di partenza."""
    candidati = [v for v in Veicolo.query.filter_by(stato="in_sede").order_by(Veicolo.marca, Veicolo.modello, Veicolo.id)
                 if v.sede is not None and servizi.puo_emettere(v, g.utente)]
    per_sede = {}
    for v in candidati:
        per_sede.setdefault(v.sede, []).append(v)
    # le sedi della concessionaria compaiono anche vuote: la partenza la sceglie l'utente
    if g.utente.interno:
        for sede in Sede.query.filter_by(attiva=True, tipo="propria"):
            per_sede.setdefault(sede, [])
    elif g.utente.sede is not None:
        per_sede.setdefault(g.utente.sede, [])
    return sorted(per_sede.items(), key=lambda x: (-len(x[1]), x[0].nome))


def _aggiungi_a_multiplo(v):
    """Aggiunge l'auto toccata alla bozza. Ritorna (categoria flash, messaggio)."""
    if v is None:
        return "errore", "Questo adesivo non è ancora abbinato a un'auto."
    nome = f"{v.descrizione} …{v.vin[-8:]}"
    if not (v.sede is not None and servizi.puo_emettere(v, g.utente)):
        return "errore", f"{nome} non può partire adesso (non è in sede, ha già un DDT o non è tua)."
    bozza = _bozza_multiplo()
    auto = list(bozza.get("auto", []))
    if v.id in auto:
        return "ok", f"{nome} è già nel DDT multiplo."
    if auto and bozza.get("sede") not in (None, v.sede.id):
        return "errore", (f"{nome} è a {v.sede.nome}: le auto del DDT partono tutte da "
                          f"{db.session.get(Sede, bozza['sede']).nome}.")
    _salva_bozza(sede=v.sede.id, auto=auto + [v.id])
    return "ok", f"Aggiunta {nome} · {len(auto) + 1} auto nel DDT multiplo."


@bp.route("/ddt/multiplo", methods=["GET", "POST"])
@login_richiesto
def ddt_multiplo():
    """Un solo DDT per più auto che partono insieme (si crea solo da qui, dalla pagina DDT).
    Le auto si spuntano dall'elenco oppure si aggiungono toccando i loro adesivi."""
    per_sede = _candidati_multiplo()
    bozza = _bozza_multiplo()
    if request.method == "POST":
        sede_id = request.form.get("da_sede_id", type=int)
        scelti = request.form.getlist("veicolo", type=int)
        _salva_bozza(sede=sede_id, auto=scelti)
        dati, errore = _dati_movimento(manca_conducente="Indica il responsabile dello spostamento.")
        disponibili = {v.id: v for sede, auto in per_sede if sede.id == sede_id for v in auto}
        veicoli = [disponibili[i] for i in scelti if i in disponibili]
        if errore:
            flash(errore, "errore")
        elif len(veicoli) != len(set(scelti)):
            flash("Alcune auto scelte non sono più disponibili in questa sede: ricontrolla l'elenco.", "errore")
        else:
            try:
                documento = servizi.crea_ddt_multiplo(
                    veicoli, dati, g.utente, firma_conducente=servizi.firma_valida(request.form.get("firma")),
                    **dict(zip(("lat", "lon"), _coord())))
                db.session.commit()
                session.pop("multiplo", None)
                flash(f"DDT multiplo n. {documento.sigla} emesso per {len(veicoli)} auto. Buon viaggio.", "ok")
                return _torna_al_ddt(documento)
            except Vietato as e:
                db.session.rollback()
                flash(str(e), "errore")
        bozza = _bozza_multiplo()
    sedi_ids = [sede.id for sede, _ in per_sede]
    sede_id = request.args.get("sede", type=int) or bozza.get("sede")
    if sede_id not in sedi_ids:
        sede_id = sedi_ids[0] if sedi_ids else None
    valori = {k: request.form.get(k, "") for k in servizi.CAMPI_MODIFICABILI}
    for k in ("a_sede_id", "vettore_id"):
        valori[k] = request.form.get(k, type=int)
    if not valori["vettore_id"]:
        valori["vettore_id"] = servizi.ultimo_vettore_id(g.utente)
    valori["conducente"] = valori["conducente"] or g.utente.nome
    return render_template("ddt_multiplo.html", per_sede=per_sede, sede_id=sede_id,
                           scelti=set(bozza.get("auto", [])), raccolta=bool(bozza.get("raccolta")),
                           valori=valori, minimo=servizi.MIN_MULTIPLO, **_contesto_modulo())


@bp.route("/ddt/multiplo/raccolta", methods=["POST"])
@login_richiesto
def ddt_multiplo_raccolta():
    """Accende o spegne l'aggiunta delle auto toccando gli adesivi."""
    accendi = request.form.get("azione") == "avvia"
    _salva_bozza(raccolta=accendi, sede=request.form.get("da_sede_id", type=int) or _bozza_multiplo().get("sede"),
                 auto=request.form.getlist("veicolo", type=int))
    if accendi:
        flash("Raccolta attiva: tocca l'adesivo sul parabrezza di ogni auto da caricare.", "ok")
    return redirect(url_for("main.ddt_multiplo"))


@bp.route("/api/ddt-multiplo", methods=["POST"])
@login_richiesto
def api_ddt_multiplo():
    """Tiene la bozza allineata alla pagina (sede e spunte) e, su Android, riceve gli adesivi
    letti dalla pagina stessa senza uscirne (Web NFC)."""
    dati = request.get_json(silent=True) or {}
    if dati.get("codice"):
        tag = Tag.query.filter_by(codice=str(dati["codice"]).upper()).first()
        if tag is None:
            return {"ok": False, "messaggio": "Adesivo non registrato nel sistema."}
        try:
            lat, lon = float(dati["lat"]), float(dati["lon"])
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "messaggio": "Senza la posizione non si può proseguire: attiva la localizzazione."}
        if tag.veicolo is not None and _tocco_aggiorna_posizione(tag.veicolo):
            servizi.registra_evento(tag.veicolo, "tocco", g.utente, lat, lon)
            db.session.commit()
        categoria, messaggio = _aggiungi_a_multiplo(tag.veicolo)
        flash(messaggio, categoria)
        return {"ok": categoria == "ok", "messaggio": messaggio}
    _salva_bozza(sede=dati.get("sede"), auto=[int(x) for x in dati.get("auto", []) if str(x).isdigit()])
    return {"ok": True}


@bp.route("/ddt/<int:ddt_id>/modifica", methods=["GET", "POST"])
@login_richiesto
def ddt_modifica(ddt_id):
    documento = _ddt_visibile(ddt_id)
    if not servizi.puo_modificare(documento, g.utente):
        abort(403, "Il DDT lo modifica solo chi l'ha emesso; dopo l'arrivo solo l'ufficio.")
    m = documento.movimento
    if request.method == "POST":
        dati, errore = _dati_movimento(conducente_obbligatorio=m.stato != "predisposto")
        if errore:
            flash(errore, "errore")
        else:
            rev = servizi.modifica_ddt(documento, dati, g.utente)
            db.session.commit()
            flash(f"DDT aggiornato (revisione {rev.numero})." if rev else "Nessuna modifica.", "ok")
            return _torna_al_ddt(documento)
    valori = {k: getattr(m, k) for k in servizi.CAMPI_MODIFICABILI}
    return render_template("ddt_modifica.html", d=documento, m=m, valori=valori,
                           **_contesto_modulo(escludi_sede_id=m.da_sede_id))


def _azione_ddt(ddt_id, funzione, messaggio):
    """Esegue un'azione sul DDT gestendo permessi ed errori in modo uniforme."""
    documento = _ddt_visibile(ddt_id)
    if documento.annullato_at:
        flash("Questo DDT è stato cancellato.", "errore")
        return _torna_al_ddt(documento)
    try:
        funzione(documento)
        db.session.commit()
        flash(messaggio, "ok")
    except Vietato as e:
        db.session.rollback()
        flash(str(e), "errore")
    return _torna_al_ddt(documento)


@bp.route("/ddt/<int:ddt_id>/presa-in-carico", methods=["POST"])
@login_richiesto
def ddt_presa_in_carico(ddt_id):
    return _azione_ddt(ddt_id, lambda d: servizi.prendi_in_carico(
        d.movimento, g.utente, request.form.get("conducente", "").strip()[:120],
        servizi.firma_valida(request.form.get("firma")), *_coord(), mezzo=request.form.get("mezzo", "")),
        "Presa in carico confermata. Buon viaggio.")


@bp.route("/ddt/<int:ddt_id>/rinnova", methods=["POST"])
@login_richiesto
def ddt_rinnova(ddt_id):
    documento = _ddt_visibile(ddt_id)
    if not servizi.puo_modificare(documento, g.utente):
        abort(403)
    ore = request.form.get("ore", 24, type=int)
    return _azione_ddt(ddt_id, lambda d: servizi.rinnova_ritiro(d.movimento, ore),
                       f"Tempo di ritiro rinnovato di {ore} ore.")


@bp.route("/ddt/<int:ddt_id>/arrivo", methods=["POST"])
@login_richiesto
def ddt_arrivo(ddt_id):
    def arriva(d):
        # DDT multiplo: le auto viaggiano insieme e arrivano insieme
        in_viaggio = [m for m in d.movimenti or [d.movimento] if m.stato == "aperto"] or [d.movimento]
        posizione = _coord()
        if current_app.config["DEMO"] and posizione[0] is None:  # demo senza GPS: come se fosse sul posto
            dest = d.movimento.a_sede
            posizione = (dest.lat, dest.lon) if dest.lat is not None else _posizione_simulata(d.movimento.veicolo)
        for m in in_viaggio:
            servizi.registra_arrivo(m, g.utente, *posizione, altrove_id=request.form.get("arrivo_sede_id", type=int),
                                    note=request.form.get("note", ""))
    documento = db.get_or_404(Ddt, ddt_id)
    risposta = _azione_ddt(ddt_id, arriva, "Arrivo confermato e DDT chiuso: posizione delle auto aggiornata."
                           if documento.multiplo else "Arrivo confermato e DDT chiuso: posizione dell'auto aggiornata.")
    if all(m.stato == "chiuso" for m in documento.movimenti or [documento.movimento]):
        # DDT completo: la copia firmata parte per email alle due parti e chi conferma la scarica sul telefono
        try:
            copie.invia(documento)
        except Exception:  # l'invio non deve mai bloccare la chiusura del DDT
            current_app.logger.exception("Copie del DDT %s non inviate", documento.sigla)
        session["scarica_ddt"] = documento.id
    if documento.multiplo:
        return risposta
    m = documento.movimento
    if m.stato == "chiuso" and request.form.get("danno") == "1":
        return redirect(url_for("main.nuovo_danno", veicolo_id=m.veicolo_id, movimento=m.id))
    # rientro in sede: si va dritti alla preparazione
    if m.stato == "chiuso" and m.veicolo.fase == "preparazione" and g.utente.interno:
        return redirect(url_for("main.preparazione"))
    return risposta


@bp.route("/ddt/<int:ddt_id>/lavorazione", methods=["POST"])
@login_richiesto
def ddt_lavorazione(ddt_id):
    """Lavorazione lasciata da indicare alla partenza col tocco: la completa chi ha in mano l'auto,
    in qualsiasi momento prima della chiusura del DDT."""
    def indica(d):
        if d.movimento.stato != "aperto" or d.movimento.lavorazione:
            raise Vietato("La lavorazione di questo DDT è già indicata: si cambia da “Modifica DDT”.")
        servizi.indica_lavorazione(d, g.utente, request.form.get("lavorazione", ""),
                                   request.form.get("lavorazione_note", ""))
    risposta = _azione_ddt(ddt_id, indica, "Lavorazione indicata: ora il DDT si può chiudere all'arrivo.")
    if request.form.get("da") == "arrivo":
        return redirect(url_for("main.arrivo_da_tap", ddt_id=ddt_id))
    return risposta


@bp.route("/ddt/<int:ddt_id>/lavorazione-completata", methods=["POST"])
@login_richiesto
def ddt_lavorazione_completata(ddt_id):
    def completa(d):
        # DDT multiplo: la lavorazione si chiude auto per auto
        scelto = request.form.get("movimento_id", type=int)
        mov = next((m for m in d.movimenti if m.id == scelto), d.movimento)
        rientro = servizi.completa_lavorazione(mov, g.utente, request.form.get("note", ""),
                                               prepara_rientro=request.form.get("rientro") == "1")
        if rientro:
            flash(f"DDT di rientro n. {rientro.sigla} pronto: la concessionaria deve solo venire a ritirare.",
                  "ok")
    return _azione_ddt(ddt_id, completa, "Lavorazione completata: la concessionaria la vede “da ritirare”.")


@bp.route("/ddt/<int:ddt_id>/annulla", methods=["POST"])
@login_richiesto
def ddt_annulla(ddt_id):
    """L'amministratore elimina il DDT del tutto (motivo facoltativo, non salvato)."""
    documento = _ddt_visibile(ddt_id)
    sigla, veicolo_id = documento.sigla, documento.movimento.veicolo_id
    try:
        servizi.elimina_ddt(documento, g.utente)
        db.session.commit()
    except Vietato as e:
        db.session.rollback()
        flash(str(e), "errore")
        return _torna_al_ddt(documento)
    flash(f"DDT {sigla} eliminato.", "ok")
    return redirect(url_for("main.veicolo", veicolo_id=veicolo_id))


@bp.route("/ddt/<int:ddt_id>/anteprima")
@login_richiesto
def ddt_anteprima(ddt_id):
    """Il DDT come foglio da leggere sul telefono; da qui si scarica il PDF o lo si condivide."""
    d = _ddt_visibile(ddt_id)
    mittente, destinatario, vettore = servizi.dati_ddt(d)
    return render_template("ddt_anteprima.html", d=d, m=d.movimento, mittente=mittente,
                           destinatario=destinatario, vettore=vettore)


@bp.route("/ddt/<int:ddt_id>.pdf")
@login_richiesto
def ddt_pdf(ddt_id):
    documento = _ddt_visibile(ddt_id)
    nome = f"DDT_{documento.numero}_{documento.anno}.pdf"
    return Response(ddt_pdf_mod.pdf(documento), mimetype="application/pdf",
                    headers={"Content-Disposition": f'{"attachment" if request.args.get("scarica") else "inline"}; '
                                                    f'filename="{nome}"'})


# ---------------------------------------------------------------- firma fornitore

@bp.route("/mia-firma", methods=["GET", "POST"])
@login_richiesto
def mia_firma():
    """Il fornitore imposta la firma predefinita della sua sede (usata come mittente e destinatario)."""
    sede = g.utente.sede
    if sede is None:
        abort(403)
    if request.method == "POST":
        firma = servizi.firma_valida(request.form.get("firma"))
        if not firma:
            flash("Firma non valida: disegnala nel riquadro.", "errore")
        else:
            sede.firma = firma
            db.session.commit()
            flash(f"Firma di {sede.nome} salvata: vale per tutti i suoi utenti.", "ok")
            return redirect(url_for("main.profilo"))
    return render_template("mia_firma.html", sede=sede)


@bp.route("/profilo")
@login_richiesto
def profilo():
    """Il mio profilo: chi sono, profilo di guida con la firma da conducente, password e, per i
    fornitori, la firma della loro azienda. Tutto ciò che riguarda me in un posto solo."""
    u = g.utente
    guida = {"vettore": u.vettore.nome if u.vettore else None, "mezzo": u.mezzo or None, "firma": bool(u.firma)}
    guida["mancano"] = [n for n, ok in (("vettore", guida["vettore"]), ("modalità di trasporto", guida["mezzo"]),
                                         ("firma", guida["firma"])) if not ok]
    return render_template("profilo.html", u=u, guida=guida)


# ---------------------------------------------------------------- assistente IA

@bp.route("/api/assistente/ddt", methods=["POST"])
@login_richiesto
def assistente_ddt():
    """Frase detta o scritta -> campi del modulo di partenza già compilati."""
    dati = request.get_json(silent=True) or {}
    v = db.session.get(Veicolo, int(dati.get("veicolo_id") or 0))
    if v is None or not servizi.puo_emettere(v, g.utente):
        return jsonify(errore="Non puoi emettere un DDT per questa auto."), 403
    try:
        return jsonify(campi=ia.compila_ddt(dati.get("testo", ""), v, g.utente))
    except ia.ErroreIA as e:
        return jsonify(errore=str(e)), 503


@bp.route("/api/assistente/chat", methods=["POST"])
@solo_interni
def assistente_chat():
    dati = request.get_json(silent=True) or {}
    try:
        risposta, ids = ia.chat(dati.get("messaggi") or [], g.utente)
    except ia.ErroreIA as e:
        return jsonify(errore=str(e)), 503
    veicoli = []
    for vid in ids:
        v = db.session.get(Veicolo, vid)
        if v:
            veicoli.append({"descrizione": v.descrizione, "vin": v.vin[-8:],
                            "url": url_for("main.veicolo", veicolo_id=v.id),
                            "mappa": (f"https://www.google.com/maps/dir/?api=1&destination={v.lat},{v.lon}"
                                      "&travelmode=walking") if v.lat is not None and v.stato != "consegnato" else None})
    return jsonify(risposta=risposta, veicoli=veicoli)


# ---------------------------------------------------------------- danni

TIPI_FOTO = {b"\xff\xd8\xff": "jpg", b"\x89PNG": "png", b"RIFF": "webp"}


def _salva_foto(files):
    """Salva le foto caricate (JPEG/PNG/WebP, già ridotte dal telefono). Restituisce i nomi."""
    nomi = []
    for f in files:
        if not f or not f.filename:
            continue
        dati = f.read()
        estensione = next((e for firma, e in TIPI_FOTO.items() if dati.startswith(firma)), None)
        if estensione is None or len(dati) > 12 * 1024 * 1024:
            raise Vietato("Le foto devono essere immagini JPEG, PNG o WebP.")
        nome = f"{uuid.uuid4().hex}.{estensione}"
        with open(os.path.join(current_app.config["FOTO_DIR"], nome), "wb") as uscita:
            uscita.write(dati)
        nomi.append(nome)
    return nomi


@bp.route("/danni")
@solo_interni
def danni():
    vista = request.args.get("vista", "aperti")
    base = Danno.query.filter(Danno.rimosso.is_(False))
    aperti = base.filter(Danno.risolto_at.is_(None)).order_by(Danno.segnalato_at.desc()).all()
    risolti = base.filter(Danno.risolto_at.isnot(None)).order_by(Danno.risolto_at.desc()).limit(100).all()
    return render_template("danni.html", vista="risolti" if vista == "risolti" else "aperti",
                           aperti=aperti, risolti=risolti)


@bp.route("/veicolo/<int:veicolo_id>/danno", methods=["GET", "POST"])
@login_richiesto
def nuovo_danno(veicolo_id):
    v = _veicolo_visibile(veicolo_id)
    mov = db.session.get(Movimento, request.values.get("movimento", type=int) or 0)
    if mov is not None and mov.veicolo_id != v.id:
        mov = None
    if request.method == "POST":
        da_script = request.headers.get("X-Dovin") == "1"  # invio con foto ridotte dal telefono
        try:
            foto = _salva_foto(request.files.getlist("foto"))
            servizi.segnala_danno(v, g.utente, request.form.get("zona", ""), request.form.get("descrizione", ""),
                                  foto, movimento=mov)
            db.session.commit()
            flash("Danno segnalato: compare come avviso nella panoramica.", "ok")
            vai = url_for("main.veicolo", veicolo_id=v.id) if g.utente.interno else url_for("main.home")
            return jsonify(vai=vai) if da_script else redirect(vai)
        except Vietato as e:
            db.session.rollback()
            if da_script:
                return jsonify(errore=str(e)), 400
            flash(str(e), "errore")
    return render_template("danno_nuovo.html", v=v, mov=mov, zone=Danno.ZONE)


@bp.route("/danno/<int:danno_id>")
@login_richiesto
def danno(danno_id):
    """Scheda completa del danno: le foto si vedono solo qui, non negli elenchi."""
    d = db.get_or_404(Danno, danno_id)
    if d.rimosso or not servizi.puo_vedere_veicolo(d.veicolo, g.utente):
        abort(404)
    return render_template("danno.html", d=d, torna=request.args.get("torna") or url_for("main.danni"))


@bp.route("/danno/<int:danno_id>/risolvi", methods=["POST"])
@solo_interni
def risolvi_danno(danno_id):
    danno = db.get_or_404(Danno, danno_id)
    da_script = request.headers.get("X-Dovin") == "1"  # invio con foto ridotte dal telefono
    vai = request.form.get("torna") or url_for("main.danni")
    try:
        foto = _salva_foto(request.files.getlist("foto"))
        servizi.risolvi_danno(danno, g.utente, request.form.get("nota", ""), foto)
        db.session.commit()
        flash("Danno risolto: la foto di conferma è nella scheda del danno.", "ok")
    except Vietato as e:
        db.session.rollback()
        if da_script:
            return jsonify(errore=str(e)), 400
        flash(str(e), "errore")
        vai = url_for("main.danno", danno_id=danno.id)
    return jsonify(vai=vai) if da_script else redirect(vai)


@bp.route("/marchi/<path:nome>")
@login_richiesto
def logo_marca(nome):
    """Loghi in instance/marchi: caricati in Gestione (ufficiali/) o scaricati per le marche nuove."""
    risposta = send_from_directory(os.path.join(current_app.instance_path, "marchi"), nome, max_age=86400)
    risposta.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"  # SVG inerti
    return risposta


@bp.route("/foto/<nome>")
@login_richiesto
def foto(nome):
    f = DannoFoto.query.filter_by(file=nome).first_or_404()
    if not servizi.puo_vedere_veicolo(f.danno.veicolo, g.utente):
        abort(403)
    return send_from_directory(current_app.config["FOTO_DIR"], f.file, max_age=86400)


@bp.route("/veicolo/<int:veicolo_id>/elimina", methods=["POST"])
@solo_interni
def elimina_veicolo(veicolo_id):
    v = db.get_or_404(Veicolo, veicolo_id)
    nome = v.descrizione
    try:
        foto = servizi.elimina_veicolo(v, g.utente, demo=current_app.config["DEMO"])
        db.session.commit()
    except Vietato as e:
        db.session.rollback()
        flash(str(e), "errore")
        return redirect(url_for("main.veicolo", veicolo_id=veicolo_id))
    for f in foto:
        try:
            os.remove(os.path.join(current_app.config["FOTO_DIR"], f))
        except OSError:
            pass
    flash(f"{nome} eliminata.", "ok")
    return redirect(url_for("main.piazzale"))


# ---------------------------------------------------------------- correzioni dell'admin

@bp.route("/azione/<int:azione_id>/ripristina", methods=["POST"])
@login_richiesto
def ripristina_azione(azione_id):
    azione = db.get_or_404(Azione, azione_id)
    try:
        servizi.ripristina(azione, g.utente)
        db.session.commit()
        flash(f"Fatto: “{azione.nome}” di {azione.utente.nome} è stato annullato.", "ok")
    except Vietato as e:
        db.session.rollback()
        flash(str(e), "errore")
    return redirect(request.form.get("torna") or url_for("main.home"))
