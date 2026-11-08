import os
import secrets
import time
from functools import wraps

from flask import (Blueprint, abort, current_app, flash, g, redirect, render_template,
                   request, session, url_for)

from . import accessi, posta
from .models import Utente, adesso, db

bp = Blueprint("auth", __name__)

# Tentativi di accesso falliti per email e per indirizzo: dopo MAX_TENTATIVI in
# FINESTRA secondi l'accesso si blocca per quel tempo (contro chi prova password a raffica).
MAX_TENTATIVI, FINESTRA = 8, 15 * 60
_falliti = {}


def _bloccato(*chiavi):
    ora = time.time()
    for k in chiavi:
        _falliti[k] = [t for t in _falliti.get(k, []) if ora - t < FINESTRA]
    return any(len(_falliti[k]) >= MAX_TENTATIVI for k in chiavi)


def _fallito(*chiavi):
    for k in chiavi:
        _falliti.setdefault(k, []).append(time.time())


def gestore(utente):
    """Gestore della piattaforma Getvingo (crea e sospende le concessionarie): email elencate in
    GETVINGO_GESTORI, separate da virgola. Si decide da Render, non dal portale."""
    elenco = current_app.config.get("GESTORI")
    if elenco is None:
        elenco = os.environ.get("GETVINGO_GESTORI", "")
    emails = {e.strip().lower() for e in elenco.split(",") if e.strip()}
    return bool(utente and utente.attivo and utente.email.lower() in emails)


def _entra(u):
    """Apre la sessione: la sessione ricorda l'impronta della password, così cambiandola
    si scollegano gli altri dispositivi."""
    session.clear()
    session.permanent = True
    session["uid"] = u.id
    session["ph"] = u.password_hash[-16:]
    u.ultimo_accesso_at = adesso()
    db.session.commit()


def init_app(app):
    @app.before_request
    def carica_utente():
        uid = session.get("uid")
        g.org_id = None  # l'utente si cerca tra tutte le organizzazioni
        g.utente = Utente.query.filter_by(id=uid, attivo=True).first() if uid else None
        if g.utente is not None and not g.utente.organizzazione.attiva:
            g.utente = None
        if g.utente is not None:
            ph = session.get("ph")
            if ph is None:  # sessioni aperte prima di questa versione: si adottano senza scollegarle
                session["ph"] = g.utente.password_hash[-16:]
            elif ph != g.utente.password_hash[-16:]:  # password cambiata altrove: questa sessione non vale più
                session.clear()
                g.utente = None
        # da qui in poi ogni lettura dal database vede solo i dati di questa organizzazione
        g.org_id = g.utente.org_id if g.utente else None

    @app.before_request
    def verifica_csrf():
        if request.method == "POST" and current_app.config["CSRF_ATTIVO"]:
            inviato = request.form.get("csrf") or request.headers.get("X-CSRF")
            if not session.get("csrf") or inviato != session.get("csrf"):
                if request.path.startswith("/api/"):
                    return {"errore": "Pagina scaduta: ricaricala e riprova."}, 400
                # niente pagina di errore: si torna dov'eri con un avviso chiaro
                flash("La pagina era scaduta: riprova adesso.", "errore")
                return redirect(request.referrer or url_for("main.home"))

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        return session["csrf"]

    # funzione globale (non variabile di contesto): così è visibile anche dentro le macro importate
    app.jinja_env.globals["csrf_token"] = csrf_token
    app.jinja_env.globals["gestore"] = lambda: gestore(g.get("utente"))

    @app.after_request
    def intestazioni_sicurezza(risposta):
        risposta.headers.setdefault("X-Content-Type-Options", "nosniff")
        risposta.headers.setdefault("X-Frame-Options", "DENY")
        risposta.headers.setdefault("Referrer-Policy", "same-origin")
        # le pagine non restano in memoria: tornando indietro si ricaricano con i dati aggiornati
        # (es. "Nuova auto" non deve riproporre un adesivo appena abbinato)
        if risposta.mimetype == "text/html":
            risposta.headers.setdefault("Cache-Control", "no-store")
        return risposta


def login_richiesto(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.utente is None:
            return redirect(url_for("auth.login", next=request.full_path))
        return view(*args, **kwargs)
    return wrapper


def ruolo_richiesto(*ruoli):
    def decoratore(view):
        @wraps(view)
        @login_richiesto
        def wrapper(*args, **kwargs):
            if g.utente.ruolo not in ruoli:
                abort(403)
            return view(*args, **kwargs)
        return wrapper
    return decoratore


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        chiavi = ("email:" + email, "ip:" + (request.remote_addr or ""))
        if _bloccato(*chiavi):
            flash("Troppi tentativi sbagliati: riprova tra 15 minuti.", "errore")
            return render_template("login.html"), 429
        u = Utente.query.filter_by(email=email, attivo=True).first()
        if u and u.verifica_password(request.form["password"]) and not u.organizzazione.attiva:
            flash("L'accesso della tua azienda a Getvingo è sospeso: contatta Getvingo.", "errore")
            return render_template("login.html")
        if u and u.verifica_password(request.form["password"]):
            _entra(u)
            dest = request.args.get("next", "")
            # solo percorsi interni, per evitare redirect verso altri siti
            if not dest.startswith("/") or dest.startswith("//"):
                dest = url_for("main.home")
            return redirect(dest)
        _fallito(*chiavi)
        flash("Email o password errati.", "errore")
    return render_template("login.html")


@bp.route("/password", methods=["GET", "POST"])
@login_richiesto
def cambia_password():
    if request.method == "POST":
        nuova = request.form.get("nuova", "")
        if not g.utente.verifica_password(request.form.get("attuale", "")):
            flash("La password attuale non è corretta.", "errore")
        elif len(nuova) < 8:
            flash("La nuova password deve avere almeno 8 caratteri.", "errore")
        elif nuova != request.form.get("conferma"):
            flash("Le due password nuove non coincidono.", "errore")
        else:
            g.utente.imposta_password(nuova)
            g.utente.invito_in_attesa = False
            db.session.commit()
            session["ph"] = g.utente.password_hash[-16:]  # resta collegato solo questo dispositivo
            accessi.avvisa_password_cambiata(g.utente)
            flash("Password cambiata. Gli altri dispositivi collegati con questo account sono stati scollegati.", "ok")
            return redirect(url_for("main.home"))
    return render_template("password.html")


@bp.route("/password-dimenticata", methods=["GET", "POST"])
def password_dimenticata():
    """Chiede l'email e manda il link per sceglierne una nuova. La risposta è sempre la stessa,
    così nessuno può scoprire quali email sono registrate."""
    attiva = posta.configurata()
    if request.method == "POST" and attiva:
        email = request.form.get("email", "").strip().lower()
        chiavi = ("recupero:" + email, "recupero-ip:" + (request.remote_addr or ""))
        if _bloccato(*chiavi):
            flash("Troppe richieste: riprova tra 15 minuti.", "errore")
            return render_template("password_dimenticata.html", attiva=attiva), 429
        _fallito(*chiavi)  # ogni richiesta conta: massimo 8 email ogni 15 minuti
        u = Utente.query.filter_by(email=email, attivo=True).first()
        if u and u.organizzazione.attiva:
            try:
                accessi.invia_recupero(u)
            except posta.ErrorePosta:
                flash("In questo momento non riusciamo a mandare l'email: riprova tra qualche minuto.", "errore")
                return render_template("password_dimenticata.html", attiva=attiva)
        return render_template("password_dimenticata.html", attiva=attiva, inviata=email)
    return render_template("password_dimenticata.html", attiva=attiva)


@bp.route("/imposta-password/<token>", methods=["GET", "POST"])
def imposta_password(token):
    """Pagina aperta dal link dell'email (invito o recupero): si sceglie la password e si entra."""
    u, scopo = accessi.leggi_link(token)
    if u is None:
        return render_template("imposta_password.html", errore=scopo), 410
    if request.method == "POST":
        nuova = request.form.get("nuova", "")
        if len(nuova) < 8:
            flash("La password deve avere almeno 8 caratteri.", "errore")
        elif nuova != request.form.get("conferma"):
            flash("Le due password non coincidono.", "errore")
        else:
            primo = u.invito_in_attesa
            u.imposta_password(nuova)
            u.invito_in_attesa = False
            db.session.commit()
            _falliti.pop("email:" + u.email, None)  # sblocca chi si era bloccato provando la vecchia
            if not primo:
                accessi.avvisa_password_cambiata(u)
            _entra(u)
            flash(f"Benvenuto in Getvingo, {u.nome}." if primo else "Password impostata: sei dentro.", "ok")
            return redirect(url_for("main.home"))
    return render_template("imposta_password.html", u=u, scopo=scopo)


@bp.route("/privacy")
def privacy():
    return render_template("privacy.html")


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
