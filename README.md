# Auditoría IA — microservicio para Render

Este es un recorte del `main.py` original: solo trae el endpoint `/api/auditar`
(clasificación de comprobantes con Claude + reconciliación determinística).
El resto de tu app (dashboard, sincronización ODBC con A2, SQLite) se queda
corriendo donde está hoy — eso no se puede mover a la nube porque depende de
leer archivos y el driver ODBC de la máquina donde está instalado A2.

## Qué vas a hacer

1. Subir este código a un repositorio en GitHub.
2. Crear un "Web Service" en Render Dashboard conectado a ese repo.
3. Configurar dos variables de entorno (tu API key de Anthropic + un secreto
   inventado por ti).
4. Copiar la URL que te da Render y usarla en tu `index.html`.

---

## Paso 1 — Crear el repositorio en GitHub

No tienes un repo todavía, así que:

1. Entra a https://github.com/new y crea un repositorio nuevo (puede ser
   privado). Nómbralo, por ejemplo, `auditoria-ia-render`. No marques
   "Add a README" (ya tienes uno).
2. En tu computadora, abre una terminal en la carpeta donde tengas estos
   archivos (`main.py`, `audit_prompt.py`, `requirements.txt`, `.gitignore`,
   `.env.example`, `README.md`) y corre:

   ```bash
   git init
   git add .
   git commit -m "Microservicio de auditoría IA"
   git branch -M main
   git remote add origin https://github.com/TU-USUARIO/auditoria-ia-render.git
   git push -u origin main
   ```

   (Reemplaza `TU-USUARIO` por tu usuario de GitHub — o pega la URL exacta
   que GitHub te muestra después de crear el repo, ahí normalmente dice
   "…or push an existing repository from the command line".)

---

## Paso 2 — Crear el Web Service en Render Dashboard

1. Entra a https://dashboard.render.com/ e inicia sesión (o crea cuenta).
2. Click en **New +** → **Web Service**.
3. Conecta tu cuenta de GitHub si no lo has hecho, y selecciona el repo
   `auditoria-ia-render` que acabas de crear.
4. Render va a detectar que es un proyecto Python. Completa:
   - **Name**: `auditoria-ia` (o el nombre que quieras — esto define tu URL:
     `https://auditoria-ia.onrender.com`)
   - **Language/Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Instance Type**: Free (para probar) o Starter si quieres evitar que
     el servicio "duerma" — ver nota abajo.
5. **Antes de darle a "Create Web Service"**, baja hasta la sección
   **Environment Variables** y agrega:

   | Key | Value |
   |---|---|
   | `ANTHROPIC_API_KEY` | tu API key real (empieza con `sk-ant-...`) |
   | `AUDIT_SHARED_SECRET` | inventa una cadena larga y aleatoria (ej. genera una con `openssl rand -hex 32`) |

   ⚠️ Esta es la API key que estaba escrita directamente en tu `main.py`
   original — **cámbiala** por una nueva desde
   https://console.anthropic.com/settings/keys antes de usarla aquí, ya que
   la anterior quedó expuesta en el código que compartiste.

6. Click en **Create Web Service**. Render va a instalar dependencias y
   levantar el servicio — toma unos 2-4 minutos la primera vez. Cuando
   termine, verás tu URL pública arriba, algo como
   `https://auditoria-ia.onrender.com`.
7. Verifica que quedó vivo abriendo `https://auditoria-ia.onrender.com/` en
   el navegador — debe responder `{"status":"ok","servicio":"auditoria-ia"}`.

### Nota sobre el plan Free

En el plan **Free**, Render apaga el servicio tras 15 minutos sin tráfico y
tarda entre 30-60 segundos en "despertar" con la siguiente petición. Para
este caso de uso (auditorías esporádicas) probablemente no importa, pero si
notas que la primera auditoría del día tarda más de lo normal, es por eso —
no es un error. Si te molesta, puedes subir al plan **Starter** (siempre
activo) más adelante desde la misma pantalla del servicio en Render.

---

## Paso 3 — Conectar tu `index.html` con la URL de Render

Como no tengo tu `index.html`, no lo edité directamente. Busca en ese
archivo el `fetch` que llama a `/api/auditar` (probablemente algo como
`fetch('/api/auditar', { method: 'POST', body: formData })`) y cámbialo por
algo así:

```javascript
const AUDIT_SERVICE_URL = "https://auditoria-ia.onrender.com/api/auditar"; // tu URL real de Render
const AUDIT_SHARED_SECRET = "el-mismo-valor-que-pusiste-en-Render"; // el de AUDIT_SHARED_SECRET

const respuesta = await fetch(AUDIT_SERVICE_URL, {
  method: "POST",
  headers: {
    "X-Audit-Secret": AUDIT_SHARED_SECRET,
  },
  body: formData, // el mismo FormData que ya armas con los archivos + totales_json
});
const resultado = await respuesta.json();
```

Puntos importantes:

- El `FormData` debe seguir trayendo el campo de archivos como `archivos`
  (uno o varios) y el campo `totales_json` como texto — igual que antes,
  no cambia el formato, solo la URL destino y el header nuevo.
- Si prefieres no dejar el secreto visible en el HTML/JS del navegador (
  cualquiera que abra las herramientas de desarrollador del navegador lo
  vería), avísame y armamos una alternativa donde tu `main.py` local actúa
  de intermediario (reenvía la petición a Render por detrás, sin exponer el
  secreto al navegador del cajero). Para un uso interno del negocio esto
  suele ser aceptable, pero es tu decisión.

Si me mandas tu `index.html`, te dejo el cambio ya hecho en el archivo en
vez de solo el snippet.

---

## Variables de entorno — resumen

| Variable | Obligatoria | Para qué sirve |
|---|---|---|
| `ANTHROPIC_API_KEY` | Sí | Tu API key de Claude. Sin ella el endpoint responde error de autenticación. |
| `AUDIT_SHARED_SECRET` | Recomendada | Protege el endpoint de que otros lo usen y gasten tu cuota. Si la dejas vacía, el endpoint queda público (verás un warning en los logs de Render recordándotelo). |
| `CORS_ALLOWED_ORIGINS` | No | Por defecto `*` (cualquier origen). Si quieres restringirlo a tu dominio, pon algo como `https://tu-dominio.com`. |

## Probar localmente antes de subir (opcional)

```bash
python3 -m venv venv
source venv/bin/activate      # en Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env          # y edita .env con tus valores reales
export $(cat .env | xargs)    # en Windows usa `set` o un .env loader
uvicorn main:app --reload --port 8080
```

Luego prueba con `curl`:

```bash
curl -X POST http://127.0.0.1:8080/api/auditar \
  -H "X-Audit-Secret: TU_SECRETO" \
  -F "archivos=@comprobante1.jpg" \
  -F 'totales_json={}'
```

## Ver logs / diagnosticar errores

En Render Dashboard, entra al servicio → pestaña **Logs**. Ahí vas a ver lo
mismo que antes veías en `auditoria_ia.log` en tu máquina local (incluyendo
cuántos tokens usó cada auditoría, si Claude devolvió `tool_use`, etc.) —
solo que ahora en vez de un archivo, Render lo captura automáticamente de
la salida estándar del proceso.
