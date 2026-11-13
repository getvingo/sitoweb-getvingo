"""Aggiornamento automatico dei database SQLite creati prima del portale multi-cliente.

Le tabelle vecchie non hanno org_id e hanno VIN/numero DDT unici su tutto il database:
SQLite non permette di cambiare questi vincoli, quindi ogni tabella viene ricreata dal
modello attuale e i dati copiati dentro, assegnati alla prima organizzazione.
Prima di toccare nulla salva una copia del file (dovin.db.prima-multi-cliente)."""
import shutil

from sqlalchemy import inspect

from .models import Organizzazione, db


def aggiorna_schema():
    ispettore = inspect(db.engine)
    tabelle = ispettore.get_table_names()
    if "veicolo" not in tabelle or "org_id" in [c["name"] for c in ispettore.get_columns("veicolo")]:
        return False  # database nuovo (lo crea create_all) o già aggiornato
    if db.engine.dialect.name != "sqlite":
        raise RuntimeError("Database senza org_id: aggiornamento automatico previsto solo per SQLite.")

    file_db = db.engine.url.database
    if file_db and file_db != ":memory:":
        shutil.copy2(file_db, file_db + ".prima-multi-cliente")

    with db.engine.begin() as conn:
        vecchie = {t: [c["name"] for c in ispettore.get_columns(t)] for t in tabelle}
        # gli indici hanno nomi globali in SQLite: vanno tolti prima di ricrearli sulle tabelle nuove
        for (nome,) in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL").fetchall():
            conn.exec_driver_sql(f'DROP INDEX "{nome}"')
        for t in tabelle:
            conn.exec_driver_sql(f'ALTER TABLE "{t}" RENAME TO "_vecchia_{t}"')
        db.metadata.create_all(conn)

        riga = conn.exec_driver_sql('SELECT ragione_sociale FROM "_vecchia_azienda" LIMIT 1').fetchone() \
            if "azienda" in tabelle else None
        conn.execute(Organizzazione.__table__.insert().values(id=1, nome=riga[0] if riga else "Concessionaria",
                                                              attiva=True))
        for t, colonne in vecchie.items():
            if t not in db.metadata.tables:
                continue
            nuove = db.metadata.tables[t].columns.keys()
            comuni = [c for c in colonne if c in nuove]
            elenco = ", ".join(f'"{c}"' for c in comuni)
            if "org_id" in nuove:
                conn.exec_driver_sql(f'INSERT INTO "{t}" ({elenco}, org_id) SELECT {elenco}, 1 FROM "_vecchia_{t}"')
            else:
                conn.exec_driver_sql(f'INSERT INTO "{t}" ({elenco}) SELECT {elenco} FROM "_vecchia_{t}"')
        for t in tabelle:
            conn.exec_driver_sql(f'DROP TABLE "_vecchia_{t}"')
    return True


def aggiungi_colonne():
    """Colonne nuove su database esistenti (SQLite e Postgres). Ritorna i nomi aggiunti."""
    ispettore = inspect(db.engine)
    if "movimento" not in ispettore.get_table_names():
        return []
    aggiunte = []
    colonne_org = [c["name"] for c in ispettore.get_columns("organizzazione")]
    for nome, tipo in (("assistenza_consentita", "BOOLEAN DEFAULT FALSE"), ("assistenza_cambiata_at", "TIMESTAMP"),
                       ("sigla", "VARCHAR(5)")):
        if nome not in colonne_org:  # consenso del cliente all'assistenza del gestore Getvingo
            with db.engine.begin() as conn:
                conn.exec_driver_sql(f"ALTER TABLE organizzazione ADD COLUMN {nome} {tipo}")
            aggiunte.append("organizzazione." + nome)
    if "ddt_id" not in [c["name"] for c in ispettore.get_columns("movimento")]:
        # DDT multiplo: il movimento indica il suo DDT (prima solo il DDT indicava il movimento)
        with db.engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE movimento ADD COLUMN ddt_id INTEGER REFERENCES ddt(id)")
            conn.exec_driver_sql("UPDATE movimento SET ddt_id = (SELECT ddt.id FROM ddt WHERE ddt.movimento_id = movimento.id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_movimento_ddt_id ON movimento (ddt_id)")
        aggiunte.append("movimento.ddt_id")
    if "danno_foto" in ispettore.get_table_names() and \
            "risoluzione" not in [c["name"] for c in ispettore.get_columns("danno_foto")]:
        # foto che confermano la riparazione di un danno
        with db.engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE danno_foto ADD COLUMN risoluzione BOOLEAN NOT NULL DEFAULT FALSE")
        aggiunte.append("danno_foto.risoluzione")
    colonne_veicolo = [c["name"] for c in ispettore.get_columns("veicolo")]
    for nome, tipo in (("cliente_nome", "VARCHAR(200) DEFAULT ''"), ("venditore_id", "INTEGER REFERENCES utente(id)"),
                       ("note_interne", "TEXT DEFAULT ''")):
        if nome not in colonne_veicolo:  # note interne (cliente, venditore): mai sul DDT
            with db.engine.begin() as conn:
                conn.exec_driver_sql(f"ALTER TABLE veicolo ADD COLUMN {nome} {tipo}")
            aggiunte.append("veicolo." + nome)
    for nome, tipo in (("consegna_sede_id", "INTEGER REFERENCES sede(id)"), ("consegna_prevista_at", "TIMESTAMP")):
        if nome not in colonne_veicolo:  # sede e orario di consegna dell'auto pronta
            with db.engine.begin() as conn:
                conn.exec_driver_sql(f"ALTER TABLE veicolo ADD COLUMN {nome} {tipo}")
            aggiunte.append("veicolo." + nome)
    if "versione" not in colonne_veicolo:
        with db.engine.begin() as conn:  # versione (allestimento / motorizzazione)
            conn.exec_driver_sql("ALTER TABLE veicolo ADD COLUMN versione VARCHAR(120) DEFAULT ''")
        aggiunte.append("veicolo.versione")
    if "condizione" not in colonne_veicolo:
        with db.engine.begin() as conn:  # auto nuova o usata
            conn.exec_driver_sql("ALTER TABLE veicolo ADD COLUMN condizione VARCHAR(10) DEFAULT ''")
        aggiunte.append("veicolo.condizione")
    if "mezzo" not in [c["name"] for c in ispettore.get_columns("movimento")]:
        with db.engine.begin() as conn:  # modalità di trasporto nel DDT
            conn.exec_driver_sql("ALTER TABLE movimento ADD COLUMN mezzo VARCHAR(40) DEFAULT ''")
        aggiunte.append("movimento.mezzo")
    if "pronta_entro_at" not in [c["name"] for c in ispettore.get_columns("preparazione")]:
        with db.engine.begin() as conn:  # scadenza della preparazione
            conn.exec_driver_sql("ALTER TABLE preparazione ADD COLUMN pronta_entro_at TIMESTAMP")
        aggiunte.append("preparazione.pronta_entro_at")
    colonne_prep = [c["name"] for c in ispettore.get_columns("preparazione")]
    for nome, tipo in (("conferma_utente_id", "INTEGER REFERENCES utente(id)"), ("conferma_at", "TIMESTAMP"),
                       ("conferma_indice", "INTEGER")):
        if nome not in colonne_prep:  # passo in attesa della conferma col tocco NFC
            with db.engine.begin() as conn:
                conn.exec_driver_sql(f"ALTER TABLE preparazione ADD COLUMN {nome} {tipo}")
            aggiunte.append("preparazione." + nome)
    colonne_utente = [c["name"] for c in ispettore.get_columns("utente")]
    for nome, tipo in (("vettore_id", "INTEGER REFERENCES vettore(id)"), ("mezzo", "VARCHAR(40) DEFAULT ''"),
                       ("firma", "TEXT")):
        if nome not in colonne_utente:  # profilo di guida per la partenza col tocco NFC
            with db.engine.begin() as conn:
                conn.exec_driver_sql(f"ALTER TABLE utente ADD COLUMN {nome} {tipo}")
            aggiunte.append("utente." + nome)
    for nome, tipo in (("invito_in_attesa", "BOOLEAN DEFAULT FALSE"), ("ultimo_accesso_at", "TIMESTAMP"),
                       ("supporto", "BOOLEAN DEFAULT FALSE"), ("eliminato", "BOOLEAN DEFAULT FALSE")):
        if nome not in colonne_utente:  # inviti e ultimo accesso
            with db.engine.begin() as conn:
                conn.exec_driver_sql(f"ALTER TABLE utente ADD COLUMN {nome} {tipo}")
            aggiunte.append("utente." + nome)
    if "comune" not in [c["name"] for c in ispettore.get_columns("sede")]:
        with db.engine.begin() as conn:  # comune del luogo: dove si trova
            conn.exec_driver_sql("ALTER TABLE sede ADD COLUMN comune VARCHAR(120) DEFAULT ''")
        aggiunte.append("sede.comune")
    # indirizzo completo (CAP, comune, provincia, stato), codice fiscale e posizione (08/11/2026)
    INDIRIZZO = [("codice_fiscale", "VARCHAR(16) DEFAULT ''"), ("cap", "VARCHAR(10) DEFAULT ''"),
                 ("comune", "VARCHAR(120) DEFAULT ''"), ("provincia", "VARCHAR(4) DEFAULT ''"),
                 ("stato", "VARCHAR(60) DEFAULT 'Italia'"), ("lat", "FLOAT"), ("lon", "FLOAT")]
    for tabella, extra in (("azienda", []), ("vettore", []),
                           ("sede", [("sede_legale", "BOOLEAN DEFAULT FALSE"), ("eliminata", "BOOLEAN DEFAULT FALSE")])):
        presenti = [c["name"] for c in ispettore.get_columns(tabella)]
        for nome, tipo in INDIRIZZO + extra:
            if nome not in presenti:
                with db.engine.begin() as conn:
                    conn.exec_driver_sql(f"ALTER TABLE {tabella} ADD COLUMN {nome} {tipo}")
                aggiunte.append(f"{tabella}.{nome}")
    if "sede.provincia" in aggiunte:
        # il comune era scritto "Villorba (TV)": si separa la provincia
        # (parametri sempre separati dal testo SQL: con Postgres un "%" scritto nella query è un errore)
        import re
        from sqlalchemy import text
        with db.engine.begin() as conn:
            for sid, comune in conn.execute(text("SELECT id, comune FROM sede")).fetchall():
                m = re.fullmatch(r"\s*(.+?)\s*\((\w{2})\)\s*", comune or "")
                if m:
                    conn.execute(text("UPDATE sede SET comune = :c, provincia = :p WHERE id = :i"),
                                 {"c": m.group(1), "p": m.group(2).upper(), "i": sid})
    colonne_vettore = [c["name"] for c in ispettore.get_columns("vettore")]
    for nome, tipo in (("proprio", "BOOLEAN DEFAULT FALSE"), ("sede_id", "INTEGER REFERENCES sede(id)")):
        if nome not in colonne_vettore:  # vettore della concessionaria / di un fornitore
            with db.engine.begin() as conn:
                conn.exec_driver_sql(f"ALTER TABLE vettore ADD COLUMN {nome} {tipo}")
            aggiunte.append("vettore." + nome)
    if "firma_salvata" not in [c["name"] for c in ispettore.get_columns("movimento")]:
        with db.engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE movimento ADD COLUMN firma_salvata BOOLEAN DEFAULT FALSE")
        aggiunte.append("movimento.firma_salvata")
    if "lavorazione_stima_giorni" not in [c["name"] for c in ispettore.get_columns("movimento")]:
        with db.engine.begin() as conn:  # giorni di lavorazione stimati (avanzamento e solleciti)
            conn.exec_driver_sql("ALTER TABLE movimento ADD COLUMN lavorazione_stima_giorni INTEGER")
        aggiunte.append("movimento.lavorazione_stima_giorni")
    # 11/11/2026: email per le copie dei DDT firmati, dati per le analisi
    nuove = [("azienda", "email_ddt", "VARCHAR(200) DEFAULT ''"), ("sede", "email_ddt", "VARCHAR(200) DEFAULT ''"),
             ("veicolo", "venduta_il", "DATE"), ("movimento", "lavorazione_stima_iniziale", "INTEGER"),
             ("movimento", "stima_viaggio_min", "INTEGER"), ("movimento", "stima_viaggio_km", "FLOAT"),
             ("ddt", "copie_inviate_at", "TIMESTAMP"), ("azienda", "invia_copie_ddt", "BOOLEAN DEFAULT FALSE"), ("ddt", "copie_inviate_a", "VARCHAR(500) DEFAULT ''"),
             # 11/11/2026 sera: DDT da ritirare assegnato a una persona (solo lei lo prende in carico)
             ("movimento", "assegnato_a_id", "INTEGER REFERENCES utente(id)"),
             # 13/11/2026: anno modello dell'auto (dal VIN o scelto all'ingresso in stock)
             ("veicolo", "anno", "INTEGER"),
             ("veicolo", "alimentazione", "VARCHAR(30) DEFAULT ''")]
    for tabella, colonna, tipo in nuove:
        if colonna not in [c["name"] for c in inspect(db.engine).get_columns(tabella)]:
            with db.engine.begin() as conn:
                conn.exec_driver_sql(f"ALTER TABLE {tabella} ADD COLUMN {colonna} {tipo}")
            aggiunte.append(f"{tabella}.{colonna}")
    return aggiunte


def aggiorna_ruoli():
    """Ruoli di prima del 10/11/2026 (ufficio, autista) alle mansioni nuove (backoffice, consegne)."""
    from sqlalchemy import text
    from .models import Utente
    if "utente" not in inspect(db.engine).get_table_names():
        return 0
    cambiati = 0
    with db.engine.begin() as conn:
        for vecchio, nuovo in Utente.RUOLI_VECCHI.items():
            cambiati += conn.execute(text("UPDATE utente SET ruolo = :nuovo WHERE ruolo = :vecchio"),
                                     {"nuovo": nuovo, "vecchio": vecchio}).rowcount or 0
    return cambiati


def assicura_vettori_propri():
    """Ogni concessionaria ha il suo vettore (mezzi propri): per quelle nate prima si adotta quello
    con il nome della concessionaria, altrimenti si crea. Idempotente, a ogni avvio."""
    from .models import Azienda, Organizzazione, Vettore
    creati = 0
    for org in Organizzazione.query.all():
        tutte = {"tutte_le_organizzazioni": True}
        if Vettore.query.execution_options(**tutte).filter_by(org_id=org.id, proprio=True).first():
            continue
        azienda = Azienda.query.execution_options(**tutte).filter_by(org_id=org.id).first()
        nome = (azienda.ragione_sociale if azienda and azienda.ragione_sociale else org.nome).strip()
        simile = [v for v in Vettore.query.execution_options(**tutte).filter_by(org_id=org.id)
                  if v.nome.lower().startswith(nome.lower())]
        if simile:
            simile[0].proprio = True
        else:
            db.session.add(Vettore(org_id=org.id, nome=nome, proprio=True,
                                   piva=azienda.piva if azienda else "", indirizzo=azienda.indirizzo if azienda else ""))
            creati += 1
    db.session.commit()
    return creati
