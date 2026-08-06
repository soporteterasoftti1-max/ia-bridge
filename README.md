# Auditoría IA — versión Vercel (con subida directa a Vercel Blob)

Esta carpeta despliega, en un solo proyecto de Vercel, tres cosas:

1. **`index.html`** — tu dashboard tal cual, con la sección de "Auditoría IA" adaptada.
2. **`api/auditar.py`** (Python) — recibe URLs de Vercel Blob, descarga cada comprobante, llama a Claude, calcula la reconciliación y responde. Reemplaza al `/api/auditar` de tu `main.py` original.
3. **`api/blob-upload-token.js`** (Node.js) — autoriza y genera los tokens que el navegador necesita para subir cada foto DIRECTO a Vercel Blob (sin pasar por tu servidor), porque las funciones de Vercel no aceptan peticiones de más de 4.5 MB y varias fotos de recibos fácilmente pesan más que eso juntas.

El resto de tu app (sincronización ODBC con A2, SQLite, dashboard de cuadre) se queda corriendo donde está hoy — nada de eso se puede mover a la nube, porque depende de leer archivos y el driver ODBC de la máquina donde está instalado A2. Aquí solo se mueve la auditoría con IA.

## ⚠️ Antes de empezar: dos cosas que debes saber

**1. Uso comercial en el plan Hobby (gratis) de Vercel.** Las condiciones de uso del plan Hobby de Vercel lo restringen a "uso personal, no comercial". Este es un negocio real (Tera Suministros / Sunmarket), así que técnicamente este uso cae fuera de esas condiciones en el plan gratis. En la práctica, para un uso interno de bajo tráfico como este (auditorías esporádicas, no un producto público) es poco probable que Vercel lo note o actúe, pero es información que debes tener para decidir con conocimiento — no te lo quería ocultar. Si en algún momento te preocupa, la alternativa es el plan Pro (de pago).

**2. Los comprobantes pasan por Vercel Blob como archivos "públicos" (aunque con nombre imposible de adivinar).** Se borran automáticamente apenas termina cada auditoría (ver `_borrar_blobs_best_effort` en `api/auditar.py`), pero mientras dura el proceso (segundos a minutos), cualquiera que consiguiera esa URL exacta podría verlos. Para este caso de uso (recibos de pago, no documentos de identidad) es un riesgo bajo, pero quería que lo supieras.

---

## Qué vas a hacer

1. Crear un repositorio en GitHub con estos archivos.
2. Crear el proyecto en Vercel Dashboard, importando ese repo.
3. Crear un Blob Store y conectarlo al proyecto (un clic, Vercel agrega la variable de entorno solo).
4. Configurar tu API key de Anthropic y un secreto inventado por ti.
5. Editar una línea de `index.html` con ese mismo secreto, y desplegar.

---

## Paso 1 — Crear el repositorio en GitHub

1. Entra a https://github.com/new y crea un repositorio (puede ser privado). Nómbralo, por ejemplo, `auditoria-ia-vercel`. No marques "Add a README".
2. En tu computadora, en la carpeta con estos archivos (`index.html`, `package.json`, `requirements.txt`, `vercel.json`, `.gitignore`, `api/auditar.py`, `api/blob-upload-token.js`, `api/audit_prompt.py`, `README.md`), corre:

   ```bash
   git init
   git add .
   git commit -m "Auditoría IA en Vercel"
   git branch -M main
   git remote add origin https://github.com/TU-USUARIO/auditoria-ia-vercel.git
   git push -u origin main
   ```

---

## Paso 2 — Crear el proyecto en Vercel Dashboard

1. Entra a https://vercel.com/new (te pedirá iniciar sesión, puedes hacerlo con tu cuenta de GitHub — **no pide tarjeta**).
2. Autoriza el acceso a GitHub si es la primera vez, y selecciona el repo `auditoria-ia-vercel`.
3. Vercel va a detectar automáticamente que hay una función Python (`api/auditar.py`) y una función Node (`api/blob-upload-token.js`). No necesitas tocar el Build Command ni el Output Directory — déjalos en blanco/por defecto.
4. **Todavía no le des a Deploy.** Abre la sección **Environment Variables** en esta misma pantalla y agrega:

   | Key | Value |
   |---|---|
   | `ANTHROPIC_API_KEY` | tu API key real de Anthropic (empieza con `sk-ant-...`) |
   | `AUDIT_SHARED_SECRET` | inventa una cadena larga y aleatoria (ej. genera una con `openssl rand -hex 32`) |

   ⚠️ Si la API key que tenías en tu `main.py` original (la que compartiste conmigo) sigue activa, **revócala** desde https://console.anthropic.com/settings/keys y genera una nueva — quedó expuesta en el código.

5. Ahora sí, click en **Deploy**. Toma 1-2 minutos.

---

## Paso 3 — Crear el Blob Store y conectarlo

1. Con el proyecto ya creado, entra a su página en Vercel y abre la pestaña **Storage** en el menú lateral.
2. Click en **Create Database** → elige **Blob**.
3. Dale un nombre (ej. "comprobantes") y **Create**.
4. Cuando te pregunte a qué proyecto conectarlo, selecciona `auditoria-ia-vercel` (el que acabas de crear), incluyendo los entornos **Production** y **Preview**.
5. Esto agrega automáticamente la variable de entorno `BLOB_READ_WRITE_TOKEN` a tu proyecto — no tienes que escribirla tú.
6. Como esta variable se agregó DESPUÉS del primer deploy, tienes que volver a desplegar para que la función la vea: ve a la pestaña **Deployments**, abre el menú (⋯) del último deploy, y elige **Redeploy**.

---

## Paso 4 — Poner el secreto en `index.html` y desplegar

1. Abre `index.html` en tu editor y busca esta línea (cerca del inicio del segundo `<script>`):

   ```javascript
   const AUDIT_SHARED_SECRET = "PON_AQUI_EL_MISMO_SECRETO_QUE_CONFIGURASTE_EN_VERCEL";
   ```

2. Reemplaza el texto entre comillas por el MISMO valor exacto que pusiste en el paso 2 en la variable de entorno `AUDIT_SHARED_SECRET` de Vercel.
3. Guarda, y sube el cambio:

   ```bash
   git add index.html
   git commit -m "Configurar secreto de auditoría"
   git push
   ```

   Vercel despliega automáticamente cada vez que subes cambios a `main`.

---

## Paso 5 — Probar

1. Abre la URL que te dio Vercel (algo como `https://auditoria-ia-vercel.vercel.app`).
2. En la sección "📄 Auditoría IA", selecciona una o dos fotos de comprobantes y click en "Subir y Analizar".
3. Deberías ver primero "⏳ Subiendo comprobante 1 de 2..." y luego "⏳ Claude está analizando...".
4. Si algo falla, abre la consola del navegador (F12 → pestaña Console) — los mensajes de error ahí son más específicos que el texto que se muestra en pantalla.

### Si ves un error al cargar la página relacionado con "module" o "Failed to resolve"

Es la línea que importa la función de subida a Vercel Blob desde un CDN. Abre `index.html`, busca:

```javascript
import { upload } from 'https://esm.sh/@vercel/blob/client';
```

y cámbiala por:

```javascript
import { upload } from 'https://cdn.jsdelivr.net/npm/@vercel/blob@2/+esm';
```

Guarda, sube el cambio (`git add`, `commit`, `push`) y prueba de nuevo. (No pude probar esta línea con una cuenta real de Vercel desde aquí, así que si falla de otra forma distinta, mándame el error exacto de la consola y lo resolvemos.)

### Si ves "Secreto de auditoría ausente o incorrecto" o error HTTP 401

El valor de `AUDIT_SHARED_SECRET` en `index.html` no coincide EXACTO (mayúsculas, espacios) con el que pusiste en Vercel → Settings → Environment Variables.

---

## Ver logs / diagnosticar errores

En Vercel Dashboard, entra a tu proyecto → pestaña **Logs** (o dentro de un deployment específico → **Functions**). Ahí aparece lo mismo que antes veías en `auditoria_ia.log`: cuántos tokens usó cada auditoría, si Claude devolvió `tool_use`, cualquier error de conexión, etc. Puedes filtrar por función (`api/auditar` o `api/blob-upload-token`).

## Probar localmente (opcional, requiere Vercel CLI)

```bash
npm i -g vercel
vercel login
vercel link                 # conecta esta carpeta a tu proyecto de Vercel
vercel env pull .env.local  # trae las variables de entorno reales (incluye BLOB_READ_WRITE_TOKEN)
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
npm install
vercel dev
```

Luego abre `http://localhost:3000`.

## Variables de entorno — resumen

| Variable | Quién la agrega | Para qué sirve |
|---|---|---|
| `ANTHROPIC_API_KEY` | Tú, a mano | Tu API key de Claude. |
| `AUDIT_SHARED_SECRET` | Tú, a mano (y debe coincidir con `index.html`) | Protege ambos endpoints (`/api/auditar` y `/api/blob-upload-token`) de que otros los usen y gasten tu cuota / llenen tu almacenamiento. |
| `BLOB_READ_WRITE_TOKEN` | Vercel, automático al conectar el Blob Store | Permite a `api/auditar.py` y `api/blob-upload-token.js` leer/escribir/borrar en tu Blob Store. |
