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
# indirizzo completo trovato sulla mappa e dati fiscali validi (obbligatori per ogni società)
INDIRIZZO = {"indirizzo": "Via Roma 12", "cap": "31050", "comune": "Ponzano Veneto", "provincia": "TV",
             "stato": "Italia", "lat": "45.7089", "lon": "12.2101"}
FISCALI = {"piva": "01234567897", "codice_fiscale": "01234567897"}
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
            for nome, ruolo, sede in [("admin", "admin", None), ("ufficio", "backoffice", None),
                                      ("autista", "consegne", None), ("carr", "fornitore", carr)]:
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
            "vin": "wvwzzz1kzaw000123", "marca": "Volkswagen", "modello": "Golf", "colore": "Bianco", "condizione": "nuovo",
            "versione": "1.5 eTSI Life", "anno": "2024", "alimentazione": "Benzina",
            "sede_id": self._id(Sede, nome="Sede A"), "lat": "45.66", "lon": "12.24"})
        self.assertEqual(r.status_code, 302)
        return self._id(Veicolo, vin=VIN)

    def dati(self, sede, causale="Conto riparazione", lavorazione="Carrozzeria", **extra):
        d = {"a_sede_id": self._id(Sede, nome=sede), "causale": causale,
             "vettore_id": self._id(Vettore, nome="Vettore X"), "conducente": "Mario Rossi",
             "lavorazione": lavorazione, "lavorazione_note": "paraurti", "firma": FIRMA,
             "mezzo": "Su strada (guidata)", "assegnato_id": str(self._id(Utente, email="autista@t.it"))}
        d.update(extra)
        return d

    def arrivo(self, ddt_id, **data):
        """Conferma l'arrivo dal punto della destinazione (il telefono è dentro l'area)."""
        with self.app.app_context():
            dest = db.session.get(Ddt, ddt_id).movimento.a_sede
            data = {"lat": str(dest.lat), "lon": str(dest.lon), **data}
        return self.c.post(f"/ddt/{ddt_id}/arrivo", data=data)

    def fine_lavori(self, ddt_id, data=None, **kw):
        """Il fornitore conferma la fine dei lavori dal posto (il telefono manda la posizione del fornitore);
        il rientro, se preparato, è assegnato all'autista della concessionaria."""
        with self.app.app_context():
            sede = db.session.get(Ddt, ddt_id).movimento.sede_arrivo_effettiva
            dove = {"lat": str(sede.lat), "lon": str(sede.lon)}
        dove["chi_riporta"] = str(self._id(Utente, email="autista@t.it"))
        return self.c.post(f"/ddt/{ddt_id}/lavorazione-completata", data={**dove, **(data or {})}, **kw)

    def presa(self, ddt_id, data=None, **kw):
        """Presa in carico vicino all'auto (il telefono manda la posizione del punto di partenza)."""
        with self.app.app_context():
            da = db.session.get(Ddt, ddt_id).movimento.da_sede
            dove = {"lat": str(da.lat), "lon": str(da.lon)}
        return self.c.post(f"/ddt/{ddt_id}/presa-in-carico", data={**dove, **(data or {})}, **kw)

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
            "vin": VIN, "marca": "Volkswagen", "modello": "Golf", "colore": "Bianco", "condizione": "nuovo",
            "versione": "1.5 eTSI Life", "anno": "2024", "alimentazione": "Benzina", "sede_id": self._id(Sede, nome="Sede A")})
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
            "vin": VIN, "marca": "Volkswagen", "modello": "Golf", "colore": "Bianco", "condizione": "nuovo",
            "versione": "1.5 eTSI Life", "anno": "2024", "alimentazione": "Benzina", "sede_id": self._id(Sede, nome="Sede A")})
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

    def test_arrivo_solo_nel_raggio_anche_per_admin(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        d = self.ddt()
        r = self.c.post(f"/ddt/{d}/arrivo", data={"lat": "45.2", "lon": "12.2"})  # admin in ufficio, lontano
        with self.app.app_context():
            self.assertEqual(self.mov(d).stato, "aperto")
        self.c.post(f"/ddt/{d}/arrivo", data={"lat": "45.1", "lon": "12.1"})  # sul posto
        with self.app.app_context():
            self.assertEqual(self.mov(d).stato, "chiuso")

    def test_il_fornitore_destinatario_conferma_da_ovunque(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        d = self.ddt()
        self.entra("carr")
        self.c.post(f"/ddt/{d}/arrivo")  # senza posizione del telefono: no, ogni passo la registra
        with self.app.app_context():
            self.assertEqual(self.mov(d).stato, "aperto")
        self.c.post(f"/ddt/{d}/arrivo", data={"lat": "45.66", "lon": "12.24"})  # da lontano sì
        with self.app.app_context():
            m = self.mov(d)
            self.assertEqual(m.stato, "chiuso")
            self.assertEqual((m.arrivo_lat, m.veicolo.lat), (45.66, 45.1))  # l'auto prende le coordinate della destinazione

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
        self.assertIn("Dove la porti?", pagina)  # dal 12/11/2026 le destinazioni stanno già qui
        self.assertIn('class="auto-breve', pagina)  # carta compatta dell'auto subito in alto

    def test_tocco_anteprima_dati_preparazione_e_danno(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        self.arrivo(self.ddt())
        self.entra("carr")
        self.fine_lavori(self.ddt(), data={"rientro": "1"})
        self.entra("autista")
        self.presa(self.ddt(), data={"conducente": "Mario", "firma": FIRMA,
                                                                 "mezzo": "Bisarca"})
        self.arrivo(self.ddt())  # rientro in Sede A: parte da sola la preparazione
        with self.app.app_context():
            prep = Preparazione.query.one()
            prep.completata_at = prep.creata_at  # simula: già pronta e consegnabile in futuro
            db.session.get(Veicolo, vid).versione = "1.5 eTSI Life"
            db.session.commit()
        self.c.post("/t/ABCD2345", data=POS)
        pagina = self.c.get(f"/veicolo/{vid}/tocco").get_data(as_text=True)
        for testo in ('class="auto-breve', "Segnala un danno", "Volkswagen Golf", "AW000123", "Sede A"):
            self.assertIn(testo, pagina)
        self.assertNotIn("Metti in preparazione", pagina)  # già pronta

    def test_tocco_metti_in_preparazione_senza_ddt(self):
        vid = self.abbina()
        self.entra("autista")
        pagina = self.c.get(f"/veicolo/{vid}/tocco").get_data(as_text=True)
        self.assertIn("Metti in preparazione", pagina)
        self.assertIn("Segnala un danno", pagina)  # dal 12/11/2026 sera: sul posto solo preparazione e danno
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
                             ("aperto", "Invio lavorazione", "Autista", "Vettore X", "Bisarca"))
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
                             ("Spostamento", Movimento.NESSUNA))
        self.arrivo(d)
        with self.app.app_context():
            self.assertEqual(self.mov(d).stato, "chiuso")

    def test_destinazione_scritta_a_mano_con_indirizzo_sulla_mappa(self):
        vid = self.abbina()
        self.profilo()
        # senza indirizzo trovato sulla mappa e senza dati fiscali non si parte
        self.c.post(f"/veicolo/{vid}/sposta", data={"nuova_destinazione": "Garage Verdi", "tipo": "officina"})
        with self.app.app_context():
            self.assertIsNone(Sede.query.filter_by(nome="Garage Verdi").first())
        self.c.post(f"/veicolo/{vid}/sposta", data={"nuova_destinazione": "  Garage   Verdi ", "tipo": "officina",
                                                    **INDIRIZZO, **FISCALI})
        d = self.ddt()
        with self.app.app_context():
            m = self.mov(d)
            self.assertEqual((m.a_sede.nome, m.a_sede.tipo, m.a_sede.lat, m.causale),
                             ("Garage Verdi", "officina", 45.7089, "Invio lavorazione"))
            self.assertEqual(m.a_sede.indirizzo_completo, "Via Roma 12, 31050 Ponzano Veneto (TV), Italia")
            self.assertEqual(m.ddt.dati_fissi["destinatario"][1], "Via Roma 12, 31050 Ponzano Veneto (TV), Italia")
        self.c.post(f"/ddt/{d}/lavorazione", data={"lavorazione": "Revisione", "lavorazione_note": "annuale"})
        self.c.post(f"/ddt/{d}/arrivo", data={"lat": "45.7089", "lon": "12.2101"})
        with self.app.app_context():
            self.assertEqual(self.mov(d).stato, "chiuso")
        # stesso nome scritto di nuovo: si riusa il luogo, niente doppioni
        with self.app.app_context():
            self.assertEqual(servizi.destinazione_scritta("garage verdi").id, self.mov(d).a_sede_id)
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

    def test_rientro_dal_fornitore_entro_200_metri(self):
        """Dal fornitore il rientro lo crea il suo account da ovunque; la concessionaria solo sul posto
        (entro 200 m), anche se la lavorazione non è ancora segnata come finita."""
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        self.arrivo(self.ddt())
        self.profilo()  # autista della concessionaria
        sede_a = self._id(Sede, nome="Sede A")
        # lontano dalla carrozzeria (45.1, 12.1): né il modulo completo né "Inizia spostamento"
        self.parti(vid, "Sede A", causale="Reso lavorazione", lavorazione="Nessuna (solo trasferimento)",
                   lat="45.66", lon="12.24")
        self.c.post("/t/ABCD2345", data=POS)
        self.assertIn("Sei a", self.c.post(f"/veicolo/{vid}/sposta", data={"a_sede_id": sede_a},
                                           follow_redirects=True).get_data(as_text=True))
        with self.app.app_context():
            self.assertEqual(Ddt.query.count(), 1)
        # senza posizione del telefono neppure
        with self.c.session_transaction() as sessione:
            sessione.pop("posizione_tocco", None)
        self.c.post(f"/veicolo/{vid}/sposta", data={"a_sede_id": sede_a})
        with self.app.app_context():
            self.assertEqual(Ddt.query.count(), 1)
        # tocco sul posto (circa 50 m): si parte, mittente il fornitore
        self.c.post("/t/ABCD2345", data={"lat": "45.1004", "lon": "12.1"})
        pagina = self.c.get(f"/veicolo/{vid}/sposta").get_data(as_text=True)
        self.assertIn("Partenza · non modificabile", pagina)
        self.c.post(f"/veicolo/{vid}/sposta", data={"a_sede_id": sede_a})
        with self.app.app_context():
            m = self.mov(self.ddt())
            self.assertEqual((m.da_sede.nome, m.causale, m.stato), ("Carrozzeria B", "Reso lavorazione", "aperto"))
            self.assertEqual(servizi.mittente(m)[0], "Carrozzeria B")  # il mittente resta il fornitore

    def test_rientro_da_ritirare_si_predispone_dall_ufficio(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        self.arrivo(self.ddt())
        andata = self.ddt()
        self.entra("autista")  # lontano dal fornitore, senza tocco
        rientro = {**self.dati("Sede A", causale="Reso lavorazione", lavorazione="Nessuna (solo trasferimento)"),
                   "azione": "predisponi", "ore_ritiro": "24"}
        self.c.post(f"/veicolo/{vid}/partenza", data=rientro)  # lavori non ancora conclusi: no
        with self.app.app_context():
            self.assertEqual(Ddt.query.count(), 1)
        self.entra("carr")
        self.fine_lavori(andata)  # senza preparare il rientro
        self.entra("autista")
        self.c.post(f"/veicolo/{vid}/partenza", data={**rientro, "assegnato_id": ""})  # senza incaricato: no
        with self.app.app_context():
            self.assertEqual(Ddt.query.count(), 1)
        self.c.post(f"/veicolo/{vid}/partenza", data=rientro)
        with self.app.app_context():
            m = self.mov(self.ddt())
            self.assertEqual((m.da_sede.nome, m.a_sede.nome, m.stato), ("Carrozzeria B", "Sede A", "predisposto"))

    def test_rientro_lo_crea_il_fornitore_da_ovunque(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        self.arrivo(self.ddt())
        self.profilo("carr")
        self.c.post(f"/veicolo/{vid}/sposta", data={"a_sede_id": self._id(Sede, nome="Sede A"),
                                                     "lat": "45.66", "lon": "12.24"})
        with self.app.app_context():
            self.assertEqual(self.mov(self.ddt()).da_sede.nome, "Carrozzeria B")
            self.assertEqual(Ddt.query.count(), 2)

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
        # il documento è essenziale: i tempi del viaggio stanno nella pagina del DDT, non nell'anteprima
        self.assertNotIn("Viaggio durato", self.c.get(f"/ddt/{d}/anteprima").get_data(as_text=True))
        pagina = self.c.get(f"/ddt/{d}").get_data(as_text=True)
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
        self.entra("ufficio")
        self.parti(vid, "Carrozzeria B", azione="predisponi", conducente="", firma="", ore_ritiro="8")
        d = self.ddt()
        with self.app.app_context():
            m = self.mov(d)
            self.assertEqual((m.stato, m.partenza_at), ("predisposto", None))
            self.assertEqual(m.veicolo.stato, "in_sede")

        self.entra("autista")
        self.presa(d, data={"conducente": "Luca", "firma": ""})
        with self.app.app_context():
            self.assertEqual(self.mov(d).stato, "predisposto")  # senza firma non parte
        self.presa(d, data={"conducente": "Luca", "firma": FIRMA})
        with self.app.app_context():
            m = self.mov(d)
            # il conducente è la persona incaricata del ritiro
            self.assertEqual((m.stato, m.conducente, m.veicolo.stato), ("aperto", m.assegnato_a.nome, "in_viaggio"))
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
        self.presa(d, data={"conducente": "Luca", "firma": FIRMA})
        with self.app.app_context():
            self.assertEqual(self.mov(d).stato, "predisposto")
        self.entra("ufficio")
        self.c.post(f"/ddt/{d}/rinnova", data={"ore": "24"})
        self.entra("autista")
        self.presa(d, data={"conducente": "Luca", "firma": FIRMA})
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
        self.fine_lavori(andata, data={"note": "fatto", "rientro": "1"})
        with self.app.app_context():
            v = db.session.get(Veicolo, vid)
            self.assertEqual(v.fase, "da_ritirare")
            rientro = v.movimento_predisposto
            self.assertEqual((rientro.a_sede.nome, rientro.ddt.creato_da.email), ("Sede A", "carr@t.it"))
            self.assertEqual(servizi.mittente(rientro)[0], "Carrozzeria B")
            rientro_id = rientro.ddt.id

        # l'autista ritira: tocca, firma, parte; all'arrivo il DDT si chiude e l'auto va in preparazione
        self.entra("autista")
        self.presa(rientro_id, data={"conducente": "Mario", "firma": FIRMA})  # manca la modalità
        with self.app.app_context():
            self.assertEqual(self.mov(rientro_id).stato, "predisposto")
        self.presa(rientro_id, data={"conducente": "Mario", "firma": FIRMA,
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

        self.c.post(f"/veicolo/{vid}/consegna", data={"lat": "45.0", "lon": "12.0"})  # senza sede di consegna non si consegna
        with self.app.app_context():
            self.assertEqual(db.session.get(Veicolo, vid).fase, "pronta")
        self.c.post(f"/veicolo/{vid}/consegna/sede", data={"sede_id": self._id(Sede, nome="Sede A")})
        self.c.post(f"/veicolo/{vid}/dati", data={"targa": "GH123AB"})  # senza targa non si consegna
        self.c.post(f"/veicolo/{vid}/consegna", data={"lat": "45.0", "lon": "12.0"})
        with self.app.app_context():
            v = db.session.get(Veicolo, vid)
            self.assertEqual((v.fase, v.tag), ("consegnato", None))
            self.assertIsNotNone(v.consegnato_at)
        self.assertIn("Volkswagen Golf", self.c.get("/preparazione?vista=consegnate").get_data(as_text=True))
        self.assertNotIn("Volkswagen Golf", self.c.get("/preparazione?vista=pronte").get_data(as_text=True))

    def test_solo_il_fornitore_completa_la_lavorazione(self):
        _, andata = self.arriva_in_carrozzeria()
        self.entra("autista")
        self.fine_lavori(andata)
        with self.app.app_context():
            self.assertIsNone(self.mov(andata).lavorazione_completata_at)

    def test_filtro_tipo_lavorazione(self):
        self.arriva_in_carrozzeria()
        self.entra("ufficio")
        self.assertIn("Volkswagen Golf", self.c.get("/lavorazioni?lavorazione=Carrozzeria").get_data(as_text=True))
        self.assertNotIn("Volkswagen Golf", self.c.get("/lavorazioni?lavorazione=Revisione").get_data(as_text=True))

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
            db.session.add(Veicolo(vin="WVWZZZ1KZAW000999", marca="Volkswagen", modello="Golf", colore="Nero",
                                   sede_id=1))
            db.session.add(Veicolo(vin="ZFA31200000123456", marca="Fiat", modello="500", sede_id=1))
            db.session.commit()
        pagina = self.c.get("/cerca", query_string={"marca": "Volkswagen", "modello": "Golf"}).get_data(as_text=True)
        self.assertEqual(pagina.count('class="voce fase'), 2)
        self.assertIn("1 simile<", self.c.get(f"/veicolo/{vid}").get_data(as_text=True))


class TestFiltriAdattivi(Base):
    def test_opzioni_solo_se_presenti(self):
        vid = self.abbina()
        pagina = self.c.get("/stock").get_data(as_text=True)
        self.assertIn("Volkswagen Golf", pagina)
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
        self.assertNotIn("Volkswagen Golf", solo_comm)
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

    def test_panoramica_con_trova_al_posto_della_chat(self):
        # 11/11/2026: per il momento la barra è solo "Trova" (la chat torna con routes.CHAT_PANORAMICA)
        import os
        os.environ.pop("ANTHROPIC_API_KEY", None)
        pagina = self.c.get("/").get_data(as_text=True)
        self.assertIn('id="trova-testo"', pagina)
        self.assertNotIn('id="chat-msg"', pagina)

    def test_trova_auto_ddt_e_adesivi(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        r = self.c.get("/api/trova?q=000123").get_json()  # ultime cifre del VIN
        self.assertEqual([a["url"] for a in r["auto"]], [f"/veicolo/{vid}"])
        self.assertIn("In viaggio", r["auto"][0]["sotto"])
        self.assertEqual(self.c.get("/api/trova?q=golf").get_json()["auto"][0]["titolo"], "Volkswagen Golf")
        d = self.c.get(f"/api/trova?q=1/{adesso().year}").get_json()["ddt"]
        self.assertEqual(d[0]["titolo"], f"DDT 1/{adesso().year}")
        self.assertEqual(self.c.get("/api/trova?q=abcd23").get_json()["adesivi"][0]["titolo"], "ABCD2345")
        self.assertEqual(self.c.get("/api/trova?q=x").get_json()["auto"], [])  # almeno 2 caratteri
        self.entra("carr")
        self.assertNotEqual(self.c.get("/api/trova?q=golf").status_code, 200)  # solo personale interno

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
        self.fine_lavori(d, data={"rientro": "1"})
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

    def test_ddt_eliminato_non_si_ripristina(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        d = self.ddt()
        self.c.post(f"/ddt/{d}/annulla")
        with self.app.app_context():
            self.assertIsNone(db.session.get(Ddt, d))
            self.assertEqual(Azione.query.filter_by(tipo="annullamento").count(), 0)



class TestPiazzaleMappa(Base):
    def test_un_punto_per_sede_esatti_se_filtro(self):
        self.abbina()
        with self.app.app_context():
            db.session.add(Veicolo(vin="WVWZZZ1KZAW000999", marca="Volkswagen", modello="Golf", sede_id=1,
                                   lat=45.0001, lon=12.0001))
            db.session.commit()
        pagina = self.c.get("/stock").get_data(as_text=True)
        self.assertIn('"n": 2', pagina)              # un punto per piazzale con 2 auto
        self.assertEqual(pagina.count('"fase":'), 2)  # le singole auto per la vista ravvicinata
        self.assertNotIn("45.660000", pagina)        # niente coordinate scritte nell'elenco
        self.assertIn('data-sede="1"', self.c.get("/stock?sede=1").get_data(as_text=True))


class TestStockEDati(Base):
    def test_nuova_auto_solo_con_adesivo(self):
        dati = {"vin": VIN, "marca": "Volkswagen", "modello": "Golf", "targa": "gh 123 ab", "condizione": "usato",
                "colore": "Bianco", "versione": "1.5 eTSI Life", "anno": "2024", "alimentazione": "Benzina",
                "tipologia": "commerciale", "sede_id": self._id(Sede, nome="Sede A")}
        self.c.post("/piazzale/nuova", data=dati)  # senza adesivo non entra in stock
        with self.app.app_context():
            self.assertEqual(Veicolo.query.count(), 0)
        self.c.post("/piazzale/nuova", data=dict(dati, tag="ABCD2345"))
        with self.app.app_context():
            v = Veicolo.query.one()
            self.assertEqual((v.targa, v.tipologia, v.sede.nome, v.tag.codice), ("GH123AB", "commerciale", "Sede A", "ABCD2345"))
        self.assertIn("Volkswagen Golf", self.c.get("/stock?q=gh123").get_data(as_text=True))

    def test_modifica_dati_e_preparazione_diretta(self):
        vid = self.abbina()
        self.c.post(f"/veicolo/{vid}/dati", data={"marca": "Volkswagen", "modello": "Golf GTI", "colore": "Rosso",
                                                  "targa": "AB123CD", "tipologia": "vettura"})
        self.c.post(f"/veicolo/{vid}/dati", data={"targa": "ZZ999ZZ"})  # la targa inserita non si cambia
        self.c.post(f"/veicolo/{vid}/preparazione", data={"pronta_entro": "2026-11-20T16:00"})
        with self.app.app_context():
            v = db.session.get(Veicolo, vid)
            # i dati restano quelli dell'ingresso: si aggiunge solo la targa
            self.assertEqual((v.modello, v.targa, v.fase), ("Golf", "AB123CD", "preparazione"))
            self.assertEqual(v.preparazione_attiva.pronta_entro_at.hour, 16)
        pagina = self.c.get(f"/veicolo/{vid}").get_data(as_text=True)
        self.assertIn("posizione aggiornata", pagina)
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
        self.entra("ufficio")
        self.c.post(f"/ddt/{d}/modifica", data={"conducente": ""})
        dati = self.dati("Sede A", causale="Spostamento", lavorazione="Lavaggio / detailing",
                         conducente="Luca Bianchi")
        self.c.post(f"/ddt/{d}/modifica", data=dati)
        self.c.post(f"/ddt/{d}/modifica", data=dati)  # nessun cambiamento: nessuna revisione
        with self.app.app_context():
            documento = db.session.get(Ddt, d)
            self.assertEqual(documento.revisione, 1)
            campi = {c["campo"]: (c["prima"], c["dopo"]) for c in documento.revisioni[0].modifiche}
            self.assertEqual(campi["Conducente"], ("Mario Rossi", "Luca Bianchi"))
        # l'admin modifica senza lasciare revisioni
        self.entra("admin")
        self.c.post(f"/ddt/{d}/modifica", data=dict(dati, conducente="Anna Verdi"))
        with self.app.app_context():
            documento = db.session.get(Ddt, d)
            self.assertEqual((documento.revisione, len(documento.revisioni)), (1, 1))
            self.assertEqual(documento.movimento.conducente, "Anna Verdi")
        self.arrivo(d)
        # dal 10/11/2026 ogni mansione interna può correggere un DDT anche dopo l'arrivo; il fornitore no
        self.entra("autista")
        self.assertEqual(self.c.get(f"/ddt/{d}/modifica").status_code, 200)
        self.entra("carr")
        self.assertEqual(self.c.post(f"/ddt/{d}/modifica", data=dati).status_code, 403)

    def test_eliminazione_solo_admin_senza_motivo_e_senza_traccia(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        d = self.ddt()
        self.entra("ufficio")
        self.c.post(f"/ddt/{d}/annulla", data={"motivo": "errore"})
        with self.app.app_context():
            self.assertIsNotNone(db.session.get(Ddt, d))
        self.entra("admin")
        self.c.post(f"/ddt/{d}/annulla")  # motivo non richiesto
        with self.app.app_context():
            self.assertIsNone(db.session.get(Ddt, d))  # sparito del tutto
            self.assertEqual(Movimento.query.count(), 0)
            self.assertEqual(DdtArchivio.query.count(), 0)
            self.assertEqual(db.session.get(Veicolo, vid).stato, "in_sede")  # torna all'origine
        self.c.get("/")  # consuma i messaggi di conferma
        self.assertNotIn(f'href="/ddt/{d}"', self.c.get("/ddt?stato=annullati").get_data(as_text=True))

    def test_filtri_archivio(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B", conducente="Giulia Verdi")
        def trovati(**q):
            return self.c.get("/ddt", query_string=q).get_data(as_text=True).count('class="riga-stock ddt-')  # elenco a righe (13/11/2026)
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
        self.assertEqual(self.c.get("/ddt").get_data(as_text=True).count('class="riga-stock ddt-'), 0)
        self.assertEqual(self.c.get("/").status_code, 200)

    def test_pagine_si_aprono_per_ogni_ruolo(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        d = self.ddt()
        interne = ["/", "/cerca", "/cerca?q=000123", f"/veicolo/{vid}", "/ddt", "/ddt?stato=tutti&q=golf",
                   f"/ddt/{d}", f"/arrivo/{d}", "/lavorazioni", "/preparazione", "/preparazione?vista=pronte",
                   "/preparazione?vista=consegnate", "/stock", "/stock?tipologia=vettura",
                   "/cerca?marca=Volkswagen&fase=in_viaggio"]
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
        r = self.c.post("/abbina/ZZZZ9999", data={"vin": VIN, "marca": "Volkswagen", "modello": "Polo", "colore": "Blu", "condizione": "usato", "versione": "1.5 eTSI Life", "anno": "2024", "alimentazione": "Benzina",
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
        self.entra("ufficio")  # una modifica dell'ufficio è una revisione con la sua copia
        self.c.post(f"/ddt/{d}/modifica", data=self.dati("Carrozzeria B", conducente="Luca Bianchi"))
        chiavi = self.versioni(d)
        self.assertEqual(len(chiavi), 2)
        self.assertIn("-rev1", chiavi[1])
        cartella = self.app.config["ARCHIVIO_DIR"]
        for k in chiavi:
            with open(f"{cartella}/{k}", "rb") as f:
                self.assertTrue(f.read().startswith(b"%PDF"))
        # l'admin corregge senza traccia: in archivio resta solo la versione nuova, senza revisione
        self.entra("admin")
        self.c.post(f"/ddt/{d}/modifica", data=self.dati("Carrozzeria B", conducente="Anna Verdi"))
        chiavi_dopo = self.versioni(d)
        self.assertEqual(len(chiavi_dopo), 1)
        self.assertIn("-rev1", chiavi_dopo[0])
        import os
        self.assertFalse(any(os.path.exists(f"{cartella}/{k}") for k in chiavi))

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
                db.session.add(Veicolo(vin=vin, marca="Volkswagen", modello=f"Golf {n}", sede=sede, stato="in_sede"))
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
        self.fine_lavori(d, data={"movimento_id": mid, "rientro": "1"})
        with self.app.app_context():
            documento = db.session.get(Ddt, d)
            fatte = [m.veicolo_id for m in documento.movimenti if m.lavorazione_completata_at]
            self.assertEqual(fatte, [vid])  # solo l'auto scelta
            self.assertIsNotNone(db.session.get(Veicolo, vid).movimento_predisposto)  # rientro pronto

    def test_eliminazione_per_tutte(self):
        self.emetti()
        d = self.ddt()
        self.c.post(f"/ddt/{d}/annulla")
        with self.app.app_context():
            self.assertIsNone(db.session.get(Ddt, d))
            self.assertTrue(all(db.session.get(Veicolo, i).stato == "in_sede" for i in self.ids))

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
            servizi.completa_lavorazione(m, carr, prepara_rientro=False, lat=45.1, lon=12.1)
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
            self.assertRegex(pagina, r"passi-(nomi|riga)")  # i 4 passi (a righe dal 13/11/2026)
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
        self.assertIn("Pronta: in attesa", self.c.get("/preparazione").get_data(as_text=True))

    def test_stock_fino_a_pronta_e_conteggi(self):
        vid = self.abbina()
        self.assertIn("Volkswagen Golf", self.c.get("/stock").get_data(as_text=True))
        self.c.post(f"/veicolo/{vid}/preparazione")
        self.c.get("/")  # consuma il messaggio di conferma
        stock = self.c.get("/stock").get_data(as_text=True)
        self.assertIn("Zona preparazione", stock)  # in preparazione resta in stock, nella sua zona
        self.assertIn("Passo 1 di 4 · Controllo rientro", stock)
        self.assertIn("Volkswagen Golf", stock)
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
        self.assertIn("Volkswagen Golf", stock)

    def test_stock_tiene_lavorazioni_esterne_e_viaggi(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        stock = self.c.get("/stock").get_data(as_text=True)
        self.assertIn("In viaggio", stock)
        self.assertIn("Volkswagen Golf", stock)
        self.arrivo(self.ddt())
        stock = self.c.get("/stock").get_data(as_text=True)
        self.assertIn("Lavorazioni esterne", stock)
        self.assertIn("Volkswagen Golf", stock)
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
            # 14/11/2026: 5 giorni lavorativi del fornitore (8 ore, 8–12 e 13–17) dall'arrivo, orario alla mezz'ora
            self.assertEqual((m.stima_giorni, m.giorni_ritardo), (5, 0))
            self.assertEqual(m.lavorazione_prevista_at, servizi.fine_lavori_prevista(m.arrivo_at, 5))
            self.assertIn(m.lavorazione_prevista_at.minute, (0, 30))
            self.assertTrue(0 < m.avanzamento_lavorazione < 100)
        self.c.post(f"/movimento/{mid}/stima", data={"giorni": "1"})  # stima più corta: già oltre la fine
        with self.app.app_context():
            m = db.session.get(Movimento, mid)
            self.assertEqual(m.stima_giorni, 1)
            self.assertEqual(m.avanzamento_lavorazione, 100 if m.lavorazione_prevista_at <= adesso() else m.avanzamento_lavorazione)
        stock = self.c.get("/stock").get_data(as_text=True)
        self.assertRegex(self.c.get("/lavorazioni").get_data(as_text=True), r"lavoro-(corso|ritardo)")  # riga colorata per stato
        self.assertIn("lavoro-breve", self.c.get(f"/veicolo/{vid}/tocco").get_data(as_text=True))  # dal 12/11/2026 barra compatta

    def test_sollecito_solo_in_ritardo_e_risposta_del_fornitore(self):
        vid, mid = self.in_carrozzeria(2)
        self.c.post(f"/movimento/{mid}/sollecito")  # ancora in tempo: niente sollecito
        with self.app.app_context():
            self.assertEqual(Sollecito.query.count(), 0)
        with self.app.app_context():
            db.session.get(Movimento, mid).arrivo_at = adesso() - timedelta(days=8)
            db.session.commit()
        pagina = self.c.get("/lavorazioni").get_data(as_text=True)
        with self.app.app_context():
            n = db.session.get(Movimento, mid).giorni_ritardo  # giorni oltre la fine prevista (orari dei fornitori)
        self.assertGreater(n, 0)
        self.assertIn(f"In ritardo di {n} {'giorno' if n == 1 else 'giorni'}", pagina)
        self.assertIn("Invia sollecito", pagina)
        self.c.post(f"/movimento/{mid}/sollecito")
        self.c.post(f"/movimento/{mid}/sollecito")  # una sola richiesta aperta alla volta
        with self.app.app_context():
            self.assertEqual(Sollecito.query.count(), 1)
            sid = Sollecito.query.one().id
        self.entra("carr")
        self.assertIn("La concessionaria chiede notizie", self.c.get("/").get_data(as_text=True))
        self.c.post(f"/sollecito/{sid}/risposta", data={"risposta": "Manca il ricambio", "nuova_stima": "2"})
        with self.app.app_context():
            m = db.session.get(Movimento, mid)
            self.assertEqual((Sollecito.query.one().risposta, m.stima_giorni, m.giorni_ritardo), ("Manca il ricambio", 10, 0))
        self.entra("admin")
        self.assertIn("Manca il ricambio", self.c.get("/lavorazioni").get_data(as_text=True))

    def test_niente_sollecito_se_finita(self):
        vid, mid = self.in_carrozzeria(9)
        self.entra("carr")
        self.fine_lavori(self.ddt(), data={"note": "ok"})
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
            v2 = Veicolo(vin="WVWZZZ1KZAW000999", marca="Volkswagen", modello="Polo", sede=Sede.query.filter_by(nome="Sede A").one(),
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
        self.assertRegex(self.c.get("/preparazione").get_data(as_text=True), r"\d{2}:\d{2}–\d{2}:\d{2}")  # orario in calendario


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
        self.c.post(f"/veicolo/{vid}/consegna/sede", data={"sede_id": eid})
        pagina = self.c.get(f"/veicolo/{vid}").get_data(as_text=True)
        self.assertIn("Emetti l'ultimo DDT", pagina)
        self.parti(vid, "Carrozzeria B")  # un'auto pronta non va altrove
        with self.app.app_context():
            self.assertEqual(Ddt.query.count(), 0)
        self.c.post(f"/veicolo/{vid}/consegna", data={"lat": "45.0", "lon": "12.0"})  # non è ancora nella sede di consegna
        self.c.post(f"/veicolo/{vid}/partenza", data=dict(self.dati("Sede E", causale="Trasferimento tra sedi",
                                                                    lavorazione="Nessuna (solo trasferimento)")))
        d = self.ddt()
        # in viaggio con l'ultimo DDT resta tra le pronte e in stock: esce solo alla consegna
        pronte = self.c.get("/preparazione?vista=pronte").get_data(as_text=True)
        self.assertIn("Volkswagen Golf", pronte)
        self.assertIn("con l'ultimo DDT", pronte)
        self.assertIn("ultimo DDT, poi consegna", self.c.get("/stock").get_data(as_text=True))
        self.assertIn("in viaggio con l'ultimo DDT", self.c.get(f"/veicolo/{vid}").get_data(as_text=True))
        self.arrivo(d)
        with self.app.app_context():
            v = db.session.get(Veicolo, vid)
            self.assertEqual((v.fase, v.sede.nome), ("pronta", "Sede E"))
        self.assertIn("Inserisci la targa", self.c.get(f"/veicolo/{vid}").get_data(as_text=True))
        self.c.post(f"/veicolo/{vid}/consegna", data={"lat": "45.0", "lon": "12.0"})  # senza targa non si consegna
        with self.app.app_context():
            self.assertEqual(db.session.get(Veicolo, vid).stato, "in_sede")
        self.c.post(f"/veicolo/{vid}/dati", data={"targa": "GH123AB"})
        self.c.post(f"/veicolo/{vid}/consegna", data={"lat": "45.0", "lon": "12.0"})
        with self.app.app_context():
            v = db.session.get(Veicolo, vid)
            self.assertEqual(v.stato, "consegnato")
            mese = f"{v.consegnato_at:%Y-%m}"
        cal = self.c.get(f"/preparazione?vista=consegnate&modo=calendario&mese={mese}").get_data(as_text=True)
        self.assertIn("cal-auto", cal)
        self.assertIn("Volkswagen Golf", self.c.get("/preparazione?vista=consegnate&q=000123").get_data(as_text=True))
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
        self.assertIn("in ritardo di", pagina.lower())
        self.assertNotIn('name="pronta_entro" value="2020', pagina)  # niente campo per spostarlo
        self.assertIn("In preparazione", pagina)  # colonna "da quanto" (14/11/2026)
        self.assertIn("Volkswagen Golf", self.c.get("/preparazione?stato=ritardo").get_data(as_text=True))

    def test_consegna_senza_appuntamento(self):
        """Getvingo calendarizza solo le preparazioni: per la consegna si indica la sede, il giorno lo
        fissa la concessionaria in un altro portale."""
        vid = TestConsegna.pronta(self)
        sid = self._id(Sede, nome="Sede A")
        pagina = self.c.get(f"/veicolo/{vid}").get_data(as_text=True)
        self.assertNotIn("data-scegli-slot", pagina)
        self.assertIn("Indica la sede di consegna", pagina)
        self.c.post(f"/veicolo/{vid}/consegna/sede", data={"sede_id": sid, "prevista": "2030-01-07T09:30"})
        with self.app.app_context():
            v = db.session.get(Veicolo, vid)
            self.assertEqual((v.consegna_sede_id, v.consegna_prevista_at), (sid, None))
        cal = self.c.get("/preparazione?vista=corso&modo=calendario&mese=2030-01").get_data(as_text=True)
        self.assertNotIn("consegna_prevista", cal)
        self.assertNotIn("Consegna prevista", self.c.get("/").get_data(as_text=True))

    def test_numeri_lavorazioni_filtrano(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        self.arrivo(self.ddt())
        self.assertIn("Volkswagen Golf", self.c.get("/lavorazioni?stato=corso").get_data(as_text=True))
        vuota = self.c.get("/lavorazioni?stato=ritardo").get_data(as_text=True)
        self.assertIn("Nessuna auto in ritardo", vuota)
        self.assertIn('class="stato-voce ritardo attivo', vuota)
        self.assertNotIn("dai fornitori</span>", vuota)  # il totale non c'è più: solo i 3 stati
        self.assertIn('class="pillole-tipi"', vuota)  # tipi di lavorazione a pillole (14/11/2026)
        self.assertNotIn("<span>in preparazione</span>", self.c.get("/preparazione").get_data(as_text=True))

    def test_stock_sedi_a_pulsanti_filtri_a_menu(self):
        self.abbina()
        stock = self.c.get("/stock").get_data(as_text=True)
        self.assertIn('class="pillole-sedi" id="scegli-piazzale"', stock)  # sedi a pillole (14/11/2026)
        self.assertNotIn('<select name="marca"', stock)  # 13/11/2026: marca, modello, colore si cercano
        self.assertIn('id="cerca-stock"', stock)


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
        self.fine_lavori(d)  # admin della concessionaria: non può
        with self.app.app_context():
            self.assertIsNone(self.mov(d).lavorazione_completata_at)
        pagina = self.c.get(f"/veicolo/{vid}").get_data(as_text=True)
        self.assertIn("paraurti", pagina)  # si vede la lavorazione in corso
        self.assertNotIn("Lavorazione completata</button>", pagina)
        self.entra("carr")
        self.fine_lavori(d)
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
        with self.app.app_context():
            giorno = servizi.giorno_lavorativo(3)  # 14/11/2026: si sceglie tra 1 e 10 giorni lavorativi
            lontano = servizi.giorno_lavorativo(15)
        g_ = giorno.isoformat()
        for fuori in (g_ + "T08:00", g_ + "T12:30", g_ + "T13:00", g_ + "T09:15", g_ + "T18:30",  # fuori dagli slot
                      lontano.isoformat() + "T09:00"):                                          # oltre 10 giorni lavorativi
            self.c.post(f"/veicolo/{vid}/preparazione", data={"modo_orario": "scelgo", "pronta_entro": fuori})
            with self.app.app_context():
                self.assertEqual(Preparazione.query.count(), 0, fuori)
        with self.app.app_context():
            slot = servizi._slot_del_giorno(datetime(2030, 1, 7).date())  # un lunedì
            self.assertEqual(len(slot), 16)  # 8 la mattina e 8 il pomeriggio
            self.assertEqual((slot[0].time(), slot[7].time(), slot[8].time(), slot[15].time()),
                             (datetime(1, 1, 1, 8, 30).time(), datetime(1, 1, 1, 12, 0).time(),
                              datetime(1, 1, 1, 14, 30).time(), datetime(1, 1, 1, 18, 0).time()))
        self.c.post(f"/veicolo/{vid}/preparazione", data={"modo_orario": "scelgo", "pronta_entro": g_ + "T18:00"})  # ultimo slot
        with self.app.app_context():
            self.assertEqual(Preparazione.query.one().pronta_entro_at, datetime.combine(giorno, datetime(1, 1, 1, 18, 0).time()))

    def test_orario_automatico_a_10_giorni_lavorativi_ed_elimina(self):
        vid = self.abbina()
        self.c.post(f"/veicolo/{vid}/preparazione", data={"modo_orario": "auto"})
        with self.app.app_context():
            p = Preparazione.query.one()
            self.assertEqual(p.pronta_entro_at.date(), servizi.giorno_lavorativo(10))
            pid = p.id
        self.c.post(f"/preparazione/{pid}/elimina")  # amministratore: la preparazione sparisce
        with self.app.app_context():
            self.assertEqual(Preparazione.query.count(), 0)
            self.assertEqual(db.session.get(Veicolo, vid).fase, "in_sede")


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
                                                 "colore": "Nero", "versione": "1.5 PHEV", "anno": "2024", "alimentazione": "Benzina",
                                                 "sede_id": self._id(Sede, nome="Sede A")})
        finally:
            marchi._scarica = originale
        self.assertTrue(chiesti and "simple-icons" in chiesti[0])
        self.assertIn("/marchi/lynk-co.svg", self.c.get("/stock").get_data(as_text=True))
        self.assertEqual(self.c.get("/marchi/lynk-co.svg").status_code, 200)


class TestLoghiUfficiali(Base):
    def test_carica_logo_ufficiale_e_precedenza(self):
        self.app.instance_path = tempfile.mkdtemp()
        self.abbina()  # Volkswagen
        pagina = self.c.get("/admin/marchi").get_data(as_text=True)
        self.assertIn("Volkswagen", pagina)
        svg = b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 10 10'><circle r='5'/></svg>"
        self.c.post("/admin/marchi", data={"marca": "Volkswagen", "file": (io.BytesIO(svg), "vw.svg")},
                    content_type="multipart/form-data")
        self.assertIn("Ufficiale · caricato in Gestione", self.c.get("/admin/marchi").get_data(as_text=True))
        self.assertIn("/marchi/ufficiali/volkswagen.svg", self.c.get("/stock").get_data(as_text=True))
        r = self.c.get("/marchi/ufficiali/volkswagen.svg")
        self.assertEqual(r.status_code, 200)
        self.assertIn("default-src 'none'", r.headers["Content-Security-Policy"])
        # un SVG con codice viene rifiutato
        cattivo = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
        self.c.post("/admin/marchi", data={"marca": "Volkswagen", "file": (io.BytesIO(cattivo), "x.svg")},
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
        self.assertNotIn("Volkswagen Golf", self.c.get("/lavorazioni").get_data(as_text=True))       # solo trasferimento

    def test_filtro_vetture_commerciali_in_preparazione(self):
        vid = self.abbina()
        self.c.post(f"/veicolo/{vid}/preparazione")
        self.assertIn("Volkswagen Golf", self.c.get("/preparazione?tipologia=vettura").get_data(as_text=True))
        self.assertNotIn("Volkswagen Golf", self.c.get("/preparazione?tipologia=commerciale").get_data(as_text=True))


class TestNuovoUsato(Base):
    def test_scelta_obbligatoria_e_mostrata_ovunque(self):
        dati = {"vin": VIN, "marca": "Volkswagen", "modello": "Golf", "tag": "ABCD2345", "sede_id": self._id(Sede, nome="Sede A"),
                "colore": "Bianco", "versione": "1.5 eTSI Life", "anno": "2024", "alimentazione": "Benzina"}
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
        for ruolo in ("admin", "ufficio", "autista", "venditore", "preparatore", "responsabile", "carrozzeria", "officina"):
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
                                                              "email": "nuovo@t.it", "ruolo": "consegne"})
        testo = r.get_data(as_text=True)
        self.assertIn("non è ancora attivo", testo)
        self.assertIn("https://getvingo.com/imposta-password/", testo)

    def test_invito_utente_nuovo(self):
        r = self.c.post("/admin/aziende/concessionaria", data={"azione": "utente", "nome": "Luca Bianchi",
                                                              "email": "Luca@t.it", "ruolo": "backoffice"})
        self.assertIn("Email mandata", r.get_data(as_text=True))
        with self.app.app_context():
            u = Utente.query.filter_by(email="luca@t.it").one()
            self.assertTrue(u.invito_in_attesa)
            self.assertEqual(u.ruolo, "backoffice")
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
        r = self.c.post("/piattaforma/nuova", data={"ragione_sociale": "Rossi Auto S.r.l.", "nome": "Mara Rossi",
                                                    "email": "mara@rossi.it", **INDIRIZZO, **FISCALI})
        self.assertIn("Invito per Mara Rossi", r.get_data(as_text=True))
        with self.app.app_context():
            org = Organizzazione.query.filter_by(nome="Rossi Auto S.r.l.").one()
            admin = Utente.query.execution_options(tutte_le_organizzazioni=True).filter_by(email="mara@rossi.it").one()
            self.assertEqual((admin.org_id, admin.ruolo, admin.invito_in_attesa), (org.id, "admin", True))
            az = Azienda.query.execution_options(tutte_le_organizzazioni=True).filter_by(org_id=org.id).one()
            self.assertEqual((az.piva, az.indirizzo, az.comune), ("01234567897", "Via Roma 12", "Ponzano Veneto"))
            legale = Sede.query.execution_options(tutte_le_organizzazioni=True).filter_by(org_id=org.id, sede_legale=True).one()
            self.assertEqual((legale.tipo, legale.indirizzo_completo), ("propria", "Via Roma 12, 31050 Ponzano Veneto (TV), Italia"))
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
        r = self.c.post("/piattaforma/nuova", data={"ragione_sociale": "Doppia", "nome": "X", "email": "autista@t.it",
                                                    **INDIRIZZO, **FISCALI})
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
                                                          "email": "nuovo@altra.it", "ruolo": "backoffice"})
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
        self.assertIn("L'assistenza Getvingo non è mai entrata", self.c.get("/admin/assistenza").get_data(as_text=True))

    def test_il_supporto_non_cambia_il_consenso(self):
        self.c.post(f"/piattaforma/{self.org2}/entra")
        self.c.post("/admin/assistenza", data={"consenti": "0"})
        with self.app.app_context():
            self.assertTrue(db.session.get(Organizzazione, self.org2).assistenza_consentita)

    def test_consenso_spento_di_base(self):
        r = self.c.post("/piattaforma/nuova", data={"ragione_sociale": "Nuova S.r.l.", "nome": "N", "email": "n@nuova.it",
                                                    **INDIRIZZO, **FISCALI})
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


class TestProfiloEGestione(Base):
    """Menu dell'avatar con "Il mio profilo"; Gestione divisa per argomento; dati aziendali con chiusure e loghi."""
    def test_menu_e_il_mio_profilo_per_ogni_ruolo(self):
        for chi in ("admin", "ufficio", "autista", "carr"):
            self.entra(chi)
            home = self.c.get("/").get_data(as_text=True)
            self.assertIn("Il mio profilo", home, chi)
            self.assertIn(f"{chi}@t.it", home, chi)  # email nell'intestazione del menu
            self.assertEqual("Gestione</a>" in home, chi == "admin", chi)
            r = self.c.get("/profilo")
            self.assertEqual(r.status_code, 200, chi)
            t = r.get_data(as_text=True)
            self.assertIn("Profilo di guida e firma da conducente", t)
            self.assertIn("Da completare", t)  # profilo di guida non ancora impostato
            self.assertEqual("Firma di Carrozzeria B" in t, chi == "carr", chi)

    def test_firma_del_fornitore_torna_al_profilo(self):
        self.entra("carr")
        r = self.c.post("/mia-firma", data={"firma": FIRMA})
        self.assertEqual(r.headers["Location"], "/profilo")

    def test_dati_aziendali(self):
        r = self.c.get("/admin/azienda")
        t = r.get_data(as_text=True)
        self.assertIn("Giorni di chiusura", t)
        self.assertIn("Loghi delle marche", t)
        self.c.post("/admin/azienda", data={"ragione_sociale": "Nuova Ragione S.p.A.", **INDIRIZZO, **FISCALI})
        with self.app.app_context():
            a = Azienda.query.one()
            self.assertEqual((a.ragione_sociale, a.indirizzo, a.piva), ("Nuova Ragione S.p.A.", "Via Roma 12", "01234567897"))
            self.assertEqual(a.firma, FIRMA)  # senza una firma nuova resta quella di prima
        self.c.post("/admin/azienda", data={"ragione_sociale": ""})
        with self.app.app_context():
            self.assertEqual(Azienda.query.one().ragione_sociale, "Nuova Ragione S.p.A.")
        # i dati non stanno più nella pagina degli utenti della concessionaria, che rimanda qui
        gruppo = self.c.get("/admin/aziende/concessionaria").get_data(as_text=True)
        self.assertNotIn('name="ragione_sociale"', gruppo)
        self.assertIn("/admin/azienda", gruppo)
        # chiusure e loghi tornano ai dati aziendali
        self.assertIn('href="/admin/azienda"', self.c.get("/admin/chiusure").get_data(as_text=True))
        self.assertIn('href="/admin/azienda"', self.c.get("/admin/marchi").get_data(as_text=True))

    def test_gestione_divisa_per_argomento(self):
        t = self.c.get("/admin/").get_data(as_text=True)
        for gruppo in ("Azienda", "Persone e accessi", "Operatività"):
            self.assertIn(gruppo, t)
        self.entra("ufficio")
        self.assertEqual(self.c.get("/admin/azienda").status_code, 403)



class TestConfigurazioneConcessionaria(Base):
    """Vettore della concessionaria, adesivi progressivi con sigla, comune dei luoghi, venditore."""
    def test_nuova_concessionaria_ha_vettore_e_sigla(self):
        with self.app.app_context():
            org = servizi.crea_organizzazione("Rossi Auto S.r.l.", "a@rossi.it", "password9")
            self.assertEqual(org.sigla, "RAUTO")
            v = Vettore.query.filter_by(org_id=org.id, proprio=True).one()
            self.assertEqual(v.nome, "Rossi Auto S.r.l.")
            org2 = servizi.crea_organizzazione("Rossi Auto S.p.A.", "b@rossi.it", "password9")
            self.assertNotEqual(org2.sigla, "RAUTO")  # unica su tutto il portale
            self.assertEqual(servizi.genera_sigla("Concessionaria Demo S.r.l."), "CDEMO")

    def test_vettore_compilato_dall_azienda(self):
        self.entra("autista")
        t = self.c.get("/profilo-guida").get_data(as_text=True)
        with self.app.app_context():
            proprio = Vettore.query.filter_by(proprio=True).one()  # creato al bisogno con i dati aziendali
            self.assertEqual(proprio.nome, "Test S.r.l.")
            self.assertIn(f'<option value="{proprio.id}" selected>', t)
        self.entra("carr")
        t = self.c.get("/profilo-guida").get_data(as_text=True)
        with self.app.app_context():
            suo = Vettore.query.filter_by(sede_id=self._id(Sede, nome="Carrozzeria B")).one()
            self.assertEqual(suo.nome, "Carrozzeria B")
            self.assertIn(f'<option value="{suo.id}" selected>', t)
            u = Utente.query.filter_by(email="carr@t.it").one()
            self.assertNotIn("vettore", servizi.profilo_guida_mancante(u))

    def test_dati_aziendali_aggiornano_il_vettore(self):
        self.c.post("/admin/azienda", data={"ragione_sociale": "Nuovo Nome S.r.l.", "sigla": "NNOME", **INDIRIZZO, **FISCALI})
        with self.app.app_context():
            v = Vettore.query.filter_by(proprio=True).one()
            self.assertEqual((v.nome, v.piva, v.comune), ("Nuovo Nome S.r.l.", "01234567897", "Ponzano Veneto"))
            self.assertEqual(db.session.get(Organizzazione, 1).sigla, "NNOME")

    def test_adesivi_progressivi_con_sigla(self):
        self.c.post("/admin/azienda", data={"ragione_sociale": "Test S.r.l.", "sigla": "tst", **INDIRIZZO, **FISCALI})
        self.c.post("/admin/tag", data={"quanti": "3"})
        self.c.post("/admin/tag", data={"quanti": "2"})
        with self.app.app_context():
            codici = sorted(t.codice for t in Tag.query if t.codice.startswith("TST-"))
            self.assertEqual(codici, ["TST-0001", "TST-0002", "TST-0003", "TST-0004", "TST-0005"])
        # il tocco funziona con il codice nuovo (anche scritto in minuscolo)
        self.assertIn("/abbina/TST-0001", self.c.post("/t/tst-0001", data=POS).headers["Location"])

    def test_sigla_non_valida_o_gia_usata(self):
        with self.app.app_context():
            servizi.crea_organizzazione("Altra S.r.l.", "x@altra.it", "password9")
            altra = Organizzazione.query.filter_by(nome="Altra S.r.l.").one().sigla
        for sbagliata in ("A1", "TROPPOLUNGA", altra):
            self.c.post("/admin/azienda", data={"ragione_sociale": "Test S.r.l.", "sigla": sbagliata, **INDIRIZZO, **FISCALI})
            with self.app.app_context():
                self.assertNotEqual(db.session.get(Organizzazione, 1).sigla, sbagliata.upper())

    def test_luoghi_con_comune_e_posizione(self):
        n = lambda: self._conta(Sede)
        prima = n()
        senza_mappa = {k: v for k, v in INDIRIZZO.items() if k not in ("lat", "lon")}
        self.c.post("/admin/sedi", data={"nome": "Piazzale Nord", "tipo": "propria", "comune": "Villorba"})
        self.c.post("/admin/sedi", data={"nome": "Piazzale Nord", "tipo": "propria", **senza_mappa})  # via non trovata
        self.assertEqual(n(), prima)
        self.c.post("/admin/sedi", data={"nome": "Piazzale Nord", "tipo": "propria", **INDIRIZZO, "comune": "Villorba",
                                         "cap": "31020", "lat": "45.70", "lon": "12.24"})
        sid = self._id(Sede, nome="Piazzale Nord")
        with self.app.app_context():
            s = db.session.get(Sede, sid)
            self.assertEqual((s.comune, s.provincia, s.lat, s.dove), ("Villorba", "TV", 45.70, "Villorba (TV)"))
            self.assertEqual(s.indirizzo_completo, "Via Roma 12, 31020 Villorba (TV), Italia")
        t = self.c.get(f"/admin/sedi/{sid}").get_data(as_text=True)
        self.assertNotIn("Firma predefinita del destinatario", t)  # sede propria: vale la firma aziendale
        self.assertIn("Dati aziendali", t)
        self.c.post(f"/admin/sedi/{sid}", data={"nome": "Piazzale Nord", "tipo": "propria", **INDIRIZZO, "comune": ""})
        with self.app.app_context():
            self.assertEqual(db.session.get(Sede, sid).comune, "Villorba")  # senza comune non si salva
        # nel caricamento dell'auto: solo i piazzali della concessionaria, con il comune
        nuova = self.c.get("/piazzale/nuova").get_data(as_text=True)
        self.assertIn("Piazzale Nord · Villorba (TV)", nuova)
        self.assertNotIn("Carrozzeria B</option>", nuova)
        self.assertNotIn("Cliente C</option>", nuova)

    def _conta(self, modello):
        with self.app.app_context():
            return modello.query.count()

    def test_venditore(self):
        self.c.post("/admin/aziende/concessionaria", data={"azione": "utente", "nome": "Giulia Vendite",
                                                          "email": "giulia@t.it", "ruolo": "venditore",
                                                          "password": "vendite12"})
        with self.app.app_context():
            u = Utente.query.filter_by(email="giulia@t.it").one()
            self.assertEqual(u.ruolo, "venditore")
            self.assertTrue(u.interno)
            self.assertTrue(u.gestione)  # dal 10/11/2026 ogni mansione interna fa tutto (tranne Gestione)
        vid = self.abbina()
        self.c.post("/logout")
        self.c.post("/login", data={"email": "giulia@t.it", "password": "vendite12"})
        for pagina in ("/", "/stock", "/preparazione", "/lavorazioni", "/ddt", f"/veicolo/{vid}"):
            self.assertEqual(self.c.get(pagina).status_code, 200, pagina)
        self.assertEqual(self.c.get("/admin/").status_code, 403)
        # sposta le auto di persona: profilo di guida (il vettore è già quello della concessionaria) e partenza
        self.c.post("/profilo-guida", data={"mezzo": "Su strada (guidata)", "firma": FIRMA})
        self.c.post("/t/ABCD2345", data=POS)
        r = self.c.post(f"/veicolo/{vid}/sposta", data={"a_sede_id": self._id(Sede, nome="Carrozzeria B")})
        self.assertEqual(r.status_code, 302)
        with self.app.app_context():
            m = Movimento.query.order_by(Movimento.id.desc()).first()
            self.assertEqual(m.conducente, "Giulia Vendite")
            self.assertTrue(m.vettore.proprio)


class TestAnagraficaEDDT(Base):
    """Indirizzi completi verificati, P. IVA e codice fiscale, sede legale, causali nuove, stima del viaggio, VIN."""
    def test_controlli_fiscali(self):
        from getvingo.anagrafica import cf_valido, piva_valida
        self.assertTrue(piva_valida("01234567897"))
        self.assertFalse(piva_valida("01234567890"))  # cifra di controllo sbagliata
        self.assertTrue(cf_valido("RSSMRA80A01H501U"))
        self.assertFalse(cf_valido("RSSMRA80A01H501X"))
        self.assertTrue(cf_valido("01234567897"))

    def _conta(self, **filtro):
        with self.app.app_context():
            return Sede.query.filter_by(**filtro).count()

    def test_destinazioni_esterne_con_dati_fiscali(self):
        base = {"nome": "Carrozzeria Nuova S.n.c.", "tipo": "carrozzeria", "gruppo": "esterne", **INDIRIZZO}
        self.c.post("/admin/sedi", data=base)  # senza P. IVA e C.F.: rifiutata
        self.c.post("/admin/sedi", data={**base, "piva": "01234567890", "codice_fiscale": "01234567890"})  # P. IVA errata
        self.assertEqual(self._conta(nome="Carrozzeria Nuova S.n.c."), 0)
        self.c.post("/admin/sedi", data={**base, **FISCALI})
        self.assertEqual(self._conta(nome="Carrozzeria Nuova S.n.c."), 1)
        # cliente privato: basta il codice fiscale
        self.c.post("/admin/sedi", data={"nome": "Mario Rossi", "tipo": "cliente", "gruppo": "esterne", **INDIRIZZO,
                                         "codice_fiscale": "RSSMRA80A01H501U"})
        self.assertEqual(self._conta(nome="Mario Rossi"), 1)
        # le due liste sono separate
        proprie = self.c.get("/admin/sedi?gruppo=proprie").get_data(as_text=True)
        esterne = self.c.get("/admin/sedi?gruppo=esterne").get_data(as_text=True)
        self.assertIn("Sede A", proprie)
        self.assertNotIn("<h3>Carrozzeria Nuova", proprie)
        for testo in ("Partner lavorazioni", "Esposizioni", "Clienti", "Carrozzeria Nuova", "Mario Rossi"):
            self.assertIn(testo, esterne)
        self.assertNotIn(">Sede A<", esterne)

    def test_sede_legale_creata_dai_dati_aziendali(self):
        self.c.post("/admin/azienda", data={"ragione_sociale": "Test S.r.l.", "sigla": "TEST", **INDIRIZZO, **FISCALI})
        self.c.post("/admin/azienda", data={"ragione_sociale": "Test S.r.l.", "sigla": "TEST", **INDIRIZZO, **FISCALI,
                                            "indirizzo": "Via Roma 14"})
        with self.app.app_context():
            legale = Sede.query.filter_by(sede_legale=True).one()  # una sola, aggiornata
            self.assertEqual((legale.tipo, legale.indirizzo, legale.lat), ("propria", "Via Roma 14", 45.7089))
            self.assertEqual(Azienda.query.one().indirizzo_completo, "Via Roma 14, 31050 Ponzano Veneto (TV), Italia")
        # i dati sbagliati non si salvano
        self.c.post("/admin/azienda", data={"ragione_sociale": "Test S.r.l.", "sigla": "TEST", **INDIRIZZO,
                                            "piva": "123", "codice_fiscale": "01234567897"})
        with self.app.app_context():
            self.assertEqual(Azienda.query.one().piva, "01234567897")

    def test_vettore_con_dati_obbligatori(self):
        self.c.post("/admin/vettori", data={"nome": "Trasporti Veloci S.r.l."})
        with self.app.app_context():
            self.assertIsNone(Vettore.query.filter_by(nome="Trasporti Veloci S.r.l.").first())
        self.c.post("/admin/vettori", data={"nome": "Trasporti Veloci S.r.l.", **INDIRIZZO, **FISCALI})
        with self.app.app_context():
            v = Vettore.query.filter_by(nome="Trasporti Veloci S.r.l.").one()
            self.assertEqual(v.indirizzo_completo, "Via Roma 12, 31050 Ponzano Veneto (TV), Italia")

    def test_causali_nuove(self):
        with self.app.app_context():
            a, carr, cli = (Sede.query.filter_by(nome=n).one() for n in ("Sede A", "Carrozzeria B", "Cliente C"))
            expo = Sede(nome="Fiera Verona", tipo="esposizione")
            self.assertEqual(servizi.causale_per(a, carr), "Invio lavorazione")
            self.assertEqual(servizi.causale_per(carr, a), "Reso lavorazione")
            self.assertEqual(servizi.causale_per(a, expo), "Conto esposizione")
            self.assertEqual(servizi.causale_per(expo, a), "Reso esposizione")
            self.assertEqual(servizi.causale_per(a, a), "Spostamento")
            self.assertEqual(servizi.causale_per(a, cli), "Consegna a cliente")
            self.assertFalse(expo.esterna)  # l'esposizione non è una lavorazione: niente lavorazione da indicare

    def test_ddt_vecchio_tiene_la_sua_causale(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B", causale="Conto riparazione")  # causale di prima: resta valida
        d = self.ddt()
        with self.app.app_context():
            self.assertEqual(db.session.get(Ddt, d).movimento.causale, "Conto riparazione")

    def test_stima_del_viaggio(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B", causale="Invio lavorazione")
        d = self.ddt()
        with self.app.app_context():
            m = db.session.get(Ddt, d).movimento
            st = servizi.stima_viaggio(m)
            self.assertGreater(st["km"], 5)       # Sede A (45.0, 12.0) → Carrozzeria B (45.1, 12.1)
            self.assertGreater(st["minuti"], 10)
        self.assertIn("arrivo previsto", self.c.get(f"/ddt/{d}").get_data(as_text=True))

    def test_marca_dal_vin(self):
        r = self.c.get("/api/veicolo?vin=WAUZZZGY5RA037752")
        self.assertEqual((r.status_code, r.json["marca"]), (200, "Audi"))
        self.assertEqual(self.c.get("/api/veicolo?vin=XXX00000000000000").status_code, 404)
        # se la marca non è scritta, al salvataggio la prende dal VIN
        self.c.post("/abbina/ABCD2345", data={"vin": "ZFA31200000123456", "modello": "Panda", "condizione": "nuovo",
                                              "colore": "Bianco", "versione": "1.0 Hybrid", "anno": "2024", "alimentazione": "Benzina",
                                              "sede_id": self._id(Sede, nome="Sede A")})
        with self.app.app_context():
            self.assertEqual(Veicolo.query.filter_by(vin="ZFA31200000123456").one().marca, "Fiat")
        self.assertIn('maxlength="17"', self.c.get("/piazzale/nuova").get_data(as_text=True))

    def test_mappa_stock_anche_senza_auto(self):
        with self.app.app_context():
            for s in Sede.query.filter_by(tipo="propria"):
                s.lat = s.lon = None
            db.session.commit()
        t = self.c.get("/stock").get_data(as_text=True)
        self.assertNotIn('id="mappa-piazzale"', t)
        self.assertIn("La mappa compare appena i piazzali hanno la loro posizione", t)
        with self.app.app_context():
            Sede.query.filter_by(nome="Sede A").one().lat, Sede.query.filter_by(nome="Sede A").one().lon = 45.0, 12.0
            s = Sede.query.filter_by(nome="Sede A").one(); s.lat, s.lon = 45.0, 12.0
            db.session.commit()
        self.assertIn('id="mappa-piazzale"', self.c.get("/stock").get_data(as_text=True))  # piazzale vuoto ma sulla mappa

    def test_ddt_emessi_non_cambiano_con_i_dati_nuovi(self):
        """I DDT emessi tengono la copia dei dati di quel momento: modifiche ad azienda, luoghi e vettori
        (e gli aggiornamenti del portale) non li toccano."""
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B", causale="Invio lavorazione")
        d = self.ddt()
        with self.app.app_context():
            prima = dict(db.session.get(Ddt, d).dati_fissi)
        self.c.post("/admin/azienda", data={"ragione_sociale": "Nome Cambiato S.r.l.", "sigla": "NCAMB", **INDIRIZZO, **FISCALI})
        with self.app.app_context():
            ddt = db.session.get(Ddt, d)
            self.assertEqual(ddt.dati_fissi, prima)
            self.assertEqual(ddt.dati_fissi["mittente"][0], "Test S.r.l.")


class TestNovitaAdmin(Base):
    """Rivenditori e allestitori, eliminazione di account e fornitori, note interne, password generata, ora di Roma."""
    def _sede(self, nome, tipo):
        with self.app.app_context():
            s = Sede(nome=nome, tipo=tipo, lat=45.2, lon=12.2, comune="Treviso", provincia="TV")
            db.session.add(s); db.session.commit()
            return s.id

    def test_rivenditore_conto_visione_o_vendita(self):
        riv = self._sede("Auto Bianchi S.r.l.", "rivenditore")
        vid = self.abbina()
        self.entra("autista")
        self.c.post("/profilo-guida", data={"vettore_id": self._id(Vettore, nome="Vettore X"), "mezzo": "Bisarca", "firma": FIRMA})
        self.c.post("/t/ABCD2345", data=POS)
        t = self.c.get(f"/veicolo/{vid}/sposta").get_data(as_text=True)
        self.assertIn(f'value="{riv}|Conto visione"', t)
        self.assertIn(f'value="{riv}|Vendita"', t)
        self.c.post(f"/veicolo/{vid}/sposta", data={"a_sede_causale": f"{riv}|Vendita"})
        d = self.ddt()
        with self.app.app_context():
            self.assertEqual(self.mov(d).causale, "Vendita")
        self.c.post(f"/ddt/{d}/arrivo", data={"lat": "45.2", "lon": "12.2"})
        with self.app.app_context():
            self.assertEqual(db.session.get(Veicolo, vid).stato, "consegnato")  # venduta: esce dallo stock
            a, r = Sede.query.filter_by(nome="Sede A").one(), db.session.get(Sede, riv)
            self.assertEqual(servizi.causale_per(a, r), "Conto visione")
            self.assertEqual(servizi.causale_per(r, a), "Reso")

    def test_allestitore_e_una_lavorazione(self):
        allest = self._sede("Allestimenti Rossi", "allestitore")
        with self.app.app_context():
            s = db.session.get(Sede, allest)
            self.assertTrue(s.esterna)
            self.assertEqual(servizi.causale_per(Sede.query.filter_by(nome="Sede A").one(), s), "Invio lavorazione")
            self.assertIn("Allestimento (veicoli commerciali)", Movimento.LAVORAZIONI)

    def test_elimina_utente(self):
        self.c.post("/admin/aziende/concessionaria", data={"azione": "utente", "nome": "Da Togliere",
                                                          "email": "togli@t.it", "ruolo": "consegne", "password": "password1"})
        uid = self._id(Utente, email="togli@t.it")
        self.c.post(f"/admin/utenti/{uid}", data={"azione": "elimina"})
        with self.app.app_context():
            self.assertIsNone(db.session.get(Utente, uid))  # mai usato: cancellato del tutto
        # un account con DDT nello storico: non entra più, sparisce dagli elenchi, l'email torna libera
        self.entra("autista")
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        self.entra("admin")
        aid = self._id(Utente, email="autista@t.it")
        self.c.post(f"/admin/utenti/{aid}", data={"azione": "elimina"})
        with self.app.app_context():
            u = db.session.get(Utente, aid)
            self.assertTrue(u.eliminato and not u.attivo)
            self.assertNotEqual(u.email, "autista@t.it")
        self.assertNotIn("autista@t.it", self.c.get("/admin/aziende/concessionaria").get_data(as_text=True))
        self.c.post("/admin/aziende/concessionaria", data={"azione": "utente", "nome": "Nuovo", "email": "autista@t.it",
                                                          "ruolo": "consegne", "password": "password1"})
        with self.app.app_context():
            self.assertEqual(Utente.query.filter_by(email="autista@t.it").count(), 1)
        # non si elimina sé stessi
        self.c.post(f"/admin/utenti/{self._id(Utente, email='admin@t.it')}", data={"azione": "elimina"})
        with self.app.app_context():
            self.assertIsNotNone(Utente.query.filter_by(email="admin@t.it").first())

    def test_elimina_fornitore(self):
        nuovo = self._sede("Officina Vuota", "officina")
        self.c.post(f"/admin/aziende/fornitore/{nuovo}", data={"azione": "utente", "nome": "Gino", "email": "gino@t.it",
                                                              "password": "password1"})
        self.c.post(f"/admin/aziende/fornitore/{nuovo}", data={"azione": "elimina_fornitore"})
        with self.app.app_context():
            self.assertIsNone(db.session.get(Sede, nuovo))
            self.assertIsNone(Utente.query.filter_by(email="gino@t.it").first())
        # con auto ancora da lui non si elimina
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        self.arrivo(self.ddt())
        carr = self._id(Sede, nome="Carrozzeria B")
        self.c.post(f"/admin/aziende/fornitore/{carr}", data={"azione": "elimina_fornitore"})
        with self.app.app_context():
            self.assertFalse(db.session.get(Sede, carr).eliminata)

    def test_note_interne(self):
        vid = self.abbina()
        aid = self._id(Utente, email="autista@t.it")
        self.c.post(f"/veicolo/{vid}/note", data={"cliente_nome": "Mario Bianchi", "venditore_id": aid,
                                                  "note_interne": "richiama martedì"})
        with self.app.app_context():
            v = db.session.get(Veicolo, vid)
            self.assertEqual((v.cliente_nome, v.venditore_id, v.note_interne), ("Mario Bianchi", aid, "richiama martedì"))
        self.assertIn("Mario Bianchi", self.c.get(f"/veicolo/{vid}").get_data(as_text=True))
        self.parti(vid, "Carrozzeria B")
        d = self.ddt()
        for pagina in (f"/ddt/{d}", f"/ddt/{d}/anteprima"):
            self.assertNotIn("Mario Bianchi", self.c.get(pagina).get_data(as_text=True))
        self.assertNotIn(b"Mario Bianchi", self.c.get(f"/ddt/{d}.pdf").data)
        self.entra("carr")  # il fornitore non le vede né le cambia
        self.assertNotIn("Mario Bianchi", self.c.get(f"/veicolo/{vid}").get_data(as_text=True))
        self.assertEqual(self.c.post(f"/veicolo/{vid}/note", data={"cliente_nome": "x"}).status_code, 403)

    def test_password_generata_una_volta(self):
        aid = self._id(Utente, email="autista@t.it")
        t = self.c.post(f"/admin/utenti/{aid}", data={"azione": "genera_password"}).get_data(as_text=True)
        import re
        pw = re.search(r'id="pw-generata"[^>]*value="([^"]+)"', t).group(1)
        self.assertEqual(len(pw), 10)
        altro = self.app.test_client()
        altro.post("/login", data={"email": "autista@t.it", "password": pw})
        self.assertEqual(altro.get("/ddt").status_code, 200)
        self.assertNotIn(pw, self.c.get(f"/admin/utenti/{aid}").get_data(as_text=True))  # non si rilegge

    def test_ora_di_roma(self):
        import os, time
        from getvingo.models import adesso
        vecchio = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"; time.tzset()
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            atteso = datetime.now(ZoneInfo("Europe/Rome")).replace(tzinfo=None, microsecond=0)
            self.assertLess(abs((adesso() - atteso).total_seconds()), 2)
        finally:
            if vecchio is None: os.environ.pop("TZ", None)
            else: os.environ["TZ"] = vecchio
            time.tzset()

    def test_concessionario_e_rivenditore_distinti(self):
        conc = self._sede("Concessionaria Verdi", "concessionario")
        with self.app.app_context():
            a, c = Sede.query.filter_by(nome="Sede A").one(), db.session.get(Sede, conc)
            self.assertEqual(Sede.TIPI["concessionario"], "Concessionario")
            self.assertEqual(Sede.TIPI["rivenditore"], "Rivenditore")
            self.assertEqual((servizi.causale_per(a, c), servizi.causale_per(c, a)), ("Conto visione", "Reso"))
            self.assertTrue(c.societa)
        esterne = self.c.get("/admin/sedi?gruppo=esterne").get_data(as_text=True)
        self.assertIn('value="concessionario"', esterne)
        self.assertIn('value="rivenditore"', esterne)
        self.assertIn("<h2>Concessionari</h2>", esterne)
        self.assertIn("<h2>Rivenditori</h2>", esterne)

    def test_preparazione_in_tempo(self):
        with self.app.app_context():
            sede = Sede.query.filter_by(nome="Sede A").one()
            for n, entro in enumerate((adesso() + timedelta(days=3), adesso() - timedelta(days=1))):
                v = Veicolo(vin=f"WVWZZZ1KZAW00{n}999", marca="Volkswagen", modello=f"Prova{n}", sede=sede, stato="in_sede")
                db.session.add(v); db.session.flush()
                db.session.add(Preparazione(veicolo=v, pronta_entro_at=entro))
            db.session.commit()
        t = self.c.get("/preparazione").get_data(as_text=True)
        self.assertRegex(t, r'(?s)stato-voce tempo[^>]*>\s*<span class="stato-riga">.*?<b>1</b>.*?Nei tempi')
        self.assertRegex(t, r'(?s)stato-voce ritardo[^>]*>\s*<span class="stato-riga">.*?<b>1</b>.*?In ritardo')
        tempo = self.c.get("/preparazione?vista=corso&stato=tempo").get_data(as_text=True)
        self.assertIn("Prova0", tempo)
        self.assertNotIn("Prova1", tempo)


class TestNovita1011(TestLavorazioniStimaESolleciti):
    """10/11/2026: mansioni della concessionaria, calendario delle lavorazioni, filtri di stato, ✕ sull'anteprima."""
    def test_mansioni_e_conversione_dei_ruoli_vecchi(self):
        from getvingo import migrazioni
        self.assertEqual(set(Utente.RUOLI_INTERNI),
                         {"backoffice", "venditore", "consegne", "preparatore", "resp_commerciale", "admin"})
        self.assertTrue(all(len(k) <= 20 for k in Utente.RUOLI))  # la colonna ruolo è di 20 caratteri
        with self.app.app_context():
            u = Utente.query.filter_by(email="autista@t.it").one()
            u.ruolo = "autista"  # come prima del 10/11/2026
            db.session.commit()
            self.assertEqual(migrazioni.aggiorna_ruoli(), 1)
            db.session.expire_all()
            u = Utente.query.filter_by(email="autista@t.it").one()
            self.assertEqual(u.ruolo, "consegne")
            self.assertTrue(u.interno and u.gestione)
        scheda = self.c.get("/admin/aziende/concessionaria").get_data(as_text=True)
        for nome in ("Backoffice", "Venditore", "Consegne", "Preparatore", "Responsabile commerciale"):
            self.assertIn(nome, scheda)
        self.c.post("/admin/aziende/concessionaria", data={"azione": "utente", "nome": "Elena", "email": "elena@t.it",
                                                          "ruolo": "resp_commerciale", "password": "password1"})
        with self.app.app_context():
            self.assertEqual(Utente.query.filter_by(email="elena@t.it").one().ruolo, "resp_commerciale")
        # tutte le mansioni vedono tutto, Gestione resta all'amministratore
        self.c.post("/logout")
        self.c.post("/login", data={"email": "elena@t.it", "password": "password1"})
        for pagina in ("/", "/stock", "/lavorazioni", "/preparazione", "/ddt", "/danni"):
            self.assertEqual(self.c.get(pagina).status_code, 200, pagina)
        self.assertEqual(self.c.get("/admin/").status_code, 403)

    def test_calendario_lavorazioni_e_fine_con_data(self):
        vid, mid = self.in_carrozzeria(2)
        with self.app.app_context():
            arrivo = db.session.get(Movimento, mid).arrivo_at
        fine = (arrivo + timedelta(days=7)).strftime("%Y-%m-%d")
        self.c.post(f"/movimento/{mid}/stima", data={"fine": fine})
        with self.app.app_context():
            self.assertEqual(db.session.get(Movimento, mid).stima_giorni, 7)
        self.c.post(f"/movimento/{mid}/stima", data={"fine": arrivo.strftime("%Y-%m-%d")})  # non prima dell'arrivo
        with self.app.app_context():
            self.assertEqual(db.session.get(Movimento, mid).stima_giorni, 7)
        mese = (arrivo + timedelta(days=7)).strftime("%Y-%m")
        cal = self.c.get(f"/lavorazioni?modo=calendario&mese={mese}").get_data(as_text=True)
        self.assertIn("cal-auto ev fine_prevista", cal)
        self.assertIn("Lavorazione da finire", cal)
        self.assertNotIn("barra-lavoro", cal)  # in calendario niente carte
        elenco = self.c.get("/lavorazioni").get_data(as_text=True)
        self.assertIn("Cambia la fine prevista", elenco)
        self.assertIn('class="stato-filtri"', elenco)
        self.assertIn("Nei tempi", elenco)
        # niente collegamento in Panoramica
        home = self.c.get("/").get_data(as_text=True)
        self.assertNotIn("/lavorazioni?modo=calendario", home)
        self.assertNotIn("/lavorazioni?stato", home)

    def test_preparazione_senza_scheda_in_preparazione_e_anteprima_con_chiudi(self):
        prep = self.c.get("/preparazione").get_data(as_text=True)
        self.assertNotIn('>In preparazione <span class="n">', prep)  # le schede doppie dei filtri non ci sono più
        for testo in ("Nei tempi", "In ritardo", "Pronte", "Consegnate"):
            self.assertIn(testo, prep)
        pronte = self.c.get("/preparazione?vista=pronte").get_data(as_text=True)
        self.assertIn('aria-current="true"', pronte)  # il filtro attivo si toglie toccandolo di nuovo
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        anteprima = self.c.get(f"/ddt/{self.ddt()}/anteprima").get_data(as_text=True)
        self.assertIn('aria-label="Chiudi l\'anteprima"', anteprima)
        self.assertIn("data-scarica-pdf", anteprima)


class TestSistemaDesign(Base):
    """10/11/2026: schede di filtro nel selettore segmentato, versione di CSS e JS, solo trasferimento dal fornitore."""
    def test_schede_filtro_e_versione_statici(self):
        ddt = self.c.get("/ddt").get_data(as_text=True)
        self.assertIn('class="interruttore filtro-gruppo in-riga schede-filtro"', ddt)
        self.assertRegex(ddt, r'style\.css\?v=\d+')
        self.assertIn('class="interruttore filtro-gruppo in-riga schede-filtro"', self.c.get("/danni").get_data(as_text=True))

    def test_solo_trasferimento_dal_fornitore(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B", lavorazione="Nessuna (solo trasferimento)", causale="Invio lavorazione")
        self.arrivo(self.ddt())
        scheda = self.c.get(f"/veicolo/{vid}").get_data(as_text=True)
        if "Presso Carrozzeria B" in scheda:
            self.assertIn("Solo trasferimento · nessuna lavorazione", scheda)
            self.assertNotIn("In lavorazione presso", scheda)

    def test_avviso_danni_proporzionato_e_riquadri_in_ordine(self):
        vid = self.abbina()
        with self.app.app_context():
            db.session.add(Danno(veicolo_id=vid, zona="Tetto", descrizione="grandine", segnalato_da_id=1))
            db.session.commit()
        home = self.c.get("/").get_data(as_text=True)
        self.assertIn("riga-avviso-danni", home)
        self.assertNotIn("blocca 1 consegna", home)  # auto in sede: non blocca consegne
        self.assertLess(home.index("In viaggio</span>"), home.index("Da ritirare</span>"))  # 14/11 sera: In viaggio · Dai fornitori · Da ritirare…
        self.assertNotIn("In stock</span>", home)  # tolto dalla Home
        self.c.post("/t/ABCD2345", data=POS)
        self.c.post(f"/veicolo/{vid}/preparazione")
        home = self.c.get("/").get_data(as_text=True)
        self.assertIn("riga-avviso-danni", home)
        self.assertIn("blocca 1 consegna", home)


class TestDdtEssenzialeEAnalisi(TestLavorazioniStimaESolleciti):
    """11/11/2026: DDT senza dati interni, copia firmata alle due parti, dati e pagina per le analisi."""
    def test_pdf_e_anteprima_senza_storico(self):
        from getvingo import ddt as ddt_pdf
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        d = self.ddt()
        self.c.post(f"/ddt/{d}/modifica", data=self.dati("Carrozzeria B", conducente="Luca Bianchi"))
        with self.app.app_context():
            documento = db.session.get(Ddt, d)
            pdf = ddt_pdf.pdf(documento, invariante=True)
        self.assertNotIn(b"STORICO MODIFICHE", pdf)
        self.assertNotIn(b"Emesso da", pdf)
        anteprima = self.c.get(f"/ddt/{d}/anteprima").get_data(as_text=True)
        self.assertNotIn("Rev. 1", anteprima)
        self.assertNotIn("Tocchi del viaggio", anteprima)
        self.assertIn('class="pagina-anteprima"', anteprima)

    def test_copia_firmata_alle_due_parti_e_download(self):
        self.app.config["POSTA_ATTIVA"] = True
        self.c.post("/admin/azienda", data={})  # niente: l'email si imposta sotto
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        d = self.ddt()
        self.arrivo(d)  # senza la spunta in Dati aziendali non parte nessuna email
        self.assertFalse([e for e in self.app.extensions.get("posta_inviata", []) if "firmato" in e["oggetto"]])
        pagina = self.c.get(f"/ddt/{d}").get_data(as_text=True)
        self.assertIn("data-scarica-subito", pagina)  # il PDF firmato si scarica da solo, una volta sola
        self.assertNotIn("data-scarica-subito", self.c.get(f"/ddt/{d}").get_data(as_text=True))
        with self.app.app_context():
            Azienda.query.first().invia_copie_ddt = True
            Azienda.query.first().email_ddt = "ddt@concessionaria.it"
            Sede.query.filter_by(nome="Carrozzeria B").one().email_ddt = "ddt@carrozzeria.it"
            db.session.commit()
        with self.app.test_request_context():
            from flask import g as _g
            from getvingo import copie
            _g.utente = Utente.query.filter_by(email="admin@t.it").one()
            copie.invia(db.session.get(Ddt, d))  # con la spunta la copia parte
        inviate = self.app.extensions.get("posta_inviata", [])
        # mittente, destinatario, backoffice (ufficio@t.it) e autista: chi ha avviato il trasporto (admin nel test)
        self.assertEqual(sorted(e["a"] for e in inviate if "firmato" in e["oggetto"]),
                         ["admin@t.it", "ddt@carrozzeria.it", "ddt@concessionaria.it", "ufficio@t.it"])
        self.assertTrue(all(e["allegati"] == [f"DDT_{1}_{adesso().year}.pdf"] for e in inviate if "firmato" in e["oggetto"]))
        with self.app.app_context():
            self.assertIsNotNone(db.session.get(Ddt, d).copie_inviate_at)

    def test_dati_per_le_analisi_e_pagina(self):
        vid, mid = self.in_carrozzeria(2)
        self.c.post(f"/movimento/{mid}/stima", data={"giorni": "9"})
        with self.app.app_context():
            m = db.session.get(Movimento, mid)
            self.assertEqual((m.lavorazione_stima_iniziale, m.stima_giorni), (5, 9))  # la prima stima resta
        self.c.post(f"/veicolo/{vid}/note", data={"venduta_il": "2026-11-01"})
        with self.app.app_context():
            self.assertEqual(str(db.session.get(Veicolo, vid).venduta_il), "2026-11-01")
        pagina = self.c.get("/analisi?periodo=tutto")
        self.assertEqual(pagina.status_code, 200)
        testo = pagina.get_data(as_text=True)
        self.assertIn("Le fasi, in media", testo)
        self.assertIn("Viaggi per tratta", testo)
        self.entra("carr")
        self.assertNotEqual(self.c.get("/analisi").status_code, 200)



class TestCorreggiPosizione(Base):
    """In "Modifica dati" l'amministratore sposta un'auto in stock senza DDT e senza lasciare traccia."""
    def setUp(self):
        super().setUp()
        with self.app.app_context():
            db.session.add(Sede(nome="Piazzale B", tipo="propria", lat=45.5, lon=12.5))
            db.session.commit()
        self.vid = self.abbina()

    def manda(self, **extra):
        d = {"vin": VIN, "condizione": "nuovo", "tipologia": "vettura", "marca": "Volkswagen", "modello": "Golf",
             "colore": "Bianco", "versione": "1.5 eTSI Life", "anno": "2024", "alimentazione": "Benzina", "in_stock_dal": adesso().date().isoformat(),
             "sede_id": self._id(Sede, nome="Piazzale B")}
        d.update(extra)
        return self.c.post(f"/veicolo/{self.vid}/modifica", data=d)

    def _conta(self):
        from getvingo.models import Evento
        with self.app.app_context():
            return (Evento.query.filter_by(veicolo_id=self.vid).count(), Azione.query.filter_by(veicolo_id=self.vid).count(),
                    Ddt.query.count(), db.session.get(Veicolo, self.vid).posizione_at)

    def test_admin_cambia_sede_e_punto_senza_traccia(self):
        prima = self._conta()
        self.assertIn("Sede, piazzale o partner lavorazioni", self.c.get(f"/veicolo/{self.vid}/modifica").get_data(as_text=True))
        self.assertEqual(self.manda(lat="45.5001", lon="12.5002").status_code, 302)
        with self.app.app_context():
            v = db.session.get(Veicolo, self.vid)
            self.assertEqual((v.sede.nome, v.lat, v.lon), ("Piazzale B", 45.5001, 12.5002))
        self.assertEqual(self._conta(), prima)

    def test_senza_punto_vale_la_sede(self):
        self.manda()
        with self.app.app_context():
            v = db.session.get(Veicolo, self.vid)
            self.assertEqual((v.lat, v.lon), (45.5, 12.5))

    def test_solo_admin_sedi_proprie_e_fornitori(self):
        self.manda(sede_id=self._id(Sede, nome="Cliente C"))  # un cliente no
        with self.app.app_context():
            self.assertEqual(db.session.get(Veicolo, self.vid).sede.nome, "Sede A")
        self.manda(sede_id=self._id(Sede, nome="Carrozzeria B"), lavorazione="Carrozzeria",
                   lavorazione_note="paraurti")  # un fornitore sì (con la lavorazione), e si può riportarla indietro
        with self.app.app_context():
            self.assertEqual(db.session.get(Veicolo, self.vid).sede.nome, "Carrozzeria B")
        # senza DDT d'arrivo le pagine si aprono lo stesso (prima la Panoramica andava in errore)
        for p in ("/", "/stock", "/lavorazioni", f"/veicolo/{self.vid}", f"/veicolo/{self.vid}/tocco", "/analisi"):
            self.assertEqual(self.c.get(p).status_code, 200, p)
        self.manda(sede_id=self._id(Sede, nome="Sede A"))
        self.entra("ufficio")
        self.assertNotIn("Sede, piazzale o partner lavorazioni", self.c.get(f"/veicolo/{self.vid}/modifica").get_data(as_text=True))
        self.manda(modello="Polo")  # gli altri dati sì, la sede no
        with self.app.app_context():
            v = db.session.get(Veicolo, self.vid)
            self.assertEqual((v.sede.nome, v.modello), ("Sede A", "Polo"))

    def test_non_in_viaggio(self):
        self.parti(self.vid, "Carrozzeria B")
        self.assertNotIn("Sede, piazzale o partner lavorazioni", self.c.get(f"/veicolo/{self.vid}/modifica").get_data(as_text=True))
        self.manda()
        with self.app.app_context():
            self.assertEqual(db.session.get(Veicolo, self.vid).stato, "in_viaggio")

class TestModificaDatiAuto(Base):
    """Un solo "Modifica dati": gli stessi campi dell'ingresso in stock, per tutto il personale interno."""
    def dati_auto(self, **extra):
        d = {"vin": VIN, "condizione": "usato", "tipologia": "commerciale", "targa": "GH123AB", "marca": "Volkswagen",
             "modello": "Polo", "versione": "1.0 TSI  Life", "colore": "Nero", "anno": "2024", "alimentazione": "Benzina", "in_stock_dal": "2026-10-01"}
        d.update(extra)
        return d

    def test_interni_cambiano_tutto(self):
        vid = self.abbina()
        self.entra("autista")
        self.assertIn(f"/veicolo/{vid}/modifica", self.c.get(f"/veicolo/{vid}").get_data(as_text=True))
        self.assertIn('value="Golf"', self.c.get(f"/veicolo/{vid}/modifica").get_data(as_text=True))
        r = self.c.post(f"/veicolo/{vid}/modifica", data=self.dati_auto(vin="WVWZZZ1KZAW000999"))
        self.assertEqual(r.status_code, 302)
        with self.app.app_context():
            v = db.session.get(Veicolo, vid)
            self.assertEqual((v.vin, v.condizione, v.tipologia, v.targa, v.modello, v.versione, v.colore,
                              v.creato_at.date().isoformat(), v.sede.nome),
                             ("WVWZZZ1KZAW000999", "usato", "commerciale", "GH123AB", "Polo", "1.0 TSI Life", "Nero",
                              "2026-10-01", "Sede A"))

    def test_controlli(self):
        vid = self.abbina()
        with self.app.app_context():
            db.session.add(Veicolo(vin="WVWZZZ1KZAW000777", marca="Volkswagen", modello="Up"))
            db.session.commit()
        for sbagliato in ({"vin": "WVWZZZ1KZAW000777"}, {"vin": "CORTO"}, {"in_stock_dal": "2999-01-01"},
                          {"modello": ""}, {"targa": "X1"}):
            self.c.post(f"/veicolo/{vid}/modifica", data=self.dati_auto(**sbagliato))
        with self.app.app_context():
            self.assertEqual(db.session.get(Veicolo, vid).modello, "Golf")
        self.c.post(f"/veicolo/{vid}/modifica", data=self.dati_auto(targa="X1", targa_ok="1"))
        with self.app.app_context():
            self.assertEqual(db.session.get(Veicolo, vid).targa, "X1")

    def test_fornitore_no(self):
        vid = self.abbina()
        self.entra("carr")
        self.assertEqual(self.c.post(f"/veicolo/{vid}/modifica", data=self.dati_auto()).status_code, 403)


class TestHomeFornitore(Base):
    """Home di chi esegue le lavorazioni esterne: in arrivo, poi Nei tempi · In ritardo · Completate."""
    in_carrozzeria = TestLavorazioniStimaESolleciti.in_carrozzeria

    def test_elenco_filtri_e_fine_lavori_sul_posto(self):
        vid, mid = self.in_carrozzeria(9)  # carrozzeria: stima 5 giorni, quindi in ritardo
        self.entra("admin")
        self.parti(self.abbina_altra(), "Carrozzeria B")  # in viaggio verso la carrozzeria
        self.entra("carr")
        pagina = self.c.get("/").get_data(as_text=True)
        for testo in ("<h1>Lavorazioni</h1>", "In arrivo da voi", "Nei tempi", "In ritardo", "Completate",
                      "Carrozzeria", "Conferma la conclusione dei lavori", "In ritardo di"):
            self.assertIn(testo, pagina)
        self.assertNotIn("None", pagina)
        self.assertNotIn("Conferma la conclusione", self.c.get("/?stato=finita").get_data(as_text=True))
        # lontano dalla carrozzeria (45.1, 12.1) o senza posizione: non si chiude
        for lontano in ({"lat": "45.66", "lon": "12.24"}, {}):
            self.c.post(f"/movimento/{mid}/lavorazione-completata", data={**lontano, "rientro": "1", "torna": "/"})
            with self.app.app_context():
                self.assertIsNone(db.session.get(Movimento, mid).lavorazione_completata_at)
        # sul posto: chiusa, posizione dell'auto aggiornata subito, rientro pronto, si torna alla home
        r = self.c.post(f"/movimento/{mid}/lavorazione-completata",
                        data={"lat": "45.1003", "lon": "12.1", "note": "fatto", "rientro": "1", "torna": "/",
                              "chi_riporta": str(self._id(Utente, email="autista@t.it"))})
        self.assertEqual(r.headers["Location"], "/")
        with self.app.app_context():
            v = db.session.get(Veicolo, vid)
            self.assertIsNotNone(db.session.get(Movimento, mid).lavorazione_completata_at)
            self.assertEqual((v.lat, v.lon), (45.1003, 12.1))
            self.assertEqual(v.ultima_posizione.come, "alla fine della lavorazione")
            self.assertIsNotNone(v.movimento_predisposto)
        pagina = self.c.get("/?stato=finita").get_data(as_text=True)
        self.assertIn("Lavori conclusi", pagina)
        self.assertIn("DDT di rientro", pagina)

    def test_admin_mette_dal_fornitore_con_lavorazione_obbligatoria(self):
        vid = self.abbina()
        base = {"vin": VIN, "condizione": "nuovo", "tipologia": "vettura", "marca": "Volkswagen", "modello": "Golf",
                "colore": "Bianco", "versione": "1.5 eTSI Life", "anno": "2024", "alimentazione": "Benzina", "in_stock_dal": adesso().date().isoformat(),
                "sede_id": self._id(Sede, nome="Carrozzeria B")}
        self.c.post(f"/veicolo/{vid}/modifica", data=base)  # senza lavorazione: no
        with self.app.app_context():
            self.assertEqual(db.session.get(Veicolo, vid).sede.nome, "Sede A")
        self.c.post(f"/veicolo/{vid}/modifica", data={**base, "lavorazione": "Nessuna (solo trasferimento)",
                                                      "lavorazione_note": "x"})  # deve essere una lavorazione vera
        with self.app.app_context():
            self.assertEqual(db.session.get(Veicolo, vid).sede.nome, "Sede A")
        self.c.post(f"/veicolo/{vid}/modifica", data={**base, "lavorazione": "Carrozzeria",
                                                      "lavorazione_note": "Graffio portiera"})
        with self.app.app_context():
            v = db.session.get(Veicolo, vid)
            self.assertEqual((v.sede.nome, v.fase), ("Carrozzeria B", "lavorazione"))
            self.assertTrue(v.arrivo_qui.senza_ddt)
            mid = v.arrivo_qui.id
            self.assertEqual(Ddt.query.count(), 0)
        for p in ("/", "/stock", "/lavorazioni", f"/veicolo/{vid}"):
            self.assertEqual(self.c.get(p).status_code, 200, p)
        self.assertIn("Graffio portiera", self.c.get("/lavorazioni").get_data(as_text=True))
        # il fornitore la vede in lavorazione, con il tipo, e la chiude sul posto
        self.entra("carr")
        pagina = self.c.get("/").get_data(as_text=True)
        self.assertIn("Graffio portiera", pagina)
        self.assertIn("Conferma la conclusione dei lavori", pagina)
        self.c.post(f"/movimento/{mid}/lavorazione-completata", data={"lat": "45.1", "lon": "12.1", "rientro": "1",
                                                                     "chi_riporta": str(self._id(Utente, email="autista@t.it"))})
        with self.app.app_context():
            v = db.session.get(Veicolo, vid)
            self.assertEqual(v.fase, "da_ritirare")
            rientro = v.movimento_predisposto
            self.assertEqual((rientro.da_sede.nome, rientro.a_sede.nome), ("Carrozzeria B", "Sede A"))
        self.assertEqual(self.c.get(f"/veicolo/{vid}").status_code, 200)
        # l'admin annulla il completamento dal punto giusto
        self.entra("admin")
        with self.app.app_context():
            azione = Azione.query.filter_by(veicolo_id=vid, tipo="lavorazione_completata").one()
            self.assertIn("Annulla completamento", self.c.get(f"/veicolo/{vid}").get_data(as_text=True))
        self.c.post(f"/azione/{azione.id}/ripristina")
        with self.app.app_context():
            v = db.session.get(Veicolo, vid)
            self.assertEqual(v.fase, "lavorazione")
            self.assertIsNone(v.movimento_predisposto)

    def test_la_lavorazione_la_indica_solo_la_concessionaria(self):
        vid = self.abbina()
        self.entra("autista")  # "Inizia spostamento": la lavorazione resta da indicare
        self.c.post("/profilo-guida", data={"vettore_id": self._id(Vettore, nome="Vettore X"),
                                            "mezzo": "Bisarca", "firma": FIRMA})
        self.c.post("/t/ABCD2345", data=POS)
        self.c.post(f"/veicolo/{vid}/sposta", data={"a_sede_id": self._id(Sede, nome="Carrozzeria B")})
        self.entra("carr")
        d = self.ddt()
        self.assertIn("La indica la concessionaria", self.c.get(f"/ddt/{d}").get_data(as_text=True))
        self.c.post(f"/ddt/{d}/lavorazione", data={"lavorazione": "Carrozzeria", "lavorazione_note": "x"})
        with self.app.app_context():
            self.assertEqual(self.mov(d).lavorazione, "")
        self.entra("autista")
        self.c.post(f"/ddt/{d}/lavorazione", data={"lavorazione": "Carrozzeria", "lavorazione_note": "x"})
        with self.app.app_context():
            self.assertEqual(self.mov(d).lavorazione, "Carrozzeria")

    def _finita_senza_rientro(self):
        vid, mid = self.in_carrozzeria(2)
        self.entra("carr")
        self.c.post(f"/movimento/{mid}/lavorazione-completata", data={"lat": "45.1", "lon": "12.1"})
        return vid, mid

    def test_rientro_preparato_dal_fornitore_per_se(self):
        vid, mid = self._finita_senza_rientro()
        pagina = self.c.get("/?stato=finita").get_data(as_text=True)
        self.assertIn("Chi riporta l'auto?", pagina)
        self.c.post(f"/movimento/{mid}/rientro", data={"chi_riporta": str(self._id(Utente, email="carr@t.it"))})
        with self.app.app_context():
            r = db.session.get(Veicolo, vid).movimento_predisposto
            self.assertEqual((r.da_sede.nome, r.a_sede.nome, r.conducente), ("Carrozzeria B", "Sede A", "Carr"))
            self.assertEqual(r.vettore.sede_id, self._id(Sede, nome="Carrozzeria B"))  # vettore della carrozzeria
            rid = r.ddt_id
        # presa in carico: senza posizione no; con la posizione parte e sparisce dalle completate
        self.c.post(f"/ddt/{rid}/presa-in-carico", data={"conducente": "Carr", "firma": FIRMA, "mezzo": "Bisarca"})
        with self.app.app_context():
            self.assertEqual(db.session.get(Veicolo, vid).stato, "in_sede")
        self.c.post(f"/ddt/{rid}/presa-in-carico", data={"conducente": "Carr", "firma": FIRMA, "mezzo": "Bisarca",
                                                         "lat": "45.1", "lon": "12.1"})
        with self.app.app_context():
            self.assertEqual(db.session.get(Veicolo, vid).stato, "in_viaggio")
        self.assertNotIn("Lavori conclusi", self.c.get("/?stato=finita").get_data(as_text=True))
        self.assertIn("Uscite da voi", self.c.get("/").get_data(as_text=True))
        self.entra("admin")
        self.assertNotIn("Golf", self.c.get("/lavorazioni?stato=finita").get_data(as_text=True))

    def test_rientro_preparato_per_un_dipendente(self):
        vid, mid = self._finita_senza_rientro()
        dip = self._id(Utente, email="autista@t.it")
        self.c.post(f"/movimento/{mid}/rientro", data={"chi_riporta": str(dip)})
        with self.app.app_context():
            r = db.session.get(Veicolo, vid).movimento_predisposto
            u = db.session.get(Utente, dip)
            self.assertEqual(r.conducente, u.nome)
            self.assertTrue(r.vettore.proprio or r.vettore_id == u.vettore_id)
        # un secondo rientro no
        self.c.post(f"/movimento/{mid}/rientro", data={"chi_riporta": ""})
        with self.app.app_context():
            self.assertEqual(Ddt.query.count(), 2)  # andata + rientro

    def test_lavorazioni_nel_menu_del_fornitore(self):
        self.entra("carr")
        pagina = self.c.get("/").get_data(as_text=True)
        self.assertIn("<h1>Lavorazioni</h1>", pagina)
        self.assertNotIn("Le mie auto", pagina)
        self.assertNotIn("vi affida", pagina)

    def abbina_altra(self):
        with self.app.app_context():
            db.session.add(Tag(codice="ALTRA234"))
            db.session.commit()
        r = self.c.post("/abbina/ALTRA234", data={
            "vin": "WVWZZZ1KZAW000999", "marca": "Volkswagen", "modello": "Polo", "colore": "Nero", "condizione": "nuovo",
            "versione": "1.5 eTSI Life", "anno": "2024", "alimentazione": "Benzina",
            "sede_id": self._id(Sede, nome="Sede A"), "lat": "45.66", "lon": "12.24"})
        self.assertEqual(r.status_code, 302)
        return self._id(Veicolo, vin="WVWZZZ1KZAW000999")


class TestPagineErrore(Base):
    def test_errori_in_italiano(self):
        self.entra("carr")
        r = self.c.get("/lavorazioni")  # riservata alla concessionaria
        self.assertEqual(r.status_code, 403)
        self.assertIn("Pagina non disponibile per il tuo account", r.get_data(as_text=True))
        self.assertNotIn("Forbidden", r.get_data(as_text=True))
        r = self.c.get("/non-esiste")
        self.assertEqual(r.status_code, 404)
        self.assertIn("Pagina non trovata", r.get_data(as_text=True))


class TestRitiroAssegnato(Base):
    """DDT da ritirare: si predispone da ovunque, assegnato a una persona; solo lei lo prende in carico,
    entro 200 m dal punto di partenza."""
    def test_solo_l_incaricato_e_solo_sul_posto(self):
        vid = self.abbina()
        self.entra("ufficio")
        self.assertIn("Assegna il ritiro a", self.c.get(f"/veicolo/{vid}").get_data(as_text=True))
        self.parti(vid, "Carrozzeria B", azione="predisponi", conducente="", firma="", ore_ritiro="8")
        d = self.ddt()
        with self.app.app_context():
            self.assertEqual(self.mov(d).assegnato_a.email, "autista@t.it")
        # un altro dipendente (anche l'admin) non lo prende in carico
        self.entra("admin")
        self.presa(d, data={"conducente": "Admin", "firma": FIRMA})
        with self.app.app_context():
            self.assertEqual(self.mov(d).stato, "predisposto")
        self.assertIn("Ritiro assegnato a", self.c.get(f"/ddt/{d}").get_data(as_text=True))
        # l'incaricato lontano dalla partenza (Sede A: 45.0, 12.0) no, sul posto sì
        self.entra("autista")
        self.c.post(f"/ddt/{d}/presa-in-carico", data={"conducente": "X", "firma": FIRMA, "lat": "45.66", "lon": "12.24"})
        with self.app.app_context():
            self.assertEqual(self.mov(d).stato, "predisposto")
        self.presa(d, data={"conducente": "X", "firma": FIRMA})
        with self.app.app_context():
            m = self.mov(d)
            self.assertEqual((m.stato, m.conducente), ("aperto", m.assegnato_a.nome))

    def test_admin_elimina_destinazione_senza_ddt(self):
        officina = self._id(Sede, nome="Officina D")
        carrozzeria = self._id(Sede, nome="Carrozzeria B")
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")  # la carrozzeria ha un DDT collegato
        self.assertIn("non si può eliminare", self.c.get(f"/admin/sedi/{carrozzeria}").get_data(as_text=True))
        self.c.post(f"/admin/sedi/{carrozzeria}/elimina")
        self.assertIn("Elimina Officina D", self.c.get(f"/admin/sedi/{officina}").get_data(as_text=True))
        self.c.post(f"/admin/sedi/{officina}/elimina")
        with self.app.app_context():
            self.assertIsNotNone(db.session.get(Sede, carrozzeria))
            self.assertIsNone(db.session.get(Sede, officina))


class TestQui(Base):
    """Chi è già dove si trova l'auto vede "Qui" e non "Portami qui"."""
    def test_fornitore_vede_qui_la_concessionaria_portami_qui(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        self.arrivo(self.ddt())
        self.entra("ufficio")  # in ufficio: la carrozzeria è altrove
        pagina = self.c.get(f"/veicolo/{vid}").get_data(as_text=True)
        self.assertIn("Portami qui", pagina)
        self.assertIn("info-striscia", pagina)  # dove si trova: striscia compatta (13/11/2026)
        self.entra("carr")  # l'auto è da loro
        pagina = self.c.get(f"/veicolo/{vid}").get_data(as_text=True)
        self.assertNotIn("Portami qui", pagina)
        self.assertNotIn("Dove si trova", pagina)
        self.assertIn("<b>Qui</b>", pagina)
        # la concessionaria dopo un tocco sul posto vede "Qui"
        self.entra("ufficio")
        self.c.post("/t/ABCD2345", data={"lat": "45.1", "lon": "12.1"})
        pagina = self.c.get(f"/veicolo/{vid}").get_data(as_text=True)
        self.assertNotIn("Portami qui", pagina)
        self.assertIn("vicino a te", pagina)

    def test_sezioni_del_fornitore(self):
        self.entra("carr")
        pagina = self.c.get("/").get_data(as_text=True)
        for testo in ("In arrivo da voi · 0", "Nessuna auto in viaggio verso di voi", "In lavorazione",
                      "Completate · da ritirare"):
            self.assertIn(testo, pagina)
        self.assertNotIn("Completate · da ritirare", self.c.get("/?stato=ritardo").get_data(as_text=True))


class TestControlliDellaSera(Base):
    """Viaggi in ritardo da verificare, Analisi solo all'admin, ricezione in sede solo dalla concessionaria."""
    def test_viaggio_oltre_un_ora_dall_arrivo_stimato(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        self.assertNotIn("Viaggi in ritardo", self.c.get("/").get_data(as_text=True))
        with self.app.app_context():
            m = self.mov(self.ddt())
            m.partenza_at = adesso() - timedelta(minutes=(m.stima_viaggio_min or 20) + 90)
            db.session.commit()
        pagina = self.c.get("/").get_data(as_text=True)
        self.assertIn("Viaggi in ritardo", pagina)
        self.assertIn("Da verificare", pagina)
        self.assertIn("Pronte oggi", pagina)  # 14/11/2026: al posto di "Preparazioni · oggi"

    def test_viaggio_dal_fornitore_senza_stima(self):
        """Rientro da un fornitore senza posizione sulla mappa: l'arrivo non si stima, ma dopo 3 ore è da verificare."""
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        with self.app.app_context():
            m = self.mov(self.ddt())
            m.a_sede.lat = m.a_sede.lon = None  # destinazione senza posizione: niente stima
            m.stima_viaggio_min = None
            m.partenza_at = adesso() - timedelta(hours=2)
            db.session.commit()
        self.assertNotIn("Viaggi in ritardo", self.c.get("/").get_data(as_text=True))
        with self.app.app_context():
            m = self.mov(self.ddt())
            m.partenza_at = adesso() - timedelta(hours=4)
            db.session.commit()
        pagina = self.c.get("/").get_data(as_text=True)
        self.assertIn("Viaggi in ritardo", pagina)
        self.assertIn("arrivo non stimabile", pagina)

    def test_analisi_solo_admin(self):
        self.assertEqual(self.c.get("/analisi").status_code, 200)
        self.entra("ufficio")
        self.assertEqual(self.c.get("/analisi").status_code, 403)
        self.assertNotIn("Analisi dei tempi", self.c.get("/").get_data(as_text=True))

    def test_ricezione_in_sede_la_conferma_la_concessionaria(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        self.arrivo(self.ddt())
        self.entra("carr")  # la carrozzeria la riporta lei, col suo DDT
        self.c.post("/profilo-guida", data={"vettore_id": self._id(Vettore, nome="Vettore X"),
                                            "mezzo": "Bisarca", "firma": FIRMA})
        self.c.post(f"/veicolo/{vid}/sposta", data={"a_sede_id": self._id(Sede, nome="Sede A"),
                                                     "lat": "45.1", "lon": "12.1"})
        d = self.ddt()
        with self.app.app_context():
            self.assertEqual(self.mov(d).stato, "aperto")
        self.assertIn("La ricezione la conferma", self.c.get(f"/ddt/{d}").get_data(as_text=True))
        self.arrivo(d)  # il fornitore arrivato in Sede A non chiude il DDT
        with self.app.app_context():
            self.assertEqual(self.mov(d).stato, "aperto")
        self.entra("ufficio")
        self.arrivo(d)
        with self.app.app_context():
            self.assertEqual(self.mov(d).stato, "chiuso")


class TestUniforma(Base):
    """Vetture e furgoni caricati tutti allo stesso modo: le scritture diverse si correggono da sole."""
    def test_regole_di_scrittura(self):
        from getvingo import uniforma
        self.assertEqual(uniforma.formatta("  grigio   moonstone "), "Grigio Moonstone")
        self.assertEqual(uniforma.formatta("GOLF 8 etsi"), "Golf 8 eTSI")
        self.assertEqual(uniforma.formatta("x3 xdrive"), "X3 xDrive")
        self.assertEqual(uniforma.formatta("golf 8 eTSI"), "Golf 8 eTSI")  # lettere miste: restano
        self.assertEqual(uniforma.formatta("a3 sportback tdi"), "A3 Sportback TDI")
        self.assertEqual(uniforma.formatta("cx-30"), "CX-30")
        self.assertEqual(uniforma.formatta("q4 e-tron"), "Q4 e-tron")
        for giusto in ("Serie 1 118i", "Formentor VZ", "600e La Prima", "Renegade 4xe", "Ypsilon LX", "C-HR", "Qashqai e-Power"):
            self.assertEqual(uniforma.formatta(giusto), giusto)  # nomi già scritti bene non si toccano
        with self.app.app_context():
            self.assertEqual(uniforma.valore("marca", "vw"), "Volkswagen")
            self.assertEqual(uniforma.valore("marca", "mercedes benz"), "Mercedes-Benz")
            self.assertEqual(uniforma.valore("marca", "bmw"), "BMW")

    def test_riusa_la_scrittura_gia_presente(self):
        vid = self.abbina()  # VW Golf Bianco
        base = {"vin": VIN, "condizione": "nuovo", "tipologia": "vettura", "in_stock_dal": adesso().date().isoformat(), "anno": "2024", "alimentazione": "Benzina"}
        self.c.post(f"/veicolo/{vid}/modifica", data={**base, "marca": "volkswagen", "modello": "golf",
                                                      "versione": "1.5 etsi life", "colore": "bianco PURO"})
        with self.app.app_context():
            v = db.session.get(Veicolo, vid)
            self.assertEqual((v.marca, v.modello, v.versione, v.colore), ("Volkswagen", "Golf", "1.5 eTSI Life", "Bianco Puro"))
            v2 = Veicolo(vin="WVWZZZ1KZAW000777", marca="  VOLKSWAGEN", modello="GOLF ", versione="1.5 ETSI LIFE",
                         colore="bianco  puro")
            db.session.add(v2)
            from getvingo import uniforma
            uniforma.auto(v2)
            self.assertEqual((v2.marca, v2.modello, v2.versione, v2.colore), ("Volkswagen", "Golf", "1.5 eTSI Life", "Bianco Puro"))
            self.assertEqual(v.descrizione, "Volkswagen Golf")  # il colore non è nel nome


class TestUniformaEsistenti(Base):
    """Gestione → Uniforma i dati delle auto: anteprima delle correzioni, poi si applicano col pulsante."""
    def test_anteprima_e_applica(self):
        with self.app.app_context():
            g.org_id = 1
            db.session.add_all([
                Veicolo(vin="WVWZZZ1KZAW000801", marca="VW", modello="golf", colore="bianco", sede_id=1),
                Veicolo(vin="WVWZZZ1KZAW000802", marca="Volkswagen", modello="Golf", colore="Bianco", sede_id=1),
                Veicolo(vin="WVWZZZ1KZAW000803", marca="volkswagen ", modello="GOLF", versione="1.5 etsi", colore="BIANCO", sede_id=1)])
            db.session.commit()
        pagina = self.c.get("/admin/uniforma").get_data(as_text=True)
        self.assertIn("Uniforma i dati delle auto", pagina)
        self.assertIn("“VW”", pagina)
        with self.app.app_context():
            self.assertEqual(Veicolo.query.filter_by(marca="VW").count(), 1)  # l'anteprima non cambia niente
        self.c.post("/admin/uniforma")
        with self.app.app_context():
            dati = {(v.marca, v.modello, v.colore) for v in Veicolo.query.filter(Veicolo.vin.like("%0008%"))}
            self.assertEqual(dati, {("Volkswagen", "Golf", "Bianco")})
            self.assertEqual(Veicolo.query.filter_by(vin="WVWZZZ1KZAW000803").one().versione, "1.5 eTSI")
        self.assertIn("Niente da correggere", self.c.get("/admin/uniforma").get_data(as_text=True))


class TestTelefonoSnello(Base):
    """Telefono snello (12/11/2026): il tocco mostra una cosa da fare, "scorri per confermare",
    "Annulla" per pochi secondi a chi ha appena fatto l'azione."""
    def profilo(self, chi="autista"):
        self.entra(chi)
        self.c.post("/profilo-guida", data={"vettore_id": self._id(Vettore, nome="Vettore X"),
                                            "mezzo": "Bisarca", "firma": FIRMA})

    def test_destinazioni_sotto_l_auto_e_lavorazione_scelta_alla_partenza(self):
        vid = self.abbina()
        self.profilo()
        self.c.post("/t/ABCD2345", data=POS)
        pagina = self.c.get(f"/veicolo/{vid}/tocco").get_data(as_text=True)
        for testo in ("Dove la porti?", "Carrozzeria B", "foglio-partenza", "data-scorri", "Nuova destinazione"):
            self.assertIn(testo, pagina)
        torna = f"/veicolo/{vid}/tocco"
        # lavorazione senza dettagli: non parte, si torna alla schermata del tocco
        r = self.c.post(f"/veicolo/{vid}/sposta", data={"a_sede_id": self._id(Sede, nome="Carrozzeria B"),
                                                        "lavorazione": "Carrozzeria", "torna": torna})
        self.assertEqual(r.headers["Location"], torna)
        with self.app.app_context():
            self.assertEqual(Ddt.query.count(), 0)
        self.c.post(f"/veicolo/{vid}/sposta", data={"a_sede_id": self._id(Sede, nome="Carrozzeria B"), "torna": torna,
                                                    "lavorazione": "Carrozzeria", "lavorazione_note": " paraurti  dietro "})
        d = self.ddt()
        with self.app.app_context():
            m = self.mov(d)
            self.assertEqual((m.lavorazione, m.lavorazione_note, m.causale), ("Carrozzeria", "paraurti dietro", "Invio lavorazione"))
        self.arrivo(d)  # con la lavorazione già indicata il DDT si chiude all'arrivo
        with self.app.app_context():
            self.assertEqual(self.mov(d).stato, "chiuso")

    def test_annulla_subito_solo_chi_ha_fatto_e_solo_per_pochi_secondi(self):
        vid = self.abbina()
        self.profilo()
        r = self.c.post(f"/veicolo/{vid}/sposta", data={"a_sede_id": self._id(Sede, nome="Officina D")})
        pagina = self.c.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn("annulla-subito", pagina)
        self.assertNotIn("annulla-subito", self.c.get(r.headers["Location"]).get_data(as_text=True))  # una volta sola
        with self.app.app_context():
            azione = Azione.query.filter_by(tipo="partenza").one().id
        self.entra("ufficio")  # un altro non può
        self.c.post(f"/azione/{azione}/ripristina")
        with self.app.app_context():
            self.assertIsNone(Ddt.query.one().annullato_at)
        self.entra("autista")
        self.c.post(f"/azione/{azione}/ripristina")
        with self.app.app_context():
            d, v = Ddt.query.one(), db.session.get(Veicolo, vid)
            self.assertIsNotNone(d.annullato_at)  # il DDT va tra i Cancellati, il numero non si riusa
            self.assertIn("Annullato subito da Autista", d.motivo_annullamento)
            self.assertEqual((v.stato, v.sede.nome), ("in_sede", "Sede A"))
        # passato il tempo non si annulla più
        self.c.post(f"/veicolo/{vid}/sposta", data={"a_sede_id": self._id(Sede, nome="Officina D")})
        with self.app.app_context():
            a = Azione.query.filter_by(tipo="partenza", ripristinata_at=None).one()
            a.at = a.at - timedelta(seconds=servizi.ANNULLA_SUBITO_S + 1)
            db.session.commit()
            azione = a.id
        self.c.post(f"/azione/{azione}/ripristina")
        with self.app.app_context():
            self.assertEqual(Ddt.query.filter(Ddt.annullato_at.is_(None)).count(), 1)

    def test_preparatore_il_tocco_vale_come_passo(self):
        vid = self.abbina()
        self.c.post(f"/veicolo/{vid}/preparazione")
        with self.app.app_context():
            u = Utente(nome="Prep", email="prep@t.it", ruolo="preparatore")
            u.imposta_password("pw")
            db.session.add(u)
            db.session.commit()
        fasi = lambda: [f.nome for f in Preparazione.query.one().fasi_fatte]
        self.entra("autista")  # le altre mansioni: il tocco da solo non chiude il passo
        self.c.post("/t/ABCD2345", data=POS)
        with self.app.app_context():
            self.assertEqual(fasi(), [])
        self.entra("prep")
        r = self.c.post("/t/ABCD2345", data=POS)
        self.assertIn("fatto=0", r.headers["Location"])
        self.assertIn("annulla-subito", self.c.get(r.headers["Location"]).get_data(as_text=True))
        with self.app.app_context():
            self.assertEqual(fasi(), ["Controllo rientro"])
            azione = Azione.query.filter_by(tipo="preparazione_fase").one().id
        self.c.post(f"/azione/{azione}/ripristina")  # toccato per sbaglio: si annulla subito
        with self.app.app_context():
            self.assertEqual(fasi(), [])

    def test_ritiro_assegnato_dal_tocco_con_la_firma_salvata(self):
        vid = self.abbina()
        self.entra("ufficio")
        self.parti(vid, "Carrozzeria B", azione="predisponi", conducente="", firma="", ore_ritiro="8")
        d = self.ddt()
        self.profilo()
        self.c.post("/t/ABCD2345", data={"lat": "45.0", "lon": "12.0"})  # sul posto, in Sede A
        pagina = self.c.get(f"/veicolo/{vid}/tocco").get_data(as_text=True)
        self.assertIn("Il ritiro è tuo", pagina)
        self.assertIn("Sei a 0 m da Sede A", pagina)
        self.c.post(f"/ddt/{d}/presa-in-carico", data={"firma_salvata": "1", "conducente": "Autista"})
        with self.app.app_context():
            m = self.mov(d)
            self.assertEqual((m.stato, m.firma_conducente, m.firma_salvata, m.mezzo), ("aperto", FIRMA, True, "Su strada (guidata)"))  # modalità: quella del DDT predisposto

    def test_partner_lavori_finiti_dal_tocco(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        d = self.ddt()
        self.entra("carr")
        self.arrivo(d)
        self.c.post("/t/ABCD2345", data={"lat": "45.1", "lon": "12.1"})
        pagina = self.c.get(f"/veicolo/{vid}/tocco").get_data(as_text=True)
        self.assertIn("Chi la riporta?", pagina)
        self.assertIn("Lavori finiti", pagina)
        self.c.post(f"/movimento/{self._id(Movimento, ddt_id=d)}/lavorazione-completata",
                    data={"rientro": "1", "chi_riporta": "altro", "chi_riporta_altro": str(self._id(Utente, email="autista@t.it"))})
        with self.app.app_context():
            self.assertIsNotNone(self.mov(d).lavorazione_completata_at)
            rientro = Movimento.query.filter_by(stato="predisposto").one()
            self.assertEqual(rientro.assegnato_a.email, "autista@t.it")

    def test_danno_senza_descrizione(self):
        vid = self.abbina()
        self.c.post(f"/veicolo/{vid}/danno", data={"zona": "Cofano", "foto": [(io.BytesIO(JPEG), "a.jpg")]},
                    content_type="multipart/form-data")
        with self.app.app_context():
            self.assertEqual((Danno.query.one().zona, Danno.query.one().descrizione), ("Cofano", ""))
        self.assertIn("Nessuna descrizione", self.c.get("/danno/1").get_data(as_text=True))

    def test_home_dal_telefono_cose_per_te(self):
        vid = self.abbina()
        self.entra("ufficio")
        self.parti(vid, "Carrozzeria B", azione="predisponi", conducente="", firma="", ore_ritiro="8")
        self.entra("autista")
        pagina = self.c.get("/").get_data(as_text=True)
        for testo in ("Cosa fai adesso?", "data-leggi-nfc", "Ritiro assegnato a te", "<h1>Home</h1>"):  # Panoramica → Home (14/11/2026)
            self.assertIn(testo, pagina)


class TestTelefonoEssenziale(Base):
    """Dal telefono (12/11/2026 sera): due voci di menu, pagine da computer con un avviso, ricerca, Trova l'auto."""
    def test_menu_e_pagine_da_computer(self):
        vid = self.abbina()
        self.entra("autista")
        home = self.c.get("/").get_data(as_text=True)
        self.assertIn("I tuoi lavori", home)
        self.assertIn('grid-template-columns:repeat(3, 1fr)', home)  # Home · Cerca · DDT
        for url in ("/stock", "/lavorazioni", "/preparazione", "/ddt", "/danni"):
            self.assertIn("Questa pagina si usa dal computer", self.c.get(url).get_data(as_text=True), url)
        cerca = self.c.get("/cerca?q=000123").get_data(as_text=True)
        self.assertIn("risultati-telefono", cerca)
        self.assertIn(f"/veicolo/{vid}", cerca)
        scheda = self.c.get(f"/veicolo/{vid}").get_data(as_text=True)
        self.assertIn("Trova l'auto", scheda)
        trova = self.c.get(f"/veicolo/{vid}/trova").get_data(as_text=True)
        self.assertIn("data-trova", trova)
        self.assertIn("Apri nelle mappe", trova)

    def test_cerca_per_parole_mappa_e_miei_ddt(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")  # DDT emesso dall'admin
        for q in ("Volkswagen Golf", "golf bianco", "ZZZ1KZ", "AW000123"):
            self.assertIn(f"/veicolo/{vid}", self.c.get(f"/cerca?q={q}&parziale=1").get_data(as_text=True), q)
        self.assertNotIn(f"/veicolo/{vid}", self.c.get("/cerca?q=golf+rosso&parziale=1").get_data(as_text=True))
        pagina = self.c.get("/cerca").get_data(as_text=True)
        self.assertIn("mappa-telefono", pagina)
        ddt = self.c.get("/miei-ddt").get_data(as_text=True)  # terza scheda: i DDT legati a chi guarda
        for testo in ("I tuoi DDT", "In corso · 1", "DDT 1/"):
            self.assertIn(testo, ddt)

    def test_home_fornitori_preparazioni_e_calendario(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        self.arrivo(self.ddt())
        self.entra("autista")  # mansione Consegne: solo ciò che serve (14/11/2026): ritiri e calendario
        home = self.c.get("/").get_data(as_text=True)
        self.assertNotIn("Fornitori in ritardo", home)          # quello è per l'ufficio
        self.assertIn("Quando saranno pronte", home)
        self.assertIn('"t": "lavorazione"', home)                # la fine lavori sta nel calendario
        self.entra("admin")
        self.assertIn("data-cal-tel", self.c.get("/").get_data(as_text=True))
        self.assertNotIn("cerca-veloce", home)  # la ricerca sta nella scheda Cerca
        self.assertIn('class="home-telefono tocco solo-telefono"', home)  # sul computer non si vede

    def test_admin_gestisce_gli_account_dal_telefono(self):
        pagina = self.c.get("/admin/aziende").get_data(as_text=True)
        self.assertNotIn("Questa pagina si usa dal computer", pagina)
        self.assertIn("Utenti e partner", self.c.get("/profilo").get_data(as_text=True))
        self.assertIn("Questa pagina si usa dal computer", self.c.get("/admin/sedi").get_data(as_text=True))


class TestDecodificaVin(unittest.TestCase):
    """Decodifica dal VIN (references/01_Guida_Struttura_VIN.md, 02_VIN_Knowledge_Base_AI.md): solo dati certi."""
    def test_esempi_delle_guide(self):
        from datetime import date
        from getvingo.vin import cifra_controllo, decodifica
        oggi = date(2026, 11, 13)
        d = decodifica("WVWZZZ1KZ6W000001", oggi)
        self.assertEqual((d["marca"], d["modello"], d["generazione"], d["anno_modello"], d["paese"]),
                         ("Volkswagen", "Golf", "V", 2006, "Germania"))
        d = decodifica("ZFA31200000123456", oggi)  # Fiat: progetto 312 = 500, anno non codificato
        self.assertEqual((d["marca"], d["modello"], d["anno_modello"], d["paese"]), ("Fiat", "500", None, "Italia"))
        self.assertEqual(cifra_controllo("1M8GDM9A_KP042788".replace("_", "0")), "X")
        d = decodifica("1M8GDM9AXKP042788", oggi)  # USA: posizione 7 numerica = ciclo 1980-2009
        self.assertEqual((d["anno_modello"], d["cifra_controllo"]["ok"], d["avvisi"]), (1989, True, []))
        self.assertEqual(decodifica("WAUZZZGY5RA037752", oggi)["anno_modello"], 2024)
        self.assertEqual(decodifica("ZFA25000001234567", oggi)["tipologia"], "commerciale")  # Ducato
        self.assertEqual(decodifica("WDD2050421F123456", oggi)["modello"], "Classe C")

    def test_niente_dati_inventati(self):
        from getvingo.vin import decodifica
        d = decodifica("VR3UHZKXZRJ456120")  # Peugeot: modello e anno non si ricavano in Europa
        self.assertEqual((d["marca"], d["modello"], d["anno_modello"], d["paese"]), ("Peugeot", "", None, "Francia"))
        self.assertEqual(decodifica("WF0XXXTTGXKA12345")["tipologia"], "")  # Ford fa auto e furgoni: non si indovina
        self.assertEqual(decodifica("WF0XXXTTGXKA12345")["alimentazione"], "")  # dal VIN non si sa
        self.assertEqual(decodifica("5YJ3E1EA7KF317000")["alimentazione"], "Elettrica")  # Tesla fa solo elettriche
        self.assertEqual(decodifica("XXX00000000000000")["marca"], "")

    def test_cifra_di_controllo_sbagliata_solo_proposta(self):
        from getvingo.vin import decodifica
        d = decodifica("1M8GDM9AXKP04278B")  # l'OCR ha letto B al posto di 8
        self.assertEqual(d["vin"], "1M8GDM9AXKP04278B")  # il VIN non cambia da solo
        self.assertEqual(d["proposta"], "1M8GDM9AXKP042788")
        self.assertIn("forse è 1M8GDM9AXKP042788", d["avvisi"][0])
        self.assertEqual(decodifica("WVWZZZ1KZ6W000001")["avvisi"], [])  # in Europa la posizione 9 è libera


class TestCivico(unittest.TestCase):
    def test_via_e_civico_puliti(self):
        from getvingo.anagrafica import pulisci_via
        for scritto, atteso in [("Via Roma, n. 12/a", "Via Roma 12/A"), ("via Roma n°12", "via Roma 12"),
                                ("Via Roma 12 A", "Via Roma 12/A"), ("Via Roma 12 BIS", "Via Roma 12 bis"),
                                ("Via 4 Novembre", "Via 4 Novembre"), ("Via 4 Novembre  7", "Via 4 Novembre 7"),
                                ("Via Roma s.n.c.", "Via Roma snc"), ("Via Roma 12/14", "Via Roma 12/14")]:
            self.assertEqual(pulisci_via(scritto), atteso, scritto)


class TestCompilaDalVin(Base):
    def test_api_e_salvataggio(self):
        r = self.c.get("/api/veicolo?vin=WVWZZZAUZFW123456").json
        self.assertEqual((r["marca"], r["modello"], r["tipologia"], r["anno_modello"], r["paese"]),
                         ("Volkswagen", "Golf", "vettura", 2015, "Germania"))
        self.assertEqual(r["fonti"], {"marca": "DAL VIN", "modello": "DAL VIN", "tipologia": "DAL VIN", "anno": "DAL VIN"})
        self.assertEqual(r["anno"], 2015)
        r = self.c.get("/api/veicolo?vin=1M8GDM9A0KP042788").json  # cifra di controllo sbagliata: avviso
        self.assertIn("non torna", r["avvisi"][0])
        # modello non scritto: al salvataggio quello del VIN
        self.c.post("/abbina/ABCD2345", data={"vin": "ZFA31200000654321", "condizione": "nuovo", "colore": "Rosso",
                                              "versione": "1.0 Hybrid", "anno": "2024", "alimentazione": "Benzina",
                                              "sede_id": self._id(Sede, nome="Sede A")})
        with self.app.app_context():
            v = Veicolo.query.filter_by(vin="ZFA31200000654321").one()
            self.assertEqual((v.marca, v.modello), ("Fiat", "500"))


class TestDatiAutoObbligatori(Base):
    def test_versione_anno_colore_obbligatori_e_scritti_uguali(self):
        base = {"vin": "WVWZZZAUZFW123456", "marca": "volkswagen", "modello": "GOLF", "condizione": "nuovo",
                "sede_id": self._id(Sede, nome="Sede A")}
        r = self.c.post("/abbina/ABCD2345", data=base)  # senza versione e colore non entra
        self.assertIn("manca versione, alimentazione, colore", r.get_data(as_text=True))
        with self.app.app_context():
            self.assertEqual(Veicolo.query.count(), 0)
        # anno non scelto: dal VIN (Volkswagen lo codifica: F = 2015); colore e versione scritti uguali
        self.c.post("/abbina/ABCD2345", data=dict(base, versione="1.6 tdi highline", colore="BIANCO perla", alimentazione="Diesel"))
        with self.app.app_context():
            v = Veicolo.query.filter_by(vin="WVWZZZAUZFW123456").one()
            self.assertEqual((v.marca, v.modello, v.versione, v.colore, v.anno),
                             ("Volkswagen", "Golf", "1.6 TDI Highline", "Bianco Perla", 2015))
        d = self.c.get("/api/versioni?marca=Volkswagen&modello=Golf&anno=2015").json
        self.assertEqual((d["anno"], d["altre"], d["colori"]), (["1.6 TDI Highline"], [], ["Bianco Perla"]))
        self.assertEqual(self.c.get("/api/versioni?marca=Volkswagen&modello=Golf&anno=2020").json["altre"], ["1.6 TDI Highline"])
        self.assertEqual(self.c.get("/api/formatta?campo=colore&testo=grigio NARDO").json["testo"], "Grigio Nardo")
        # un valore già presente scritto male non si ricopia: si corregge
        with self.app.app_context():
            v = Veicolo.query.one(); v.colore = "rosso corsa"; db.session.commit()
        self.assertEqual(self.c.get("/api/formatta?campo=colore&testo=ROSSO CORSA").json["testo"], "Rosso Corsa")


class TestRientroDaComputer(Base):
    def test_concessionaria_predispone_partner_crea(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        self.arrivo(self.ddt())
        with self.app.app_context():
            m = Movimento.query.filter_by(veicolo_id=vid).one()
            m.lavorazione_completata_at = adesso()
            db.session.commit()
        for url in (f"/veicolo/{vid}", "/lavorazioni"):
            pagina = self.c.get(url).get_data(as_text=True)
            self.assertNotIn("Crea DDT di rientro", pagina, url)  # vale solo sul posto: da computer no
        self.assertIn("Predisponi rientro", self.c.get("/lavorazioni").get_data(as_text=True))
        self.assertIn('id="predisponi"', self.c.get(f"/veicolo/{vid}").get_data(as_text=True))


class TestDestinazioniOrdinate(Base):
    """"Dove la porti?" dalla più probabile (13/11/2026 notte)."""
    def test_ordine_e_vedi_tutte(self):
        vid = self.abbina()  # Golf (vettura) in Sede A
        with self.app.app_context():
            db.session.add_all([Sede(nome="Sede E", tipo="propria", lat=45.5, lon=12.5),
                                Sede(nome="Allestimenti F", tipo="allestitore", lat=45.6, lon=12.6),
                                Sede(nome="Detailing G", tipo="altro", lat=45.7, lon=12.7)])
            db.session.commit()
        with self.app.app_context():
            g.utente = Utente.query.filter_by(email="admin@t.it").one()
            v = db.session.get(Veicolo, vid)
            nomi = [s.nome for s, _ in servizi.destinazioni_ordinate(v, g.utente)]
            self.assertEqual(nomi[0], "Sede E")                # prima le altre sedi della concessionaria
            self.assertEqual(nomi[-1], "Allestimenti F")       # un allestitore per una vettura: in fondo
        self.c.post("/profilo-guida", data={"vettore_id": self._id(Vettore, nome="Vettore X"), "mezzo": "Bisarca", "firma": FIRMA})
        self.c.post("/t/ABCD2345", data=POS)
        pagina = self.c.get(f"/veicolo/{vid}/tocco").get_data(as_text=True)
        self.assertIn('class="btn pieno vedi-tutte"', pagina)                 # più di 4: le altre con "Vedi tutte"
        self.assertEqual(pagina.count("data-in-piu hidden"), len(nomi) - 4)
        # dal fornitore: in testa il luogo da cui è arrivata
        self.parti(vid, "Carrozzeria B")
        self.arrivo(self.ddt())
        with self.app.app_context():
            g.utente = Utente.query.filter_by(email="admin@t.it").one()
            primo, motivo = servizi.destinazioni_ordinate(db.session.get(Veicolo, vid), g.utente)[0]
            self.assertEqual((primo.nome, motivo), ("Sede A", "da qui è arrivata"))

    def test_anagrafica_dell_auto(self):
        vid = self.abbina()
        self.c.post("/t/ABCD2345", data=POS)
        self.assertIn(f"/veicolo/{vid}/dati", self.c.get(f"/veicolo/{vid}/tocco").get_data(as_text=True))
        pagina = self.c.get(f"/veicolo/{vid}/dati").get_data(as_text=True)
        for testo in ("Telaio (VIN)", "Versione", "1.5 eTSI Life", "Alimentazione", "Benzina", "Anno", "2024", "Adesivo NFC"):
            self.assertIn(testo, pagina)


class TestCalendarioPanoramica(Base):
    def test_preparazioni_e_lavorazioni_insieme(self):
        vid = self.abbina()
        self.parti(vid, "Carrozzeria B")
        self.arrivo(self.ddt())
        home = self.c.get("/").get_data(as_text=True)
        self.assertIn("Pronte oggi", home)              # in Home solo oggi (14/11/2026)
        self.assertIn("/calendario", home)
        cal = self.c.get("/calendario").get_data(as_text=True)  # il mese intero: preparazioni e lavorazioni insieme
        self.assertIn('class="cal-voce lavorazione previsto', cal)
        self.assertIn("fine lavori", cal)


class TestFinePrevistaFornitori(unittest.TestCase):
    """Fine lavori con gli orari delle aziende esterne (14/11/2026): 8–12 e 13–17, giorni lavorativi, alla mezz'ora."""
    def test_calcolo(self):
        app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://", "SECRET_KEY": "t"})
        usa_org_1(app)
        with app.app_context():
            db.create_all()
            f = servizi.fine_lavori_prevista
            self.assertEqual(f(datetime(2026, 11, 16, 10, 10), 1), datetime(2026, 11, 17, 10, 30))  # 10:10 → arrotondata 10:30
            self.assertEqual(f(datetime(2026, 11, 14, 10, 0), 1), datetime(2026, 11, 16, 17, 0))    # arrivata sabato: lunedì
            self.assertEqual(f(datetime(2026, 11, 16, 16, 50), 2), datetime(2026, 11, 18, 17, 0))
            self.assertEqual(f(datetime(2026, 12, 23, 15, 20), 2), datetime(2026, 12, 28, 15, 30))  # Natale e Santo Stefano saltati
