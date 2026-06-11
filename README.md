
# DM Linker Web V10 – Browser Lookup

Denna version använder Playwright/Chromium för att slå upp produkter som en riktig browser.

## Viktigt på Render
Använd Docker runtime eller låt Render bygga med Dockerfile.
Den här appen behöver Chromium, därför används Playwright Docker image.

## Render
- Environment: Docker om möjligt
- Dockerfile finns i repo
- Start sker via Dockerfile CMD

## Beteende
- Hittar riktig produktsida => länk skapas
- Hittar ingen säker produktsida => ingen länk
- Ingen search/fallback-länk skapas i PDF:en
