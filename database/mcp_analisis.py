# ==============================================================================
# backend/database/mcp_analisis.py
# Tools de análisis del negocio para claves de nivel 2 (solo lectura).
#
# NO calcula nada nuevo: reutiliza EXACTAMENTE las mismas funciones que el
# Análisis Inteligente del gestor (database/analitica.py + analisis/reglas.py),
# así el agente y la pantalla del dueño ven los mismos números. Los hallazgos
# salen del motor de reglas, no del LLM: análisis no alucinativo.
#
# Privacidad (decisión del dueño):
#   - SÍ se exponen agregados (ingresos, ganancia, margen, gastos, valor de
#     inventario a costo).
#   - NUNCA se exponen costos unitarios por producto ni lotes con su costo.
#
# Costo backend: generar() es UNA query pesada. Ambos tools comparten un caché
# TTL de 5 minutos por tenant+rango: una ráfaga de preguntas del agente
# ("cómo voy", "y mis pendientes?", "y el stock?") golpea la base UNA vez.
# ==============================================================================

import threading
import time
from datetime import date, timedelta

from analisis.reglas import evaluar
from database.analitica import generar
from database.helpers import ventana_ts_de_rango, hoy_negocio
from database.imagenes import _get_tenant_plan

# Periodos que aceptan los tools (nombres pensados para un agente IA).
# Se normalizan con _normalizar_periodo: el agente puede escribir variantes
# naturales ("30 días", "este mes", "histórico") y todas caen bien.
PERIODOS = {"mes", "30dias", "90dias", "todo"}

_ALIASES_PERIODO = {
    "": "mes", "mes actual": "mes", "este mes": "mes", "1mes": "mes",
    "30 dias": "30dias", "30 días": "30dias", "1 mes": "30dias",
    "90 dias": "90dias", "90 días": "90dias", "3 meses": "90dias",
    "historico": "todo", "histórico": "todo", "historial": "todo", "todo el negocio": "todo",
}


def _normalizar_periodo(periodo: str) -> str:
    p = (periodo or "mes").strip().lower()
    return _ALIASES_PERIODO.get(p, p)


def _rango_periodo(tenant_id: str, periodo: str) -> tuple:
    """
    (desde_d, hasta_d) según el periodo ya normalizado. 'hasta' EXCLUSIVA.
    Se calcula directo con hoy_negocio (mismo camino que 30dias/90dias, que
    funciona en producción) — resolver_rango_q queda fuera por si acaso.
    """
    hoy = hoy_negocio(tenant_id)
    if periodo == "todo":
        return None, None
    if periodo == "mes":
        inicio = hoy.replace(day=1)
        if hoy.month == 12:
            siguiente = date(hoy.year + 1, 1, 1)
        else:
            siguiente = date(hoy.year, hoy.month + 1, 1)
        return inicio, siguiente
    dias = 30 if periodo == "30dias" else 90
    return hoy - timedelta(days=dias - 1), None


# Filtros de hallazgos: cada categoría agrupa los tipos del motor de reglas.
# Filtrar es gratis (Python); NUNCA significa otra query a la base.
_FILTROS_TIPO = {
    "todos": None,
    "dinero": {"margen_bajo", "gastos_comodin", "gastos_pendientes", "concentracion_dias"},
    "stock": {"quiebre_best_seller", "stock_muerto", "lotes_inconsistentes", "vendido_sin_lote"},
    "ventas": {"tendencia_baja", "venta_nocturna", "mes_pico", "ticket_gap"},
}

_ALIASES_TIPO = {
    "todo": "todos", "general": "todos",
    "cobros": "dinero", "gastos": "dinero", "dinero": "dinero",
    "inventario": "stock", "productos": "stock", "stock": "stock",
    "ventas": "ventas",
}

_CACHE_TTL_SEG = 300  # 5 minutos
_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


def _analisis_completo(tenant_id: str, periodo: str) -> dict:
    """generar() + evaluar() con caché TTL. Lanza ValueError si el plan no es Plus."""
    if _get_tenant_plan(tenant_id) != "plus":
        raise ValueError("El Análisis Inteligente es exclusivo del plan Plus.")

    periodo = _normalizar_periodo(periodo)
    if periodo not in PERIODOS:
        raise ValueError(
            f"Periodo inválido: '{periodo}'. Usa 'mes' (mes contable actual), "
            "'30dias', '90dias' o 'todo'."
        )

    cache_key = f"{tenant_id}:{periodo}"
    ahora = time.monotonic()
    with _lock:
        cached = _cache.get(cache_key)
        if cached and ahora - cached[0] < _CACHE_TTL_SEG:
            return cached[1]

    desde_d, hasta_d = _rango_periodo(tenant_id, periodo)
    desde_ts, hasta_ts = ventana_ts_de_rango(tenant_id, desde_d, hasta_d)

    paquete = generar(tenant_id, desde_ts, hasta_ts, desde_d, hasta_d)
    hechos = paquete.pop("hechos", {})
    paquete["hallazgos"] = evaluar(hechos)
    # Ventana PEDIDA (distinta de rango_analizado, que muestra los días con
    # ventas reales: si el mes no tiene ventas, rango_analizado se ve de 1 día
    # aunque la ventana sea de 30). El agente lo usa para explicar al dueño.
    paquete["rango_solicitado"] = {
        "desde": desde_d.isoformat() if desde_d else None,
        "hasta": (hasta_d - timedelta(days=1)).isoformat() if hasta_d else None,
        "nota": (
            "'desde'/'hasta' son la ventana pedida; 'rango_analizado' son los días "
            "con ventas registradas dentro de esa ventana."
        ),
    }

    with _lock:
        # Un tenant puede pedir dos periodos en la misma ráfaga; el caché
        # crece acotado (4 periodos por tenant) y se refresca por TTL.
        if len(_cache) > 500:
            _cache.clear()
        _cache[cache_key] = (ahora, paquete)
    return paquete


def resumen_negocio(tenant_id: str, periodo: str = "mes") -> dict:
    """KPIs agregados del periodo. Sin costos unitarios ni lotes."""
    paquete = _analisis_completo(tenant_id, periodo)
    return {
        "rango_solicitado": paquete.get("rango_solicitado"),
        "rango_analizado": paquete.get("rango_analizado"),
        "mensaje": paquete.get("mensaje") or "",
        "metricas": paquete.get("metricas") or {},
    }


def hallazgos_inteligentes(tenant_id: str, periodo: str = "mes", tipo: str = "todos") -> dict:
    """Hallazgos del motor de reglas (puntos ciegos + recomendaciones)."""
    tipo = (tipo or "todos").strip().lower()
    tipo = _ALIASES_TIPO.get(tipo, tipo)
    if tipo not in _FILTROS_TIPO:
        raise ValueError(
            f"Tipo inválido: '{tipo}'. Usa 'todos', 'dinero', 'stock' o 'ventas'."
        )
    paquete = _analisis_completo(tenant_id, periodo)
    tipos_permitidos = _FILTROS_TIPO[tipo]
    hallazgos = paquete.get("hallazgos") or []
    if tipos_permitidos is not None:
        hallazgos = [h for h in hallazgos if h.get("tipo") in tipos_permitidos]
    return {
        "rango_solicitado": paquete.get("rango_solicitado"),
        "rango_analizado": paquete.get("rango_analizado"),
        "mensaje": paquete.get("mensaje") or "",
        "tipo_filtro": tipo,
        "total": len(hallazgos),
        "hallazgos": hallazgos,
    }
