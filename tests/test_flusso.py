"""Test dei flussi Getvingo: abbinamento, DDT, firme, fornitori, preparazione, annullamento."""
import io
import tempfile
import unittest
from datetime import datetime, timedelta

from getvingo import archivio, auth, create_app, servizi
from getvingo.demo import _png_firma
from flask import appcontext_pushed, g

from getvingo.models import (Azienda, Azione, Danno, Ddt, DdtArchivio, Movimento, Organizzazione, Preparazione, Sede,
                            Sollecito, Tag, Utente, Veicolo, Vettore, adesso, db)
from getvingo.vin import normalizza, valido

VIN = "WVWZZZ1KZAW000123"
POS = {"lat": "45.66", "lon": "12.24"}  # posizione del telefono al tocco dell'adesivo
FIRMA = _png_firma(1)


def usa_org_1(app):
    """Nei test i dati creati fuori da una richiesta appartengono all'organizzazione 1
    (nelle richieste vale quella dell'utente collegato)."""
    def imposta(sender, **kw):
        g.org_id = 1
    appcontext_pushed.connect(imposta, app, weak=False)


class Base(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://",
                               "CSRF_ATTIVO": False, "SECRET_KEY": "test", "FOTO_DIR": tempfile.mkdtemp(),
                               "ARCHIVIO_DIR": tempfile.mkdtemp()})
        usa_org_1(self.app)
        auth._falliti.clear()
        with self.app.app_context():
            db.create_all()
            db.session.add(Organizzazione(id=1, nome="Test S.r.l."))
            db.session.add(Azienda(ragione_sociale="Test S.r.l.", indirizzo="Via Test 1", piva="123",
                                   firma=FIRMA))
            carr = Sede(nome="Carrozzeria B", tipo="carrozzeria", lat=45.1, lon=12.1, firma=FIRMA)
            db.session.add_all([
                Tag(codice="ABCD2345"), Sede(nome="Sede A", tipo="propria", lat=45.0, lon=12.0), carr,
                Sede(nome="Cliente C", tipo="cliente", lat=45.3, lon=12.3),
                Sede(nome="Officina D", tipo="officina", lat=45.4, lon=12.4),
                Vettore(nome="Vettore X"),
            ])
            db.session.flush()
            for nome, ruolo, sede in [("admin", "admin", None), ("ufficio", "ufficio", None),
                                      ("autista", "autista", None), ("carr", "fornitore", carr)]:
                u = Utente(nome=nome.capitalize(), email=f"{nome}@t.it", ruolo=ruolo, sede=sede)
                u.imposta_password("pw")
                db.session.add(u)
            db.session.commit()
        self.c = self.app.test_client()
        self.entra("admin")

    def entra(self, chi):
        self.c.post("/logout")
        self.c.post("/login", data={"email": f"{chi}@t.it", "password": "pw"})

    def _id(self, modello, **filtro):
        with self.app.app_context():
            return modello.query.filter_by(**filtro).first().id

    def abbina(self):
        r = self.c.post("/abbina/ABCD2345", data={
            "vin": "wvwzzz1kzaw000123", "marca": "VW", "modello": "Golf", "colore": "Bianco", "condizione": "nuovo",
            "sede_id": self._id(Sede, nome="Sede A"), "lat": "45.66", "lon": "12.24"})
        self.assertEqual(r.status_code, 302)
        return self._id(Veicolo, vin=VIN)

    def dati(self, sede, causale="Conto riparazione", lavorazione="Carrozzeria", **extra):
        d = {"a_sede_id": self._id(Sede, nome=sede), "causale": causale,
             "vettore_id": self._id(Vettore, nome="Vettore X"), "conducente": "Mario Rossi",
             "lavorazione": lavorazione, "lavorazione_note": "paraurti", "firma": FIRMA,
             "mezzo": "Su strada (guidata)"}
        d.update(extra)
        return d

    def arrivo(self, ddt_id, **data):
        """Conferma l'arrivo dal punto della destinazione (il telefono è dentro l'area)."""
        with self.app.app_context():
            dest = db.session.get(Ddt, ddt_id).movimento.a_sede
            data = {"lat": str(dest.lat), "lon": str(dest.lon), **data}
        return self.c.post(f"/ddt/{ddt_id}/arrivo", data=data)

    def parti(self, vid, sede, **kw):
        return self.c.post(f"/veicolo/{vid}/partenza", data=self.dati(sede, **kw))

    def ddt(self, **filtro):
        with self.app.app_context():
            return Ddt.query.filter_by(**filtro).order_by(Ddt.id.desc()).first().id

    def mov(self, ddt_id):
        return db.session.get(Ddt, ddt_id).movimento

    def avanza(self, prep_id, codice="ABCD2345"):
        """Un passo della preparazione: tocco dell'adesivo, poi "fatto" nella schermata del tocco."""
        with self.app.app_context():
            vid = db.session.get(Preparazione, prep_id).veicolo_id
        self.c.post(f"/t/{codice}", data=POS)
        return self.c.post(f"/veicolo/{vid}/tocco/passo")


class TestVinEAbbinamento(Base):
    def test_vin(self):
        self.assertEqual(normalizza(" wvw-zzz1kzaw00O123 "), VIN)
        self.assertTrue(valido(VIN))
        self.assertFalse(valido("WVWZZZ1KZAW00012"))

    def test_tap_tag_libero_porta_ad_abbinamento(self):
        self.assertIn("/abbina/ABCD2345", self.c.post("/t/abcd2345", data=POS).headers["Location"])

    def test_posizione_chiesta_solo_al_tocco(self):
        """Il GPS si chiede al tocco; l'abbinamento che segue usa quella posizione senza richiederla."""
        from getvingo.models import Evento
        self.c.post("/t/ABCD2345", data={"lat": "45.5", "lon": "12.5"})
        r = self.c.post("/abbina/ABCD2345", data={
            "vin": VIN, "marca": "VW", "modello": "Golf", "colore": "Bianco", "condizione": "nuovo",
            "sede_id": self._id(Sede, nome="Sede A")})
        self.assertEqual(r.status_code, 302)
        with self.app.app_context():
            e = Evento.query.filter_by(tipo="abbinamento").one()
            self.assertEqual((e.lat, e.lon), (45.5, 12.5))
        # un nuovo tocco senza posizione non usa quella vecchia: si ferma a chiedere il GPS
        r = self.c.post("/t/ABCD2345", data={})
        self.assertEqual(r.status_code, 200)
        # le pagine del portale non chiedono più la posizione
        self.assertNotIn("blocco-gps", self.c.get("/").get_data(as_text=True))

    def test_posizione_del_tocco_scade(self):
        from getvingo import routes
        self.c.post("/t/ABCD2345", data=POS)
        with self.c.session_transaction() as s:
            s["posizione_tocco"]["at"] -= routes.VALIDITA_TOCCO_S + 1
        self.c.post("/abbina/ABCD2345", data={
            "vin": VIN, "marca": "VW", "modello": "Golf", "colore": "Bianco", "condizione": "nuovo",
            "sede_id": self._id(Sede, nome="Sede A")})
        from getvingo.models import Evento
        with self.app.app_context():
            self.assertIsNone(Evento.query.filter_by(tipo="abbinamento").one().lat)

    def test_vin_non_valido_rifiutato(self):
        self.assertEqual(self.c.post("/abbina/ABCD2345", data={"vin": "CORTO", "sede_id": 1}).status_code, 200)
        with self.app.app_context():
            self.assertEqual(Veicolo.query.count(), 0)

    def test_import_csv_e_precompilazione(self):
        csv = f"vin;marca;modello;colore\n{VIN};Volkswagen;Golf;Nero\nERRATO;X;Y;Z\n".encode()
        self.c.post("/admin/import", data={"file": (io.BytesIO(csv), "stock.csv")},
                    content_type="multipart/form-data")
        self.assertEqual(self.c.get(f"/api/veicolo?vin={VIN}").json["modello"], "Golf")
        self.abbina()
        with self.app.app_context():
            self.assertEqual(Veicolo.query.count(), 1)


class TestPartenzaEArrivo(Base):
    def test_serve_la_firma_del_conducente(self):
        vid = self.abbina()
        self.parti(vid, "Sede A", firma="")
        self.parti(vid, "Carrozzeria B", firma="data:image/png;base64,bm9uLXBuZw==")
        with self.app.app_context():
            self.assertEqual(Ddt.query.count(), 0)

    def test_firme_automatiche_e_pdf(self):
        vid = self.abbina()
        r = self.parti(vid, "Carrozzeria B")
        d = self.ddt()
        self.assertIn(f"/ddt/{d}", r.headers["Location"])
        with self.app.app_context():
            documento = db.session.get(Ddt, d)
            self.assertEqual(documento.firma_mittente, FIRMA)
            self.assertEqual(documento.movimento.firma_conducente, FIRMA)
            self.assertEqual(documento.creato_da.email, "admin@t.it")
        self.assertTrue(self.c.get(f"/ddt/{d}.pdf").data.startswith(b"%PDF"))

    def test_arrivo_si_chiude_entro_200_metri(self):
        vid = self.abbina()
        self.entra("autista")
        self.parti(vid, "Carrozzeria B")
        d = self.ddt()
        self.c.post(f"/ddt/{d}/arrivo")  # senza posizione del telefono non si chiude
        self.c.post(f"/ddt/{d}/arrivo", data={"lat": "45.1030", "lon": "12.1"})  # ~330 m: fuori dall'area
        with self.app.app_context():
            self.assertEqual(self.mov(d).stato, "aperto")
        # chiunque sia dentro l'area chiude, anche l'autista presso un fornitore esterno
        self.c.post(f"/ddt/{d}/arrivo", data={"lat": "45.1015", "lon": "12.1", "note": "ok"})  # ~170 m
        with self.app.app_context():
            m = self.mov(d)
            self.assertEqual((m.stato, m.veicolo.sede.nome), ("chiuso", "Carrozzeria B"))
            self.assertEqual((m.veicolo.lat, m.veicolo.lon), (45.1015, 12.1))
            self.assertEqual(m.firma_destinatario, FIRMA)
            self.assertFalse(m.arrivo_per_conto)

    def test_admin_conferma_per_conto(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        d = self.ddt()
        self.c.post(f"/ddt/{d}/arrivo", data={"lat": "45.2", "lon": "12.2"})  # admin in ufficio, lontano
        with self.app.app_context():
            m = self.mov(d)
            self.assertTrue(m.arrivo_per_conto)
            self.assertEqual(m.veicolo.lat, 45.1)  # l'auto prende le coordinate della destinazione

    def test_arrivo_solo_nella_destinazione_del_ddt(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        d = self.ddt()
        self.c.post(f"/ddt/{d}/arrivo", data={"arrivo_sede_id": self._id(Sede, nome="Sede A")})
        with self.app.app_context():
            self.assertEqual(self.mov(d).stato, "aperto")

    def test_nota_nel_luogo_del_ddt(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        d = self.ddt()
        self.arrivo(d, note="graffio sul cofano")
        with self.app.app_context():
            self.assertEqual(self.mov(d).note_arrivo, "graffio sul cofano")

    def test_consegna_cliente_libera_adesivo(self):
        vid = self.abbina()
        self.parti(vid, "Cliente C", causale="Consegna a cliente")  # senza targa non parte verso il cliente
        with self.app.app_context():
            self.assertEqual(Ddt.query.count(), 0)
        self.c.post(f"/veicolo/{vid}/dati", data={"targa": "GH123AB"})
        self.parti(vid, "Cliente C", causale="Consegna a cliente")
        self.arrivo(self.ddt())
        with self.app.app_context():
            self.assertEqual(db.session.get(Veicolo, vid).stato, "consegnato")
            self.assertIsNone(Tag.query.filter_by(codice="ABCD2345").one().veicolo_id)


class TestSpostamentoColTocco(Base):
    """Tocco NFC -> "Inizia spostamento" -> solo la destinazione; lavorazione entro la chiusura."""
    def profilo(self, chi="autista"):
        self.entra(chi)
        self.c.post("/profilo-guida", data={"vettore_id": self._id(Vettore, nome="Vettore X"),
                                            "mezzo": "Bisarca", "firma": FIRMA})

    def test_tocco_mostra_dati_e_azioni(self):
        vid = self.abbina()
        self.entra("autista")
        r = self.c.post("/t/ABCD2345", data=POS)
        self.assertIn(f"/veicolo/{vid}/tocco", r.headers["Location"])
        pagina = self.c.get(f"/veicolo/{vid}/tocco").get_data(as_text=True)
        self.assertIn("Inizia spostamento", pagina)
        self.assertIn('class="carta-auto', pagina)  # dati dell'auto subito in alto

    def test_tocco_anteprima_dati_preparazione_e_danno(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        self.arrivo(self.ddt())
        self.entra("carr")
        self.c.post(f"/ddt/{self.ddt()}/lavorazione-completata", data={"rientro": "1"})
        self.entra("autista")
        self.c.post(f"/ddt/{self.ddt()}/presa-in-carico", data={"conducente": "Mario", "firma": FIRMA,
                                                                 "mezzo": "Bisarca"})
        self.arrivo(self.ddt())  # rientro in Sede A: parte da sola la preparazione
        with self.app.app_context():
            prep = Preparazione.query.one()
            prep.completata_at = prep.creata_at  # simula: già pronta e consegnabile in futuro
            db.session.get(Veicolo, vid).versione = "1.5 eTSI Life"
            db.session.commit()
        self.c.post("/t/ABCD2345", data=POS)
        pagina = self.c.get(f"/veicolo/{vid}/tocco").get_data(as_text=True)
        for testo in ('class="carta-auto', "Segnala danno", "1.5 eTSI Life", "ABCD2345", "Sede A", "toccando l&#39;adesivo",
                      "<span class=\"conta\">2</span>"):
            self.assertIn(testo, pagina)
        self.assertNotIn("Metti in preparazione", pagina)  # già pronta

    def test_tocco_metti_in_preparazione_senza_ddt(self):
        vid = self.abbina()
        self.entra("autista")
        pagina = self.c.get(f"/veicolo/{vid}/tocco").get_data(as_text=True)
        self.assertIn("Metti in preparazione", pagina)
        self.assertIn("Nessun DDT emesso finora", pagina)
        self.c.post(f"/veicolo/{vid}/preparazione")
        with self.app.app_context():
            self.assertEqual((db.session.get(Veicolo, vid).fase, Ddt.query.count()), ("preparazione", 0))

    def test_senza_profilo_si_chiede_una_volta(self):
        vid = self.abbina()
        self.entra("autista")
        self.assertIn("/profilo-guida", self.c.get(f"/veicolo/{vid}/sposta").headers["Location"])
        self.profilo()
        pagina = self.c.get(f"/veicolo/{vid}/sposta").get_data(as_text=True)
        self.assertIn("Dove la porti?", pagina)
        self.assertIn("Carrozzeria B", pagina)

    def test_partenza_con_un_tocco_e_lavorazione_prima_della_chiusura(self):
        vid = self.abbina()
        self.profilo()
        r = self.c.post(f"/veicolo/{vid}/sposta", data={"a_sede_id": self._id(Sede, nome="Carrozzeria B")})
        self.assertIn("/ddt/", r.headers["Location"])
        d = self.ddt()
        with self.app.app_context():
            m = self.mov(d)
            self.assertEqual((m.stato, m.causale, m.conducente, m.vettore.nome, m.mezzo),
                             ("aperto", "Conto lavorazione", "Autista", "Vettore X", "Bisarca"))
            self.assertEqual((m.firma_conducente, m.firma_salvata, m.lavorazione), (FIRMA, True, ""))
            self.assertEqual((m.partenza_lat, m.partenza_lon), (45.66, 12.24))  # posizione del tocco
        self.assertTrue(self.c.get(f"/ddt/{d}.pdf").data.startswith(b"%PDF"))
        # senza lavorazione il DDT non si chiude, nemmeno sul posto
        self.arrivo(d)
        with self.app.app_context():
            self.assertEqual(self.mov(d).stato, "aperto")
        self.c.post(f"/ddt/{d}/lavorazione", data={"lavorazione": "Carrozzeria", "lavorazione_note": ""})
        with self.app.app_context():
            self.assertEqual(self.mov(d).lavorazione, "")  # servono i dettagli
        self.c.post(f"/ddt/{d}/lavorazione", data={"lavorazione": "Carrozzeria", "lavorazione_note": "paraurti"})
        self.arrivo(d)
        with self.app.app_context():
            m = self.mov(d)
            self.assertEqual((m.stato, m.lavorazione, m.lavorazione_note), ("chiuso", "Carrozzeria", "paraurti"))
            self.assertEqual(m.ddt.revisioni[0].modifiche[0]["campo"], "Lavorazione")

    def test_tra_sedi_proprie_nessuna_lavorazione(self):
        vid = self.abbina()
        with self.app.app_context():
            db.session.add(Sede(nome="Sede E", tipo="propria", lat=45.5, lon=12.5))
            db.session.commit()
        self.profilo()
        self.c.post(f"/veicolo/{vid}/sposta", data={"a_sede_id": self._id(Sede, nome="Sede E")})
        d = self.ddt()
        with self.app.app_context():
            self.assertEqual((self.mov(d).causale, self.mov(d).lavorazione),
                             ("Trasferimento tra sedi", Movimento.NESSUNA))
        self.arrivo(d)
        with self.app.app_context():
            self.assertEqual(self.mov(d).stato, "chiuso")

    def test_destinazione_scritta_a_mano_fissata_al_primo_arrivo(self):
        vid = self.abbina()
        self.profilo()
        self.c.post(f"/veicolo/{vid}/sposta", data={"nuova_destinazione": "  Garage   Verdi, Via Po 3 ", "tipo": "officina"})
        d = self.ddt()
        with self.app.app_context():
            m = self.mov(d)
            self.assertEqual((m.a_sede.nome, m.a_sede.tipo, m.a_sede.lat, m.causale),
                             ("Garage Verdi, Via Po 3", "officina", None, "Conto lavorazione"))
        self.c.post(f"/ddt/{d}/lavorazione", data={"lavorazione": "Revisione", "lavorazione_note": "annuale"})
        self.c.post(f"/ddt/{d}/arrivo")  # senza posizione no
        self.c.post(f"/ddt/{d}/arrivo", data={"lat": "45.9", "lon": "12.9"})  # il primo arrivo fissa il punto
        with self.app.app_context():
            m = self.mov(d)
            self.assertEqual((m.stato, m.a_sede.lat, m.a_sede.lon, m.arrivo_per_conto), ("chiuso", 45.9, 12.9, False))
        # stesso nome scritto di nuovo: si riusa il luogo, niente doppioni
        with self.app.app_context():
            self.assertEqual(servizi.destinazione_scritta("garage verdi, via po 3").id, self.mov(d).a_sede_id)
        self.c.post(f"/veicolo/{vid}/sposta", data={"nuova_destinazione": "ab"})  # troppo corto
        with self.app.app_context():
            self.assertEqual(Ddt.query.count(), 1)

    def test_tocco_su_auto_in_preparazione_un_tocco_un_passo(self):
        vid = self.abbina()
        self.c.post(f"/veicolo/{vid}/preparazione")
        self.entra("autista")
        fasi = lambda: [f.nome for f in Preparazione.query.one().fasi_fatte]
        # senza tocco non si aggiorna nulla: la schermata chiede di toccare l'adesivo
        self.c.post(f"/veicolo/{vid}/tocco/passo")
        with self.app.app_context():
            self.assertEqual(fasi(), [])
        self.assertIn("Tocca l'adesivo NFC per il prossimo passo", self.c.get(f"/veicolo/{vid}/tocco").get_data(as_text=True))
        self.assertIn(f"/veicolo/{vid}/tocco", self.c.post("/t/ABCD2345", data=POS).headers["Location"])
        pagina = self.c.get(f"/veicolo/{vid}/tocco").get_data(as_text=True)
        self.assertIn("Controllo rientro fatto", pagina)
        self.assertIn("data-anello", pagina)
        r = self.c.post(f"/veicolo/{vid}/tocco/passo")  # subito dopo il tocco: il passo si chiude
        self.assertIn("fatto=0&da=0", r.headers["Location"])
        self.assertIn("confermato col tocco", self.c.get(r.headers["Location"]).get_data(as_text=True))
        self.c.post(f"/veicolo/{vid}/tocco/passo")  # stesso tocco: il passo dopo non si chiude
        with self.app.app_context():
            self.assertEqual(fasi(), ["Controllo rientro"])
        self.c.post("/t/ABCD2345", data=POS)  # nuovo tocco, nuovo passo
        self.c.post(f"/veicolo/{vid}/tocco/passo")
        with self.app.app_context():
            self.assertEqual(fasi(), ["Controllo rientro", "Preparazione"])

    def test_rientro_lo_emette_chiunque_dopo_il_completamento(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        self.arrivo(self.ddt())
        self.entra("autista")
        self.parti(vid, "Sede A", causale="Rientro da lavorazione", lavorazione="Nessuna (solo trasferimento)")
        with self.app.app_context():
            self.assertEqual(Ddt.query.count(), 1)  # lavorazione non ancora confermata: no
        self.entra("carr")
        self.c.post(f"/ddt/{self.ddt()}/lavorazione-completata", data={"note": "fatto"})  # senza predisporre il rientro
        self.profilo()
        pagina = self.c.get(f"/veicolo/{vid}/sposta").get_data(as_text=True)
        self.assertIn("Partenza · non modificabile", pagina)
        self.assertIn("Carrozzeria B", pagina)
        self.c.post(f"/veicolo/{vid}/sposta", data={"a_sede_id": self._id(Sede, nome="Sede A")})
        with self.app.app_context():
            m = self.mov(self.ddt())
            self.assertEqual((m.da_sede.nome, m.causale, m.stato), ("Carrozzeria B", "Rientro da lavorazione", "aperto"))
            self.assertEqual(servizi.mittente(m)[0], "Carrozzeria B")  # il mittente resta il fornitore

    def test_anteprima_e_tocchi_del_viaggio(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        d = self.ddt()
        pagina = self.c.get(f"/ddt/{d}/anteprima").get_data(as_text=True)
        self.assertIn("Scarica PDF", pagina)
        self.assertIn("data-in-viaggio", pagina)  # in viaggio: contatore del tempo
        self.c.post("/t/ABCD2345", data=POS)  # tocco durante il viaggio
        self.arrivo(d)
        with self.app.app_context():
            nomi = [t["nome"] for t in servizi.tocchi_viaggio(self.mov(d))]
        self.assertEqual(nomi, ["Partenza", "Tocco dell'adesivo", "Arrivo"])
        pagina = self.c.get(f"/ddt/{d}/anteprima").get_data(as_text=True)
        self.assertIn("Viaggio durato", pagina)
        self.assertNotIn("data-in-viaggio", pagina)

    def test_ddt_si_scarica(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        r = self.c.get(f"/ddt/{self.ddt()}.pdf?scarica=1")
        self.assertTrue(r.headers["Content-Disposition"].startswith("attachment"))
        self.assertIn("data-condividi-pdf", self.c.get(f"/arrivo/{self.ddt()}").get_data(as_text=True))


class TestPredisposto(Base):
    def test_ufficio_predispone_autista_prende_in_carico(self):
        vid = self.abbina()
        self.entra("autista")
        self.parti(vid, "Carrozzeria B", azione="predisponi", conducente="")
        with self.app.app_context():
            self.assertEqual(Ddt.query.count(), 0)  # l'autista non può predisporre

        self.entra("ufficio")
        self.parti(vid, "Carrozzeria B", azione="predisponi", conducente="", firma="", ore_ritiro="8")
        d = self.ddt()
        with self.app.app_context():
            m = self.mov(d)
            self.assertEqual((m.stato, m.partenza_at), ("predisposto", None))
            self.assertEqual(m.veicolo.stato, "in_sede")

        self.entra("autista")
        self.c.post(f"/ddt/{d}/presa-in-carico", data={"conducente": "Luca", "firma": ""})
        with self.app.app_context():
            self.assertEqual(self.mov(d).stato, "predisposto")  # senza firma non parte
        self.c.post(f"/ddt/{d}/presa-in-carico", data={"conducente": "Luca", "firma": FIRMA})
        with self.app.app_context():
            m = self.mov(d)
            self.assertEqual((m.stato, m.conducente, m.veicolo.stato), ("aperto", "Luca", "in_viaggio"))
            self.assertEqual(m.partenza_utente.email, "autista@t.it")
            self.assertEqual(db.session.get(Ddt, d).creato_da.email, "ufficio@t.it")

    def test_ritiro_scaduto_e_rinnovo(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B", azione="predisponi")
        d = self.ddt()
        with self.app.app_context():
            self.mov(d).ritiro_entro_at -= timedelta(days=2)
            db.session.commit()
        self.entra("autista")
        self.c.post(f"/ddt/{d}/presa-in-carico", data={"conducente": "Luca", "firma": FIRMA})
        with self.app.app_context():
            self.assertEqual(self.mov(d).stato, "predisposto")
        self.entra("ufficio")
        self.c.post(f"/ddt/{d}/rinnova", data={"ore": "24"})
        self.entra("autista")
        self.c.post(f"/ddt/{d}/presa-in-carico", data={"conducente": "Luca", "firma": FIRMA})
        with self.app.app_context():
            self.assertEqual(self.mov(d).stato, "aperto")


class TestLavorazioniEPreparazione(Base):
    def arriva_in_carrozzeria(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        andata = self.ddt()
        self.entra("carr")
        self.arrivo(andata)
        return vid, andata

    def test_ciclo_fornitore_rientro_preparazione_consegna(self):
        vid, andata = self.arriva_in_carrozzeria()
        with self.app.app_context():
            self.assertEqual(self.mov(andata).giorni_lavorazione, 0)
            # compare solo il fornitore che ha auto: l'officina vuota no
            self.assertEqual([s.nome for s, _ in servizi.presso_fornitori()], ["Carrozzeria B"])
            v = db.session.get(Veicolo, vid)
            self.assertEqual(v.fase, "lavorazione")
            # lo stesso stato visto dal fornitore è "in sede da voi"
            self.assertEqual(v.stato_per(Utente.query.filter_by(email="carr@t.it").one())[0], "in_sede")

        # la concessionaria non può emettere la bolla di rientro: il mittente è la carrozzeria
        self.entra("autista")
        self.parti(vid, "Sede A", causale="Rientro da lavorazione", lavorazione="Nessuna (solo trasferimento)")
        with self.app.app_context():
            self.assertEqual(Ddt.query.count(), 1)

        # la carrozzeria completa e prepara il DDT di rientro (mittente = carrozzeria)
        self.entra("carr")
        self.c.post(f"/ddt/{andata}/lavorazione-completata", data={"note": "fatto", "rientro": "1"})
        with self.app.app_context():
            v = db.session.get(Veicolo, vid)
            self.assertEqual(v.fase, "da_ritirare")
            rientro = v.movimento_predisposto
            self.assertEqual((rientro.a_sede.nome, rientro.ddt.creato_da.email), ("Sede A", "carr@t.it"))
            self.assertEqual(servizi.mittente(rientro)[0], "Carrozzeria B")
            rientro_id = rientro.ddt.id

        # l'autista ritira: tocca, firma, parte; all'arrivo il DDT si chiude e l'auto va in preparazione
        self.entra("autista")
        self.c.post(f"/ddt/{rientro_id}/presa-in-carico", data={"conducente": "Mario", "firma": FIRMA})  # manca la modalità
        with self.app.app_context():
            self.assertEqual(self.mov(rientro_id).stato, "predisposto")
        self.c.post(f"/ddt/{rientro_id}/presa-in-carico", data={"conducente": "Mario", "firma": FIRMA,
                                                                 "mezzo": "Carro attrezzi"})
        self.assertIn(f"/arrivo/{rientro_id}", self.c.post("/t/ABCD2345", data=POS).headers["Location"])
        r = self.arrivo(rientro_id)
        self.assertIn("/preparazione", r.headers["Location"])
        with self.app.app_context():
            self.assertEqual(servizi.presso_fornitori(), [])
            self.assertEqual(db.session.get(Veicolo, vid).fase, "preparazione")
            prep_id = Preparazione.query.one().id
        for _ in Preparazione.FASI:
            self.avanza(prep_id)
        with self.app.app_context():
            prep = db.session.get(Preparazione, prep_id)
            self.assertEqual((prep.avanzamento, prep.veicolo.fase), (100, "pronta"))
            tappe = [t["nome"] for t in prep.tappe]
            self.assertEqual(tappe, ["Rientro"] + Preparazione.FASI)
            self.assertEqual(prep.tappe[0]["giorni_fa"], 0)
            r = prep.riepilogo
            self.assertEqual((r["esterni"], r["fornitore"], r["interni"]), (0, "Carrozzeria B", 0))
            self.assertIsNotNone(r["completata_at"])
            nomi = [t[0] for t in prep.tempi]
            self.assertEqual(nomi[:3], ["Lavorazione · Carrozzeria B", "Attesa ritiro", "Viaggio di rientro"])
            self.assertTrue(all(t[3] is not None for t in prep.tempi))
        pagina = self.c.get(f"/veicolo/{vid}").get_data(as_text=True)
        self.assertIn("Lavorazione esterna", pagina)
        self.assertIn("Completata il", pagina)

        self.c.post(f"/veicolo/{vid}/consegna")  # senza sede di consegna non si consegna
        with self.app.app_context():
            self.assertEqual(db.session.get(Veicolo, vid).fase, "pronta")
        self.c.post(f"/veicolo/{vid}/consegna/sede", data={"sede_id": self._id(Sede, nome="Sede A")})
        self.c.post(f"/veicolo/{vid}/dati", data={"targa": "GH123AB"})  # senza targa non si consegna
        self.c.post(f"/veicolo/{vid}/consegna")
        with self.app.app_context():
            v = db.session.get(Veicolo, vid)
            self.assertEqual((v.fase, v.tag), ("consegnato", None))
            self.assertIsNotNone(v.consegnato_at)
        self.assertIn("VW Golf", self.c.get("/preparazione?vista=consegnate").get_data(as_text=True))
        self.assertNotIn("VW Golf", self.c.get("/preparazione?vista=pronte").get_data(as_text=True))

    def test_solo_il_fornitore_completa_la_lavorazione(self):
        _, andata = self.arriva_in_carrozzeria()
        self.entra("autista")
        self.c.post(f"/ddt/{andata}/lavorazione-completata")
        with self.app.app_context():
            self.assertIsNone(self.mov(andata).lavorazione_completata_at)

    def test_filtro_tipo_lavorazione(self):
        self.arriva_in_carrozzeria()
        self.entra("ufficio")
        self.assertIn("VW Golf", self.c.get("/lavorazioni?lavorazione=Carrozzeria").get_data(as_text=True))
        self.assertNotIn("VW Golf", self.c.get("/lavorazioni?lavorazione=Revisione").get_data(as_text=True))

    def test_tap_in_viaggio_mostra_solo_arrivo(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        d = self.ddt()
        self.entra("autista")
        r = self.c.post("/t/ABCD2345", data=POS)
        self.assertIn(f"/arrivo/{d}", r.headers["Location"])
        pagina = self.c.get(f"/arrivo/{d}").get_data(as_text=True)
        self.assertNotIn("Inizia spostamento", pagina)
        self.assertIn("Arrivato a destinazione", pagina)  # lo chiude chi è sul posto (controllo GPS)
        self.entra("carr")
        self.assertIn("Arrivato a destinazione", self.c.get(f"/arrivo/{d}").get_data(as_text=True))


class TestCercaSimili(Base):
    def test_filtri_marca_modello(self):
        vid = self.abbina()
        with self.app.app_context():
            db.session.add(Veicolo(vin="WVWZZZ1KZAW000999", marca="VW", modello="Golf", colore="Nero",
                                   sede_id=1))
            db.session.add(Veicolo(vin="ZFA31200000123456", marca="Fiat", modello="500", sede_id=1))
            db.session.commit()
        pagina = self.c.get("/cerca", query_string={"marca": "VW", "modello": "Golf"}).get_data(as_text=True)
        self.assertEqual(pagina.count('class="voce fase'), 2)
        self.assertIn("1 simile<", self.c.get(f"/veicolo/{vid}").get_data(as_text=True))


class TestFiltriAdattivi(Base):
    def test_opzioni_solo_se_presenti(self):
        vid = self.abbina()
        pagina = self.c.get("/stock").get_data(as_text=True)
        self.assertIn("VW Golf", pagina)
        # il menu c'è anche con un solo valore, ma elenca solo i valori presenti
        self.assertIn('name="tipologia"', pagina)
        self.assertNotIn("Veicolo commerciale", pagina)
        self.assertIn('name="fase"', pagina)
        with self.app.app_context():
            db.session.add(Veicolo(vin="ZFA25000002K98765", marca="Fiat", modello="Ducato",
                                   tipologia="commerciale", sede_id=1))
            db.session.commit()
        pagina = self.c.get("/stock").get_data(as_text=True)
        self.assertIn("Veicolo commerciale", pagina)
        solo_comm = self.c.get("/stock?tipologia=commerciale").get_data(as_text=True)
        self.assertIn("Ducato", solo_comm)
        self.assertNotIn("VW Golf", solo_comm)
        # archivio DDT: nessun DDT cancellato -> niente pillola "Cancellati"
        self.parti(vid, "Carrozzeria B")
        self.assertNotIn("Cancellati", self.c.get("/ddt").get_data(as_text=True))


class _Blocco:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _ClienteFinto:
    """Sostituisce l'SDK Anthropic: restituisce le risposte preparate in ordine."""
    def __init__(self, risposte):
        self.risposte, self.richieste = list(risposte), []
        self.beta = self
        self.messages = self

    def create(self, **kw):
        self.richieste.append(kw)
        return self.risposte.pop(0)


def _risposta(*blocchi, stop="end_turn"):
    return _Blocco(content=list(blocchi), stop_reason=stop)


class TestAssistenteIA(Base):
    def setUp(self):
        super().setUp()
        from getvingo import ia
        self.ia = ia
        self.originale = ia._cliente

    def tearDown(self):
        self.ia._cliente = self.originale

    def finto(self, *risposte):
        cliente = _ClienteFinto(risposte)
        self.ia._cliente = lambda: cliente
        return cliente

    def test_chat_visibile_in_panoramica_anche_senza_chiave(self):
        import os
        os.environ.pop("ANTHROPIC_API_KEY", None)
        pagina = self.c.get("/").get_data(as_text=True)
        self.assertIn('id="chat-msg"', pagina)
        self.assertIn("non è ancora attivo", pagina)

    def test_senza_chiave_messaggio_chiaro(self):
        import os
        vid = self.abbina()
        os.environ.pop("ANTHROPIC_API_KEY", None)
        r = self.c.post("/api/assistente/ddt", json={"veicolo_id": vid, "testo": "porto l'auto"})
        self.assertEqual(r.status_code, 503)
        self.assertIn("chiave", r.json["errore"])

    def test_compila_ddt_da_frase(self):
        import json
        vid = self.abbina()
        carr = self._id(Sede, nome="Carrozzeria B")
        campi = {"conducente": "Luca", "a_sede_id": carr, "lavorazione": "Carrozzeria",
                 "lavorazione_note": "cerchi verniciati neri", "causale": "Conto riparazione",
                 "vettore_id": 0, "riepilogo": "Luca porta l'auto in carrozzeria per i cerchi neri", "dubbi": ""}
        cliente = self.finto(_risposta(_Blocco(type="text", text=json.dumps(campi))))
        r = self.c.post("/api/assistente/ddt", json={"veicolo_id": vid,
                        "testo": "sono Luca e sto portando questa auto a fare i cerchi neri dalla carrozzeria B"})
        self.assertEqual(r.status_code, 200, r.json)
        self.assertEqual((r.json["campi"]["a_sede_id"], r.json["campi"]["conducente"]), (carr, "Luca"))
        self.assertTrue(r.json["campi"]["vettore_id"])  # vettore 0 -> si propone quello di default
        richiesta = cliente.richieste[0]
        self.assertEqual(richiesta["model"], "claude-opus-5-5")
        schema = richiesta["output_config"]["format"]["schema"]
        # la sede in cui l'auto si trova non è una destinazione possibile
        self.assertNotIn(self._id(Sede, nome="Sede A"), schema["properties"]["a_sede_id"]["enum"])

    def test_compila_ddt_solo_chi_puo_emettere(self):
        vid = self.abbina()
        self.entra("carr")
        r = self.c.post("/api/assistente/ddt", json={"veicolo_id": vid, "testo": "x"})
        self.assertEqual(r.status_code, 403)

    def test_chat_usa_gli_strumenti(self):
        vid = self.abbina()
        self.finto(
            _risposta(_Blocco(type="tool_use", id="t1", name="cerca_veicoli",
                              input={"testo": "golf", "tipologia": "", "stato": "", "includi_consegnati": False}),
                      stop="tool_use"),
            _risposta(_Blocco(type="text", text="La Golf 000123 è in Sede A.")))
        r = self.c.post("/api/assistente/chat", json={"messaggi": [{"ruolo": "utente", "testo": "dov'è la golf?"}]})
        self.assertEqual(r.status_code, 200, r.json)
        self.assertIn("Sede A", r.json["risposta"])
        self.assertEqual(r.json["veicoli"][0]["url"], f"/veicolo/{vid}")

    def test_chat_rifiuto(self):
        self.finto(_risposta(stop="refusal"))
        r = self.c.post("/api/assistente/chat", json={"messaggi": [{"ruolo": "utente", "testo": "ciao"}]})
        self.assertEqual(r.status_code, 503)

    def test_chat_vietata_ai_fornitori(self):
        self.entra("carr")
        self.assertEqual(self.c.post("/api/assistente/chat", json={"messaggi": []}).status_code, 403)


JPEG = b"\xff\xd8\xff\xe0" + b"0" * 200


class TestDanni(Base):
    def segnala(self, vid, **extra):
        dati = {"zona": "Lato sinistro", "descrizione": "graffio portiera",
                "foto": [(io.BytesIO(JPEG), "a.jpg")], **extra}
        return self.c.post(f"/veicolo/{vid}/danno", data=dati, content_type="multipart/form-data")

    def test_danno_in_piazzale_con_foto_e_allerta(self):
        vid = self.abbina()
        self.entra("autista")
        # senza foto non si segnala
        self.c.post(f"/veicolo/{vid}/danno", data={"zona": "Tetto", "descrizione": "x"},
                    content_type="multipart/form-data")
        # un file che non è un'immagine viene rifiutato
        self.c.post(f"/veicolo/{vid}/danno", data={"zona": "Tetto", "descrizione": "x",
                    "foto": [(io.BytesIO(b"%PDF-1.4"), "a.pdf")]}, content_type="multipart/form-data")
        with self.app.app_context():
            self.assertEqual(Danno.query.count(), 0)
        self.segnala(vid)
        with self.app.app_context():
            d = Danno.query.one()
            self.assertEqual((d.contesto, len(d.foto), d.aperto), ("piazzale", 1, True))
            nome = d.foto[0].file
        self.assertIn("danno da verificare", self.c.get("/").get_data(as_text=True))
        self.assertEqual(self.c.get(f"/foto/{nome}").data, JPEG)
        self.entra("carr")
        self.assertEqual(self.c.get(f"/foto/{nome}").status_code, 403)  # auto non sua
        self.entra("ufficio")
        # negli elenchi solo i dati: le foto si vedono aprendo il danno
        self.assertNotIn(f"/foto/{nome}", self.c.get("/danni").get_data(as_text=True))
        self.assertIn(f"/foto/{nome}", self.c.get("/danno/1").get_data(as_text=True))
        # per chiuderlo serve la foto della riparazione
        self.c.post("/danno/1/risolvi", data={"nota": "era già presente"})
        self.assertIn("danno da verificare", self.c.get("/").get_data(as_text=True))
        self.c.post("/danno/1/risolvi", data={"nota": "era già presente", "foto": [(io.BytesIO(JPEG), "ok.jpg")]},
                    content_type="multipart/form-data")
        with self.app.app_context():
            self.assertEqual(len(db.session.get(Danno, 1).foto_risoluzione), 1)
        self.assertNotIn("danno da verificare", self.c.get("/").get_data(as_text=True))
        self.assertIn("era già presente", self.c.get("/danni?vista=risolti").get_data(as_text=True))

    def test_danno_all_arrivo(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        d = self.ddt()
        self.entra("carr")
        r = self.arrivo(d, danno="1")
        self.assertIn(f"/veicolo/{vid}/danno?movimento=", r.headers["Location"])
        with self.app.app_context():
            mid = self.mov(d).id
        self.segnala(vid, movimento=str(mid))
        with self.app.app_context():
            danno = Danno.query.one()
            self.assertEqual((danno.contesto, danno.movimento_id), ("arrivo", mid))


class TestRipristino(Base):
    def test_admin_annulla_arrivo_di_un_altro(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        d = self.ddt()
        self.entra("carr")
        self.arrivo(d)
        with self.app.app_context():
            ultima = Azione.query.order_by(Azione.id.desc()).first()
            self.assertEqual((ultima.tipo, ultima.utente.email), ("arrivo", "carr@t.it"))
            aid = ultima.id
        self.entra("ufficio")
        self.assertNotIn("Annulla arrivo", self.c.get(f"/ddt/{d}").get_data(as_text=True))
        self.c.post(f"/azione/{aid}/ripristina")  # solo l'admin
        with self.app.app_context():
            self.assertEqual(self.mov(d).stato, "chiuso")
        self.entra("admin")
        # la correzione sta nella cronologia del DDT, accanto all'arrivo
        self.assertIn(f"/azione/{aid}/ripristina", self.c.get(f"/ddt/{d}").get_data(as_text=True))
        self.c.post(f"/azione/{aid}/ripristina")
        with self.app.app_context():
            m = self.mov(d)
            self.assertEqual((m.stato, m.arrivo_at, m.veicolo.stato), ("aperto", None, "in_viaggio"))

    def test_solo_l_ultima_azione_e_ripristino_completamento(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        d = self.ddt()
        self.entra("carr")
        self.arrivo(d)
        self.c.post(f"/ddt/{d}/lavorazione-completata", data={"rientro": "1"})
        self.entra("admin")
        with self.app.app_context():
            arrivo = Azione.query.filter_by(tipo="arrivo").one().id
            completata = Azione.query.filter_by(tipo="lavorazione_completata").one().id
        lav = self.c.get("/lavorazioni").get_data(as_text=True)
        self.assertIn(f"/azione/{completata}/ripristina", lav)  # in Lavorazioni, sull'auto
        self.assertNotIn(f"/azione/{arrivo}/ripristina", self.c.get(f"/ddt/{d}").get_data(as_text=True))
        with self.app.app_context():
            self.assertFalse(Azione.query.filter_by(tipo="predisposizione").count())
        self.c.post(f"/azione/{arrivo}/ripristina")  # non è l'ultima: rifiutata
        with self.app.app_context():
            self.assertEqual(self.mov(d).stato, "chiuso")
        self.c.post(f"/azione/{completata}/ripristina")
        with self.app.app_context():
            v = db.session.get(Veicolo, vid)
            self.assertEqual((v.fase, v.movimento_predisposto), ("lavorazione", None))
        self.c.post(f"/azione/{arrivo}/ripristina")  # ora sì
        with self.app.app_context():
            self.assertEqual(self.mov(d).stato, "aperto")
        self.assertEqual(self.c.get("/registro").status_code, 404)  # niente pagina separata

    def test_ripristino_cancellazione_ddt(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        d = self.ddt()
        self.c.post(f"/ddt/{d}/annulla", data={"motivo": "sbaglio"})
        with self.app.app_context():
            aid = Azione.query.filter_by(tipo="annullamento").one().id
        self.c.post(f"/azione/{aid}/ripristina")
        with self.app.app_context():
            documento = db.session.get(Ddt, d)
            self.assertIsNone(documento.annullato_at)
            self.assertEqual(documento.movimento.veicolo.stato, "in_viaggio")


class TestPiazzaleMappa(Base):
    def test_un_punto_per_sede_esatti_se_filtro(self):
        self.abbina()
        with self.app.app_context():
            db.session.add(Veicolo(vin="WVWZZZ1KZAW000999", marca="VW", modello="Golf", sede_id=1,
                                   lat=45.0001, lon=12.0001))
            db.session.commit()
        pagina = self.c.get("/stock").get_data(as_text=True)
        self.assertIn('"n": 2', pagina)              # un punto per piazzale con 2 auto
        self.assertEqual(pagina.count('"fase":'), 2)  # le singole auto per la vista ravvicinata
        self.assertNotIn("45.660000", pagina)        # niente coordinate scritte nell'elenco
        self.assertIn('data-sede="1"', self.c.get("/stock?sede=1").get_data(as_text=True))


class TestStockEDati(Base):
    def test_nuova_auto_solo_con_adesivo(self):
        dati = {"vin": VIN, "marca": "VW", "modello": "Golf", "targa": "gh 123 ab", "condizione": "usato",
                "tipologia": "commerciale", "sede_id": self._id(Sede, nome="Sede A")}
        self.c.post("/piazzale/nuova", data=dati)  # senza adesivo non entra in stock
        with self.app.app_context():
            self.assertEqual(Veicolo.query.count(), 0)
        self.c.post("/piazzale/nuova", data=dict(dati, tag="ABCD2345"))
        with self.app.app_context():
            v = Veicolo.query.one()
            self.assertEqual((v.targa, v.tipologia, v.sede.nome, v.tag.codice), ("GH123AB", "commerciale", "Sede A", "ABCD2345"))
        self.assertIn("VW Golf", self.c.get("/stock?q=gh123").get_data(as_text=True))

    def test_modifica_dati_e_preparazione_diretta(self):
        vid = self.abbina()
        self.c.post(f"/veicolo/{vid}/dati", data={"marca": "VW", "modello": "Golf GTI", "colore": "Rosso",
                                                  "targa": "AB123CD", "tipologia": "vettura"})
        self.c.post(f"/veicolo/{vid}/dati", data={"targa": "ZZ999ZZ"})  # la targa inserita non si cambia
        self.c.post(f"/veicolo/{vid}/preparazione", data={"pronta_entro": "2026-11-20T16:00"})
        with self.app.app_context():
            v = db.session.get(Veicolo, vid)
            # i dati restano quelli dell'ingresso: si aggiunge solo la targa
            self.assertEqual((v.modello, v.targa, v.fase), ("Golf", "AB123CD", "preparazione"))
            self.assertEqual(v.preparazione_attiva.pronta_entro_at.hour, 16)
        pagina = self.c.get(f"/veicolo/{vid}").get_data(as_text=True)
        self.assertIn("Posizione aggiornata", pagina)
        self.assertIn("abbinamento dell&#39;adesivo", pagina)
        self.assertNotIn("45.660000", pagina)  # niente coordinate in gradi

    def test_preparazione_diretta_non_dai_fornitori(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        self.entra("carr")
        self.arrivo(self.ddt())
        self.entra("admin")
        self.c.post(f"/veicolo/{vid}/preparazione")
        with self.app.app_context():
            self.assertEqual(db.session.get(Veicolo, vid).fase, "lavorazione")

    def test_posizione_aggiornata_con_ddt(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        self.entra("carr")
        self.arrivo(self.ddt())
        self.entra("admin")
        self.assertIn("arrivo con DDT 1/", self.c.get(f"/veicolo/{vid}").get_data(as_text=True))


class TestGruppiAziende(Base):
    def test_utenti_del_fornitore_condividono_dati_e_firma(self):
        carr = self._id(Sede, nome="Carrozzeria B")
        self.c.post(f"/admin/aziende/fornitore/{carr}", data={"azione": "utente", "nome": "Sara", "email": "sara@t.it",
                                                               "password": "segreta1"})
        with self.app.app_context():
            sara = Utente.query.filter_by(email="sara@t.it").one()
            self.assertEqual((sara.ruolo, sara.sede_id), ("fornitore", carr))
            sara_id = sara.id
        # un'unica firma per tutto il gruppo: la usa anche il nuovo utente
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        self.c.post("/logout")
        self.c.post("/login", data={"email": "sara@t.it", "password": "segreta1"})
        self.arrivo(self.ddt())
        with self.app.app_context():
            self.assertEqual(self.mov(self.ddt()).firma_destinatario, FIRMA)
        self.entra("admin")
        self.c.post(f"/admin/utenti/{sara_id}", data={"nome": "Sara Bianchi", "email": "sara@t.it", "sede_id": carr})
        with self.app.app_context():
            u = db.session.get(Utente, sara_id)
            self.assertEqual((u.nome, u.attivo), ("Sara Bianchi", False))  # spunta "attivo" tolta

    def test_modifica_vettore_non_cambia_i_ddt_emessi(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        self.c.post("/admin/vettori/1", data={"nome": "Nuovo Nome Trasporti", "attivo": "1"})
        pagina = self.c.get(f"/ddt/{self.ddt()}").get_data(as_text=True)
        with self.app.app_context():
            from getvingo.servizi import dati_ddt
            self.assertEqual(dati_ddt(db.session.get(Ddt, self.ddt()))[2][0], "Vettore X")


class TestCsrfAttivo(unittest.TestCase):
    """Con la protezione attiva i bottoni devono funzionare davvero (bug del token vuoto)."""
    def test_i_moduli_hanno_il_token(self):
        import re
        app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://", "SECRET_KEY": "t",
                          "ARCHIVIO_DIR": tempfile.mkdtemp()})
        usa_org_1(app)
        with app.app_context():
            db.create_all()
            db.session.add(Organizzazione(id=1, nome="T"))
            u = Utente(nome="A", email="a@t.it", ruolo="admin")
            u.imposta_password("pw")
            db.session.add_all([u, Sede(nome="S", tipo="propria")])
            db.session.flush()
            v = Veicolo(vin=VIN, sede_id=1)
            db.session.add(v)
            db.session.flush()
            servizi.avvia_preparazione(v)
            db.session.commit()
        c = app.test_client()
        token = lambda html: re.search(r'name="csrf" value="([0-9a-f]*)"', html).group(1)
        c.post("/login", data={"email": "a@t.it", "password": "pw",
                               "csrf": token(c.get("/login").get_data(as_text=True))})
        html = c.get("/lavorazioni?vista=preparazione").get_data(as_text=True)
        self.assertTrue(token(html))
        c.post("/preparazione/1/scadenza", data={"csrf": token(html), "pronta_entro": "2030-01-01T10:00"})
        with app.app_context():
            self.assertIsNotNone(db.session.get(Preparazione, 1).pronta_entro_at)
        # token sbagliato: niente errore 400, si torna indietro con un avviso
        r = c.post("/preparazione/1/scadenza", data={"csrf": "sbagliato"})
        self.assertEqual(r.status_code, 302)


class TestDdtGestione(Base):
    def test_modifica_con_revisioni_e_permessi(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        d = self.ddt()
        self.c.post(f"/ddt/{d}/modifica", data={"conducente": ""})
        dati = self.dati("Sede A", causale="Trasferimento tra sedi", lavorazione="Lavaggio / detailing",
                         conducente="Luca Bianchi")
        self.c.post(f"/ddt/{d}/modifica", data=dati)
        self.c.post(f"/ddt/{d}/modifica", data=dati)  # nessun cambiamento: nessuna revisione
        with self.app.app_context():
            documento = db.session.get(Ddt, d)
            self.assertEqual(documento.revisione, 1)
            campi = {c["campo"]: (c["prima"], c["dopo"]) for c in documento.revisioni[0].modifiche}
            self.assertEqual(campi["Conducente"], ("Mario Rossi", "Luca Bianchi"))
        self.arrivo(d)
        self.entra("autista")
        self.assertEqual(self.c.post(f"/ddt/{d}/modifica", data=dati).status_code, 403)
        self.entra("ufficio")
        self.assertEqual(self.c.get(f"/ddt/{d}/modifica").status_code, 200)

    def test_annullamento_solo_admin(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        d = self.ddt()
        self.entra("ufficio")
        self.c.post(f"/ddt/{d}/annulla", data={"motivo": "errore"})
        with self.app.app_context():
            self.assertIsNone(db.session.get(Ddt, d).annullato_at)
        self.entra("admin")
        self.c.post(f"/ddt/{d}/annulla", data={"motivo": ""})
        with self.app.app_context():
            self.assertIsNone(db.session.get(Ddt, d).annullato_at)
        self.c.post(f"/ddt/{d}/annulla", data={"motivo": "auto sbagliata"})
        with self.app.app_context():
            documento = db.session.get(Ddt, d)
            self.assertIsNotNone(documento.annullato_at)
            self.assertEqual(documento.movimento.veicolo.stato, "in_sede")  # torna all'origine
        self.assertEqual(self.c.get("/ddt").get_data(as_text=True).count('class="voce"'), 0)
        self.assertIn("auto sbagliata", self.c.get("/ddt?stato=annullati").get_data(as_text=True))
        # il numero non viene riutilizzato
        self.parti(vid, "Carrozzeria B")
        with self.app.app_context():
            self.assertEqual(sorted(x.numero for x in Ddt.query.all()), [1, 2])

    def test_filtri_archivio(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B", conducente="Giulia Verdi")
        def trovati(**q):
            return self.c.get("/ddt", query_string=q).get_data(as_text=True).count('class="voce"')
        self.assertEqual(trovati(conducente="giulia"), 1)
        self.assertEqual(trovati(conducente="mario"), 0)
        self.assertEqual(trovati(vin="000123"), 1)
        self.assertEqual(trovati(mezzo="golf"), 1)
        self.assertEqual(trovati(lavorazione="Carrozzeria"), 1)
        self.assertEqual(trovati(destinazione=str(self._id(Sede, nome="Sede A"))), 0)
        self.assertEqual(trovati(dal="2000-01-01", al="2000-01-02"), 0)
        self.assertEqual(trovati(stato="chiuso"), 0)


class TestFornitoreVedeSoloIlSuo(Base):
    def test_permessi_fornitore(self):
        vid = self.abbina()
        self.parti(vid, "Officina D", lavorazione="Meccanica / tagliando")
        d = self.ddt()
        self.entra("carr")
        self.assertEqual(self.c.get(f"/ddt/{d}").status_code, 403)
        self.assertEqual(self.c.get("/cerca").status_code, 403)
        self.assertEqual(self.c.get("/admin/").status_code, 403)
        self.assertEqual(self.c.get("/ddt").get_data(as_text=True).count('class="voce"'), 0)
        self.assertEqual(self.c.get("/").status_code, 200)

    def test_pagine_si_aprono_per_ogni_ruolo(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        d = self.ddt()
        interne = ["/", "/cerca", "/cerca?q=000123", f"/veicolo/{vid}", "/ddt", "/ddt?stato=tutti&q=golf",
                   f"/ddt/{d}", f"/arrivo/{d}", "/lavorazioni", "/preparazione", "/preparazione?vista=pronte",
                   "/preparazione?vista=consegnate", "/stock", "/stock?tipologia=vettura",
                   "/cerca?marca=VW&fase=in_viaggio"]
        for url in interne + [f"/ddt/{d}/modifica", "/admin/", "/admin/sedi", "/admin/sedi/2",
                              "/admin/vettori", "/admin/vettori/1", "/admin/aziende", "/admin/aziende/concessionaria",
                              "/admin/aziende/fornitore/2", "/admin/utenti/1", "/admin/tag",
                              "/admin/import", "/ddt?stato=annullati", "/piazzale/nuova"]:
            self.assertEqual(self.c.get(url).status_code, 200, url)
        self.entra("autista")
        for url in interne:
            self.assertEqual(self.c.get(url).status_code, 200, url)
        self.entra("carr")
        for url in ["/", "/ddt", f"/ddt/{d}", f"/ddt/{d}.pdf", f"/arrivo/{d}", "/mia-firma"]:
            self.assertEqual(self.c.get(url).status_code, 200, url)
        self.assertIn(f"/arrivo/{d}", self.c.post("/t/ABCD2345", data=POS).headers["Location"])
        self.entra("ufficio")
        self.assertEqual(self.c.get("/admin/").status_code, 403)  # la gestione è solo dell'admin


if __name__ == "__main__":
    unittest.main()


class TestEliminaAuto(Base):
    def test_senza_ddt_l_admin_elimina_e_libera_l_adesivo(self):
        vid = self.abbina()
        self.c.post(f"/veicolo/{vid}/danno", data={"zona": "Tetto", "descrizione": "graffio",
                    "foto": (io.BytesIO(JPEG), "f.jpg")}, content_type="multipart/form-data")
        with self.app.app_context():
            self.assertEqual(Danno.query.count(), 1)
        self.entra("autista")
        self.c.post(f"/veicolo/{vid}/elimina")
        with self.app.app_context():
            self.assertIsNotNone(db.session.get(Veicolo, vid))  # solo l'admin
        self.entra("admin")
        self.assertIn("Elimina questa auto", self.c.get(f"/veicolo/{vid}").get_data(as_text=True))
        self.assertEqual(self.c.post(f"/veicolo/{vid}/elimina").status_code, 302)
        with self.app.app_context():
            self.assertIsNone(db.session.get(Veicolo, vid))
            self.assertEqual(Danno.query.count(), 0)
            self.assertIsNone(Tag.query.filter_by(codice="ABCD2345").first().veicolo_id)

    def test_con_ddt_solo_in_demo(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        self.assertNotIn("Elimina questa auto", self.c.get(f"/veicolo/{vid}").get_data(as_text=True))
        self.c.post(f"/veicolo/{vid}/elimina")
        with self.app.app_context():
            self.assertIsNotNone(db.session.get(Veicolo, vid))
        self.app.config["DEMO"] = True
        self.assertIn("vengono eliminati anche 1 DDT", self.c.get(f"/veicolo/{vid}").get_data(as_text=True))
        self.c.post(f"/veicolo/{vid}/elimina")
        with self.app.app_context():
            self.assertIsNone(db.session.get(Veicolo, vid))
            self.assertEqual(Ddt.query.count() + Movimento.query.count() + Azione.query.count(), 0)


class TestPiuClienti(Base):
    """Due concessionarie sullo stesso portale: nessuna vede i dati dell'altra."""
    def setUp(self):
        super().setUp()
        with self.app.app_context():
            org2 = servizi.crea_organizzazione("Altra Auto S.p.A.", "admin@altra.it", "password2")
            db.session.add_all([Sede(nome="Sede Altra", tipo="propria"), Sede(nome="Carrozzeria Altra", tipo="carrozzeria"),
                                Vettore(nome="Vettore Altra"), Tag(codice="ZZZZ9999")])
            db.session.commit()
            self.org2 = org2.id

    def _id2(self, modello, **filtro):
        with self.app.app_context():
            g.org_id = self.org2
            return modello.query.filter_by(**filtro).first().id

    def test_dati_separati_e_numerazione_ddt_propria(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        self.c.post("/logout")
        self.c.post("/login", data={"email": "admin@altra.it", "password": "password2"})
        # l'altra concessionaria non vede auto, DDT, sedi né adesivi della prima
        self.assertEqual(self.c.get(f"/veicolo/{vid}").status_code, 404)
        self.assertEqual(self.c.get(f"/ddt/{self.ddt()}").status_code, 404)
        self.assertEqual(self.c.post("/t/ABCD2345", data=POS).status_code, 404)
        html = self.c.get("/stock").get_data(as_text=True)
        self.assertNotIn("Golf", html)
        self.assertNotIn("<b>Sede A</b>", self.c.get("/admin/sedi").get_data(as_text=True))
        # stesso VIN ammesso in un'altra concessionaria, e il suo primo DDT è il n. 1
        r = self.c.post("/abbina/ZZZZ9999", data={"vin": VIN, "marca": "VW", "modello": "Polo", "colore": "Blu", "condizione": "usato",
                                                 "sede_id": self._id2(Sede, nome="Sede Altra")})
        self.assertEqual(r.status_code, 302)
        with self.app.app_context():
            g.org_id = self.org2
            vid2 = Veicolo.query.filter_by(vin=VIN).one().id
        d = {"a_sede_id": self._id2(Sede, nome="Carrozzeria Altra"), "causale": "Conto riparazione",
             "vettore_id": self._id2(Vettore, nome="Vettore Altra"), "conducente": "Luca", "lavorazione": "Carrozzeria",
             "lavorazione_note": "fiancata", "mezzo": "Bisarca", "firma": FIRMA}
        self.assertEqual(self.c.post(f"/veicolo/{vid2}/partenza", data=d).status_code, 302)
        with self.app.app_context():
            numeri = sorted((x.org_id, x.numero) for x in
                            Ddt.query.execution_options(tutte_le_organizzazioni=True).all())
            self.assertEqual(numeri, [(1, 1), (self.org2, 1)])

    def test_email_unica_su_tutto_il_portale(self):
        with self.app.app_context():
            with self.assertRaises(servizi.Vietato):
                servizi.crea_organizzazione("Terza", "admin@t.it", "password3")


class TestSicurezza(Base):
    def test_blocco_dopo_troppi_tentativi(self):
        self.c.post("/logout")
        for _ in range(auth.MAX_TENTATIVI):
            self.c.post("/login", data={"email": "admin@t.it", "password": "sbagliata"})
        r = self.c.post("/login", data={"email": "admin@t.it", "password": "pw"})
        self.assertEqual(r.status_code, 429)  # anche con la password giusta, finché dura il blocco

    def test_cambio_password(self):
        r = self.c.post("/password", data={"attuale": "pw", "nuova": "corta", "conferma": "corta"})
        self.assertIn("almeno 8", r.get_data(as_text=True))
        self.c.post("/password", data={"attuale": "pw", "nuova": "nuovapass1", "conferma": "nuovapass1"})
        self.c.post("/logout")
        self.c.post("/login", data={"email": "admin@t.it", "password": "nuovapass1"})
        self.assertEqual(self.c.get("/").status_code, 200)

    def test_privacy_pubblica_e_intestazioni(self):
        self.c.post("/logout")
        r = self.c.get("/privacy")
        self.assertEqual(r.status_code, 200)
        self.assertIn("GDPR", r.get_data(as_text=True))
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")


class TestArchivioDdt(Base):
    def versioni(self, d):
        with self.app.app_context():
            return [a.chiave for a in DdtArchivio.query.filter_by(ddt_id=d).order_by(DdtArchivio.id)]

    def test_una_copia_per_ogni_versione(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        d = self.ddt()
        self.assertEqual(len(self.versioni(d)), 1)  # emissione
        self.c.get(f"/ddt/{d}")  # consultare non crea copie
        self.assertEqual(len(self.versioni(d)), 1)
        self.c.post(f"/ddt/{d}/modifica", data=self.dati("Carrozzeria B", conducente="Luca Bianchi"))
        self.arrivo(d)
        self.c.post(f"/ddt/{d}/annulla", data={"motivo": "prova"})
        chiavi = self.versioni(d)
        self.assertEqual(len(chiavi), 4)
        self.assertIn("-rev1", chiavi[1])
        self.assertTrue(chiavi[-1].startswith("org-1/") and "-annullato" in chiavi[-1])
        cartella = self.app.config["ARCHIVIO_DIR"]
        for k in chiavi:
            with open(f"{cartella}/{k}", "rb") as f:
                self.assertTrue(f.read().startswith(b"%PDF"))

    def test_recupero_dopo_invio_fallito(self):
        class Guasto:
            def salva(self, chiave, dati):
                raise OSError("rete assente")
        originale = archivio.destinazione
        archivio.destinazione = lambda: Guasto()
        try:
            vid = self.abbina()
            r = self.parti(vid, "Carrozzeria B")
        finally:
            archivio.destinazione = originale
        self.assertEqual(r.status_code, 302)  # il DDT viene emesso comunque
        d = self.ddt()
        self.assertEqual(self.versioni(d), [])
        with self.app.app_context():
            self.assertEqual(archivio.archivia_tutti()[0], 1)
            self.assertEqual(archivio.archivia_tutti()[0], 0)  # già in archivio: niente doppioni

    def test_invio_s3(self):
        inviati = []
        class ClienteFinto:
            def put_object(self, **kw):
                inviati.append(kw)
        import boto3
        originale = boto3.client
        boto3.client = lambda *a, **kw: ClienteFinto()
        self.app.config["ARCHIVIO_S3_BUCKET"] = "ddt-test"
        try:
            vid = self.abbina()
            self.parti(vid, "Carrozzeria B")
        finally:
            boto3.client = originale
        self.assertEqual(len(inviati), 1)
        self.assertEqual((inviati[0]["Bucket"], inviati[0]["ContentType"]), ("ddt-test", "application/pdf"))
        self.assertTrue(inviati[0]["Body"].startswith(b"%PDF"))


class BaseMultiplo(Base):
    """Tre auto in Sede A pronte per un DDT multiplo."""
    VINS = ["WVWZZZ1KZAW000201", "WVWZZZ1KZAW000202", "WVWZZZ1KZAW000203"]

    def setUp(self):
        super().setUp()
        with self.app.app_context():
            sede = Sede.query.filter_by(nome="Sede A").first()
            for n, vin in enumerate(self.VINS):
                db.session.add(Veicolo(vin=vin, marca="VW", modello=f"Golf {n}", sede=sede, stato="in_sede"))
            db.session.commit()
            self.ids = [Veicolo.query.filter_by(vin=v).first().id for v in self.VINS]

    def emetti(self, ids=None, sede="Carrozzeria B", **kw):
        dati = self.dati(sede, **kw)
        dati["veicolo"] = self.ids if ids is None else ids
        dati.setdefault("da_sede_id", self._id(Sede, nome="Sede A"))
        return self.c.post("/ddt/multiplo", data=dati)


class TestDdtMultiplo(BaseMultiplo):
    def test_tasto_solo_nella_pagina_ddt_e_creazione(self):
        self.assertIn("/ddt/multiplo", self.c.get("/ddt").get_data(as_text=True))
        vid = self.ids[0]
        self.assertNotIn("/ddt/multiplo", self.c.get(f"/veicolo/{vid}").get_data(as_text=True))
        self.assertEqual(self.c.get("/ddt/multiplo").status_code, 200)
        r = self.emetti()
        self.assertEqual(r.status_code, 302)
        with self.app.app_context():
            self.assertEqual(Ddt.query.count(), 1)  # un solo numero per tutte le auto
            d = Ddt.query.first()
            self.assertTrue(d.multiplo)
            self.assertEqual(sorted(m.veicolo_id for m in d.movimenti), sorted(self.ids))
            self.assertTrue(all(db.session.get(Veicolo, i).stato == "in_viaggio" for i in self.ids))
        pagina = self.c.get(f"/ddt/{d.id}").get_data(as_text=True)
        self.assertIn("3 auto", pagina)
        pdf = self.c.get(f"/ddt/{d.id}.pdf")
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.data.startswith(b"%PDF"))

    def test_regole(self):
        self.emetti(ids=self.ids[:1])  # una sola auto: non è un multiplo
        self.emetti(sede="Sede A")      # destinazione = luogo di partenza
        self.emetti(firma="")           # serve la firma
        with self.app.app_context():
            self.assertEqual(Ddt.query.count(), 0)
            # auto in un altro luogo: tutte devono partire dallo stesso
            db.session.get(Veicolo, self.ids[2]).sede = Sede.query.filter_by(nome="Officina D").first()
            db.session.commit()
        self.emetti()
        with self.app.app_context():
            self.assertEqual(Ddt.query.count(), 0)
        self.entra("carr")  # il fornitore non emette bolle per auto che non sono da lui
        self.emetti(ids=self.ids[:2], da_sede_id=self._id(Sede, nome="Carrozzeria B"))
        with self.app.app_context():
            self.assertEqual(Ddt.query.count(), 0)

    def test_arrivo_insieme_lavorazione_auto_per_auto(self):
        self.emetti()
        d = self.ddt()
        self.entra("carr")
        self.arrivo(d)
        with self.app.app_context():
            documento = db.session.get(Ddt, d)
            self.assertTrue(all(m.stato == "chiuso" for m in documento.movimenti))
            self.assertTrue(all(m.veicolo.sede.nome == "Carrozzeria B" for m in documento.movimenti))
            secondo = documento.movimenti[1]
            mid, vid = secondo.id, secondo.veicolo_id
        self.c.post(f"/ddt/{d}/lavorazione-completata", data={"movimento_id": mid, "rientro": "1"})
        with self.app.app_context():
            documento = db.session.get(Ddt, d)
            fatte = [m.veicolo_id for m in documento.movimenti if m.lavorazione_completata_at]
            self.assertEqual(fatte, [vid])  # solo l'auto scelta
            self.assertIsNotNone(db.session.get(Veicolo, vid).movimento_predisposto)  # rientro pronto

    def test_annullamento_e_ripristino_per_tutte(self):
        self.emetti()
        d = self.ddt()
        self.c.post(f"/ddt/{d}/annulla", data={"motivo": "bisarca sbagliata"})
        with self.app.app_context():
            self.assertTrue(all(db.session.get(Veicolo, i).stato == "in_sede" for i in self.ids))
            azione = Azione.query.filter_by(tipo="annullamento", veicolo_id=self.ids[0]).first()
        self.c.post(f"/azione/{azione.id}/ripristina", data={"torna": f"/ddt/{d}"})
        with self.app.app_context():
            self.assertIsNone(db.session.get(Ddt, d).annullato_at)
            self.assertTrue(all(db.session.get(Veicolo, i).stato == "in_viaggio" for i in self.ids))

    def test_annulla_arrivo_solo_se_ultimo_per_tutte(self):
        self.emetti()
        d = self.ddt()
        self.arrivo(d)
        with self.app.app_context():
            arrivo = Azione.query.filter_by(tipo="arrivo", veicolo_id=self.ids[0]).first()
            admin = Utente.query.filter_by(email="admin@t.it").first()
            self.assertTrue(servizi.puo_ripristinare(arrivo, admin))
            # una lavorazione chiusa su un'altra auto blocca l'annullo dell'arrivo collettivo
            m = db.session.get(Ddt, d).movimenti[2]
            g.org_id = 1
            carr = Utente.query.filter_by(email="carr@t.it").first()  # la fine lavorazione la segna il fornitore
            servizi.completa_lavorazione(m, carr, prepara_rientro=False)
            db.session.commit()
            self.assertFalse(servizi.puo_ripristinare(arrivo, admin))

    def test_pdf_su_piu_pagine(self):
        from getvingo import ddt as ddt_pdf
        with self.app.app_context():
            sede = Sede.query.filter_by(nome="Sede A").first()
            for n in range(14):
                db.session.add(Veicolo(vin=f"WVWZZZ1KZAW0003{n:02d}", marca="Fiat", modello="Ducato",
                                       sede=sede, stato="in_sede"))
            db.session.commit()
            tutti = [v.id for v in Veicolo.query.filter_by(stato="in_sede")]
        self.emetti(ids=tutti)
        with self.app.app_context():
            documento = Ddt.query.first()
            self.assertEqual(len(documento.movimenti), 17)
            dati = ddt_pdf.pdf(documento)
            self.assertGreaterEqual(dati.count(b"/Type /Page\n") + dati.count(b"/Type /Page "), 2)


class TestDdtMultiploAdesivi(BaseMultiplo):
    """Aggiunta delle auto al DDT multiplo toccando gli adesivi NFC."""
    def setUp(self):
        super().setUp()
        with self.app.app_context():
            for n, vid in enumerate(self.ids):
                db.session.add(Tag(codice=f"MULT{n}", veicolo_id=vid))
            db.session.commit()

    def test_partenza_scelta_e_sedi_vuote_visibili(self):
        pagina = self.c.get("/ddt/multiplo").get_data(as_text=True)
        self.assertIn("Partenza da", pagina)
        self.assertIn("Sede A · 3 auto disponibili", pagina)
        self.assertNotIn("Carrozzeria B ·", pagina)  # la concessionaria non emette da un fornitore

    def test_tocco_senza_raccolta_apre_la_scheda(self):
        r = self.c.post("/t/MULT0", data=POS)
        self.assertIn(f"/veicolo/{self.ids[0]}", r.headers["Location"])

    def test_tocchi_aggiungono_e_si_emette(self):
        self.c.post("/ddt/multiplo/raccolta", data={"azione": "avvia"})
        for n in range(3):
            r = self.c.post(f"/t/MULT{n}", data=POS)
            self.assertTrue(r.headers["Location"].endswith("/ddt/multiplo"))
        self.c.post("/t/MULT0", data=POS)  # secondo tocco: nessun doppione
        pagina = self.c.get("/ddt/multiplo").get_data(as_text=True)
        self.assertEqual(pagina.count('name="veicolo" value="') , 3)
        self.assertEqual(pagina.count("checked>"), 3)
        dati = self.dati("Carrozzeria B")
        dati.update(da_sede_id=self._id(Sede, nome="Sede A"), veicolo=self.ids)
        self.c.post("/ddt/multiplo", data=dati)
        with self.app.app_context():
            self.assertEqual(len(Ddt.query.first().movimenti), 3)
        # emesso il DDT la raccolta si chiude: il tocco torna a fare il solito
        self.assertNotIn("/ddt/multiplo", self.c.post("/t/MULT0", data=POS).headers["Location"])

    def test_tocco_di_auto_in_altra_sede_rifiutato(self):
        with self.app.app_context():
            sede = Sede(nome="Sede E", tipo="propria")
            db.session.add(sede)
            db.session.flush()
            db.session.get(Veicolo, self.ids[2]).sede = sede
            db.session.commit()
        self.c.post("/ddt/multiplo/raccolta", data={"azione": "avvia"})
        self.c.post("/t/MULT0", data=POS)
        self.c.post("/t/MULT2", data=POS)
        with self.c.session_transaction() as sess:
            self.assertEqual(sess["multiplo"]["auto"], [self.ids[0]])

    def test_lettura_dalla_pagina_android(self):
        self.c.post("/ddt/multiplo/raccolta", data={"azione": "avvia"})
        self.assertFalse(self.c.post("/api/ddt-multiplo", json={"codice": "mult1"}).json["ok"])  # serve il GPS
        r = self.c.post("/api/ddt-multiplo", json={"codice": "mult1", "lat": 45.7, "lon": 12.2})
        self.assertTrue(r.json["ok"])
        self.assertFalse(self.c.post("/api/ddt-multiplo", json={"codice": "NESSUNO"}).json["ok"])
        with self.c.session_transaction() as sess:
            self.assertEqual(sess["multiplo"]["auto"], [self.ids[1]])


class TestRegoleNuove(Base):
    """Tocco con posizione obbligatoria, danni che bloccano lavaggio/pronta, stock e panoramica."""
    def test_tocco_chiede_posizione_e_la_salva(self):
        vid = self.abbina()
        self.assertIn("Rilevo la posizione", self.c.get("/t/ABCD2345").get_data(as_text=True))
        self.assertEqual(self.c.post("/t/ABCD2345").status_code, 200)  # senza GPS non si prosegue
        self.c.post("/t/ABCD2345", data={"lat": "45.7001", "lon": "12.2502"})
        with self.app.app_context():
            v = db.session.get(Veicolo, vid)
            self.assertEqual((v.lat, v.lon, v.ultima_posizione.tipo), (45.7001, 12.2502, "tocco"))

    def test_preparazione_si_aggiorna_solo_col_tocco(self):
        vid = self.abbina()
        self.c.post(f"/veicolo/{vid}/preparazione")
        with self.app.app_context():
            pid = Preparazione.query.one().id
        fasi = lambda: len(db.session.get(Preparazione, pid).fasi)
        # dal portale solo lo stato di avanzamento: niente tasto "fatto" né "prossimo passo"
        for url in (f"/veicolo/{vid}", "/preparazione"):
            pagina = self.c.get(url).get_data(as_text=True)
            self.assertNotIn("btn-fatto", pagina)
            self.assertNotIn("Prossimo passo", pagina)
            self.assertIn("tocco-passi", pagina)
        self.assertEqual(self.c.post(f"/preparazione/{pid}/avanza").status_code, 404)  # non esiste più
        self.c.post("/t/ABCD2345", data=POS)  # il tocco da solo non chiude il passo
        with self.app.app_context():
            self.assertEqual(fasi(), 0)
        r = self.avanza(pid)
        self.assertIn("/tocco?fatto=0&da=0", r.headers["Location"])
        with self.app.app_context():
            self.assertEqual(fasi(), 1)

    def test_danno_blocca_solo_pronta_e_consegna(self):
        vid = self.abbina()
        self.c.post(f"/veicolo/{vid}/preparazione")
        with self.app.app_context():
            prep = Preparazione.query.one()
            pid = prep.id
            db.session.add(Danno(veicolo_id=vid, zona="Tetto", descrizione="grandine", segnalato_da_id=1))
            db.session.commit()
        self.avanza(pid)  # Controllo rientro: consentito
        self.avanza(pid)  # Preparazione: consentito
        self.avanza(pid)  # Lavaggio: consentito anche con il danno aperto
        self.avanza(pid)  # Pronta: bloccata dal danno
        with self.app.app_context():
            self.assertEqual(db.session.get(Preparazione, pid).prossima_fase, "Pronta")
        self.assertIn("Pronta bloccato", self.c.get("/preparazione").get_data(as_text=True))

    def test_stock_fino_a_pronta_e_conteggi(self):
        vid = self.abbina()
        self.assertIn("VW Golf", self.c.get("/stock").get_data(as_text=True))
        self.c.post(f"/veicolo/{vid}/preparazione")
        self.c.get("/")  # consuma il messaggio di conferma
        stock = self.c.get("/stock").get_data(as_text=True)
        self.assertIn("Zona preparazione", stock)  # in preparazione resta in stock, nella sua zona
        self.assertIn("Passo 1 di 4 · Controllo rientro", stock)
        self.assertIn("VW Golf", stock)
        self.assertEqual(self.c.get("/piazzale").status_code, 302)  # vecchio indirizzo
        pagina = self.c.get("/").get_data(as_text=True)
        self.assertIn("Da ritirare", pagina)
        self.assertIn("Passo 1 di 4: Controllo rientro", pagina)
        self.assertIn("Da completare", pagina)
        with self.app.app_context():
            prep = Preparazione.query.one()
            prep.completata_at = prep.creata_at  # pronta per la consegna: resta in stock fino alla consegna
            db.session.commit()
        stock = self.c.get("/stock").get_data(as_text=True)
        self.assertIn("Pronte per la consegna", stock)
        self.assertIn("VW Golf", stock)

    def test_stock_tiene_lavorazioni_esterne_e_viaggi(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        stock = self.c.get("/stock").get_data(as_text=True)
        self.assertIn("In viaggio", stock)
        self.assertIn("VW Golf", stock)
        self.arrivo(self.ddt())
        stock = self.c.get("/stock").get_data(as_text=True)
        self.assertIn("Lavorazioni esterne", stock)
        self.assertIn("VW Golf", stock)
        pagina = self.c.get(f"/veicolo/{vid}").get_data(as_text=True)
        self.assertIn("Portami qui", pagina)  # solo dai fornitori esterni
        self.assertNotIn("Parcheggiata qui", pagina)


class TestLavorazioniStimaESolleciti(Base):
    """Avanzamento delle lavorazioni esterne sul tempo stimato; sollecito se il tempo finisce."""
    def in_carrozzeria(self, giorni_fa):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        d = self.ddt()
        self.arrivo(d)
        with self.app.app_context():
            m = self.mov(d)
            m.arrivo_at = adesso() - timedelta(days=giorni_fa)
            db.session.commit()
            return vid, m.id

    def test_avanzamento_sulla_stima(self):
        vid, mid = self.in_carrozzeria(2)
        with self.app.app_context():
            m = db.session.get(Movimento, mid)
            self.assertEqual((m.stima_giorni, m.avanzamento_lavorazione, m.giorni_ritardo), (5, 40, 0))
        self.c.post(f"/movimento/{mid}/stima", data={"giorni": "2"})
        with self.app.app_context():
            m = db.session.get(Movimento, mid)
            self.assertEqual((m.avanzamento_lavorazione, m.giorni_ritardo), (100, 0))
        stock = self.c.get("/stock").get_data(as_text=True)
        self.assertIn("barra-lavoro", stock)
        self.assertIn("anello-lavoro", self.c.get(f"/veicolo/{vid}/tocco").get_data(as_text=True))

    def test_sollecito_solo_in_ritardo_e_risposta_del_fornitore(self):
        vid, mid = self.in_carrozzeria(2)
        self.c.post(f"/movimento/{mid}/sollecito")  # ancora in tempo: niente sollecito
        with self.app.app_context():
            self.assertEqual(Sollecito.query.count(), 0)
        with self.app.app_context():
            db.session.get(Movimento, mid).arrivo_at = adesso() - timedelta(days=8)
            db.session.commit()
        pagina = self.c.get("/lavorazioni").get_data(as_text=True)
        self.assertIn("In ritardo di 3 giorni", pagina)
        self.assertIn("Invia sollecito", pagina)
        self.c.post(f"/movimento/{mid}/sollecito")
        self.c.post(f"/movimento/{mid}/sollecito")  # una sola richiesta aperta alla volta
        with self.app.app_context():
            self.assertEqual(Sollecito.query.count(), 1)
            sid = Sollecito.query.one().id
        self.entra("carr")
        self.assertIn("Richieste dalla concessionaria", self.c.get("/").get_data(as_text=True))
        self.c.post(f"/sollecito/{sid}/risposta", data={"risposta": "Manca il ricambio", "nuova_stima": "2"})
        with self.app.app_context():
            m = db.session.get(Movimento, mid)
            self.assertEqual((Sollecito.query.one().risposta, m.stima_giorni, m.giorni_ritardo), ("Manca il ricambio", 10, 0))
        self.entra("admin")
        self.assertIn("Manca il ricambio", self.c.get("/lavorazioni").get_data(as_text=True))

    def test_niente_sollecito_se_finita(self):
        vid, mid = self.in_carrozzeria(9)
        self.entra("carr")
        self.c.post(f"/ddt/{self.ddt()}/lavorazione-completata", data={"note": "ok"})
        self.entra("admin")
        self.c.post(f"/movimento/{mid}/sollecito")
        with self.app.app_context():
            self.assertEqual(Sollecito.query.count(), 0)
        self.assertIn("Finita, da ritirare", self.c.get("/lavorazioni").get_data(as_text=True))


class TestCalendarioPreparazione(Base):
    """Preparazione avviata: in calendario a 10 giorni, slot da 30 minuti lun-ven 8:30-12:30 e 14:30-18:30."""
    def test_slot_utili(self):
        from datetime import datetime as dt
        with self.app.app_context():
            slot = servizi.primo_slot_libero
            self.assertEqual(slot(dt(2026, 11, 16, 17, 0)), dt(2026, 11, 16, 17, 0))    # lunedì, slot giusto
            self.assertEqual(slot(dt(2026, 11, 16, 17, 10)), dt(2026, 11, 16, 17, 30))  # si va allo slot dopo
            self.assertEqual(slot(dt(2026, 11, 16, 12, 40)), dt(2026, 11, 16, 14, 30))  # pausa pranzo
            self.assertEqual(slot(dt(2026, 11, 16, 18, 45)), dt(2026, 11, 17, 8, 30))   # dopo l'orario
            self.assertEqual(slot(dt(2026, 11, 14, 10, 0)), dt(2026, 11, 16, 8, 30))    # sabato -> lunedì
            self.assertEqual(len(servizi._slot_del_giorno(dt(2026, 11, 16).date())), 16)  # 8 + 8 al giorno

    def test_avvio_mette_in_calendario_e_scala_se_occupato(self):
        vid = self.abbina()
        with self.app.app_context():
            v2 = Veicolo(vin="WVWZZZ1KZAW000999", marca="VW", modello="Polo", sede=Sede.query.filter_by(nome="Sede A").one(),
                         condizione="nuovo")
            db.session.add(v2)
            db.session.commit()
            vid2 = v2.id
        self.c.post(f"/veicolo/{vid}/preparazione")
        self.c.post(f"/veicolo/{vid2}/preparazione")
        with self.app.app_context():
            a, b = (Preparazione.query.filter_by(veicolo_id=x).one().pronta_entro_at for x in (vid, vid2))
            self.assertGreaterEqual(a, adesso() + timedelta(days=10) - timedelta(minutes=1))
            self.assertLess(a.weekday(), 5)
            self.assertIn((a.hour, a.minute), [(t.hour, t.minute) for t in servizi._slot_del_giorno(a.date())])
            self.assertGreater(b, a)  # stesso momento: la seconda scala allo slot libero successivo
        self.assertIn("In calendario", self.c.get("/preparazione").get_data(as_text=True))


class TestConsegna(Base):
    """Auto pronta: sede di consegna, ultimo DDT se è altrove, consegna e archivio con calendario."""
    def pronta(self):
        vid = self.abbina()
        self.c.post(f"/veicolo/{vid}/preparazione")
        with self.app.app_context():
            pid = Preparazione.query.one().id
        for _ in range(4):
            self.avanza(pid)
        with self.app.app_context():
            self.assertEqual(db.session.get(Veicolo, vid).fase, "pronta")
        return vid

    def test_in_preparazione_niente_ddt(self):
        vid = self.abbina()
        self.c.post(f"/veicolo/{vid}/preparazione")
        self.assertNotIn("Sposta l'auto", self.c.get(f"/veicolo/{vid}").get_data(as_text=True))
        self.parti(vid, "Carrozzeria B")
        with self.app.app_context():
            self.assertEqual(Ddt.query.count(), 0)

    def test_ultimo_ddt_verso_la_sede_di_consegna(self):
        vid = self.pronta()
        with self.app.app_context():
            altra = Sede(nome="Sede E", tipo="propria", lat=45.3, lon=12.3)
            db.session.add(altra)
            db.session.commit()
            eid = altra.id
        self.c.post(f"/veicolo/{vid}/consegna/sede", data={"sede_id": eid, "prevista": "2026-11-20T10:30"})
        pagina = self.c.get(f"/veicolo/{vid}").get_data(as_text=True)
        self.assertIn("Emetti l'ultimo DDT", pagina)
        self.parti(vid, "Carrozzeria B")  # un'auto pronta non va altrove
        with self.app.app_context():
            self.assertEqual(Ddt.query.count(), 0)
        self.c.post(f"/veicolo/{vid}/consegna")  # non è ancora nella sede di consegna
        self.c.post(f"/veicolo/{vid}/partenza", data=dict(self.dati("Sede E", causale="Trasferimento tra sedi",
                                                                    lavorazione="Nessuna (solo trasferimento)")))
        d = self.ddt()
        # in viaggio con l'ultimo DDT resta tra le pronte e in stock: esce solo alla consegna
        pronte = self.c.get("/preparazione?vista=pronte").get_data(as_text=True)
        self.assertIn("VW Golf", pronte)
        self.assertIn("con l'ultimo DDT", pronte)
        self.assertIn("ultimo DDT, poi consegna", self.c.get("/stock").get_data(as_text=True))
        self.assertIn("in viaggio con l'ultimo DDT", self.c.get(f"/veicolo/{vid}").get_data(as_text=True))
        self.arrivo(d)
        with self.app.app_context():
            v = db.session.get(Veicolo, vid)
            self.assertEqual((v.fase, v.sede.nome), ("pronta", "Sede E"))
        self.assertIn("Inserisci la targa", self.c.get(f"/veicolo/{vid}").get_data(as_text=True))
        self.c.post(f"/veicolo/{vid}/consegna")  # senza targa non si consegna
        with self.app.app_context():
            self.assertEqual(db.session.get(Veicolo, vid).stato, "in_sede")
        self.c.post(f"/veicolo/{vid}/dati", data={"targa": "GH123AB"})
        self.c.post(f"/veicolo/{vid}/consegna")
        with self.app.app_context():
            v = db.session.get(Veicolo, vid)
            self.assertEqual(v.stato, "consegnato")
            mese = f"{v.consegnato_at:%Y-%m}"
        cal = self.c.get(f"/preparazione?vista=consegnate&modo=calendario&mese={mese}").get_data(as_text=True)
        self.assertIn("cal-auto", cal)
        self.assertIn("VW Golf", self.c.get("/preparazione?vista=consegnate&q=000123").get_data(as_text=True))
        self.assertIn("Nessuna auto consegnata con questi filtri",
                      self.c.get("/preparazione?vista=consegnate&dal=2000-01-01&al=2000-01-02").get_data(as_text=True))


class TestRegole0311Sera(Base):
    """Orario della preparazione fisso, appuntamenti a slot, numeri che filtrano, filtri a pulsanti."""
    def test_orario_preparazione_non_si_sposta_e_ritardo(self):
        vid = self.abbina()
        self.c.post(f"/veicolo/{vid}/preparazione")
        with self.app.app_context():
            prep = Preparazione.query.one()
            fissato = prep.pronta_entro_at
            self.assertIsNotNone(fissato)
            pid = prep.id
        self.c.post(f"/preparazione/{pid}/scadenza", data={"pronta_entro": "2030-01-07T10:00"})
        with self.app.app_context():
            self.assertEqual(db.session.get(Preparazione, pid).pronta_entro_at, fissato)
            prep = db.session.get(Preparazione, pid)
            prep.pronta_entro_at = datetime(2020, 1, 6, 9, 0)  # orario passato: in ritardo
            db.session.commit()
        pagina = self.c.get("/preparazione").get_data(as_text=True)
        self.assertIn("in ritardo di", pagina)
        self.assertNotIn('name="pronta_entro" value="2020', pagina)  # niente campo per spostarlo
        self.assertIn("In preparazione da", pagina)
        self.assertIn("VW Golf", self.c.get("/preparazione?stato=ritardo").get_data(as_text=True))

    def test_appuntamento_consegna_uno_slot_per_sede(self):
        vid = TestConsegna.pronta(self)
        with self.app.app_context():
            sede = Sede.query.filter_by(nome="Sede A").one()
            altra = Veicolo(vin="WVWZZZ1KZAW999999", marca="Fiat", modello="Panda", sede_id=sede.id,
                            consegna_sede_id=sede.id, consegna_prevista_at=datetime(2030, 1, 7, 9, 0))
            db.session.add(altra)
            db.session.commit()
            sid = sede.id
        pagina = self.c.get(f"/veicolo/{vid}").get_data(as_text=True)
        self.assertIn("data-scegli-slot", pagina)
        self.c.post(f"/veicolo/{vid}/consegna/sede", data={"sede_id": sid, "prevista": "2030-01-07T09:00"})
        with self.app.app_context():
            self.assertIsNone(db.session.get(Veicolo, vid).consegna_prevista_at)  # slot già preso
        self.c.post(f"/veicolo/{vid}/consegna/sede", data={"sede_id": sid, "prevista": "2030-01-07T09:30"})
        with self.app.app_context():
            self.assertEqual(db.session.get(Veicolo, vid).consegna_prevista_at, datetime(2030, 1, 7, 9, 30))
        # il calendario della preparazione mostra solo quando le auto devono essere pronte, non le consegne
        cal = self.c.get("/preparazione?vista=corso&modo=calendario&mese=2030-01").get_data(as_text=True)
        self.assertNotIn("consegna_prevista", cal)
        self.assertNotIn("Consegna prevista", self.c.get("/").get_data(as_text=True))

    def test_numeri_lavorazioni_filtrano(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        self.arrivo(self.ddt())
        self.assertIn("VW Golf", self.c.get("/lavorazioni?stato=corso").get_data(as_text=True))
        vuota = self.c.get("/lavorazioni?stato=ritardo").get_data(as_text=True)
        self.assertIn("Nessuna auto in ritardo", vuota)
        self.assertIn('class="tile-numero ritardo attivo"', vuota)
        self.assertNotIn("dai fornitori</span>", vuota)  # il totale non c'è più: solo i 3 stati
        self.assertIn('class="interruttore filtro-gruppo in-riga"', vuota)  # tipi di lavorazione su una riga
        self.assertNotIn("<span>in preparazione</span>", self.c.get("/preparazione").get_data(as_text=True))

    def test_stock_sedi_a_pulsanti_filtri_a_menu(self):
        self.abbina()
        stock = self.c.get("/stock").get_data(as_text=True)
        self.assertIn('class="interruttore filtro-gruppo in-riga tutta-larghezza" id="scegli-piazzale"', stock)
        self.assertIn('<select name="marca"', stock)  # gli altri filtri restano menu


class TestCalendarioETarga(Base):
    def test_feste_e_giorni_chiusi_saltati(self):
        from getvingo.models import GiornoChiuso
        with self.app.app_context():
            # 24/12/2026 è giovedì: dopo l'ultimo slot, 25 e 26 sono feste, poi sabato e domenica
            self.assertEqual(servizi.primo_slot_libero(datetime(2026, 12, 24, 19, 0)), datetime(2026, 12, 28, 8, 30))
            self.assertEqual(servizi.festivita(datetime(2027, 3, 29).date()), "Lunedì dell'Angelo")
            db.session.add(GiornoChiuso(data=datetime(2026, 12, 28).date(), motivo="Ponte"))
            db.session.commit()
            self.assertEqual(servizi.primo_slot_libero(datetime(2026, 12, 24, 19, 0)), datetime(2026, 12, 29, 8, 30))
        r = self.c.post("/admin/chiusure", data={"dal": "2030-04-26", "al": "2030-04-27", "motivo": "Patrono"})
        self.assertEqual(r.status_code, 302)
        with self.app.app_context():
            self.assertEqual(GiornoChiuso.query.filter(GiornoChiuso.motivo == "Patrono").count(), 2)
        pagina = self.c.get("/admin/chiusure").get_data(as_text=True)
        self.assertIn("Patrono", pagina)
        self.assertIn("Natale", pagina)

    def test_targa_con_formato_insolito_va_confermata(self):
        vid = self.abbina()
        self.c.post(f"/veicolo/{vid}/dati", data={"targa": "TV123456"})
        with self.app.app_context():
            self.assertFalse(db.session.get(Veicolo, vid).targa)  # non salvata senza conferma
        self.c.post(f"/veicolo/{vid}/dati", data={"targa": "TV123456", "targa_ok": "1"})
        with self.app.app_context():
            self.assertEqual(db.session.get(Veicolo, vid).targa, "TV123456")


class TestRegoleLavorazioneECalendario(Base):
    def test_la_concessionaria_non_chiude_la_lavorazione(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        d = self.ddt()
        self.arrivo(d)
        self.c.post(f"/ddt/{d}/lavorazione-completata")  # admin della concessionaria: non può
        with self.app.app_context():
            self.assertIsNone(self.mov(d).lavorazione_completata_at)
        pagina = self.c.get(f"/veicolo/{vid}").get_data(as_text=True)
        self.assertIn("paraurti", pagina)  # si vede la lavorazione in corso
        self.assertNotIn("Lavorazione completata</button>", pagina)
        self.entra("carr")
        self.c.post(f"/ddt/{d}/lavorazione-completata")
        with self.app.app_context():
            self.assertIsNotNone(self.mov(d).lavorazione_completata_at)

    def test_calendario_preparazione_e_oggi_in_panoramica(self):
        from getvingo.models import adesso
        vid = self.abbina()
        with self.app.app_context():
            slot = servizi.primo_slot_libero(adesso())  # oggi se c'è ancora uno slot, altrimenti il primo giorno utile
        self.c.post(f"/veicolo/{vid}/preparazione", data={"pronta_entro": slot.strftime("%Y-%m-%dT%H:%M")})
        with self.app.app_context():
            self.assertEqual(Preparazione.query.one().pronta_entro_at, slot)
        cal = self.c.get(f"/preparazione?vista=corso&modo=calendario&mese={slot:%Y-%m}").get_data(as_text=True)
        self.assertIn("cal-auto ev pronta_entro", cal)
        if slot.date() == adesso().date():
            home = self.c.get("/").get_data(as_text=True)
            self.assertIn("oggi-voce ev pronta_entro", home)
            self.assertIn("Entro oggi", home)

    def test_preparazione_solo_negli_slot_di_30_minuti(self):
        vid = self.abbina()
        for fuori in ("2030-01-07T08:00", "2030-01-07T12:30", "2030-01-07T13:00", "2030-01-07T09:15",
                      "2030-01-07T18:30", "2030-01-05T09:00"):  # prima delle 8:30, pausa, non a mezz'ora, sabato
            self.c.post(f"/veicolo/{vid}/preparazione", data={"pronta_entro": fuori})
            with self.app.app_context():
                self.assertEqual(Preparazione.query.count(), 0, fuori)
        with self.app.app_context():
            giorno = datetime(2030, 1, 7).date()  # lunedì
            slot = servizi._slot_del_giorno(giorno)
            self.assertEqual(len(slot), 16)  # 8 la mattina e 8 il pomeriggio
            self.assertEqual((slot[0].time(), slot[7].time(), slot[8].time(), slot[15].time()),
                             (datetime(1, 1, 1, 8, 30).time(), datetime(1, 1, 1, 12, 0).time(),
                              datetime(1, 1, 1, 14, 30).time(), datetime(1, 1, 1, 18, 0).time()))
        self.c.post(f"/veicolo/{vid}/preparazione", data={"pronta_entro": "2030-01-07T18:00"})  # ultimo slot
        with self.app.app_context():
            self.assertEqual(Preparazione.query.one().pronta_entro_at, datetime(2030, 1, 7, 18, 0))


class TestLoghiMarche(Base):
    def test_logo_di_una_marca_nuova_si_scarica_da_solo(self):
        from getvingo import marchi
        self.app.instance_path = tempfile.mkdtemp()  # i loghi scaricati non toccano la cartella vera
        self.app.config["MARCHI_SCARICA"] = True
        chiesti = []
        originale = marchi._scarica
        marchi._scarica = lambda indirizzo: (chiesti.append(indirizzo), b"<svg xmlns='http://www.w3.org/2000/svg'/>")[1]
        try:
            self.c.post("/piazzale/nuova", data={"vin": VIN, "marca": "Lynk & Co", "modello": "01", "tag": "ABCD2345", "condizione": "nuovo",
                                                 "sede_id": self._id(Sede, nome="Sede A")})
        finally:
            marchi._scarica = originale
        self.assertTrue(chiesti and "simple-icons" in chiesti[0])
        self.assertIn("/marchi/lynk-co.svg", self.c.get("/stock").get_data(as_text=True))
        self.assertEqual(self.c.get("/marchi/lynk-co.svg").status_code, 200)


class TestLoghiUfficiali(Base):
    def test_carica_logo_ufficiale_e_precedenza(self):
        self.app.instance_path = tempfile.mkdtemp()
        self.abbina()  # VW
        pagina = self.c.get("/admin/marchi").get_data(as_text=True)
        self.assertIn("VW", pagina)
        svg = b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 10 10'><circle r='5'/></svg>"
        self.c.post("/admin/marchi", data={"marca": "VW", "file": (io.BytesIO(svg), "vw.svg")},
                    content_type="multipart/form-data")
        self.assertIn("Ufficiale · caricato in Gestione", self.c.get("/admin/marchi").get_data(as_text=True))
        self.assertIn("/marchi/ufficiali/volkswagen.svg", self.c.get("/stock").get_data(as_text=True))
        r = self.c.get("/marchi/ufficiali/volkswagen.svg")
        self.assertEqual(r.status_code, 200)
        self.assertIn("default-src 'none'", r.headers["Content-Security-Policy"])
        # un SVG con codice viene rifiutato
        cattivo = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
        self.c.post("/admin/marchi", data={"marca": "VW", "file": (io.BytesIO(cattivo), "x.svg")},
                    content_type="multipart/form-data")
        self.assertEqual(self.c.get("/marchi/ufficiali/volkswagen.svg").data, svg)
        self.entra("ufficio")
        self.assertNotEqual(self.c.get("/admin/marchi").status_code, 200)  # solo admin


class TestCampiDdtEFiltri(Base):
    def test_ddt_solo_con_tutti_i_campi(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B", mezzo="")                      # manca la modalità di trasporto
        self.parti(vid, "Carrozzeria B", lavorazione_note="")           # c'è una lavorazione ma niente dettagli
        with self.app.app_context():
            self.assertEqual(Ddt.query.count(), 0)
        self.parti(vid, "Carrozzeria B", mezzo="Bisarca")
        d = self.ddt()
        with self.app.app_context():
            self.assertEqual(self.mov(d).mezzo, "Bisarca")
        self.assertIn("Bisarca", self.c.get(f"/ddt/{d}").get_data(as_text=True))

    def test_arrivati_fuori_da_in_corso_e_lavorazioni_senza_trasferimenti(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B", causale="Trasferimento tra sedi", lavorazione="Nessuna (solo trasferimento)",
                   lavorazione_note="")
        d = self.ddt()
        self.arrivo(d)
        self.assertNotIn(f"/ddt/{d}\"", self.c.get("/ddt").get_data(as_text=True))          # "In corso"
        self.assertIn(f"/ddt/{d}\"", self.c.get("/ddt?stato=chiuso").get_data(as_text=True))
        self.assertNotIn("VW Golf", self.c.get("/lavorazioni").get_data(as_text=True))       # solo trasferimento

    def test_filtro_vetture_commerciali_in_preparazione(self):
        vid = self.abbina()
        self.c.post(f"/veicolo/{vid}/preparazione")
        self.assertIn("VW Golf", self.c.get("/preparazione?tipologia=vettura").get_data(as_text=True))
        self.assertNotIn("VW Golf", self.c.get("/preparazione?tipologia=commerciale").get_data(as_text=True))


class TestNuovoUsato(Base):
    def test_scelta_obbligatoria_e_mostrata_ovunque(self):
        dati = {"vin": VIN, "marca": "VW", "modello": "Golf", "tag": "ABCD2345", "sede_id": self._id(Sede, nome="Sede A")}
        self.c.post("/piazzale/nuova", data=dati)  # senza nuova/usata non entra
        with self.app.app_context():
            self.assertEqual(Veicolo.query.count(), 0)
        self.c.post("/piazzale/nuova", data=dict(dati, condizione="usato"))
        with self.app.app_context():
            vid = Veicolo.query.one().id
        self.assertIn("badge usato", self.c.get("/stock").get_data(as_text=True))          # al posto di "In sede"
        self.assertIn('badge usato', self.c.get(f"/veicolo/{vid}").get_data(as_text=True))  # nella carta dell'auto
        self.parti(vid, "Carrozzeria B")
        d = self.ddt()
        self.assertIn('class="condizione usato"', self.c.get(f"/ddt/{d}").get_data(as_text=True))
        self.assertTrue(self.c.get(f"/ddt/{d}.pdf").data.startswith(b"%PDF"))


class TestDemo(unittest.TestCase):
    """La demo usa i servizi veri: se una regola nuova la rompe, questo test lo dice subito."""
    def test_la_demo_si_carica_con_ogni_situazione(self):
        from getvingo import demo
        app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://", "SECRET_KEY": "t",
                          "FOTO_DIR": tempfile.mkdtemp(), "ARCHIVIO_DIR": tempfile.mkdtemp()})
        with app.app_context():
            demo.carica()
            g.org_id = 1
            fasi = {v.fase for v in Veicolo.query}
            self.assertEqual(fasi, {"in_sede", "in_viaggio", "da_ritirare", "lavorazione", "preparazione", "pronta",
                                    "consegnato"})
            self.assertTrue(any(d.multiplo for d in Ddt.query))
            self.assertEqual({v.condizione for v in Veicolo.query}, {"nuovo", "usato"})


class TestPagineDemoPulite(unittest.TestCase):
    """Ogni ruolo apre le pagine principali e le schede delle auto che vede: niente errori né "None" a video,
    e il pulsante Indietro del fornitore non porta allo Stock (che per lui è vietato)."""
    def test_pagine_per_ruolo(self):
        import re
        from getvingo import demo
        app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://", "SECRET_KEY": "t",
                          "CSRF_ATTIVO": False, "FOTO_DIR": tempfile.mkdtemp(), "ARCHIVIO_DIR": tempfile.mkdtemp()})
        with app.app_context():
            demo.carica()
        auth._falliti.clear()
        for ruolo in ("admin", "ufficio", "autista", "carrozzeria", "officina"):
            c = app.test_client()
            c.post("/login", data={"email": f"{ruolo}@demo.it", "password": "demo"})
            home = c.get("/").get_data(as_text=True)
            pagine = ["/", "/ddt", "/password"] + (["/stock", "/lavorazioni", "/preparazione", "/danni"]
                                                  if ruolo not in ("carrozzeria", "officina") else [])
            pagine += sorted(set(re.findall(r'href="(/veicolo/\d+)"', home)))[:8]
            pagine += sorted(set(re.findall(r'href="(/ddt/\d+)"', c.get("/ddt").get_data(as_text=True))))[:8]
            for p in pagine:
                r = c.get(p)
                self.assertEqual(r.status_code, 200, f"{ruolo} {p}")
                testo = re.sub(r"<[^>]+>", " ", r.get_data(as_text=True))
                self.assertNotRegex(testo, r"\bNone\b|\bundefined\b|\bnan\b", f"{ruolo} {p}")
                if ruolo in ("carrozzeria", "officina") and p.startswith("/veicolo/"):
                    self.assertNotIn('class="torna-indietro" href="/stock"', r.get_data(as_text=True))


class TestAccessi(Base):
    """Recupero password, inviti, sessioni dopo il cambio password, Pannello Getvingo e sospensione."""
    def setUp(self):
        super().setUp()
        self.app.config.update(POSTA_ATTIVA=True, GESTORI="admin@t.it", BASE_URL="https://getvingo.com")
        self.posta = self.app.extensions.setdefault("posta_inviata", [])
        self.posta.clear()

    def _link(self, email):
        import re
        ultima = [m for m in self.posta if m["a"] == email][-1]
        return re.search(r"https://getvingo\.com(/imposta-password/\S+)", ultima["testo"]).group(1)

    def test_recupero_password(self):
        self.c.post("/logout")
        r = self.c.post("/password-dimenticata", data={"email": "Autista@t.it"})
        self.assertIn("tra poco ti arriva", r.get_data(as_text=True))
        link = self._link("autista@t.it")
        # stessa risposta per un'email sconosciuta, e nessuna email
        n = len(self.posta)
        r = self.c.post("/password-dimenticata", data={"email": "nessuno@t.it"})
        self.assertIn("tra poco ti arriva", r.get_data(as_text=True))
        self.assertEqual(len(self.posta), n)
        self.assertEqual(self.c.get(link).status_code, 200)
        self.assertEqual(self.c.post(link, data={"nuova": "corta", "conferma": "corta"}).status_code, 200)
        r = self.c.post(link, data={"nuova": "nuovapass1", "conferma": "nuovapass1"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.c.get("/").status_code, 200)  # è dentro
        self.assertEqual(self.posta[-1]["oggetto"], "Getvingo: la tua password è stata cambiata")
        # il link si usa una volta sola, la vecchia password non vale più, la nuova sì
        r = self.c.get(link)
        self.assertEqual(r.status_code, 410)
        self.assertIn("già stato usato", r.get_data(as_text=True))
        self.c.post("/logout")
        self.c.post("/login", data={"email": "autista@t.it", "password": "pw"})
        self.assertEqual(self.c.get("/ddt").status_code, 302)
        self.c.post("/login", data={"email": "autista@t.it", "password": "nuovapass1"})
        self.assertEqual(self.c.get("/").status_code, 200)

    def test_link_scaduto_e_falso(self):
        from getvingo import accessi
        self.c.post("/password-dimenticata", data={"email": "autista@t.it"})
        link = self._link("autista@t.it")
        vecchia = accessi.DURATA["recupero"]
        accessi.DURATA["recupero"] = -1
        try:
            self.assertIn("scaduto", self.c.get(link).get_data(as_text=True))
        finally:
            accessi.DURATA["recupero"] = vecchia
        self.assertEqual(self.c.get(link[:-3] + "abc").status_code, 410)

    def test_link_sempre_con_indirizzo_pubblico(self):
        """Un Host falso nella richiesta non deve finire nel link dell'email."""
        self.c.post("/password-dimenticata", data={"email": "autista@t.it"}, headers={"Host": "sito-falso.com",
                                                                                    "X-Forwarded-Host": "sito-falso.com"})
        self.assertNotIn("sito-falso", self.posta[-1]["testo"])
        self.assertIn("https://getvingo.com/imposta-password/", self.posta[-1]["testo"])

    def test_senza_email_configurata(self):
        self.app.config["POSTA_ATTIVA"] = False
        r = self.c.get("/password-dimenticata")
        self.assertIn("Chiedi all'amministratore", r.get_data(as_text=True))
        self.c.post("/password-dimenticata", data={"email": "autista@t.it"})
        self.assertEqual(self.posta, [])
        # l'admin crea un utente: niente email, ma il link da copiare
        r = self.c.post("/admin/aziende/concessionaria", data={"azione": "utente", "nome": "Nuovo Autista",
                                                              "email": "nuovo@t.it", "ruolo": "autista"})
        testo = r.get_data(as_text=True)
        self.assertIn("non è ancora attivo", testo)
        self.assertIn("https://getvingo.com/imposta-password/", testo)

    def test_invito_utente_nuovo(self):
        r = self.c.post("/admin/aziende/concessionaria", data={"azione": "utente", "nome": "Luca Bianchi",
                                                              "email": "Luca@t.it", "ruolo": "ufficio"})
        self.assertIn("Email mandata", r.get_data(as_text=True))
        with self.app.app_context():
            u = Utente.query.filter_by(email="luca@t.it").one()
            self.assertTrue(u.invito_in_attesa)
            self.assertEqual(u.ruolo, "ufficio")
        self.assertIn("Invito in attesa", self.c.get("/admin/aziende/concessionaria").get_data(as_text=True))
        link = self._link("luca@t.it")
        self.c.post("/logout")
        self.assertIn("Benvenuto, Luca Bianchi", self.c.get(link).get_data(as_text=True))
        n = len(self.posta)
        self.c.post(link, data={"nuova": "lucapass1", "conferma": "lucapass1"})
        self.assertEqual(len(self.posta), n)  # al primo accesso niente avviso di "password cambiata"
        self.assertEqual(self.c.get("/").status_code, 200)
        with self.app.app_context():
            u = Utente.query.filter_by(email="luca@t.it").one()
            self.assertFalse(u.invito_in_attesa)
            self.assertIsNotNone(u.ultimo_accesso_at)

    def test_invito_fornitore_e_rinvio(self):
        carr = self._id(Sede, nome="Carrozzeria B")
        self.c.post(f"/admin/aziende/fornitore/{carr}", data={"azione": "utente", "nome": "Gino", "email": "gino@t.it"})
        uid = self._id(Utente, email="gino@t.it")
        with self.app.app_context():
            self.assertEqual(db.session.get(Utente, uid).ruolo, "fornitore")
            self.assertEqual(db.session.get(Utente, uid).sede_id, carr)
        r = self.c.post(f"/admin/utenti/{uid}", data={"azione": "link"})
        self.assertIn("Invito per Gino", r.get_data(as_text=True))
        # utente già entrato: il link è per una nuova password
        r = self.c.post(f"/admin/utenti/{self._id(Utente, email='autista@t.it')}", data={"azione": "link"})
        self.assertIn("Nuova password per Autista", r.get_data(as_text=True))
        self.assertEqual(self.posta[-1]["oggetto"], "Getvingo: reimposta la tua password")

    def test_cambio_password_scollega_gli_altri_dispositivi(self):
        altro = self.app.test_client()
        altro.post("/login", data={"email": "admin@t.it", "password": "pw"})
        self.assertEqual(altro.get("/").status_code, 200)
        self.c.post("/password", data={"attuale": "pw", "nuova": "nuovapw12", "conferma": "nuovapw12"})
        self.assertEqual(self.c.get("/").status_code, 200)   # chi l'ha cambiata resta dentro
        self.assertEqual(altro.get("/ddt").status_code, 302)    # l'altro dispositivo no

    def test_pannello_solo_per_il_gestore(self):
        self.entra("ufficio")
        self.assertEqual(self.c.get("/piattaforma/").status_code, 404)
        self.assertNotIn("Pannello Getvingo", self.c.get("/").get_data(as_text=True))
        self.entra("admin")
        self.assertIn("Pannello Getvingo", self.c.get("/").get_data(as_text=True))
        self.assertEqual(self.c.get("/piattaforma/").status_code, 200)

    def test_nuova_concessionaria_separata_e_sospesa(self):
        self.abbina()  # un'auto nella concessionaria del gestore
        r = self.c.post("/piattaforma/nuova", data={"ragione_sociale": "Rossi Auto S.r.l.", "piva": "IT999",
                                                    "indirizzo": "Via Roma 1", "nome": "Mara Rossi",
                                                    "email": "mara@rossi.it"})
        self.assertIn("Invito per Mara Rossi", r.get_data(as_text=True))
        with self.app.app_context():
            org = Organizzazione.query.filter_by(nome="Rossi Auto S.r.l.").one()
            admin = Utente.query.execution_options(tutte_le_organizzazioni=True).filter_by(email="mara@rossi.it").one()
            self.assertEqual((admin.org_id, admin.ruolo, admin.invito_in_attesa), (org.id, "admin", True))
            az = Azienda.query.execution_options(tutte_le_organizzazioni=True).filter_by(org_id=org.id).one()
            self.assertEqual((az.piva, az.indirizzo), ("IT999", "Via Roma 1"))
            org_id = org.id
        # il gestore resta nella sua concessionaria
        self.assertIn("Golf", self.c.get("/stock").get_data(as_text=True))
        pannello = self.c.get("/piattaforma/").get_data(as_text=True)
        self.assertIn("Rossi Auto S.r.l.", pannello)
        # l'amministratore nuovo entra dal link e non vede nulla dell'altra concessionaria
        link = self._link("mara@rossi.it")
        mara = self.app.test_client()
        mara.post(link, data={"nuova": "marapass1", "conferma": "marapass1"})
        self.assertEqual(mara.get("/").status_code, 200)
        self.assertNotIn("Golf", mara.get("/stock").get_data(as_text=True))
        self.assertEqual(mara.get(f"/veicolo/{self._id(Veicolo, vin=VIN)}").status_code, 404)
        self.assertEqual(mara.get("/piattaforma/").status_code, 404)
        # sospesa: fuori subito, non rientra e i link non valgono
        self.c.post(f"/piattaforma/{org_id}/stato")
        self.assertEqual(mara.get("/ddt").status_code, 302)
        r = mara.post("/login", data={"email": "mara@rossi.it", "password": "marapass1"})
        self.assertIn("sospeso", r.get_data(as_text=True))
        self.c.post("/password-dimenticata", data={"email": "mara@rossi.it"})
        self.assertEqual(len([m for m in self.posta if m["a"] == "mara@rossi.it"]), 1)  # solo l'invito iniziale
        # riattivata: rientra
        self.c.post(f"/piattaforma/{org_id}/stato")
        mara.post("/login", data={"email": "mara@rossi.it", "password": "marapass1"})
        self.assertEqual(mara.get("/").status_code, 200)

    def test_il_gestore_non_sospende_la_propria(self):
        self.c.post("/piattaforma/1/stato")
        with self.app.app_context():
            self.assertTrue(db.session.get(Organizzazione, 1).attiva)

    def test_email_duplicata_nuova_concessionaria(self):
        r = self.c.post("/piattaforma/nuova", data={"ragione_sociale": "Doppia", "nome": "X", "email": "autista@t.it"})
        self.assertIn("Email già registrata", r.get_data(as_text=True))
        with self.app.app_context():
            self.assertIsNone(Organizzazione.query.filter_by(nome="Doppia").first())


class TestUtenzaMaster(Base):
    """Il gestore entra in qualsiasi concessionaria come amministratore ("Supporto Getvingo"), vede e
    modifica tutto, e torna al Pannello. Nessun altro può usare quell'accesso."""
    def setUp(self):
        super().setUp()
        self.app.config.update(POSTA_ATTIVA=False, GESTORI="admin@t.it", BASE_URL="https://getvingo.com")
        with self.app.app_context():
            org2 = servizi.crea_organizzazione("Altra Auto S.r.l.", "admin@altra.it", "password2")
            db.session.add_all([Sede(nome="Sede Altra", tipo="propria"), Tag(codice="ZZZZ9999")])
            db.session.add(Veicolo(vin="ZFA31200000123456", marca="Fiat", modello="Panda", condizione="nuovo",
                                   sede=Sede.query.filter_by(nome="Sede Altra").first(), stato="in_sede"))
            db.session.commit()
            org2.assistenza_consentita = True  # il cliente ha consentito l'assistenza (test del consenso a parte)
            db.session.commit()
            self.org2 = org2.id
            self.v2 = Veicolo.query.filter_by(vin="ZFA31200000123456").one().id

    def test_entra_vede_modifica_e_torna(self):
        self.abbina()
        self.assertEqual(self.c.get(f"/veicolo/{self.v2}").status_code, 404)  # da gestore, nella sua: non la vede
        r = self.c.post(f"/piattaforma/{self.org2}/entra")
        self.assertEqual(r.status_code, 302)
        home = self.c.get("/").get_data(as_text=True)
        self.assertIn("Sei dentro <b>Altra Auto S.r.l.</b> come Supporto Getvingo", home)
        stock = self.c.get("/stock").get_data(as_text=True)
        self.assertIn("Panda", stock)
        self.assertNotIn("Golf", stock)  # i dati della concessionaria del gestore restano fuori
        self.assertEqual(self.c.get(f"/veicolo/{self.v2}").status_code, 200)
        # modifica: aggiunge un utente al cliente (resta del cliente)
        self.c.post("/admin/aziende/concessionaria", data={"azione": "utente", "nome": "Nuovo Altra",
                                                          "email": "nuovo@altra.it", "ruolo": "ufficio"})
        with self.app.app_context():
            u = Utente.query.execution_options(tutte_le_organizzazioni=True).filter_by(email="nuovo@altra.it").one()
            self.assertEqual(u.org_id, self.org2)
            sup = Utente.query.execution_options(tutte_le_organizzazioni=True).filter_by(supporto=True).one()
            self.assertEqual((sup.org_id, sup.ruolo, sup.nome), (self.org2, "admin", "Supporto Getvingo"))
        # il cliente vede il supporto nel suo elenco, ma non può aprirlo
        gruppo = self.c.get("/admin/aziende/concessionaria").get_data(as_text=True)
        self.assertIn("Accesso dell'assistenza Getvingo", gruppo)
        self.assertEqual(self.c.get(f"/admin/utenti/{sup.id}").status_code, 302)
        # torna al Pannello con il proprio account
        self.assertEqual(self.c.post("/piattaforma/torna").headers["Location"], "/piattaforma/")
        self.assertIn("Golf", self.c.get("/stock").get_data(as_text=True))
        self.assertEqual(self.c.get(f"/veicolo/{self.v2}").status_code, 404)

    def test_solo_il_gestore_puo_entrare(self):
        self.entra("ufficio")
        self.assertEqual(self.c.post(f"/piattaforma/{self.org2}/entra").status_code, 404)
        self.assertEqual(self.c.get(f"/veicolo/{self.v2}").status_code, 404)

    def test_il_supporto_non_entra_con_password_ne_link(self):
        self.c.post(f"/piattaforma/{self.org2}/entra")
        self.c.post("/piattaforma/torna")
        with self.app.app_context():
            sup = Utente.query.execution_options(tutte_le_organizzazioni=True).filter_by(supporto=True).one()
            sup.imposta_password("conosciuta1")
            db.session.commit()
            email = sup.email
        altro = self.app.test_client()
        altro.post("/login", data={"email": email, "password": "conosciuta1"})
        self.assertEqual(altro.get("/ddt").status_code, 302)
        self.app.config["POSTA_ATTIVA"] = True
        self.app.extensions.setdefault("posta_inviata", []).clear()
        altro.post("/password-dimenticata", data={"email": email})
        self.assertEqual(self.app.extensions["posta_inviata"], [])

    def test_se_non_e_piu_gestore_esce_subito(self):
        self.c.post(f"/piattaforma/{self.org2}/entra")
        self.app.config["GESTORI"] = ""
        self.assertEqual(self.c.get("/ddt").status_code, 302)
        self.assertEqual(self.c.get(f"/veicolo/{self.v2}").status_code, 302)

    def test_entra_anche_in_un_cliente_sospeso(self):
        self.c.post(f"/piattaforma/{self.org2}/stato")  # sospesa
        self.c.post(f"/piattaforma/{self.org2}/entra")
        testo = self.c.get("/").get_data(as_text=True)
        self.assertIn("accesso sospeso", testo)
        cliente = self.app.test_client()
        cliente.post("/login", data={"email": "admin@altra.it", "password": "password2"})
        self.assertEqual(cliente.get("/ddt").status_code, 302)  # il cliente resta fuori

    def test_supporto_non_conta_tra_gli_utenti_del_cliente(self):
        self.c.post(f"/piattaforma/{self.org2}/entra")
        self.c.post("/piattaforma/torna")
        pannello = self.c.get("/piattaforma/").get_data(as_text=True)
        self.assertNotIn("supporto-", pannello)

    def _cliente(self):
        c = self.app.test_client()
        c.post("/login", data={"email": "admin@altra.it", "password": "password2"})
        return c

    def test_senza_consenso_non_si_entra(self):
        cliente = self._cliente()
        cliente.post("/admin/assistenza", data={"consenti": "0"})
        r = self.c.post(f"/piattaforma/{self.org2}/entra")
        self.assertEqual(r.headers["Location"], "/piattaforma/")
        self.assertEqual(self.c.get(f"/veicolo/{self.v2}").status_code, 404)
        self.assertIn("Assistenza non consentita", self.c.get("/piattaforma/").get_data(as_text=True))
        # il cliente lo riattiva: si entra
        cliente.post("/admin/assistenza", data={"consenti": "1"})
        self.c.post(f"/piattaforma/{self.org2}/entra")
        self.assertEqual(self.c.get(f"/veicolo/{self.v2}").status_code, 200)

    def test_tolto_il_consenso_il_gestore_torna_al_suo_account(self):
        self.abbina()
        self.c.post(f"/piattaforma/{self.org2}/entra")
        self._cliente().post("/admin/assistenza", data={"consenti": "0"})
        r = self.c.get("/stock")
        self.assertEqual(r.status_code, 200)
        testo = r.get_data(as_text=True)
        self.assertIn("Golf", testo)            # di nuovo nella sua concessionaria
        self.assertNotIn("Panda", testo)
        self.assertIn("ha tolto il consenso", testo)

    def test_ingressi_registrati_e_visibili_al_cliente(self):
        self.app.config["POSTA_ATTIVA"] = True
        posta = self.app.extensions.setdefault("posta_inviata", [])
        posta.clear()
        self.c.post(f"/piattaforma/{self.org2}/entra")
        self.assertEqual([m["a"] for m in posta], ["admin@altra.it"])
        self.assertIn("accesso dell'assistenza", posta[0]["oggetto"])
        self.c.post("/piattaforma/torna")
        pagina = self._cliente().get("/admin/assistenza").get_data(as_text=True)
        self.assertIn("admin@t.it", pagina)
        self.assertIn("Assistenza consentita", pagina)
        # il registro è del cliente: non compare nella concessionaria del gestore
        self.assertNotIn("admin@t.it</span>", self.c.get("/admin/assistenza").get_data(as_text=True))

    def test_il_supporto_non_cambia_il_consenso(self):
        self.c.post(f"/piattaforma/{self.org2}/entra")
        self.c.post("/admin/assistenza", data={"consenti": "0"})
        with self.app.app_context():
            self.assertTrue(db.session.get(Organizzazione, self.org2).assistenza_consentita)

    def test_consenso_spento_di_base(self):
        r = self.c.post("/piattaforma/nuova", data={"ragione_sociale": "Nuova S.r.l.", "nome": "N", "email": "n@nuova.it"})
        with self.app.app_context():
            self.assertFalse(Organizzazione.query.filter_by(nome="Nuova S.r.l.").one().assistenza_consentita)


class TestGoogle(Base):
    """Anteprima nei risultati di Google: la pagina d'ingresso è pubblica e descritta, il resto no."""
    def test_pagina_principale_con_testi_per_google(self):
        self.c.post("/logout")
        r = self.c.get("/")
        self.assertEqual(r.status_code, 200)  # niente rimando a /login: Google vede getvingo.com
        t = r.get_data(as_text=True)
        self.assertIn("<title>Getvingo · Gestione auto con il VIN per concessionarie</title>", t)
        self.assertIn('<meta name="description" content="Spostamenti con DDT automatico', t)
        self.assertIn('<link rel="canonical" href="https://getvingo.com/">', t)
        self.assertIn('"@type": "WebSite", "name": "Getvingo"', t)
        self.assertIn('og:image" content="https://getvingo.com/static/icone/anteprima-condivisione.png"', t)
        self.assertNotIn("X-Robots-Tag", r.headers)
        # il modulo della pagina principale entra davvero
        r = self.c.post("/login", data={"email": "admin@t.it", "password": "pw"})
        self.assertEqual(r.headers["Location"], "/")
        self.assertEqual(self.c.get("/").status_code, 200)

    def test_pagine_interne_e_link_personali_mai_indicizzati(self):
        self.assertEqual(self.c.get("/").headers.get("X-Robots-Tag"), "noindex, nofollow")  # collegato
        self.assertEqual(self.c.get("/ddt").headers.get("X-Robots-Tag"), "noindex, nofollow")
        self.c.post("/logout")
        self.assertEqual(self.c.get("/imposta-password/qualcosa").headers.get("X-Robots-Tag"), "noindex, nofollow")
        self.assertEqual(self.c.get("/password-dimenticata").headers.get("X-Robots-Tag"), "noindex, nofollow")
        self.assertIsNone(self.c.get("/privacy").headers.get("X-Robots-Tag"))

    def test_robots_sitemap_favicon(self):
        r = self.c.get("/robots.txt", base_url="https://getvingo.com")
        self.assertIn("Allow: /$", r.get_data(as_text=True))
        self.assertIn("Sitemap: https://getvingo.com/sitemap.xml", r.get_data(as_text=True))
        tecnico = self.c.get("/robots.txt", base_url="https://getvingo.onrender.com").get_data(as_text=True)
        self.assertEqual(tecnico, "User-agent: *\nDisallow: /\n")
        self.assertIn("<loc>https://getvingo.com/</loc>", self.c.get("/sitemap.xml").get_data(as_text=True))
        r = self.c.get("/favicon.ico")
        self.assertEqual(r.status_code, 200)
        r.close()
