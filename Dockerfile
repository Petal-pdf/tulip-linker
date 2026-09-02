FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Installerar Chromium + alla systembibliotek Playwright behöver
# (--with-deps kör apt-get under huven, kräver root - fungerar i Docker,
# fungerar INTE i Renders vanliga Python-buildpack).
RUN playwright install --with-deps chromium

COPY . .

ENV WEB_CONCURRENCY=1
EXPOSE 8000

CMD ["gunicorn", "--workers", "1", "--threads", "2", "--timeout", "300", "-b", "0.0.0.0:8000", "app:app"]
