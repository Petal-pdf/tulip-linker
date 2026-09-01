# DM Linker – Live jula.se-uppslag

Laddar upp en Jula-DM (PDF), hittar artikelnummer automatiskt, slår upp
varje nummer live mot jula.se, verifierar produktnamnet, och lägger in
klickbara länkar direkt i PDF:en – kant-till-kant över produktrutan.

## Beteende
- Hittar riktig produktsida => länk skapas
- Hittar ingen säker/verifierad produktsida => ingen länk (regel: inga
  söksides-länkar hamnar i PDF:en)
- Klickytan beräknas från PDF:ens faktiska bakgrundsrutor/bilder, snäpps
  ut mot sidkant där det är relevant (ingen tom marginal)

## Output
Endpointen `/link` returnerar en zip: `dm_linker_resultat.zip` med:
- `linked.pdf` – den länkade PDF:en
- `links.csv` – Artikelnummer, Produktnamn, URL, Sida
- `qa_report.json` – antal hittade produkter, antal skapade länkar,
  samt en lista över osäkra matchningar som INTE länkades

## Köra lokalt
```bash
pip install -r requirements.txt
python app.py
# öppna http://localhost:8000
```

## Deploy på Render
- Använder `render.yaml` (Python-runtime, inga extra system-beroenden
  behövs eftersom vi kör ren `requests`/`beautifulsoup4`-baserad
  uppslagning – ingen Playwright/Chromium krävs för basflödet).
- `Dockerfile` finns kvar som alternativ om du vill styra runtime själv.

## Kända begränsningar
- Rutdetektionen är en heuristik (störst omslutande bakgrundsruta/bild
  runt artikelnumret). Fungerar mycket bra på Julas standardlayout, men
  granska QA-rapporten på sidor med ovanligt tät eller överlappande
  layout.
- jula.se:s sökresultat skrapas som HTML. Om sökningen börjar missa
  många produkter (t.ex. pga JS-rendering) är nästa steg att byta ut
  `lookup_jula()` mot en Playwright-baserad uppslagning.
- Uppslagningscache (`LOOKUP_CACHE`) är bara in-memory och nollställs
  vid omstart av processen.
