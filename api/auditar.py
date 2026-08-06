#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
api/auditar.py — Microservicio "Auditoría IA" (versión Vercel, archivo único)
==============================================================================

Vercel convierte cada archivo .py dentro de la carpeta api/ en su propia
Vercel Function, mapeada automáticamente a la URL que coincide con la ruta
del archivo (este archivo -> /api/auditar). Por eso este proyecto debe vivir
en tu repo exactamente en: api/auditar.py

IMPORTANTE -- por qué este archivo es autocontenido (sin imports propios):
Vercel empaqueta cada función de /api/ por separado y, en pruebas, no
siempre incluye módulos hermanos (ej. audit_prompt.py) en el mismo paquete,
lo que producía "ModuleNotFoundError: No module named 'audit_prompt'". Para
evitar ese problema de raíz, aquí NO se importa nada que no sea una
librería instalada (fastapi, anthropic, etc.) -- el prompt completo del
auditor vive como una constante dentro de este mismo archivo.

Qué hace este servicio: recibe los comprobantes (imágenes/PDF) más los
totales del sistema ya calculados por el frontend, llama a la API de Claude
para clasificarlos, calcula la reconciliación determinística en Python
(igual que en el main.py original de la app de escritorio/Render) y
devuelve el JSON de resultado. El resto de la app (dashboard, sync ODBC con
A2, SQLite) NO vive aquí -- sigue corriendo donde ya estaba.

SEGURIDAD:
- La API key de Anthropic NUNCA va en el código -- se lee de la variable de
  entorno ANTHROPIC_API_KEY (Vercel Dashboard -> Environment Variables).
- El endpoint exige un header "X-Audit-Secret" igual a la variable de
  entorno AUDIT_SHARED_SECRET, para que nadie más gaste tu cuota de Claude
  llamando a esta URL pública. Tu index.html debe mandar ese mismo valor.
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
# Logging: Vercel captura stdout/stderr automáticamente y lo muestra en la
# pestaña "Logs" del proyecto -- no se escribe a un archivo local.
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("auditoria_ia")

# ---------------------------------------------------------------------------
# Configuración desde variables de entorno (Vercel Dashboard -> tu proyecto
# -> Settings -> Environment Variables)
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
AUDIT_SHARED_SECRET = os.environ.get("AUDIT_SHARED_SECRET", "")
CORS_ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("CORS_ALLOWED_ORIGINS", "*").split(",") if o.strip()]

if not ANTHROPIC_API_KEY:
    logger.warning("ANTHROPIC_API_KEY no está configurada -- /api/auditar fallará hasta que la definas en Vercel.")
if not AUDIT_SHARED_SECRET:
    logger.warning("AUDIT_SHARED_SECRET no está configurada -- el endpoint quedaría SIN protección. Configúrala en Vercel.")

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
    allow_methods=["POST", "OPTIONS", "GET"],
    allow_headers=["*"],
)


def verificar_secreto(x_audit_secret: str = Header(default="")) -> None:
    """Exige que la petición traiga el header X-Audit-Secret igual a
    AUDIT_SHARED_SECRET. Corta con 401 ANTES de gastar un solo token
    llamando a Claude si no coincide."""
    if not AUDIT_SHARED_SECRET:
        return
    if x_audit_secret != AUDIT_SHARED_SECRET:
        raise HTTPException(status_code=401, detail="Header X-Audit-Secret ausente o incorrecto.")


# ---------------------------------------------------------------------------
# Reconciliación determinística (idéntica a la del main.py original) — NO
# confiar en la suma que redacta la IA como fuente de verdad para los
# montos.
# ---------------------------------------------------------------------------
TIPO_COMPROBANTE_A_CLAVE_SISTEMA = {
    "Pago Móvil": "Cheque",
    "Transferencia": "Transferencias",
    "Tarjeta de Débito": "Tarjeta de Débito",
    "Tarjeta de Crédito": "Tarjeta de Crédito",
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

# ---------------------------------------------------------------------------
# Prompt del auditor -- EXACTAMENTE el mismo texto de la app original.
# Va inline (no en un archivo aparte) a propósito: ver nota al inicio del
# archivo sobre por qué el import cruzado fallaba en Vercel.
# ---------------------------------------------------------------------------
PROMPT_AUDITOR_TEMPLATE = """
Eres un auditor contable experto en comprobantes de pago venezolanos (POS bancario, pago móvil, transferencias).
Sé estricto, detallista, y NUNCA confundas un tipo de documento con otro por parecido de palabras.

TU ÚNICO TRABAJO ES CLASIFICAR Y EXTRAER DATOS — NO CALCULES NI DECLARES DESCUADRES, FALTANTES,
SOBRANTES NI NINGUNA COMPARACIÓN CONTRA EL SISTEMA A2. No se te está mostrando el cuadre de caja del
sistema a propósito: esa comparación la hace por separado un cálculo determinístico en el servidor,
que es más confiable que cualquier suma/comparación que redactes en texto libre. Tu "veredicto_final"
y "analisis_detallado" son solo un resumen de QUÉ viste y CÓMO lo clasificaste — nunca una conclusión
de si algo cuadra o no, ni un monto de diferencia. Si escribes una frase como "DESCUADRE DETECTADO"
o "FALTANTE DE X BS", eso es un error de tu parte: no tienes la información del sistema para saber
eso, y confundirá al usuario que sí puede ver la comparación real más abajo en pantalla.

Vas a recibir varias imágenes de comprobantes. Cada una puede ser UNA de estas categorías.
Usa EXCLUSIVAMENTE las palabras/estructura del documento (no el contexto general) para clasificar:

1) TARJETA DE DÉBITO o CRÉDITO (compra en punto de venta):
   - Encabezado con nombre del banco emisor (ej: "BANCO DE VENEZUELA", "BANCRECER") y el comercio afiliado.
   - Contiene explícitamente "RECIBO DE COMPRA DEBITO" o "RECIBO DE COMPRA CREDITO".
   - Tiene: AID, campo "T:" (terminal) y "L:" (lote), "APRO:"/"APROB:", "REF:", "TRACE:", y la línea
     "MONTO A PAGAR Bs. X" o similar, asociada a UNA sola compra.
   - Si dice "MAESTRO" o "VISA"/"MASTERCARD" + número de tarjeta enmascarado → Débito o Crédito según corresponda.
   - Llena "terminal_identificador" copiando el banco + el número junto a "T:" (ej: "BDV T:00445215"), tal
     cual aparece en el recibo. El banco SIEMPRE es la entidad bancaria del encabezado (Banco de
     Venezuela, Bancrecer, Banesco, etc.), NUNCA "TERA" ni ningún nombre del propio comercio — el
     comercio es el afiliado, no el banco. Si el nombre del banco no es legible en la foto, usa solo
     el número junto a "T:"/"L:" (ej: "T:2002 L:137") sin inventar un nombre de banco. Si ni el
     banco ni el número de terminal son legibles, deja "terminal_identificador" vacío — nunca lo
     rellenes con un nombre de negocio o un dato que no esté impreso en el documento.

2) CIERRE DE LOTE / REPORTE DE CIERRE (NO es un cobro nuevo, es un resumen del terminal):
   - ⚠️ ANTES QUE NADA, DISTINGUE ESTO: esta categoría (2) es EXCLUSIVAMENTE para cierres impresos
     POR UN BANCO (Banco de Venezuela, Bancrecer, Banesco, Mercantil, etc.) sobre SU terminal de
     tarjeta. La forma más confiable de saberlo: el encabezado del documento dice el NOMBRE DE UN
     BANCO como emisor del documento (ej: "Bancrecer, S.A. Banco Microfinanciero", "BANCO DE
     VENEZUELA"), y más abajo aparece "TERA SUMINISTROS"/"TERA REFRIGERACIÓN"/"TERA SOLUCIONES" solo
     como el COMERCIO AFILIADO (cliente del banco), no como el emisor.
     MUY DISTINTO es un "REPORTE INTERNO DEL SISTEMA": un ticket donde el ENCABEZADO/EMISOR del
     documento es "TERA SUMINISTROS C.A." o "TERA SOLUCIONES C.A." (el propio negocio, NO un banco),
     con campos como "Corte de Caja (X)", "Cierre de Caja (Z)", "Reporte X", "Reporte Z", "Documento
     No Fiscal", "Usuario:", "Equipo:", "No.Estación:", "Turno:" — y que generalmente lista VARIOS
     medios de pago juntos en una sola tabla "Formas de Pago"/"Detalle de Pagos" (ej: Tarjeta Débito +
     Cheques + Efectivo + CASHEA, todos en el mismo ticket). Esto es un reporte que el propio negocio
     se imprime a sí mismo para su cuadre físico — NO es una confirmación bancaria independiente, y
     sus cifras normalmente YA están incluidas en el "Estado actual del Cuadre de Caja" (JSON) de
     arriba, así que compararlo contra sí mismo sería circular. Clasifica ESTOS como tipo "Reporte
     Interno del Sistema (Corte/Cierre X/Z)", es_resumen_no_cobro = true, monto = el total general
     del ticket (solo como referencia) — y DEJA "total_fila_credito"/"total_fila_debito"/
     "total_fila_mc_visa_debit"/"total_fila_extrafin" EN 0/vacíos, NUNCA los llenes para este tipo,
     aunque el ticket muestre una fila "Tarjeta Débito". Estos
     tickets suelen tener partes borrosas o tapadas por sombra en la foto — si no puedes leer un
     campo con certeza, NO lo inventes ni lo confundas con el total general de ventas del ticket.
   - Frases clave (de un cierre BANCARIO real, categoría 2): "REPORTE DE CIERRE", "CIERRE DEBITO",
     "TRANSMISIÓN DE LOTE", "LOTE ACEPTADO", "LOTE CREDITO nro", "LOTE DEBITO nro", "MONTO TOTAL".
   - Estructura de TABLA con columnas "Compra / Anulada / Total" para Crédito, Débito y MC/Visa Debit.
   - IMPORTANTE: "TRANSMISIÓN DE LOTE" y "LOTE" **NO SIGNIFICAN "TRANSFERENCIA"**. Es la palabra
     bancaria para el cierre/consolidado de las ventas del día en el terminal, aunque se parezca
     fonéticamente. Si ves "LOTE" o "TRANSMISIÓN", clasifica como "Cierre de Lote / Reporte de Cierre"
     y marca "es_resumen_no_cobro": true. Este monto normalmente ya está incluido en los recibos de
     compra individuales del mismo terminal — NO lo sumes de nuevo al total, solo úsalo para verificar
     que el cierre coincide con la suma de las compras individuales de ese terminal.
   - OBLIGATORIO para cierres de lote de TARJETA (débito/crédito): NO decidas ni combines nada tú
     mismo. Estos cierres tienen hasta 4 secciones/filas independientes, cada una con su propio
     "Terminal ... nro" y "Total ...". Tu única tarea es una TRANSCRIPCIÓN literal, fila por fila,
     SIN interpretar cuál es "la categoría del documento": copia cada uno de estos 4 números tal
     cual aparecen impresos (usa 0 si esa fila no existe en el documento):
         • "total_fila_credito"          = el número junto a "Total Credito" (fila "Terminal Credito nro")
         • "total_fila_debito"           = el número junto a "Total Débito" (fila "Terminal Débito nro", SIN "MC/Visa" en el nombre)
         • "total_fila_mc_visa_debit"    = el número junto a "Total MC/Visa" (fila "Terminal MC/Visa Debit nro")
         • "total_fila_extrafin"         = el número junto a "Total ExtraFin" (fila "Terminal ExtraFin nro"), si existe
     El sistema (no tú) decide después cuáles de estas 4 filas cuentan como débito o crédito — por
     eso es CRÍTICO que copies las 4 de forma independiente y literal, sin mezclarlas ni sumarlas
     entre sí, y sin usar el número de terminal para "adivinar" a cuál fila pertenece un monto:
     cada fila tiene su PROPIA etiqueta ("Total Credito"/"Total Débito"/"Total MC/Visa"/"Total
     ExtraFin") impresa junto a su número — usa esa etiqueta, no el número de terminal, para saber
     en qué campo va cada cifra. Ejemplo real de un cierre con 4 secciones:
         Terminal Credito nro 00001002 / Compra Credito 0 / Total Credito Bs. 0,00
         Terminal Débito nro 00002002 / Compra Débito 0 / Total Débito Bs. 0,00
         Terminal MC/Visa Debit nro 00001002 / Compra MC/Visa 1 / Total MC/Visa Bs. 3.161,90
         Terminal ExtraFin nro 00005002 / Total ExtraFin Bs. 0,00
         MONTO TOTAL: Bs. 3.161,90
     La transcripción CORRECTA de este ejemplo es: total_fila_credito=0, total_fila_debito=0,
     total_fila_mc_visa_debit=3161.90, total_fila_extrafin=0 — cada número copiado exactamente de
     SU PROPIA fila, sin importar que el número de terminal "00001002" se repita en dos filas.
     SEGUNDO EJEMPLO — formato "REPORTE DE CIERRE" (distinto al formato de tabla de arriba, pero
     la misma regla aplica): a veces el documento trae un título/encabezado general como este:
         REPORTE DE CIERRE
         BANCO DE VENEZUELA ... TERA SUMINISTROS ...
         CIERRE CREDITO T:1002 L:498
         MASTER/VISA DEBITO T:1002 L:498
         APROBADO
         TARJETA CREDITO
         COMPRA 0  Bs. 0,00
         ANULACION 0  Bs. 0,00
         TOTAL 0  Bs. 0,00
         MASTER/VISA DEBITO
         COMPRA 1  Bs. 7.836,68
         ANULACION 0  Bs. 0,00
         TOTAL 1  Bs. 7.836,68
     ⚠️ El título dice "CIERRE CREDITO" — pero eso es SOLO el identificador del reporte/terminal, NO
     el tipo de tarjeta del monto. La sección "TARJETA CREDITO" está en Bs. 0,00 (vacía, ignorar). El
     monto real, Bs. 7.836,68, está bajo la sección "MASTER/VISA DEBITO" — que es DÉBITO. La
     transcripción CORRECTA es: total_fila_credito=0, total_fila_debito=0,
     total_fila_mc_visa_debit=7836.68. Poner este monto en total_fila_credito solo porque el título
     del documento dice "CIERRE CREDITO" es INCORRECTO — es el error más común en este formato de
     documento. SIEMPRE mira la etiqueta de la SECCIÓN donde está el número (TARJETA CREDITO vs.
     MASTER/VISA DEBITO / TARJETA DEBITO), nunca el título general del documento.
     Cada comprobante se clasifica usando SOLO los datos impresos en SU PROPIA imagen — nunca copies
     un nombre de banco, terminal, o cifra que viste en otra imagen del mismo lote de archivos.
     También llena "terminal_identificador" igual que en la categoría (1) (banco + número junto a
     "T:"), para que el sistema pueda emparejar este cierre con el/los recibo(s) individual(es) del
     MISMO terminal (si los subiste) y no contarlos dos veces. Esto es importante: muchos comercios
     NO fotografían cada recibo de compra individual de tarjeta, solo el cierre de lote de cada
     terminal al final del día — ese cierre por sí solo ya representa el total de TODAS las compras
     de ese terminal en el día.
   - CASHEA (financiamiento/BNPL) es un caso APARTE — usa su PROPIO tipo "Cashea" (NO "Cierre de Lote /
     Reporte de Cierre", aunque también sea un resumen consolidado del día): suele ser un screenshot de
     navegador con encabezado "Historial" y un panel "Cierre de caja del día:" que muestra: Sucursal,
     Órdenes del día, Total facturado, Total financiado por Cashea (en $).
     ⚠️ CAMPO CORRECTO A USAR: "Total financiado por Cashea" — este es el dinero que Cashea le paga
     al comercio, y es el único que se compara contra el sistema. "Total facturado" es OTRO número
     (el valor total de la venta al cliente final, incluyendo lo que el cliente pagó de inicial) — NO
     lo uses, aunque esté más arriba o se vea más prominente en la pantalla. Confundir estos dos
     campos es un error común: por ejemplo, en un panel con "Total facturado: $175.34" y "Total
     financiado por Cashea: $105.20", el monto correcto es 105.20, NO 175.34.
     tipo = "Cashea", es_resumen_no_cobro = true, monto = el número junto a "Total financiado por
     Cashea" (siempre en dólares $). El panel lateral "Historial" de esa misma pantalla lista cierres
     de OTROS días anteriores — ignóralo por completo, solo te interesa el panel principal "Cierre de
     caja del día" cuya fecha coincide con la fecha del comprobante que se está auditando.
     NO clasifiques esta pantalla como "Otro" ni como "Cierre de Lote / Reporte de Cierre" — usa
     siempre el tipo dedicado "Cashea".

3) PAGO MÓVIL:
   - ⚠️ NO exijas que el texto diga literalmente "Pago Móvil": cada banco le pone su propio nombre
     comercial a su función de pago móvil (ej. "Tpago" de Banesco, "Pago Móvil BDV", "C-Móvil" de
     Mercantil, etc.). El texto puede decir cosas como "¡Listo! Tu Tpago fue exitoso" y SIGUE siendo
     categoría (3).
   - LA REGLA ESTRUCTURAL QUE SÍ DECIDE (más confiable que el nombre comercial): fíjate en cómo se
     identifica al BENEFICIARIO/DESTINO del pago:
         • Si el beneficiario/destino se identifica con un NÚMERO DE TELÉFONO (04XX-XXXXXXX) + cédula
           o RIF (ej: "Beneficiario: Tera Suministros Ca - 0412-1766662"), es PAGO MÓVIL (categoría 3),
           sin importar qué palabra use el título de la pantalla. El campo "Cuenta origen" que a veces
           aparece es la cuenta del CLIENTE que paga (el emisor), no del comercio — no lo confundas con
           una cuenta destino.
         • Si el destino se identifica con un NÚMERO DE CUENTA BANCARIA completo o parcial (ej: "Cuenta
           destino: 0102-****-**-1234"), es TRANSFERENCIA (categoría 4).
     Esta es la señal más confiable porque el nombre del producto varía por banco, pero la forma de
     identificar al destinatario (teléfono vs. número de cuenta) es constante.
   - Otras señales típicas de categoría (3): número de teléfono (0412/0414/0424/0416/0426...), cédula o
     RIF del emisor y receptor, número de referencia, screenshot de app bancaria (BDV, Mercantil,
     Banesco, etc.), no de un terminal físico.
   - MONTO A USAR: si el comprobante muestra por separado "Monto", "Comisión" y "Total" (Monto + Comisión =
     Total), usa SIEMPRE el "Monto" (neto) en el campo principal "monto", NUNCA el "Total". La comisión
     la cobra el banco emisor al que paga, no es parte de lo que recibe el comercio — usar el Total
     infla el descuadre en el valor exacto de la comisión.
     ADEMÁS, siempre que el comprobante muestre "Comisión" y "Total" por separado, llena también
     "comision_pago_movil" y "total_pago_movil_bruto" con esos dos valores tal cual aparecen impresos
     — esto es una transcripción de RESPALDO independiente (no la omitas aunque estés seguro de cuál
     valor pusiste en "monto"): permite que el sistema detecte automáticamente si por error pusiste el
     Total en el campo "monto" en vez del Monto neto, y lo corrija.

4) TRANSFERENCIA BANCARIA (real):
   - Contiene una cuenta DESTINO identificada por NÚMERO DE CUENTA BANCARIA (no por teléfono), y viene
     de un screenshot/comprobante de banca en línea o app móvil. Puede decir explícitamente
     "Transferencia" (no "Transmisión de Lote"), pero lo que realmente decide es el número de cuenta
     destino — ver la regla estructural de la categoría (3) arriba. Si el beneficiario se identifica
     con un número de teléfono en vez de una cuenta, NO es esta categoría, es categoría (3) Pago Móvil,
     aunque la pantalla tenga un campo "Cuenta origen" o el título no diga "Pago Móvil".
   - NO tiene estructura de terminal/lote/lote aceptado.
   - PROHIBIDO clasificar como Transferencia cualquier monto que en la MISMA imagen esté acompañado, en
     cualquier parte de la foto (arriba, abajo, al lado), por las palabras "TRANSMISIÓN DE LOTE" o
     "LOTE ACEPTADO" — sin importar que la foto tenga varios recibos distintos pegados uno junto al otro.
     Si tienes dudas sobre a cuál recibo pertenece un monto en una foto con múltiples recibos, y alguno
     de esos recibos en la misma imagen dice "LOTE", clasifica ese monto como categoría (2), no (4).

5) EFECTIVO: no aplica comprobante fotográfico normalmente; ignora salvo que se indique lo contrario.

REGLA DE ORO: si un documento tiene "LOTE" en el texto y una tabla de Compra/Anulada/Total, es
categoría (2), nunca (4), sin importar qué tan parecido suene a "transferencia".

CASO ESPECIAL — "MONTO Bs. X" pegado a una "TRANSMISIÓN DE LOTE" en la MISMA imagen:
Algunos terminales imprimen en un mismo rollo de papel, uno debajo del otro, dos documentos: (i) una
línea suelta "MONTO Bs. X" SIN "RECIBO DE COMPRA", SIN tipo de tarjeta (Maestro/Visa/Mastercard) y
SIN campos de AID/APRO/TRACE de una compra individual, seguida inmediatamente (más abajo, en la
misma foto) por un reporte "TRANSMISIÓN DE LOTE" / "LOTE ACEPTADO" con tablas de Compra/Anulada/Total.
En ese caso, ese "MONTO Bs. X" NO es un cobro nuevo independiente ni una transferencia: es el encabezado
o resumen del MISMO cierre de lote que aparece debajo. Trata TODO ese bloque (el "MONTO Bs. X" +
la transmisión de lote) como UN SOLO comprobante de categoría (2) "Cierre de Lote / Reporte de Cierre",
con es_resumen_no_cobro = true. Solo clasifica un "MONTO Bs. X" como cobro individual (categoría 1, 3 o 4)
si tiene los identificadores propios de una compra o transferencia individual (RIF/cédula del pagador,
tipo de tarjeta, número de referencia de transferencia/pago móvil, etc.) y NO viene pegado a una tabla
de lote en la misma imagen.

CASO ESPECIAL 2 — recibo de compra INDIVIDUAL completo + reporte de cierre del MISMO terminal, ambos
impresos uno debajo del otro en la MISMA foto (a diferencia del CASO ESPECIAL de arriba, aquí SÍ hay
un recibo de compra individual real y completo, con sus propios AID/APRO/REF/TRACE — no es solo un
"MONTO Bs. X" suelto): en este caso, reporta DOS elementos separados en "comprobantes_leidos" para
ESE MISMO "archivo" (repite el mismo nombre de archivo en ambos): uno de categoría (1) con el monto
de la compra individual y su "terminal_identificador", y otro de categoría (2) "Cierre de Lote /
Reporte de Cierre" con sus "total_fila_credito"/"total_fila_debito"/"total_fila_mc_visa_debit"/
"total_fila_extrafin" y el MISMO "terminal_identificador" (para que el sistema los relacione).
NO combines ambos documentos en un solo elemento, y NO reportes solo uno de los dos ignorando el otro.

CASO ESPECIAL 3 — DOS reportes de cierre de lote DISTINTOS (dos "REPORTE DE CIERRE" o dos "TRANSMISIÓN
DE LOTE" completos, cada uno con su propio "T:"/"L:") impresos uno debajo del otro en la MISMA foto,
en el mismo rollo de papel: esto pasa seguido porque el comercio imprime varios cierres de terminal
seguidos y los fotografía juntos para ahorrar fotos. Cada "REPORTE DE CIERRE"/"TRANSMISIÓN DE LOTE" que
tenga su PROPIO número de lote ("L:") es un documento independiente, AUNQUE estén en la misma imagen —
repórtalos como elementos SEPARADOS en "comprobantes_leidos" (mismo nombre de "archivo" en ambos, igual
que en CASO ESPECIAL 2), cada uno con sus propios 4 campos "total_fila_*" transcritos literalmente de
SU sección, y NUNCA combines ni sumes los números de un reporte con los del otro solo porque comparten
la imagen — cada uno se transcribe y se envía como si fuera la única foto que existe.
⚠️ PASO OBLIGATORIO ANTES DE TERMINAR CON CUALQUIER FOTO DE CATEGORÍA (2): cuenta cuántas veces aparece
un encabezado NUEVO de cierre en la imagen — cada aparición de las palabras "REPORTE DE CIERRE",
"CIERRE CREDITO T:", "CIERRE DEBITO T:", o un nuevo bloque "APROBADO" con su propia línea "TARJETA
CREDITO"/"MASTER/VISA DEBITO"/"TARJETA DEBITO" debajo, es un reporte independiente. Si cuentas 2, DEBES
generar 2 elementos en "comprobantes_leidos" para ese archivo, sin excepción. NO te detengas después
del primer bloque "APROBADO" que encuentres — sigue mirando hacia abajo en la misma imagen, porque es
muy común que haya un SEGUNDO "REPORTE DE CIERRE" completo impreso justo debajo del primero, a veces
con su propio encabezado de banco repetido. Omitir ese segundo bloque es el error más costoso de todo
este documento: desaparece dinero real del cuadre.
Ejemplo real #1 — una sola foto con dos reportes seguidos:
    REPORTE DE CIERRE #1: "CIERRE CREDITO T:1002 L:499", TARJETA CREDITO en Bs. 0,00, MASTER/VISA
    DEBITO en Bs. 1.627,66 (⚠️ el título dice CREDITO pero el monto real está bajo la sección MASTER/
    VISA DEBITO → sigue la regla de arriba: es débito, no crédito).
    REPORTE DE CIERRE #2: "CIERRE DEBITO T:2002 L:141", TARJETA DEBITO (MAESTRO) en Bs. 7.309,51.
La transcripción CORRECTA es DOS elementos separados: el primero con total_fila_mc_visa_debit=1627.66
(el resto en 0) y terminal_identificador "BDV T:1002 L:499"; el segundo con total_fila_debito=7309.51
(el resto en 0) y terminal_identificador "BDV T:2002 L:141".
Ejemplo real #2 — MISMO patrón, otra foto distinta (para que veas que no es un caso aislado):
    REPORTE DE CIERRE #1: "CIERRE CREDITO T:1002 L:497", TARJETA CREDITO en Bs. 0,00, MASTER/VISA
    DEBITO en Bs. 1.949,89.
    REPORTE DE CIERRE #2 (impreso justo debajo, en la misma foto): "CIERRE DEBITO T:2002 L:139",
    TARJETA DEBITO MAESTRO en Bs. 11.341,32.
La transcripción CORRECTA es, otra vez, DOS elementos: total_fila_mc_visa_debit=1949.89 para el
primero, y total_fila_debito=11341.32 para el segundo — el segundo NO se puede omitir aunque el primer
bloque ya "se vea completo" con su propia línea "APROBADO".
Reportar solo UNO de los dos reportes, o mezclar sus cifras en un solo elemento, hace desaparecer
dinero real del cuadre — es el error más común cuando dos reportes de cierre comparten una sola foto.

Cada imagen viene precedida por una línea de texto "--- Archivo #N de TOTAL: nombre exacto = "..." ---"
indicando su nombre real de archivo. USA ESE NOMBRE EXACTO (tal cual, con extensión) en el campo
"archivo" de cada elemento de "comprobantes_leidos". NO inventes ni parafrasees el nombre.

Tu tarea:
a. Lee cada comprobante, decide su categoría según las reglas de arriba, y extrae el monto exacto.
   NO compares nada contra el sistema A2 — no se te muestra esa información a propósito (ver nota
   al inicio del mensaje). Tu trabajo termina en clasificar y extraer datos.
b. Los "Cierre de Lote / Reporte de Cierre" (categoría 2) son resúmenes de terminal, no cobros nuevos
   — clasifícalos igual que cualquier otro comprobante, sin intentar verificar si "cuadran" con nada.
c. "Otro" es el ÚLTIMO RECURSO: úsalo solo si la imagen está ilegible/borrosa o claramente NO es un
   comprobante ni cierre de ningún medio de pago. Antes de usar "Otro", revisa si el documento encaja
   en alguna de las categorías (1)-(5) de arriba, incluyendo el caso de plataformas no bancarias como
   Cashea explicado en la categoría (2) — un panel de "Cierre de caja del día" de una app de pagos
   SIEMPRE tiene una categoría correcta entre (1)-(5), nunca es "Otro".
d. OBLIGATORIO: "comprobantes_leidos" debe tener AL MENOS {n_archivos} elementos — como mínimo
   uno por cada archivo recibido (ver el total indicado en cada etiqueta "--- Archivo #N de TOTAL ---").
   No omitas ningún archivo. Si una imagen está borrosa, ilegible o no corresponde a ningún comprobante
   de pago reconocible, IGUAL inclúyela en la lista con tipo "Otro", monto 0, y explica el motivo
   en analisis_detallado — pero nunca la excluyas de la lista.
   EXCEPCIÓN (más elementos que archivos): si una misma foto contiene DOS documentos físicos distintos
   que deben reportarse por separado (ver "CASO ESPECIAL 2": recibo de compra individual + reporte de
   cierre del mismo terminal en una sola foto; o "CASO ESPECIAL 3": dos reportes de cierre de lote
   DISTINTOS —cada uno con su propio "L:"— impresos uno debajo del otro en la misma foto), agrega DOS
   elementos para ese archivo (mismo nombre de "archivo" en ambos). En ese caso el total de elementos
   será MAYOR a {n_archivos}, y eso está bien — nunca sacrifiques la separación de esos dos
   documentos solo por igualar el conteo.

Usa la herramienta "registrar_auditoria" para entregar tu resultado estructurado.
"""


@app.get("/")
def salud():
    """Health check simple."""
    return {"status": "ok", "servicio": "auditoria-ia"}


async def _auditar_comprobantes_impl(archivos: List[UploadFile], totales_json: str):
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


# Se registran DOS rutas para el mismo handler ("/api/auditar" y "/") a
# propósito: en el modo "función por archivo" de Vercel no siempre está claro
# si el prefijo de carpeta (api/auditar.py -> /api/auditar) llega ya recortado
# a la app de FastAPI o no. Registrando ambas, el endpoint funciona sin
# importar cuál de los dos casos aplique en tu proyecto -- puedes borrar la
# que no uses una vez confirmes cuál responde.
@app.post("/api/auditar", dependencies=[Depends(verificar_secreto)])
async def auditar_comprobantes(
    archivos: List[UploadFile] = File(...),
    totales_json: str = Form(...),
):
    return await _auditar_comprobantes_impl(archivos, totales_json)


@app.post("/", dependencies=[Depends(verificar_secreto)])
async def auditar_comprobantes_raiz(
    archivos: List[UploadFile] = File(...),
    totales_json: str = Form(...),
):
    return await _auditar_comprobantes_impl(archivos, totales_json)
