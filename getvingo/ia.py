"""Assistente IA (Claude): compila il DDT da una frase e risponde alle domande sui mezzi.

Credenziali: variabile d'ambiente ANTHROPIC_API_KEY (o ANTHROPIC_AUTH_TOKEN).
Senza credenziali le funzioni sollevano ErroreIA con un messaggio leggibile.
"""
import json
import os

import anthropic

from . import servizi
from . import vin as vin_mod
from .models import Movimento, Sede, Veicolo, Vettore, adesso, db

MODELLO = "claude-opus-5-5"
# su un rifiuto dei filtri di sicurezza l'API riprova da sola su un modello adatto
BETA_FALLBACK = "server-side-fallback-2026-07-01"
MAX_GIRI_STRUMENTI = 6

_client = None


class ErroreIA(Exception):
    """Errore da mostrare all'utente così com'è."""


def disponibile():
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _cliente():
    global _client
    if not disponibile():
        raise ErroreIA("Assistente non attivo: manca la chiave Anthropic (ANTHROPIC_API_KEY) sul server.")
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def _chiama(**parametri):
    """Una richiesta a Claude con fallback automatico ed errori tradotti in italiano."""
    try:
        risposta = _cliente().beta.messages.create(
            model=MODELLO, betas=[BETA_FALLBACK], fallbacks="default", **parametri)
    except anthropic.AuthenticationError:
        raise ErroreIA("La chiave Anthropic non è valida.")
    except anthropic.RateLimitError:
        raise ErroreIA("Troppe richieste all'assistente: riprova tra qualche secondo.")
    except anthropic.APIStatusError as e:
        raise ErroreIA(f"L'assistente non ha risposto (errore {e.status_code}). Riprova.")
    except anthropic.APIConnectionError:
        raise ErroreIA("Assistente non raggiungibile: controlla la connessione.")
    if risposta.stop_reason == "refusal":
        raise ErroreIA("L'assistente non può rispondere a questa richiesta.")
    return risposta


def _testo(risposta):
    return "".join(b.text for b in risposta.content if b.type == "text").strip()


# ---------------------------------------------------------------- compilazione DDT

def compila_ddt(testo, veicolo, utente):
    """Da una frase libera ("sono Luca e porto l'auto a fare i cerchi neri dalla carrozzeria AA")
    ai campi del modulo di partenza. Restituisce un dict pronto per il form."""
    testo = (testo or "").strip()
    if not testo:
        raise ErroreIA("Dimmi chi sposta l'auto, dove la porta e per quale lavorazione.")
    sedi = [s for s in Sede.query.filter_by(attiva=True).order_by(Sede.nome) if s.id != veicolo.sede_id]
    vettori = Vettore.query.filter_by(attivo=True).order_by(Vettore.nome).all()
    elenco_sedi = "\n".join(f"- id {s.id}: {s.nome} ({Sede.TIPI[s.tipo]}, {s.indirizzo or 'senza indirizzo'})"
                            for s in sedi)
    elenco_vettori = "\n".join(f"- id {v.id}: {v.nome}" for v in vettori)
    schema = {
        "type": "object",
        "properties": {
            "conducente": {"type": "string"},
            "a_sede_id": {"type": "integer", "enum": [0] + [s.id for s in sedi]},
            "lavorazione": {"type": "string", "enum": Movimento.LAVORAZIONI},
            "lavorazione_note": {"type": "string"},
            "causale": {"type": "string", "enum": Movimento.CAUSALI},
            "vettore_id": {"type": "integer", "enum": [0] + [v.id for v in vettori]},
            "riepilogo": {"type": "string"},
            "dubbi": {"type": "string"},
        },
        "required": ["conducente", "a_sede_id", "lavorazione", "lavorazione_note", "causale",
                     "vettore_id", "riepilogo", "dubbi"],
        "additionalProperties": False,
    }
    sistema = (
        "Compili un documento di trasporto (DDT) per lo spostamento di un'auto, partendo da una frase "
        "detta a voce da chi la sposta in piazzale. Rispondi solo con i campi richiesti.\n"
        "- conducente: nome e cognome di chi guida, come detto nella frase; stringa vuota se non detto.\n"
        "- a_sede_id: l'id del luogo di destinazione scelto dall'elenco; i nomi detti a voce possono essere "
        "abbreviati o storpiati, scegli il più somigliante. 0 se non si capisce o non c'è.\n"
        "- lavorazione: la voce dell'elenco che corrisponde meglio a cosa va fatto; "
        f"\"{Movimento.NESSUNA}\" se è solo uno spostamento.\n"
        "- lavorazione_note: i dettagli concreti della lavorazione (es. \"cerchi verniciati neri\"), "
        "brevi, senza ripetere la voce scelta.\n"
        "- causale: invio lavorazione verso carrozzerie, officine e altri fornitori; reso lavorazione quando "
        "l'auto torna da un fornitore; conto esposizione verso una fiera o uno showroom esterno e reso esposizione "
        "al ritorno; conto visione (o vendita, se è detto che è venduta) verso un altro concessionario o "
        "rivenditore e reso al ritorno; spostamento tra sedi proprie; consegna a cliente verso un cliente.\n"
        "- vettore_id: il vettore se nominato; altrimenti 0.\n"
        "- riepilogo: una frase in italiano che riassume cosa hai capito.\n"
        "- dubbi: cosa manca o è incerto, in poche parole; stringa vuota se è tutto chiaro.\n\n"
        f"Luoghi possibili:\n{elenco_sedi}\n\nVettori:\n{elenco_vettori}"
    )
    risposta = _chiama(
        max_tokens=2000,
        system=sistema,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content":
                   f"Auto: {veicolo.descrizione}, VIN {veicolo.vin}, ora presso "
                   f"{veicolo.sede.nome if veicolo.sede else '—'}.\nFrase: {testo}"}],
    )
    try:
        dati = json.loads(_testo(risposta))
    except ValueError:
        raise ErroreIA("Non ho capito la risposta dell'assistente: riprova.")
    # se il nome non è stato detto, si propone chi sta usando il telefono
    if not dati.get("conducente") and utente.ruolo != "admin":
        dati["conducente"] = utente.nome
    if not dati.get("vettore_id"):
        dati["vettore_id"] = servizi.ultimo_vettore_id(utente) or (vettori[0].id if vettori else 0)
    return dati


# ---------------------------------------------------------------- chat sui mezzi

def _riassunto_veicolo(v, utente):
    k, stato = v.stato_per(utente)
    voce = {"id": v.id, "descrizione": v.descrizione, "tipologia": Veicolo.TIPOLOGIE[v.tipologia],
            "vin": v.vin, "targa": v.targa or None, "stato": stato,
            "dove": v.sede.nome if v.sede and v.stato != "in_viaggio" else None}
    ultimo = v.arrivo_qui
    if ultimo and ultimo.giorni_lavorazione is not None:
        voce["giorni_lavorazione"] = ultimo.giorni_lavorazione
    if v.lat is not None and v.stato != "consegnato":
        voce["gps"] = {"lat": round(v.lat, 6), "lon": round(v.lon, 6),
                       "aggiornata": v.posizione_at.strftime("%d/%m/%Y %H:%M") if v.posizione_at else None}
    return voce


def _cerca_veicoli(utente, testo="", tipologia="", stato="", includi_consegnati=False):
    q = Veicolo.query
    if not includi_consegnati:
        q = q.filter(Veicolo.stato != "consegnato")
    risultati = []
    parole = [p.lower() for p in (testo or "").split() if p]
    for v in q.order_by(Veicolo.id).all():
        campo = f"{v.marca} {v.modello} {v.colore} {v.vin} {v.targa or ''} {v.sede.nome if v.sede else ''}".lower()
        if parole and not all(p in campo or vin_mod.normalizza(p).lower() in v.vin.lower() for p in parole):
            continue
        if tipologia and v.tipologia != tipologia:
            continue
        if stato and v.fase != stato:
            continue
        risultati.append(_riassunto_veicolo(v, utente))
    return {"trovati": len(risultati), "veicoli": risultati[:30]}


def _dettaglio_veicolo(utente, veicolo_id):
    v = db.session.get(Veicolo, veicolo_id)
    if v is None:
        return {"errore": "Veicolo non trovato"}
    voce = _riassunto_veicolo(v, utente)
    movimenti = sorted((m for m in v.movimenti if m.ddt and not m.ddt.annullato_at),
                       key=lambda m: m.ddt.emesso_at)
    voce["ddt"] = [{"numero": m.ddt.sigla, "data": m.ddt.emesso_at.strftime("%d/%m/%Y"),
                    "da": m.da_sede.nome if m.da_sede else None, "a": m.sede_arrivo_effettiva.nome,
                    "stato": Movimento.STATI[m.stato], "conducente": m.conducente,
                    "lavorazione": m.lavorazione, "note": m.lavorazione_note} for m in movimenti]
    voce["lavorazioni_fatte"] = [
        {"lavorazione": m.lavorazione, "presso": m.sede_arrivo_effettiva.nome,
         "completata": m.lavorazione_completata_at.strftime("%d/%m/%Y"), "giorni": m.giorni_lavorazione}
        for m in movimenti if m.lavorazione_completata_at]
    return voce


def _riepilogo_generale(utente):
    attivi = Veicolo.query.filter(Veicolo.stato != "consegnato").all()
    per_stato = {}
    for v in attivi:
        nome = Veicolo.FASI[v.fase]
        per_stato[nome] = per_stato.get(nome, 0) + 1
    ferme = [_riassunto_veicolo(v, utente) for v in attivi
             if v.fase == "lavorazione" and v.arrivo_qui and (v.arrivo_qui.giorni_lavorazione or 0) >= 7]
    return {"oggi": adesso().strftime("%d/%m/%Y"), "veicoli_per_stato": per_stato,
            "in_lavorazione_da_7_giorni_o_piu": ferme}


STRUMENTI = [
    {"name": "cerca_veicoli", "strict": True,
     "description": "Cerca veicoli per testo libero (marca, modello, colore, parte del VIN, luogo), "
                    "tipologia e stato. Restituisce stato, luogo, posizione GPS e giorni di lavorazione.",
     "input_schema": {"type": "object", "properties": {
         "testo": {"type": "string", "description": "Parole da cercare; stringa vuota per tutti"},
         "tipologia": {"type": "string", "enum": ["", "vettura", "commerciale"]},
         "stato": {"type": "string", "enum": [""] + list(Veicolo.FASI)},
         "includi_consegnati": {"type": "boolean"}},
         "required": ["testo", "tipologia", "stato", "includi_consegnati"], "additionalProperties": False}},
    {"name": "dettaglio_veicolo", "strict": True,
     "description": "Storico completo di un veicolo: tutti i DDT, le lavorazioni fatte, posizione GPS.",
     "input_schema": {"type": "object", "properties": {"veicolo_id": {"type": "integer"}},
                      "required": ["veicolo_id"], "additionalProperties": False}},
    {"name": "riepilogo_generale", "strict": True,
     "description": "Quanti veicoli ci sono in ogni stato e quali sono fermi dai fornitori da 7 giorni o più.",
     "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
]


def _esegui(nome, argomenti, utente):
    if nome == "cerca_veicoli":
        return _cerca_veicoli(utente, **argomenti)
    if nome == "dettaglio_veicolo":
        return _dettaglio_veicolo(utente, argomenti["veicolo_id"])
    if nome == "riepilogo_generale":
        return _riepilogo_generale(utente)
    return {"errore": f"strumento sconosciuto {nome}"}


SISTEMA_CHAT = (
    "Sei l'assistente di Getvingo, il portale con cui una concessionaria gestisce le proprie auto: "
    "spostamenti con DDT, lavorazioni presso fornitori esterni, preparazione alla consegna e posizione "
    "GPS nei piazzali. Rispondi in italiano, in modo breve e diretto: spesso la risposta viene letta ad "
    "alta voce a chi è in piazzale, quindi niente tabelle né markdown, frasi semplici. Usa sempre gli "
    "strumenti per i dati, non inventare. Per dire dove si trova un'auto indica il luogo e quando è stata "
    "aggiornata la posizione; il pulsante per raggiungerla compare da solo sotto la risposta. "
    "Le ultime 8 cifre del VIN bastano per identificare un'auto a voce."
)


def chat(storia, utente):
    """storia: [{"ruolo": "utente"|"assistente", "testo": str}]. Restituisce (risposta, id_veicoli)."""
    messaggi = [{"role": "user" if m.get("ruolo") == "utente" else "assistant",
                 "content": str(m.get("testo", ""))[:4000]}
                for m in storia[-20:] if str(m.get("testo", "")).strip()]
    if not messaggi or messaggi[-1]["role"] != "user":
        raise ErroreIA("Scrivi o di' una domanda.")
    while messaggi[0]["role"] != "user":
        messaggi.pop(0)
    veicoli_citati = []
    for _ in range(MAX_GIRI_STRUMENTI):
        risposta = _chiama(max_tokens=4000, system=SISTEMA_CHAT, tools=STRUMENTI,
                           output_config={"effort": "low"}, messages=messaggi)
        if risposta.stop_reason != "tool_use":
            testo = _testo(risposta) or "Non ho trovato una risposta."
            return testo, _veicoli_nella_risposta(testo, veicoli_citati)
        messaggi.append({"role": "assistant", "content": risposta.content})
        risultati = []
        for blocco in risposta.content:
            if blocco.type != "tool_use":
                continue
            esito = _esegui(blocco.name, blocco.input, utente)
            for v in esito.get("veicoli", []) + ([esito] if "vin" in esito else []):
                if v.get("id") and v["id"] not in veicoli_citati:
                    veicoli_citati.append(v["id"])
            risultati.append({"type": "tool_result", "tool_use_id": blocco.id,
                              "content": json.dumps(esito, ensure_ascii=False)})
        messaggi.append({"role": "user", "content": risultati})
    return "La domanda richiede troppe ricerche: prova a essere più specifico.", []


def _veicoli_nella_risposta(testo, ids):
    """Pulsanti sotto la risposta: solo le auto effettivamente nominate (modello o ultime cifre VIN)."""
    t = testo.lower()
    nominati = []
    for vid in ids:
        v = db.session.get(Veicolo, vid)
        if v and (v.vin[-6:].lower() in t or (v.modello and v.modello.lower() in t)):
            nominati.append(v)
    if not nominati and len(ids) <= 3:
        nominati = [db.session.get(Veicolo, vid) for vid in ids]
    return [v.id for v in nominati if v][:6]
