
# DM Linker Web v3

## Varför den förra versionen såg trasig ut
Om Jinja-kod som `{% with ... %}` syns i webbläsaren är HTML-sidan inte renderad av Flask.
Det betyder att sidan hostades statiskt eller öppnades fel.

## Den här versionen fixar det
- HTML renderas direkt från `render_template_string()` i Flask
- uppladdningsformuläret postar till `/link`
- filen skickas tillbaka som nedladdning från backend

## Deploy
### Render / Railway / Azure App Service
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn app:app`

### Docker
```bash
docker build -t dm-linker-web-v3 .
docker run -p 8000:8000 dm-linker-web-v3
```

## Hälso-check
GET `/health` ska returnera `{"ok": true}`.
