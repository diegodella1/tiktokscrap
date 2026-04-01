# tiktokscrap

Monitor self-hosted de perfiles de TikTok. Escanea cuentas periódicamente, guarda posts recientes en SQLite y los muestra en un dashboard web con autenticación.

## Features

- **TikTok scraping** via [yt-dlp](https://github.com/yt-dlp/yt-dlp) — extrae posts recientes, stats (views, likes, comments, reposts) y thumbnails
- **Dashboard web** con auth por password, gestión de cuentas y visualización de posts
- **Panel `/admin`** para configurar Slack y reglas de alertas por views/tiempo
- **Auto-scan** configurable (intervalo en minutos, pause/resume desde la UI)
- **Scan manual** on-demand desde el dashboard
- **Purge automático** de posts viejos (+24h)
- **API REST** completa para integración

## Stack

- Python 3.11+ / Flask
- SQLite (WAL mode)
- APScheduler (scans periódicos)
- yt-dlp (TikTok)

## Setup

```bash
git clone https://github.com/diegodella1/tiktokscrap.git
cd tiktokscrap
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Crear `.env`:

```env
MONITOR_PASSWORD=tu_password
SCAN_INTERVAL_MINUTES=30
```

## Uso

```bash
source venv/bin/activate
python app.py
```

El dashboard queda en `http://localhost:3457`.

El panel de administración queda en `http://localhost:3457/admin`.

## API

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| GET | `/api/accounts` | Listar cuentas TikTok |
| POST | `/api/accounts` | Agregar cuenta `{"username": "x"}` |
| DELETE | `/api/accounts/<username>` | Eliminar cuenta |
| GET | `/api/posts` | Listar posts `?username=x&limit=50&offset=0` |
| GET | `/api/posts/counts` | Posts por cuenta |
| POST | `/api/scan` | Scan manual TikTok |
| GET | `/api/status` | Estado del scanner |
| POST | `/api/autoscan` | Toggle auto-scan `{"enabled": true}` |
| POST | `/api/interval` | Cambiar intervalo `{"minutes": 30}` |
| GET | `/api/admin/settings` | Leer configuración de Slack |
| POST | `/api/admin/settings` | Guardar webhook/canal por defecto |
| GET | `/api/admin/rules` | Listar reglas de alertas |
| POST | `/api/admin/rules` | Crear regla |
| PUT | `/api/admin/rules/<id>` | Editar regla |
| DELETE | `/api/admin/rules/<id>` | Eliminar regla |
| POST | `/api/admin/alerts/run` | Ejecutar evaluación manual |

Todos los endpoints requieren autenticación (cookie o password).

## Estructura

```
tiktokscrap/
├── app.py              # Flask app, routes, scheduler
├── db.py               # SQLite: schema, CRUD, scan logs
├── scraper.py          # TikTok scraping via yt-dlp
├── requirements.txt
├── templates/
│   ├── index.html      # Dashboard principal
│   ├── login.html      # Login page
│   └── docs.html       # API docs
└── data/
    └── tiktok.db       # SQLite DB (se crea automáticamente)
```
