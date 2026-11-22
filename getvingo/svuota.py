"""Svuota una concessionaria per ripartire con un test (22/11/2026, richiesta dell'utente): toglie auto, DDT
(con revisioni e registro dell'archivio), adesivi NFC e tutto ciò che è legato alle auto (movimenti, storico,
azioni, danni con foto, preparazioni, solleciti). Restano utenti, sedi, fornitori, vettori, dati aziendali,
catalogo e giorni chiusi. Dopo, i DDT ripartono dal n. 1 e gli adesivi da SIGLA-0001.
Prima di cancellare salva una copia di tutte le righe tolte in un file JSON.
Comando: flask svuota-stock --org <id> [--conferma]"""
import json
from datetime import date, datetime

from sqlalchemy import inspect

from .models import (Azione, Danno, DannoFoto, Ddt, DdtArchivio, DdtRevisione, Evento, Movimento, Organizzazione,
                     Preparazione, PreparazioneFase, Sollecito, Tag, Veicolo, db)

# ordine di cancellazione: prima chi rimanda agli altri
ORDINE = (DannoFoto, Danno, PreparazioneFase, Preparazione, Azione, Evento, Sollecito, DdtRevisione, DdtArchivio,
          Ddt, Movimento, Tag, Veicolo)
TUTTE = {"tutte_le_organizzazioni": True}


def _righe(modello, org_id):
    return modello.query.filter(modello.org_id == org_id).execution_options(**TUTTE)


def conta(org_id):
    return {m.__tablename__: _righe(m, org_id).count() for m in ORDINE}


def _valore(x):
    return x.isoformat() if isinstance(x, (datetime, date)) else x


def esporta(org_id, percorso):
    """Copia di sicurezza: ogni riga che verrà tolta, colonna per colonna."""
    copia = {"organizzazione": org_id, "fatta_il": datetime.now().isoformat(), "tabelle": {}}
    for m in ORDINE:
        colonne = [c.key for c in inspect(m).mapper.column_attrs]
        copia["tabelle"][m.__tablename__] = [{k: _valore(getattr(r, k)) for k in colonne} for r in _righe(m, org_id)]
    with open(percorso, "w", encoding="utf-8") as f:
        json.dump(copia, f, ensure_ascii=False)
    return percorso


def svuota(org_id):
    """Cancella tutto in una sola transazione (o tutto o niente)."""
    if db.session.get(Organizzazione, org_id) is None:
        raise ValueError(f"Nessuna organizzazione n. {org_id}")
    # movimento ↔ DDT si rimandano a vicenda: prima si stacca il DDT dal movimento
    _righe(Movimento, org_id).update({Movimento.ddt_id: None}, synchronize_session=False)
    tolte = {}
    for m in ORDINE:
        tolte[m.__tablename__] = _righe(m, org_id).delete(synchronize_session=False)
    db.session.commit()
    return tolte
