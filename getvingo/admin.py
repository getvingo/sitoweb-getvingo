from collections import Counter
import csv
import io
import re
import secrets

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, url_for

from . import accessi, anagrafica, posta, servizi, uniforma
from . import vin as vin_mod
from .auth import ruolo_richiesto
from datetime import datetime, timedelta

from .models import AccessoSupporto, Azienda, CatalogoAuto, GiornoChiuso, Sede, Tag, Utente, Veicolo, Vettore, adesso, db

bp = Blueprint("admin", __name__, url_prefix="/admin")



def _testo(campo):
    return request.form.get(campo, "").strip()


@bp.route("/")
@ruolo_richiesto("admin")
def indice():
    return render_template("admin/indice.html")


@bp.route("/marchi", methods=["GET", "POST"])
@ruolo_richiesto("admin")
def marchi_auto():
    """Loghi delle marche presenti: si vede da dove arriva ciascuno e si carica il file ufficiale della casa."""
    from . import marchi
    if request.method == "POST":
        marca = _testo("marca")
        if request.form.get("azione") == "rimuovi":
            marchi.rimuovi_ufficiale(marca)
            flash(f"{marca}: tolto il file caricato.", "ok")
        else:
            file = request.files.get("file")
            try:
                if not file or not file.filename:
                    raise ValueError("Scegli il file del logo.")
                marchi.salva_ufficiale(marca, file.read(), file.filename)
                flash(f"{marca}: logo ufficiale caricato.", "ok")
            except ValueError as e:
                flash(str(e), "errore")
        if request.form.get("torna") == "catalogo":
            return redirect(url_for("admin.catalogo") + "#loghi")
        return redirect(url_for("admin.marchi_auto"))
    elenco = []
    for marca in sorted({v.marca.strip() for v in Veicolo.query if v.marca and v.marca.strip()}, key=str.lower):
        fonte, indirizzo = marchi.trova(marca)
        elenco.append({"marca": marca, "url": indirizzo, "fonte": fonte,
                       "nome_fonte": marchi.NOMI_FONTE.get(fonte, "Nessun logo · si vedono le iniziali"),
                       "ufficiale": fonte in ("caricato", "sito")})
    return render_template("admin/marchi.html", elenco=elenco)


@bp.route("/azienda", methods=["GET", "POST"])
@ruolo_richiesto("admin")
def azienda():
    """Dati aziendali: ragione sociale, indirizzo, P. IVA e firma del mittente sui DDT; da qui anche
    giorni di chiusura e loghi delle marche (impostazioni della concessionaria)."""
    a = Azienda.query.first() or Azienda(ragione_sociale="")
    if request.method == "POST":
        org = g.utente.organizzazione
        try:
            sigla = servizi.sigla_valida(_testo("sigla") or org.sigla or servizi.genera_sigla(org.nome), org.id)
        except servizi.Vietato as e:
            sigla = None
            flash(str(e), "errore")
        errore = None
        if sigla:
            try:
                if not _testo("ragione_sociale"):
                    raise servizi.Vietato("La ragione sociale è obbligatoria.")
                a.ragione_sociale = _testo("ragione_sociale")
                a.email_ddt = _email_ddt()
                a.invia_copie_ddt = request.form.get("invia_copie_ddt") == "1"
                anagrafica.leggi_fiscali(a)
                anagrafica.leggi_indirizzo(a)
            except servizi.Vietato as e:
                errore = str(e)
        if errore:
            db.session.rollback()
            flash(errore, "errore")
        elif sigla:
            firma = servizi.firma_valida(request.form.get("firma"))
            if firma:
                a.firma = firma
            timbro = servizi.timbro_valido(request.form.get("timbro"))
            if timbro:
                a.timbro = timbro
            org.sigla = sigla
            db.session.add(a)
            servizi.allinea_vettore_proprio(a)
            servizi.allinea_sede_legale(a)
            db.session.commit()
            flash("Dati aziendali salvati: compaiono sui prossimi DDT (quelli già emessi non cambiano). "
                  "La sede legale è anche tra le Sedi e piazzali.", "ok")
            return redirect(url_for("admin.azienda"))
    oggi = adesso().date()
    chiusure = GiornoChiuso.query.filter(GiornoChiuso.data >= oggi).order_by(GiornoChiuso.data).all()
    marche = {v.marca.strip().lower() for v in Veicolo.query if v.marca and v.marca.strip()}
    org = g.utente.organizzazione
    return render_template("admin/azienda.html", a=a, chiusure=chiusure, n_marche=len(marche),
                           sigla=org.sigla or servizi.genera_sigla(org.nome),
                           adesivi=Tag.query.count())


@bp.route("/chiusure", methods=["GET", "POST"])
@ruolo_richiesto("admin")
def chiusure():
    """Giorni chiusi oltre alle feste nazionali (patrono, ferie, ponti): il calendario delle
    preparazioni li salta."""
    if request.method == "POST":
        try:
            dal = datetime.strptime(request.form.get("dal", ""), "%Y-%m-%d").date()
            al = datetime.strptime(request.form.get("al") or request.form.get("dal", ""), "%Y-%m-%d").date()
        except ValueError:
            flash("Indica il giorno.", "errore")
            return redirect(url_for("admin.chiusure"))
        if al < dal or (al - dal).days > 60:
            flash("Il periodo va da un giorno a un altro successivo, al massimo 60 giorni.", "errore")
            return redirect(url_for("admin.chiusure"))
        gia = {gc.data for gc in GiornoChiuso.query.filter(GiornoChiuso.data >= dal, GiornoChiuso.data <= al)}
        nuovi = [dal + timedelta(days=n) for n in range((al - dal).days + 1)]
        db.session.add_all(GiornoChiuso(data=d, motivo=_testo("motivo")[:120]) for d in nuovi if d not in gia)
        db.session.commit()
        flash(f"{len(nuovi)} {'giorno chiuso' if len(nuovi) == 1 else 'giorni chiusi'} nel calendario.", "ok")
        return redirect(url_for("admin.chiusure"))
    oggi = adesso().date()
    feste = []
    for n in range(366):
        giorno = oggi + timedelta(days=n)
        nome = servizi.festivita(giorno)
        if nome:
            feste.append((giorno, nome))
    chiusi = GiornoChiuso.query.filter(GiornoChiuso.data >= oggi).order_by(GiornoChiuso.data).all()
    return render_template("admin/chiusure.html", chiusi=chiusi, feste=feste)


@bp.route("/chiusure/<int:chiuso_id>/elimina", methods=["POST"])
@ruolo_richiesto("admin")
def elimina_chiusura(chiuso_id):
    gc = db.get_or_404(GiornoChiuso, chiuso_id)
    db.session.delete(gc)
    db.session.commit()
    flash(f"{gc.data:%d/%m/%Y} di nuovo lavorativo.", "ok")
    return redirect(url_for("admin.chiusure"))


GRUPPI_LUOGHI = {
    "proprie": ("Sedi e piazzali", "I luoghi della concessionaria: sede legale, sedi e piazzali dove stanno le auto.",
                ("propria",)),
    "esterne": ("Destinazioni esterne", "Partner lavorazioni, allestitori, altri concessionari, esposizioni e clienti: dove le auto vanno e da dove tornano.",
                ("carrozzeria", "officina", "allestitore", "altro", "concessionario", "rivenditore", "esposizione", "cliente")),
}
SEZIONI_ESTERNE = [("Partner lavorazioni", ("carrozzeria", "officina", "altro")),
                   ("Allestitori (veicoli commerciali)", ("allestitore",)),
                   ("Concessionari", ("concessionario",)), ("Rivenditori", ("rivenditore",)),
                   ("Esposizioni", ("esposizione",)), ("Clienti", ("cliente",))]


def _luogo_dal_modulo(s):
    """Nome, tipo, indirizzo verificato sulla mappa e dati fiscali (obbligatori per le società,
    solo codice fiscale per i clienti, nessuno per le sedi della concessionaria)."""
    if not _testo("nome"):
        raise servizi.Vietato("Il nome è obbligatorio.")
    tipo = request.form.get("tipo") if request.form.get("tipo") in Sede.TIPI else (s.tipo or "altro")
    if s.sede_legale:
        tipo = "propria"  # la sede legale resta della concessionaria
    s.nome, s.tipo = _testo("nome"), tipo
    anagrafica.leggi_indirizzo(s)
    if tipo in ("propria", "esposizione"):  # 22/11/2026: posti auto (Home: vetture su posti)
        posti = request.form.get("posti", "").strip()
        s.posti = int(posti) if posti.isdigit() and 0 < int(posti) <= 100000 else None
    if tipo == "propria":
        s.piva = s.codice_fiscale = ""
    else:
        anagrafica.leggi_fiscali(s, obbligatori=True, solo_cf=tipo == "cliente")


@bp.route("/sedi", methods=["GET", "POST"])
@ruolo_richiesto("admin")
def sedi():
    gruppo = request.values.get("gruppo") if request.values.get("gruppo") in GRUPPI_LUOGHI else "proprie"
    titolo, descrizione, tipi_gruppo = GRUPPI_LUOGHI[gruppo]
    if request.method == "POST":
        nuova = Sede(tipo=tipi_gruppo[0])
        try:
            _luogo_dal_modulo(nuova)
        except servizi.Vietato as e:
            flash(str(e), "errore")
            return render_template("admin/sedi.html", gruppo=gruppo, titolo=titolo, descrizione=descrizione,
                                   tipi={k: Sede.TIPI[k] for k in tipi_gruppo}, sezioni=_sezioni(gruppo),
                                   nuova=nuova, aperto=True)
        db.session.add(nuova)
        db.session.commit()
        flash(f"{nuova.nome} aggiunto, con la sua posizione sulla mappa.", "ok")
        return redirect(url_for("admin.sedi", gruppo=gruppo))
    return render_template("admin/sedi.html", gruppo=gruppo, titolo=titolo, descrizione=descrizione,
                           tipi={k: Sede.TIPI[k] for k in tipi_gruppo}, sezioni=_sezioni(gruppo), nuova=None,
                           aperto=False)


@bp.route("/destinazioni")
@ruolo_richiesto("admin")
def sedi_esterne():
    return redirect(url_for("admin.sedi", gruppo="esterne"))


def _sezioni(gruppo):
    tutte = [x for x in Sede.query.order_by(Sede.nome) if not x.eliminata]
    if gruppo == "proprie":
        return [("", [x for x in tutte if x.tipo == "propria"])]
    return [(t, [x for x in tutte if x.tipo in tipi]) for t, tipi in SEZIONI_ESTERNE]


@bp.route("/sedi/<int:sede_id>", methods=["GET", "POST"])
@ruolo_richiesto("admin")
def sede(sede_id):
    s = db.get_or_404(Sede, sede_id)
    gruppo = "proprie" if s.tipo == "propria" else "esterne"
    if request.method == "POST":
        try:
            _luogo_dal_modulo(s)
        except servizi.Vietato as e:
            db.session.rollback()
            flash(str(e), "errore")
            return redirect(url_for("admin.sede", sede_id=s.id))
        firma = servizi.firma_valida(request.form.get("firma"))
        if firma and s.tipo != "propria":
            s.firma = firma
        timbro = servizi.timbro_valido(request.form.get("timbro"))
        if timbro and s.tipo != "propria":
            s.timbro = timbro
        if s.tipo != "propria":
            try:
                s.email_ddt = _email_ddt()
            except servizi.Vietato as e:
                db.session.rollback()
                flash(str(e), "errore")
                return redirect(url_for("admin.sede", sede_id=s.id))
        db.session.commit()
        flash("Luogo aggiornato. I DDT già emessi restano con i dati di quando sono stati emessi.", "ok")
        return redirect(url_for("admin.sedi", gruppo="proprie" if s.tipo == "propria" else "esterne"))
    tipi = {k: Sede.TIPI[k] for k in GRUPPI_LUOGHI[gruppo][2]}
    return render_template("admin/sede.html", s=s, tipi=tipi, gruppo=gruppo,
                           ddt_collegati=servizi.ddt_collegati(s) if s.tipo != "propria" else None)


@bp.route("/sedi/<int:sede_id>/elimina", methods=["POST"])
@ruolo_richiesto("admin")
def elimina_sede(sede_id):
    """Destinazione esterna (fornitore, cliente…) senza DDT collegati: l'amministratore la elimina."""
    s = db.get_or_404(Sede, sede_id)
    try:
        if s.tipo == "propria":
            raise servizi.Vietato("Le sedi della concessionaria non si eliminano: si disattivano.")
        if servizi.ddt_collegati(s):
            raise servizi.Vietato(f"{s.nome} ha DDT collegati: non si può eliminare, solo disattivare.")
        nome = s.nome
        servizi.elimina_fornitore(s, g.utente)
        db.session.commit()
    except servizi.Vietato as e:
        db.session.rollback()
        flash(str(e), "errore")
        return redirect(url_for("admin.sede", sede_id=sede_id))
    flash(f"{nome} eliminato, con i suoi account.", "ok")
    return redirect(url_for("admin.sedi", gruppo="esterne"))


def _email_ddt():
    """Email (facoltativa) a cui mandare la copia del DDT firmato."""
    email = (request.form.get("email_ddt") or "").strip().lower()[:200]
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", email):
        raise servizi.Vietato(f"L'email per i DDT “{email}” non sembra valida.")
    return email


@bp.route("/vettori", methods=["GET", "POST"])
@ruolo_richiesto("admin")
def vettori():
    nuovo = None
    if request.method == "POST":
        nuovo = Vettore(nome=_testo("nome"))
        try:
            if not nuovo.nome:
                raise servizi.Vietato("Il nome del vettore è obbligatorio.")
            anagrafica.leggi_fiscali(nuovo)
            anagrafica.leggi_indirizzo(nuovo)
            nuovo.firma = servizi.firma_valida(request.form.get("firma"))
        except servizi.Vietato as e:
            flash(str(e), "errore")
        else:
            db.session.add(nuovo)
            db.session.commit()
            flash("Vettore aggiunto.", "ok")
            return redirect(url_for("admin.vettori"))
    servizi.vettore_proprio()
    db.session.commit()
    return render_template("admin/vettori.html", vettori=Vettore.query.order_by(Vettore.proprio.desc(), Vettore.nome).all(),
                           nuovo=nuovo)


@bp.route("/attiva/<tipo>/<int:oggetto_id>", methods=["POST"])
@ruolo_richiesto("admin")
def attiva(tipo, oggetto_id):
    """Attiva/disattiva sedi e vettori (non si cancellano: sono nei DDT emessi)."""
    modello, campo, pagina = {"sede": (Sede, "attiva", "admin.sedi"),
                              "vettore": (Vettore, "attivo", "admin.vettori")}[tipo]
    oggetto = db.get_or_404(modello, oggetto_id)
    setattr(oggetto, campo, not getattr(oggetto, campo))
    db.session.commit()
    if tipo == "sede":
        return redirect(url_for(pagina, gruppo="proprie" if oggetto.tipo == "propria" else "esterne"))
    return redirect(url_for(pagina))


@bp.route("/assistenza", methods=["GET", "POST"])
@ruolo_richiesto("admin")
def assistenza():
    """Il cliente decide se il gestore Getvingo può entrare per assistenza e vede ogni suo ingresso."""
    org = g.utente.organizzazione
    if request.method == "POST":
        if g.utente.supporto:
            flash("Il consenso all'assistenza lo decide solo un amministratore della concessionaria.", "errore")
        else:
            org.assistenza_consentita = request.form.get("consenti") == "1"
            org.assistenza_cambiata_at = adesso()
            db.session.commit()
            flash("Assistenza Getvingo consentita: potrà entrare finché non la togli." if org.assistenza_consentita
                  else "Assistenza Getvingo non consentita: da ora non può entrare.", "ok")
        return redirect(url_for("admin.assistenza"))
    accessi_supporto = AccessoSupporto.query.order_by(AccessoSupporto.at.desc()).limit(100).all()
    return render_template("admin/assistenza.html", org=org, accessi_supporto=accessi_supporto)


@bp.route("/utenti")
@ruolo_richiesto("admin")
def utenti():
    return redirect(url_for("admin.aziende"))


# ---------------------------------------------------------------- aziende = gruppi di utenti
# Ogni azienda ha i suoi dati e una sola firma, condivisi da tutti i suoi utenti:
# la concessionaria (admin, ufficio, autisti) e ogni fornitore esterno.

RUOLI_INTERNI = {k: Utente.RUOLI[k] for k in Utente.RUOLI_INTERNI}


def _crea_utente(ruolo, sede_id=None):
    """Nuovo utente del gruppo. Senza password riceve l'invito per sceglierla da sé (consigliato):
    ritorna la pagina con l'esito dell'invito; altrimenti None (si torna al gruppo)."""
    email = _testo("email").lower()
    password = request.form.get("password", "")
    if not (_testo("nome") and email):
        flash("Nome ed email sono obbligatori.", "errore")
        return None
    if password and len(password) < 8:
        flash("La password deve avere almeno 8 caratteri (oppure lasciala vuota e mandiamo l'invito).", "errore")
        return None
    if Utente.query.filter_by(email=email).execution_options(tutte_le_organizzazioni=True).first():
        flash("Email già registrata.", "errore")
        return None
    if password:
        u = Utente(nome=_testo("nome"), email=email, ruolo=ruolo, sede_id=sede_id)
        u.imposta_password(password)
        db.session.add(u)
        db.session.commit()
        flash(f"{u.nome} aggiunto: comunicagli tu la password, potrà cambiarla dal suo menu.", "ok")
        return None
    u = accessi.nuovo_utente_invitato(_testo("nome"), email, ruolo, sede_id)
    u.invito_in_attesa = True
    db.session.commit()
    return _esito_link(u, "invito")


def _esito_link(u, scopo):
    """Manda il link (invito o nuova password) e mostra cosa è successo, con il link da copiare
    se l'email non è partita o non è configurata."""
    errore = None
    if scopo == "invito":
        link, inviata, errore = accessi.invia_invito(u, invitato_da=g.utente.nome)
    else:
        link, inviata = accessi.crea_link(u, "recupero"), False
        if posta.configurata():
            try:
                accessi.invia_recupero(u)
                inviata = True
            except posta.ErrorePosta as e:
                errore = str(e)
    torna = url_for("admin.gruppo_fornitore", sede_id=u.sede_id) if u.ruolo == "fornitore" and u.sede_id \
        else url_for("admin.gruppo_concessionaria")
    return render_template("admin/invito.html", u=u, scopo=scopo, link=link, inviata=inviata, errore=errore,
                           torna=torna, durata_ore=accessi.DURATA[scopo] // 3600)


@bp.route("/aziende")
@ruolo_richiesto("admin")
def aziende():
    fornitori = [s for s in Sede.query.order_by(Sede.nome) if s.esterna and not s.eliminata]
    return render_template("admin/aziende.html", azienda=Azienda.query.first(), fornitori=fornitori,
                           interni=[u for u in Utente.query.all() if u.interno and not u.supporto and not u.eliminato])


@bp.route("/aziende/concessionaria", methods=["GET", "POST"])
@ruolo_richiesto("admin")
def gruppo_concessionaria():
    a = Azienda.query.first() or Azienda(ragione_sociale="")
    if request.method == "POST":
        if request.form.get("azione") == "utente":
            ruolo = request.form.get("ruolo") if request.form.get("ruolo") in RUOLI_INTERNI else "backoffice"
            esito = _crea_utente(ruolo)
            if esito is not None:
                return esito
        else:
            a.ragione_sociale, a.indirizzo, a.piva = _testo("ragione_sociale"), _testo("indirizzo"), _testo("piva")
            firma = servizi.firma_valida(request.form.get("firma"))
            if firma:
                a.firma = firma
            timbro = servizi.timbro_valido(request.form.get("timbro"))
            if timbro:
                a.timbro = timbro
            db.session.add(a)
            db.session.commit()
            flash("Dati e firma della concessionaria salvati: valgono per tutti i suoi utenti.", "ok")
        return redirect(url_for("admin.gruppo_concessionaria"))
    utenti = sorted((u for u in Utente.query.all() if u.interno and not u.eliminato), key=lambda u: (u.supporto, u.nome))
    return render_template("admin/gruppo.html", tipo="concessionaria", nome=a.ragione_sociale or "Concessionaria",
                           dati=a, utenti=utenti, ruoli=RUOLI_INTERNI)


@bp.route("/aziende/fornitore/<int:sede_id>", methods=["GET", "POST"])
@ruolo_richiesto("admin")
def gruppo_fornitore(sede_id):
    sede = db.get_or_404(Sede, sede_id)
    if not sede.esterna:
        return redirect(url_for("admin.gruppo_concessionaria"))
    if request.method == "POST" and request.form.get("azione") == "elimina_fornitore":
        nome = sede.nome
        try:
            del_tutto = servizi.elimina_fornitore(sede, g.utente)
            db.session.commit()
        except servizi.Vietato as e:
            db.session.rollback()
            flash(str(e), "errore")
            return redirect(url_for("admin.gruppo_fornitore", sede_id=sede.id))
        flash(f"{nome} eliminato con i suoi account." + ("" if del_tutto else
              " Resta solo sui DDT già emessi."), "ok")
        return redirect(url_for("admin.aziende"))
    if request.method == "POST":
        if request.form.get("azione") == "utente":
            esito = _crea_utente("fornitore", sede.id)
            if esito is not None:
                return esito
        else:
            try:
                sede.nome = _testo("ragione_sociale") or sede.nome
                anagrafica.leggi_fiscali(sede)
                anagrafica.leggi_indirizzo(sede)
            except servizi.Vietato as e:
                db.session.rollback()
                flash(str(e), "errore")
                return redirect(url_for("admin.gruppo_fornitore", sede_id=sede.id))
            firma = servizi.firma_valida(request.form.get("firma"))
            if firma:
                sede.firma = firma
            timbro = servizi.timbro_valido(request.form.get("timbro"))
            if timbro:
                sede.timbro = timbro
            db.session.commit()
            flash(f"Dati e firma di {sede.nome} salvati: valgono per tutti i suoi utenti.", "ok")
        return redirect(url_for("admin.gruppo_fornitore", sede_id=sede.id))
    return render_template("admin/gruppo.html", tipo="fornitore", nome=sede.nome, dati=sede,
                           utenti=sorted((u for u in sede.utenti if not u.eliminato), key=lambda u: u.nome),
                           ruoli={"fornitore": "Partner lavorazioni"})


@bp.route("/utenti/<int:utente_id>", methods=["GET", "POST"])
@ruolo_richiesto("admin")
def utente(utente_id):
    u = db.get_or_404(Utente, utente_id)
    if u.eliminato:
        return redirect(url_for("admin.aziende"))
    if u.supporto:  # l'accesso del gestore Getvingo non si modifica né si usa dalla concessionaria
        flash("“Supporto Getvingo” è l'accesso del gestore della piattaforma: non si modifica da qui.", "errore")
        return redirect(url_for("admin.gruppo_concessionaria"))
    torna = url_for("admin.gruppo_fornitore", sede_id=u.sede_id) if u.ruolo == "fornitore" and u.sede_id \
        else url_for("admin.gruppo_concessionaria")
    if request.method == "POST" and request.form.get("azione") == "genera_password":
        if u.id == g.utente.id or not u.attivo:
            flash("Per il tuo account usa Cambia password; un account disattivato va prima riattivato.", "errore")
            return redirect(url_for("admin.utente", utente_id=u.id))
        # leggibile a voce: niente 0/O, 1/l/I
        alfabeto = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        nuova = "".join(secrets.choice(alfabeto) for _ in range(10))
        u.imposta_password(nuova)
        u.invito_in_attesa = False
        db.session.commit()  # gli altri dispositivi di quell'account si scollegano (impronta cambiata)
        return render_template("admin/password_generata.html", u=u, password=nuova, torna=torna)
    if request.method == "POST" and request.form.get("azione") == "elimina":
        nome = u.nome
        try:
            del_tutto = servizi.elimina_utente(u, g.utente)
            db.session.commit()
        except servizi.Vietato as e:
            db.session.rollback()
            flash(str(e), "errore")
            return redirect(url_for("admin.utente", utente_id=u.id))
        flash(f"Account di {nome} eliminato." + ("" if del_tutto else
              " Il nome resta sui DDT e nella cronologia già registrati; l'email torna libera."), "ok")
        return redirect(torna)
    if request.method == "POST" and request.form.get("azione") == "link":
        if not u.attivo:
            flash("L'account è disattivato: riattivalo prima di mandare il link.", "errore")
            return redirect(url_for("admin.utente", utente_id=u.id))
        return _esito_link(u, "invito" if u.invito_in_attesa else "recupero")
    if request.method == "POST":
        email = _testo("email").lower()
        if not _testo("nome") or not email:
            flash("Nome ed email sono obbligatori.", "errore")
        elif Utente.query.filter(Utente.email == email, Utente.id != u.id) \
                .execution_options(tutte_le_organizzazioni=True).first():
            flash("Email già usata da un altro utente.", "errore")
        elif request.form.get("password") and len(request.form["password"]) < 8:
            flash("La password deve avere almeno 8 caratteri.", "errore")
        else:
            u.nome, u.email = _testo("nome"), email
            if u.ruolo != "fornitore" and request.form.get("ruolo") in RUOLI_INTERNI and u.id != g.utente.id:
                u.ruolo = request.form["ruolo"]
            if u.ruolo == "fornitore" and request.form.get("sede_id", type=int):
                nuova = db.session.get(Sede, request.form.get("sede_id", type=int))
                if nuova is not None and nuova.esterna:
                    u.sede = nuova
            if u.id != g.utente.id:
                u.attivo = request.form.get("attivo") == "1"
            if request.form.get("password"):
                u.imposta_password(request.form["password"])
                u.invito_in_attesa = False
            db.session.commit()
            flash("Utente aggiornato.", "ok")
            return redirect(torna)
    return render_template("admin/utente.html", u=u, ruoli=RUOLI_INTERNI, torna=torna, posta_attiva=posta.configurata(),
                           fornitori=[s for s in Sede.query.order_by(Sede.nome) if s.esterna])


@bp.route("/vettori/<int:vettore_id>", methods=["GET", "POST"])
@ruolo_richiesto("admin")
def vettore(vettore_id):
    vt = db.get_or_404(Vettore, vettore_id)
    if vt.proprio:  # segue i Dati aziendali: si cambia da lì
        return redirect(url_for("admin.azienda"))
    if request.method == "POST":
        try:
            if not _testo("nome"):
                raise servizi.Vietato("Il nome è obbligatorio.")
            vt.nome = _testo("nome")
            anagrafica.leggi_fiscali(vt)
            anagrafica.leggi_indirizzo(vt)
            vt.firma = servizi.firma_valida(request.form.get("firma")) or vt.firma
        except servizi.Vietato as e:
            db.session.rollback()
            flash(str(e), "errore")
            return redirect(url_for("admin.vettore", vettore_id=vt.id))
        vt.attivo = request.form.get("attivo") == "1"
        db.session.commit()
        flash("Vettore aggiornato. I DDT già emessi restano con i dati di quando sono stati emessi.", "ok")
        return redirect(url_for("admin.vettori"))
    return render_template("admin/vettore.html", vt=vt)


def _base_adesivi():
    """Indirizzo del portale da scrivere sugli adesivi (sempre HTTPS fuori dal computer locale)."""
    base = (current_app.config["BASE_URL"] or request.url_root).rstrip("/")
    host = request.host.split(":")[0]
    locale = host in ("127.0.0.1", "localhost") or host.startswith(("192.168.", "10."))
    if not current_app.config["BASE_URL"] and not locale and base.startswith("http://"):
        base = "https://" + base[len("http://"):]
    return base, locale


@bp.route("/adesivi/prepara")
@ruolo_richiesto("admin")
def prepara_adesivi():
    """Prepara adesivi (21/11/2026, solo amministratore, da Android con Chrome): si appoggia un adesivo e il
    portale ci scrive il link del primo codice libero (Web NFC). Gli adesivi così preparati vanno su ogni telefono."""
    base, _ = _base_adesivi()
    ultimi = (Tag.query.filter_by(origine="prepara").order_by(Tag.preparato_at.desc()).limit(20).all())
    return render_template("admin/prepara_adesivi.html", base=base, sigla=g.utente.organizzazione.sigla, ultimi=ultimi)


@bp.route("/adesivi/prenota", methods=["POST"])
@ruolo_richiesto("admin")
def prenota_adesivo():
    """Codice per l'adesivo appoggiato, dal suo numero di serie: lo stesso adesivo riceve sempre lo stesso codice
    (se la scrittura fallisce si riprova senza bruciare numeri); un adesivo nuovo prende il primo codice libero."""
    from flask import jsonify
    from .routes import pulisci_uid
    uid = pulisci_uid((request.get_json(silent=True) or {}).get("seriale"))
    if not uid:
        return jsonify(errore="Numero di serie dell'adesivo non letto: riappoggialo."), 400
    t = Tag.query.filter_by(uid=uid).execution_options(tutte_le_organizzazioni=True).first()
    if t is not None and t.org_id != g.org_id:
        return jsonify(errore="Questo adesivo è registrato da un'altra concessionaria."), 403
    nuovo = t is None
    if nuovo:
        t = servizi.primo_adesivo_libero(uid, "prepara", g.utente)
        db.session.commit()
    base, _ = _base_adesivi()
    return jsonify(codice=t.codice, link=f"{base}/t/{t.codice}", nuovo=nuovo,
                   auto=t.veicolo.descrizione if t.veicolo else "")


@bp.route("/tag", methods=["GET", "POST"])
@ruolo_richiesto("admin")
def tag():
    """Adesivi NFC (21/11/2026): registro di ogni adesivo della concessionaria (come è nato, chi l'ha preparato,
    primo abbinamento, dove si trova). I codici nascono da soli (Prepara adesivi, link unico); "Forza" ne crea uno
    a mano da computer, per scriverlo con un'app NFC."""
    if request.method == "POST":
        codice = servizi.nuovi_codici_adesivi(1)[0]
        db.session.flush()
        t = Tag.query.filter_by(codice=codice).first()
        t.origine, t.preparato_da, t.preparato_at = "manuale", g.utente, adesso()
        db.session.commit()
        flash(f"Codice {codice} creato: scrivi sull'adesivo il link che trovi nella sua riga.", "ok")
        return redirect(url_for("admin.tag", vista="liberi") + "#" + codice)
    base, locale = _base_adesivi()  # sugli adesivi serve sempre HTTPS (il GPS del browser funziona solo così)
    tutti = Tag.query.all()
    tutti.sort(key=lambda t: (len(t.codice), t.codice), reverse=True)
    def vista_di(t):
        if t.trasferito_at:
            return "trasferiti"
        if t.veicolo_id:
            return "auto"
        return "consegnate" if t.ultimo_veicolo_id or t.primo_abbinamento_at else "liberi"
    conta = Counter(vista_di(t) for t in tutti)
    vista = request.args.get("vista", "")
    righe = [t for t in tutti if not vista or vista_di(t) == vista]
    ultime = {t.id: db.session.get(Veicolo, t.ultimo_veicolo_id) for t in righe if t.ultimo_veicolo_id and not t.veicolo_id}
    return render_template("admin/tag.html", base=base, link_locale=locale and not current_app.config["BASE_URL"],
                           righe=righe, conta=conta, totale=len(tutti), vista=vista, vista_di=vista_di, ultime=ultime,
                           sigla=g.utente.organizzazione.sigla)


@bp.route("/uniforma", methods=["GET", "POST"])
@ruolo_richiesto("admin")
def uniforma_auto():
    """Correzione una tantum delle auto già presenti con le regole di scrittura (uniforma.py):
    prima l'elenco di cosa cambierebbe, poi si applica con un pulsante."""
    if request.method == "POST":
        n = uniforma.applica()
        db.session.commit()
        flash(f"{n} {'correzione applicata' if n == 1 else 'correzioni applicate'}: le auto ora sono scritte tutte "
              "allo stesso modo." if n else "Niente da correggere.", "ok")
        return redirect(url_for("admin.uniforma_auto"))
    return render_template("admin/uniforma.html", elenco=uniforma.proposte())


@bp.route("/catalogo", methods=["GET", "POST"])
@ruolo_richiesto("admin")
def catalogo():
    """Catalogo delle vetture (15/11/2026): file CSV o Excel con marca, modello e versione scritti come li vuole
    la concessionaria. Ogni caricamento sostituisce il precedente. È la base per correggere i dati delle auto
    al salvataggio e per le opzioni proposte quando si carica un'auto (uniforma.da_catalogo / opzioni_catalogo)."""
    if request.method == "POST":
        if request.form.get("azione") == "svuota":
            n = CatalogoAuto.query.delete()
            db.session.commit()
            uniforma.svuota_cache_catalogo()
            flash(f"Catalogo svuotato ({n} voci tolte)." if n else "Il catalogo era già vuoto.", "ok")
            return redirect(url_for("admin.catalogo"))
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Scegli un file CSV o Excel (.xlsx).", "errore")
            return redirect(url_for("admin.catalogo"))
        try:
            voci, scartate = uniforma.leggi_file_catalogo(file.filename, file.read())
        except Exception:
            current_app.logger.exception("catalogo: file non leggibile")
            voci, scartate = [], []
        if not voci:
            flash("Nel file non ho trovato voci con marca e modello: controlla le colonne (marca, modello, versione).",
                  "errore")
            return redirect(url_for("admin.catalogo"))
        CatalogoAuto.query.delete()
        db.session.add_all(CatalogoAuto(marca=m, modello=mo, versione=ve) for m, mo, ve in voci)
        db.session.commit()
        uniforma.svuota_cache_catalogo()
        c = uniforma.catalogo()
        flash(f"Catalogo caricato: {len(c['marche'])} marche, {sum(len(x) for x in c['modelli'].values())} modelli, "
              f"{sum(len(x) for x in c['versioni'].values())} versioni.", "ok")
        if scartate:
            flash(f"Righe senza marca o modello, saltate: {', '.join(map(str, scartate[:30]))}"
                  f"{'…' if len(scartate) > 30 else ''}", "errore")
        return redirect(url_for("admin.catalogo"))
    voci = CatalogoAuto.query.order_by(CatalogoAuto.id).all()
    if request.args.get("scarica"):
        out = io.StringIO()
        w = csv.writer(out, delimiter=";")
        w.writerow(["marca", "modello", "versione"])
        w.writerows((v.marca, v.modello, v.versione or "") for v in voci)
        return current_app.response_class("\ufeff" + out.getvalue(), mimetype="text/csv", headers={
            "Content-Disposition": "attachment; filename=catalogo-vetture.csv"})
    albero = {}
    for v in voci:
        albero.setdefault(v.marca, {}).setdefault(v.modello, [])
        if v.versione:
            albero[v.marca][v.modello].append(v.versione)
    n_correzioni = sum(1 for _, campo, *_ in uniforma.proposte() if campo != "colore") if voci else 0
    from . import marchi
    loghi = {}
    marche_loghi = list(albero) or sorted({v.marca.strip() for v in Veicolo.query.with_entities(Veicolo.marca)
                                           if v.marca and v.marca.strip()}, key=str.lower)
    for marca in marche_loghi:
        fonte, indirizzo = marchi.trova(marca)
        loghi[marca] = {"url": indirizzo, "fonte": fonte, "ufficiale": fonte in ("caricato", "sito"),
                        "nome_fonte": marchi.NOMI_FONTE.get(fonte, "Nessun logo · si vedono le iniziali")}
    return render_template("admin/catalogo.html", albero=albero, n_voci=len(voci), n_correzioni=n_correzioni,
                           loghi=loghi)


@bp.route("/catalogo/loghi", methods=["POST"])
@ruolo_richiesto("admin")
def catalogo_loghi():
    """Loghi aggiornati di più marche insieme: ogni file va alla marca del suo nome (volkswagen.svg → Volkswagen),
    cercata tra le marche del catalogo e quelle in stock, senza badare a maiuscole, spazi e trattini."""
    import os
    from . import marchi
    marche = {uniforma.chiave(m): m for m in uniforma.catalogo()["marche"].values()}
    for v in Veicolo.query.with_entities(Veicolo.marca).distinct():
        if v.marca and v.marca.strip():
            marche.setdefault(uniforma.chiave(v.marca), v.marca.strip())
    caricati, senza_marca, errori = [], [], []
    for file in request.files.getlist("loghi"):
        if not file or not file.filename:
            continue
        nome = os.path.splitext(os.path.basename(file.filename))[0]
        k = uniforma.chiave(re.sub(r"(?i)[-_ ]*(logo|marchio|brand)[-_ ]*", " ", nome))
        marca = marche.get(k) or marche.get(uniforma.chiave(nome)) or uniforma.da_catalogo("marca", nome)
        if not marca and uniforma.MARCHE.get(nome.lower().replace("_", " ").replace("-", " ")):
            marca = uniforma.MARCHE[nome.lower().replace("_", " ").replace("-", " ")]
        if not marca:
            senza_marca.append(file.filename)
            continue
        try:
            marchi.salva_ufficiale(marca, file.read(), file.filename)
            caricati.append(marca)
        except ValueError as e:
            errori.append(f"{file.filename}: {e}")
    if caricati:
        flash(f"Loghi caricati: {', '.join(caricati)}.", "ok")
    if senza_marca:
        flash("Non ho capito la marca di: " + ", ".join(senza_marca) + ". Chiama il file col nome della marca "
              "(es. volkswagen.svg) o caricalo dal pulsante accanto alla marca.", "errore")
    for e in errori:
        flash(e, "errore")
    if not (caricati or senza_marca or errori):
        flash("Scegli uno o più file di logo.", "errore")
    return redirect(url_for("admin.catalogo") + "#loghi")


@bp.route("/import", methods=["GET", "POST"])
@ruolo_richiesto("admin")
def importa():
    """Import veicoli da export CSV del gestionale (primo passo di integrazione DMS).
    Colonne: vin, marca, modello, colore (separatore ; o ,)."""
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Seleziona un file CSV.", "errore")
            return redirect(url_for("admin.importa"))
        testo = file.read().decode("utf-8-sig", errors="replace")
        dialetto = csv.Sniffer().sniff(testo.splitlines()[0], delimiters=";,")
        righe = csv.DictReader(io.StringIO(testo), dialect=dialetto)
        nuovi = aggiornati = 0
        scartati = []
        for n, riga in enumerate(righe, start=2):
            riga = {(k or "").strip().lower(): (v or "").strip() for k, v in riga.items()}
            vin = vin_mod.normalizza(riga.get("vin") or riga.get("telaio"))
            if not vin_mod.valido(vin):
                scartati.append(n)
                continue
            veicolo = Veicolo.query.filter_by(vin=vin).first()
            nuovo = veicolo is None
            if veicolo is None:
                veicolo = Veicolo(vin=vin)
                db.session.add(veicolo)
                nuovi += 1
            else:
                aggiornati += 1
            for campo in ("marca", "modello", "versione", "colore"):
                if riga.get(campo):
                    setattr(veicolo, campo, riga[campo])
            alim = (riga.get("alimentazione") or riga.get("carburante") or "").strip().lower()
            for a in Veicolo.ALIMENTAZIONI:  # colonna facoltativa, scritta come si vuole: "benzina", "ELETTRICA"
                if alim == a.lower():
                    veicolo.alimentazione = a
            vin_mod.completa(veicolo, tipologia=nuovo and not (riga.get("tipologia") or riga.get("tipo")))  # dal VIN ciò che manca
            uniforma.auto(veicolo)
            cond = (riga.get("condizione") or riga.get("nuovo/usato") or riga.get("stato") or "").strip().lower()
            if cond in ("nuovo", "nuova", "n"):
                veicolo.condizione = "nuovo"
            elif cond in ("usato", "usata", "u"):
                veicolo.condizione = "usato"
            tipo = (riga.get("tipologia") or riga.get("tipo") or "").lower()
            if tipo.startswith("comm") or tipo in ("n1", "furgone", "veicolo commerciale"):
                veicolo.tipologia = "commerciale"
            elif tipo:
                veicolo.tipologia = "vettura"
        db.session.commit()
        from . import marchi
        for marca in {r.marca for r in Veicolo.query.with_entities(Veicolo.marca).distinct() if r.marca}:
            marchi.assicura(marca)  # loghi delle marche nuove
        flash(f"Import completato: {nuovi} nuovi, {aggiornati} aggiornati.", "ok")
        if scartati:
            flash(f"Righe con VIN non valido, saltate: {', '.join(map(str, scartati[:30]))}", "errore")
        return redirect(url_for("admin.importa"))
    return render_template("admin/import.html")


@bp.route("/svuota", methods=["GET", "POST"])
@ruolo_richiesto("admin")
def svuota():
    """Svuota la concessionaria per ripartire con un test (22/11/2026, richiesta dell'utente; stesso lavoro di
    `flask svuota-stock`, getvingo/svuota.py): auto, DDT e adesivi via, copia di sicurezza in instance/.
    Si conferma scrivendo SVUOTA."""
    import os
    from . import svuota as svuota_mod
    if request.method == "POST":
        if _testo("conferma").upper() != "SVUOTA":
            flash("Niente cancellato: per confermare scrivi SVUOTA.", "errore")
            return redirect(url_for("admin.svuota"))
        cartella = os.path.join(current_app.instance_path, "svuotamenti")
        os.makedirs(cartella, exist_ok=True)
        svuota_mod.esporta(g.org_id, os.path.join(cartella, datetime.now().strftime(f"org{g.org_id}-%Y%m%d-%H%M%S.json")))
        tolte = svuota_mod.svuota(g.org_id)
        current_app.logger.warning("Svuotamento della concessionaria %s da %s: %s", g.org_id, g.utente.email, tolte)
        flash(f"Portale svuotato: tolte {tolte['veicolo']} auto, {tolte['ddt']} DDT e {tolte['tag']} adesivi. "
              "I DDT ripartono dal n. 1 e gli adesivi dal 0001.", "ok")
        return redirect(url_for("admin.indice"))
    return render_template("admin/svuota.html", n=svuota_mod.conta(g.org_id))
