import re

# 17 caratteri, lettere I, O, Q escluse dallo standard ISO 3779
VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


def normalizza(vin):
    """Pulisce un VIN letto da OCR o digitato: maiuscolo, senza spazi/trattini,
    e corregge le confusioni tipiche (O->0, I->1, Q->0) visto che quelle
    lettere non possono comparire in un VIN."""
    vin = re.sub(r"[\s\-\.]", "", (vin or "").upper())
    return vin.translate(str.maketrans({"O": "0", "I": "1", "Q": "0"}))


def valido(vin):
    return bool(VIN_RE.match(vin or ""))


# Costruttore dalle prime 3 lettere del VIN (WMI, codice mondiale del costruttore): le marche più
# diffuse in Italia. Se il codice non è qui la marca si scrive a mano.
WMI = {
    "WVW": "Volkswagen", "WV1": "Volkswagen", "WV2": "Volkswagen", "WV3": "Volkswagen", "WVG": "Volkswagen",
    "WAU": "Audi", "WUA": "Audi", "TRU": "Audi", "WBA": "BMW", "WBS": "BMW", "WBY": "BMW", "WMW": "Mini",
    "WDB": "Mercedes-Benz", "WDD": "Mercedes-Benz", "WDC": "Mercedes-Benz", "W1K": "Mercedes-Benz",
    "W1N": "Mercedes-Benz", "W1V": "Mercedes-Benz", "WDF": "Mercedes-Benz", "WME": "Smart", "WMX": "Mercedes-AMG",
    "WF0": "Ford", "WF1": "Ford", "WP0": "Porsche", "WP1": "Porsche", "W0L": "Opel", "W0V": "Opel", "VXK": "Opel",
    "ZFA": "Fiat", "ZFC": "Fiat", "ZFF": "Ferrari", "ZAR": "Alfa Romeo", "ZLA": "Lancia", "ZAM": "Maserati",
    "ZHW": "Lamborghini", "ZAC": "Jeep", "1C4": "Jeep", "ZCF": "Iveco", "ZDM": "Ducati", "ZAP": "Piaggio",
    "VF1": "Renault", "VF3": "Peugeot", "VR3": "Peugeot", "VF7": "Citroën", "VR7": "Citroën", "VR1": "DS",
    "UU1": "Dacia", "TMB": "Škoda", "VSS": "SEAT", "VSZ": "Cupra", "SAL": "Land Rover", "SAJ": "Jaguar",
    "SCC": "Lotus", "SCB": "Bentley", "SCA": "Rolls-Royce", "SBM": "McLaren", "SJN": "Nissan", "VSK": "Nissan",
    "JN1": "Nissan", "SB1": "Toyota", "JTD": "Toyota", "JTE": "Toyota", "JTN": "Toyota", "JTH": "Lexus",
    "NMT": "Toyota", "VNK": "Toyota", "JHM": "Honda", "SHH": "Honda", "JMZ": "Mazda", "JMB": "Mitsubishi",
    "JSA": "Suzuki", "TSM": "Suzuki", "KMH": "Hyundai", "TMA": "Hyundai", "NLH": "Hyundai", "KNA": "Kia",
    "U5Y": "Kia", "YV1": "Volvo", "LVY": "Volvo", "YS3": "Saab", "5YJ": "Tesla", "7SA": "Tesla", "LRW": "Tesla",
    "XP7": "Tesla", "LSJ": "MG", "LDP": "Voyah", "LGX": "BYD", "LC0": "BYD", "VGA": "Alpine", "LMG": "MG",
}


def marca_da_vin(vin):
    """Marca dal VIN, se il costruttore è tra quelli noti; altrimenti stringa vuota."""
    vin = normalizza(vin)
    return WMI.get(vin[:3], "") if len(vin) >= 3 else ""
