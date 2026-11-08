from datetime import datetime, timedelta

from flask import g, has_app_context
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.orm import Session, declared_attr, with_loader_criteria
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


def adesso():
    return datetime.now().replace(microsecond=0)


# ---------------------------------------------------------------- più clienti sullo stesso portale

class Organizzazione(db.Model):
    """Cliente di Getvingo (una concessionaria con i suoi fornitori): ogni dato appartiene
    a una sola organizzazione e nessuno vede quelli delle altre."""
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(200), nullable=False)
    attiva = db.Column(db.Boolean, default=True)
    creata_at = db.Column(db.DateTime, default=adesso)
    # il cliente consente al gestore Getvingo di entrare per assistenza (lo decide il suo admin in Gestione)
    assistenza_consentita = db.Column(db.Boolean, default=False)
    assistenza_cambiata_at = db.Column(db.DateTime)
    # sigla dei codici degli adesivi NFC (es. CDEMO → CDEMO-0001): unica su tutto il portale
    sigla = db.Column(db.String(5), unique=True)


def org_attiva():
    """Organizzazione di chi sta usando il portale (impostata al caricamento dell'utente)."""
    return g.get("org_id") if has_app_context() else None


class PerOrganizzazione:
    """Dato che appartiene a un'organizzazione: alla creazione prende quella attiva."""
    @declared_attr
    def org_id(cls):
        return db.Column(db.Integer, db.ForeignKey("organizzazione.id"), nullable=False, index=True,
                         default=org_attiva)


@event.listens_for(Session, "do_orm_execute")
def _solo_la_propria_organizzazione(stato):
    """Filtro unico e automatico: ogni lettura vede solo i dati dell'organizzazione attiva.
    Senza organizzazione attiva (login, comandi da terminale) non filtra."""
    org = org_attiva()
    if org is None or not stato.is_select or stato.is_column_load \
            or stato.execution_options.get("tutte_le_organizzazioni"):
        return
    stato.statement = stato.statement.options(with_loader_criteria(
        PerOrganizzazione, lambda cls: cls.org_id == org, include_aliases=True))


class AccessoSupporto(PerOrganizzazione, db.Model):
    """Registro degli ingressi del gestore Getvingo nella concessionaria (visibile al cliente)."""
    id = db.Column(db.Integer, primary_key=True)
    at = db.Column(db.DateTime, nullable=False, default=adesso)
    gestore = db.Column(db.String(200), nullable=False)  # email del gestore che è entrato


class ConIndirizzo:
    """Indirizzo completo come sui DDT: "Via Roma 12, 31050 Ponzano Veneto (TV), Italia".
    Per i dati inseriti prima della scomposizione (solo "indirizzo"), si mostra quello che c'è."""

    @property
    def dove(self):
        """Comune con provincia, es. "Villorba (TV)"."""
        comune = (getattr(self, "comune", "") or "").strip()
        prov = (getattr(self, "provincia", "") or "").strip()
        return f"{comune} ({prov})" if comune and prov else comune

    @property
    def indirizzo_completo(self):
        via = (self.indirizzo or "").strip()
        if not getattr(self, "comune", None):
            return via
        luogo = " ".join(x for x in ((getattr(self, "cap", "") or "").strip(), self.dove) if x)
        stato = (getattr(self, "stato", "") or "").strip()
        return ", ".join(x for x in (via, luogo, stato) if x)


class Azienda(PerOrganizzazione, ConIndirizzo, db.Model):
    """Dati del mittente stampati sul DDT (una sola riga)."""
    id = db.Column(db.Integer, primary_key=True)
    ragione_sociale = db.Column(db.String(200), nullable=False)
    indirizzo = db.Column(db.String(300), default="")  # sede legale: via e civico
    piva = db.Column(db.String(30), default="")
    codice_fiscale = db.Column(db.String(16), default="")
    cap = db.Column(db.String(10), default="")
    comune = db.Column(db.String(120), default="")
    provincia = db.Column(db.String(4), default="")
    stato = db.Column(db.String(60), default="Italia")
    lat = db.Column(db.Float)
    lon = db.Column(db.Float)
    # Firma predefinita del mittente (PNG in data URL), apposta in automatico sui DDT
    firma = db.Column(db.Text)


class Utente(PerOrganizzazione, db.Model):
    RUOLI = {"autista": "Autista", "venditore": "Venditore", "ufficio": "Ufficio", "admin": "Amministratore",
             "fornitore": "Fornitore esterno"}

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(200), unique=True, nullable=False)
    password_hash = db.Column(db.String(300), nullable=False)
    ruolo = db.Column(db.String(20), nullable=False, default="autista")
    # Solo per i fornitori: la sede (carrozzeria, officina…) per cui operano
    sede_id = db.Column(db.Integer, db.ForeignKey("sede.id"))
    attivo = db.Column(db.Boolean, default=True)
    # Profilo di guida: compila da solo il DDT quando si parte col tocco NFC ("Inizia spostamento")
    vettore_id = db.Column(db.Integer, db.ForeignKey("vettore.id"))
    mezzo = db.Column(db.String(40), default="")  # modalità di trasporto abituale (Movimento.MEZZI)
    firma = db.Column(db.Text)  # firma personale salvata, apposta come firma del conducente
    # accessi: invito mandato ma password non ancora scelta; ultimo ingresso nel portale
    invito_in_attesa = db.Column(db.Boolean, default=False)
    ultimo_accesso_at = db.Column(db.DateTime)
    # "Supporto Getvingo": utente con cui il gestore della piattaforma entra in questa concessionaria
    # (dal Pannello Getvingo). Non entra mai con una password.
    supporto = db.Column(db.Boolean, default=False)

    sede = db.relationship("Sede", backref="utenti")
    vettore = db.relationship("Vettore")
    organizzazione = db.relationship("Organizzazione")

    @property
    def interno(self):
        return self.ruolo in ("autista", "venditore", "ufficio", "admin")

    @property
    def gestione(self):
        return self.ruolo in ("ufficio", "admin")

    def imposta_password(self, password):
        # pbkdf2: lo scrypt di default non c'è nel Python di sistema macOS (LibreSSL)
        self.password_hash = generate_password_hash(password, method="pbkdf2:sha256")

    def verifica_password(self, password):
        return check_password_hash(self.password_hash, password)


class Sede(PerOrganizzazione, ConIndirizzo, db.Model):
    """Qualsiasi luogo di partenza/arrivo: sedi proprie, carrozzerie, officine, clienti."""
    TIPI = {
        "propria": "Sede o piazzale della concessionaria",
        "carrozzeria": "Carrozzeria",
        "officina": "Officina",
        "esposizione": "Esposizione (fiera, showroom esterno)",
        "cliente": "Cliente",
        "altro": "Altro fornitore",
    }
    FORNITORI = ("carrozzeria", "officina", "altro")  # luoghi dove le auto vanno in lavorazione

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(200), nullable=False)
    indirizzo = db.Column(db.String(300), default="")
    comune = db.Column(db.String(120), default="")  # es. "Villorba": con la provincia dice dove si trova
    cap = db.Column(db.String(10), default="")
    provincia = db.Column(db.String(4), default="")
    stato = db.Column(db.String(60), default="Italia")
    piva = db.Column(db.String(30), default="")
    codice_fiscale = db.Column(db.String(16), default="")
    # la sede legale della concessionaria, creata e aggiornata dai Dati aziendali
    sede_legale = db.Column(db.Boolean, default=False)
    tipo = db.Column(db.String(20), nullable=False, default="propria")
    attiva = db.Column(db.Boolean, default=True)
    # Coordinate usate per aggiornare la posizione dell'auto all'arrivo
    lat = db.Column(db.Float)
    lon = db.Column(db.Float)
    # Firma predefinita del destinatario, apposta in automatico alla conferma di arrivo
    firma = db.Column(db.Text)

    @property
    def esterna(self):
        """Fornitore esterno: le auto ci vanno in lavorazione e poi tornano."""
        return self.tipo in self.FORNITORI

    @property
    def societa(self):
        """Luogo di un'altra azienda (fornitore, esposizione): servono P. IVA e codice fiscale."""
        return self.tipo in self.FORNITORI + ("esposizione",)

    @property
    def dove(self):
        """Riga sotto il nome: comune e provincia (per sapere dove si trova); senza, il tipo di luogo."""
        return ConIndirizzo.dove.fget(self) or self.TIPI.get(self.tipo, "")


class GiornoChiuso(PerOrganizzazione, db.Model):
    """Giorno senza preparazioni né consegne oltre alle festività nazionali (patrono, ferie, ponti)."""
    __table_args__ = (db.UniqueConstraint("org_id", "data"),)

    id = db.Column(db.Integer, primary_key=True)
    data = db.Column(db.Date, nullable=False)
    motivo = db.Column(db.String(120), default="")


class Vettore(PerOrganizzazione, ConIndirizzo, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(200), nullable=False)
    piva = db.Column(db.String(30), default="")
    indirizzo = db.Column(db.String(300), default="")
    codice_fiscale = db.Column(db.String(16), default="")
    cap = db.Column(db.String(10), default="")
    comune = db.Column(db.String(120), default="")
    provincia = db.Column(db.String(4), default="")
    stato = db.Column(db.String(60), default="Italia")
    lat = db.Column(db.Float)
    lon = db.Column(db.Float)
    attivo = db.Column(db.Boolean, default=True)
    # vettore della concessionaria (mezzi propri): creato con il cliente, segue i dati aziendali
    proprio = db.Column(db.Boolean, default=False)
    # vettore di un fornitore (es. la carrozzeria che riporta l'auto con un suo autista)
    sede_id = db.Column(db.Integer, db.ForeignKey("sede.id"))


class Veicolo(PerOrganizzazione, db.Model):
    STATI = {"in_sede": "In sede", "in_viaggio": "In viaggio", "consegnato": "Consegnato"}
    TIPOLOGIE = {"vettura": "Vettura", "commerciale": "Veicolo commerciale"}
    CONDIZIONI = {"nuovo": "Nuovo", "usato": "Usato"}

    __table_args__ = (db.UniqueConstraint("org_id", "vin"),)

    id = db.Column(db.Integer, primary_key=True)
    vin = db.Column(db.String(17), nullable=False, index=True)
    marca = db.Column(db.String(80), default="")
    modello = db.Column(db.String(120), default="")
    versione = db.Column(db.String(120), default="")  # allestimento / motorizzazione, facoltativa
    colore = db.Column(db.String(80), default="")
    targa = db.Column(db.String(12), default="")  # facoltativa: le auto nuove di solito non l'hanno
    tipologia = db.Column(db.String(20), nullable=False, default="vettura")
    condizione = db.Column(db.String(10), default="")  # nuovo / usato: scelto all'ingresso in stock, sempre mostrato
    stato = db.Column(db.String(20), nullable=False, default="in_sede")
    sede_id = db.Column(db.Integer, db.ForeignKey("sede.id"))
    # Ultima posizione nota (aggiornata a ogni tap e a ogni arrivo)
    lat = db.Column(db.Float)
    lon = db.Column(db.Float)
    posizione_at = db.Column(db.DateTime)
    creato_at = db.Column(db.DateTime, default=adesso)
    consegnato_at = db.Column(db.DateTime)
    # auto pronta: dove e quando verrà consegnata al cliente (l'ultimo DDT la porta lì)
    consegna_sede_id = db.Column(db.Integer, db.ForeignKey("sede.id"))
    consegna_prevista_at = db.Column(db.DateTime)

    sede = db.relationship("Sede", foreign_keys="Veicolo.sede_id")
    consegna_sede = db.relationship("Sede", foreign_keys=[consegna_sede_id])

    @property
    def descrizione(self):
        return " ".join(p for p in (self.marca, self.modello, self.colore) if p) or "Veicolo"

    def _movimento(self, stato):
        return (Movimento.query.join(Ddt, Movimento.ddt_id == Ddt.id).filter(
            Movimento.veicolo_id == self.id, Movimento.stato == stato, Ddt.annullato_at.is_(None))
            .order_by(Movimento.id.desc()).first())

    @property
    def movimento_aperto(self):
        return self._movimento("aperto")

    @property
    def movimento_predisposto(self):
        return self._movimento("predisposto")

    @property
    def ultimo_arrivo(self):
        return self._movimento("chiuso")

    @property
    def preparazione_attiva(self):
        return Preparazione.query.filter_by(veicolo_id=self.id, completata_at=None).first()

    @property
    def ultima_posizione(self):
        """Ultimo aggiornamento della posizione: chi, quando e come (con quale DDT o a mano)."""
        return (Evento.query.filter(Evento.veicolo_id == self.id, Evento.lat.isnot(None))
                .order_by(Evento.at.desc(), Evento.id.desc()).first())

    @property
    def danni_aperti(self):
        return [d for d in self.danni if d.aperto]

    @property
    def ultima_preparazione(self):
        return (Preparazione.query.filter_by(veicolo_id=self.id)
                .order_by(Preparazione.id.desc()).first())

    @property
    def fase(self):
        """Fase del ciclo vista dalla concessionaria:
        consegnato · in_viaggio · lavorazione · da_ritirare · preparazione · pronta · in_sede"""
        if self.stato in ("consegnato", "in_viaggio"):
            return self.stato
        if self.sede and self.sede.esterna:
            m = self.ultimo_arrivo
            return "da_ritirare" if m and m.lavorazione_completata_at else "lavorazione"
        if self.preparazione_attiva:
            return "preparazione"
        prep = self.ultima_preparazione
        if prep and prep.completata_at:
            return "pronta"
        return "in_sede"

    @property
    def verso_consegna(self):
        """Pronta e in viaggio con l'ultimo DDT verso la sede di consegna: resta tra le pronte
        (e in stock) finché non è consegnata al cliente."""
        if self.stato != "in_viaggio" or self.consegna_sede_id is None:
            return False
        prep, m = self.ultima_preparazione, self.movimento_aperto
        return bool(prep and prep.completata_at and m and m.a_sede_id == self.consegna_sede_id)

    @property
    def da_consegnare(self):
        """Pronta per la consegna, ferma o già in viaggio verso la sede di consegna."""
        return self.fase == "pronta" or self.verso_consegna

    FASI = {
        "in_viaggio": "In viaggio",
        "lavorazione": "In lavorazione",
        "da_ritirare": "Da ritirare",
        "preparazione": "In preparazione",
        "pronta": "Pronta per la consegna",
        "in_sede": "In sede",
        "consegnato": "Consegnata",
    }

    def stato_per(self, utente):
        """(chiave css, etichetta) dal punto di vista di chi guarda: per un fornitore
        l'auto che ha in officina è "in sede", per la concessionaria è "in lavorazione"."""
        fase = self.fase
        if utente is not None and not utente.interno and self.sede is not None \
                and self.sede.id == utente.sede_id \
                and fase in ("lavorazione", "da_ritirare"):
            return ("in_sede", "In sede da voi") if fase == "lavorazione" else ("pronta", "Pronta, attesa ritiro")
        return fase, self.FASI[fase]


class Tag(PerOrganizzazione, db.Model):
    """Adesivo NFC: contiene l'URL /t/<codice>."""
    id = db.Column(db.Integer, primary_key=True)
    codice = db.Column(db.String(16), unique=True, nullable=False, index=True)
    veicolo_id = db.Column(db.Integer, db.ForeignKey("veicolo.id"), unique=True)
    abbinato_at = db.Column(db.DateTime)
    creato_at = db.Column(db.DateTime, default=adesso)

    veicolo = db.relationship("Veicolo", backref=db.backref("tag", uselist=False))


class Movimento(PerOrganizzazione, db.Model):
    CAUSALI = [
        "Spostamento",           # tra sedi e piazzali della concessionaria
        "Invio lavorazione",     # verso carrozzeria, officina, altro fornitore
        "Reso lavorazione",      # dal fornitore indietro alla concessionaria
        "Conto esposizione",     # verso una fiera o uno showroom esterno
        "Reso esposizione",      # dall'esposizione indietro alla concessionaria
        "Consegna a cliente",
        "Conto visione",
        "Reso",
    ]
    # causali dei DDT emessi prima dell'08/11/2026: restano valide sui documenti già fatti
    CAUSALI_STORICHE = ["Trasferimento tra sedi", "Conto lavorazione", "Conto riparazione", "Rientro da lavorazione"]
    LAVORAZIONI = [
        "Nessuna (solo trasferimento)",
        "Carrozzeria",
        "Meccanica / tagliando",
        "Preparazione alla consegna",
        "Lavaggio / detailing",
        "Montaggio accessori",
        "Pellicole / oscuramento vetri",
        "Revisione",
        "Altro",
    ]
    NESSUNA = LAVORAZIONI[0]
    # giorni di lavorazione stimati per tipo (si possono cambiare auto per auto: lavorazione_stima_giorni)
    GIORNI_STIMATI = {"Carrozzeria": 5, "Meccanica / tagliando": 2, "Preparazione alla consegna": 2,
                      "Lavaggio / detailing": 1, "Montaggio accessori": 2, "Pellicole / oscuramento vetri": 2,
                      "Revisione": 1, "Altro": 3}
    # come viaggia l'auto (scritto nel DDT)
    MEZZI = ["Su strada (guidata)", "Bisarca", "Carro attrezzi", "Furgone / carrello", "Altro"]
    # predisposto: creato dall'ufficio, in attesa di presa in carico
    STATI = {"predisposto": "Da ritirare", "aperto": "In viaggio", "chiuso": "Arrivato"}

    id = db.Column(db.Integer, primary_key=True)
    veicolo_id = db.Column(db.Integer, db.ForeignKey("veicolo.id"), nullable=False)
    da_sede_id = db.Column(db.Integer, db.ForeignKey("sede.id"))
    a_sede_id = db.Column(db.Integer, db.ForeignKey("sede.id"), nullable=False)
    causale = db.Column(db.String(80), nullable=False)
    vettore_id = db.Column(db.Integer, db.ForeignKey("vettore.id"), nullable=False)
    # Persona che sposta fisicamente il mezzo (può non essere un utente del portale)
    conducente = db.Column(db.String(120), nullable=False, default="")
    lavorazione = db.Column(db.String(80), nullable=False, default="")
    lavorazione_note = db.Column(db.String(300), default="")
    mezzo = db.Column(db.String(40), default="")  # modalità di trasporto (Movimento.MEZZI)
    stato = db.Column(db.String(12), nullable=False, default="aperto")
    # DDT che accompagna il movimento: uno per auto, oppure uno solo per più auto (DDT multiplo)
    ddt_id = db.Column(db.Integer, db.ForeignKey("ddt.id", use_alter=True, name="fk_movimento_ddt"), index=True)

    # DDT predisposto: entro quando va preso in carico
    ritiro_entro_at = db.Column(db.DateTime)

    partenza_at = db.Column(db.DateTime)
    partenza_utente_id = db.Column(db.Integer, db.ForeignKey("utente.id"))
    partenza_lat = db.Column(db.Float)
    partenza_lon = db.Column(db.Float)
    firma_conducente = db.Column(db.Text)
    # True se la firma del conducente è quella salvata nel suo profilo (partenza col tocco NFC)
    firma_salvata = db.Column(db.Boolean, default=False)

    arrivo_at = db.Column(db.DateTime)
    arrivo_utente_id = db.Column(db.Integer, db.ForeignKey("utente.id"))
    arrivo_lat = db.Column(db.Float)
    arrivo_lon = db.Column(db.Float)
    # Valorizzata solo se l'auto è arrivata in un luogo diverso da quello del DDT
    arrivo_sede_id = db.Column(db.Integer, db.ForeignKey("sede.id"))
    note_arrivo = db.Column(db.String(300), default="")
    # Copia della firma predefinita del destinatario al momento dell'arrivo
    firma_destinatario = db.Column(db.Text)
    # True se un amministratore ha confermato al posto del destinatario
    arrivo_per_conto = db.Column(db.Boolean, default=False)

    lavorazione_completata_at = db.Column(db.DateTime)
    lavorazione_completata_da_id = db.Column(db.Integer, db.ForeignKey("utente.id"))
    lavorazione_note_fine = db.Column(db.String(300), default="")
    lavorazione_stima_giorni = db.Column(db.Integer)  # stima del fornitore o della concessionaria (vuota = per tipo)

    veicolo = db.relationship("Veicolo", backref="movimenti")
    da_sede = db.relationship("Sede", foreign_keys=[da_sede_id])
    a_sede = db.relationship("Sede", foreign_keys=[a_sede_id])
    arrivo_sede = db.relationship("Sede", foreign_keys=[arrivo_sede_id])
    vettore = db.relationship("Vettore")
    partenza_utente = db.relationship("Utente", foreign_keys=[partenza_utente_id])
    arrivo_utente = db.relationship("Utente", foreign_keys=[arrivo_utente_id])
    lavorazione_completata_da = db.relationship("Utente", foreign_keys=[lavorazione_completata_da_id])
    ddt = db.relationship("Ddt", foreign_keys=[ddt_id], back_populates="movimenti", post_update=True)

    @property
    def sede_arrivo_effettiva(self):
        return self.arrivo_sede or self.a_sede

    @property
    def con_lavorazione(self):
        return bool(self.lavorazione) and self.lavorazione != self.NESSUNA

    @property
    def scaduto(self):
        return self.stato == "predisposto" and self.ritiro_entro_at and self.ritiro_entro_at < adesso()

    @property
    def stima_giorni(self):
        return self.lavorazione_stima_giorni or self.GIORNI_STIMATI.get(self.lavorazione, 3)

    @property
    def lavorazione_prevista_at(self):
        """Quando la lavorazione dovrebbe finire: arrivo dal fornitore + giorni stimati."""
        return self.arrivo_at + timedelta(days=self.stima_giorni) if self.arrivo_at else None

    @property
    def avanzamento_lavorazione(self):
        """% del tempo stimato già passato (100 se finita). Può superare 100 se in ritardo: si limita a 100."""
        if self.lavorazione_completata_at:
            return 100
        if not self.arrivo_at:
            return 0
        totale = (self.lavorazione_prevista_at - self.arrivo_at).total_seconds()
        return max(0, min(100, round(100 * (adesso() - self.arrivo_at).total_seconds() / totale)))

    @property
    def giorni_ritardo(self):
        """Giorni oltre la stima senza che il fornitore abbia segnato la fine (0 = in tempo)."""
        if self.lavorazione_completata_at or not self.arrivo_at or adesso() <= self.lavorazione_prevista_at:
            return 0
        return max(1, (adesso() - self.lavorazione_prevista_at).days)

    @property
    def sollecito_aperto(self):
        return next((s for s in sorted(self.solleciti, key=lambda s: s.at, reverse=True) if not s.risposta_at), None)

    @property
    def ultimo_sollecito(self):
        return max(self.solleciti, key=lambda s: s.at) if self.solleciti else None

    @property
    def giorni_lavorazione(self):
        """Giorni dall'arrivo dal fornitore alla fine lavorazione (o a oggi)."""
        if self.stato != "chiuso" or not self.con_lavorazione or not self.sede_arrivo_effettiva.esterna:
            return None
        fine = self.lavorazione_completata_at or adesso()
        return (fine.date() - self.arrivo_at.date()).days


class Ddt(PerOrganizzazione, db.Model):
    __table_args__ = (db.UniqueConstraint("org_id", "anno", "numero"),)

    id = db.Column(db.Integer, primary_key=True)
    anno = db.Column(db.Integer, nullable=False)
    numero = db.Column(db.Integer, nullable=False)
    movimento_id = db.Column(db.Integer, db.ForeignKey("movimento.id"), unique=True, nullable=False)
    emesso_at = db.Column(db.DateTime, nullable=False, default=adesso)
    creato_da_id = db.Column(db.Integer, db.ForeignKey("utente.id"), nullable=False)
    revisione = db.Column(db.Integer, nullable=False, default=0)
    # Copia della firma del mittente al momento della creazione
    firma_mittente = db.Column(db.Text)
    # Dati di mittente, destinatario e vettore fissati all'emissione: modificare un'anagrafica
    # dopo non cambia i DDT già emessi. {"mittente": [nome, indirizzo, piva], ...}
    dati_fissi = db.Column(db.JSON)

    # Annullamento (il DDT non si cancella fisicamente: il numero resta occupato)
    annullato_at = db.Column(db.DateTime)
    annullato_da_id = db.Column(db.Integer, db.ForeignKey("utente.id"))
    motivo_annullamento = db.Column(db.String(300), default="")

    # movimento capofila (il primo): per un DDT di una sola auto è l'unico
    movimento = db.relationship("Movimento", foreign_keys=[movimento_id])
    movimenti = db.relationship("Movimento", foreign_keys="Movimento.ddt_id", back_populates="ddt",
                                order_by="Movimento.id", post_update=True)
    creato_da = db.relationship("Utente", foreign_keys=[creato_da_id])
    annullato_da = db.relationship("Utente", foreign_keys=[annullato_da_id])

    @property
    def sigla(self):
        return f"{self.numero}/{self.anno}"

    @property
    def multiplo(self):
        return len(self.movimenti) > 1

    @property
    def revisioni_ordinate(self):
        return sorted(self.revisioni, key=lambda r: r.at)

    @property
    def stato(self):
        """(chiave css, etichetta) del DDT."""
        if self.annullato_at:
            return "annullato", "Annullato"
        if self.movimento.scaduto:
            return "scaduto", "Ritiro scaduto"
        return self.movimento.stato, Movimento.STATI[self.movimento.stato]


class Sollecito(PerOrganizzazione, db.Model):
    """Richiesta della concessionaria al fornitore quando la lavorazione supera il tempo stimato:
    il fornitore risponde con il motivo del ritardo e, se serve, una nuova stima."""
    id = db.Column(db.Integer, primary_key=True)
    movimento_id = db.Column(db.Integer, db.ForeignKey("movimento.id"), nullable=False, index=True)
    utente_id = db.Column(db.Integer, db.ForeignKey("utente.id"), nullable=False)
    at = db.Column(db.DateTime, nullable=False, default=adesso)
    messaggio = db.Column(db.String(500), nullable=False)
    risposta = db.Column(db.String(500))
    risposta_at = db.Column(db.DateTime)
    risposta_utente_id = db.Column(db.Integer, db.ForeignKey("utente.id"))
    nuova_stima_giorni = db.Column(db.Integer)

    movimento = db.relationship("Movimento", backref="solleciti")
    utente = db.relationship("Utente", foreign_keys=[utente_id])
    risposta_utente = db.relationship("Utente", foreign_keys=[risposta_utente_id])


class DdtRevisione(PerOrganizzazione, db.Model):
    """Ogni modifica a un DDT emesso: chi, quando, cosa (prima -> dopo)."""
    id = db.Column(db.Integer, primary_key=True)
    ddt_id = db.Column(db.Integer, db.ForeignKey("ddt.id"), nullable=False)
    numero = db.Column(db.Integer, nullable=False)
    utente_id = db.Column(db.Integer, db.ForeignKey("utente.id"), nullable=False)
    at = db.Column(db.DateTime, nullable=False, default=adesso)
    modifiche = db.Column(db.JSON, nullable=False)  # [{"campo", "prima", "dopo"}]

    ddt = db.relationship("Ddt", backref="revisioni")
    utente = db.relationship("Utente")


class DdtArchivio(PerOrganizzazione, db.Model):
    """Copia PDF fissa di una versione del DDT, salvata nell'archivio (S3 o cartella locale).
    Una riga per ogni versione diversa: le copie precedenti non si sovrascrivono mai."""
    id = db.Column(db.Integer, primary_key=True)
    ddt_id = db.Column(db.Integer, db.ForeignKey("ddt.id"), nullable=False, index=True)
    at = db.Column(db.DateTime, nullable=False, default=adesso)
    impronta = db.Column(db.String(64), nullable=False)  # sha256 del PDF
    chiave = db.Column(db.String(300), nullable=False)   # percorso nell'archivio

    # eliminando un DDT (solo in demo) spariscono le righe, non i PDF già in archivio
    ddt = db.relationship("Ddt", backref=db.backref("archiviazioni", order_by="DdtArchivio.id",
                                                    cascade="all, delete-orphan"))


class Preparazione(PerOrganizzazione, db.Model):
    """Lista di preparazione per le auto rientrate da una lavorazione esterna."""
    FASI = ["Controllo rientro", "Preparazione", "Lavaggio", "Pronta"]

    id = db.Column(db.Integer, primary_key=True)
    veicolo_id = db.Column(db.Integer, db.ForeignKey("veicolo.id"), nullable=False)
    movimento_id = db.Column(db.Integer, db.ForeignKey("movimento.id"))  # rientro che l'ha generata
    creata_at = db.Column(db.DateTime, nullable=False, default=adesso)
    completata_at = db.Column(db.DateTime)
    pronta_entro_at = db.Column(db.DateTime)  # quando deve essere pronta (calendario della preparazione)
    # passo segnato "fatto" in attesa della conferma col tocco NFC: chi, quando, quale passo
    conferma_utente_id = db.Column(db.Integer, db.ForeignKey("utente.id"))
    conferma_at = db.Column(db.DateTime)
    conferma_indice = db.Column(db.Integer)

    veicolo = db.relationship("Veicolo", backref="preparazioni")
    movimento = db.relationship("Movimento")

    @property
    def fasi_fatte(self):
        return sorted(self.fasi, key=lambda f: f.indice)

    @property
    def prossima_fase(self):
        n = len(self.fasi)
        return self.FASI[n] if n < len(self.FASI) else None

    @property
    def avanzamento(self):
        return round(100 * len(self.fasi) / len(self.FASI))

    @property
    def tempi(self):
        """Durata di ogni fase del ciclo, dalla lavorazione esterna alla preparazione:
        [(nome, inizio, fine, durata timedelta o None se in corso)]."""
        righe = []
        rientro = self.movimento
        if rientro is not None and rientro.da_sede is not None:
            andata = (Movimento.query.join(Ddt, Movimento.ddt_id == Ddt.id).filter(
                Movimento.veicolo_id == self.veicolo_id, Movimento.stato == "chiuso",
                Movimento.arrivo_at <= rientro.partenza_at, Ddt.annullato_at.is_(None),
                Movimento.id != rientro.id).order_by(Movimento.arrivo_at.desc()).first())
            if andata is not None and andata.lavorazione_completata_at:
                righe.append((f"Lavorazione · {andata.sede_arrivo_effettiva.nome}",
                               andata.arrivo_at, andata.lavorazione_completata_at))
                righe.append(("Attesa ritiro", andata.lavorazione_completata_at, rientro.partenza_at))
            righe.append(("Viaggio di rientro", rientro.partenza_at, rientro.arrivo_at))
        inizio = self.creata_at
        for f in self.fasi_fatte:
            righe.append((f.nome, inizio, f.at))
            inizio = f.at
        if self.prossima_fase:
            righe.append((self.prossima_fase, inizio, None))
        return [(n, a, b, (b - a) if a and b else None) for n, a, b in righe]

    @property
    def tappe(self):
        """Rientro e ogni passo fatto, con la data e i giorni passati da allora."""
        oggi = adesso().date()
        righe = [("Rientro", self.creata_at)] + [(f.nome, f.at) for f in self.fasi_fatte]
        return [{"nome": n, "data": d, "giorni_fa": (oggi - d.date()).days} for n, d in righe]

    @property
    def riepilogo(self):
        """Versione semplice dei tempi: giorni di lavorazione esterna e interna, data di fine."""
        esterni = fornitore = None
        rientro = self.movimento
        if rientro is not None:
            andata = (Movimento.query.join(Ddt, Movimento.ddt_id == Ddt.id).filter(
                Movimento.veicolo_id == self.veicolo_id, Movimento.stato == "chiuso",
                Movimento.arrivo_at <= rientro.partenza_at, Ddt.annullato_at.is_(None),
                Movimento.id != rientro.id).order_by(Movimento.arrivo_at.desc()).first())
            if andata is not None and andata.sede_arrivo_effettiva.esterna:
                fine = andata.lavorazione_completata_at or rientro.partenza_at
                esterni = (fine.date() - andata.arrivo_at.date()).days
                fornitore = andata.sede_arrivo_effettiva.nome
        interni = ((self.completata_at or adesso()).date() - self.creata_at.date()).days
        return {"esterni": esterni, "fornitore": fornitore, "interni": interni,
                "completata_at": self.completata_at}

    @property
    def durata_totale(self):
        t = self.tempi
        if not t or not t[0][1]:
            return None
        fine = self.completata_at or adesso()
        return fine - t[0][1]


class PreparazioneFase(PerOrganizzazione, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    preparazione_id = db.Column(db.Integer, db.ForeignKey("preparazione.id"), nullable=False)
    indice = db.Column(db.Integer, nullable=False)
    utente_id = db.Column(db.Integer, db.ForeignKey("utente.id"), nullable=False)
    at = db.Column(db.DateTime, nullable=False, default=adesso)

    preparazione = db.relationship("Preparazione", backref="fasi")
    utente = db.relationship("Utente")

    @property
    def nome(self):
        return Preparazione.FASI[self.indice]


class Danno(PerOrganizzazione, db.Model):
    """Danno riscontrato su un veicolo: in piazzale o all'arrivo di uno spostamento."""
    ZONE = ["Anteriore", "Posteriore", "Lato sinistro", "Lato destro", "Tetto", "Cofano",
            "Cerchi / gomme", "Vetri / specchi", "Interni", "Altro"]
    CONTESTI = {"piazzale": "In piazzale", "arrivo": "All'arrivo"}

    id = db.Column(db.Integer, primary_key=True)
    veicolo_id = db.Column(db.Integer, db.ForeignKey("veicolo.id"), nullable=False)
    movimento_id = db.Column(db.Integer, db.ForeignKey("movimento.id"))  # se rilevato all'arrivo
    contesto = db.Column(db.String(12), nullable=False, default="piazzale")
    zona = db.Column(db.String(40), nullable=False)
    descrizione = db.Column(db.String(500), nullable=False)
    segnalato_da_id = db.Column(db.Integer, db.ForeignKey("utente.id"), nullable=False)
    segnalato_at = db.Column(db.DateTime, nullable=False, default=adesso)
    risolto_at = db.Column(db.DateTime)
    risolto_da_id = db.Column(db.Integer, db.ForeignKey("utente.id"))
    nota_risoluzione = db.Column(db.String(300), default="")
    rimosso = db.Column(db.Boolean, default=False)  # ripristino della segnalazione da parte dell'admin

    veicolo = db.relationship("Veicolo", backref="danni")
    movimento = db.relationship("Movimento")
    segnalato_da = db.relationship("Utente", foreign_keys=[segnalato_da_id])
    risolto_da = db.relationship("Utente", foreign_keys=[risolto_da_id])

    @property
    def aperto(self):
        return self.risolto_at is None and not self.rimosso

    @property
    def foto_segnalazione(self):
        return [f for f in self.foto if not f.risoluzione]

    @property
    def foto_risoluzione(self):
        return [f for f in self.foto if f.risoluzione]


class DannoFoto(PerOrganizzazione, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    danno_id = db.Column(db.Integer, db.ForeignKey("danno.id"), nullable=False)
    file = db.Column(db.String(80), nullable=False, unique=True)  # nome nella cartella foto
    # False: foto della segnalazione · True: foto che conferma la riparazione
    risoluzione = db.Column(db.Boolean, nullable=False, default=False)

    danno = db.relationship("Danno", backref="foto")


class Azione(PerOrganizzazione, db.Model):
    """Registro di ciò che fanno gli utenti: l'amministratore può ripristinare l'ultima
    azione fatta su un'auto."""
    NOMI = {
        "partenza": "DDT emesso e partenza",
        "predisposizione": "DDT predisposto",
        "presa_in_carico": "Presa in carico",
        "arrivo": "Arrivo confermato",
        "lavorazione_completata": "Lavorazione completata",
        "preparazione_fase": "Passo di preparazione",
        "consegna": "Consegna al cliente",
        "annullamento": "DDT cancellato",
        "danno": "Danno segnalato",
        "danno_risolto": "Danno risolto",
    }

    id = db.Column(db.Integer, primary_key=True)
    tipo = db.Column(db.String(30), nullable=False)
    utente_id = db.Column(db.Integer, db.ForeignKey("utente.id"), nullable=False)
    veicolo_id = db.Column(db.Integer, db.ForeignKey("veicolo.id"), nullable=False)
    ddt_id = db.Column(db.Integer, db.ForeignKey("ddt.id"))
    dati = db.Column(db.JSON, default=dict)  # ciò che serve per tornare indietro
    at = db.Column(db.DateTime, nullable=False, default=adesso)
    ripristinata_at = db.Column(db.DateTime)
    ripristinata_da_id = db.Column(db.Integer, db.ForeignKey("utente.id"))

    utente = db.relationship("Utente", foreign_keys=[utente_id])
    ripristinata_da = db.relationship("Utente", foreign_keys=[ripristinata_da_id])
    veicolo = db.relationship("Veicolo")
    ddt = db.relationship("Ddt")

    @property
    def nome(self):
        return self.NOMI.get(self.tipo, self.tipo)


class Evento(PerOrganizzazione, db.Model):
    """Registro di ogni tap: abbinamento, partenza, arrivo, riposizionamento."""
    id = db.Column(db.Integer, primary_key=True)
    veicolo_id = db.Column(db.Integer, db.ForeignKey("veicolo.id"), nullable=False)
    tipo = db.Column(db.String(20), nullable=False)
    utente_id = db.Column(db.Integer, db.ForeignKey("utente.id"))
    lat = db.Column(db.Float)
    lon = db.Column(db.Float)
    at = db.Column(db.DateTime, nullable=False, default=adesso)

    ddt_id = db.Column(db.Integer, db.ForeignKey("ddt.id"))

    veicolo = db.relationship("Veicolo", backref="eventi")
    utente = db.relationship("Utente")
    ddt = db.relationship("Ddt")

    @property
    def come(self):
        """Descrizione breve di come è stata aggiornata la posizione."""
        if self.ddt is not None:
            return f"{'arrivo' if self.tipo == 'arrivo' else 'partenza'} con DDT {self.ddt.sigla}"
        return {"riposizionamento": "a mano (“Parcheggiata qui”)", "abbinamento": "all'abbinamento dell'adesivo",
                "inserimento": "all'inserimento in stock",
                "tocco": "toccando l'adesivo"}.get(self.tipo, "a mano")
