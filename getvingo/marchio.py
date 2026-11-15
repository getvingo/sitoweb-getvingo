"""Marchio Getvingo (manuale Brand Identity v1.0, 15/11/2026): impronta del brand e logo.

Le forme si leggono dagli SVG ufficiali in static/marchio/ (mai ridisegnate a mano), così PDF dei DDT
e icone PNG restano identici ai file del manuale. Usato da ddt.py e da strumenti/genera-icone-marchio.py.
"""
import os
import re

ASFALTO = "#11171D"
VERDE_VIA = "#00D68F"        # segni e azioni su scuro
VERDE_VIA_SCURO = "#00A86B"  # segni su chiaro, verde dell'impronta
VERDE_TESTI = "#007A4E"      # link e testi verdi su chiaro
BIANCO_SEGNALETICA = "#F3F4F0"
RIQUADRO = "#EEF0EC"
FILO = "#C9CEC8"
GRIGIO_ASFALTO = "#5A6672"

# Impronta del brand B3.1: 5 × 5 celle, "a" asfalto, "v" il solo verde (riga 3, colonna 4), "." bianche col filo
IMPRONTA = ("a...a",
            ".aaa.",
            ".a.v.",
            ".aaa.",
            "a...a")
# griglia su un campo di 100: cella 16, passo 19, raggio 4; le bianche rientrano di 0,6 per il filo da 1,2
CELLA, PASSO, RAGGIO, FILO_SP = 16, 19, 4, 1.2

_CARTELLA = os.path.join(os.path.dirname(__file__), "static", "marchio")
LOGO_VIEWBOX = (-30, -67, 5286, 1167)  # x, y, larghezza, altezza
PUNTO = (2324, 133, 89)                # centro e raggio del punto della i
ANELLO = (2324, 133, 156, 31)          # anello attorno al punto: raggio, spessore (opacità 45%)
TRATTINI = ((4309, 727, 389, 73), (4827, 727, 389, 73))  # x, y, larghezza, altezza


def celle():
    """(riga, colonna, tipo) per le 25 celle dell'impronta."""
    return [(r, c, t) for r, riga in enumerate(IMPRONTA) for c, t in enumerate(riga)]


def _lettere():
    with open(os.path.join(_CARTELLA, "getvingo-logo-positivo.svg"), encoding="utf-8") as f:
        return re.search(r'<path d="([^"]+)" fill="#11171D"', f.read()).group(1)


def tracciati_logo():
    """Le lettere del logo come sotto-tracciati: liste di comandi ("M", x, y) / ("L", x, y) / ("Q", cx, cy, x, y)
    in coordinate assolute dell'SVG (y verso il basso). Legge M L H V Q Z, anche con coordinate ripetute."""
    gettoni = re.findall(r"[MLHVQZ]|-?\d+(?:\.\d+)?", _lettere())
    tracciati, attuale, x, y, cmd, i = [], [], 0.0, 0.0, None, 0
    while i < len(gettoni):
        g = gettoni[i]
        if g in "MLHVQZ":
            cmd, i = g, i + 1
            if cmd == "Z":
                if attuale:
                    tracciati.append(attuale)
                attuale = []
            continue
        n = lambda k: float(gettoni[i + k])  # noqa: E731
        if cmd == "M":
            if attuale:
                tracciati.append(attuale)
            x, y = n(0), n(1)
            attuale = [("M", x, y)]
            i += 2
            cmd = "L"  # coppie dopo M = linee
        elif cmd == "L":
            x, y = n(0), n(1)
            attuale.append(("L", x, y))
            i += 2
        elif cmd == "H":
            x = n(0)
            attuale.append(("L", x, y))
            i += 1
        elif cmd == "V":
            y = n(0)
            attuale.append(("L", x, y))
            i += 1
        elif cmd == "Q":
            attuale.append(("Q", n(0), n(1), n(2), n(3)))
            x, y = n(2), n(3)
            i += 4
    if attuale:
        tracciati.append(attuale)
    return tracciati


def punti(tracciato, passi=8):
    """Un sotto-tracciato come poligono (curve spezzate in `passi` segmenti), per disegnarlo a pixel."""
    out, x0, y0 = [], 0.0, 0.0
    for c in tracciato:
        if c[0] in "ML":
            x0, y0 = c[1], c[2]
            out.append((x0, y0))
        else:
            _, cx, cy, x, y = c
            for k in range(1, passi + 1):
                t = k / passi
                out.append(((1 - t) ** 2 * x0 + 2 * (1 - t) * t * cx + t * t * x,
                            (1 - t) ** 2 * y0 + 2 * (1 - t) * t * cy + t * t * y))
            x0, y0 = x, y
    return out


# --- PDF (reportlab) ---------------------------------------------------------------------------------

def impronta_pdf(c, x, y, lato, nero=False):
    """Impronta del brand in un quadrato di `lato` punti con l'angolo in basso a sinistra in (x, y)."""
    from reportlab.lib.colors import HexColor
    u = lato / 100
    scuro = HexColor("#000000" if nero else ASFALTO)
    for r, col, t in celle():
        cx, cy = x + (4 + col * PASSO) * u, y + lato - (4 + r * PASSO + CELLA) * u
        if t == ".":
            c.setFillColor(HexColor("#FFFFFF"))
            c.setStrokeColor(scuro if nero else HexColor(FILO))
            c.setLineWidth(FILO_SP * u)
            m = FILO_SP / 2 * u
            c.roundRect(cx + m, cy + m, CELLA * u - 2 * m, CELLA * u - 2 * m, (RAGGIO - .4) * u, stroke=1, fill=1)
        else:
            c.setFillColor(scuro if (t == "a" or nero) else HexColor(VERDE_VIA_SCURO))
            c.roundRect(cx, cy, CELLA * u, CELLA * u, RAGGIO * u, stroke=0, fill=1)


def logo_pdf(c, x, y, altezza, nero=False):
    """Logo positivo (lettere asfalto, punto e trattini verde via scuro) alto `altezza` punti,
    angolo in basso a sinistra in (x, y). Restituisce la larghezza disegnata."""
    from reportlab.lib.colors import HexColor
    vx, vy, vw, vh = LOGO_VIEWBOX
    s = altezza / vh

    def P(px, py):
        return x + (px - vx) * s, y + altezza - (py - vy) * s

    c.saveState()
    c.setFillColor(HexColor("#000000" if nero else ASFALTO))
    p = c.beginPath()
    for tr in tracciati_logo():
        x0 = y0 = 0
        for cmd in tr:
            if cmd[0] == "M":
                p.moveTo(*P(cmd[1], cmd[2]))
                x0, y0 = cmd[1], cmd[2]
            elif cmd[0] == "L":
                p.lineTo(*P(cmd[1], cmd[2]))
                x0, y0 = cmd[1], cmd[2]
            else:  # quadratica → cubica
                _, qx, qy, ex, ey = cmd
                c1 = (x0 + 2 / 3 * (qx - x0), y0 + 2 / 3 * (qy - y0))
                c2 = (ex + 2 / 3 * (qx - ex), ey + 2 / 3 * (qy - ey))
                p.curveTo(*P(*c1), *P(*c2), *P(ex, ey))
                x0, y0 = ex, ey
        p.close()
    c.drawPath(p, stroke=0, fill=1, fillMode=1)  # FILL_NON_ZERO, come l SVG
    verde = HexColor("#000000" if nero else VERDE_VIA_SCURO)
    cx, cy, r, sp = ANELLO
    c.setStrokeColor(verde)
    c.setStrokeAlpha(.45)
    c.setLineWidth(sp * s)
    c.circle(*P(cx, cy), r * s, stroke=1, fill=0)
    c.setStrokeAlpha(1)
    c.setFillColor(verde)
    c.circle(*P(PUNTO[0], PUNTO[1]), PUNTO[2] * s, stroke=0, fill=1)
    for tx, ty, tw, th in TRATTINI:
        bx, by = P(tx, ty + th)
        c.rect(bx, by, tw * s, th * s, stroke=0, fill=1)
    c.restoreState()
    return vw * s
