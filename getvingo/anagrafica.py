"""Dati anagrafici delle società e dei luoghi: indirizzo completo verificato sulla mappa,
partita IVA e codice fiscale. Usato da tutte le pagine che li chiedono (stessi controlli ovunque)."""
import re

from flask import request

from .servizi import Vietato

CAMPI_INDIRIZZO = ("indirizzo", "cap", "comune", "provincia", "stato")


def _testo(nome):
    return " ".join((request.form.get(nome) or "").split())


def italia(stato):
    return (stato or "Italia").strip().lower() in ("italia", "italy", "it")


def piva_valida(piva):
    """Partita IVA italiana: 11 cifre con la cifra di controllo giusta."""
    if not re.fullmatch(r"\d{11}", piva or ""):
        return False
    somma = 0
    for i, c in enumerate(piva[:10]):
        n = int(c)
        if i % 2:
            n *= 2
            n = n - 9 if n > 9 else n
        somma += n
    return (10 - somma % 10) % 10 == int(piva[10])


def cf_valido(cf):
    """Codice fiscale: 16 caratteri di una persona (anche con omocodia) oppure 11 cifre di una società."""
    cf = (cf or "").upper()
    if re.fullmatch(r"\d{11}", cf):
        return piva_valida(cf)
    if not re.fullmatch(r"[A-Z]{6}[0-9LMNPQRSTUV]{2}[A-Z][0-9LMNPQRSTUV]{2}[A-Z][0-9LMNPQRSTUV]{3}[A-Z]", cf):
        return False
    dispari = {**{str(i): v for i, v in enumerate([1, 0, 5, 7, 9, 13, 15, 17, 19, 21])},
               **dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", [1, 0, 5, 7, 9, 13, 15, 17, 19, 21, 2, 4, 18, 20, 11, 3, 6,
                                                       8, 12, 14, 16, 10, 22, 25, 24, 23]))}
    pari = {**{str(i): i for i in range(10)}, **{c: i for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")}}
    somma = sum(dispari[c] if i % 2 == 0 else pari[c] for i, c in enumerate(cf[:15]))
    return chr(ord("A") + somma % 26) == cf[15]


def leggi_fiscali(oggetto, obbligatori=True, solo_cf=False):
    """P. IVA e codice fiscale dal modulo. obbligatori: per ogni società; solo_cf: clienti (anche privati),
    dove basta il codice fiscale e la P. IVA è facoltativa."""
    piva = re.sub(r"\s", "", _testo("piva")).upper()
    if piva.startswith("IT") and re.fullmatch(r"IT\d{11}", piva):
        piva = piva[2:]
    cf = re.sub(r"\s", "", _testo("codice_fiscale")).upper()
    estero = not italia(_testo("stato") or getattr(oggetto, "stato", ""))
    if obbligatori:
        if not cf:
            raise Vietato("Il codice fiscale è obbligatorio.")
        if not piva and not solo_cf:
            raise Vietato("La partita IVA è obbligatoria.")
    if not estero:
        if piva and not piva_valida(piva):
            raise Vietato("Partita IVA non valida: servono 11 cifre (controlla di averla copiata bene).")
        if cf and not cf_valido(cf):
            raise Vietato("Codice fiscale non valido: 16 caratteri per una persona, 11 cifre per una società.")
    oggetto.piva, oggetto.codice_fiscale = piva, cf


def leggi_indirizzo(oggetto, obbligatorio=True):
    """Indirizzo completo dal modulo. Vale solo se trovato sulla mappa (coordinate presenti): se la via
    non esiste la pagina la segna in rosso e non manda le coordinate, e qui ci si ferma."""
    valori = {c: _testo(c) for c in CAMPI_INDIRIZZO}
    valori["provincia"] = valori["provincia"].upper()[:4]
    valori["stato"] = valori["stato"] or "Italia"
    try:
        lat, lon = float(_testo("lat").replace(",", ".")), float(_testo("lon").replace(",", "."))
    except ValueError:
        lat = lon = None
    if obbligatorio:
        mancano = [n for n, c in (("via e civico", "indirizzo"), ("comune", "comune"), ("CAP", "cap"))
                   if not valori[c]]
        if italia(valori["stato"]) and not valori["provincia"]:
            mancano.append("provincia")
        if mancano:
            raise Vietato("Indirizzo incompleto: manca " + ", ".join(mancano) + ".")
        if lat is None:
            raise Vietato("Indirizzo non trovato sulla mappa: controlla via e comune, oppure tocca il punto sulla mappa.")
    for c, v in valori.items():
        setattr(oggetto, c, v)
    if hasattr(oggetto, "lat") and lat is not None:
        oggetto.lat, oggetto.lon = lat, lon
