"""Link personali per scegliere la password: invito (utente nuovo) e recupero (password dimenticata).

Il link contiene l'utente e un pezzo dell'impronta della sua password attuale, firmati con la chiave
segreta del portale: scade da solo (invito 7 giorni, recupero 1 ora) e vale una volta sola, perché
appena la password cambia l'impronta non corrisponde più. Nel database non si salva nulla.
"""
import secrets

from flask import current_app, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import posta
from .models import Utente, db

DURATA = {"invito": 7 * 24 * 3600, "recupero": 3600}


def _firmatario():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="getvingo-password")


def _impronta(utente):
    return utente.password_hash[-16:]


def password_provvisoria():
    """Password casuale mai comunicata a nessuno: l'utente sceglie la sua dal link d'invito."""
    return secrets.token_urlsafe(24)


def crea_link(utente, scopo):
    token = _firmatario().dumps({"u": utente.id, "h": _impronta(utente), "s": scopo})
    percorso = url_for("auth.imposta_password", token=token)
    # mai l'indirizzo della richiesta (un Host falso porterebbe il link altrove): quello pubblico del portale
    base = (current_app.config.get("BASE_URL") or "").rstrip("/")
    return base + percorso if base else url_for("auth.imposta_password", token=token, _external=True)


def leggi_link(token):
    """(utente, scopo) se il link è valido; altrimenti (None, motivo da mostrare)."""
    try:
        dati = _firmatario().loads(token, max_age=max(DURATA.values()))
    except SignatureExpired:
        return None, "Il link è scaduto."
    except BadSignature:
        return None, "Il link non è valido."
    scopo = dati.get("s")
    try:
        _firmatario().loads(token, max_age=DURATA.get(scopo, 0))
    except SignatureExpired:
        return None, "Il link è scaduto." if scopo == "invito" else "Il link è scaduto: valeva un'ora."
    u = Utente.query.execution_options(tutte_le_organizzazioni=True).filter_by(id=dati.get("u")).first()
    if u is None or not u.attivo or u.supporto or not u.organizzazione.attiva:
        return None, "L'account non è attivo: contatta l'amministratore della tua azienda."
    if dati.get("h") != _impronta(u):
        return None, "Il link è già stato usato."
    return u, scopo


def invia_invito(utente, invitato_da=None):
    """Email di benvenuto con il link per scegliere la password. Ritorna (link, inviata, errore)."""
    link = crea_link(utente, "invito")
    if not posta.configurata():
        return link, False, None
    try:
        posta.invia(utente.email, "Benvenuto in Getvingo: scegli la tua password", "invito",
                    utente=utente, link=link, invitato_da=invitato_da,
                    azienda=utente.organizzazione.nome, giorni=DURATA["invito"] // 86400)
        return link, True, None
    except posta.ErrorePosta as e:
        return link, False, str(e)


def invia_recupero(utente):
    link = crea_link(utente, "recupero")
    posta.invia(utente.email, "Getvingo: reimposta la tua password", "recupero", utente=utente, link=link)


def avvisa_password_cambiata(utente):
    """Avviso di sicurezza: se non è stato lui, sa subito che deve intervenire."""
    if not posta.configurata():
        return
    try:
        posta.invia(utente.email, "Getvingo: la tua password è stata cambiata", "password_cambiata", utente=utente)
    except posta.ErrorePosta:
        pass  # l'avviso non deve bloccare il cambio password


def nuovo_utente_invitato(nome, email, ruolo, sede_id=None):
    """Utente con password provvisoria (sconosciuta a tutti): entra solo dal link d'invito."""
    u = Utente(nome=nome, email=email, ruolo=ruolo, sede_id=sede_id)
    u.imposta_password(password_provvisoria())
    db.session.add(u)
    return u
