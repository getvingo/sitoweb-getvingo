"""Filtri Jinja per la presentazione."""
import os
from datetime import timedelta

from flask import g
from markupsafe import Markup, escape

from .models import adesso


def vin(valore):
    """VIN tutto attaccato, con le ultime 8 cifre in evidenza."""
    v = escape(valore or "")
    if len(v) != 17:
        return Markup(f'<span class="vin">{v}</span>')
    return Markup(f'<span class="vin" title="{v}">{v[:9]}<b>{v[9:]}</b></span>')


def marchio(marca):
    """Indirizzo del logo della marca (incluso o scaricato), oppure None."""
    from . import marchi
    return marchi.url(marca)


def da_quanto(momento):
    if not momento:
        return ""
    minuti = int((adesso() - momento).total_seconds() // 60)
    if minuti < 1:
        return "adesso"
    if minuti < 60:
        return f"{minuti} min fa"
    ore = minuti // 60
    if ore < 24:
        return f"{ore} h fa" if minuti % 60 < 5 else f"{ore} h {minuti % 60} min fa"
    giorni = ore // 24
    return "ieri" if giorni == 1 else f"{giorni} giorni fa"


def da_tempo(momento):
    """Da quanto dura uno stato: "da 2 giorni", "da 3 h", "da oggi" (per "in preparazione da …")."""
    if not momento:
        return ""
    minuti = int((adesso() - momento).total_seconds() // 60)
    if minuti < 60:
        return "da pochi minuti" if minuti < 5 else f"da {minuti} min"
    ore = minuti // 60
    if ore < 24:
        return f"da {ore} h"
    giorni = ore // 24
    return "da 1 giorno" if giorni == 1 else f"da {giorni} giorni"


def ritardo(momento):
    """Quanto si è oltre l'orario fissato: "3 giorni", "5 h", "20 min" (vuoto se non è in ritardo)."""
    if not momento or momento >= adesso():
        return ""
    minuti = int((adesso() - momento).total_seconds() // 60)
    if minuti < 60:
        return f"{max(minuti, 1)} min"
    ore = minuti // 60
    if ore < 24:
        return f"{ore} h"
    giorni = ore // 24
    return "1 giorno" if giorni == 1 else f"{giorni} giorni"


def iniziali(nome):
    parti = (nome or "?").split()
    return "".join(p[0] for p in parti[:2]).upper()


def data(momento, formato="%d/%m/%Y %H:%M"):
    return momento.strftime(formato) if momento else "—"


GIORNI_BREVI = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]


def slot(momento):
    """Slot di 30 minuti della preparazione, es. "lun 17/11 · 09:00–09:30"."""
    if not momento:
        return ""
    fine = momento + timedelta(minutes=30)
    return f"{GIORNI_BREVI[momento.weekday()]} {momento:%d/%m} · {momento:%H:%M}–{fine:%H:%M}"


def init_app(app):
    app.jinja_env.filters.update(vin=vin, da_quanto=da_quanto, iniziali=iniziali, data=data, marchio=marchio,
                                 slot=slot, da_tempo=da_tempo, ritardo=ritardo)
    # g come globale: serve anche dentro le macro importate (che non vedono il contesto)
    app.jinja_env.globals["g"] = g

    def annullabile(veicolo_id, tipo, ddt_id=None, **dati):
        from .servizi import azione_annullabile
        return azione_annullabile(getattr(g, "utente", None), veicolo_id, tipo, ddt_id, **dati)
    app.jinja_env.globals["annullabile"] = annullabile

    def bloccata_da_danni(prep):
        from .servizi import bloccata_da_danni
        return bloccata_da_danni(prep)
    app.jinja_env.globals["bloccata_da_danni"] = bloccata_da_danni


    def stai_qui(v):
        """True se chi guarda è dove si trova l'auto: il fornitore che l'ha da sé, oppure chi ha appena
        toccato un adesivo (posizione del tocco, 15 minuti) entro 200 m dall'auto."""
        import time
        from flask import session
        from .servizi import RAGGIO_ARRIVO_M, distanza_m
        u = getattr(g, "utente", None)
        if u is None or v.stato != "in_sede" or v.sede is None:
            return False
        if not u.interno and u.sede_id is not None and u.sede_id == v.sede_id:
            return True
        t = session.get("posizione_tocco") or {}
        if t and time.time() - t.get("at", 0) <= 15 * 60 and v.lat is not None:
            return distanza_m(t["lat"], t["lon"], v.lat, v.lon) <= RAGGIO_ARRIVO_M
        return False
    app.jinja_env.globals["stai_qui"] = stai_qui

    def puo_emettere(v):
        from .servizi import puo_emettere
        return puo_emettere(v, g.utente)
    app.jinja_env.globals["puo_emettere"] = puo_emettere

    def puo_sollecitare(mov):
        from .servizi import puo_sollecitare
        return puo_sollecitare(mov, g.utente)
    app.jinja_env.globals["puo_sollecitare"] = puo_sollecitare

    def tocchi_viaggio(mov):
        from .servizi import tocchi_viaggio
        return tocchi_viaggio(mov)
    app.jinja_env.globals["tocchi_viaggio"] = tocchi_viaggio

    def secondi_da(momento):
        """Secondi trascorsi: la pagina poi li fa avanzare da sola (contatore "in viaggio da")."""
        return int((adesso() - momento).total_seconds()) if momento else 0
    app.jinja_env.globals["secondi_da"] = secondi_da
    app.jinja_env.globals["adesso"] = adesso
    app.jinja_env.globals["timedelta"] = timedelta
    def ddt_per_id(ddt_id):
        from .models import Ddt, db as _db
        return _db.session.get(Ddt, ddt_id)
    app.jinja_env.globals["ddt_per_id"] = ddt_per_id
    # versione di CSS e JS nell'indirizzo: dopo un aggiornamento i telefoni scaricano subito quelli nuovi
    import os
    cartella = os.path.join(app.root_path, "static")
    app.jinja_env.globals["versione_statici"] = str(max(
        int(os.path.getmtime(os.path.join(cartella, f))) for f in os.listdir(cartella)
        if f.endswith((".css", ".js"))))
    from .servizi import destinatario_conferma_ovunque, stima_viaggio
    app.jinja_env.globals["stima_viaggio"] = stima_viaggio
    app.jinja_env.globals["destinatario_conferma_ovunque"] = destinatario_conferma_ovunque
    from .models import Movimento
    app.jinja_env.globals["MEZZI_TRASPORTO"] = Movimento.MEZZI

    def condizione(v):
        """Etichetta Nuovo / Usato accanto al nome dell'auto, ovunque compaia."""
        c = getattr(v, "condizione", "") or ""
        if c not in ("nuovo", "usato"):
            return ""
        return Markup(f'<span class="condizione {c}">{"Nuovo" if c == "nuovo" else "Usato"}</span>')
    app.jinja_env.globals["condizione"] = condizione

    @app.context_processor
    def contatori_menu():
        """Numeri mostrati nel menu: i danni da verificare (solo per la concessionaria)."""
        utente = getattr(g, "utente", None)
        if utente is None or not utente.interno:
            return {}
        from .models import Danno
        return {"danni_aperti_n": Danno.query.filter(Danno.risolto_at.is_(None), Danno.rimosso.is_(False)).count()}
