"""Copia del DDT firmato alle due parti (11/11/2026).

Quando il DDT si chiude (arrivo confermato) il PDF definitivo, con tutte le firme, parte per email a:
mittente, destinatario, backoffice della concessionaria (tutti gli utenti con quella mansione) e autista
(chi ha avviato il trasporto). Ognuno conserva la sua copia. PREDISPOSTO, NON ATTIVO: parte solo quando c'è
il server di posta (GETVINGO_SMTP_*); il testo dell'email (email/ddt_firmato.*) è provvisorio, da disegnare.
Indirizzi delle parti:
- concessionaria (sede o piazzale propri): "Email per i DDT" dei Dati aziendali, altrimenti gli amministratori
- luoghi esterni (fornitori, clienti, concessionari…): "Email per i DDT" del luogo, altrimenti gli utenti
  del fornitore
Senza server di posta non parte nulla (il DDT si chiude lo stesso). Data e indirizzi restano sul DDT.
"""
from flask import current_app

from . import ddt as ddt_pdf
from . import posta
from .models import Azienda, Utente, adesso, db


def indirizzi(sede):
    if sede is None:
        return []
    if sede.tipo == "propria":
        azienda = Azienda.query.first()
        if azienda and azienda.email_ddt:
            return [azienda.email_ddt]
        return [u.email for u in Utente.query.filter_by(ruolo="admin", attivo=True, eliminato=False, supporto=False)]
    if sede.email_ddt:
        return [sede.email_ddt]
    return [u.email for u in sede.utenti if u.attivo and not u.eliminato]


def backoffice():
    return [u.email for u in Utente.query.filter_by(ruolo="backoffice", attivo=True, eliminato=False, supporto=False)]


def autista(movimento):
    """Chi ha avviato il trasporto (ha toccato l'adesivo e firmato): riceve la copia anche se è un fornitore."""
    u = movimento.partenza_utente
    return [u.email] if u is not None and u.attivo and not u.eliminato and not u.supporto else []


def destinatari(documento):
    m = documento.movimento
    tutti = indirizzi(m.da_sede) + indirizzi(m.sede_arrivo_effettiva) + backoffice() + autista(m)
    visti, unici = set(), []
    for e in tutti:
        if e and e.lower() not in visti:
            visti.add(e.lower())
            unici.append(e)
    return unici


def invia(documento):
    """Manda la copia firmata a tutte e due le parti; ritorna gli indirizzi (vuoto se non è partito nulla)."""
    m = documento.movimento
    azienda = Azienda.query.first()
    if not (azienda and azienda.invia_copie_ddt):  # casella in Dati aziendali: se non è spuntata non parte nulla
        return []
    if documento.copie_inviate_at or m.stato != "chiuso" or not posta.configurata():
        return []
    a = destinatari(documento)
    if not a:
        return []
    pdf = ddt_pdf.pdf(documento)
    movimenti = documento.movimenti or [m]
    beni = (f"{len(movimenti)} autoveicoli" if len(movimenti) > 1
            else f"{m.veicolo.descrizione} · telaio {m.veicolo.vin}")
    tratta = f"{m.da_sede.nome if m.da_sede else '—'} → {m.sede_arrivo_effettiva.nome}"
    nome = f"DDT_{documento.numero}_{documento.anno}.pdf"
    for indirizzo in a:
        try:
            posta.invia(indirizzo, f"DDT n. {documento.sigla} firmato · {tratta}", "ddt_firmato",
                        allegati=[(nome, pdf, "application/pdf")], in_sottofondo=True,
                        d=documento, m=m, tratta=tratta, beni=beni)
        except posta.ErrorePosta as e:
            current_app.logger.error("Copia del DDT %s non inviata a %s: %s", documento.sigla, indirizzo, e)
    documento.copie_inviate_at = adesso()
    documento.copie_inviate_a = ", ".join(a)[:500]
    db.session.commit()
    return a
