"""Invio delle email del portale (inviti, recupero password, avvisi di sicurezza).

Server di posta da variabili d'ambiente (casella di getvingo.com su Aruba):
  GETVINGO_SMTP_HOST      es. smtps.aruba.it
  GETVINGO_SMTP_PORTA     465 (SSL, predefinita) oppure 587 (STARTTLS)
  GETVINGO_SMTP_UTENTE    indirizzo completo della casella, es. noreply@getvingo.com
  GETVINGO_SMTP_PASSWORD  password della casella
  GETVINGO_MITTENTE       facoltativo, es. "Getvingo <noreply@getvingo.com>"
Senza server il portale funziona lo stesso: inviti e link si copiano dalla pagina e si mandano a mano.
Nei test le email non partono: finiscono in app.extensions["posta_inviata"].
"""
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from flask import current_app, render_template


class ErrorePosta(Exception):
    """Email non partita: il messaggio è da mostrare così com'è."""


def _impostazioni():
    return {
        "host": os.environ.get("GETVINGO_SMTP_HOST", ""),
        "porta": int(os.environ.get("GETVINGO_SMTP_PORTA") or 465),
        "utente": os.environ.get("GETVINGO_SMTP_UTENTE", ""),
        "password": os.environ.get("GETVINGO_SMTP_PASSWORD", ""),
        "mittente": os.environ.get("GETVINGO_MITTENTE", ""),
    }


def configurata():
    if current_app.config.get("TESTING"):
        return current_app.config.get("POSTA_ATTIVA", False)
    s = _impostazioni()
    return bool(s["host"] and s["utente"] and s["password"])


def invia(destinatario, oggetto, modello, **dati):
    """Manda l'email scritta nei modelli email/<modello>.txt e .html."""
    testo = render_template(f"email/{modello}.txt", **dati)
    html = render_template(f"email/{modello}.html", oggetto=oggetto, **dati)
    if current_app.config.get("TESTING"):
        current_app.extensions.setdefault("posta_inviata", []).append(
            {"a": destinatario, "oggetto": oggetto, "testo": testo, "html": html})
        return
    s = _impostazioni()
    if not configurata():
        raise ErrorePosta("Invio email non configurato sul server.")
    msg = EmailMessage()
    msg["Subject"] = oggetto
    msg["From"] = s["mittente"] or formataddr(("Getvingo", s["utente"]))
    msg["To"] = destinatario
    msg["Message-ID"] = make_msgid(domain=s["utente"].split("@")[-1])
    msg.set_content(testo)
    msg.add_alternative(html, subtype="html")
    try:
        if s["porta"] == 465:
            with smtplib.SMTP_SSL(s["host"], s["porta"], context=ssl.create_default_context(), timeout=20) as srv:
                srv.login(s["utente"], s["password"])
                srv.send_message(msg)
        else:
            with smtplib.SMTP(s["host"], s["porta"], timeout=20) as srv:
                srv.starttls(context=ssl.create_default_context())
                srv.login(s["utente"], s["password"])
                srv.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        current_app.logger.error("Email a %s non inviata: %s", destinatario, e)
        raise ErrorePosta("Il server di posta non ha accettato l'email: riprova più tardi.")
