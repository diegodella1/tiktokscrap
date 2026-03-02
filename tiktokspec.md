Necesito que crees un proyecto Next.js llamado "tiktok-monitor" que funcione como un monitor self-hosted de perfiles de TikTok. Va a correr en mi Raspberry Pi 5 con IP residencial.

## Qué tiene que hacer

1. Monitorear una lista de usernames de TikTok
2. Cada X horas (configurable, default 4hs), visitar el perfil público de cada usuario
3. Scrapear el JSON embebido en el HTML (tag `__UNIVERSAL_DATA_FOR_REHYDRATION__`) — NO usar APIs de terceros, NO usar browser headless
4. Extraer: info del perfil (bio, seguidores, etc) y posts recientes
5. Comparar contra lo que ya tenemos guardado y detectar posts nuevos
6. Guardar todo en SQLite local (usar `better-sqlite3`)
7. Servir un dashboard web donde puedo ver todo y gestionar usuarios

## Stack

- Next.js 14 con App Router y TypeScript
- SQLite via `better-sqlite3` (archivo en `./data/tiktok.db`, se crea solo)
- `node-cron` para el polling automático (se inicia via `instrumentation.ts`)
- Sin Tailwind — CSS vanilla con variables, estética dark/terminal monocromática
- Puerto 3456

## Estructura

```
tiktok-monitor/
├── app/
│   ├── api/
│   │   ├── monitor/route.ts    # POST → ejecutar scan manual
│   │   ├── users/route.ts      # GET/POST/DELETE/PATCH → CRUD usuarios
│   │   ├── posts/route.ts      # GET → listar posts (?user=x&limit=100)
│   │   └── export/route.ts     # GET → descargar CSV con todo
│   ├── dashboard/
│   │   └── Dashboard.tsx        # Client component — UI principal
│   ├── globals.css
│   ├── layout.tsx
│   └── page.tsx                 # Server component, carga data inicial de SQLite
├── lib/
│   ├── db.ts                    # Todo SQLite: schema, CRUD users/profiles/posts/logs
│   ├── scraper.ts               # Fetch TikTok HTML + parsear JSON embebido
│   └── monitor.ts               # Orquestación: recorre usuarios, scrapea, guarda
├── instrumentation.ts           # Hook de Next.js para iniciar el cron
├── data/                        # Se crea solo, contiene tiktok.db
└── package.json
```

## Módulos en detalle

### lib/scraper.ts
- `scrapeUser(username)` → hace fetch a `https://www.tiktok.com/@{username}` con headers de browser real
- Parsea el HTML buscando `<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">`, extrae el JSON
- `extractProfile(data)` → saca username, nickname, bio, verified, followers, following, likes, videos desde `data.__DEFAULT_SCOPE__["webapp.user-detail"].userInfo`
- `extractPosts(data)` → saca la lista de posts desde `itemList` con video_id, description, create_time, stats (likes, comments, shares, plays), URL
- Si el path principal falla, intentar fallback con `data.UserModule.users` (legacy)
- Incluir comentarios claros tipo ">>> SI SE ROMPE, AJUSTÁ LOS PATHS ACÁ <<<"
- 3 segundos de espera entre cada usuario

### lib/db.ts
- Schema: tablas `users`, `profiles`, `posts`, `logs` con foreign keys y indexes
- `getActiveUsernames()`, `addUser()`, `removeUser()`, `toggleUser()`
- `upsertProfile()` con INSERT OR CONFLICT UPDATE
- `getKnownVideoIds(username)` → Set para comparar
- `insertPost()` con INSERT OR IGNORE
- `addLog()` con auto-prune a 500 entradas
- `getAllDataForExport()` para el CSV

### lib/monitor.ts
- `runMonitor()` → recorre usuarios activos, llama scrapeUser, compara, guarda nuevos, loggea todo
- Retorna un objeto con `totalNewPosts`, `totalErrors`, y `details` por usuario

### instrumentation.ts
- Usa `node-cron` para programar `runMonitor()` 
- Schedule configurable via `process.env.CRON_SCHEDULE` (default: `"0 */4 * * *"`)
- Activar `experimental.instrumentationHook` en `next.config.js`

### Dashboard (app/dashboard/Dashboard.tsx)
- Client component "use client" con estado local
- 3 tabs: Posts, Usuarios, Log
- **Posts**: feed vertical, cada post muestra @username, fecha, descripción (linkeada al video), stats (plays/likes/comments). Los del último batch con borde accent
- **Usuarios**: input para agregar (sin @), lista de usuarios con su perfil (seguidores, likes, bio), botones Pausar y Eliminar. Usuarios pausados se muestran con opacity baja
- **Log**: estilo terminal, cada línea con timestamp + nivel coloreado (ERROR rojo, WARN amarillo, OK verde, INFO gris) + mensaje
- Header con: título "◉ TIKTOK MONITOR", contador de usuarios/posts, botón "↓ CSV" (link a /api/export), botón "▶ Escanear ahora" (POST a /api/monitor, muestra resultado como toast)
- Estética: fondo negro (#0a0a0f), surfaces oscuras, font monospace, accent rojo/rosa (#ff3b5c), verde para OK (#00d68f)

### API routes
- `POST /api/monitor` → ejecuta runMonitor(), retorna JSON con resultado
- `GET /api/users` → lista usuarios
- `POST /api/users` → `{"username": "x"}` → agregar
- `DELETE /api/users` → `{"username": "x"}` → eliminar (cascada posts+profile)  
- `PATCH /api/users` → `{"username": "x", "active": true/false}` → toggle
- `GET /api/posts` → query params `?user=x&limit=100`
- `GET /api/export` → devuelve CSV con Content-Disposition attachment

## Después de crear todo

1. Correr `npm install`
2. Correr `npm run build`
3. Correr `npm start` 
4. Verificar que el dashboard carga en http://localhost:3456
5. Decime qué IP/puerto quedó corriendo

## Notas
- El `page.tsx` raíz es un Server Component que carga la data inicial de SQLite y la pasa como props al Dashboard client component
- Usar `export const dynamic = "force-dynamic"` en page.tsx
- El export CSV combina perfiles y posts en un solo archivo
- better-sqlite3 puede necesitar rebuild para ARM64 (Raspberry Pi) — si falla, correr `npm rebuild better-sqlite3`
