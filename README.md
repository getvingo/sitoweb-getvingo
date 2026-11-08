# getVINgo

Portale web per concessionarie: movimentazione auto per VIN con DDT automatico,
lavorazioni presso fornitori, preparazione consegne e localizzazione delle auto
in piazzale (adesivi NFC).

Stack: Python 3.9+ · Flask · SQLAlchemy (SQLite in locale, Postgres in produzione) · ReportLab per i PDF.

## Avvio in locale

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/flask --app getvingo demo                 # crea dati di prova
.venv/bin/flask --app getvingo run --port 5050      # http://127.0.0.1:5050
```

Accessi demo (password `demo`): `admin@demo.it`, `ufficio@demo.it`,
`autista@demo.it`, `carrozzeria@demo.it`, `officina@demo.it`.

Avvio senza dati demo: `.venv/bin/flask --app getvingo init-db`

## Test

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest tests
```

## Configurazione (variabili d'ambiente)

| Variabile | Uso |
|---|---|
| `DOVIN_DATI` | cartella di foto, archivio DDT e loghi (default `instance/`) |
| `DOVIN_SECRET_KEY` | chiave di sessione (se assente viene creata in `instance/secret_key`) |
| `DOVIN_DATABASE_URL` | database Postgres in produzione (default SQLite in `instance/`) |
| `ANTHROPIC_API_KEY` | assistente IA (facoltativo) |
| `DOVIN_S3_BUCKET`, `DOVIN_S3_ENDPOINT`, `DOVIN_S3_REGIONE`, `DOVIN_S3_CHIAVE_ID`, `DOVIN_S3_SEGRETO` | archivio DDT su storage S3 compatibile (facoltativo) |

Nessun dato, database o segreto è incluso nel repository (cartella `instance/` esclusa).

## Pubblicazione

Produzione: Render (Francoforte), tutto descritto in [`render.yaml`](render.yaml).
Dettagli di funzionamento: [`LEGGIMI.txt`](LEGGIMI.txt).
