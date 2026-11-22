"""Vetture di prova in una concessionaria vera (22/11/2026, richiesta dell'utente): la stessa simulazione della demo
(una trentina di auto in ogni stato, con DDT, lavorazioni, preparazioni, danni e consegne) aggiunta alla
concessionaria attiva SENZA cancellare niente. Usa i suoi piazzali; luoghi, partner e vettore di prova nascono
con "(prova)" nel nome e la mail @prova.getvingo.invalid, e "Svuota per un nuovo test" li toglie insieme alle auto.
Adesivi di prova con codici a parte (SIGLA-P001…): non consumano la numerazione vera."""
import secrets

from flask import g

from . import demo, servizi
from .models import Azienda, Organizzazione, Sede, Tag, Utente, Veicolo, Vettore, db

SUFFISSO = " (prova)"
DOMINIO = "prova.getvingo.invalid"
VIN_SPIA = "WVWZZZCDZRW104381"  # la Golf della demo: se c'è già, le vetture di prova sono già caricate


class GiaCaricate(Exception):
    pass


def gia_caricate():
    return Veicolo.query.filter_by(vin=VIN_SPIA).first() is not None


def carica(admin):
    """Aggiunge le vetture di prova alla concessionaria attiva. admin = chi lo chiede (fa da personale interno)."""
    if gia_caricate():
        raise GiaCaricate("Le vetture di prova ci sono già: per ricaricarle prima svuota il portale.")
    org = db.session.get(Organizzazione, g.org_id)
    if not org.sigla:
        org.sigla = servizi.genera_sigla(org.nome)
    azienda = Azienda.query.first()
    base_lat = (azienda.lat if azienda and azienda.lat else None) or 45.6672
    base_lon = (azienda.lon if azienda and azienda.lon else None) or 12.2431

    # piazzali della concessionaria (la sede legale non è un piazzale); se mancano, uno di prova
    piazzali = [s for s in Sede.query.filter_by(tipo="propria", attiva=True).order_by(Sede.id) if not s.sede_legale]
    nuovi = []
    if not piazzali:
        p = Sede(nome="Piazzale" + SUFFISSO, tipo="propria", indirizzo="Via di prova 1", comune="Treviso", provincia="TV",
                 lat=base_lat, lon=base_lon, posti=40)
        nuovi.append(p)
        piazzali = [p]
    for s in piazzali:
        if s.lat is None:  # senza posizione le auto non si vedono sulla mappa: la prendo dall'azienda
            s.lat, s.lon = base_lat, base_lon
    if len(piazzali) < 2:  # gli spostamenti tra sedi vogliono due piazzali: il secondo è di prova
        p2 = Sede(nome="Piazzale 2" + SUFFISSO, tipo="propria", indirizzo="Via di prova 2", comune="Treviso",
                  provincia="TV", lat=piazzali[0].lat + 0.038, lon=piazzali[0].lon + 0.004, posti=5)
        nuovi.append(p2)
        piazzali.append(p2)
    principale, secondo = piazzali[0], piazzali[1]

    def luogo(nome, tipo, dlat, dlon, firma):
        return Sede(nome=nome + SUFFISSO, tipo=tipo, indirizzo="Via di prova", comune="Treviso", provincia="TV",
                    piva="00000000000", codice_fiscale="00000000000", lat=base_lat + dlat, lon=base_lon + dlon,
                    firma=demo._png_firma(firma))
    carrozzeria = luogo("Carrozzeria Rossi", "carrozzeria", 0.041, -0.033, 2)
    officina = luogo("Officina Bianchi", "officina", 0.023, -0.008, 5)
    detailing = luogo("Lucido Detailing", "altro", -0.005, -0.025, 6)
    vettore = Vettore(nome="Trasporti Bianchi" + SUFFISSO, piva="00000000000", codice_fiscale="00000000000",
                      indirizzo="Via di prova", comune="Treviso", provincia="TV", firma=demo._png_firma(4))
    db.session.add_all(nuovi + [carrozzeria, officina, detailing, vettore])
    db.session.flush()

    # persone: il personale interno è chi chiede la prova; i partner sono account di prova senza password
    def partner(nome, sede, chiave):
        u = Utente(nome=nome + SUFFISSO, email=f"{chiave}-{org.id}@{DOMINIO}", ruolo="fornitore", sede=sede)
        u.imposta_password(secrets.token_urlsafe(32))  # nessuno la conosce: l'account non si usa per entrare
        db.session.add(u)
        return u
    utenti = {k: admin for k in ("admin", "ufficio", "autista", "venditore", "preparatore", "responsabile")}
    utenti["carrozzeria"] = partner("Paolo Rossi", carrozzeria, "carrozzeria")
    utenti["officina"] = partner("Anna Bianchi", officina, "officina")
    db.session.flush()

    def nuovo_tag(n):
        return Tag(codice=f"{org.sigla}-P{n:03d}", origine="manuale")

    demo._popola(principale, secondo, carrozzeria, officina, detailing, vettore, vettore, utenti, nuovo_tag, liberi=False)
    return Veicolo.query.filter(Veicolo.stato != "consegnato").count()


def togli_luoghi_e_persone_di_prova(org_id):
    """Dopo lo svuotamento: via partner, vettore e luoghi di prova di questa concessionaria (non hanno più auto
    né DDT collegati). Filtrati per org_id anche da terminale, dove il filtro automatico non c'è."""
    tutte = {"tutte_le_organizzazioni": True}
    tolte = 0
    for u in Utente.query.filter(Utente.org_id == org_id, Utente.email.like(f"%@{DOMINIO}")).execution_options(**tutte).all():
        db.session.delete(u)
        tolte += 1
    db.session.flush()
    for vt in Vettore.query.filter(Vettore.org_id == org_id, Vettore.nome.like("%" + SUFFISSO)).execution_options(**tutte).all():
        db.session.delete(vt)
    for s in Sede.query.filter(Sede.org_id == org_id, Sede.nome.like("%" + SUFFISSO)).execution_options(**tutte).all():
        db.session.delete(s)
    return tolte
