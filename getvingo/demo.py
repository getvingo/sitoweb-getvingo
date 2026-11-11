"""Dati di prova: ogni situazione del flusso è rappresentata almeno una volta."""
import math
import struct
import zlib
from datetime import datetime, time, timedelta

from . import servizi
from flask import g

from .models import Azienda, DannoFoto, Movimento, Organizzazione, Sede, Tag, Utente, Veicolo, Vettore, adesso, db

PASSWORD = "demo"


def _png_firma(seme):
    """Firma finta: una curva disegnata in un PNG 300x100 (senza librerie esterne)."""
    w, h = 300, 100
    px = [[0] * w for _ in range(h)]
    for t in range(2000):
        x = int(20 + t * 0.13)
        y = int(55 + 22 * math.sin(t / (35 + seme * 7)) * math.cos(t / (90 + seme * 11)) - t * 0.004)
        for dx in (0, 1):
            for dy in (0, 1):
                if 0 <= x + dx < w and 0 <= y + dy < h:
                    px[y + dy][x + dx] = 255
    righe = b"".join(b"\x00" + bytes(c for a in r for c in (11, 22, 38, a)) for r in px)

    def blocco(tipo, dati):
        return struct.pack(">I", len(dati)) + tipo + dati + struct.pack(">I", zlib.crc32(tipo + dati))

    png = (b"\x89PNG\r\n\x1a\n" + blocco(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
           + blocco(b"IDAT", zlib.compress(righe, 9)) + blocco(b"IEND", b""))
    import base64
    return servizi.PREFISSO_FIRMA + base64.b64encode(png).decode()


def _indietro(**kw):
    return adesso() - timedelta(**kw)


def carica():
    db.drop_all()
    db.create_all()
    org = Organizzazione(nome="Concessionaria Demo S.r.l.", sigla="CDEMO")
    db.session.add(org)
    db.session.flush()
    g.org_id = org.id  # tutto ciò che segue appartiene alla concessionaria demo
    db.session.add(Azienda(ragione_sociale="Concessionaria Demo S.r.l.", piva="01234567897", codice_fiscale="01234567897",
                           indirizzo="Viale Europa 1", cap="31100", comune="Treviso", provincia="TV", stato="Italia",
                           lat=45.6672, lon=12.2431, firma=_png_firma(1)))
    treviso = Sede(nome="Sede Treviso", indirizzo="Viale Europa 1", cap="31100", comune="Treviso", provincia="TV", tipo="propria", sede_legale=True,
                   lat=45.6672, lon=12.2431)
    villorba = Sede(nome="Piazzale Villorba", indirizzo="Via Postumia 50", cap="31020", comune="Villorba", provincia="TV",
                    tipo="propria", lat=45.7051, lon=12.2468)
    carrozzeria = Sede(nome="Carrozzeria Rossi", indirizzo="Via Roma 12", cap="31050", comune="Ponzano Veneto", provincia="TV", codice_fiscale="09876543210",
                       piva="09876543210", tipo="carrozzeria", lat=45.7089, lon=12.2101, firma=_png_firma(2))
    officina = Sede(nome="Officina Bianchi", indirizzo="Via Feltrina 88", cap="31100", comune="Treviso", provincia="TV", codice_fiscale="05556667778",
                    piva="05556667778", tipo="officina", lat=45.6905, lon=12.2350)
    detailing = Sede(nome="Lucido Detailing", indirizzo="Via Noalese 3", cap="31100", comune="Treviso", provincia="TV",
                     tipo="altro", lat=45.6620, lon=12.2180)
    proprio = Vettore(nome="Concessionaria Demo S.r.l.", proprio=True, piva="01234567897", codice_fiscale="01234567897",
                      indirizzo="Viale Europa 1", cap="31100", comune="Treviso", provincia="TV", lat=45.6672, lon=12.2431)
    esterno = Vettore(nome="Trasporti Bianchi S.r.l.", piva="11223344556", codice_fiscale="11223344556",
                      indirizzo="Via Industria 5", cap="31030", comune="Casier", provincia="TV")
    db.session.add_all([treviso, villorba, carrozzeria, officina, detailing, proprio, esterno])
    db.session.flush()

    utenti = {}
    for chiave, nome, email, ruolo, sede in [
            ("admin", "Admin Demo", "admin@demo.it", "admin", None),
            ("ufficio", "Laura Backoffice", "ufficio@demo.it", "backoffice", None),
            ("autista", "Mario Consegne", "autista@demo.it", "consegne", None),
            ("venditore", "Giulia Venditrice", "venditore@demo.it", "venditore", None),
            ("preparatore", "Luca Preparatore", "preparatore@demo.it", "preparatore", None),
            ("responsabile", "Elena Responsabile", "responsabile@demo.it", "resp_commerciale", None),
            ("carrozzeria", "Paolo Rossi", "carrozzeria@demo.it", "fornitore", carrozzeria),
            ("carrozzeria2", "Sara Rossi", "carrozzeria2@demo.it", "fornitore", carrozzeria),
            ("officina", "Anna Bianchi", "officina@demo.it", "fornitore", officina)]:
        u = Utente(nome=nome, email=email, ruolo=ruolo, sede=sede)
        if sede is None:  # profilo di guida già pronto: "Inizia spostamento" chiede solo la destinazione
            u.vettore, u.mezzo, u.firma = proprio, "Su strada (guidata)", _png_firma(3)
        u.imposta_password(PASSWORD)
        db.session.add(u)
        utenti[chiave] = u
    db.session.flush()

    from flask import current_app
    import base64 as b64
    import os
    firma_autista = _png_firma(3)
    MEZZO = Movimento.MEZZI  # su strada, bisarca, carro attrezzi, furgone/carrello, altro
    contatore_tag = [0]

    def foto_demo(seme):
        nome = f"demo-danno-{seme}.png"
        with open(os.path.join(current_app.config["FOTO_DIR"], nome), "wb") as f:
            f.write(b64.b64decode(_png_firma(seme).split(",", 1)[1]))
        return nome

    def auto(vin, marca, modello, colore, sede, condizione="nuovo", tipologia="vettura", targa=None, dove=(0, 0),
             versione=""):
        """Auto in stock con il suo adesivo NFC (senza adesivo non entra)."""
        contatore_tag[0] += 1
        v = Veicolo(vin=vin, marca=marca, modello=modello, versione=versione, colore=colore, sede=sede, stato="in_sede",
                    condizione=condizione, tipologia=tipologia, targa=targa,
                    lat=sede.lat + dove[0], lon=sede.lon + dove[1], posizione_at=_indietro(days=20))
        db.session.add_all([v, Tag(codice=f"DEMO{contatore_tag[0]:04d}", veicolo=v, abbinato_at=_indietro(days=20))])
        db.session.flush()
        return v

    def viaggio(v, dest, causale, lavorazione, note, quando, mezzo=MEZZO[0], vettore=proprio, conducente="Mario Consegne"):
        dati = dict(a_sede_id=dest.id, causale=causale, vettore_id=vettore.id, conducente=conducente,
                    lavorazione=lavorazione, lavorazione_note=note, mezzo=mezzo)
        return servizi.crea_movimento(v, dati, utenti["autista"], firma_conducente=firma_autista,
                                      partenza_at=quando).movimento

    def arriva(m, chi, quando):
        servizi.registra_arrivo(m, chi, m.a_sede.lat, m.a_sede.lon)  # sul posto, dentro l'area
        m.arrivo_at = quando

    def in_lavorazione(v, fornitore, chi, lavorazione, note, giorni, mezzo=MEZZO[0], vettore=proprio, conducente="Mario Consegne"):
        m = viaggio(v, fornitore, "Invio lavorazione",
                    lavorazione, note, _indietro(days=giorni, hours=2), mezzo, vettore, conducente)
        arriva(m, chi, _indietro(days=giorni))
        return m

    def rientro_in_sede(v, fornitore, chi, giorni_fa, giorni_lav, ore_attesa, mezzo=MEZZO[0]):
        """Andata, lavorazione, DDT di rientro preparato dal fornitore, ritiro e arrivo: l'auto va in preparazione."""
        m = in_lavorazione(v, fornitore, chi, "Carrozzeria", "Ritocchi carrozzeria", giorni_fa)
        rientro = servizi.completa_lavorazione(m, chi, lat=fornitore.lat, lon=fornitore.lon,
                                               chi_riporta=utenti["autista"].id).movimento
        m.lavorazione_completata_at = _indietro(days=giorni_fa - giorni_lav)
        servizi.prendi_in_carico(rientro, utenti["autista"], "Mario Consegne", firma_autista,
                                 fornitore.lat, fornitore.lon, mezzo=mezzo)
        rientro.partenza_at = m.lavorazione_completata_at + timedelta(hours=ore_attesa)
        rientro.ddt.emesso_at = m.lavorazione_completata_at
        arriva(rientro, utenti["autista"], rientro.partenza_at + timedelta(minutes=35))
        db.session.flush()
        prep = v.preparazioni[-1]
        prep.creata_at = rientro.arrivo_at
        return prep

    def passi(prep, quanti, ogni_ore=3):
        for k in range(quanti):
            servizi.avanza_preparazione(prep, utenti["autista"])
            prep.fasi_fatte[-1].at = prep.creata_at + timedelta(hours=(k + 1) * ogni_ore)
        if prep.completata_at:
            prep.completata_at = prep.fasi_fatte[-1].at

    def pronta_diretta(v, giorni_fa):
        """Auto venduta che non va dai fornitori: preparazione completa."""
        prep = servizi.metti_in_preparazione(v, utenti["ufficio"])
        prep.creata_at = _indietro(days=giorni_fa)
        passi(prep, 4, ogni_ore=4)
        return prep

    def consegnata(v, sede, quando):
        pronta_diretta(v, (adesso() - quando).days + 2)
        servizi.imposta_consegna(v, utenti["ufficio"], sede)
        if not v.targa:  # senza targa non si consegna: la si mette all'immatricolazione
            v.targa = "GV" + v.vin[-3:] + "EM"
        servizi.consegna_al_cliente(v, utenti["ufficio"], v.sede.lat, v.sede.lon)
        v.consegnato_at = quando

    # orari della demo sempre negli slot di 30 minuti (lun–ven 8:30–12:30 e 14:30–18:30)
    def slot_da(momento):
        return servizi.primo_slot_libero(momento)

    def slot_passato():
        """Uno slot dell'ultimo giorno lavorativo prima di oggi (preparazione in ritardo)."""
        giorno = adesso().date() - timedelta(days=1)
        while not servizi._slot_del_giorno(giorno):
            giorno -= timedelta(days=1)
        return datetime.combine(giorno, time(17, 0))

    # "entro oggi": il primo slot libero da adesso (se oggi non ce ne sono più, il primo del giorno lavorativo dopo)
    oggi_tardi = slot_da(adesso())

    # ------------------------------------------------------------- STOCK (nuove e usate, vetture e commerciali)
    golf_bianca = auto("WVWZZZCDZRW104999", "Volkswagen", "Golf 8 eTSI", "Bianco Puro", villorba, dove=(0.00021, -0.00032),
                       versione="1.5 eTSI 150 CV Life DSG")
    t_roc = auto("WVGZZZA1ZRV201457", "Volkswagen", "T-Roc", "Blu Ravenna", villorba, dove=(-0.00012, 0.00022),
                 versione="1.0 TSI 115 CV Style")
    captur = auto("VF1RJB00Y72345601", "Renault", "Captur E-Tech", "Arancio Valencia", villorba, dove=(0.00008, 0.00041),
                  versione="Full Hybrid 145 CV Techno")
    chr_usata = auto("JTNKBBBX20R045812", "Toyota", "C-HR", "Grigio Celestite", villorba, "usato", targa="GE512LM",
                     dove=(-0.00025, -0.00010))
    transit = auto("WF0XXXTTGXRA77777", "Ford", "Transit Custom", "Grigio Magnetic", villorba, "usato", "commerciale",
                   "GK119PZ", dove=(-0.00027, 0.00018))
    peugeot = auto("VR3UPHNKSRT006789", "Peugeot", "208 Allure", "Rosso Elixir", treviso, "usato", targa="GH482KL",
                   dove=(-0.00018, 0.00055))
    ducato = auto("ZFA25000002K98765", "Fiat", "Ducato Furgone L2H2", "Bianco", treviso, tipologia="commerciale",
                  dove=(0.00052, 0.00012))
    sportage = auto("U5YPU81AGRL123987", "Kia", "Sportage HEV", "Verde Experience", treviso, dove=(0.00030, -0.00020))
    tonale = auto("ZARNAA6V2R7654321", "Alfa Romeo", "Tonale Veloce", "Rosso Alfa", treviso, dove=(-0.00005, -0.00035))
    formentor = auto("VSSZZZKMZRR098765", "Cupra", "Formentor VZ", "Grigio Magnetic", treviso, dove=(0.00015, 0.00030))

    # ------------------------------------------------------------- tutte le altre auto partono dalla sede di Treviso
    golf = auto("WVWZZZCDZRW104381", "Volkswagen", "Golf 8 eTSI", "Grigio Moonstone", treviso)
    aygo = auto("JTDKAAAB10A987654", "Toyota", "Aygo X", "Rosso Chili", treviso)
    clio = auto("VF1RJA00X71256044", "Renault", "Clio E-Tech", "Bianco Ghiaccio", treviso)
    duster = auto("UU1HSDAH567890123", "Dacia", "Duster Journey", "Grigio Urban", treviso)
    transit2 = auto("WF0XXXTTGXRB12345", "Ford", "Transit Connect", "Bianco Frozen", treviso, "usato", "commerciale", "GL208XY")
    fiat600 = auto("ZFA3120000P915622", "Fiat", "600e La Prima", "Arancio Sole", treviso)
    corsa = auto("W0VPAAHX0R1234567", "Opel", "Corsa Electric", "Giallo Kult", treviso)
    clio_rientro = auto("VF1RJA00X71256099", "Renault", "Clio E-Tech", "Blu Iron", treviso)
    cx30 = auto("JMZDM6WE600123456", "Mazda", "CX-30", "Rosso Soul", treviso)
    a3 = auto("WAUZZZGY5RA037752", "Audi", "A3 Sportback", "Nero Mythos", treviso)
    serie1 = auto("WBA7K310X07654321", "BMW", "Serie 1 118i", "Blu Portimao", treviso)
    octavia = auto("TMBJJ7NX5R1234567", "Skoda", "Octavia Wagon", "Argento Brillante", treviso)
    ducato2 = auto("ZFA25000002K11111", "Fiat", "Ducato Maxi", "Bianco", treviso, tipologia="commerciale")
    puma = auto("WF0FXXGCHFRA12345", "Ford", "Puma ST-Line", "Blu Desert Island", treviso)
    tucson = auto("TMAJ381AAPJ123456", "Hyundai", "Tucson Hybrid", "Bianco Atlas", treviso)
    qashqai = auto("SJNFAAJ11U2345678", "Nissan", "Qashqai e-Power", "Grigio Ceramic", treviso)
    xc40 = auto("YV1XZK9V1R2345678", "Volvo", "XC40 Recharge", "Verde Sage", treviso)
    yaris = auto("JTDKBABB80A123456", "Toyota", "Yaris Cross", "Bianco Perla", treviso)
    renegade = auto("1C4BU0000PP123456", "Jeep", "Renegade 4xe", "Verde Matter", treviso)
    classe_a = auto("W1K1770841N123456", "Mercedes-Benz", "Classe A 180", "Nero Cosmo", treviso, "usato", targa="GD903RS")
    troc = auto("WVGZZZA1ZRV077123", "Volkswagen", "T-Roc", "Grigio Indium", treviso, targa="GH512KT")
    avenger = auto("ZACNJABB4PPA54321", "Jeep", "Avenger Altitude", "Verde Granite", villorba)
    c3 = auto("VF7SXHMZ6RT123456", "Citroën", "C3 Shine", "Rosso Elixir", treviso, "usato", targa="FY228KD")
    smart = auto("LEVBAAAA1PE123456", "Smart", "#1 Pulse", "Bianco Digital", treviso)
    ypsilon = auto("ZLA31200000123456", "Lancia", "Ypsilon LX", "Blu Cassiopea", treviso)

    # ------------------------------------------------------------- CONSEGNATE (calendario: mese scorso e questo)
    consegnata(smart, treviso, (adesso().replace(day=1) - timedelta(days=9)).replace(hour=15, minute=30))
    consegnata(c3, treviso, _indietro(days=12).replace(hour=16, minute=15))
    consegnata(avenger, villorba, _indietro(days=3).replace(hour=10, minute=30))
    consegnata(ypsilon, treviso, _indietro(days=1).replace(hour=11, minute=0))

    # ------------------------------------------------------------- PREPARAZIONE e PRONTE
    prep = rientro_in_sede(puma, carrozzeria, utenti["carrozzeria"], giorni_fa=6, giorni_lav=5, ore_attesa=18)
    passi(prep, 3)  # prossimo passo: Pronta -> bloccato dal danno qui sotto (il lavaggio si fa)
    prep.pronta_entro_at = slot_da((adesso() + timedelta(days=1)).replace(hour=12, minute=0, second=0))
    rientro_puma = [m for m in puma.movimenti if m.causale == "Reso lavorazione"][0]
    servizi.segnala_danno(puma, utenti["autista"], "Cerchi / gomme", "Cerchio anteriore destro rigato al rientro",
                          [foto_demo(8), foto_demo(9)], movimento=rientro_puma)
    for v, entro, fatti, ore_fa in ((tucson, oggi_tardi, 1, 6),                                          # entro oggi
                                    (qashqai, slot_passato(), 2, 40),                                   # in ritardo
                                    (xc40, slot_da((adesso() + timedelta(days=3)).replace(hour=12, minute=0)), 0, 2)):
        prep = servizi.metti_in_preparazione(v, utenti["ufficio"], entro)
        prep.creata_at = _indietro(hours=ore_fa)
        passi(prep, fatti, ogni_ore=2)
    prep = rientro_in_sede(yaris, carrozzeria, utenti["carrozzeria"], giorni_fa=8, giorni_lav=4, ore_attesa=26)
    passi(prep, 4)                                                                           # pronta, sede da indicare
    pronta_diretta(renegade, 4)
    servizi.imposta_consegna(renegade, utenti["ufficio"], villorba)                          # serve l'ultimo DDT
    pronta_diretta(classe_a, 3)
    servizi.imposta_consegna(classe_a, utenti["ufficio"], treviso)
    pronta_diretta(troc, 3)                         # già in viaggio con l'ultimo DDT: resta tra le pronte fino alla consegna
    servizi.imposta_consegna(troc, utenti["ufficio"], villorba)
    viaggio(troc, villorba, "Spostamento", Movimento.NESSUNA, "", _indietro(minutes=35))

    # ------------------------------------------------------------- DAI FORNITORI
    m = in_lavorazione(a3, carrozzeria, utenti["carrozzeria"], "Carrozzeria", "Paraurti anteriore", 16,
                       MEZZO[2], esterno, "Giulia Verdi (Trasporti Bianchi)")                  # urgente
    servizi.modifica_ddt(m.ddt, {"a_sede_id": carrozzeria.id, "causale": "Invio lavorazione", "vettore_id": esterno.id,
                                 "conducente": "Giulia Verdi (Trasporti Bianchi)", "lavorazione": "Carrozzeria",
                                 "lavorazione_note": "Paraurti anteriore + sensore parcheggio", "mezzo": MEZZO[2]},
                         utenti["ufficio"])
    in_lavorazione(serie1, officina, utenti["officina"], "Meccanica / tagliando", "Tagliando 30.000 km + freni", 9)
    in_lavorazione(octavia, detailing, utenti["admin"], "Lavaggio / detailing", "Lucidatura e trattamento interni", 2,
                   MEZZO[3])
    in_lavorazione(ducato2, carrozzeria, utenti["carrozzeria"], "Nessuna (solo trasferimento)", "", 4)  # non in Lavorazioni
    m = in_lavorazione(cx30, carrozzeria, utenti["carrozzeria"], "Pellicole / oscuramento vetri", "Vetri posteriori 20%", 5)
    servizi.completa_lavorazione(m, utenti["carrozzeria"], "Pellicole montate", prepara_rientro=False,
                                 lat=carrozzeria.lat, lon=carrozzeria.lon)  # attesa DDT

    # ------------------------------------------------------------- DA RITIRARE
    m = in_lavorazione(clio_rientro, officina, utenti["officina"], "Meccanica / tagliando", "Tagliando pre-consegna", 3)
    servizi.completa_lavorazione(m, utenti["officina"], "Tagliando fatto, filtri sostituiti", chi_riporta=utenti["autista"].id,
                                 lat=officina.lat, lon=officina.lon)  # rientro pronto
    m.lavorazione_completata_at = _indietro(days=1)
    servizi.crea_movimento(fiat600, dict(a_sede_id=detailing.id, causale="Invio lavorazione", vettore_id=proprio.id,
                                         conducente="", lavorazione="Lavaggio / detailing",
                                         lavorazione_note="Trattamento ceramico", mezzo=MEZZO[0]),
                           utenti["ufficio"], predisponi_ore=4, assegnato=utenti["autista"])                                  # entro oggi
    scaduto = servizi.crea_movimento(corsa, dict(a_sede_id=carrozzeria.id, causale="Invio lavorazione",
                                                 vettore_id=esterno.id, conducente="", lavorazione="Carrozzeria",
                                                 lavorazione_note="Specchietto destro", mezzo=MEZZO[2]),
                                     utenti["ufficio"], predisponi_ore=24, assegnato=utenti["autista"])
    scaduto.movimento.ritiro_entro_at = _indietro(hours=5)                                      # ritiro scaduto

    # ------------------------------------------------------------- IN VIAGGIO
    viaggio(golf, carrozzeria, "Invio lavorazione", "Carrozzeria", "Graffio portiera post. sx", _indietro(minutes=50))
    viaggio(transit2, officina, "Invio lavorazione", "Meccanica / tagliando", "Non parte: batteria e motorino",
            _indietro(hours=1, minutes=20), MEZZO[2], esterno, "Giulia Verdi (Trasporti Bianchi)")
    multiplo = servizi.crea_ddt_multiplo([aygo, clio, duster], dict(
        a_sede_id=villorba.id, causale="Spostamento", vettore_id=esterno.id,
        conducente="Trasporti Bianchi (bisarca)", lavorazione="Nessuna (solo trasferimento)", lavorazione_note="",
        mezzo=MEZZO[1]), utenti["autista"], firma_conducente=firma_autista)
    for mv in multiplo.movimenti:
        mv.partenza_at = _indietro(minutes=25)

    # ------------------------------------------------------------- DDT cancellato, danni, adesivi liberi
    d = viaggio(peugeot, villorba, "Spostamento", "Nessuna (solo trasferimento)", "", _indietro(hours=3))
    servizi.annulla_ddt(d.ddt, utenti["admin"], "Creato sull'auto sbagliata")
    servizi.segnala_danno(golf_bianca, utenti["autista"], "Lato sinistro",
                          "Graffio di circa 10 cm sulla portiera posteriore", [foto_demo(7)])
    danno = servizi.segnala_danno(sportage, utenti["autista"], "Anteriore", "Piccola ammaccatura sul paraurti",
                                  [foto_demo(10)])
    danno.segnalato_at = _indietro(days=5)
    servizi.risolvi_danno(danno, utenti["ufficio"], "Riparato con levabolli", [foto_demo(11)])
    for n in range(5):
        db.session.add(Tag(codice=f"LIBERO{n + 1:02d}"))
    db.session.commit()
