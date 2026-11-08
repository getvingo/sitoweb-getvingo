import os
import secrets
from datetime import timedelta

import click
from flask import Flask
from sqlalchemy.exc import OperationalError
from werkzeug.middleware.proxy_fix import ProxyFix

from .models import db


def _chiave_segreta(app):
    if os.environ.get("DOVIN_SECRET_KEY"):
        return os.environ["DOVIN_SECRET_KEY"]
    percorso = os.path.join(app.instance_path, "secret_key")
    if not os.path.exists(percorso):
        with open(percorso, "w") as f:
            f.write(secrets.token_hex(32))
    with open(percorso) as f:
        return f.read().strip()


def create_app(config=None):
    # DOVIN_DATI: cartella di foto, archivio DDT e loghi fuori dal codice (su Google Cloud è lo
    # spazio Cloud Storage montato come cartella); senza, la cartella instance/ accanto al codice
    app = Flask(__name__, instance_relative_config=True, instance_path=os.environ.get("DOVIN_DATI") or None)
    # Dietro un tunnel/proxy HTTPS (Cloudflare, Nginx): usa schema e host pubblici per i link
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
    os.makedirs(app.instance_path, exist_ok=True)
    # installazioni nate con il vecchio nome (MOVINTA): il database si chiamava movinta.db
    vecchio, nuovo = (os.path.join(app.instance_path, n) for n in ("movinta.db", "dovin.db"))
    if os.path.exists(vecchio) and not os.path.exists(nuovo):
        try:
            os.rename(vecchio, nuovo)
        except OSError:  # già rinominato da un altro processo in avvio
            pass
    database = os.environ.get("DOVIN_DATABASE_URL", "sqlite:///" + os.path.join(app.instance_path, "dovin.db"))
    # Postgres sempre con il driver psycopg 3 (indicato esplicitamente: non dipende dalla
    # versione di SQLAlchemy); accetta anche la forma postgres:// usata da molti hosting
    for prefisso in ("postgres://", "postgresql://"):
        if database.startswith(prefisso):
            database = "postgresql+psycopg://" + database[len(prefisso):]
    app.config.update(
        SQLALCHEMY_DATABASE_URI=database,
        # URL pubblico scritto negli adesivi NFC (es. https://portale.dovin.it)
        # su Render, finché non c'è il dominio definitivo, vale l'indirizzo assegnato da Render
        BASE_URL=os.environ.get("DOVIN_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL", ""),
        # demo: l'admin può eliminare anche auto con DDT (in produzione i DDT non si cancellano mai)
        DEMO=os.environ.get("DOVIN_DEMO") == "1",
        # indirizzo ufficiale per Google e per le anteprime dei link (canonical, sitemap)
        SITO_URL=(os.environ.get("GETVINGO_SITO_URL") or "https://getvingo.com").rstrip("/"),
        CSRF_ATTIVO=True,
        MAX_CONTENT_LENGTH=40 * 1024 * 1024,  # fino a qualche foto di danni per invio
        # archivio PDF dei DDT su storage S3 compatibile (senza bucket: cartella instance/archivio-ddt)
        ARCHIVIO_S3_BUCKET=os.environ.get("DOVIN_S3_BUCKET", ""),
        ARCHIVIO_S3_ENDPOINT=os.environ.get("DOVIN_S3_ENDPOINT", ""),  # es. https://<account>.r2.cloudflarestorage.com
        ARCHIVIO_S3_REGIONE=os.environ.get("DOVIN_S3_REGIONE", ""),
        ARCHIVIO_S3_CHIAVE_ID=os.environ.get("DOVIN_S3_CHIAVE_ID", ""),
        ARCHIVIO_S3_SEGRETO=os.environ.get("DOVIN_S3_SEGRETO", ""),
    )
    # cookie della sessione: non leggibili da script, mai inviati da altri siti, solo HTTPS se online
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=app.config["BASE_URL"].startswith("https://"),
                      PERMANENT_SESSION_LIFETIME=timedelta(days=30))
    if config:
        app.config.update(config)
    # nei test niente download dei loghi da internet
    app.config.setdefault("MARCHI_SCARICA", not app.config.get("TESTING"))
    if "SECRET_KEY" not in (config or {}):
        app.config["SECRET_KEY"] = _chiave_segreta(app)

    app.config.setdefault("FOTO_DIR", os.path.join(app.instance_path, "foto"))
    os.makedirs(app.config["FOTO_DIR"], exist_ok=True)
    app.config.setdefault("ARCHIVIO_DIR", os.path.join(app.instance_path, "archivio-ddt"))
    db.init_app(app)
    from . import migrazioni
    with app.app_context():
        if migrazioni.aggiorna_schema():
            app.logger.warning("Database aggiornato al portale multi-cliente (copia: *.prima-multi-cliente)")
        if "organizzazione" in db.inspect(db.engine).get_table_names():
            try:
                db.create_all()  # aggiunge le tabelle nuove (es. archivio DDT) ai database esistenti
            except OperationalError:  # creata nello stesso istante da un altro processo del server
                pass
            try:
                migrazioni.aggiungi_colonne()
            except OperationalError:
                pass

    from . import admin, archivio, auth, filtri, piattaforma, routes
    app.register_blueprint(auth.bp)
    app.register_blueprint(routes.bp)
    app.register_blueprint(admin.bp)
    app.register_blueprint(piattaforma.bp)
    auth.init_app(app)
    archivio.init_app(app)
    filtri.init_app(app)

    def _nuovo_cliente(azienda, admin_email, admin_password):
        from . import servizi
        try:
            org = servizi.crea_organizzazione(azienda, admin_email, admin_password)
        except servizi.Vietato as e:
            raise click.ClickException(str(e))
        click.echo(f"Cliente “{org.nome}” creato (n. {org.id}). Accesso: {admin_email}")

    # le variabili GETVINGO_* permettono il primo avvio senza domande (job su Google Cloud)
    @app.cli.command("init-db")
    @click.option("--admin-email", prompt=True, envvar="GETVINGO_ADMIN_EMAIL")
    @click.option("--admin-password", prompt=True, hide_input=True, confirmation_prompt=True,
                  envvar="GETVINGO_ADMIN_PASSWORD")
    @click.option("--azienda", prompt="Ragione sociale concessionaria", envvar="GETVINGO_AZIENDA")
    def init_db(admin_email, admin_password, azienda):
        """Crea le tabelle e il primo cliente con il suo amministratore."""
        db.create_all()
        _nuovo_cliente(azienda, admin_email, admin_password)

    @app.cli.command("primo-avvio")
    def primo_avvio():
        """Prima di ogni avvio online: crea le tabelle mancanti e, solo se il portale è vuoto,
        il primo cliente da GETVINGO_AZIENDA / GETVINGO_ADMIN_EMAIL / GETVINGO_ADMIN_PASSWORD."""
        from .models import Organizzazione
        db.create_all()
        if Organizzazione.query.execution_options(tutte_le_organizzazioni=True).first():
            click.echo("Portale già inizializzato.")
            return
        dati = [os.environ.get(k, "") for k in ("GETVINGO_AZIENDA", "GETVINGO_ADMIN_EMAIL",
                                                 "GETVINGO_ADMIN_PASSWORD")]
        if not all(dati):
            raise click.ClickException("Portale vuoto: imposta GETVINGO_AZIENDA, GETVINGO_ADMIN_EMAIL "
                                       "e GETVINGO_ADMIN_PASSWORD per creare il primo cliente.")
        _nuovo_cliente(*dati)

    @app.cli.command("nuovo-cliente")
    @click.option("--azienda", prompt="Ragione sociale concessionaria")
    @click.option("--admin-email", prompt="Email dell'amministratore")
    @click.option("--admin-password", prompt="Password", hide_input=True, confirmation_prompt=True)
    def nuovo_cliente(azienda, admin_email, admin_password):
        """Aggiunge una concessionaria al portale: dati separati da tutte le altre."""
        _nuovo_cliente(azienda, admin_email, admin_password)

    @app.cli.command("backup")
    @click.option("--cartella", default=None, help="Dove salvare (predefinito: instance/backup)")
    @click.option("--tieni", default=14, help="Quante copie conservare")
    def backup_cmd(cartella, tieni):
        """Copia di sicurezza di database e foto (da lanciare ogni giorno)."""
        from . import backup
        click.echo("Backup salvato: " + backup.esegui(cartella or os.path.join(app.instance_path, "backup"), tieni))

    @app.cli.command("archivia-ddt")
    def archivia_ddt_cmd():
        """Archivia i PDF dei DDT non ancora in archivio (recupero dopo un invio non riuscito)."""
        from . import archivio
        nuovi, dove = archivio.archivia_tutti()
        click.echo(f"DDT archiviati: {nuovi} ({dove})")

    @app.cli.command("aggiorna-loghi")
    def aggiorna_loghi_cmd():
        """Riscarica i loghi delle marche presenti (versione più recente disponibile)."""
        from . import marchi
        from .models import Veicolo
        marche = sorted({v.marca for v in Veicolo.query.execution_options(tutte_le_organizzazioni=True) if v.marca})
        for m in marche:
            click.echo(f"{m}: {'ok' if marchi.assicura(m, forza=True) else 'non trovato (restano le iniziali)'}")

    @app.cli.command("demo")
    def demo_cmd():
        """Database di prova con utenti, sedi, auto e DDT (password: demo). Cancella il db!"""
        from . import demo
        demo.carica()
        click.echo("Demo pronta. Accessi (password: demo): admin@demo.it, ufficio@demo.it, "
                   "autista@demo.it, carrozzeria@demo.it, officina@demo.it")

    return app
