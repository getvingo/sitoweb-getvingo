"""Impronte dei beni (manuale del marchio v1.0, sezione 05): ogni auto riceve un'impronta sua, diversa da tutte le altre.

Regole del manuale: 25 celle in tre stati (bianca, scura, una sola verde), 10–18 celle scure; si assegnano in
sequenza come le targhe e si mescolano con una regola fissa perché non sembrino in ordine (calcolarle dal VIN
darebbe doppioni). Esclusioni: l'impronta del brand e le versioni precedenti, quelle che ne differiscono per una
sola cella, righe o colonne tutte bianche, disegni che si leggono come lettere o numeri.

Strategia (15/11/2026):
- `Veicolo.impronta` = numero d'ordine N (0, 1, 2…), unico su tutto il portale (indice unico nel database).
- All'ingresso in stock l'auto prende il **numero libero più basso** che dà un'impronta valida: senza
  eliminazioni i numeri sono consecutivi nell'ordine di caricamento; un'impronta torna libera solo se la sua
  auto viene eliminata (le auto consegnate la tengono per sempre).
- Il disegno si ricava dal numero con una permutazione fissa (Feistel a 4 giri su 30 bit, chiave fissa,
  "cycle walking" dentro le 419.430.400 combinazioni con un verde): numeri diversi danno sempre disegni diversi,
  e lo stesso numero dà sempre lo stesso disegno. Cambiare CHIAVE cambierebbe tutte le impronte: non toccarla.
"""
import hashlib

from markupsafe import Markup

from . import marchio as M

CHIAVE = b"getvingo-impronte-beni-v1"
TOTALE = 25 * 2 ** 24  # un verde in 25 posizioni, le altre 24 piene o vuote
BIT, META = 30, 15

# disegni da cui stare lontani: brand B3.1 e versione precedente (verde al centro, anello chiuso)
BRAND = "".join(M.IMPRONTA)
PRECEDENTE = "a...a" ".aaa." ".ava." ".aaa." "a...a"

# lettere e cifre in 5×5 ("#" = cella piena): un'impronta uguale o a una cella di distanza si leggerebbe come testo
_GLIFI = """
.###. #...# #...# ##### #...# #...# #...#
####. #...# ####. #...# #...# ####. ####.
.#### #.... #.... #.... #.... #.... .####
####. #...# #...# #...# #...# #...# ####.
##### #.... ####. #.... #.... #.... #####
##### #.... ####. #.... #.... #.... #....
.#### #.... #.##. #...# #...# #...# .###.
#...# #...# ##### #...# #...# #...# #...#
##### ..#.. ..#.. ..#.. ..#.. ..#.. #####
..### ...#. ...#. ...#. #..#. #..#. .##..
#...# #..#. ###.. #..#. #...# #...# #...#
#.... #.... #.... #.... #.... #.... #####
#...# ##.## #.#.# #...# #...# #...# #...#
#...# ##..# #.#.# #..## #...# #...# #...#
.###. #...# #...# #...# #...# #...# .###.
####. #...# ####. #.... #.... #.... #....
.###. #...# #...# #.#.# #..#. .##.# .....
####. #...# ####. #..#. #...# #...# #...#
.#### #.... .###. ....# ....# ....# ####.
##### ..#.. ..#.. ..#.. ..#.. ..#.. ..#..
#...# #...# #...# #...# #...# #...# .###.
#...# #...# #...# #...# .#.#. .#.#. ..#..
#...# #...# #...# #.#.# #.#.# ##.## #...#
#...# .#.#. ..#.. ..#.. .#.#. #...# #...#
#...# .#.#. ..#.. ..#.. ..#.. ..#.. ..#..
##### ...#. ..#.. .#... #.... #.... #####
.###. #..## #.#.# ##..# #...# #...# .###.
..#.. .##.. ..#.. ..#.. ..#.. ..#.. .###.
.###. #...# ...#. ..#.. .#... #.... #####
####. ....# .###. ....# ....# ....# ####.
#...# #...# #...# ##### ....# ....# ....#
##### #.... ####. ....# ....# #...# .###.
.###. #.... ####. #...# #...# #...# .###.
##### ....# ...#. ..#.. ..#.. ..#.. ..#..
.###. #...# .###. #...# #...# #...# .###.
.###. #...# #...# .#### ....# ....# .###.
"""


def _glifi():
    """Ogni carattere è scritto su 7 righe: si tengono le 5 combinazioni di 5 righe consecutive."""
    out = set()
    for riga in _GLIFI.strip().splitlines():
        r = riga.split()
        for i in range(len(r) - 4):
            out.add("".join(r[i:i + 5]))
    return out


GLIFI = _glifi()


def _f(r, giro):
    h = hashlib.sha256(CHIAVE + bytes([giro]) + r.to_bytes(4, "big")).digest()
    return int.from_bytes(h[:4], "big") & ((1 << META) - 1)


def _feistel(x):
    s, d = x >> META, x & ((1 << META) - 1)
    for giro in range(4):
        s, d = d, s ^ _f(d, giro)
    return (s << META) | d


def combinazione(n):
    """Numero d'ordine → combinazione in [0, TOTALE): permutazione fissa (stesso n, stessa combinazione)."""
    x = _feistel(n)
    while x >= TOTALE:  # cycle walking: resta una permutazione dentro TOTALE
        x = _feistel(x)
    return x


def celle(n):
    """Le 25 celle (stringa di "a" scura, "v" verde, "." bianca, per righe) dell'impronta numero n."""
    p = combinazione(n)
    verde, bit = p % 25, p // 25
    out, k = [], 0
    for i in range(25):
        if i == verde:
            out.append("v")
        else:
            out.append("a" if (bit >> k) & 1 else ".")
            k += 1
    return "".join(out)


def _distanza(a, b):
    return sum(x != y for x, y in zip(a, b))


def valida(c):
    """Le regole del manuale (sezione 05) per un'impronta assegnabile."""
    if not 10 <= c.count("a") <= 18:
        return False
    righe = [c[i * 5:i * 5 + 5] for i in range(5)]
    if any(r == "....." for r in righe) or any(all(r[j] == "." for r in righe) for j in range(5)):
        return False
    if _distanza(c, BRAND) <= 1 or _distanza(c, PRECEDENTE) <= 1:
        return False
    piena = c.replace("v", "a").replace("a", "#")
    return not any(_distanza(piena, g) <= 1 for g in GLIFI)


def liberi(usati):
    """I numeri liberi con un'impronta valida, dal più basso (per assegnarne molti in una volta)."""
    n = 0
    while True:
        if n not in usati and valida(celle(n)):
            yield n
        n += 1


def prossimo(usati):
    """Il numero libero più basso con un'impronta valida."""
    return next(liberi(usati))


def numeri_usati():
    """Le impronte già assegnate su tutto il portale (tutte le organizzazioni)."""
    from .models import Veicolo, db
    righe = db.session.query(Veicolo.impronta).filter(Veicolo.impronta.isnot(None)) \
        .execution_options(tutte_le_organizzazioni=True).all()
    return {r[0] for r in righe}


def assegna_mancanti():
    """Dà l'impronta alle auto che non ce l'hanno, nell'ordine di caricamento (creato_at, id). Ritorna quante."""
    from .models import Veicolo, db
    da_fare = Veicolo.query.filter(Veicolo.impronta.is_(None)).order_by(Veicolo.creato_at, Veicolo.id) \
        .execution_options(tutte_le_organizzazioni=True).all()
    if not da_fare:
        return 0
    gen = liberi(numeri_usati())
    for v in da_fare:
        v.impronta = next(gen)
    db.session.commit()
    return len(da_fare)


def svg(n, lato=40, riquadro=False, titolo=""):
    """L'impronta come SVG in linea (campo 100 come gli SVG del marchio; nel riquadro chiaro per i fondi scuri)."""
    if n is None:
        return Markup("")
    vb = "-14 -14 128 128" if riquadro else "0 0 100 100"
    parti = [f'<svg class="impronta" xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" width="{lato}" height="{lato}"'
             f' role="img" aria-label="Impronta Getvingo n. {n + 1}">']
    if titolo:
        parti.append(f"<title>{Markup.escape(titolo)}</title>")
    if riquadro:
        parti.append(f'<rect x="-14" y="-14" width="128" height="128" rx="28" fill="{M.RIQUADRO}"/>')
    for i, t in enumerate(celle(n)):
        r, c = divmod(i, 5)
        x, y = 4 + c * M.PASSO, 4 + r * M.PASSO
        if t == ".":
            parti.append(f'<rect x="{x + .6}" y="{y + .6}" width="14.8" height="14.8" rx="3.6" fill="#FFFFFF"'
                         f' stroke="{M.FILO}" stroke-width="1.2"/>')
        else:
            colore = M.ASFALTO if t == "a" else M.VERDE_VIA_SCURO
            parti.append(f'<rect x="{x}" y="{y}" width="16" height="16" rx="4" fill="{colore}"/>')
    parti.append("</svg>")
    return Markup("".join(parti))
