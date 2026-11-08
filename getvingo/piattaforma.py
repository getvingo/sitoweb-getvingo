"""Pannello Getvingo: il gestore della piattaforma crea le concessionarie clienti, invita il loro primo
amministratore e le sospende o riattiva. Non vede né tocca i loro dati (auto, DDT, danni): solo quanti sono.

Chi è gestore: le email in GETVINGO_GESTORI (variabile su Render), che devono anche avere un account.
"""
from functools import wraps

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import func

from . import accessi, servizi
from .auth import gestore, login_richiesto
from .models import Azienda, Ddt, Organizzazione, Utente, Veicolo, db

bp = Blueprint("piattaforma", __name__, url_prefix="/piattaforma")


def solo_gestore(view):
    @wraps(view)
    @login_richiesto
    def wrapper(*args, **kwargs):
        if not gestore(g.utente):
            abort(404)  # per gli altri la pagina non esiste
        return view(*args, **kwargs)
    return wrapper


def _conta(modello, *filtri):
    """Conteggi per organizzazione, su tutte le organizzazioni (solo numeri, nessun dato)."""
    q = db.session.query(modello.org_id, func.count(modello.id)).filter(*filtri).group_by(modello.org_id)
    return dict(q.execution_options(tutte_le_organizzazioni=True).all())


@bp.route("/")
@solo_gestore
def indice():
    org = Organizzazione.query.order_by(Organizzazione.nome).all()
    utenti = _conta(Utente, Utente.attivo.is_(True))
    auto = _conta(Veicolo, Veicolo.stato != "consegnato")
    ddt = _conta(Ddt)
    ultimo = dict(db.session.query(Utente.org_id, func.max(Utente.ultimo_accesso_at)).group_by(Utente.org_id)
                  .execution_options(tutte_le_organizzazioni=True).all())
    admin = {}
    for u in Utente.query.filter_by(ruolo="admin").order_by(Utente.id) \
            .execution_options(tutte_le_organizzazioni=True):
        admin.setdefault(u.org_id, []).append(u)
    return render_template("piattaforma/indice.html", organizzazioni=org, utenti=utenti, auto=auto, ddt=ddt,
                           ultimo=ultimo, admin=admin)


@bp.route("/nuova", methods=["GET", "POST"])
@solo_gestore
def nuova():
    if request.method == "POST":
        f = {k: request.form.get(k, "").strip() for k in ("ragione_sociale", "piva", "indirizzo", "nome", "email")}
        if not all(f[k] for k in ("ragione_sociale", "nome", "email")):
            flash("Servono ragione sociale, nome ed email dell'amministratore.", "errore")
            return render_template("piattaforma/nuova.html", f=f)
        mia = g.org_id
        try:
            org = servizi.crea_organizzazione(f["ragione_sociale"], f["email"], accessi.password_provvisoria(),
                                              admin_nome=f["nome"])
        except servizi.Vietato as e:
            db.session.rollback()
            g.org_id = mia
            flash(str(e), "errore")
            return render_template("piattaforma/nuova.html", f=f)
        # crea_organizzazione attiva la nuova: i dati aggiunti qui sotto vanno a lei
        azienda = Azienda.query.first()
        azienda.piva, azienda.indirizzo = f["piva"], f["indirizzo"]
        admin = Utente.query.filter_by(email=f["email"].lower()).first()
        admin.invito_in_attesa = True
        db.session.commit()
        link, inviata, errore = accessi.invia_invito(admin, invitato_da="Getvingo")
        g.org_id = mia  # si torna alla propria organizzazione
        return render_template("admin/invito.html", u=admin, scopo="invito", link=link, inviata=inviata,
                               errore=errore, torna=url_for("piattaforma.indice"), durata_ore=168)
    return render_template("piattaforma/nuova.html", f={})


def _organizzazione(org_id):
    org = db.session.get(Organizzazione, org_id)
    if org is None:
        abort(404)
    return org


@bp.route("/<int:org_id>/stato", methods=["POST"])
@solo_gestore
def cambia_stato(org_id):
    org = _organizzazione(org_id)
    if org.id == g.utente.org_id:
        flash("Non puoi sospendere l'azienda con cui sei collegato.", "errore")
    else:
        org.attiva = not org.attiva
        db.session.commit()
        flash(f"{org.nome}: {'accesso riattivato' if org.attiva else 'accesso sospeso, nessuno dei suoi utenti può entrare'}.",
              "ok")
    return redirect(url_for("piattaforma.indice"))


@bp.route("/<int:org_id>/invito/<int:utente_id>", methods=["POST"])
@solo_gestore
def rimanda_invito(org_id, utente_id):
    """Rimanda il link all'amministratore di un cliente (invito se non è mai entrato, altrimenti nuova password)."""
    _organizzazione(org_id)
    u = Utente.query.filter_by(id=utente_id, org_id=org_id, ruolo="admin") \
        .execution_options(tutte_le_organizzazioni=True).first_or_404()
    if u.invito_in_attesa:
        link, inviata, errore = accessi.invia_invito(u, invitato_da="Getvingo")
        scopo = "invito"
    else:
        scopo, inviata, errore = "recupero", False, None
        link = accessi.crea_link(u, "recupero")
        from . import posta
        if posta.configurata():
            try:
                accessi.invia_recupero(u)
                inviata = True
            except posta.ErrorePosta as e:
                errore = str(e)
    return render_template("admin/invito.html", u=u, scopo=scopo, link=link, inviata=inviata, errore=errore,
                           torna=url_for("piattaforma.indice"), durata_ore=accessi.DURATA[scopo] // 3600)
