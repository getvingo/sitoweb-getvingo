"""PDF del DDT (contenuti minimi DPR 472/1996)."""
import base64
from io import BytesIO

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfgen import canvas

from . import servizi

INCHIOSTRO = HexColor("#1C1533")
NOTTE = HexColor("#2C1A5C")
BLU = HexColor("#1F6FB2")
ARANCIO = HexColor("#F28C1E")
TENUE = HexColor("#5A5470")
BORDO = HexColor("#DFDBE9")
FONDO = HexColor("#F6F5FA")
ROSSO = HexColor("#B42318")
# stessi colori di stato del portale (style.css, --fase)
STATO = {"predisposto": ("Da ritirare", HexColor("#DB2777")), "aperto": ("In viaggio", HexColor("#7C3AED")),
         "chiuso": ("Consegnato", HexColor("#16A34A")), "annullato": ("Annullato", ROSSO)}


def _righe(testi, font, corpo, larghezza):
    """Testi a capo dentro la larghezza data (mai troncati)."""
    out = []
    for t in testi:
        if t:
            out += simpleSplit(str(t), font, corpo, larghezza) or [""]
    return out


def _scheda(c, x, y, w, titolo, righe, altezza=None, accento=BLU):
    """Riquadro su fondo chiaro: etichetta blu, prima riga in grassetto, il resto normale.
    Ritorna l'altezza usata (o quella imposta, per affiancare due schede uguali)."""
    corpo = _contenuto(righe, w)
    h = altezza or _altezza(corpo)
    c.setFillColor(FONDO)
    c.roundRect(x, y - h, w, h, 2 * mm, stroke=0, fill=1)
    c.setFillColor(accento)
    c.setFont("Helvetica-Bold", 6.8)
    c.drawString(x + 4 * mm, y - 5.5 * mm, titolo.upper(), charSpace=0.6)
    ty = y - 10.5 * mm
    for font, corpo_pt, colore, testo in corpo:
        c.setFillColor(colore)
        c.setFont(font, corpo_pt)
        c.drawString(x + 4 * mm, ty, testo)
        ty -= 4.4 * mm
    return h


def _contenuto(righe, w):
    out = []
    for i, r in enumerate(x for x in righe if x):
        font, pt, col = ("Helvetica-Bold", 9.8, INCHIOSTRO) if i == 0 else ("Helvetica", 8.8, TENUE)
        out += [(font, pt, col, t) for t in _righe([r], font, pt, w - 8 * mm)]
    return out


def _altezza(corpo):
    return 13 * mm + max(len(corpo) - 1, 0) * 4.4 * mm


def _altezza_kv(coppie, w):
    righe = sum(len(_righe([v or "-"], "Helvetica", 8.8, w - 34 * mm)) for _, v in coppie)
    return 10 * mm + righe * 4.6 * mm + 1 * mm


def _scheda_kv(c, x, y, w, titolo, coppie, altezza):
    """Riquadro con righe "etichetta  valore": si legge in colonna, senza frasi."""
    c.setFillColor(FONDO)
    c.roundRect(x, y - altezza, w, altezza, 2 * mm, stroke=0, fill=1)
    c.setFillColor(BLU)
    c.setFont("Helvetica-Bold", 6.8)
    c.drawString(x + 4 * mm, y - 5.5 * mm, titolo.upper(), charSpace=0.6)
    ty = y - 10.5 * mm
    for i, (etichetta, valore) in enumerate(coppie):
        c.setFillColor(TENUE)
        c.setFont("Helvetica", 7.8)
        c.drawString(x + 4 * mm, ty, etichetta)
        for k, t in enumerate(_righe([valore or "-"], "Helvetica", 8.8, w - 34 * mm)):
            c.setFillColor(INCHIOSTRO)
            c.setFont("Helvetica-Bold" if i == 0 else "Helvetica", 8.8)
            c.drawString(x + 30 * mm, ty, t)
            ty -= 4.6 * mm


def _coppia(c, sx, dx, y, meta, a, b):
    """Due schede affiancate della stessa altezza."""
    h = max(_altezza(_contenuto(a[1], meta)), _altezza(_contenuto(b[1], meta)))
    _scheda(c, sx, y, meta, *a, altezza=h)
    _scheda(c, dx, y, meta, *b, altezza=h)
    return h


def _marchio(c, x, y, s):
    """Pittogramma Getvingo "G a ruota": pneumatico, barra e asterisco del VIN in un unico
    tratto viola notte (stesso disegno di brand-getvingo/getvingo-pittogramma.svg, griglia 48)."""
    u = s / 48

    def P(px, py):  # coordinate della griglia SVG (y verso il basso) -> pagina
        return x + px * u, y + s - py * u

    c.setStrokeColor(NOTTE)
    c.setLineWidth(4.5 * u)
    c.setLineCap(1)
    c.setLineJoin(1)
    p = c.beginPath()
    p.moveTo(*P(14.5, 24))
    p.lineTo(*P(42, 24))
    p.arcTo(*P(6, 42), *P(42, 6), startAng=0, extent=-320)  # pneumatico r 18, apertura 40°
    c.drawPath(p, stroke=1, fill=0)
    for (x1, y1), (x2, y2) in (((19.25, 15.77), (28.75, 32.23)), ((28.75, 15.77), (19.25, 32.23))):
        c.line(*P(x1, y1), *P(x2, y2))


def _scritta(c, x_destra, y, corpo):
    """Scritta "getVINgo" allineata a destra: get viola notte, VIN blu, go arancio."""
    font = "Helvetica-BoldOblique"
    parti = (("get", NOTTE), ("VIN", BLU), ("go", ARANCIO))
    x = x_destra - sum(c.stringWidth(t, font, corpo) for t, _ in parti)
    c.setFont(font, corpo)
    for t, colore in parti:
        c.setFillColor(colore)
        c.drawString(x, y, t)
        x += c.stringWidth(t, font, corpo)


def _firma(c, x, y, w, h, dato):
    """Disegna una firma PNG (data URL) dentro il riquadro, mantenendo le proporzioni."""
    if not dato:
        return
    try:
        img = ImageReader(BytesIO(base64.b64decode(dato.split(",", 1)[1])))
    except Exception:
        return
    c.drawImage(img, x, y, w, h, mask="auto", preserveAspectRatio=True, anchor="c")


def _vin(c, x, y, vin, corpo=10):
    """VIN tutto attaccato, ultime 8 cifre in grassetto: si leggono a colpo d'occhio."""
    vin = vin or ""
    for testo, forte in ((vin[:9], False), (vin[9:], True)):
        font = "Courier-Bold" if forte else "Courier"
        c.setFont(font, corpo)
        c.setFillColor(INCHIOSTRO if forte else TENUE)
        c.drawString(x, y, testo)
        x += c.stringWidth(testo, font, corpo)


def pdf(ddt, invariante=False):
    """invariante: stesso DDT -> stessi byte (niente data di creazione nel file), per l'archivio."""
    m = ddt.movimento
    v = m.veicolo
    (nome_m, indirizzo_m, piva_m), destinatario, vettore = servizi.dati_ddt(ddt)
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4, invariant=invariante)
    c.setTitle(f"DDT {ddt.sigla}")
    c.setAuthor(nome_m)
    W, H = A4
    sx, larg = 15 * mm, W - 30 * mm
    meta = larg / 2 - 2 * mm
    dx = sx + larg / 2 + 2 * mm
    spazio = 4 * mm
    pagine = [1]

    def fine_pagina():
        if ddt.annullato_at:
            c.saveState()
            c.setFillColor(ROSSO)
            c.setFont("Helvetica-Bold", 72)
            c.translate(W / 2, H / 2)
            c.rotate(30)
            c.setFillAlpha(0.12)
            c.drawCentredString(0, 0, "ANNULLATO")
            c.restoreState()

        # Piede (11/11/2026): niente dati interni (chi l'ha emesso, modifiche, n. movimento) — solo la pagina
        c.setFillColor(TENUE)
        c.setFont("Helvetica", 7)
        c.drawRightString(sx + larg, 9 * mm, f"DDT {ddt.sigla}  ·  pagina {pagine[0]}")
        c.showPage()

    def nuova_pagina():
        """Chiude la pagina e ne apre una nuova con una testata ridotta; ritorna la y di partenza."""
        fine_pagina()
        pagine[0] += 1
        c.setFillColor(NOTTE)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(sx, H - 18 * mm, f"Documento di trasporto N. {ddt.sigla} del {ddt.emesso_at:%d/%m/%Y} (segue)")
        return H - 24 * mm

    # Testata: marchio e titolo a sinistra, numero e data in evidenza a destra
    y = H - 14 * mm
    _marchio(c, sx, y - 12 * mm, 12 * mm)
    c.setFillColor(NOTTE)
    c.setFont("Helvetica-Bold", 17)
    c.drawString(sx + 16 * mm, y - 6 * mm, "Documento di trasporto")
    c.setFillColor(TENUE)
    c.setFont("Helvetica", 8.5)
    c.drawString(sx + 16 * mm, y - 11 * mm, "D.D.T. ai sensi del D.P.R. 472/1996")

    bw, bh = 54 * mm, 17 * mm
    bx = sx + larg - bw
    c.setFillColor(NOTTE)
    c.roundRect(bx, y - bh + 2 * mm, bw, bh, 2 * mm, stroke=0, fill=1)
    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica", 7)
    c.drawString(bx + 4 * mm, y - 3 * mm, "NUMERO", charSpace=0.6)
    c.drawRightString(bx + bw - 4 * mm, y - 3 * mm, "DATA")
    c.setFont("Helvetica-Bold", 15)
    c.drawString(bx + 4 * mm, y - 10 * mm, ddt.sigla)
    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(bx + bw - 4 * mm, y - 10 * mm, f"{ddt.emesso_at:%d/%m/%Y}")

    y -= 21 * mm
    # Tratta: da dove a dove, e quando parte (il dato che si cerca per primo)
    c.setFillColor(FONDO)
    c.roundRect(sx, y - 14 * mm, larg, 14 * mm, 2 * mm, stroke=0, fill=1)
    c.setFillColor(NOTTE)
    c.rect(sx, y - 14 * mm, 1.2 * mm, 14 * mm, stroke=0, fill=1)
    c.setFillColor(TENUE)
    c.setFont("Helvetica-Bold", 6.8)
    c.drawString(sx + 5 * mm, y - 5 * mm, "TRATTA", charSpace=0.6)
    partenza = m.da_sede.nome if m.da_sede else "-"
    arrivo = m.sede_arrivo_effettiva.nome
    c.setFillColor(INCHIOSTRO)
    c.setFont("Helvetica-Bold", 11.5)
    testo_tratta = f"{partenza}  →  {arrivo}"
    c.drawString(sx + 5 * mm, y - 10.5 * mm, _righe([testo_tratta], "Helvetica-Bold", 11.5, larg - 75 * mm)[0])
    c.setFillColor(TENUE)
    c.setFont("Helvetica-Bold", 6.8)
    c.drawRightString(sx + larg - 5 * mm, y - 5 * mm, "INIZIO TRASPORTO")
    c.setFillColor(INCHIOSTRO)
    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(sx + larg - 5 * mm, y - 10.5 * mm,
                      f"{m.partenza_at:%d/%m/%Y · ore %H:%M}" if m.partenza_at
                      else f"al ritiro, entro {m.ritiro_entro_at:%d/%m/%Y %H:%M}")
    y -= 14 * mm + spazio

    if ddt.annullato_at:
        h = _scheda(c, sx, y, larg, "Documento annullato", [
            f"Annullato il {ddt.annullato_at:%d/%m/%Y alle %H:%M} da {ddt.annullato_da.nome}",
            f"Motivo: {ddt.motivo_annullamento}" if ddt.motivo_annullamento else ""], accento=ROSSO)
        c.setFillColor(ROSSO)
        c.rect(sx, y - h, 1.2 * mm, h, stroke=0, fill=1)
        y -= h + spazio

    y -= _coppia(c, sx, dx, y, meta,
                 ("Mittente", [nome_m, indirizzo_m, f"P.IVA {piva_m}" if piva_m else ""]),
                 ("Destinatario e luogo di destinazione",
                  [destinatario[0], destinatario[1], f"P.IVA {destinatario[2]}" if destinatario[2] else ""])) + spazio
    da_ritirare = m.stato == "predisposto"
    vettore_kv = [kv for kv in (("Vettore", vettore[0]), ("Indirizzo", vettore[1]), ("P.IVA", vettore[2])) if kv[1]]
    vettore_kv.append(("Responsabile" if ddt.multiplo else "Conducente",
                       m.conducente or ("da indicare al ritiro" if da_ritirare else "-")))
    trasporto_kv = [("Causale", m.causale), ("Modalità", m.mezzo or ("da indicare al ritiro" if da_ritirare else "-")),
                    ("A cura del", "vettore")]
    h = max(_altezza_kv(vettore_kv, meta), _altezza_kv(trasporto_kv, meta))
    _scheda_kv(c, sx, y, meta, "Vettore e conducente", vettore_kv, h)
    _scheda_kv(c, dx, y, meta, "Trasporto", trasporto_kv, h)
    y -= h + spazio

    # Beni trasportati: tabella, una riga per auto (su più pagine se le auto sono tante)
    movimenti = ddt.movimenti or [m]
    colonne = [("Q.tà", 0), ("Descrizione", 14 * mm), ("Condizione", 76 * mm), ("Telaio (VIN)", 101 * mm), ("Targa", 154 * mm)]

    def testata_tabella(y):
        c.setFillColor(BLU)
        c.setFont("Helvetica-Bold", 6.8)
        titolo = f"BENI TRASPORTATI · {len(movimenti)} AUTOVEICOLI" if len(movimenti) > 1 else "BENI TRASPORTATI"
        c.drawString(sx, y - 3 * mm, titolo, charSpace=0.6)
        y -= 6 * mm
        c.setFillColor(FONDO)
        c.rect(sx, y - 6.5 * mm, larg, 6.5 * mm, stroke=0, fill=1)
        c.setFillColor(TENUE)
        c.setFont("Helvetica-Bold", 7.5)
        for nome, ox in colonne:
            c.drawString(sx + 3 * mm + ox, y - 4.3 * mm, nome)
        return y - 6.5 * mm

    y = testata_tabella(y)
    for n, mv in enumerate(movimenti):
        auto = mv.veicolo
        descr = _righe([" ".join(p for p in (auto.marca, auto.modello) if p) or "Autoveicolo"],
                       "Helvetica-Bold", 9.5, 58 * mm)
        dettagli = _righe([" · ".join(p for p in (
            auto.colore, "Veicolo commerciale" if auto.tipologia == "commerciale" else "Autovettura") if p)],
            "Helvetica", 8, 58 * mm)
        riga_h = (len(descr) + len(dettagli)) * 4.2 * mm + 4 * mm
        if y - riga_h < 30 * mm:  # spazio finito: la tabella continua sulla pagina dopo
            y = nuova_pagina()
            y = testata_tabella(y)
        if n:
            c.setStrokeColor(BORDO)
            c.setLineWidth(0.4)
            c.line(sx, y, sx + larg, y)
        ty = y - 5 * mm
        c.setFillColor(INCHIOSTRO)
        c.setFont("Helvetica-Bold", 9.5)
        c.drawString(sx + 3 * mm, ty, "1")
        for i, t in enumerate(descr + dettagli):
            c.setFont("Helvetica-Bold" if i < len(descr) else "Helvetica", 9.5 if i < len(descr) else 8)
            c.setFillColor(INCHIOSTRO if i < len(descr) else TENUE)
            c.drawString(sx + 17 * mm, ty - i * 4.2 * mm, t)
        c.setFillColor(INCHIOSTRO)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(sx + 79 * mm, ty, {"nuovo": "Nuovo", "usato": "Usato"}.get(auto.condizione or "", "-"))
        _vin(c, sx + 104 * mm, ty, auto.vin, 9.5)
        c.setFillColor(INCHIOSTRO)
        c.setFont("Helvetica-Bold" if auto.targa else "Helvetica", 9.5)
        c.drawString(sx + 157 * mm, ty, auto.targa or "-")
        y -= riga_h
    c.setStrokeColor(BORDO)
    c.setLineWidth(0.6)
    c.line(sx, y, sx + larg, y)
    c.setFillColor(TENUE)
    c.setFont("Helvetica", 7.5)
    totale = f"Totale colli: {len(movimenti)}  ·  " if len(movimenti) > 1 else ""
    condizioni = {a.veicolo.condizione for a in movimenti} - {"", None}
    aspetto = {"nuovo": "nuovo", "usato": "usato"}.get(condizioni.pop(), "nuovo/usato") if len(condizioni) == 1 else "nuovo/usato"
    c.drawString(sx + 3 * mm, y - 4 * mm, f"{totale}Aspetto esteriore dei beni: autoveicolo {aspetto}, "
                                          f"{(m.mezzo or 'in movimento su strada').lower()}")
    y -= 7 * mm + spazio

    # Lavorazione richiesta e, a consegna avvenuta, la ricezione (con la nota del destinatario)
    lavoro = [m.lavorazione or "Nessuna lavorazione indicata", m.lavorazione_note]
    if m.stato == "chiuso":
        consegna = [f"Ricevuto il {m.arrivo_at:%d/%m/%Y alle %H:%M}",
                    f"Luogo di arrivo: {m.arrivo_sede.nome}" if m.arrivo_sede and m.arrivo_sede != m.sede_arrivo_effettiva else "",
                    f"Note: {m.note_arrivo}" if m.note_arrivo else ""]
        y -= _coppia(c, sx, dx, y, meta, ("Lavorazione richiesta a destinazione", lavoro),
                     ("Consegna", consegna)) + spazio
    else:
        y -= _scheda(c, sx, y, larg, "Lavorazione richiesta a destinazione", lavoro) + spazio

    # Firme: in fondo alla pagina (o su una pagina nuova se il contenuto è arrivato troppo in basso)
    if y - 6 * mm < 52 * mm:
        y = nuova_pagina()
    fy = min(y - 6 * mm, 58 * mm)
    if y - fy > 34 * mm:  # spazio libero: riquadro per le annotazioni scritte a mano alla consegna
        ah = min(36 * mm, y - fy - 10 * mm)
        c.setStrokeColor(BORDO)
        c.setLineWidth(0.8)
        c.roundRect(sx, fy + 6 * mm, larg, ah, 2 * mm, stroke=1, fill=0)
        c.setFillColor(BLU)
        c.setFont("Helvetica-Bold", 6.8)
        c.drawString(sx + 4 * mm, fy + 6 * mm + ah - 5.5 * mm, "ANNOTAZIONI ALLA CONSEGNA", charSpace=0.6)
        c.setDash(1, 2)
        riga = fy + 6 * mm + ah - 13 * mm
        while riga > fy + 10 * mm:
            c.line(sx + 4 * mm, riga, sx + larg - 4 * mm, riga)
            riga -= 8 * mm
        c.setDash()
    terzi = larg / 3
    firme = [
        ("Firma mittente", ddt.firma_mittente, nome_m, f"{ddt.emesso_at:%d/%m/%Y %H:%M}"),
        ("Firma responsabile" if ddt.multiplo else "Firma conducente", m.firma_conducente, m.conducente or "",
         f"{m.partenza_at:%d/%m/%Y %H:%M}" if m.partenza_at else ""),
        ("Firma destinatario", m.firma_destinatario, destinatario[0],
         f"{m.arrivo_at:%d/%m/%Y %H:%M}" if m.arrivo_at else ""),
    ]
    for i, (etichetta, dato, chi, quando) in enumerate(firme):
        x = sx + i * terzi + (0 if i == 0 else 2 * mm)
        w = terzi - 2 * mm
        c.setStrokeColor(BORDO)
        c.setLineWidth(0.8)
        c.roundRect(x, fy - 30 * mm, w, 30 * mm, 2 * mm, stroke=1, fill=0)
        c.setFillColor(BLU)
        c.setFont("Helvetica-Bold", 6.8)
        c.drawString(x + 3 * mm, fy - 4.5 * mm, etichetta.upper(), charSpace=0.6)
        _firma(c, x + 3 * mm, fy - 21 * mm, w - 6 * mm, 14 * mm, dato)
        c.line(x + 3 * mm, fy - 22 * mm, x + w - 3 * mm, fy - 22 * mm)
        c.setFillColor(INCHIOSTRO)
        c.setFont("Helvetica-Bold", 7.5)
        c.drawString(x + 3 * mm, fy - 25.5 * mm, _righe([chi], "Helvetica-Bold", 7.5, w - 6 * mm)[0] if chi else "")
        c.setFillColor(TENUE)
        c.setFont("Helvetica", 7)
        c.drawString(x + 3 * mm, fy - 28.5 * mm, quando or "In attesa")

    fine_pagina()
    c.save()
    return buf.getvalue()
