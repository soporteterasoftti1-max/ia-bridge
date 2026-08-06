// api/blob-upload-token.js
// ==========================
// Función Node.js (Vercel Function) que autoriza y genera los tokens que el
// navegador necesita para subir cada comprobante DIRECTO a Vercel Blob, sin
// pasar por el límite de 4.5 MB de las funciones normales.
//
// Vive aparte de api/auditar.py (que es Python) porque este "handshake" de
// tokens de Vercel Blob solo está disponible en el SDK de JavaScript
// (@vercel/blob) -- no existe todavía un equivalente oficial en Python. Cada
// archivo dentro de /api se despliega como su propia función independiente,
// así que Python y Node conviven sin problema en el mismo proyecto: las
// peticiones a /api/auditar van a auditar.py, y las de
// /api/blob-upload-token van a este archivo.
//
// Seguridad: el navegador manda el mismo secreto compartido (AUDIT_SHARED_
// SECRET) dentro de "clientPayload" -- si no coincide, se rechaza la
// generación del token ANTES de darle acceso al Blob store. Sin esto,
// cualquiera que encontrara esta URL podría llenar tu almacenamiento gratis
// de Vercel Blob subiendo archivos.

import { handleUpload } from '@vercel/blob/client';

export default async function handler(request) {
  if (request.method !== 'POST') {
    return new Response(JSON.stringify({ error: 'Method not allowed' }), {
      status: 405,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  const body = await request.json();

  try {
    const jsonResponse = await handleUpload({
      body,
      request,
      onBeforeGenerateToken: async (pathname, clientPayload) => {
        const secretoEsperado = process.env.AUDIT_SHARED_SECRET || '';
        if (secretoEsperado) {
          let secretoRecibido = '';
          try {
            secretoRecibido = JSON.parse(clientPayload || '{}').secret || '';
          } catch (e) {
            secretoRecibido = '';
          }
          if (secretoRecibido !== secretoEsperado) {
            throw new Error('Secreto de auditoría ausente o incorrecto.');
          }
        }

        return {
          // Solo imágenes y PDF -- lo mismo que ya validaba /api/auditar.
          allowedContentTypes: [
            'image/jpeg',
            'image/png',
            'image/webp',
            'image/heic',
            'image/heif',
            'application/pdf',
          ],
          addRandomSuffix: true,
          // 15 MB por archivo es de sobra para una foto de recibo; evita que
          // alguien suba archivos gigantes a tu Blob store gratis.
          maximumSizeInBytes: 15 * 1024 * 1024,
          // 'public': la URL resultante no aparece listada en ningún lado,
          // pero cualquiera que la adivine/consiga podría verla (el sufijo
          // aleatorio la hace prácticamente imposible de adivinar). Se borra
          // automáticamente después de cada auditoría (ver auditar.py).
          access: 'public',
          tokenPayload: JSON.stringify({}),
        };
      },
      onUploadCompleted: async () => {
        // No necesitamos hacer nada aquí: el navegador ya sabe la URL final
        // apenas termina cada subida (la devuelve la propia función upload())
        // y es él quien arma la lista de comprobantes para /api/auditar.
        // Este callback solo debe existir para que handleUpload no falle.
      },
    });

    return new Response(JSON.stringify(jsonResponse), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  } catch (error) {
    return new Response(JSON.stringify({ error: error.message }), {
      // El SDK reintenta 5 veces si no ve un 200 en la llamada de
      // confirmación (onUploadCompleted) -- para el rechazo de autenticación
      // en onBeforeGenerateToken, 400 es correcto y no genera reintentos ahí.
      status: 400,
      headers: { 'Content-Type': 'application/json' },
    });
  }
}
