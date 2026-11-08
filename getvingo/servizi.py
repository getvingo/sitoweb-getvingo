"""Logica dei movimenti e dei DDT, condivisa tra scheda veicolo, pagina DDT e fornitori."""
import base64
import binascii
import math
from datetime import date, datetime, time, timedelta
from itertools import groupby

from sqlalchemy import func

from flask import g

from .models import (Azienda, Azione, Danno, DannoFoto, Ddt, DdtRevisione, Evento, GiornoChiuso, Movimento, Organizzazione,
                     Preparazione, PreparazioneFase, Sede, Sollecito, Tag, Utente, Veicolo, Vettore, adesso, db)

PREFISSO_FIRMA = "data:image/png;base64,"


class Vietato(Exception):
    """Azione non consentita a questo utente o in questo stato."""


def crea_organizzazione(nome, admin_email, admin_password, admin_nome="Amministratore"):
    """Nuovo cliente del portale: organizzazione, dati azienda (mittente dei DDT) e primo admin.
    Lascia attiva la nuova organizzazione, così ciò che si crea dopo le appartiene."""
    nome, admin_email = (nome or "").strip(), (admin_email or "").strip().lower()
    if not nome or not admin_email or len(admin_password or "") < 8:
        raise Vietato("Servono ragione sociale, email e una password di almeno 8 caratteri.")
    if Utente.query.filter_by(email=admin_email).execution_options(tutte_le_organizzazioni=True).first():
        raise Vietato("Email già registrata.")
    org = Organizzazione(nome=nome, sigla=genera_sigla(nome))
    db.session.add(org)
    db.session.flush()
    g.org_id = org.id
    db.session.add(Azienda(ragione_sociale=nome))
    db.session.add(Vettore(nome=nome, proprio=True))  # il vettore della concessionaria, già pronto
    admin = Utente(nome=admin_nome, email=admin_email, ruolo="admin")
    admin.imposta_password(admin_password)
    db.session.add(admin)
    db.session.commit()
    return org


def firma_valida(dato):
    """Accetta solo PNG in data URL, max ~500 KB (firma timbrata). Restituisce la firma o None."""
    if not dato or not dato.startswith(PREFISSO_FIRMA) or len(dato) > 700_000:  # anche con il timbro
        return None
    try:
        grezzo = base64.b64decode(dato[len(PREFISSO_FIRMA):], validate=True)
    except (binascii.Error, ValueError):
        return None
    return dato if grezzo.startswith(b"\x89PNG") else None


def registra_azione(tipo, utente, veicolo, ddt=None, **dati):
    db.session.add(Azione(tipo=tipo, utente_id=utente.id, veicolo=veicolo, ddt=ddt, dati=dati))


def registra_evento(veicolo, tipo, utente, lat, lon, ddt=None):
    db.session.add(Evento(veicolo=veicolo, tipo=tipo, utente_id=utente.id, lat=lat, lon=lon, ddt=ddt))
    if lat is not None:
        veicolo.lat, veicolo.lon, veicolo.posizione_at = lat, lon, adesso()


def account_del_luogo(sede, utente):
    """True se l'utente "appartiene" a quel luogo: le sedi proprie sono della
    concessionaria (personale interno), quelle esterne del loro fornitore."""
    if sede is None or not sede.esterna:
        return utente.interno
    return utente.sede_id is not None and utente.sede_id == sede.id


def mittente(mov):
    """(ragione sociale, indirizzo, piva, firma) di chi emette il DDT: la concessionaria
    se l'auto parte da una sede propria, altrimenti il fornitore da cui parte."""
    sede = mov.da_sede
    if sede is not None and sede.esterna:
        return sede.nome, sede.indirizzo_completo, sede.piva, sede.firma
    a = Azienda.query.first()
    return (a.ragione_sociale, a.indirizzo_completo, a.piva, a.firma) if a else ("", "", "", None)


def rientro_libero(veicolo):
    """Il fornitore ha confermato la lavorazione completata: il DDT di rientro lo può emettere
    chiunque (il mittente sul documento resta il fornitore, luogo da cui l'auto parte)."""
    if veicolo.sede is None or not veicolo.sede.esterna:
        return False
    m = veicolo.ultimo_arrivo
    return (m is not None and m.lavorazione_completata_at is not None
            and m.sede_arrivo_effettiva.id == veicolo.sede.id)


def puo_emettere_dal_luogo(veicolo, utente):
    return account_del_luogo(veicolo.sede, utente) or rientro_libero(veicolo)


def puo_emettere(veicolo, utente):
    """La bolla di partenza la emette un account del luogo da cui l'auto parte; dal fornitore, a
    lavorazione completata, chiunque (rientro_libero). Un'auto in preparazione non si sposta; una
    pronta si sposta solo con l'ultimo DDT verso la sede di consegna (vedi puo_emettere_ultimo_ddt)."""
    return (veicolo.stato == "in_sede" and veicolo.movimento_predisposto is None
            and veicolo.fase not in ("preparazione", "pronta")
            and puo_emettere_dal_luogo(veicolo, utente))


def puo_emettere_ultimo_ddt(veicolo, utente):
    """Auto pronta con la sede di consegna indicata ma che si trova altrove: serve l'ultimo DDT."""
    return (veicolo.stato == "in_sede" and veicolo.fase == "pronta" and veicolo.movimento_predisposto is None
            and veicolo.consegna_sede_id is not None and veicolo.sede_id != veicolo.consegna_sede_id
            and account_del_luogo(veicolo.sede, utente))


def puo_predisporre(veicolo, utente):
    return puo_emettere(veicolo, utente) and (utente.gestione or not utente.interno)


def _emetti_ddt(movimento, utente):
    """Assegna il prossimo numero progressivo dell'anno e crea il DDT,
    con la firma predefinita del mittente apposta in automatico."""
    emesso = movimento.partenza_at or adesso()
    ultimo = db.session.query(func.max(Ddt.numero)).filter_by(anno=emesso.year).scalar() or 0
    ddt = Ddt(anno=emesso.year, numero=ultimo + 1, movimento=movimento, creato_da_id=utente.id,
              revisione=0, emesso_at=emesso, firma_mittente=mittente(movimento)[3])
    movimento.ddt = ddt
    ddt.dati_fissi = {"mittente": list(mittente(movimento)[:3])}
    _fissa_controparti(ddt)
    db.session.add(ddt)
    return ddt


def _fissa_controparti(ddt):
    """Copia sul DDT i dati attuali di destinatario e vettore (all'emissione o dopo una modifica)."""
    m = ddt.movimento
    dest = m.a_sede or db.session.get(Sede, m.a_sede_id)
    vt = m.vettore or db.session.get(Vettore, m.vettore_id)
    fissi = dict(ddt.dati_fissi or {})
    fissi["destinatario"] = [dest.nome, dest.indirizzo_completo or "", dest.piva or ""]
    fissi["vettore"] = [vt.nome, vt.indirizzo_completo or "", vt.piva or ""]
    ddt.dati_fissi = fissi


def dati_ddt(ddt):
    """(mittente, destinatario, vettore) come [nome, indirizzo, piva], fissati all'emissione."""
    fissi = ddt.dati_fissi or {}
    m = ddt.movimento
    return (fissi.get("mittente") or list(mittente(m)[:3]),
            fissi.get("destinatario") or [m.a_sede.nome, m.a_sede.indirizzo_completo, m.a_sede.piva],
            fissi.get("vettore") or [m.vettore.nome, m.vettore.indirizzo_completo, m.vettore.piva])


def crea_movimento(veicolo, dati, utente, firma_conducente=None, lat=None, lon=None,
                   predisponi_ore=None, partenza_at=None):
    """Parte subito (serve la firma del conducente) oppure, se predisponi_ore,
    crea un DDT da ritirare entro quel numero di ore."""
    if veicolo.stato != "in_sede":
        raise Vietato("Il veicolo non risulta in sede: impossibile avviare un movimento.")
    if veicolo.movimento_predisposto:
        raise Vietato("Per questo veicolo c'è già un DDT da ritirare.")
    if veicolo.fase == "preparazione":
        raise Vietato("L'auto è in preparazione per la consegna: non si sposta, non serve un DDT.")
    if veicolo.fase == "pronta" and dati["a_sede_id"] != veicolo.consegna_sede_id:
        raise Vietato("Un'auto pronta si sposta solo con l'ultimo DDT, verso la sede di consegna indicata.")
    if not puo_emettere_dal_luogo(veicolo, utente):
        raise Vietato(f"La bolla la emette {veicolo.sede.nome} con il suo account finché non conferma "
                      "la lavorazione completata: dopo, il rientro lo può emettere chiunque.")
    # si confrontano gli oggetti, non gli id: l'id si aggiorna solo al salvataggio
    if veicolo.sede is not None and dati["a_sede_id"] == veicolo.sede.id:
        raise Vietato("La destinazione è il luogo in cui l'auto si trova già.")
    if verso_il_cliente(dati["a_sede_id"], dati.get("causale")):
        controlla_targa_consegna(veicolo)
    if predisponi_ore and not puo_predisporre(veicolo, utente):
        raise Vietato("Solo l'ufficio può predisporre un DDT.")
    mov = Movimento(veicolo=veicolo, da_sede=veicolo.sede, **dati)
    if predisponi_ore:
        mov.stato = "predisposto"
        mov.ritiro_entro_at = adesso() + timedelta(hours=predisponi_ore)
        db.session.add(mov)
        db.session.flush()
        ddt = _emetti_ddt(mov, utente)
        registra_azione("predisposizione", utente, veicolo, ddt)
        return ddt

    if not firma_conducente:
        raise Vietato("Serve la firma di chi sposta il mezzo.")
    _in_partenza(mov, utente, firma_conducente, lat, lon, partenza_at)
    # Il DDT nasce PRIMA della partenza (obbligo DPR 472/1996)
    ddt = _emetti_ddt(mov, utente)
    _parti(mov, utente, lat, lon)
    return ddt


def _in_partenza(mov, utente, firma_conducente, lat, lon, partenza_at=None):
    mov.stato = "aperto"
    mov.partenza_at = partenza_at or adesso()
    mov.partenza_utente_id = utente.id
    mov.partenza_lat, mov.partenza_lon = lat, lon
    mov.firma_conducente = firma_conducente
    db.session.add(mov)
    db.session.flush()


def _parti(mov, utente, lat, lon):
    veicolo = mov.veicolo
    veicolo.stato, veicolo.sede = "in_viaggio", None
    registra_evento(veicolo, "partenza", utente, lat, lon, mov.ddt)
    registra_azione("partenza", utente, veicolo, mov.ddt)


MIN_MULTIPLO = 2


def crea_ddt_multiplo(veicoli, dati, utente, firma_conducente=None, lat=None, lon=None):
    """Un solo DDT per più auto che partono insieme dallo stesso luogo verso la stessa
    destinazione (es. bisarca). Ogni auto ha il suo movimento; il numero DDT è uno."""
    veicoli = list(dict.fromkeys(veicoli))
    if len(veicoli) < MIN_MULTIPLO:
        raise Vietato(f"Un DDT multiplo serve per almeno {MIN_MULTIPLO} auto.")
    origine = veicoli[0].sede
    if any(v.sede is not origine for v in veicoli):
        raise Vietato("Le auto di un DDT multiplo devono partire tutte dallo stesso luogo.")
    for v in veicoli:
        if not puo_emettere(v, utente):
            raise Vietato(f"{v.descrizione} ({v.vin[-8:]}) non può partire: non è in sede, ha già un DDT "
                          "da ritirare oppure la bolla la emette un altro account.")
    if origine is not None and dati["a_sede_id"] == origine.id:
        raise Vietato("La destinazione è il luogo in cui le auto si trovano già.")
    if verso_il_cliente(dati["a_sede_id"], dati.get("causale")):
        for v in veicoli:
            controlla_targa_consegna(v)
    if not firma_conducente:
        raise Vietato("Serve la firma di chi sposta i mezzi.")
    ora = adesso()
    movimenti = [Movimento(veicolo=v, da_sede=origine, **dati) for v in veicoli]
    db.session.add_all(movimenti)
    for m in movimenti:
        _in_partenza(m, utente, firma_conducente, lat, lon, ora)
    ddt = _emetti_ddt(movimenti[0], utente)
    for m in movimenti[1:]:
        m.ddt = ddt
    for m in movimenti:
        _parti(m, utente, lat, lon)
    return ddt


# ---------------------------------------------------------------- partenza col tocco NFC

# ---------------------------------------------------------------- vettori e adesivi della concessionaria

def vettore_proprio():
    """Il vettore della concessionaria attiva (mezzi propri), creato se manca; segue i dati aziendali."""
    v = Vettore.query.filter_by(proprio=True).first()
    if v is None:
        azienda = Azienda.query.first()
        org = db.session.get(Organizzazione, g.org_id) if g.get("org_id") else None
        nome = (azienda.ragione_sociale if azienda and azienda.ragione_sociale else (org.nome if org else "Concessionaria"))
        v = Vettore(nome=nome, proprio=True, piva=azienda.piva if azienda else "",
                    indirizzo=azienda.indirizzo if azienda else "")
        db.session.add(v)
        db.session.flush()
    return v


def allinea_vettore_proprio(azienda):
    """Dopo aver cambiato i dati aziendali, il vettore della concessionaria li riprende."""
    v = vettore_proprio()
    v.nome, v.attivo = azienda.ragione_sociale, True
    for campo in ("piva", "codice_fiscale", "indirizzo", "cap", "comune", "provincia", "stato", "lat", "lon"):
        setattr(v, campo, getattr(azienda, campo))


def allinea_sede_legale(azienda):
    """La sede legale della concessionaria è anche una destinazione (sede propria), creata e aggiornata
    dai Dati aziendali: stesso indirizzo e stessa posizione sulla mappa."""
    if not azienda.comune:
        return None
    s = Sede.query.filter_by(sede_legale=True).first()
    if s is None:
        s = Sede(tipo="propria", sede_legale=True, nome="Sede legale")
        db.session.add(s)
    for campo in ("indirizzo", "cap", "comune", "provincia", "stato", "lat", "lon"):
        setattr(s, campo, getattr(azienda, campo))
    s.attiva = True
    return s


def vettore_predefinito(utente):
    """Il vettore di chi guida, dall'azienda per cui lavora: per la concessionaria il suo vettore,
    per un fornitore un vettore con i dati della sua azienda (creato al primo uso)."""
    if utente.ruolo == "fornitore" and utente.sede is not None:
        v = Vettore.query.filter_by(sede_id=utente.sede_id).first()
        if v is None:
            s = utente.sede
            v = Vettore(nome=s.nome, piva=s.piva or "", codice_fiscale=s.codice_fiscale or "", indirizzo=s.indirizzo,
                        cap=s.cap, comune=s.comune, provincia=s.provincia, stato=s.stato, lat=s.lat, lon=s.lon,
                        sede_id=s.id)
            db.session.add(v)
            db.session.flush()
        return v
    return vettore_proprio()


def completa_vettore(utente):
    """Se chi guida non ha ancora un vettore, prende quello della sua azienda."""
    if utente.vettore is None or not utente.vettore.attivo:
        utente.vettore = vettore_predefinito(utente)
    return utente.vettore


LEGALI = {"SRL", "SPA", "SNC", "SAS", "SRLS", "SS", "SAPA", "SCARL", "COOP", "DI", "DEI", "DELLA", "DEL", "E", "&"}


def genera_sigla(nome):
    """Sigla dei codici degli adesivi dal nome (es. "Concessionaria Demo S.r.l." → CDEMO): iniziali delle
    parole significative, completata con l'ultima parola fino a 5 lettere; unica su tutto il portale."""
    import re
    import unicodedata
    testo = unicodedata.normalize("NFKD", nome or "").encode("ascii", "ignore").decode().upper()
    parole = [p for p in re.split(r"[^A-Z]+", testo.replace(".", "")) if p and p not in LEGALI] or ["GV"]
    base = "".join(p[0] for p in parole)[:5]
    if len(base) < 5:
        base = (base[:-1] + parole[-1])[:5] if len(parole) > 1 else parole[0][:5]
    base = base if len(base) >= 2 else (base + "GV")[:2]
    tutte = {o.sigla for o in Organizzazione.query.all() if o.sigla}
    sigla, n = base, 1
    while sigla in tutte:
        n += 1
        sigla = base[:5 - len(str(n))] + str(n)
    return sigla


def sigla_valida(sigla, org_id):
    """La sigla scelta in Dati aziendali: 2-5 lettere, non usata da un'altra concessionaria."""
    import re
    sigla = (sigla or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2,5}", sigla):
        raise Vietato("La sigla degli adesivi deve avere da 2 a 5 lettere (senza numeri né spazi).")
    altra = Organizzazione.query.filter(Organizzazione.sigla == sigla, Organizzazione.id != org_id).first()
    if altra:
        raise Vietato("Questa sigla è già usata da un'altra concessionaria: scegline un'altra.")
    return sigla


def nuovi_codici_adesivi(quanti):
    """Codici progressivi della concessionaria attiva: SIGLA-0001, SIGLA-0002… (si riparte dal più alto)."""
    org = db.session.get(Organizzazione, g.org_id)
    if not org.sigla:
        org.sigla = genera_sigla(org.nome)
    prefisso = org.sigla + "-"
    numeri = [int(t.codice[len(prefisso):]) for t in Tag.query.filter(Tag.codice.like(prefisso + "%"))
              .execution_options(tutte_le_organizzazioni=True) if t.codice[len(prefisso):].isdigit()]
    primo = max(numeri, default=0) + 1
    codici = [f"{prefisso}{n:04d}" for n in range(primo, primo + quanti)]
    for c in codici:
        db.session.add(Tag(codice=c))
    return codici


def profilo_guida_mancante(utente):
    """Cosa manca al profilo di guida per partire col solo tocco (vuoto = pronto).
    Il vettore non manca mai: si prende quello dell'azienda per cui si lavora."""
    completa_vettore(utente)
    mancano = []
    if utente.vettore is None or not utente.vettore.attivo:
        mancano.append("vettore")
    if utente.mezzo not in Movimento.MEZZI:
        mancano.append("modalità di trasporto")
    if not utente.firma:
        mancano.append("firma")
    return mancano


def causale_per(da_sede, a_sede):
    """La causale si ricava dal tipo di destinazione (e di partenza, per i rientri)."""
    if a_sede.tipo == "cliente":
        return "Consegna a cliente"
    if a_sede.esterna:
        return "Invio lavorazione"
    if a_sede.tipo == "esposizione":
        return "Conto esposizione"
    if a_sede.tipo in Sede.COMMERCIO:
        return "Conto visione"
    if da_sede is not None and da_sede.esterna:
        return "Reso lavorazione"
    if da_sede is not None and da_sede.tipo == "esposizione":
        return "Reso esposizione"
    if da_sede is not None and da_sede.tipo in Sede.COMMERCIO:
        return "Reso"
    return "Spostamento"


def destinazioni_rapide(veicolo, utente):
    """Destinazioni proponibili con un tocco: (più usate da questo utente, gruppi per tipo).
    Un'auto pronta va solo nella sede di consegna."""
    sedi = Sede.query.filter(Sede.attiva.is_(True), Sede.id != (veicolo.sede_id or 0))
    if veicolo.fase == "pronta":
        sedi = sedi.filter(Sede.id == veicolo.consegna_sede_id)
    sedi = sedi.order_by(Sede.tipo != "propria", Sede.tipo, Sede.nome).all()
    uso = dict(db.session.query(Movimento.a_sede_id, func.count())
               .filter(Movimento.partenza_utente_id == utente.id).group_by(Movimento.a_sede_id).all())
    preferite = sorted((s for s in sedi if uso.get(s.id)), key=lambda s: -uso[s.id])[:4]
    altre = [s for s in sedi if s not in preferite]
    return preferite, [(Sede.TIPI[t], list(gr)) for t, gr in groupby(altre, key=lambda s: s.tipo)]


TIPI_DESTINAZIONE_NUOVA = ("altro", "cliente", "carrozzeria", "officina", "allestitore", "esposizione", "concessionario",
                           "rivenditore")


def destinazione_scritta(nome, tipo="altro"):
    """Destinazione non in elenco, scritta da chi parte: se esiste già con quel nome si usa quella,
    altrimenti nasce un nuovo luogo. La sua posizione si fissa al primo arrivo (vedi _per_conto)."""
    from . import anagrafica
    nome = " ".join((nome or "").split())[:200]
    if len(nome) < 3:
        raise Vietato("Scrivi il nome della destinazione.")
    esistente = Sede.query.filter(func.lower(Sede.nome) == nome.lower()).first()
    if esistente is not None:
        if not esistente.attiva:
            raise Vietato(f"{esistente.nome} è disattivata in Gestione.")
        return esistente
    sede = Sede(nome=nome, tipo=tipo if tipo in TIPI_DESTINAZIONE_NUOVA else "altro")
    # come in Gestione: indirizzo trovato sulla mappa e dati fiscali (per un cliente basta il codice fiscale)
    anagrafica.leggi_indirizzo(sede)
    anagrafica.leggi_fiscali(sede, obbligatori=True, solo_cf=sede.tipo == "cliente")
    db.session.add(sede)
    db.session.flush()
    return sede


def parti_col_tocco(veicolo, a_sede, utente, lat=None, lon=None, causale=None):
    """"Inizia spostamento": si sceglie solo la destinazione, il resto viene dal profilo di guida.
    La lavorazione, se la destinazione è esterna, si indica dopo (entro la chiusura del DDT)."""
    mancano = profilo_guida_mancante(utente)
    if mancano:
        raise Vietato("Completa il tuo profilo di guida: manca " + ", ".join(mancano) + ".")
    if a_sede is None or not a_sede.attiva:
        raise Vietato("Scegli la destinazione.")
    # verso un concessionario/rivenditore si sceglie: conto visione (può tornare) o vendita
    if a_sede.tipo not in Sede.COMMERCIO or causale not in Movimento.CAUSALI_RIVENDITORE:
        causale = causale_per(veicolo.sede, a_sede)
    dati = {"a_sede_id": a_sede.id, "causale": causale,
            "vettore_id": utente.vettore_id, "conducente": utente.nome,
            "lavorazione": "" if a_sede.esterna else Movimento.NESSUNA, "lavorazione_note": "",
            "mezzo": utente.mezzo}
    ddt = crea_movimento(veicolo, dati, utente, firma_conducente=utente.firma, lat=lat, lon=lon)
    ddt.movimento.firma_salvata = True
    return ddt


def puo_prendere_in_carico(mov, utente):
    """Ritira chi guida: personale interno, oppure il fornitore di partenza o di arrivo."""
    if mov.stato != "predisposto":
        return False
    if utente.interno:
        return True
    return utente.sede_id is not None and utente.sede_id in (mov.da_sede_id, mov.a_sede_id)


def prendi_in_carico(mov, utente, conducente, firma_conducente, lat=None, lon=None, mezzo=""):
    """Chi ritira l'auto conferma il DDT già creato dall'ufficio, firma e parte."""
    if not puo_prendere_in_carico(mov, utente):
        raise Vietato("Non puoi prendere in carico questo DDT.")
    if mov.scaduto:
        raise Vietato("Il tempo per il ritiro è scaduto: chiedi all'ufficio di rinnovarlo.")
    if not conducente:
        raise Vietato("Indica chi sposta il mezzo.")
    if not firma_conducente:
        raise Vietato("Serve la firma di chi sposta il mezzo.")
    if not mov.mezzo:
        if mezzo not in Movimento.MEZZI:
            raise Vietato("Indica come viaggia l'auto (su strada, bisarca, carro attrezzi…).")
        mov.mezzo = mezzo
    mov.stato = "aperto"
    mov.conducente = conducente
    mov.partenza_at = adesso()
    mov.partenza_utente_id = utente.id
    mov.partenza_lat, mov.partenza_lon = lat, lon
    mov.firma_conducente = firma_conducente
    mov.veicolo.stato, mov.veicolo.sede = "in_viaggio", None
    registra_evento(mov.veicolo, "partenza", utente, lat, lon, mov.ddt)
    registra_azione("presa_in_carico", utente, mov.veicolo, mov.ddt)


def rinnova_ritiro(mov, ore=24):
    mov.ritiro_entro_at = adesso() + timedelta(hours=ore)


RAGGIO_ARRIVO_M = 200

NOMI_TOCCO = {"partenza": "Partenza", "tocco": "Tocco dell'adesivo", "arrivo": "Arrivo"}


def tocchi_viaggio(mov):
    """Ogni tocco registrato sull'auto durante il viaggio, con orario e minuti trascorsi:
    [{"nome", "at", "utente", "dal_precedente", "dalla_partenza"}] (timedelta)."""
    if mov.partenza_at is None:
        return []
    fine = mov.arrivo_at or adesso()
    eventi = (Evento.query.filter(Evento.veicolo_id == mov.veicolo_id, Evento.at >= mov.partenza_at,
                                  Evento.at <= fine, Evento.tipo.in_(list(NOMI_TOCCO)))
              .order_by(Evento.at, Evento.id).all())
    righe, prima = [], mov.partenza_at
    for e in eventi:
        righe.append({"nome": NOMI_TOCCO[e.tipo], "at": e.at, "utente": e.utente,
                      "dal_precedente": e.at - prima, "dalla_partenza": e.at - mov.partenza_at})
        prima = e.at
    return righe


def distanza_m(lat1, lon1, lat2, lon2):
    """Distanza in metri tra due punti (formula dell'emisenoverso)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(a))


def _distanza_leggibile(metri):
    return f"{metri:.0f} m" if metri < 1000 else f"{metri / 1000:.1f} km".replace(".", ",")


def puo_confermare_arrivo(mov, utente):
    """Chiude il DDT chi si trova entro RAGGIO_ARRIVO_M dalla destinazione (la posizione la controlla
    registra_arrivo); il fornitore destinatario anche da lontano."""
    return mov.stato == "aperto"


def destinatario_conferma_ovunque(mov, utente):
    """L'account del fornitore destinatario conferma la ricezione anche senza essere nel raggio."""
    return utente.ruolo == "fornitore" and utente.sede_id is not None and utente.sede_id == mov.a_sede_id


def _per_conto(mov, utente, lat, lon):
    """False se il telefono è nell'area della destinazione (o chi conferma è il fornitore destinatario).
    Per tutti gli altri, amministratore compreso, fuori dall'area l'arrivo non si può confermare."""
    dest = mov.a_sede
    if destinatario_conferma_ovunque(mov, utente):
        return False
    if lat is not None and dest.lat is not None:
        metri = distanza_m(lat, lon, dest.lat, dest.lon)
        if metri <= RAGGIO_ARRIVO_M:
            return False
        motivo = (f"Sei a {_distanza_leggibile(metri)} da {dest.nome}: il DDT si chiude solo entro "
                  f"{RAGGIO_ARRIVO_M} m dalla destinazione.")
    elif dest.lat is None and lat is not None:
        # destinazione nuova (scritta a mano o senza mappa): il primo arrivo ne fissa il punto
        dest.lat, dest.lon = lat, lon
        return False
    else:
        motivo = "Serve la posizione del telefono per confermare l'arrivo: attiva la localizzazione e riprova."
    raise Vietato(motivo)


def indica_lavorazione(ddt, utente, lavorazione, note):
    """Lavorazione aggiunta durante il viaggio (revisione tracciata del DDT)."""
    note = (note or "").strip()[:300]
    if lavorazione not in Movimento.LAVORAZIONI:
        raise Vietato("Scegli la lavorazione da fare.")
    if lavorazione != Movimento.NESSUNA and not note:
        raise Vietato("Scrivi i dettagli della lavorazione da fare.")
    nuovi = {campo: getattr(ddt.movimento, campo) for campo in CAMPI_MODIFICABILI}
    nuovi.update(lavorazione=lavorazione, lavorazione_note=note)
    return modifica_ddt(ddt, nuovi, utente)


def puo_modificare(ddt, utente):
    """Lo modifica chi l'ha emesso (account del mittente). Dopo l'arrivo solo ufficio e admin
    della concessionaria, e solo sui DDT emessi dalla concessionaria."""
    if ddt.annullato_at:
        return False
    m = ddt.movimento
    if not account_del_luogo(m.da_sede, utente) and ddt.creato_da_id != utente.id:
        return False
    return m.stato != "chiuso" or utente.gestione


def puo_vedere_veicolo(veicolo, utente):
    if utente.interno:
        return True
    if utente.sede_id is None:
        return False
    if veicolo.sede is not None and veicolo.sede.id == utente.sede_id and veicolo.stato == "in_sede":
        return True
    return any(m.ddt and puo_vedere(m.ddt, utente) and m.stato != "chiuso" for m in veicolo.movimenti)


def puo_vedere(ddt, utente):
    if utente.interno:
        return True
    m = ddt.movimento
    return utente.sede_id is not None and utente.sede_id in (m.da_sede_id, m.a_sede_id, m.arrivo_sede_id)


# campo del movimento -> (etichetta, come mostrarne il valore)
CAMPI_MODIFICABILI = {
    "a_sede_id": ("Destinazione", lambda v: db.session.get(Sede, v).nome if v else "—"),
    "causale": ("Causale", str),
    "vettore_id": ("Vettore", lambda v: db.session.get(Vettore, v).nome if v else "—"),
    "conducente": ("Conducente", lambda v: v or "—"),
    "lavorazione": ("Lavorazione", str),
    "lavorazione_note": ("Note lavorazione", lambda v: v or "—"),
    "mezzo": ("Modalità di trasporto", lambda v: v or "—"),
}


def modifica_ddt(ddt, nuovi, utente):
    """Applica le modifiche e salva una revisione con i soli campi cambiati.
    Restituisce la revisione, o None se non è cambiato nulla."""
    mov = ddt.movimento
    cambi = []
    for campo, (etichetta, mostra) in CAMPI_MODIFICABILI.items():
        prima, dopo = getattr(mov, campo), nuovi[campo]
        if prima != dopo:
            cambi.append({"campo": etichetta, "prima": mostra(prima), "dopo": mostra(dopo)})
            for m in ddt.movimenti or [mov]:
                setattr(m, campo, dopo)
    if not cambi:
        return None
    db.session.flush()
    for m in ddt.movimenti or [mov]:
        db.session.expire(m, ["a_sede", "vettore"])
    _fissa_controparti(ddt)
    if utente.ruolo == "admin":
        # l'amministratore corregge senza lasciare traccia: niente revisione, l'archivio tiene solo la versione nuova
        from . import archivio
        archivio.dimentica(ddt)
        return None
    ddt.revisione += 1
    rev = DdtRevisione(ddt=ddt, numero=ddt.revisione, utente_id=utente.id, modifiche=cambi)
    db.session.add(rev)
    return rev


def registra_arrivo(mov, utente, lat=None, lon=None, altrove_id=None, note=""):
    if not puo_confermare_arrivo(mov, utente):
        raise Vietato("Questo DDT non è in viaggio.")
    if not mov.lavorazione:
        raise Vietato("Prima di chiudere il DDT indica la lavorazione da fare.")
    # l'auto arriva solo nella destinazione indicata nel DDT: per un altro luogo serve un nuovo DDT
    if altrove_id and altrove_id != mov.a_sede_id:
        raise Vietato(f"L'arrivo si conferma solo a {mov.a_sede.nome}, la destinazione indicata nel DDT.")
    per_conto = _per_conto(mov, utente, lat, lon)
    if per_conto:
        lat = lon = None  # l'admin è altrove: l'auto prende le coordinate della destinazione
    sede_finale = mov.sede_arrivo_effettiva
    mov.note_arrivo = (note or "").strip()[:300]
    mov.arrivo_at, mov.arrivo_utente_id = adesso(), utente.id
    mov.arrivo_lat, mov.arrivo_lon = lat, lon
    mov.stato = "chiuso"
    destinatario_interno = sede_finale.tipo == "propria"
    mov.arrivo_per_conto = per_conto
    # firma predefinita del destinatario (per le sedi proprie, se manca, quella aziendale)
    azienda = Azienda.query.first()
    mov.firma_destinatario = sede_finale.firma or (
        azienda.firma if destinatario_interno and azienda else None)

    v = mov.veicolo
    tag_id = v.tag.id if v.tag else None
    if mov.causale in ("Consegna a cliente", "Vendita") or sede_finale.tipo == "cliente":
        _segna_consegnato(v, sede_finale)
    else:
        v.stato, v.sede = "in_sede", sede_finale

    # posizione: GPS del telefono se disponibile, altrimenti le coordinate della sede
    if lat is None and sede_finale.lat is not None:
        lat, lon = sede_finale.lat, sede_finale.lon
    registra_evento(v, "arrivo", utente, lat, lon, mov.ddt)

    # rientro da un fornitore esterno in una sede propria -> subito in preparazione per la consegna
    prep = None
    if destinatario_interno and mov.da_sede and mov.da_sede.esterna and v.stato == "in_sede":
        prep = avvia_preparazione(v, mov)
    db.session.flush()
    registra_azione("arrivo", utente, v, mov.ddt, tag_id=tag_id, prep_id=prep.id if prep else None)


def _segna_consegnato(v, sede=None):
    """Auto consegnata al cliente: va in archivio e l'adesivo si stacca e torna disponibile."""
    v.stato, v.consegnato_at = "consegnato", adesso()
    if sede is not None:
        v.sede = sede
    prep = v.preparazione_attiva
    if prep:
        prep.completata_at = adesso()
    if v.tag:
        v.tag.abbinato_at = None
        v.tag.veicolo = None


def puo_consegnare(v, utente):
    """Si consegna un'auto pronta, già arrivata nella sede di consegna indicata, senza danni aperti."""
    return (utente.interno and v.stato == "in_sede" and v.fase in ("pronta", "in_sede") and not v.danni_aperti
            and v.consegna_sede_id is not None and v.sede_id == v.consegna_sede_id)


def imposta_consegna(v, utente, sede):
    """Sede (propria) in cui l'auto pronta verrà consegnata al cliente. Il giorno della consegna si
    fissa con il cliente in un altro portale: Getvingo calendarizza solo le preparazioni."""
    if not utente.interno:
        raise Vietato("La consegna la organizza la concessionaria.")
    if v.fase != "pronta":
        raise Vietato("La sede di consegna si indica quando l'auto è pronta per la consegna.")
    if sede is None or sede.tipo != "propria" or not sede.attiva:
        raise Vietato("Scegli una sede della concessionaria.")
    # anche l'id subito: chi controlla la sede nella stessa operazione (es. consegna) lo trova già
    v.consegna_sede, v.consegna_sede_id, v.consegna_prevista_at = sede, sede.id, None


def verso_il_cliente(a_sede_id, causale):
    """Il DDT porta l'auto al cliente finale."""
    a_sede = db.session.get(Sede, a_sede_id) if a_sede_id else None
    return causale == "Consegna a cliente" or (a_sede is not None and a_sede.tipo == "cliente")


def controlla_targa_consegna(v):
    """Al cliente finale l'auto va solo targata (consegna in concessionaria o DDT verso il cliente)."""
    if not v.targa:
        raise Vietato(f"{v.descrizione} ({v.vin[-8:]}) non ha la targa: inseriscila prima della consegna al cliente.")


def consegna_al_cliente(v, utente):
    """Il cliente ritira l'auto in concessionaria (nessun trasporto, nessun DDT)."""
    controlla_targa_consegna(v)
    if v.danni_aperti:
        raise Vietato("L'auto ha un danno da verificare: risolvilo prima della consegna.")
    if v.consegna_sede_id is None:
        raise Vietato("Indica prima in quale sede verrà consegnata.")
    if v.sede_id != v.consegna_sede_id:
        raise Vietato(f"L'auto deve prima arrivare a {v.consegna_sede.nome} con l'ultimo DDT.")
    if not puo_consegnare(v, utente):
        raise Vietato("Si consegna solo un'auto pronta, presso una sede della concessionaria.")
    tag_id = v.tag.id if v.tag else None
    prep = v.preparazione_attiva
    _segna_consegnato(v)
    registra_evento(v, "consegna", utente, None, None)
    registra_azione("consegna", utente, v, tag_id=tag_id, prep_chiusa=prep.id if prep else None)


def puo_completare_lavorazione(mov, utente):
    if mov.stato != "chiuso" or not mov.con_lavorazione or mov.lavorazione_completata_at:
        return False
    sede = mov.sede_arrivo_effettiva
    if not sede.esterna or mov.veicolo.sede is not sede:
        return False
    # solo l'azienda esterna che fa il lavoro: la concessionaria non può chiuderlo al suo posto
    return utente.sede_id is not None and utente.sede_id == sede.id


ORE_RITIRO_RIENTRO = 72


def completa_lavorazione(mov, utente, note="", prepara_rientro=True):
    """Il fornitore chiude la lavorazione e, di norma, prepara già il DDT di rientro
    (lui è il mittente): la concessionaria deve solo andare a prendere l'auto."""
    if not puo_completare_lavorazione(mov, utente):
        raise Vietato("La fine lavorazione la conferma il fornitore che ha l'auto.")
    mov.lavorazione_completata_at = adesso()
    mov.lavorazione_completata_da_id = utente.id
    mov.lavorazione_note_fine = (note or "").strip()[:300]
    if not prepara_rientro or mov.veicolo.movimento_predisposto:
        registra_azione("lavorazione_completata", utente, mov.veicolo, mov.ddt)
        return None
    ritorno = mov.da_sede if mov.da_sede is not None and mov.da_sede.tipo == "propria" else \
        Sede.query.filter_by(tipo="propria", attiva=True).order_by(Sede.id).first()
    if ritorno is None:
        return None
    dati = dict(a_sede_id=ritorno.id, causale="Reso lavorazione", vettore_id=mov.vettore_id,
                conducente="", lavorazione=Movimento.NESSUNA, lavorazione_note="")
    rientro = crea_movimento(mov.veicolo, dati, utente, predisponi_ore=ORE_RITIRO_RIENTRO)
    db.session.flush()
    # la predisposizione automatica fa parte di questa azione: si ripristinano insieme
    ultima = Azione.query.filter_by(veicolo_id=mov.veicolo_id).order_by(Azione.id.desc()).first()
    if ultima is not None and ultima.tipo == "predisposizione":
        db.session.delete(ultima)
    registra_azione("lavorazione_completata", utente, mov.veicolo, mov.ddt, rientro_ddt_id=rientro.id)
    return rientro


def annulla_ddt(ddt, utente, motivo):
    """'Cancellazione' riservata all'admin: il DDT sparisce dagli elenchi ma resta
    in archivio (numero non riutilizzabile). Se l'auto era in viaggio torna all'origine."""
    if utente.ruolo != "admin":
        raise Vietato("Solo un amministratore può cancellare un DDT.")
    if ddt.annullato_at:
        raise Vietato("DDT già cancellato.")
    if not (motivo or "").strip():
        raise Vietato("Indica il motivo della cancellazione.")
    _annulla(ddt, utente, motivo)
    for m in ddt.movimenti or [ddt.movimento]:
        registra_azione("annullamento", utente, m.veicolo, ddt)


def elimina_ddt(ddt, utente):
    """Eliminazione vera, solo admin, motivo non richiesto: il DDT sparisce dal database e dall'archivio
    come se non fosse mai stato emesso (scelta della concessionaria; la numerazione può avere un buco).
    Se l'auto era in viaggio torna "in sede" nel luogo di partenza."""
    from . import archivio
    from .models import Sollecito
    if utente.ruolo != "admin":
        raise Vietato("Solo un amministratore può eliminare un DDT.")
    movimenti = list(ddt.movimenti or [ddt.movimento])
    for m in movimenti:
        if m.stato == "aperto":
            m.veicolo.stato, m.veicolo.sede = "in_sede", m.da_sede
    archivio.dimentica(ddt)
    for r in list(ddt.revisioni):
        db.session.delete(r)
    Azione.query.filter_by(ddt_id=ddt.id).delete()
    Evento.query.filter_by(ddt_id=ddt.id).update({"ddt_id": None})
    ids = [m.id for m in movimenti]
    Sollecito.query.filter(Sollecito.movimento_id.in_(ids)).delete(synchronize_session=False)
    Preparazione.query.filter(Preparazione.movimento_id.in_(ids)).update({"movimento_id": None}, synchronize_session=False)
    Danno.query.filter(Danno.movimento_id.in_(ids)).update({"movimento_id": None}, synchronize_session=False)
    for m in movimenti:
        m.ddt_id = None
    db.session.flush()
    db.session.delete(ddt)
    db.session.flush()
    for m in movimenti:
        db.session.delete(m)


def elimina_veicolo(v, utente, demo=False):
    """Elimina l'auto e tutto ciò che la riguarda; l'adesivo torna libero.
    Con DDT emessi solo in modalità demo: in produzione la numerazione fiscale non può avere buchi.
    Restituisce i nomi dei file foto da cancellare dal disco."""
    if utente.ruolo != "admin":
        raise Vietato("Solo un amministratore può eliminare un'auto.")
    ddt_emessi = [m.ddt for m in v.movimenti if m.ddt]
    if any(d.multiplo for d in ddt_emessi):
        raise Vietato("L'auto è in un DDT multiplo insieme ad altre: non si può eliminare.")
    if ddt_emessi and not demo:
        raise Vietato("L'auto ha DDT emessi: non si può eliminare (i DDT restano in archivio). "
                      "Se è stata consegnata o venduta, segnala la consegna.")
    if v.tag:
        v.tag.veicolo_id = None
    foto = []
    for d in v.danni:
        foto += [f.file for f in d.foto]
        for f in d.foto:
            db.session.delete(f)
        db.session.delete(d)
    for p in v.preparazioni:
        for f in p.fasi:
            db.session.delete(f)
        db.session.delete(p)
    Azione.query.filter_by(veicolo_id=v.id).delete()
    for e in v.eventi:
        db.session.delete(e)
    for d in ddt_emessi:
        for r in d.revisioni:
            db.session.delete(r)
        db.session.delete(d)
    for m in v.movimenti:
        db.session.delete(m)
    db.session.flush()
    db.session.delete(v)
    return foto


def _annulla(ddt, utente, motivo):
    for mov in ddt.movimenti or [ddt.movimento]:
        if mov.stato == "aperto":
            mov.veicolo.stato, mov.veicolo.sede = "in_sede", mov.da_sede
    ddt.annullato_at, ddt.annullato_da_id = adesso(), utente.id
    ddt.motivo_annullamento = motivo.strip()[:300]


# Calendario della preparazione: slot di 30 minuti, lun-ven 8:30-12:30 e 14:30-18:30 (8 + 8 slot al giorno)
GIORNI_PER_PREPARARE = 10
SLOT_MINUTI = 30
FASCE_PREPARAZIONE = ((time(8, 30), time(12, 30)), (time(14, 30), time(18, 30)))


def pasqua(anno):
    """Domenica di Pasqua (calendario gregoriano, algoritmo di Meeus/Jones/Butcher)."""
    a, b, c = anno % 19, anno // 100, anno % 100
    d, e = divmod(b, 4)
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 19 * l) // 433
    mese = (h + l - 7 * m + 90) // 25
    return date(anno, mese, (h + l - 7 * m + 33 * mese + 19) % 32)


FESTE_FISSE = {(1, 1): "Capodanno", (1, 6): "Epifania", (4, 25): "Festa della Liberazione",
               (5, 1): "Festa del lavoro", (6, 2): "Festa della Repubblica", (8, 15): "Ferragosto",
               (11, 1): "Ognissanti", (12, 8): "Immacolata", (12, 25): "Natale", (12, 26): "Santo Stefano"}


def festivita(giorno):
    """Nome della festività nazionale italiana di quel giorno (None se non è festa)."""
    if (giorno.month, giorno.day) in FESTE_FISSE:
        return FESTE_FISSE[(giorno.month, giorno.day)]
    if giorno == pasqua(giorno.year) + timedelta(days=1):
        return "Lunedì dell'Angelo"
    return None


def giorni_chiusi(dal, al):
    """Giorni chiusi scelti in Gestione tra due date: {data: motivo}."""
    return {gc.data: gc.motivo or "Chiuso" for gc in
            GiornoChiuso.query.filter(GiornoChiuso.data >= dal, GiornoChiuso.data <= al)}


def motivo_chiusura(giorno, chiusi=None):
    """Perché in quel giorno non si lavora (weekend, festa, chiusura), oppure None."""
    if giorno.weekday() >= 5:
        return "Sabato" if giorno.weekday() == 5 else "Domenica"
    chiusi = giorni_chiusi(giorno, giorno) if chiusi is None else chiusi
    return festivita(giorno) or chiusi.get(giorno)


def _slot_del_giorno(giorno, chiusi=None):
    """Inizio di ogni slot di un giorno lavorativo (vuoto nel weekend, nelle feste e nei giorni chiusi)."""
    if motivo_chiusura(giorno, chiusi):
        return []
    slot = []
    for inizio, fine in FASCE_PREPARAZIONE:
        t = datetime.combine(giorno, inizio)
        while t < datetime.combine(giorno, fine):
            slot.append(t)
            t += timedelta(minutes=SLOT_MINUTI)
    return slot


def e_uno_slot(momento):
    """True se è l'inizio di uno slot di 30 minuti in un giorno lavorativo: lun–ven 8:30–12:30 e
    14:30–18:30 (8 slot la mattina e 8 il pomeriggio), feste e giorni chiusi esclusi."""
    return momento is not None and momento in _slot_del_giorno(momento.date())


FUORI_SLOT = ("La preparazione dura 30 minuti e si fissa solo in uno slot: lun–ven dalle 8:30 alle 12:30 "
              "o dalle 14:30 alle 18:30, ogni mezz'ora (feste e giorni chiusi esclusi).")


def slot_occupati_preparazione(escludi=None):
    """Slot già presi dalle preparazioni in corso: {inizio dello slot: preparazione}."""
    return {p.pronta_entro_at: p for p in Preparazione.query.filter(
        Preparazione.completata_at.is_(None), Preparazione.pronta_entro_at.isnot(None)) if p is not escludi}


def primo_slot_libero(da, escludi=None):
    """Il primo slot che inizia da `da` in poi (giorni lavorativi) non già occupato da un'altra
    preparazione in corso."""
    occupati = slot_occupati_preparazione(escludi)
    giorno = da.date()
    chiusi = giorni_chiusi(giorno, giorno + timedelta(days=366))
    for _ in range(366):
        for s in _slot_del_giorno(giorno, chiusi):
            if s >= da and s not in occupati:
                return s
        giorno += timedelta(days=1)
    raise Vietato("Calendario della preparazione pieno per il prossimo anno.")


def calendarizza(prep, da=None):
    """Preparazione avviata: in calendario entro 10 giorni, nel primo slot utile (se quel giorno non è
    lavorativo si va al primo successivo; se lo slot è preso, al successivo libero)."""
    prep.pronta_entro_at = primo_slot_libero((da or adesso()) + timedelta(days=GIORNI_PER_PREPARARE), escludi=prep)
    return prep.pronta_entro_at


def avvia_preparazione(veicolo, movimento=None):
    if Preparazione.query.filter_by(veicolo_id=veicolo.id, completata_at=None).first():
        return None
    prep = Preparazione(veicolo=veicolo, movimento=movimento)
    calendarizza(prep)
    db.session.add(prep)
    return prep


FASI_BLOCCATE_DA_DANNI = ("Pronta",)  # con un danno aperto si lava, ma non si segna pronta


def bloccata_da_danni(prep):
    """True se il prossimo passo è vietato finché l'auto ha danni da verificare."""
    return prep.prossima_fase in FASI_BLOCCATE_DA_DANNI and bool(prep.veicolo.danni_aperti)


MINUTI_CONFERMA_TOCCO = 10  # per quanto vale un tocco dell'adesivo per chiudere un passo della preparazione


def _controlla_passo(prep):
    if prep.completata_at:
        raise Vietato("Preparazione già completata.")
    if bloccata_da_danni(prep):
        raise Vietato(f"{prep.veicolo.descrizione} ha un danno da verificare: finché non è risolto "
                      "non si può segnare pronta per la consegna.")


def avanza_preparazione(prep, utente):
    _controlla_passo(prep)
    fase = PreparazioneFase(preparazione=prep, indice=len(prep.fasi), utente_id=utente.id)
    db.session.add(fase)
    db.session.flush()
    if len(prep.fasi) == len(Preparazione.FASI):
        prep.completata_at = adesso()
    registra_azione("preparazione_fase", utente, prep.veicolo, fase_id=fase.id, prep_id=prep.id)


def puo_gestire_lavorazione(mov, utente):
    """Stima e risposte ai solleciti: la concessionaria o il fornitore che ha l'auto."""
    return utente.interno or (utente.sede_id is not None and utente.sede_id == mov.sede_arrivo_effettiva.id)


def imposta_stima(mov, utente, giorni):
    if not puo_gestire_lavorazione(mov, utente):
        raise Vietato("La stima la indica la concessionaria o il fornitore che ha l'auto.")
    if not mov.con_lavorazione or mov.lavorazione_completata_at:
        raise Vietato("La lavorazione è già finita.")
    if not giorni or not 1 <= giorni <= 120:
        raise Vietato("Indica i giorni di lavorazione stimati (da 1 a 120).")
    mov.lavorazione_stima_giorni = giorni


def puo_sollecitare(mov, utente):
    """Tempo stimato finito, nessuna fine lavorazione e nessun DDT di rientro pronto: si chiede perché."""
    return (utente.interno and mov.stato == "chiuso" and mov.con_lavorazione and mov.giorni_ritardo > 0
            and mov.veicolo.sede is not None and mov.veicolo.sede.id == mov.sede_arrivo_effettiva.id
            and mov.veicolo.movimento_predisposto is None and mov.sollecito_aperto is None)


def sollecita(mov, utente, messaggio=""):
    if not puo_sollecitare(mov, utente):
        raise Vietato("Si sollecita solo una lavorazione oltre il tempo stimato, senza una richiesta già aperta.")
    v = mov.veicolo
    testo = (messaggio or "").strip()[:500] or (
        f"{v.descrizione} (telaio …{v.vin[-8:]}): la lavorazione “{mov.lavorazione}” doveva finire il "
        f"{mov.lavorazione_prevista_at:%d/%m}. Ci indicate il motivo del ritardo e quando sarà pronta?")
    s = Sollecito(movimento=mov, utente_id=utente.id, messaggio=testo)
    db.session.add(s)
    return s


def rispondi_sollecito(sollecito, utente, risposta, nuova_stima=None):
    mov = sollecito.movimento
    if utente.interno or not puo_gestire_lavorazione(mov, utente):
        raise Vietato("Risponde il fornitore che ha l'auto.")
    risposta = (risposta or "").strip()[:500]
    if not risposta:
        raise Vietato("Scrivi il motivo del ritardo.")
    if nuova_stima:
        giorni_da_arrivo = (adesso().date() - mov.arrivo_at.date()).days + nuova_stima
        imposta_stima(mov, utente, giorni_da_arrivo)
        sollecito.nuova_stima_giorni = nuova_stima
    sollecito.risposta, sollecito.risposta_at, sollecito.risposta_utente_id = risposta, adesso(), utente.id


def presso_fornitori(solo_sede_id=None, lavorazione=None):
    """[(sede, [(veicolo, movimento di arrivo)])] solo per i fornitori che hanno
    almeno un'auto in questo momento (le auto rientrate non compaiono)."""
    query = (Veicolo.query.join(Sede, Veicolo.sede_id == Sede.id)
             .filter(Veicolo.stato == "in_sede", Sede.tipo.notin_(("propria", "cliente"))))
    if solo_sede_id:
        query = query.filter(Sede.id == solo_sede_id)
    veicoli = sorted(query.all(), key=lambda v: (v.sede.nome, v.id))
    risultato = []
    for sede, gruppo in groupby(veicoli, key=lambda v: v.sede):
        voci = [(v, v.ultimo_arrivo) for v in gruppo]
        if lavorazione:
            voci = [x for x in voci if x[1] and x[1].lavorazione == lavorazione]
        if not voci:
            continue
        # prima le auto ferme da più tempo
        voci.sort(key=lambda x: -((x[1].giorni_lavorazione or 0) if x[1] else 0))
        risultato.append((sede, voci))
    return risultato


def ultimo_vettore_id(utente):
    m = (Movimento.query.filter_by(partenza_utente_id=utente.id)
         .order_by(Movimento.id.desc()).first())
    return m.vettore_id if m else None


# ---------------------------------------------------------------- danni

def segnala_danno(veicolo, utente, zona, descrizione, foto, movimento=None):
    """foto: nomi dei file già salvati nella cartella foto."""
    if zona not in Danno.ZONE:
        raise Vietato("Indica in che zona dell'auto è il danno.")
    if not (descrizione or "").strip():
        raise Vietato("Descrivi il danno in poche parole.")
    if not foto:
        raise Vietato("Allega almeno una foto del danno.")
    danno = Danno(veicolo=veicolo, movimento=movimento, contesto="arrivo" if movimento else "piazzale",
                  zona=zona, descrizione=descrizione.strip()[:500], segnalato_da_id=utente.id)
    db.session.add(danno)
    for nome in foto:
        db.session.add(DannoFoto(danno=danno, file=nome))
    db.session.flush()
    registra_azione("danno", utente, veicolo, movimento.ddt if movimento else None, danno_id=danno.id)
    return danno


def risolvi_danno(danno, utente, nota="", foto=()):
    """foto: nomi dei file (già salvati) che mostrano il danno riparato. Almeno una è obbligatoria."""
    if not utente.interno:
        raise Vietato("Solo la concessionaria può chiudere una segnalazione di danno.")
    if not danno.aperto:
        raise Vietato("Il danno è già stato chiuso.")
    if not foto:
        raise Vietato("Per segnarlo risolto serve almeno una foto dell'auto riparata.")
    for nome in foto:
        db.session.add(DannoFoto(danno=danno, file=nome, risoluzione=True))
    danno.risolto_at, danno.risolto_da_id = adesso(), utente.id
    danno.nota_risoluzione = (nota or "").strip()[:300]
    registra_azione("danno_risolto", utente, danno.veicolo, danno_id=danno.id)


# ---------------------------------------------------------------- ripristino (admin)

AZIONI_DANNO = ("danno", "danno_risolto")


# su un DDT multiplo queste azioni riguardano tutte le auto insieme: si ripristinano insieme
AZIONI_COLLETTIVE = ("partenza", "arrivo", "annullamento")


def _gruppo(azione):
    """L'azione e, se collettiva, le gemelle sulle altre auto dello stesso DDT."""
    if azione.tipo not in AZIONI_COLLETTIVE or azione.ddt_id is None:
        return [azione]
    return (Azione.query.filter_by(ddt_id=azione.ddt_id, tipo=azione.tipo, ripristinata_at=None)
            .order_by(Azione.id).all()) or [azione]


def _ultima_su_veicolo(azione):
    ultima = (Azione.query.filter(Azione.veicolo_id == azione.veicolo_id, Azione.ripristinata_at.is_(None),
                                  Azione.tipo.notin_(AZIONI_DANNO))
              .order_by(Azione.id.desc()).first())
    return ultima is not None and ultima.id == azione.id


def puo_ripristinare(azione, utente):
    """Solo l'admin; per gli spostamenti solo l'ultima azione fatta sull'auto (su tutte le auto,
    per un DDT multiplo), così stati e DDT restano coerenti. I danni si ripristinano sempre."""
    if utente.ruolo != "admin" or azione.ripristinata_at:
        return False
    if azione.tipo in AZIONI_DANNO:
        return True
    return all(_ultima_su_veicolo(a) for a in _gruppo(azione))


def _riaggancia_tag(v, tag_id):
    tag = db.session.get(Tag, tag_id) if tag_id else None
    if tag is not None and tag.veicolo_id is None:
        tag.veicolo, tag.abbinato_at = v, adesso()


def _movimento_di(ddt, veicolo_id):
    if ddt is None:
        return None
    return next((m for m in ddt.movimenti if m.veicolo_id == veicolo_id), ddt.movimento)


def ripristina(azione, utente):
    if not puo_ripristinare(azione, utente):
        raise Vietato("Si può ripristinare solo l'ultima azione fatta su quest'auto.")
    gruppo = _gruppo(azione)
    d = azione.ddt
    motivo = f"Ripristino di {utente.nome}"

    if azione.tipo in ("partenza", "predisposizione"):
        _annulla(d, utente, motivo)
    elif azione.tipo == "annullamento":
        d.annullato_at = d.annullato_da_id = None
        d.motivo_annullamento = ""
        for m in d.movimenti or [d.movimento]:
            if m.stato == "aperto":
                m.veicolo.stato, m.veicolo.sede = "in_viaggio", None
    else:
        for a in gruppo:
            _ripristina_su_veicolo(a, utente, motivo)
    for a in gruppo:
        a.ripristinata_at, a.ripristinata_da_id = adesso(), utente.id


def _ripristina_su_veicolo(azione, utente, motivo):
    v, d, dati = azione.veicolo, azione.ddt, azione.dati or {}
    m = _movimento_di(d, v.id)

    if azione.tipo == "presa_in_carico":
        m.stato, m.partenza_at, m.partenza_utente_id = "predisposto", None, None
        m.partenza_lat = m.partenza_lon = m.firma_conducente = None
        v.stato, v.sede = "in_sede", m.da_sede
    elif azione.tipo == "arrivo":
        m.stato, m.arrivo_at, m.arrivo_utente_id = "aperto", None, None
        m.arrivo_lat = m.arrivo_lon = m.firma_destinatario = None
        m.arrivo_sede, m.note_arrivo, m.arrivo_per_conto = None, "", False
        v.stato, v.sede, v.consegnato_at = "in_viaggio", None, None
        _riaggancia_tag(v, dati.get("tag_id"))
        prep = db.session.get(Preparazione, dati["prep_id"]) if dati.get("prep_id") else None
        if prep is not None:
            for f in list(prep.fasi):
                db.session.delete(f)
            db.session.delete(prep)
    elif azione.tipo == "lavorazione_completata":
        m.lavorazione_completata_at = m.lavorazione_completata_da_id = None
        m.lavorazione_note_fine = ""
        rientro = db.session.get(Ddt, dati["rientro_ddt_id"]) if dati.get("rientro_ddt_id") else None
        if rientro is not None and not rientro.annullato_at and rientro.movimento.stato == "predisposto":
            _annulla(rientro, utente, motivo)
    elif azione.tipo == "preparazione_fase":
        fase = db.session.get(PreparazioneFase, dati.get("fase_id"))
        if fase is not None:
            fase.preparazione.completata_at = None
            db.session.delete(fase)
    elif azione.tipo == "consegna":
        v.stato, v.consegnato_at = "in_sede", None
        _riaggancia_tag(v, dati.get("tag_id"))
        prep = db.session.get(Preparazione, dati["prep_chiusa"]) if dati.get("prep_chiusa") else None
        if prep is not None:
            prep.completata_at = None
    elif azione.tipo == "danno":
        db.session.get(Danno, dati["danno_id"]).rimosso = True
    elif azione.tipo == "danno_risolto":
        danno = db.session.get(Danno, dati["danno_id"])
        danno.risolto_at = danno.risolto_da_id = None
        danno.nota_risoluzione = ""
        for f in danno.foto_risoluzione:  # il danno riaperto torna senza foto di conferma
            db.session.delete(f)


def azione_annullabile(utente, veicolo_id, tipo, ddt_id=None, **dati):
    """L'azione di quel tipo (e di quel DDT / danno / preparazione) se l'admin può annullarla
    in quel punto della pagina; altrimenti None."""
    if utente is None or utente.ruolo != "admin":
        return None
    q = Azione.query.filter_by(veicolo_id=veicolo_id, tipo=tipo, ripristinata_at=None)
    if ddt_id is not None:
        q = q.filter_by(ddt_id=ddt_id)
    for azione in q.order_by(Azione.id.desc()):
        if all((azione.dati or {}).get(k) == v for k, v in dati.items()):
            return azione if puo_ripristinare(azione, utente) else None
    return None


def motivo_no_preparazione(v, utente):
    """Perché l'auto non può entrare in preparazione (None = può)."""
    if not utente.interno:
        return "Solo la concessionaria può mettere un'auto in preparazione."
    if v.stato != "in_sede" or v.sede is None or v.sede.tipo != "propria":
        return "L'auto deve trovarsi in una sede della concessionaria."
    if v.fase in ("preparazione", "pronta"):
        return "L'auto è già in preparazione o pronta."
    if v.movimento_predisposto is not None:
        return "C'è un DDT da ritirare per quest'auto: annullalo prima."
    return None


def metti_in_preparazione(v, utente, pronta_entro=None):
    """Auto che non va da nessun fornitore: entra direttamente in preparazione per la consegna.
    pronta_entro: quando deve essere pronta (compare nel calendario della preparazione)."""
    motivo = motivo_no_preparazione(v, utente)
    if motivo:
        raise Vietato(motivo)
    prep = avvia_preparazione(v)  # già in calendario nel primo slot utile tra 10 giorni
    if prep is not None and pronta_entro is not None:
        if not e_uno_slot(pronta_entro):
            raise Vietato(FUORI_SLOT)
        if pronta_entro in slot_occupati_preparazione(escludi=prep):
            raise Vietato(f"Lo slot {pronta_entro:%d/%m %H:%M} è già preso da un'altra preparazione: scegline un altro.")
        prep.pronta_entro_at = pronta_entro  # data scelta a mano dall'ufficio
    return prep


def fissa_scadenza(prep, pronta_entro):
    """L'orario della preparazione si fissa una volta sola: poi non si sposta (se si sfora, è in ritardo)."""
    if prep.pronta_entro_at is not None:
        raise Vietato("L'orario della preparazione è già fissato e non si può spostare.")
    if pronta_entro is None:
        raise Vietato("Scegli il giorno e l'orario.")
    if not e_uno_slot(pronta_entro):
        raise Vietato(FUORI_SLOT)
    if pronta_entro in slot_occupati_preparazione(escludi=prep):
        raise Vietato("Questo slot è già preso da un'altra preparazione: scegline un altro.")
    prep.pronta_entro_at = pronta_entro


def giorni_con_slot(da=None, giorni_lavorativi=15):
    """I prossimi giorni lavorativi con i loro slot di 30 minuti ancora futuri (per scegliere l'orario)."""
    da = da or adesso()
    giorno, out = da.date(), []
    chiusi = giorni_chiusi(giorno, giorno + timedelta(days=120))
    while len(out) < giorni_lavorativi and giorno < da.date() + timedelta(days=120):
        slot = [s for s in _slot_del_giorno(giorno, chiusi) if s >= da]
        if slot:
            out.append((giorno, slot))
        giorno += timedelta(days=1)
    return out


# ---------------------------------------------------------------- stima del viaggio

VELOCITA_KMH = {"Su strada (guidata)": 48, "Bisarca": 42, "Carro attrezzi": 45, "Furgone / carrello": 45, "Altro": 45}
FATTORE_STRADA = 1.3  # la strada è più lunga della linea d'aria (media per percorsi locali e provinciali)


def traffico(quando):
    """Fattore di traffico tipico dell'orario (senza servizi a pagamento non c'è il traffico in tempo reale)."""
    ora = quando.hour + quando.minute / 60
    if quando.weekday() >= 5:
        return 1.0, "fine settimana"
    if 7.5 <= ora < 9.5 or 17 <= ora < 19.5:
        return 1.35, "ora di punta"
    if 12.5 <= ora < 14.5:
        return 1.12, "pausa pranzo"
    if ora >= 21 or ora < 6:
        return 0.9, "notte"
    return 1.0, "traffico normale"


def stima_viaggio(mov):
    """Distanza e tempo stimati da partenza a destinazione: {km, minuti, arrivo, traffico} o None
    se manca la posizione di uno dei due luoghi."""
    da, a = mov.da_sede, mov.a_sede
    if da is None or a is None or da.lat is None or a.lat is None:
        return None
    km = distanza_m(da.lat, da.lon, a.lat, a.lon) / 1000 * FATTORE_STRADA
    partenza = mov.partenza_at or adesso()
    fattore, nota = traffico(partenza)
    minuti = max(5, round(km / VELOCITA_KMH.get(mov.mezzo, 45) * 60 * fattore + 5))  # + manovre e parcheggio
    return {"km": round(km, 1) if km < 10 else round(km), "minuti": minuti, "traffico": nota,
            "arrivo": partenza + timedelta(minutes=minuti)}


# ---------------------------------------------------------------- eliminazione di account e fornitori

def _riferimenti(oggetto, salvo=()):
    """Quante righe di altre tabelle puntano a questo oggetto (DDT, movimenti, eventi…)."""
    from sqlalchemy import func as f, select
    tabella = oggetto.__table__
    totale = 0
    for t in db.metadata.sorted_tables:
        if t.name in salvo:
            continue
        for col in t.columns:
            if any(fk.column.table is tabella for fk in col.foreign_keys):
                totale += db.session.execute(select(f.count()).select_from(t).where(col == oggetto.id)).scalar()
    return totale


def elimina_utente(u, admin):
    """Elimina un account. Se compare nello storico resta come nome sui documenti ma non entra più,
    non compare negli elenchi e la sua email torna libera. Ritorna True se cancellato del tutto."""
    if admin.ruolo != "admin":
        raise Vietato("Solo un amministratore può eliminare un account.")
    if u.id == admin.id:
        raise Vietato("Non puoi eliminare il tuo account.")
    if u.supporto:
        raise Vietato("L'accesso dell'assistenza Getvingo non si elimina da qui.")
    if u.ruolo == "admin" and Utente.query.filter_by(ruolo="admin", attivo=True, eliminato=False, supporto=False).count() <= 1:
        raise Vietato("È l'unico amministratore: nominane un altro prima di eliminarlo.")
    if _riferimenti(u) == 0:
        db.session.delete(u)
        return True
    u.eliminato, u.attivo, u.invito_in_attesa = True, False, False
    u.email = f"eliminato-{u.id}-{u.email}"[:200]
    return False


def elimina_fornitore(sede, admin):
    """Elimina un'azienda esterna con i suoi account. Se compare nello storico resta sui documenti
    ma sparisce da elenchi e scelte. Ritorna True se cancellata del tutto."""
    if admin.ruolo != "admin":
        raise Vietato("Solo un amministratore può eliminare un fornitore.")
    if sede.tipo == "propria":
        raise Vietato("Le sedi della concessionaria non si eliminano: si disattivano.")
    if any(v.stato != "consegnato" for v in Veicolo.query.filter_by(sede_id=sede.id)):
        raise Vietato(f"Da {sede.nome} ci sono ancora auto: riportale prima di eliminarlo.")
    for u in list(sede.utenti):
        if not u.eliminato:
            elimina_utente(u, admin)
    db.session.flush()
    vettore = Vettore.query.filter_by(sede_id=sede.id).first()
    if vettore is not None and _riferimenti(vettore, salvo=("utente",)) == 0:
        Utente.query.filter_by(vettore_id=vettore.id).update({"vettore_id": None})
        db.session.delete(vettore)
        db.session.flush()
    if _riferimenti(sede) == 0:
        db.session.delete(sede)
        return True
    sede.eliminata, sede.attiva = True, False
    if vettore is not None and vettore in db.session:
        vettore.attivo = False
    return False
