#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py — Microservicio "Auditoría IA" (versión Render)
=========================================================

Este servicio expone SOLAMENTE el endpoint /api/auditar de la app original
"tecnosoft_auditor". Se extrajo a propósito del main.py completo porque el
resto de esa app (sincronización ODBC con A2 Softway, lectura de archivos
.dat locales, base SQLite con documentos/pagos/cierres) SOLO puede correr en
la máquina donde está instalado A2 -- Render no tiene forma de alcanzar ese
driver DBISAM ni esas rutas locales (C:\\lab\\...).

Este microservicio, en cambio, no depende de nada local: recibe los
comprobantes (imágenes/PDF) más los totales del sistema ya calculados por el
frontend, llama a la API de Claude para clasificar los comprobantes, calcula
la reconciliación determinística en Python (igual que en la app original) y
devuelve el JSON de resultado. Tu app local sigue haciendo todo lo demás
(dashboard, sync con A2); solo esta llamada se mueve a la nube.

SEGURIDAD:
- La API key de Anthropic NUNCA va en el código -- se lee de la variable de
  entorno ANTHROPIC_API_KEY (la configuras en Render, no aquí).
- Como este servicio queda con una URL pública, cualquiera que la encuentre
  podría llamarlo y gastar tu cuota de la API de Claude. Por eso se exige un
  header "X-Audit-Secret" que debe coincidir con la variable de entorno
  AUDIT_SHARED_SECRET. Tu frontend (index.html) debe mandar ese mismo valor
  en cada petición -- ver el snippet de integración que te compartí aparte.
"""

import base64
import json
import logging
import os
import traceback
from typing import List

import anthropic
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

# ---------------------------------------------------------------------------
# Logging: en Render no hay disco persistente ni consola visible como en el
# .exe local -- Render captura automáticamente todo lo que se escribe a
# stdout/stderr y lo muestra en la pestaña "Logs" del servicio. Por eso aquí
# NO se escribe a un archivo (a diferencia del main.py original), se deja el
# logging por defecto a stderr.
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("auditoria_ia")

# ---------------------------------------------------------------------------
# Configuración desde variables de entorno (se configuran en Render, sección
# "Environment" del servicio -- ver README.md)
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
AUDIT_SHARED_SECRET = os.environ.get("AUDIT_SHARED_SECRET", "")
# Lista de orígenes permitidos separados por coma (ej. "http://127.0.0.1:8080,https://mi-dashboard.com").
# Si se deja "*", cualquier origen puede llamar al endpoint desde el navegador
# -- aceptable porque igual exigimos el header secreto, pero puedes
# restringirlo más si quieres.
CORS_ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("CORS_ALLOWED_ORIGINS", "*").split(",") if o.strip()]

if not ANTHROPIC_API_KEY:
    logger.warning("ANTHROPIC_API_KEY no está configurada -- /api/auditar fallará hasta que la definas en Render.")
if not AUDIT_SHARED_SECRET:
    logger.warning("AUDIT_SHARED_SECRET no está configurada -- el endpoint quedaría SIN protección. Config\u00farala en Render.")

CLIENTE_IA = anthropic.Anthropic(
    api_key=ANTHROPIC_API_KEY,
    timeout=240.0,
    max_retries=2,
)

app = FastAPI(title="Auditoría IA - servicio de reconciliación")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["*"],
)


def verificar_secreto(x_audit_secret: str = Header(default="")) -> None:
    """Dependencia de FastAPI: exige que la petición traiga el header
    X-Audit-Secret con el mismo valor que AUDIT_SHARED_SECRET. Protege el
    endpoint (que cuesta dinero por cada llamada a la API de Claude) de que
    cualquiera en internet lo use si llega a adivinar/encontrar la URL.

    Se declara como dependencia real de FastAPI (Depends) en la firma de
    /api/auditar -- FastAPI la ejecuta ANTES del cuerpo del endpoint y
    corta la petición con 401 si no coincide, antes de gastar ni un solo
    token en la llamada a Claude."""
    if not AUDIT_SHARED_SECRET:
        # Si el operador no configuró el secreto, se deja pasar (fail-open)
        # para no romper el primer despliegue -- pero queda el warning arriba
        # en los logs recordando configurarlo.
        return
    if x_audit_secret != AUDIT_SHARED_SECRET:
        raise HTTPException(status_code=401, detail="Header X-Audit-Secret ausente o incorrecto.")


# ---------------------------------------------------------------------------
# Reconciliación determinística (idéntica a la del main.py original) — NO
# confiar en la suma que redacta la IA como fuente de verdad para los
# montos: la IA clasifica bien pero comete errores de aritmética al sumar
# varios comprobantes en texto libre. Las sumas por categoría se recalculan
# aquí en Python a partir de "comprobantes_leidos" (datos estructurados).
# ---------------------------------------------------------------------------
TIPO_COMPROBANTE_A_CLAVE_SISTEMA = {
    "Pago Móvil": "Cheque",  # en este sistema A2, Pago Móvil se guarda bajo "Cheque"
    "Transferencia": "Transferencias",
    "Tarjeta de Débito": "Tarjeta de Débito",
    "Tarjeta de Crédito": "Tarjeta de Crédito",
    # OJO: "Efectivo" queda fuera a propósito (ver nota en main.py original).
}

TOLERANCIA_DIFERENCIA_INSIGNIFICANTE_BS = 1.00
TOLERANCIA_DIFERENCIA_INSIGNIFICANTE_USD = 0.05


def _prefijo_moneda(moneda):
    return "$" if moneda == "USD" else "Bs."


def formatear_monto_ve(valor):
    try:
        return f"{float(valor):,.2f}".replace(",", "TEMP").replace(".", ",").replace("TEMP", ".")
    except (TypeError, ValueError):
        return "0,00"


def parsear_monto_ve(valor):
    if valor is None:
        return 0.0
    if isinstance(valor, (int, float)):
        return float(valor)
    texto = str(valor).strip()
    if not texto:
        return 0.0
    try:
        return float(texto.replace(".", "").replace(",", "."))
    except ValueError:
        return 0.0


def _num(valor):
    try:
        return float(valor)
    except (TypeError, ValueError):
        return 0.0


def _describir_fuente_tarjeta(monto_lote, monto_individual):
    if monto_lote > 0 and monto_individual > 0:
        return "cierres de lote + recibos individuales (de otros terminales)"
    if monto_lote > 0:
        return "cierres de lote del terminal"
    return "comprobantes individuales"


def calcular_reconciliacion(comprobantes_leidos, totales_json_str):
    try:
        totales_sistema = json.loads(totales_json_str) if totales_json_str else {}
    except (json.JSONDecodeError, TypeError):
        totales_sistema = {}

    items = [i for i in (comprobantes_leidos or []) if isinstance(i, dict)]

    terminales_cubiertos_por_lote = set()
    suma_lote_debito = 0.0
    suma_lote_credito = 0.0
    for item in items:
        if item.get("tipo") != "Cierre de Lote / Reporte de Cierre":
            continue
        total_credito = _num(item.get("total_fila_credito"))
        total_debito = _num(item.get("total_fila_debito"))
        total_mc_visa_debit = _num(item.get("total_fila_mc_visa_debit"))
        monto_debito = total_debito + total_mc_visa_debit
        monto_credito = total_credito
        if monto_debito <= 0 and monto_credito <= 0:
            continue
        suma_lote_debito += monto_debito
        suma_lote_credito += monto_credito
        terminal = (item.get("terminal_identificador") or "").strip()
        if terminal:
            terminales_cubiertos_por_lote.add(terminal)

    suma_individual_debito = 0.0
    suma_individual_credito = 0.0
    sumas_individuales_otros = {}
    for item in items:
        if item.get("es_resumen_no_cobro"):
            continue
        tipo = item.get("tipo")
        if tipo not in TIPO_COMPROBANTE_A_CLAVE_SISTEMA:
            continue
        monto = _num(item.get("monto"))
        if tipo == "Pago Móvil":
            comision = _num(item.get("comision_pago_movil"))
            total_bruto = _num(item.get("total_pago_movil_bruto"))
            if comision > 0 and total_bruto > 0 and abs(monto - total_bruto) < 0.01:
                monto = round(total_bruto - comision, 2)
        if tipo in ("Tarjeta de Débito", "Tarjeta de Crédito"):
            terminal = (item.get("terminal_identificador") or "").strip()
            if terminal and terminal in terminales_cubiertos_por_lote:
                continue
            if tipo == "Tarjeta de Débito":
                suma_individual_debito += monto
            else:
                suma_individual_credito += monto
        else:
            sumas_individuales_otros[tipo] = sumas_individuales_otros.get(tipo, 0.0) + monto

    suma_cashea_usd = 0.0
    for item in items:
        if item.get("tipo") == "Cashea":
            suma_cashea_usd += _num(item.get("monto"))

    sumas_finales = {
        "Pago Móvil": sumas_individuales_otros.get("Pago Móvil", 0.0),
        "Transferencia": sumas_individuales_otros.get("Transferencia", 0.0),
        "Tarjeta de Débito": suma_lote_debito + suma_individual_debito,
        "Tarjeta de Crédito": suma_lote_credito + suma_individual_credito,
    }
    fuentes = {
        "Pago Móvil": "comprobantes individuales",
        "Transferencia": "comprobantes individuales",
        "Tarjeta de Débito": _describir_fuente_tarjeta(suma_lote_debito, suma_individual_debito),
        "Tarjeta de Crédito": _describir_fuente_tarjeta(suma_lote_credito, suma_individual_credito),
    }

    reconciliacion = {}
    total_general_comprobantes = 0.0
    for tipo, clave_sistema in TIPO_COMPROBANTE_A_CLAVE_SISTEMA.items():
        suma_comprobantes = round(sumas_finales.get(tipo, 0.0), 2)
        fuente = fuentes.get(tipo, "comprobantes individuales")

        info_sistema = totales_sistema.get(clave_sistema, {}) if isinstance(totales_sistema, dict) else {}
        monto_sistema = round(parsear_monto_ve(info_sistema.get("monto_ventas_sistema")), 2)
        diferencia = round(suma_comprobantes - monto_sistema, 2)
        total_general_comprobantes += suma_comprobantes
        cuadra = abs(diferencia) <= TOLERANCIA_DIFERENCIA_INSIGNIFICANTE_BS
        diferencia_insignificante = cuadra and abs(diferencia) >= 0.01
        reconciliacion[tipo] = {
            "clave_sistema": clave_sistema,
            "suma_comprobantes": suma_comprobantes,
            "monto_sistema": monto_sistema,
            "diferencia": diferencia,
            "cuadra": cuadra,
            "diferencia_insignificante": diferencia_insignificante,
            "fuente": fuente,
            "moneda": "Bs",
        }

    if suma_cashea_usd > 0 or (isinstance(totales_sistema, dict) and totales_sistema.get("CASHEA")):
        tasa_dia = parsear_monto_ve(totales_sistema.get("_tasa_dia")) if isinstance(totales_sistema, dict) else 0.0
        info_cashea = totales_sistema.get("CASHEA", {}) if isinstance(totales_sistema, dict) else {}
        monto_cashea_sistema_bs = parsear_monto_ve(info_cashea.get("monto_ventas_sistema"))
        suma_cashea_usd_r = round(suma_cashea_usd, 2)
        if tasa_dia > 0:
            monto_cashea_sistema_usd = round(monto_cashea_sistema_bs / tasa_dia, 2)
            diferencia_cashea = round(suma_cashea_usd_r - monto_cashea_sistema_usd, 2)
            cuadra_cashea = abs(diferencia_cashea) <= TOLERANCIA_DIFERENCIA_INSIGNIFICANTE_USD
            reconciliacion["Cashea"] = {
                "clave_sistema": "CASHEA",
                "suma_comprobantes": suma_cashea_usd_r,
                "monto_sistema": monto_cashea_sistema_usd,
                "diferencia": diferencia_cashea,
                "cuadra": cuadra_cashea,
                "diferencia_insignificante": cuadra_cashea and abs(diferencia_cashea) >= 0.01,
                "fuente": "comprobantes individuales",
                "moneda": "USD",
            }
        else:
            reconciliacion["Cashea"] = {
                "clave_sistema": "CASHEA",
                "suma_comprobantes": suma_cashea_usd_r,
                "monto_sistema": None,
                "diferencia": None,
                "cuadra": True,
                "diferencia_insignificante": False,
                "fuente": "comprobantes individuales (sin tasa del día para comparar contra el sistema)",
                "moneda": "USD",
            }

    categorias_mal = {t: r for t, r in reconciliacion.items() if not r["cuadra"]}
    categorias_redondeo = {t: r for t, r in reconciliacion.items() if r["diferencia_insignificante"]}
    if not categorias_mal:
        veredicto_calculado = "✅ CUADRA: todas las categorías con comprobantes coinciden con el sistema A2."
        if categorias_redondeo:
            notas = [
                f"{tipo} ({_prefijo_moneda(r.get('moneda', 'Bs'))} {formatear_monto_ve(abs(r['diferencia']))})"
                for tipo, r in categorias_redondeo.items()
            ]
            veredicto_calculado += " Diferencias mínimas por redondeo (dentro de tolerancia, no requieren acción): " + ", ".join(notas) + "."
    else:
        partes = []
        for tipo, r in categorias_mal.items():
            signo = "SOBRANTE" if r["diferencia"] > 0 else "FALTANTE"
            prefijo = _prefijo_moneda(r.get("moneda", "Bs"))
            partes.append(
                f"{tipo}: {signo} de {prefijo} {formatear_monto_ve(abs(r['diferencia']))} "
                f"(comprobantes [{r['fuente']}]: {prefijo} {formatear_monto_ve(r['suma_comprobantes'])} vs sistema '{r['clave_sistema']}': "
                f"{prefijo} {formatear_monto_ve(r['monto_sistema'])})"
            )
        veredicto_calculado = "⚠️ DESCUADRE DETECTADO — " + " | ".join(partes)

    return {
        "por_tipo": reconciliacion,
        "total_general_comprobantes_calculado": round(total_general_comprobantes, 2),
        "veredicto_calculado": veredicto_calculado,
    }


# ---------------------------------------------------------------------------
# Definición de la herramienta (tool use de Claude) — idéntica a la original
# ---------------------------------------------------------------------------
HERRAMIENTA_AUDITORIA = {
    "name": "registrar_auditoria",
    "description": "Registra el resultado de la auditoría de comprobantes de pago contra el cuadre de caja.",
    "input_schema": {
        "type": "object",
        "properties": {
            "veredicto_final": {
                "type": "string",
                "description": "Resumen breve de la CLASIFICACIÓN realizada, no una conclusión de cuadre. Ejemplo bueno: 'Se clasificaron 15 comprobantes: 7 Pago Móvil, 1 Tarjeta de Débito individual, 3 cierres de lote de tarjeta, 4 reportes internos del sistema.' PROHIBIDO: no escribas 'CUADRA', 'DESCUADRE', 'FALTANTE', 'SOBRANTE' ni ningún monto de diferencia — no tienes los datos del sistema para calcular eso, lo hace el servidor por separado con datos más precisos.",
            },
            "analisis_detallado": {
                "type": "string",
                "description": "Lista breve de CADA comprobante con SOLO su clasificación: archivo, tipo detectado, y monto extraído (1 línea por comprobante). PROHIBIDO comparar contra el sistema o calcular sumas/diferencias — eso lo hace el servidor por separado. SÉ CONCISO: máximo 1 línea por comprobante.",
            },
            "comprobantes_leidos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "archivo": {"type": "string"},
                        "monto": {"type": "number"},
                        "tipo": {
                            "type": "string",
                            "enum": [
                                "Pago Móvil",
                                "Transferencia",
                                "Tarjeta de Débito",
                                "Tarjeta de Crédito",
                                "Efectivo",
                                "Cierre de Lote / Reporte de Cierre",
                                "Reporte Interno del Sistema (Corte/Cierre X/Z)",
                                "Cashea",
                                "Otro",
                            ],
                        },
                        "es_resumen_no_cobro": {"type": "boolean"},
                        "total_fila_credito": {"type": "number"},
                        "total_fila_debito": {"type": "number"},
                        "total_fila_mc_visa_debit": {"type": "number"},
                        "total_fila_extrafin": {"type": "number"},
                        "terminal_identificador": {"type": "string"},
                        "comision_pago_movil": {"type": "number"},
                        "total_pago_movil_bruto": {"type": "number"},
                    },
                    "required": ["archivo", "monto", "tipo"],
                },
            },
        },
        "required": [
            "veredicto_final",
            "analisis_detallado",
            "comprobantes_leidos",
        ],
    },
}

# El prompt del auditor es largo y NO cambió respecto al main.py original --
# se importa desde audit_prompt.py para no duplicar/perder detalle aquí.
from audit_prompt import PROMPT_AUDITOR_TEMPLATE  # noqa: E402


@app.get("/")
def salud():
    """Endpoint simple para confirmar que el servicio está arriba (Render lo
    usa también como health check si lo configuras)."""
    return {"status": "ok", "servicio": "auditoria-ia"}


@app.post("/api/auditar", dependencies=[Depends(verificar_secreto)])
async def auditar_comprobantes(
    archivos: List[UploadFile] = File(...),
    totales_json: str = Form(...),
):
    logger.info("Recibida solicitud /api/auditar con %d archivo(s)", len(archivos))
    try:
        content_blocks = []
        archivos_procesados = []

        for indice, archivo in enumerate(archivos, start=1):
            contenido_bytes = await archivo.read()
            base64_encoded = base64.b64encode(contenido_bytes).decode("utf-8")
            mime_type = archivo.content_type

            if mime_type.startswith("image/"):
                tipo_bloque = "image"
            elif mime_type == "application/pdf":
                tipo_bloque = "document"
            else:
                continue

            content_blocks.append({
                "type": "text",
                "text": f'--- Archivo #{indice} de {len(archivos)}: nombre exacto = "{archivo.filename}" ---'
            })
            content_blocks.append({
                "type": tipo_bloque,
                "source": {"type": "base64", "media_type": mime_type, "data": base64_encoded}
            })
            archivos_procesados.append(archivo.filename)

        if not content_blocks:
            return {"status": "error", "message": "No se encontraron formatos de imagen o PDF válidos."}

        prompt_auditor = PROMPT_AUDITOR_TEMPLATE.format(n_archivos=len(archivos))
        content_blocks.append({"type": "text", "text": prompt_auditor})

        peso_mb = sum(len(b["source"]["data"]) for b in content_blocks if b["type"] in ("image", "document")) / 1_000_000
        logger.info("Payload de %d bloque(s), ~%.2f MB en base64. Llamando a la API de Anthropic...", len(content_blocks), peso_mb)

        try:
            respuesta = CLIENTE_IA.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=16000,
                temperature=0.0,
                tools=[HERRAMIENTA_AUDITORIA],
                tool_choice={"type": "tool", "name": "registrar_auditoria"},
                messages=[{"role": "user", "content": content_blocks}],
            )
        except anthropic.APIConnectionError as e:
            logger.error("Error de CONEXIÓN de red hacia Anthropic: %s", e)
            return {"status": "error", "message": "No se pudo conectar con el servidor de IA. Detalle: " + str(e)}
        except anthropic.AuthenticationError as e:
            logger.error("Error de AUTENTICACIÓN (API key inválida o sin créditos): %s", e)
            return {"status": "error", "message": "La API key de Anthropic no es válida o no tiene acceso. Detalle: " + str(e)}
        except anthropic.RateLimitError as e:
            logger.error("Rate limit alcanzado: %s", e)
            return {"status": "error", "message": "Se alcanzó el límite de solicitudes a la IA. Intenta de nuevo en unos segundos."}
        except anthropic.APIStatusError as e:
            logger.error("La API de Anthropic respondió con error %s: %s", e.status_code, e.response.text)
            return {"status": "error", "message": f"La IA respondió con error {e.status_code}: {e.message}"}

        logger.info(
            "Respuesta recibida de Anthropic. stop_reason=%s, tokens_entrada=%s, tokens_salida=%s",
            respuesta.stop_reason, respuesta.usage.input_tokens, respuesta.usage.output_tokens
        )

        if respuesta.stop_reason == "max_tokens":
            logger.error("La respuesta se CORTÓ por exceder max_tokens.")
            return {
                "status": "error",
                "message": "La IA se quedó sin espacio de respuesta (demasiados comprobantes en un solo lote). "
                            "Intenta subir menos archivos a la vez."
            }

        bloque_tool = next((b for b in respuesta.content if b.type == "tool_use"), None)
        if bloque_tool is None:
            logger.warning("Claude no devolvió tool_use. Contenido crudo: %s", respuesta.content)
            return {
                "status": "error",
                "message": "Claude no devolvió una respuesta estructurada.",
                "raw": [b.model_dump() for b in respuesta.content]
            }
        datos_auditoria = bloque_tool.input

        campos_faltantes = [
            campo for campo in ("veredicto_final", "analisis_detallado", "comprobantes_leidos")
            if not datos_auditoria.get(campo)
        ]
        if campos_faltantes:
            logger.warning("La respuesta de la IA vino incompleta. Campos vacíos/faltantes: %s", campos_faltantes)

        comprobantes = datos_auditoria.get("comprobantes_leidos") or []
        archivos_mencionados = {c.get("archivo") for c in comprobantes if isinstance(c, dict)}
        archivos_faltantes = [a for a in archivos_procesados if a not in archivos_mencionados]

        if archivos_faltantes:
            logger.warning(
                "La IA devolvió %d comprobante(s) pero se subieron %d archivo(s). Faltan: %s",
                len(comprobantes), len(archivos_procesados), archivos_faltantes
            )

        datos_auditoria["archivos_evaluados"] = archivos_procesados
        datos_auditoria["archivos_no_analizados"] = archivos_faltantes

        reconciliacion = calcular_reconciliacion(comprobantes, totales_json)
        datos_auditoria["reconciliacion_calculada"] = reconciliacion
        logger.info("Reconciliación calculada (Python): %s", reconciliacion)

        mensaje = "Auditoría IA completada con éxito"
        if campos_faltantes:
            mensaje = "Auditoría completada, pero con campos incompletos: " + ", ".join(campos_faltantes)
        if archivos_faltantes:
            mensaje += f" | ATENCIÓN: {len(archivos_faltantes)} archivo(s) subido(s) NO aparecen en el análisis: " + ", ".join(archivos_faltantes)

        return {"status": "success", "message": mensaje, "data": datos_auditoria}

    except Exception as e:
        logger.error("Excepción no controlada en /api/auditar: %s\n%s", e, traceback.format_exc())
        return {"status": "error", "message": str(e)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
