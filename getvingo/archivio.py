"""Archivio dei DDT: una copia PDF fissa di ogni versione (emissione, firme, arrivo, modifiche,
annullamento), su uno storage S3 compatibile (Cloudflare R2, Backblaze B2, AWS...) oppure, se
non configurato, nella cartella instance/archivio-ddt.

Dopo ogni salvataggio che tocca un DDT la nuova versione viene archiviata a fine richiesta.
Se l'invio non riesce il DDT resta valido: `flask archivia-ddt` recupera le copie mancanti."""
import hashlib
import os

from flask import current_app
from sqlalchemy import event
from sqlalchemy.orm import Session

from .models import Ddt, DdtArchivio, DdtRevisione, Movimento, adesso, db


# ---------------------------------------------------------------- dove si salva

class _Cartella:
    def __init__(self, cartella):
        self.cartella = cartella

    def salva(self, chiave, dati):
        percorso = os.path.join(self.cartella, chiave)
        os.makedirs(os.path.dirname(percorso), exist_ok=True)
        with open(percorso, "wb") as f:
            f.write(dati)

    def descrizione(self):
        return "cartella " + self.cartella

    def elimina(self, chiave):
        try:
            os.remove(os.path.join(self.cartella, chiave))
        except OSError:
            pass


class _S3:
    def __init__(self, bucket, endpoint, regione, chiave_id, segreto):
        import boto3
        self.bucket = bucket
        self.client = boto3.client("s3", endpoint_url=endpoint or None, region_name=regione or None,
                                   aws_access_key_id=chiave_id, aws_secret_access_key=segreto)

    def salva(self, chiave, dati):
        self.client.put_object(Bucket=self.bucket, Key=chiave, Body=dati, ContentType="application/pdf")

    def descrizione(self):
        return "S3 bucket " + self.bucket

    def elimina(self, chiave):
        try:  # con la conservazione bloccata (retention) lo storage può rifiutare: la copia resta lì
            self.client.delete_object(Bucket=self.bucket, Key=chiave)
        except Exception:
            pass


def destinazione():
    c = current_app.config
    if c.get("ARCHIVIO_S3_BUCKET"):
        return _S3(c["ARCHIVIO_S3_BUCKET"], c.get("ARCHIVIO_S3_ENDPOINT"), c.get("ARCHIVIO_S3_REGIONE"),
                   c.get("ARCHIVIO_S3_CHIAVE_ID"), c.get("ARCHIVIO_S3_SEGRETO"))
    return _Cartella(c["ARCHIVIO_DIR"])


# ---------------------------------------------------------------- archiviazione

def archivia(ddt, dest=None):
    """Salva la versione attuale del DDT se diversa dall'ultima archiviata. Ritorna la riga creata o None."""
    from .ddt import pdf
    dati = pdf(ddt, invariante=True)
    impronta = hashlib.sha256(dati).hexdigest()
    ultima = ddt.archiviazioni[-1] if ddt.archiviazioni else None
    if ultima and ultima.impronta == impronta:
        return None
    ora = adesso()
    stato = "-annullato" if ddt.annullato_at else ""
    chiave = (f"org-{ddt.org_id}/{ddt.anno}/DDT_{ddt.numero:05d}_{ddt.anno}/"
              f"{ora:%Y%m%d-%H%M%S}-rev{ddt.revisione}{stato}-{impronta[:8]}.pdf")
    (dest or destinazione()).salva(chiave, dati)
    riga = DdtArchivio(ddt=ddt, org_id=ddt.org_id, at=ora, impronta=impronta, chiave=chiave)
    db.session.add(riga)
    return riga


def dimentica(ddt):
    """Toglie le copie archiviate del DDT (eliminazione o modifica dell'admin senza traccia)."""
    dest = destinazione()
    for riga in list(ddt.archiviazioni):
        dest.elimina(riga.chiave)
        db.session.delete(riga)


def archivia_tutti():
    """Recupero: archivia ogni DDT la cui versione attuale non è ancora in archivio."""
    dest, nuovi = destinazione(), 0
    for ddt in Ddt.query.order_by(Ddt.id).all():
        if archivia(ddt, dest):
            nuovi += 1
            db.session.commit()
    return nuovi, dest.descrizione()


# ---------------------------------------------------------------- DDT toccati da un salvataggio

@event.listens_for(Session, "after_flush")
def _segna_toccati(sessione, contesto):
    toccati = sessione.info.setdefault("ddt_toccati", set())
    for obj in list(sessione.new) + list(sessione.dirty):
        ddt = obj if isinstance(obj, Ddt) else obj.ddt if isinstance(obj, (DdtRevisione, Movimento)) else None
        if ddt is not None and ddt.id is not None:  # un DDT non ancora salvato arriva col suo flush
            toccati.add(ddt.id)


@event.listens_for(Session, "after_commit")
def _confermati(sessione):
    sessione.info.setdefault("ddt_da_archiviare", set()).update(sessione.info.pop("ddt_toccati", ()))


@event.listens_for(Session, "after_soft_rollback")
def _annullati(sessione, transazione):
    sessione.info.pop("ddt_toccati", None)


def init_app(app):
    @app.after_request
    def archivia_in_sospeso(risposta):
        ids = db.session.info.pop("ddt_da_archiviare", None)
        if not ids or not app.config.get("ARCHIVIO_ATTIVO", True):
            return risposta
        try:
            dest = destinazione()
            for ddt_id in sorted(ids):
                ddt = db.session.get(Ddt, ddt_id)
                if ddt is not None:
                    archivia(ddt, dest)
                    db.session.commit()
            db.session.info.pop("ddt_da_archiviare", None)  # il commit qui sopra ri-segna gli stessi DDT
        except Exception:
            db.session.rollback()
            app.logger.exception("Archivio DDT non riuscito (recupero: flask archivia-ddt)")
        return risposta
