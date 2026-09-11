#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
api/index.py — Microservicio "Auditoría IA" (versión Vercel, archivo único)
==============================================================================
Se llama "index.py" (y no "auditar.py") por un requisito específico de
Vercel: desde que detecta automáticamente aplicaciones FastAPI, solo lo hace
si el archivo está en una ubicación "por defecto" reconocida -- app.py,
index.py, server.py, main.py, wsgi.py o asgi.py, ya sea suelto o dentro de
src/, app/ o api/. Un nombre distinto (ej. "auditar.py") hace que Vercel
detecte la app pero se niegue a usarla automáticamente ("No FastAPI
entrypoint found in default locations, but found potential entrypoints...").
El endpoint real que llama el frontend sigue siendo /api/auditar -- eso lo
define la propia ruta de FastAPI más abajo (@app.post("/api/auditar", ...)),
no el nombre de este archivo. El nombre del archivo solo le dice a Vercel
CUÁL app cargar; las rutas de adentro son las que de verdad importan.
LÍMITE DE TAMAÑO -- por qué el frontend manda "lotes" en vez de todo junto:
las funciones de Vercel rechazan peticiones de más de ~4.5 MB
(FUNCTION_PAYLOAD_TOO_LARGE). Se intentó primero subir cada foto DIRECTO a
Vercel Blob para evitar ese límite (subida "client upload"), pero ese
protocolo de subida es interno y no está documentado públicamente -- tras
varios intentos fallidos reconstruyéndolo a mano, se abandonó. En su lugar,
index.html divide los comprobantes en lotes que SÍ caben bajo el límite y
llama a /api/auditar una vez POR LOTE (el usuario solo ve "Lote X de Y...",
nunca tiene que elegir los lotes a mano). Cada lote se clasifica con Claude
por separado, así que la "reconciliación" que devuelve CADA llamada a
/api/auditar es solo la de ESE lote -- el frontend combina los
"comprobantes_leidos" de todos los lotes y llama a /api/reconciliar (ver más
abajo) UNA sola vez con la lista completa para obtener el resultado final
correcto (comparar una suma parcial contra el total completo del sistema
daría descuadres falsos).
IMPORTANTE -- por qué este archivo es autocontenido (sin imports propios):
Vercel empaqueta cada función de /api/ por separado y, en pruebas, no
siempre incluye módulos hermanos (ej. audit_prompt.py) en el mismo paquete,
lo que producía "ModuleNotFoundError: No module named 'audit_prompt'". Para
evitar ese problema de raíz, aquí NO se importa nada que no sea una
librería instalada (fastapi, anthropic, etc.) -- el prompt completo del
auditor se arma como un f-string DENTRO de la función que lo usa (no como
una constante de módulo aparte), para que no pueda volver a desincronizarse
del código que lo rellena -- ver la nota junto a "prompt_auditor" más abajo.
Qué hace este servicio: recibe los comprobantes (imágenes/PDF) de UN lote más
los totales del sistema ya calculados por el frontend, llama a la API de
Claude para clasificarlos, calcula la reconciliación determinística en
Python para ESE lote (igual que en el main.py original de la app de
escritorio/Render) y devuelve el JSON de resultado. El resto de la app
(dashboard, sync ODBC con A2, SQLite) NO vive aquí -- sigue corriendo donde
ya estaba.
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
import re
import traceback
from typing import List
import anthropic
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
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
# Prefijos de celular venezolanos -- si "destino_telefono_o_cuenta" empieza por uno de estos, el
# destino del pago es un TELÉFONO, sin ambigüedad posible (los números de cuenta bancaria no
# usan este formato). Esta es la corrección automática que el prompt promete: si la IA elige
# mal el "tipo" pero transcribe bien el número de destino, este chequeo lo corrige solo, SIN
# depender de que la IA haya "razonado" correctamente sobre el diseño de la pantalla.
_PREFIJOS_CELULAR_VE = ("0412", "0414", "0416", "0422", "0424", "0426")
def _clasificar_por_destino(destino_telefono_o_cuenta):
    """A partir del número que la IA transcribió en "destino_telefono_o_cuenta", determina si el
    comprobante DEBERÍA ser "Pago Móvil" o "Transferencia" -- independiente de lo que la IA haya
    elegido en "tipo". Devuelve "Pago Móvil", "Transferencia", o None si el campo viene vacío o
    con un formato que no se puede clasificar con confianza (mejor no corregir nada a forzar una
    corrección equivocada)."""
    if not destino_telefono_o_cuenta:
        return None
    solo_digitos = re.sub(r"\D", "", str(destino_telefono_o_cuenta))
    if not solo_digitos:
        return None
    candidato = solo_digitos
    # Algunos comprobantes omiten el "0" inicial del celular (ej. "412-1766662" en vez de
    # "0412-1766662") -- se completa antes de comparar el prefijo.
    if len(candidato) == 10 and not candidato.startswith("0"):
        candidato = "0" + candidato
    if len(candidato) == 11 and candidato[:4] in _PREFIJOS_CELULAR_VE:
        return "Pago Móvil"
    # Un número de cuenta bancaria venezolano tiene 20 dígitos (código de banco de 4 + resto de
    # la cuenta), pero en los comprobantes suele venir parcial/enmascarado con asteriscos (ya
    # descartados arriba, en "solo_digitos") -- por eso solo se exige un mínimo razonable de
    # dígitos, no los 20 completos, para no dejar de reconocer una cuenta truncada como tal.
    if len(solo_digitos) >= 10:
        return "Transferencia"
    return None
def _corregir_tipos_por_destino(items):
    """Recorre los comprobantes ya leídos y, para cada uno de tipo Pago Móvil o Transferencia,
    verifica su "destino_telefono_o_cuenta" contra _clasificar_por_destino -- si no coincide con el
    "tipo" que eligió la IA, lo corrige ahí mismo (mutando el dict en el lugar) y deja un
    registro de la corrección para mostrarlo en el resultado (transparencia: nunca se corrige
    algo en silencio). No toca nada si destino_telefono_o_cuenta viene vacío o no es clasificable."""
    correcciones = []
    for item in items:
        tipo_actual = item.get("tipo")
        if tipo_actual not in ("Pago Móvil", "Transferencia"):
            continue
        tipo_correcto = _clasificar_por_destino(item.get("destino_telefono_o_cuenta"))
        if tipo_correcto and tipo_correcto != tipo_actual:
            correcciones.append({
                "archivo": item.get("archivo"),
                "monto": item.get("monto"),
                "destino_telefono_o_cuenta": item.get("destino_telefono_o_cuenta"),
                "tipo_original_ia": tipo_actual,
                "tipo_corregido": tipo_correcto,
            })
            item["tipo"] = tipo_correcto
    return correcciones
def _detectar_reportes_faltantes(items):
    """Agrupa los elementos de "Cierre de Lote / Reporte de Cierre" por archivo, y compara
    cuántos hay contra lo que la propia IA declaró en "reportes_en_esta_foto" (ver CASO ESPECIAL
    3 del prompt). Si hay menos elementos de los que la IA misma contó, es señal fuerte de que se
    le olvidó transcribir un reporte que sí notó en la foto -- no se puede inventar el dato
    faltante, así que solo se expone como advertencia para revisar esa foto a mano."""
    por_archivo = {}
    for item in items:
        if item.get("tipo") != "Cierre de Lote / Reporte de Cierre":
            continue
        archivo = item.get("archivo") or "(sin nombre)"
        por_archivo.setdefault(archivo, []).append(item)
    advertencias = []
    for archivo, elementos in por_archivo.items():
        conteos_declarados = [int(_num(e.get("reportes_en_esta_foto"))) for e in elementos if e.get("reportes_en_esta_foto")]
        conteo_declarado = max(conteos_declarados) if conteos_declarados else 0
        if conteo_declarado > len(elementos):
            advertencias.append({
                "archivo": archivo,
                "reportes_declarados_por_ia": conteo_declarado,
                "elementos_transcritos": len(elementos),
                "mensaje": (
                    f'La IA contó {conteo_declarado} reporte(s) de cierre en "{archivo}", pero solo '
                    f"transcribió {len(elementos)}. Probablemente falta un monto de tarjeta en el "
                    f"cuadre -- revisa esta foto a mano."
                ),
            })
    return advertencias
def _detectar_discrepancias_monto_tarjeta(items):
    """Para cada Cierre de Lote, compara "monto" (la cifra general que reportó la IA) contra la
    suma de sus propios campos detallados (total_fila_credito + total_fila_debito +
    total_fila_mc_visa_debit + total_fila_extrafin) -- que es lo que realmente se usa para la
    reconciliación (ver el bucle de arriba). Si difieren en más de Bs. 1, la IA transcribió dos
    cosas inconsistentes para el mismo comprobante -- no hay forma confiable de saber cuál de las
    dos está bien sin ver la foto, así que no se auto-corrige nada, solo se expone."""
    advertencias = []
    for item in items:
        if item.get("tipo") != "Cierre de Lote / Reporte de Cierre":
            continue
        monto_general = _num(item.get("monto"))
        suma_detallada = (
            _num(item.get("total_fila_credito"))
            + _num(item.get("total_fila_debito"))
            + _num(item.get("total_fila_mc_visa_debit"))
            + _num(item.get("total_fila_extrafin"))
        )
        if abs(monto_general - suma_detallada) > 1.00:
            advertencias.append({
                "archivo": item.get("archivo"),
                "monto_general_ia": round(monto_general, 2),
                "suma_campos_detallados": round(suma_detallada, 2),
                "mensaje": (
                    f'En "{item.get("archivo")}", el monto general que dio la IA (Bs. '
                    f"{formatear_monto_ve(monto_general)}) no coincide con la suma de sus propios "
                    f"campos detallados de tarjeta (Bs. {formatear_monto_ve(suma_detallada)}). "
                    f"Uno de los dos números está mal transcrito -- revisa esta foto a mano "
                    f"(la reconciliación usa la suma de los campos detallados, no el monto general)."
                ),
            })
    return advertencias
def _agregar_efectivo_fisico(items):
    """Combina el desglose de billetes de TODOS los comprobantes de tipo 'Efectivo' (conteo de
    billetes físicos) en un solo resumen: cuántos billetes hay de cada denominación -- Bs y USD
    por separado -- y el total de cada moneda. A diferencia de las demás categorías, esto NO se
    compara contra ningún total del sistema A2 -- es puramente un conteo de lo que hay
    físicamente en caja, para que el usuario lo vea desglosado sin tener que contarlo a mano."""
    desglose_bs = {}
    desglose_usd = {}
    for item in items:
        if item.get("tipo") != "Efectivo":
            continue
        for billete in (item.get("billetes_bs") or []):
            if not isinstance(billete, dict):
                continue
            denom = _num(billete.get("denominacion"))
            cant = int(_num(billete.get("cantidad")))
            if denom <= 0 or cant <= 0:
                continue
            desglose_bs[denom] = desglose_bs.get(denom, 0) + cant
        for billete in (item.get("billetes_usd") or []):
            if not isinstance(billete, dict):
                continue
            denom = _num(billete.get("denominacion"))
            cant = int(_num(billete.get("cantidad")))
            if denom <= 0 or cant <= 0:
                continue
            desglose_usd[denom] = desglose_usd.get(denom, 0) + cant
    def _armar_lista(desglose):
        return [
            {"denominacion": denom, "cantidad": cant, "subtotal": round(denom * cant, 2)}
            for denom, cant in sorted(desglose.items(), reverse=True)
        ]
    return {
        "desglose_bs": _armar_lista(desglose_bs),
        "desglose_usd": _armar_lista(desglose_usd),
        "total_bs": round(sum(d["subtotal"] for d in _armar_lista(desglose_bs)), 2),
        "total_usd": round(sum(d["subtotal"] for d in _armar_lista(desglose_usd)), 2),
    }
def calcular_reconciliacion(comprobantes_leidos, totales_json_str):
    try:
        totales_sistema = json.loads(totales_json_str) if totales_json_str else {}
    except (json.JSONDecodeError, TypeError):
        totales_sistema = {}
    items = [i for i in (comprobantes_leidos or []) if isinstance(i, dict)]
    correcciones_destino = _corregir_tipos_por_destino(items)
    advertencias_calidad = _detectar_reportes_faltantes(items) + _detectar_discrepancias_monto_tarjeta(items)
    # El "monto" que muestra la tabla en pantalla ("Monto Extraído") se reemplaza aquí por la
    # suma real de los campos detallados de cada Cierre de Lote -- que es lo que efectivamente
    # se usa para el cuadre (ver el bucle de abajo). Se hace DESPUÉS de _detectar_discrepancias_
    # monto_tarjeta (para no perder esa advertencia) pero ANTES de sumar nada: así lo que el
    # usuario ve en pantalla siempre coincide con lo que realmente se calculó, en vez de
    # depender de que la IA haya sido internamente consistente consigo misma al escribir
    # "monto" por separado (ya se vio que puede no serlo, aunque cada campo individual esté
    # bien -- ej. sumar mal sus propios números al calcular el total general).
    for item in items:
        if item.get("tipo") != "Cierre de Lote / Reporte de Cierre":
            continue
        item["monto"] = round(
            _num(item.get("total_fila_credito"))
            + _num(item.get("total_fila_debito"))
            + _num(item.get("total_fila_mc_visa_debit"))
            + _num(item.get("total_fila_extrafin")),
            2,
        )
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
        if tipo in ("Tarjeta de Débito", "Tarjeta de Crédito"):
            continue  # se combinan más abajo en una sola categoría "Tarjeta (Débito + Crédito)"
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
    # --- Tarjeta de Débito + Tarjeta de Crédito, combinadas en UNA sola comparación ---
    # En el sistema A2 de este comercio, las ventas a crédito casi siempre quedan registradas
    # bajo la misma clave "Tarjeta de Débito" del sistema (la clave "Tarjeta de Crédito" suele
    # quedar en Bs. 0,00 aunque sí hubo ventas a crédito reales, según los propios comprobantes
    # de cierre de lote). Comparar cada una por separado contra el sistema mostraba un "FALTANTE"
    # de débito y un "SOBRANTE" de crédito por EL MISMO MONTO exacto -- un descuadre falso, no un
    # problema real (confirmado en varias auditorías reales de este comercio). Por eso se
    # combinan ambas categorías en una sola comparación: la suma total de comprobantes de
    # tarjeta (débito + crédito) contra la suma total que reporta el sistema en esas dos claves
    # (normalmente solo "Tarjeta de Débito" trae algo, pero se suman las dos por si el sistema
    # alguna vez sí separa el crédito). Esto NO oculta un descuadre real de tarjeta: si la suma
    # total de comprobantes de tarjeta no coincide con la suma total del sistema, la categoría
    # combinada igual se marca como descuadre -- solo deja de dividir un mismo total real en dos
    # mitades que nunca van a cuadrar por separado en este sistema.
    suma_comprobantes_tarjeta = round(
        sumas_finales.get("Tarjeta de Débito", 0.0) + sumas_finales.get("Tarjeta de Crédito", 0.0), 2
    )
    info_sistema_debito = totales_sistema.get("Tarjeta de Débito", {}) if isinstance(totales_sistema, dict) else {}
    info_sistema_credito = totales_sistema.get("Tarjeta de Crédito", {}) if isinstance(totales_sistema, dict) else {}
    monto_sistema_tarjeta = round(
        parsear_monto_ve(info_sistema_debito.get("monto_ventas_sistema"))
        + parsear_monto_ve(info_sistema_credito.get("monto_ventas_sistema")),
        2,
    )
    diferencia_tarjeta = round(suma_comprobantes_tarjeta - monto_sistema_tarjeta, 2)
    total_general_comprobantes += suma_comprobantes_tarjeta
    cuadra_tarjeta = abs(diferencia_tarjeta) <= TOLERANCIA_DIFERENCIA_INSIGNIFICANTE_BS
    fuente_tarjeta = _describir_fuente_tarjeta(
        suma_lote_debito + suma_lote_credito, suma_individual_debito + suma_individual_credito
    )
    reconciliacion["Tarjeta (Débito + Crédito)"] = {
        "clave_sistema": "Tarjeta de Débito + Tarjeta de Crédito",
        "suma_comprobantes": suma_comprobantes_tarjeta,
        "monto_sistema": monto_sistema_tarjeta,
        "diferencia": diferencia_tarjeta,
        "cuadra": cuadra_tarjeta,
        "diferencia_insignificante": cuadra_tarjeta and abs(diferencia_tarjeta) >= 0.01,
        "fuente": fuente_tarjeta,
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
        "correcciones_destino": correcciones_destino,
        "advertencias_calidad": advertencias_calidad,
        "efectivo_fisico": _agregar_efectivo_fisico(items),
    }
# ---------------------------------------------------------------------------
# Definición de la herramienta (tool use de Claude). Incluye "destino_telefono_o_cuenta"
# y "reportes_en_esta_foto" -- agregados para que coincidan con las instrucciones
# nuevas del prompt (más abajo). Sin declararlos aquí, Claude no puede devolverlos
# aunque el prompt se lo pida: el tool-calling de la API solo acepta propiedades
# que estén en este schema.
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
                "description": "Lista breve de CADA comprobante con SOLO su clasificación: archivo, tipo detectado, y monto extraído (1 línea por comprobante). Ejemplo: 'Archivo X: Pago Móvil, Bs. 3.087,68.' PROHIBIDO: no compares contra el sistema, no menciones 'descuadre'/'faltante'/'sobrante'/'coincide con'/'no coincide con', no calcules sumas totales ni diferencias — eso lo hace el servidor por separado. Si necesitas aclarar algo sobre un comprobante (ej. imagen borrosa, dos documentos en una foto), descríbelo sin mencionar montos del sistema. SÉ CONCISO: máximo 1 línea por comprobante. NO transcribas todos los campos del recibo (AID, TRACE, RIF, etc.). Con lotes grandes (10+ archivos) la brevedad es obligatoria para no quedarse sin espacio de respuesta.",
            },
            "comprobantes_leidos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "archivo": {"type": "string"},
                        "monto": {
                            "type": "number",
                            "description": "El monto principal de este comprobante, en la moneda que corresponda (Bs. para casi todo, USD solo para Cashea). Para tipo 'Cierre de Lote / Reporte de Cierre': este número DEBE ser exactamente igual a la suma de los campos 'total_fila_credito' + 'total_fila_debito' + 'total_fila_mc_visa_debit' + 'total_fila_extrafin' que pongas en este mismo elemento -- son el mismo dinero contado de dos formas distintas, nunca deberían diferir. Antes de responder, súmalos tú mismo y verifica que 'monto' coincida con esa suma; si el ticket imprime una línea 'MONTO TOTAL' (formato 'TRANSMISIÓN DE LOTE'), usa ese valor impreso para 'monto' Y confirma que tu propia suma de los campos 'total_fila_*' da ese mismo número -- si no coinciden, revisa cuál de los dos leíste mal antes de responder, no entregues dos cifras contradictorias para el mismo comprobante.",
                        },
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
                            "description": "REGLA CLAVE, la más importante para no perder dinero real del cuadre: para elegir entre 'Cierre de Lote / Reporte de Cierre' y 'Reporte Interno del Sistema (Corte/Cierre X/Z)' -- sea el documento un 'REPORTE DE CIERRE' o una 'TRANSMISIÓN DE LOTE' (ambos formatos son la misma categoría, un cierre de terminal de tarjeta, solo cambia el diseño según el banco) -- mira SOLO el nombre en el encabezado/emisor de arriba del todo del documento (la primera línea con un nombre propio). Si ese encabezado/emisor es el nombre de un BANCO (Banco de Venezuela, Bancrecer, Banesco, Mercantil, BOD, Provincial, Bancaribe, Banplus, etc.) -> SIEMPRE 'Cierre de Lote / Reporte de Cierre', SIN EXCEPCIÓN -- aunque más abajo en el mismo documento aparezca 'TERA SUMINISTROS'/'TERA SOLUCIONES'/'TERA REFRIGERACIÓN', eso es solo el comercio afiliado (cliente del banco), NUNCA el emisor. Solo es 'Reporte Interno del Sistema' cuando el encabezado/emisor ES LITERALMENTE 'TERA SUMINISTROS C.A.'/'TERA SOLUCIONES C.A.'/'TERA REFRIGERACIÓN C.A.' (el propio negocio imprimiéndose un reporte a sí mismo, ej. 'Corte de Caja (X)'/'Cierre de Caja (Z)'). Un documento cuyo encabezado es el nombre de un banco NUNCA es 'Reporte Interno del Sistema', sin importar cuántas otras palabras contenga ni qué tan parecido se vea su diseño al de un corte de caja. Esta confusión ya ha causado que se pierdan miles de bolívares reales de tarjeta de débito/crédito del cuadre -- revisa el encabezado con cuidado antes de elegir.",
                        },
                        "es_resumen_no_cobro": {
                            "type": "boolean",
                            "description": "true si el documento es un reporte de cierre de lote / transmisión de lote / reporte interno del sistema / cierre de caja del día de Cashea (resume varias transacciones, no es un cobro individual nuevo)."
                        },
                        "total_fila_credito": {
                            "type": "number",
                            "description": "SOLO cuando tipo = 'Cierre de Lote / Reporte de Cierre'. Copia TAL CUAL el número que aparece junto a la SECCIÓN/FILA etiquetada 'Total Credito' / 'Total Crédito' / 'TARJETA CREDITO' (la sección que muestra COMPRA/ANULACION/TOTAL para tarjetas de crédito). ⚠️ IGNORA POR COMPLETO el título/encabezado general del documento (ej. si el documento dice arriba de todo 'CIERRE CREDITO T:1002 L:498' o 'REPORTE DE CIERRE ... CIERRE CREDITO'): ese título es solo un identificador de terminal/lote, NO indica que el monto sea crédito. Lo único que importa es la etiqueta de la SECCIÓN inmediatamente encima del número. Si la sección 'TARJETA CREDITO' muestra 0,00 (aunque el título del documento diga 'CIERRE CREDITO'), usa 0 aquí. NO decidas nada, NO combines con otras filas: solo copia el número de ESA sección. Usa 0 si esa sección no existe en el documento o su total está en blanco/0,00.",
                        },
                        "total_fila_debito": {
                            "type": "number",
                            "description": "SOLO cuando tipo = 'Cierre de Lote / Reporte de Cierre'. Copia TAL CUAL el número que aparece junto a la SECCIÓN/FILA etiquetada 'Total Débito' / 'TARJETA DEBITO' (la fila/sección que dice 'Débito' SOLO, sin 'MC/Visa' ni 'Master/Visa' en su nombre — a veces con la marca 'MAESTRO' debajo). NO decidas nada, NO combines con otras filas: solo copia ese número exacto. Usa 0 si esa fila no existe o su total está en 0,00.",
                        },
                        "total_fila_mc_visa_debit": {
                            "type": "number",
                            "description": "SOLO cuando tipo = 'Cierre de Lote / Reporte de Cierre'. Copia TAL CUAL el número que aparece junto a una sección que combina una marca de tarjeta con la palabra 'Débito'/'Debit' — puede aparecer como 'Total MC/Visa', 'Total MC/Visa Debit', 'MASTER/VISA DEBITO', 'MAESTRO/VISA DEBITO', o variantes similares. TODAS estas son DÉBITO, sin importar que contengan las palabras 'Visa' o 'Master' (esas son marcas de tarjeta, no indican crédito). Esta es una sección DISTINTA de 'Total Débito' puro y de 'Total Credito'/'TARJETA CREDITO', aunque su número de terminal a veces coincida con el de otra fila, o aunque el título general del documento diga 'CIERRE CREDITO' — no la confundas con esas por el título. NO decidas nada, NO combines con otras filas: solo copia el número de ESA sección exacto. Usa 0 si esa sección no existe o su total está en 0,00.",
                        },
                        "total_fila_extrafin": {
                            "type": "number",
                            "description": "SOLO cuando tipo = 'Cierre de Lote / Reporte de Cierre'. Copia TAL CUAL el número que aparece junto a la etiqueta 'Total ExtraFin' de la tabla, si existe (financiamiento extra, no es tarjeta débito/crédito tradicional). Usa 0 si esa fila no existe o está en 0,00.",
                        },
                        "terminal_identificador": {
                            "type": "string",
                            "description": "Para comprobantes de tipo 'Tarjeta de Débito', 'Tarjeta de Crédito' o 'Cierre de Lote / Reporte de Cierre': copia el identificador del terminal tal como aparece en el documento, combinando el nombre/código del banco emisor con el número junto a 'T:' o 'TERMINAL:' (ejemplo: 'BDV T:00445215'). Esto permite relacionar un recibo de compra individual con el cierre de lote de ESE MISMO terminal y evitar sumarlo dos veces. Si el terminal no es visible en el documento, deja el campo vacío (no inventes un número).",
                        },
                        "reportes_en_esta_foto": {
                            "type": "integer",
                            "description": "OBLIGATORIO cuando tipo = 'Cierre de Lote / Reporte de Cierre': ANTES de transcribir los montos, cuenta cuántos reportes de cierre COMPLETOS e INDEPENDIENTES (cada uno con su propio 'L:'/número de lote y su propio bloque 'APROBADO'/'Compra-Anulada-Total') hay impresos en ESTA MISMA foto -- ver 'CASO ESPECIAL 3'. Si en la foto solo hay un reporte, pon 1. Si hay dos reportes distintos apilados uno debajo del otro (muy común: el comercio fotografía varios cierres de terminal juntos para ahorrar fotos), pon 2 -- y en ese caso DEBES generar 2 elementos separados en 'comprobantes_leidos' para este mismo 'archivo', y este mismo número 2 va en AMBOS elementos (es el conteo total de la foto, no un índice). Un reporte con el MISMO 'L:' que reporta crédito Y débito juntos sigue contando como 1 (ver 'CASO ESPECIAL 4' -- eso va en UN solo elemento con ambos campos 'total_fila_*' llenos, no en dos elementos). El servidor usa este número para detectar automáticamente si accidentalmente reportaste menos elementos de los que tú mismo contaste en la foto, y así nunca perder un reporte que sí notaste pero olvidaste transcribir. Si no puedes determinar el conteo, usa 1.",
                        },
                        "comision_pago_movil": {
                            "type": "number",
                            "description": "SOLO cuando tipo = 'Pago Móvil' y el comprobante muestra un campo 'Comisión' por separado. Copia ESE número tal cual (el que está junto a la palabra 'Comisión'). Usa 0 si el comprobante no muestra comisión por separado.",
                        },
                        "total_pago_movil_bruto": {
                            "type": "number",
                            "description": "SOLO cuando tipo = 'Pago Móvil' y el comprobante muestra un campo 'Total' por separado del 'Monto' (donde Monto + Comisión = Total). Copia ESE número de 'Total' tal cual, AUNQUE ya hayas puesto el 'Monto' neto en el campo principal 'monto' — esto es una transcripción de respaldo independiente, no una repetición: sirve para que el sistema verifique automáticamente que no confundiste Monto con Total. Usa 0 si el comprobante no muestra un 'Total' separado del 'Monto'.",
                        },
                        "destino_telefono_o_cuenta": {
                            "type": "string",
                            "description": "Este campo va SIEMPRE presente en cada elemento (aunque sea con texto vacío \"\") porque el esquema lo exige, pero solo tiene contenido real cuando tipo = 'Pago Móvil' o 'Transferencia' -- para cualquier otro tipo (tarjeta, efectivo, Cashea, cierre de lote, etc.) déjalo como cadena vacía \"\". Cuando tipo SÍ es 'Pago Móvil' o 'Transferencia', llenarlo es OBLIGATORIO: copia TAL CUAL (solo los dígitos, y guiones si los tiene) el NÚMERO DE TELÉFONO o NÚMERO DE CUENTA BANCARIA del BENEFICIARIO/DESTINO del pago -- el campo que en el comprobante suele decir 'Beneficiario:', 'Destino:', 'Cuenta destino:', 'Número celular de destino:' o similar. ⚠️ NUNCA copies aquí una CÉDULA o RIF (campos como 'Identificación receptor:', 'C.I.:', 'RIF:') -- aunque el nombre de ese campo se parezca al nombre de este ('identificación' vs. 'destino_telefono_o_cuenta'), son cosas DISTINTAS: una cédula/RIF NUNCA va aquí, solo un TELÉFONO o una CUENTA. Tampoco copies el de 'Cuenta origen:'/'Número celular de origen:', ese es el pagador, no el destino. Esto es una transcripción de RESPALDO independiente de tu elección de 'tipo': el sistema usa este número para verificar automáticamente por su formato si es un teléfono (04XX-XXXXXXX, 11 dígitos) o una cuenta bancaria (código de banco de 4 dígitos que NO empieza en '04' + resto de la cuenta, hasta 20 dígitos, a veces parcialmente enmascarada con asteriscos) -- y corrige el tipo si no coincide con lo que elegiste. Por eso es más importante que nunca copiarlo bien, incluso si estás seguro de qué 'tipo' pusiste. Solo déjalo vacío en un elemento de tipo 'Pago Móvil'/'Transferencia' si el documento genuinamente no muestra ningún teléfono ni cuenta de destino en ningún lado de la imagen.",
                        },
                        "billetes_usd": {
                            "type": "array",
                            "description": "SOLO para tipo = 'Efectivo': lista de los billetes en DÓLARES que se ven en la foto, agrupados por denominación -- un elemento por cada valor distinto de billete presente, con cuántos hay de ese valor. Para cualquier otro tipo, deja un arreglo vacío ([]). Ver la categoría (5) del prompt para el detalle de cómo contar.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "denominacion": {"type": "number", "description": "Valor impreso en el billete (1, 2, 5, 10, 20, 50, 100...)."},
                                    "cantidad": {"type": "integer", "description": "Cuántos billetes de ESA denominación se cuentan en la foto."},
                                },
                                "required": ["denominacion", "cantidad"],
                            },
                        },
                        "billetes_bs": {
                            "type": "array",
                            "description": "SOLO para tipo = 'Efectivo': lista de los billetes en BOLÍVARES que se ven en la foto, agrupados por denominación -- mismo formato que 'billetes_usd'. Para cualquier otro tipo, deja un arreglo vacío ([]).",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "denominacion": {"type": "number", "description": "Valor impreso en el billete."},
                                    "cantidad": {"type": "integer", "description": "Cuántos billetes de ESA denominación se cuentan en la foto."},
                                },
                                "required": ["denominacion", "cantidad"],
                            },
                        },
                    },
                    "required": ["archivo", "monto", "tipo", "destino_telefono_o_cuenta"],
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

@app.get("/")
def salud():
    """Health check simple."""
    return {"status": "ok", "servicio": "auditoria-ia"}
async def _auditar_comprobantes_impl(archivos: List[UploadFile], totales_json: str):
    """Recibe los archivos de UN LOTE (ver nota sobre límite de tamaño al
    inicio del archivo) directo como multipart/form-data -- igual que en el
    main.py original de escritorio. index.html se encarga de dividir la
    selección completa del usuario en lotes que quepan bajo el límite de
    ~4.5 MB antes de llamar aquí, y de combinar los resultados de todos los
    lotes después."""
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

        # NOTA sobre este cambio: el prompt se arma como f-string AQUÍ MISMO (no como
        # una constante de módulo aparte + .format()/.replace()). Así, "{len(archivos)}"
        # es una expresión real de Python evaluada contra el parámetro "archivos" que
        # ya está en este scope -- no puede desincronizarse de un mecanismo de relleno
        # aparte (eso fue justo lo que causó el KeyError: 'len(archivos)' anterior).
        prompt_auditor = f"""
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
           - REGLA DE ORO (la única que importa, más simple y confiable que cualquier otra señal): si en el
             comprobante aparece un número de teléfono venezolano (empieza por 0412, 0414, 0416, 0422, 0424 o
             0426) identificando al DESTINO/BENEFICIARIO del pago (a quien RECIBE el dinero -- busca el campo
             etiquetado "destino", "celular destino", "número celular de destino" o "beneficiario"; NO el de
             "origen", que es quien paga y normalmente aparece tapado con asteriscos, ej. "04**-***2001"), es
             PAGO MÓVIL (categoría 3) -- sin importar nada más: ni el banco, ni el logo, ni el color o diseño
             de la pantalla, ni un nombre de persona, ni si hay dos bancos distintos en la misma pantalla
             (interbancario), ni si la palabra "Pago Móvil" aparece o no (cada banco le pone su propio nombre
             comercial: "Tpago" de Banesco, "Pago Móvil BDV", "C-Móvil" de Mercantil, etc.). Las
             Transferencias bancarias NUNCA identifican su destino con un número de teléfono -- solo con un
             número de cuenta bancaria. Ante cualquier duda sobre el "tipo", esta única señal decide todo.
           - "destino_telefono_o_cuenta" (ver su descripción en el schema) es OBLIGATORIO en TODO comprobante de
             tipo Pago Móvil o Transferencia: copia ahí el número de teléfono o de cuenta del destino tal cual
             (sin acortar ni enmascarar), aunque ya estés seguro del "tipo" elegido. El servidor vuelve a
             verificar el tipo automáticamente a partir de este número y lo corrige si hace falta -- un
             "destino_telefono_o_cuenta" vacío o mal copiado es lo único que hace que un error de clasificación no
             se pueda arreglar después. Es el punto donde más dinero se ha perdido del cuadre por error de
             clasificación.
           - Otras señales típicas de categoría (3): cédula o RIF del emisor y receptor, número de referencia,
             screenshot de app bancaria (BDV, Mercantil, Banesco, etc.), no de un terminal físico.
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
             destino — ver la "REGLA DE ORO" de la categoría (3) arriba. Si el beneficiario se identifica
             con un número de teléfono en vez de una cuenta, NO es esta categoría, es categoría (3) Pago Móvil,
             aunque la pantalla tenga un campo "Cuenta origen" o el título no diga "Pago Móvil".
           - NO tiene estructura de terminal/lote/lote aceptado DE TARJETA (tabla Compra/Anulada/Total con
             "Terminal ... nro" — esa es la categoría (2), ver más abajo la palabra "LOTE").
           - CASO REAL — portal bancario de la empresa ("BDVenlínea empresas", "Mercantil Empresas", etc.)
             mostrando un listado de créditos/abonos recibidos (ej. título "Consulta de Créditos", con columnas
             "Beneficiario", "N° Documento", "N° Cuenta", "Monto", "N° Lote", "Fecha", "Ref. del abono",
             "Estatus"): esto SÍ es Transferencia (categoría 4), NO "Otro" y NO categoría (2), aunque tenga una
             columna llamada "N° Lote". ⚠️ Ese "N° Lote" es solo el número de referencia interno del banco para
             ESE abono/depósito individual — NO tiene relación con "Cierre de Lote"/"Transmisión de Lote" de un
             terminal de tarjeta (categoría 2): no hay tabla de Compra/Anulada/Total, no hay "Terminal ... nro",
             es una fila de una consulta de movimientos/créditos con "Beneficiario" y "N° Cuenta" (identifica un
             destino por cuenta bancaria, la señal estructural de categoría 4) y un "Estatus" tipo "Pago Exitoso".
             Usa el número de la columna "Monto" como "monto", y copia el número de "N° Cuenta" (los dígitos
             visibles, con asteriscos si están enmascarados) en "destino_telefono_o_cuenta". Confundir este formato
             con un cierre de lote de tarjeta y clasificarlo como "Otro" es un error real que ya ha pasado y
             hace desaparecer transferencias genuinas del cuadre — antes de usar "Otro", revisa si la pantalla
             tiene "Beneficiario" + "Monto" + "Estatus" (transferencia/consulta de créditos bancaria) en vez de
             preguntarte solo si la palabra "Lote" aparece en algún lado.
           - PROHIBIDO clasificar como Transferencia cualquier monto que en la MISMA imagen esté acompañado, en
             cualquier parte de la foto (arriba, abajo, al lado), por las palabras "TRANSMISIÓN DE LOTE" o
             "LOTE ACEPTADO" — sin importar que la foto tenga varios recibos distintos pegados uno junto al otro.
             Si tienes dudas sobre a cuál recibo pertenece un monto en una foto con múltiples recibos, y alguno
             de esos recibos en la misma imagen dice "LOTE", clasifica ese monto como categoría (2), no (4).

        5) EFECTIVO (conteo de billetes físicos) -- categoría especial, DISTINTA a todas las demás:
           - Es una foto de billetes físicos (dólares y/o bolívares) puestos sobre una mesa, mostrador, o en
             la mano, para contar cuánto efectivo hay -- NO es un comprobante de pago ni un cierre de
             sistema, y NO se compara contra ningún total del sistema A2 (por eso "monto" y
             "destino_telefono_o_cuenta" no aplican aquí -- déjalos en 0 y "" respectivamente).
           - Identifica CADA billete visible en la foto por su denominación IMPRESA (el número grande, ej.
             "20", "5", "1" para dólares; el valor impreso para bolívares) y su moneda (USD o Bs) -- algunos
             billetes pueden estar boca abajo, al revés, o parcialmente tapados por otro billete de la pila;
             identifícalos igual por el número impreso que sí se alcance a ver, sin importar la orientación.
           - Agrupa por denominación: cuenta cuántos billetes hay de cada valor, para cada moneda por
             separado, y repórtalo en "billetes_usd" y "billetes_bs" (arreglos de {"denominacion":
             N, "cantidad": N}). Ej. si ves 3 billetes de $20, 2 de $5, y 10 de $1: billetes_usd =
             [{"denominacion": 20, "cantidad": 3}, {"denominacion": 5, "cantidad": 2}, {"denominacion": 1,
             "cantidad": 10}]. Si la foto solo tiene billetes de una moneda, deja el arreglo de la otra
             moneda vacío ([]).
           - Un mismo billete NUNCA se cuenta dos veces, incluso si aparece parcialmente detrás de otro en la
             pila o abanico de billetes -- cuenta cada billete físico UNA sola vez. Si genuinamente no puedes
             distinguir cuántos billetes hay en una pila muy gruesa (no se ven los bordes individuales), cuenta
             los que sí puedas distinguir con confianza y no inventes una cantidad para el resto.

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
        La transcripción CORRECTA es DOS elementos separados, AMBOS con tipo="Cierre de Lote / Reporte de
        Cierre" (⚠️ NUNCA "Reporte Interno del Sistema" — el encabezado de ESTE documento es "BANCO DE
        VENEZUELA", un banco, no "TERA...C.A."; que el nombre "TERA SUMINISTROS" también aparezca impreso más
        abajo como afiliado NO cambia esto, ver la regla clave del campo "tipo" de arriba): el primero con
        total_fila_mc_visa_debit=1627.66 (el resto en 0) y terminal_identificador "BDV T:1002 L:499"; el
        segundo con total_fila_debito=7309.51 (el resto en 0) y terminal_identificador "BDV T:2002 L:141".
        Etiquetar estos dos como "Reporte Interno del Sistema" es un error real que ya ha pasado: sus montos
        quedan completamente excluidos del cuadre (esa categoría no se compara contra nada), desapareciendo
        miles de bolívares de tarjeta de débito que sí estaban en el sistema.
        Ejemplo real #2 — MISMO patrón, otra foto distinta (para que veas que no es un caso aislado):
            REPORTE DE CIERRE #1: "CIERRE CREDITO T:1002 L:497", TARJETA CREDITO en Bs. 0,00, MASTER/VISA
            DEBITO en Bs. 1.949,89.
            REPORTE DE CIERRE #2 (impreso justo debajo, en la misma foto): "CIERRE DEBITO T:2002 L:139",
            TARJETA DEBITO MAESTRO en Bs. 11.341,32.
        La transcripción CORRECTA es, otra vez, DOS elementos, AMBOS tipo="Cierre de Lote / Reporte de
        Cierre": total_fila_mc_visa_debit=1949.89 para el primero, y total_fila_debito=11341.32 para el
        segundo — el segundo NO se puede omitir aunque el primer bloque ya "se vea completo" con su propia
        línea "APROBADO".
        Reportar solo UNO de los dos reportes, mezclar sus cifras en un solo elemento, o etiquetarlos como
        "Reporte Interno del Sistema" en vez de "Cierre de Lote / Reporte de Cierre", hace desaparecer dinero
        real del cuadre — son los errores más comunes cuando dos reportes de cierre bancario comparten una
        sola foto.
        Ejemplo real #3 — este error ya volvió a pasar incluso teniendo esta misma instrucción por escrito, así
        que léelo con doble cuidado: una sola foto con dos "REPORTE DE CIERRE" de Banco de Venezuela impresos
        uno debajo del otro, MISMA estructura que el ejemplo #1:
            REPORTE DE CIERRE #1: "CIERRE CREDITO T:1002 L:502" / "MASTER/VISA DEBITO T:1002 L:502" en el
            encabezado. Cuerpo: sección "TARJETA CREDITO" con TOTAL 0 Bs. 0,00 (ignorar, está vacía). Sección
            "MASTER/VISA DEBITO" con COMPRA 2 Bs. 18.135,68 y TOTAL 2 Bs. 18.135,68 — este es el monto real,
            va en total_fila_mc_visa_debit.
            REPORTE DE CIERRE #2 (impreso justo debajo, mismo rollo de papel): "CIERRE DEBITO T:2002 L:144" en
            el encabezado. Cuerpo: sección "TARJETA DEBITO" con MAESTRO 6 Bs. 24.529,61, COMPRA 6 Bs. 24.529,61,
            TOTAL 6 Bs. 24.529,61 — va en total_fila_debito.
            La transcripción CORRECTA es DOS elementos, AMBOS con reportes_en_esta_foto=2 (porque hay 2 reportes
            en esta foto): el primero con tipo="Cierre de Lote / Reporte de Cierre",
            total_fila_mc_visa_debit=18135.68, terminal_identificador="BDV T:1002 L:502"; el segundo con
            tipo="Cierre de Lote / Reporte de Cierre", total_fila_debito=24529.61,
            terminal_identificador="BDV T:2002 L:144".
            LO QUE YA PASÓ EN LA REALIDAD (el error a evitar): se generó UN SOLO elemento para toda la foto,
            con un monto pequeño e incorrecto (ni 18.135,68 ni 24.529,61) — es decir, el segundo reporte se
            perdió por completo y encima el monto del primero se transcribió mal. Resultado: Bs. 42.665,29
            reales de tarjeta de débito desaparecieron del cuadre en una sola foto. NO te apures a "terminar"
            con el primer bloque "APROBADO"/TOTAL que veas: sigue leyendo hacia abajo, cuenta los reportes con
            "reportes_en_esta_foto" ANTES de transcribir montos, y verifica cada número dígito por dígito contra
            la sección correcta (no confundas "18.135,68" con un número de tres cifras) antes de darlo por bueno.

        CASO ESPECIAL 4 — un ÚNICO reporte de cierre (UN SOLO "L:", UN SOLO encabezado, NO dos reportes
        distintos como en el CASO ESPECIAL 3 de arriba) que trae DOS SECCIONES DE TOTAL distintas dentro de
        ESE MISMO reporte: una sección "TARJETA CREDITO"/"CIERRE CREDITO" con su propio TOTAL, Y ADEMÁS una
        sección "MASTER/VISA DEBITO" o "TARJETA DEBITO" con OTRO TOTAL, ambas bajo el MISMO número de lote
        ("L:") y el mismo encabezado. Esto es DISTINTO del CASO ESPECIAL 3: ahí eran dos reportes con dos "L:"
        diferentes que van en DOS elementos separados; aquí es UN SOLO reporte (un solo "L:") que simplemente
        reporta dos tipos de venta (crédito y débito) juntos — va en UN SOLO elemento de "comprobantes_leidos",
        pero con AMBOS campos "total_fila_credito" Y "total_fila_mc_visa_debit"/"total_fila_debito" llenos a
        la vez (no dejes uno en 0 solo porque ya llenaste el otro).
        ⚠️ Este es exactamente el error que ya pasó en la realidad: la IA transcribió el total de
        "TARJETA CREDITO" y se detuvo ahí, dando por completo el reporte — sin seguir leyendo hacia abajo,
        en la MISMA sección/bloque, hasta encontrar el segundo total de "MASTER/VISA DEBITO" que venía justo
        debajo, dentro de ese mismo reporte. El resultado fue que ese segundo monto (un total real de venta a
        débito) desapareció por completo del cuadre.
        Ejemplo real — un reporte de Banco de Venezuela con encabezado "CIERRE CREDITO T:1002 L:503" seguido,
        en la misma línea o justo debajo, de "MASTER/VISA DEBITO T:1002 L:503" (el MISMO "L:503" en ambos,
        señal de que es UN SOLO reporte, no dos): el cuerpo trae primero una sección con "TOTAL 1 Bs.
        11.192,61" (esta es la sección de crédito → total_fila_credito=11192.61), y ADEMÁS, más abajo en el
        mismo bloque, una sección "MASTER/VISA DEBITO" con "COMPRA 1 Bs. 13.509,81" y "TOTAL 1 Bs. 13.509,81"
        (esta es la sección de débito → total_fila_mc_visa_debit=13509.81). La transcripción CORRECTA es UN
        elemento con tipo="Cierre de Lote / Reporte de Cierre", total_fila_credito=11192.61 Y
        total_fila_mc_visa_debit=13509.81 AL MISMO TIEMPO (ambos distintos de 0), terminal_identificador="BDV
        T:1002 L:503". Transcribir solo el total de crédito (11.192,61) y dejar total_fila_mc_visa_debit en 0,
        como si el reporte ya hubiera terminado ahí, es el error real que ya pasó: Bs. 13.509,81 de venta a
        débito desaparecieron del cuadre en esa sola foto.
        REGLA PRÁCTICA para no repetir este error: cuando un reporte tenga un encabezado que menciona DOS tipos
        de tarjeta a la vez (ej. "CIERRE CREDITO T:X L:Y" seguido de "MASTER/VISA DEBITO T:X L:Y" con el MISMO
        "L:"), es una señal segura de que ese reporte trae DOS totales que transcribir (uno de crédito y uno de
        débito), NO uno solo — sigue leyendo el bloque completo hasta encontrar AMBOS "TOTAL" antes de dar el
        reporte por transcrito, aunque el primer total que encuentres ya "se vea completo" por sí solo.

        CASO ESPECIAL 5 — COMBINACIÓN de los dos casos anteriores en la MISMA foto: dos reportes de cierre
        DISTINTOS (dos "L:" diferentes, como en el CASO ESPECIAL 3), Y ADEMÁS uno de esos dos reportes trae la
        estructura de doble total del CASO ESPECIAL 4 (crédito Y débito juntos bajo el mismo "L:", aunque uno
        de los dos números sea "0,00"). Esto va en DOS elementos (uno por cada "L:" distinto, igual que el
        CASO ESPECIAL 3) — pero el elemento del reporte con doble total lleva AMBOS campos "total_fila_*"
        llenos (igual que el CASO ESPECIAL 4), no solo uno.
        Ejemplo real — dos reportes de Banco de Venezuela en la misma foto, con distinta hora ("H:") cada uno
        (señal de que son reportes genuinamente distintos, no repetidos):
          • Primer reporte, "H:144137", encabezado "CIERRE CREDITO T:1002 L:502" seguido de "MASTER/VISA
            DEBITO T:1002 L:502" (mismo "L:502" en ambos -- es el CASO ESPECIAL 4 dentro de este reporte):
            trae "TARJETA CREDITO ... TOTAL 0 Bs. 0,00" (el crédito de ESTE reporte da cero, sigue siendo un
            campo real que transcribir, no lo omitas) y, más abajo en el mismo bloque, "MASTER/VISA DEBITO
            ... TOTAL 2 Bs. 18.135,68". Va en UN elemento: total_fila_credito=0, total_fila_mc_visa_debit=
            18135.68, terminal_identificador="BDV T:1002 L:502".
          • Segundo reporte, "H:144146" (hora DISTINTA a la del primero -- confirma que es un reporte
            genuinamente aparte, no el mismo repetido), encabezado "CIERRE DEBITO T:2002 L:144": trae
            "TARJETA DEBITO ... TOTAL 6 Bs. 24.529,61". Va en un SEGUNDO elemento separado: total_fila_debito=
            24529.61, terminal_identificador="BDV T:2002 L:144".
          • AMBOS elementos llevan "reportes_en_esta_foto"=2 (es la misma foto, dos reportes contados).
        El error real que ya pasó con esta foto: transcribir SOLO el primer reporte (y encima con el monto de
        débito mal leído, perdiendo dígitos), dejando el segundo reporte completo (Bs. 24.529,61) fuera del
        cuadre -- una foto así puede perder más de Bs. 40.000 de tarjeta si no se separan bien los dos
        reportes. Antes de dar una foto de tarjeta por transcrita, busca explícitamente un segundo bloque
        "REPORTE DE CIERRE"/"TRANSMISIÓN DE LOTE" más abajo en la misma imagen, aunque el primero ya parezca
        completo por sí solo.

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
           comprobante ni cierre de ningún medio de pago. Antes de usar "Otro", revisa PRIMERO si el
           documento tiene un número de teléfono de destino (ver "REGLA DE ORO" de la categoría 3 arriba) —
           si lo tiene, es Pago Móvil, sin importar qué tan distinto o inusual se vea el diseño de la
           pantalla (colores, apps de terceros como "SUICHE7B"/"Dinero Rápido", nombres de persona en vez de
           bancos, etc.). Una pantalla de pago que no reconoces todavía NO es motivo para "Otro" -- revisa si
           encaja en alguna de las categorías (1)-(5) de arriba, incluyendo el caso de plataformas no
           bancarias como Cashea explicado en la categoría (2) — un panel de "Cierre de caja del día" de una
           app de pagos SIEMPRE tiene una categoría correcta entre (1)-(5), nunca es "Otro".
        d. OBLIGATORIO: "comprobantes_leidos" debe tener AL MENOS {len(archivos)} elementos — como mínimo
           uno por cada archivo recibido (ver el total indicado en cada etiqueta "--- Archivo #N de TOTAL ---").
           No omitas ningún archivo. Si una imagen está borrosa, ilegible o no corresponde a ningún comprobante
           de pago reconocible, IGUAL inclúyela en la lista con tipo "Otro", monto 0, y explica el motivo
           en analisis_detallado — pero nunca la excluyas de la lista.
           EXCEPCIÓN (más elementos que archivos): si una misma foto contiene DOS documentos físicos distintos
           que deben reportarse por separado (ver "CASO ESPECIAL 2": recibo de compra individual + reporte de
           cierre del mismo terminal en una sola foto; o "CASO ESPECIAL 3": dos reportes de cierre de lote
           DISTINTOS —cada uno con su propio "L:"— impresos uno debajo del otro en la misma foto), agrega DOS
           elementos para ese archivo (mismo nombre de "archivo" en ambos). En ese caso el total de elementos
           será MAYOR a {len(archivos)}, y eso está bien — nunca sacrifiques la separación de esos dos
           documentos solo por igualar el conteo.

        Usa la herramienta "registrar_auditoria" para entregar tu resultado estructurado.
        """

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
# si el prefijo de carpeta (api/index.py -> /api/auditar) llega ya recortado
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

# ---------------------------------------------------------------------------
# /api/reconciliar -- combina los resultados de TODOS los lotes en una sola
# reconciliación final. Necesario porque cada lote se audita por separado
# (ver nota de "LÍMITE DE TAMAÑO" al inicio del archivo): la reconciliación
# que devuelve /api/auditar para un lote individual solo tiene sentido para
# ESE lote -- compararla contra el total del sistema (que es del DÍA
# completo) mostraría un "descuadre" falso mientras falten lotes por
# procesar. index.html junta los "comprobantes_leidos" de todos los lotes
# ya auditados y llama aquí UNA sola vez al final con la lista completa.
# No llama a Claude -- es puro cálculo Python, así que es rápido y el cuerpo
# de la petición es liviano (nada de imágenes, solo texto/JSON).
# ---------------------------------------------------------------------------
@app.post("/api/reconciliar", dependencies=[Depends(verificar_secreto)])
async def reconciliar_comprobantes(request: Request):
    try:
        cuerpo = await request.json()
        comprobantes_leidos = cuerpo.get("comprobantes_leidos") or []
        totales_json = cuerpo.get("totales_json") or "{}"
        reconciliacion = calcular_reconciliacion(comprobantes_leidos, totales_json)
        logger.info("Reconciliación FINAL combinada (Python): %s", reconciliacion)
        return {"status": "success", "reconciliacion_calculada": reconciliacion}
    except Exception as e:
        logger.error("Excepción no controlada en /api/reconciliar: %s\n%s", e, traceback.format_exc())
        return {"status": "error", "message": str(e)}
@app.post("/reconciliar", dependencies=[Depends(verificar_secreto)])
async def reconciliar_comprobantes_alt(request: Request):
    return await reconciliar_comprobantes(request)
