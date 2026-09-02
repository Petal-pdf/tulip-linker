# Playwrights officiella image - har Chromium + alla systembibliotek
# förinstallerade, så vi slipper apt-get-strul med föråldrade
# fontpaket (t.ex. ttf-unifont) som annars uppstår när man kör
# "playwright install --with-deps" manuellt ovanpå python:slim.
#
# VIKTIGT: taggen (v1.47.0-noble) måste matcha playwright-versionen i
# requirements.txt exakt, annars hittar Playwright inte webbläsaren.
FROM mcr.microsoft.com/playwright/python:v1.47.0-noble

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# OBS: ingen "playwright install" behövs - Chromium + libs finns redan
# i basimagen (bekräftat av Playwright-teamet).

COPY . .

ENV WEB_CONCURRENCY=1
EXPOSE 8000

CMD ["gunicorn", "--workers", "1", "--threads", "2", "--timeout", "300", "-b", "0.0.0.0:8000", "app:app"]
