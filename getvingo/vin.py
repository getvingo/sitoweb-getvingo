"""VIN (telaio): pulizia, controllo e decodifica.

Regole e tabelle da references/01_Guida_Struttura_VIN.md e 02_VIN_Knowledge_Base_AI.md (ISO 3779/3780).
Regola d'oro: mai inventare un dato. Si compila solo ciò che il VIN dice con certezza (costruttore, Paese,
tipo; modello e anno solo per le marche che li codificano); il resto resta vuoto e lo completa l'operatore.
"""
import re
from datetime import date

# 17 caratteri, lettere I, O, Q escluse dallo standard ISO 3779
VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


def normalizza(vin):
    """Pulisce un VIN letto da OCR o digitato: maiuscolo, senza spazi/trattini/asterischi,
    e corregge le confusioni tipiche (O->0, I->1, Q->0) visto che quelle
    lettere non possono comparire in un VIN."""
    vin = re.sub(r"[\s\-\.\*★<>]", "", (vin or "").upper())
    return vin.translate(str.maketrans({"O": "0", "I": "1", "Q": "0"}))


def valido(vin):
    return bool(VIN_RE.match(vin or ""))


# ---- Cifra di controllo (posizione 9) ----
VALORI = {**{str(i): i for i in range(10)},
          **dict(zip("ABCDEFGHJKLMNPRSTUVWXYZ", [1, 2, 3, 4, 5, 6, 7, 8, 1, 2, 3, 4, 5, 7, 9, 2, 3, 4, 5, 6, 7, 8, 9]))}
PESI = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]


def cifra_controllo(vin):
    resto = sum(VALORI[c] * PESI[i] for i, c in enumerate(vin)) % 11
    return "X" if resto == 10 else str(resto)


def controllo_ok(vin):
    return valido(vin) and vin[8] == cifra_controllo(vin)


def controllo_obbligatorio(vin):
    """Obbligatoria per i veicoli del Nord America (VIN che inizia con 1-5) e della Cina (L).
    In Europa è facoltativa: una posizione 9 'sbagliata' non è un errore."""
    return bool(vin) and vin[0] in "12345L"


# Confusioni frequenti dell'OCR, da risolvere con la cifra di controllo
CONFUSIONI = {"8": "B", "B": "8", "5": "S", "S": "5", "2": "Z", "Z": "2", "6": "G", "G": "6", "1": "7", "7": "1",
              "0": "D", "D": "0", "U": "V", "V": "U", "M": "N", "N": "MH", "H": "N"}


def correggi_ocr(vin):
    """VIN con cifra di controllo obbligatoria che non torna: prova a cambiare un carattere alla volta tra
    quelli che l'OCR confonde. Se una sola variante torna la restituisce, altrimenti None."""
    if not valido(vin) or not controllo_obbligatorio(vin) or controllo_ok(vin):
        return None
    varianti = set()
    for i, c in enumerate(vin):
        for alt in CONFUSIONI.get(c, ""):
            v = vin[:i] + alt + vin[i + 1:]
            if controllo_ok(v):
                varianti.add(v)
    return varianti.pop() if len(varianti) == 1 else None


# ---- Costruttore dalle prime 3 lettere (WMI) ----
# tipo: A = autovettura/SUV (vettura), C = veicolo commerciale, M = moto/scooter, X = sia auto sia commerciali
_COSTRUTTORI = """
ZFA Fiat A; ZFC Fiat C; ZAR Alfa Romeo A; ZLA Lancia A; ZAM Maserati A; ZFF Ferrari A; ZHW Lamborghini A;
ZCF Iveco C; ZAC Jeep A; ZDM Ducati M; ZD4 Aprilia M; ZGU Moto Guzzi M; ZAP Piaggio M; ZCG MV Agusta M;
WVW Volkswagen A; WVG Volkswagen A; WV1 Volkswagen C; WV2 Volkswagen C; WV3 Volkswagen C; WAU Audi A; WUA Audi A;
WBA BMW A; WBS BMW A; WBY BMW A; WB1 BMW M; WMW Mini A; WDB Mercedes-Benz A; WDD Mercedes-Benz A;
WDC Mercedes-Benz A; W1K Mercedes-Benz A; W1N Mercedes-Benz A; WDF Mercedes-Benz C; W1V Mercedes-Benz C;
WMX Mercedes-AMG A; WME Smart A; WP0 Porsche A; WP1 Porsche A; W0L Opel A; W0V Opel A; VXK Opel A;
WF0 Ford X; WF1 Ford X; WMA MAN C;
VF1 Renault X; VF3 Peugeot A; VR3 Peugeot A; VF7 Citroën A; VR7 Citroën A; VR1 DS A; VF9 Bugatti A;
VNK Toyota A; VGA Alpine A; UU1 Dacia A; VSS SEAT A; VSZ Cupra A; VWV Volkswagen A; VS6 Ford A; VSK Nissan X;
VBK KTM M; TMB Škoda A; TMA Hyundai A; TRU Audi A; TSM Suzuki A; YV1 Volvo A; YV4 Volvo A; YS3 Saab A; YS2 Scania C;
SAJ Jaguar A; SAD Jaguar A; SAL Land Rover A; SCA Rolls-Royce A; SCB Bentley A; SCC Lotus A; SCF Aston Martin A;
SBM McLaren A; SJN Nissan A; SB1 Toyota A; SHH Honda A; SMT Triumph M;
JTD Toyota A; JTE Toyota A; JTM Toyota A; JTN Toyota A; JTH Lexus A; JTJ Lexus A; JHM Honda A; JHL Honda A;
JH2 Honda M; JN1 Nissan A; JN8 Nissan A; JM1 Mazda A; JMZ Mazda A; JF1 Subaru A; JF2 Subaru A; JS2 Suzuki A;
JS3 Suzuki A; JSA Suzuki A; JS1 Suzuki M; JMB Mitsubishi A; JA3 Mitsubishi A; JA4 Mitsubishi A; JAA Isuzu C;
JYA Yamaha M; JKA Kawasaki M;
KMH Hyundai A; KMT Genesis A; KNA Kia A; KND Kia A; KNE Kia A; U5Y Kia A; KPT KGM A; NLH Hyundai A; NMT Toyota A;
NM0 Ford C; MAL Hyundai A; MA3 Maruti Suzuki A; MAT Tata X; MA1 Mahindra X; MD2 Bajaj M; ME3 Royal Enfield M;
ME4 Honda M; MBL Hero M;
LSV Volkswagen A; LBV BMW A; LRW Tesla A; LGX BYD A; LC0 BYD A; LSJ MG A; LMG MG A; LPS Polestar A; LYV Volvo A;
LVY Volvo A; LDP Voyah A; XP7 Tesla A;
1G1 Chevrolet A; 1GC Chevrolet C; 1GT GMC C; 1G6 Cadillac A; 1FA Ford A; 1FT Ford C; 1FM Ford A; 1C4 Jeep A;
1J4 Jeep A; 1J8 Jeep A; 1C6 Ram C; 3C6 Ram C; 5YJ Tesla A; 7SA Tesla A; 4US BMW A; 5UX BMW A; 5YM BMW A;
4JG Mercedes-Benz A; 55S Mercedes-Benz A; 4T1 Toyota A; 5TD Toyota A; 19X Honda A; 1HG Honda A; 5FN Honda A;
5J6 Honda A; 2HG Honda A; 2HK Honda A; 1N4 Nissan A; 5N1 Nissan A; 5NP Hyundai A; 5NM Hyundai A;
3VW Volkswagen A; 3N1 Nissan A; 1HD Harley-Davidson M; 56K Indian M; 9BW Volkswagen A; 9BD Fiat A; 9BG Chevrolet A
"""
COSTRUTTORI = {}
for _voce in _COSTRUTTORI.replace("\n", " ").split(";"):
    _voce = _voce.strip()
    if _voce:
        _codice, _resto = _voce.split(" ", 1)
        _marca, _tipo = _resto.rsplit(" ", 1)
        COSTRUTTORI[_codice] = (_marca, _tipo)
# marca per WMI: usata anche per scrivere le marche col nome ufficiale (uniforma.MARCHE)
WMI = {k: m for k, (m, _) in COSTRUTTORI.items()}
TIPOLOGIA = {"A": "vettura", "C": "commerciale", "M": "moto", "X": ""}


def _costruttore(vin):
    if len(vin) < 3:
        return None
    if vin[:3] in COSTRUTTORI:
        return COSTRUTTORI[vin[:3]]
    if vin[:2] == "JT":  # Toyota usa molti codici JT…
        return ("Toyota", "A")
    return None


def marca_da_vin(vin):
    """Marca dal VIN, se il costruttore è tra quelli noti; altrimenti stringa vuota."""
    c = _costruttore(normalizza(vin))
    return c[0] if c else ""


# ---- Paese di produzione (posizioni 1-2): dove è stato costruito, non la nazionalità del marchio ----
_ORDINE = "ABCDEFGHJKLMNPRSTUVWXYZ1234567890"
_PAESI = [  # (prima lettera, seconda da, seconda a, Paese); seconda vuota = tutta la lettera
    ("A", "A", "H", "Sudafrica"), ("J", "", "", "Giappone"), ("K", "L", "R", "Corea del Sud"), ("L", "", "", "Cina"),
    ("M", "A", "E", "India"), ("M", "F", "K", "Indonesia"), ("M", "L", "R", "Thailandia"), ("N", "A", "E", "Iran"),
    ("N", "L", "R", "Turchia"), ("P", "L", "R", "Malesia"), ("R", "F", "K", "Taiwan"),
    ("S", "A", "M", "Regno Unito"), ("S", "N", "T", "Germania"), ("S", "U", "Z", "Polonia"),
    ("T", "A", "H", "Svizzera"), ("T", "J", "P", "Repubblica Ceca"), ("T", "R", "V", "Ungheria"),
    ("U", "U", "7", "Romania"), ("V", "A", "E", "Austria"), ("V", "F", "R", "Francia"), ("V", "S", "W", "Spagna"),
    ("W", "", "", "Germania"), ("X", "L", "R", "Paesi Bassi"), ("X", "3", "0", "Russia"), ("Y", "A", "E", "Belgio"),
    ("Y", "F", "K", "Finlandia"), ("Y", "S", "W", "Svezia"), ("Z", "A", "R", "Italia"),
    ("1", "", "", "Stati Uniti"), ("4", "", "", "Stati Uniti"), ("5", "", "", "Stati Uniti"), ("2", "", "", "Canada"),
    ("3", "A", "W", "Messico"), ("6", "", "", "Australia"), ("7", "", "", "Nuova Zelanda"),
    ("8", "A", "E", "Argentina"), ("9", "A", "E", "Brasile"),
]
_CONTINENTI = [("ABCDEFGH", "Africa"), ("JKLMNPR", "Asia"), ("STUVWXYZ", "Europa"), ("12345", "Nord America"),
               ("67", "Oceania"), ("89", "Sud America")]


def paese(vin):
    if len(vin) < 2:
        return ""
    for p, da, a, nome in _PAESI:
        if vin[0] == p and (not da or _ORDINE.index(da) <= _ORDINE.index(vin[1]) <= _ORDINE.index(a)):
            return nome
    return ""


def continente(vin):
    return next((n for lettere, n in _CONTINENTI if vin and vin[0] in lettere), "")


# ---- Anno modello (posizione 10): solo per chi lo codifica ----
_ANNI = "ABCDEFGHJKLMNPRSTVWXY123456789"  # A = 1980/2010 … 9 = 2009/2039
# Marche che in Europa scrivono l'anno in posizione 10 (gruppo Volkswagen, Hyundai/Kia, Tesla, Volvo)
ANNO_IN_POS_10 = {"WVW", "WVG", "WV1", "WV2", "WV3", "WAU", "WUA", "TRU", "TMB", "VSS", "VSZ", "VWV", "WP0", "WP1",
                  "KMH", "KMT", "KNA", "KND", "KNE", "U5Y", "TMA", "NLH", "MAL", "5YJ", "7SA", "LRW", "XP7",
                  "YV1", "YV4", "LYV", "LVY"}


def anno_modello(vin, oggi=None):
    """Anno modello, o None se il costruttore non lo codifica (Fiat, Mercedes, Renault, PSA in Europa…).
    Il codice si ripete ogni 30 anni: si sceglie l'anno più recente non oltre l'anno prossimo
    (un'auto prodotta a settembre può essere già 'modello' dell'anno dopo)."""
    if not valido(vin) or vin[9] not in _ANNI:
        return None
    nord_america = vin[0] in "12345"
    if not (nord_america or vin[:3] in ANNO_IN_POS_10):
        return None
    base = 1980 + _ANNI.index(vin[9])
    if nord_america:  # regola USA: posizione 7 numerica = ciclo 1980-2009, lettera = 2010-2039
        return base if vin[6].isdigit() else base + 30
    limite = (oggi or date.today()).year + 1
    return base + 30 if base + 30 <= limite else base


# ---- Modello: solo dove la regola del costruttore è stabile ----
# Gruppo Volkswagen: posizioni 7-8 (con posizioni 4-6 "ZZZ" nei VIN europei)
MODELLI_VAG = {
    "1J": "Golf", "1K": "Golf", "5K": "Golf", "AU": "Golf", "CD": "Golf", "6R": "Polo", "AW": "Polo",
    "3C": "Passat", "3G": "Passat", "5N": "Tiguan", "AD": "Tiguan", "A1": "T-Roc", "C1": "T-Cross",
    "E1": "ID.3", "E2": "ID.4", "1T": "Touran", "3H": "Arteon", "CR": "Touareg", "AA": "up!",
    "7H": "Transporter", "7E": "Transporter", "2K": "Caddy",
    "8L": "A3", "8P": "A3", "8V": "A3", "8Y": "A3", "GY": "A3", "8K": "A4", "8W": "A4", "4G": "A6", "4K": "A6",
    "8R": "Q5", "FY": "Q5", "4M": "Q7", "F3": "Q3", "GA": "Q2", "GB": "A1",
    "5E": "Octavia", "NX": "Octavia", "5F": "Leon",
}
COMMERCIALI_VAG = {"7H", "7E", "2K"}
GENERAZIONI_VAG = {"1J": "IV", "1K": "V", "5K": "VI", "AU": "VII", "CD": "VIII", "6R": "V", "AW": "VI", "3C": "B6/B7",
                   "3G": "B8", "5N": "I", "AD": "II", "7H": "T5", "7E": "T6", "8L": "8L", "8P": "8P", "8V": "8V",
                   "8Y": "8Y", "GY": "8Y", "8K": "B8", "8W": "B9", "4G": "C7", "4K": "C8", "5E": "III", "NX": "IV",
                   "5F": "III"}
# Fiat, Alfa Romeo, Lancia, Abarth: posizioni 4-6 = numero di progetto
PROGETTI_FIAT = {"312": "500", "169": "Panda", "319": "Panda", "199": "Punto", "334": "500X", "356": "Tipo",
                 "250": "Ducato", "263": "Doblò", "225": "Fiorino",
                 "940": "Giulietta", "955": "MiTo", "952": "Giulia", "949": "Stelvio"}
COMMERCIALI_FIAT = {"250", "263", "225"}
# Mercedes-Benz: posizioni 4-6 = serie del Baumuster
SERIE_MERCEDES = {"176": "Classe A", "177": "Classe A", "205": "Classe C", "206": "Classe C", "212": "Classe E",
                  "213": "Classe E", "214": "Classe E", "221": "Classe S", "222": "Classe S", "223": "Classe S",
                  "253": "GLC", "254": "GLC", "166": "GLE", "167": "GLE", "463": "Classe G", "464": "Classe G",
                  "906": "Sprinter", "907": "Sprinter", "447": "Vito"}
COMMERCIALI_MERCEDES = {"906", "907", "447"}
MODELLI_TESLA = {"S": "Model S", "3": "Model 3", "X": "Model X", "Y": "Model Y"}
VAG = {"WVW", "WVG", "WV1", "WV2", "WV3", "WAU", "WUA", "TRU", "TMB", "VSS", "VSZ", "VWV"}


def _modello(vin):
    """(modello, generazione, tipo 'C' se commerciale) o (None, None, None)."""
    w = vin[:3]
    if w in VAG and vin[3:6] == "ZZZ":
        cod = vin[6:8]
        if cod in MODELLI_VAG:
            return MODELLI_VAG[cod], GENERAZIONI_VAG.get(cod), "C" if cod in COMMERCIALI_VAG else None
    if w in ("ZFA", "ZFC", "ZAR", "ZLA") and vin[3:6] in PROGETTI_FIAT:
        p = vin[3:6]
        return PROGETTI_FIAT[p], None, "C" if p in COMMERCIALI_FIAT else None
    if WMI.get(w) == "Mercedes-Benz" and vin[3:6] in SERIE_MERCEDES:
        s = vin[3:6]
        return SERIE_MERCEDES[s], s, "C" if s in COMMERCIALI_MERCEDES else None
    if WMI.get(w) == "Tesla" and vin[3] in MODELLI_TESLA:
        return MODELLI_TESLA[vin[3]], None, None
    return None, None, None


def decodifica(vin, oggi=None):
    """Quello che il VIN dice con certezza, con fonte e affidabilità di ogni dato (mai inventato).

    Restituisce un dizionario: vin (pulito), valido, proposta (VIN corretto di un carattere, da far confermare),
    marca, tipologia ('vettura' | 'commerciale' | 'moto' | ''), modello, generazione, anno_modello, paese,
    continente, seriale, cifra_controllo {obbligatoria, ok}, affidabilita {campo: alta|media}, avvisi [testo].
    Il VIN non si cambia mai da solo: la correzione resta una proposta che l'operatore accetta a vista."""
    vin = normalizza(vin)
    d = {"vin": vin, "valido": valido(vin), "proposta": None, "marca": "", "tipologia": "", "modello": "",
         "generazione": None, "anno_modello": None, "paese": paese(vin), "continente": continente(vin),
         "seriale": vin[11:] if len(vin) == 17 else "", "cifra_controllo": {"obbligatoria": False, "ok": None},
         "affidabilita": {}, "avvisi": []}
    if not d["valido"]:
        if vin:
            d["avvisi"].append("Servono 17 caratteri, senza I, O e Q." if len(vin) != 17 else "Caratteri non ammessi nel VIN.")
        return d
    obbl = controllo_obbligatorio(vin)
    if obbl and not controllo_ok(vin):
        d["proposta"] = correggi_ocr(vin)
        d["avvisi"].append("La cifra di controllo (9° carattere) non torna: " +
                           (f"forse è {d['proposta']}? Confrontalo con l'etichetta." if d["proposta"]
                            else "rileggi il VIN sull'etichetta."))
    d["cifra_controllo"] = {"obbligatoria": obbl, "ok": controllo_ok(vin) if obbl or controllo_ok(vin) else None}
    if d["paese"]:
        d["affidabilita"]["paese"] = "alta"
    c = _costruttore(vin)
    if c:
        d["marca"], d["tipologia"] = c[0], TIPOLOGIA[c[1]]
        d["affidabilita"]["marca"] = "alta"
        if d["tipologia"]:
            d["affidabilita"]["tipologia"] = "media"
    modello, gen, tipo = _modello(vin)
    if modello:
        d["modello"], d["generazione"] = modello, gen
        d["affidabilita"]["modello"] = "media"  # regola del costruttore: può avere eccezioni, si conferma a vista
        if tipo == "C":
            d["tipologia"] = "commerciale"
    anno = anno_modello(vin, oggi)
    if anno:
        d["anno_modello"] = anno
        d["affidabilita"]["anno_modello"] = "alta"
    return d


def completa(veicolo, tipologia=False):
    """Riempie dal VIN i dati non scritti (marca, modello, anno; con tipologia=True anche vettura/commerciale).
    Non tocca mai ciò che è già compilato."""
    d = decodifica(veicolo.vin)
    if not veicolo.marca and d["marca"]:
        veicolo.marca = d["marca"]
    if not veicolo.modello and d["modello"]:
        veicolo.modello = d["modello"]
    if not getattr(veicolo, "anno", None) and d["anno_modello"]:
        veicolo.anno = d["anno_modello"]
    if tipologia and d["tipologia"] in ("vettura", "commerciale"):
        veicolo.tipologia = d["tipologia"]
