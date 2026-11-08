import csv
import io
import secrets

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, url_for

from . import accessi, posta, servizi
from . import vin as vin_mod
from .auth import ruolo_richiesto
from datetime import datetime, timedelta

from .models import AccessoSupporto, Azienda, GiornoChiuso, Sede, Tag, Utente, Veicolo, Vettore, adesso, db

bp = Blueprint("admin", __name__, url_prefix="/admin")

# senza caratteri ambigui (0/O, 1/I/L) per chi li legge a voce
ALFABETO_TAG = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


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
        return redirect(url_for("admin.marchi_auto"))
    elenco = []
    for marca in sorted({v.marca.strip() for v in Veicolo.query if v.marca and v.marca.strip()}, key=str.lower):
        fonte, indirizzo = marchi.trova(marca)
        elenco.append({"marca": marca, "url": indirizzo, "fonte": fonte,
                       "nome_fonte": marchi.NOMI_FONTE.get(fonte, "Nessun logo · si vedono le iniziali"),
                       "ufficiale": fonte in ("caricato", "sito")})
    return render_template("admin/marchi.html", elenco=elenco)


@bp.route("/azienda")
@ruolo_richiesto("admin")
def azienda():
    return redirect(url_for("admin.gruppo_concessionaria"))


@bp.route("/chiusure", methods=["GET", "POST"])
@ruolo_richiesto("admin")
def chiusure():
    """Giorni senza preparazioni né consegne oltre alle feste nazionali (patrono, ferie, ponti):
    nel calendario a slot non si possono scegliere."""
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


@bp.route("/sedi", methods=["GET", "POST"])
@ruolo_richiesto("admin")
def sedi():
    if request.method == "POST" and _testo("nome"):
        db.session.add(Sede(nome=_testo("nome"), indirizzo=_testo("indirizzo"), piva=_testo("piva"),
                            tipo=request.form.get("tipo") if request.form.get("tipo") in Sede.TIPI else "altro"))
        db.session.commit()
        flash("Luogo aggiunto.", "ok")
        return redirect(url_for("admin.sedi"))
    return render_template("admin/sedi.html", sedi=Sede.query.order_by(Sede.nome).all(), tipi=Sede.TIPI)


@bp.route("/sedi/<int:sede_id>", methods=["GET", "POST"])
@ruolo_richiesto("admin")
def sede(sede_id):
    s = db.get_or_404(Sede, sede_id)
    if request.method == "POST":
        s.nome = _testo("nome") or s.nome
        s.indirizzo, s.piva = _testo("indirizzo"), _testo("piva")
        if request.form.get("tipo") in Sede.TIPI:
            s.tipo = request.form["tipo"]
        try:
            s.lat = float(request.form["lat"]) if _testo("lat") else None
            s.lon = float(request.form["lon"]) if _testo("lon") else None
        except ValueError:
            flash("Coordinate non valide.", "errore")
            return redirect(url_for("admin.sede", sede_id=s.id))
        firma = servizi.firma_valida(request.form.get("firma"))
        if firma:
            s.firma = firma
        db.session.commit()
        flash("Luogo aggiornato.", "ok")
        return redirect(url_for("admin.sedi"))
    return render_template("admin/sede.html", s=s, tipi=Sede.TIPI)


@bp.route("/vettori", methods=["GET", "POST"])
@ruolo_richiesto("admin")
def vettori():
    if request.method == "POST" and _testo("nome"):
        db.session.add(Vettore(nome=_testo("nome"), piva=_testo("piva"), indirizzo=_testo("indirizzo")))
        db.session.commit()
        flash("Vettore aggiunto.", "ok")
        return redirect(url_for("admin.vettori"))
    return render_template("admin/vettori.html", vettori=Vettore.query.order_by(Vettore.nome).all())


@bp.route("/attiva/<tipo>/<int:oggetto_id>", methods=["POST"])
@ruolo_richiesto("admin")
def attiva(tipo, oggetto_id):
    """Attiva/disattiva sedi e vettori (non si cancellano: sono nei DDT emessi)."""
    modello, campo, pagina = {"sede": (Sede, "attiva", "admin.sedi"),
                              "vettore": (Vettore, "attivo", "admin.vettori")}[tipo]
    oggetto = db.get_or_404(modello, oggetto_id)
    setattr(oggetto, campo, not getattr(oggetto, campo))
    db.session.commit()
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

RUOLI_INTERNI = {k: v for k, v in Utente.RUOLI.items() if k != "fornitore"}


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
    fornitori = [s for s in Sede.query.order_by(Sede.nome) if s.esterna]
    return render_template("admin/aziende.html", azienda=Azienda.query.first(), fornitori=fornitori,
                           interni=[u for u in Utente.query.all() if u.interno and not u.supporto])


@bp.route("/aziende/concessionaria", methods=["GET", "POST"])
@ruolo_richiesto("admin")
def gruppo_concessionaria():
    a = Azienda.query.first() or Azienda(ragione_sociale="")
    if request.method == "POST":
        if request.form.get("azione") == "utente":
            ruolo = request.form.get("ruolo") if request.form.get("ruolo") in RUOLI_INTERNI else "autista"
            esito = _crea_utente(ruolo)
            if esito is not None:
                return esito
        else:
            a.ragione_sociale, a.indirizzo, a.piva = _testo("ragione_sociale"), _testo("indirizzo"), _testo("piva")
            firma = servizi.firma_valida(request.form.get("firma"))
            if firma:
                a.firma = firma
            db.session.add(a)
            db.session.commit()
            flash("Dati e firma della concessionaria salvati: valgono per tutti i suoi utenti.", "ok")
        return redirect(url_for("admin.gruppo_concessionaria"))
    utenti = sorted((u for u in Utente.query.all() if u.interno), key=lambda u: (u.supporto, u.nome))
    return render_template("admin/gruppo.html", tipo="concessionaria", nome=a.ragione_sociale or "Concessionaria",
                           dati=a, utenti=utenti, ruoli=RUOLI_INTERNI)


@bp.route("/aziende/fornitore/<int:sede_id>", methods=["GET", "POST"])
@ruolo_richiesto("admin")
def gruppo_fornitore(sede_id):
    sede = db.get_or_404(Sede, sede_id)
    if not sede.esterna:
        return redirect(url_for("admin.gruppo_concessionaria"))
    if request.method == "POST":
        if request.form.get("azione") == "utente":
            esito = _crea_utente("fornitore", sede.id)
            if esito is not None:
                return esito
        else:
            sede.nome = _testo("ragione_sociale") or sede.nome
            sede.indirizzo, sede.piva = _testo("indirizzo"), _testo("piva")
            firma = servizi.firma_valida(request.form.get("firma"))
            if firma:
                sede.firma = firma
            db.session.commit()
            flash(f"Dati e firma di {sede.nome} salvati: valgono per tutti i suoi utenti.", "ok")
        return redirect(url_for("admin.gruppo_fornitore", sede_id=sede.id))
    return render_template("admin/gruppo.html", tipo="fornitore", nome=sede.nome, dati=sede,
                           utenti=sorted(sede.utenti, key=lambda u: u.nome), ruoli={"fornitore": "Fornitore"})


@bp.route("/utenti/<int:utente_id>", methods=["GET", "POST"])
@ruolo_richiesto("admin")
def utente(utente_id):
    u = db.get_or_404(Utente, utente_id)
    if u.supporto:  # l'accesso del gestore Getvingo non si modifica né si usa dalla concessionaria
        flash("“Supporto Getvingo” è l'accesso del gestore della piattaforma: non si modifica da qui.", "errore")
        return redirect(url_for("admin.gruppo_concessionaria"))
    torna = url_for("admin.gruppo_fornitore", sede_id=u.sede_id) if u.ruolo == "fornitore" and u.sede_id \
        else url_for("admin.gruppo_concessionaria")
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
    if request.method == "POST":
        if not _testo("nome"):
            flash("Il nome è obbligatorio.", "errore")
        else:
            vt.nome, vt.piva, vt.indirizzo = _testo("nome"), _testo("piva"), _testo("indirizzo")
            vt.attivo = request.form.get("attivo") == "1"
            db.session.commit()
            flash("Vettore aggiornato. I DDT già emessi restano con i dati di quando sono stati emessi.", "ok")
            return redirect(url_for("admin.vettori"))
    return render_template("admin/vettore.html", vt=vt)


@bp.route("/tag", methods=["GET", "POST"])
@ruolo_richiesto("admin")
def tag():
    if request.method == "POST":
        quanti = max(1, min(request.form.get("quanti", 10, type=int), 500))
        for _ in range(quanti):
            codice = "".join(secrets.choice(ALFABETO_TAG) for _ in range(8))
            if not Tag.query.filter_by(codice=codice).execution_options(tutte_le_organizzazioni=True).first():
                db.session.add(Tag(codice=codice))
        db.session.commit()
        flash(f"{quanti} codici creati: scrivi ciascun link su un adesivo NFC.", "ok")
        return redirect(url_for("admin.tag"))
    base = (current_app.config["BASE_URL"] or request.url_root).rstrip("/")
    host = request.host.split(":")[0]
    locale = host in ("127.0.0.1", "localhost") or host.startswith(("192.168.", "10."))
    # sugli adesivi serve sempre HTTPS (il GPS del browser funziona solo così)
    if not current_app.config["BASE_URL"] and not locale and base.startswith("http://"):
        base = "https://" + base[len("http://"):]
    return render_template("admin/tag.html", base=base, link_locale=locale and not current_app.config["BASE_URL"],
                           liberi=Tag.query.filter_by(veicolo_id=None).order_by(Tag.creato_at.desc()).all(),
                           abbinati=Tag.query.filter(Tag.veicolo_id.isnot(None)).count())


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
            if veicolo is None:
                veicolo = Veicolo(vin=vin)
                db.session.add(veicolo)
                nuovi += 1
            else:
                aggiornati += 1
            for campo in ("marca", "modello", "versione", "colore"):
                if riga.get(campo):
                    setattr(veicolo, campo, riga[campo])
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
