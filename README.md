
# DM Linker Web v5 – no fallback

## Ändring i v5
- Inga fallback/search-länkar skapas längre.
- Om artikelnummer saknas i `data/master_mapping.csv` blir det **ingen länk alls**.
- Resultatsidan visar vilka artikelnummer som saknar URL.

## UI
Visar bara:
- PDF
- Land
- Länka PDF

## Master mapping
Fyll på `data/master_mapping.csv` så här:

```csv
country,article,url
PL,000815,https://www.jula.pl/catalog/.../kempingowe-krzeslo-000815/
SE,000815,https://www.jula.se/catalog/.../campingstol-000815/
```

## Deploy på Render
- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn app:app`
