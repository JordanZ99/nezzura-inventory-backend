# ==============================================================================
# backend/analisis/reglas.py
# Motor determinista del Análisis Inteligente: hechos ya calculados en SQL →
# hallazgos con severidad, ventana y recomendación. Sin escrituras y sin LLM.
#
# Reglas de escritura de cada hallazgo (valen para todos):
#   1. El título es la CONCLUSIÓN ("Te faltan 3 productos que sí vendes"),
#      nunca el dato crudo.
#   2. El detalle es la evidencia, en dinero y unidades.
#   3. Cada tarjeta declara su VENTANA: las reglas de inventario miran 90 días
#      aunque el usuario analice otro rango, y decirlo es la diferencia entre
#      un reporte y una adivinanza.
#   4. Nada de jerga interna (nada de "PEPS", "stock muerto", "lote").
# ==============================================================================

from datetime import date

# ── Umbrales (calibrados con el tenant Kali; validar con un segundo giro) ──
VENTAS_COLD_START = 20
TOP_BEST_SELLER = 10
DIAS_VENTANA = 90
STOCK_MUERTO_MIN_COSTO = 100.0
STOCK_MUERTO_PCT_ROJO = 0.30
TENDENCIA_RATIO = 0.70
MARGEN_HOLGURA_PTS = 5.0
TOP_MARGEN = 15
CONCENTRACION_PCT = 0.60
CONCENTRACION_DIAS = 3
GASTOS_OTROS_PCT = 0.30
PENDIENTES_MIN = 5
PENDIENTES_PCT_MES = 0.15
NOCTURNA_PCT = 0.15
NOCTURNA_MIN = 5
TICKET_GAP_RATIO = 1.4

# Ventanas declaradas al usuario (texto corto, se muestra como etiqueta).
V_PERIODO = "el período elegido"
V_90 = "últimos 90 días"
V_MES = "mes en curso"
V_8SEM = "últimas 8 semanas"

_ORDEN = {"rojo": 0, "amarillo": 1, "verde": 2}
_MESES = (
    "", "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)


def _dinero(valor) -> str:
    return f"${float(valor):,.0f}"


def _hallazgo(tipo, severidad, titulo, detalle, recomendacion, ventana=V_PERIODO, accion=None) -> dict:
    h = {
        "tipo": tipo,
        "severidad": severidad,
        "titulo": titulo,
        "detalle": detalle,
        "recomendacion": recomendacion,
        "ventana": ventana,
    }
    if accion:
        h["accion"] = accion
    return h


def _ir_a(href: str, que: str) -> dict:
    return {"texto": que, "href": href}


def _mes_legible(iso: str) -> str:
    try:
        anio, mes = iso.split("-")
        return f"{_MESES[int(mes)]} {anio}"
    except (ValueError, IndexError):
        return iso


def _unidades(valor) -> str:
    """'1 unidad' / '2 unidades' — sin decimales, como lo ve la dueña."""
    n = float(valor or 0)
    if abs(n - round(n)) < 0.05:
        n = int(round(n))
        return f"{n} unidad" if n == 1 else f"{n} unidades"
    return f"{n:.1f} unidades".replace(".0 unidades", " unidades")


def evaluar(hechos: dict) -> list[dict]:
    """Devuelve hallazgos ordenados por severidad. Lista vacía en cold start."""
    if hechos.get("cold_start"):
        return []

    hallazgos: list[dict] = []
    hallazgos.extend(_quiebre(hechos))
    for h in (_sin_movimiento(hechos), _tendencia(hechos), _concentracion(hechos),
              _gastos_otros(hechos), _error_inventario(hechos), _sin_lote(hechos)):
        if h:
            hallazgos.append(h)
    hallazgos.extend(_margen_bajo(hechos))
    for h in (_pendientes(hechos), _nocturna(hechos)):
        if h:
            hallazgos.append(h)
    hallazgos.extend(_informativos(hechos))

    hallazgos.sort(key=lambda h: _ORDEN.get(h["severidad"], 9))
    return hallazgos[:12]


# ── Rojo: cosas que están costando dinero ahora ─────────────────────────────

def _quiebre(hechos: dict) -> list[dict]:
    """Un SOLO hallazgo con los productos agotados que sí se vendían.

    Antes eran 3 tarjetas rojas casi idénticas; una sola con la lista se lee
    mucho mejor y la acción es la misma para todos."""
    top = sorted(hechos.get("productos_90") or [], key=lambda p: p["ingresos"], reverse=True)[:TOP_BEST_SELLER]
    agotados = [p for p in top if p["ingresos"] > 0 and p["stock"] <= 0]
    if not agotados:
        return []

    nombres = [f"{p['producto']} (~{_unidades(p['unidades'] / 3)}/mes)" for p in agotados[:4]]
    if len(agotados) > 4:
        nombres.append(f"y {len(agotados) - 4} más")
    n = len(agotados)
    return [_hallazgo(
        "quiebre_best_seller", "rojo",
        f"Te falta stock de {n} producto{'s' if n > 1 else ''} que sí vendes",
        f"Sin existencias: {', '.join(nombres)}. En esos 90 días movableon "
        f"{_dinero(sum(p['ingresos'] for p in agotados))}.",
        "Reabastécelos cuanto antes: son los que más rotan de tu negocio.",
        ventana=V_90,
        accion=_ir_a("/inventario", "Ir a Inventario"),
    )]


def _sin_movimiento(hechos: dict) -> dict | None:
    """Dinero parado: inventario que nadie está comprando."""
    sin_mover = [
        p for p in (hechos.get("productos_90") or [])
        if p["ingresos"] <= 0 and p["valor_costo"] > 0 and p["stock"] > 0
    ]
    valiosos = [p for p in sin_mover if p["valor_costo"] >= STOCK_MUERTO_MIN_COSTO]
    if not valiosos:
        return None
    valor = sum(p["valor_costo"] for p in valiosos)
    inventario = float(hechos.get("valor_inventario_costo") or 0)
    pct = (valor / inventario) if inventario > 0 else 0
    rojo = pct >= STOCK_MUERTO_PCT_ROJO
    nombres = ", ".join(p["producto"] for p in valiosos[:3])
    extra = f" y {len(valiosos) - 3} más" if len(valiosos) > 3 else ""
    unidades = sum(p["stock"] for p in valiosos)
    return _hallazgo(
        "stock_muerto",
        "rojo" if rojo else "amarillo",
        f"Tienes {_dinero(valor)} de producto que nadie está comprando",
        f"{len(valiosos)} producto(s) llevan 90 días sin venderse una sola pieza "
        f"({_unidades(unidades)} en total): {nombres}{extra}. Es el {pct * 100:.0f}% "
        "de lo que tienes invertido en inventario.",
        "Bájale el precio, arma un combo o deja de reponerlo: ese dinero está parado.",
        ventana=V_90,
        accion=_ir_a("/inventario", "Ir a Inventario"),
    )


# ── Amarillo: conviene revisarlo esta semana ───────────────────────────────

def _tendencia(hechos: dict) -> dict | None:
    semanas: list[float] = hechos.get("semanas_8") or []
    if len(semanas) < 8:
        return None
    # Con pocas semanas con venta el promedio no significa nada: saldría
    # "caída del 97%" solo porque el negocio lleva semanas sin registrar.
    if sum(1 for s in semanas if s > 0) < 4:
        return None
    prom8 = sum(semanas) / 8
    prom2 = sum(semanas[-2:]) / 2
    if prom8 <= 0 or prom2 >= TENDENCIA_RATIO * prom8:
        return None
    return _hallazgo(
        "tendencia_baja", "amarillo",
        "Estás vendiendo menos que en semanas anteriores",
        f"Las últimas 2 semanas promedian {_dinero(prom2)} contra {_dinero(prom8)} "
        f"de las últimas 8 ({prom2 / prom8 * 100:.0f}% de antes).",
        "Revisa si se te acabó stock de lo que más rota o si bajaron los clientes esos días.",
        ventana=V_8SEM,
    )


def _margen_bajo(hechos: dict) -> list[dict]:
    vendidos = [p for p in (hechos.get("productos_90") or []) if p["ingresos"] > 0]
    if len(vendidos) < 3:
        return []
    ingresos = sum(p["ingresos"] for p in vendidos)
    ganancia = sum(p["ganancia"] for p in vendidos)
    if ingresos <= 0:
        return []
    margen_prom = ganancia / ingresos * 100
    piso = margen_prom - MARGEN_HOLGURA_PTS
    top = sorted(vendidos, key=lambda p: p["ingresos"], reverse=True)[:TOP_MARGEN]
    salida = []
    for p in top:
        margen = p["ganancia"] / p["ingresos"] * 100
        if margen >= piso:
            continue
        salida.append(_hallazgo(
            "margen_bajo", "amarillo",
            f"Dejas poca ganancia en {p['producto']}",
            f"Te quedas con el {margen:.0f}% de cada venta, cuando en el resto de tus "
            f"productos te quedas con el {margen_prom:.0f}%. Es de los que más vendes "
            f"({_dinero(p['ingresos'])} en 90 días).",
            "Súbele el precio un poco o revisa cuánto te cuesta comprarlo.",
            ventana=V_90,
            accion=_ir_a("/inventario", "Ir a Inventario"),
        ))
        if len(salida) == 2:
            break
    return salida


def _concentracion(hechos: dict) -> dict | None:
    dias = [d for d in (hechos.get("ingresos_por_dow") or []) if d["ingresos"] > 0]
    total = sum(d["ingresos"] for d in dias)
    if total <= 0 or len(dias) <= CONCENTRACION_DIAS:
        return None
    top = sorted(dias, key=lambda d: d["ingresos"], reverse=True)[:CONCENTRACION_DIAS]
    pct = sum(d["ingresos"] for d in top) / total
    if pct <= CONCENTRACION_PCT:
        return None
    nombres = ", ".join(d["dia"] for d in top)
    flojos = sorted(dias, key=lambda d: d["ingresos"])[0]
    return _hallazgo(
        "concentracion_dias", "amarillo",
        f"Casi todo se te vende en {CONCENTRACION_DIAS} días a la semana",
        f"El {pct * 100:.0f}% de tus ingresos entran {nombres}. El día más flojo es "
        f"{flojos['dia']} ({_dinero(flojos['ingresos'])}).",
        f"Prueba una promo corta el {flojos['dia']} antes de concluir que no viene gente.",
        ventana=V_PERIODO,
    )


def _gastos_otros(hechos: dict) -> dict | None:
    categorias = hechos.get("gastos_por_categoria") or []
    total = sum(c["monto"] for c in categorias)
    if total <= 0:
        return None
    otros = next((c for c in categorias if c["categoria"].strip().lower() in ("otros", "otro")), None)
    if not otros or otros["monto"] / total <= GASTOS_OTROS_PCT:
        return None
    pct = otros["monto"] / total
    return _hallazgo(
        "gastos_comodin", "amarillo",
        "Buena parte de tus gastos no está en ninguna categoría",
        f"{_dinero(otros['monto'])} de {_dinero(total)} ({pct * 100:.0f}%) están como "
        "'Otros', sin saber en qué se te fue.",
        "Repártelos en categorías: lo que no puedes nombrar, no lo puedes recortar.",
        ventana=V_PERIODO,
    )


def _error_inventario(hechos: dict) -> dict | None:
    filas = hechos.get("lotes_inconsistentes") or []
    if not filas:
        return None
    nombres = ", ".join(f["producto"] for f in filas[:3])
    extra = f" y {len(filas) - 3} más" if len(filas) > 3 else ""
    return _hallazgo(
        "lotes_inconsistentes", "amarillo",
        f"Hay {len(filas)} producto{'s' if len(filas) > 1 else ''} con el inventario mal",
        f"En {nombres}{extra} el inventario quedó en negativo o con costo negativo. "
        "Es un error de captura, no un cálculo.",
        "Corrígelo en Inventario antes de volver a vender esos productos: el conteo "
        "automático se descuadra con el real.",
        ventana="ahora",
        accion=_ir_a("/inventario", "Ir a Inventario"),
    )


def _sin_lote(hechos: dict) -> dict | None:
    nombres = hechos.get("vendidos_sin_lote") or []
    if not nombres:
        return None
    return _hallazgo(
        "vendido_sin_lote", "amarillo",
        "Hay ventas que no están saliendo del inventario",
        f"{', '.join(nombres[:3])} se vendieron sin restar existencias. Tu stock real "
        "puede estar más alto de lo que dice el sistema.",
        "Revisa esas ventas: si el producto sí tenía existencias, el descuento no se aplicó.",
        ventana=V_PERIODO,
        accion=_ir_a("/inventario", "Ir a Inventario"),
    )


# ── Verde: informativo o una buena noticia ──────────────────────────────────

def _pendientes(hechos: dict) -> dict | None:
    n = int(hechos.get("pendientes_n") or 0)
    monto = float(hechos.get("pendientes_monto") or 0)
    gastos_mes = float(hechos.get("gastos_mes") or 0)
    pct = (monto / gastos_mes) if gastos_mes > 0 else 0
    if n < PENDIENTES_MIN and pct <= PENDIENTES_PCT_MES:
        return None
    return _hallazgo(
        "gastos_pendientes", "verde",
        f"Tienes {n} gasto{'s' if n > 1 else ''} sin confirmar",
        f"Suman {_dinero(monto)}" + (f", el {pct * 100:.0f}% de lo que gastaste este mes." if gastos_mes else "."),
        "Confírmalos o bórralos: hasta entonces no cuentan en tu utilidad.",
        ventana=V_MES,
        accion=_ir_a("/gastos", "Ir a Gastos"),
    )


def _nocturna(hechos: dict) -> dict | None:
    n = int(hechos.get("ventas_nocturnas") or 0)
    total = int(hechos.get("num_ventas") or 0)
    if total <= 0 or n < NOCTURNA_MIN or n / total <= NOCTURNA_PCT:
        return None
    return _hallazgo(
        "venta_nocturna", "verde",
        "Muchas ventas se registran de madrugada",
        f"El {n / total * 100:.0f}% de las ventas entra entre las 00:00 y las 05:59. "
        "Si no abres a esas horas, probablemente se capturan al día siguiente.",
        "Registra la venta el mismo día: así el corte de caja cuadra con lo que pasó.",
        ventana=V_PERIODO,
    )


def _informativos(hechos: dict) -> list[dict]:
    salida = []
    # Con un rango de un solo mes, "tu mejor mes" no dice nada: se compara
    # consigo mismo.
    if hechos.get("meses_analizados", 0) >= 2:
        mejor = hechos.get("mejor_mes") or {}
        if mejor.get("mes") and mejor.get("ingresos"):
            salida.append(_hallazgo(
                "mes_pico", "verde",
                f"Tu mejor mes fue {_mes_legible(mejor['mes'])}",
                f"Ese mes ingresaste {_dinero(mejor['ingresos'])}, por encima de los demás "
                "meses que analizaste.",
                "Repite lo que hiciste ese mes (stock, promo o temporada) en el siguiente.",
                ventana=V_PERIODO,
            ))
    proy = hechos.get("proyeccion_mes_actual")
    hoy: date | None = hechos.get("hoy")
    if proy and hoy and hoy.day >= 3:
        salida.append(_hallazgo(
            "proyeccion_mes", "verde",
            f"A este ritmo el mes cierra en {_dinero(proy)}",
            "Estimación: se proyecta lo que vas sumando al ritmo de los días que ya "
            "transcurrieron. No es una predicción.",
            "Úsala para decidir si compras hoy o esperas al corte.",
            ventana=V_MES,
        ))
    promedio = float(hechos.get("ticket_promedio") or 0)
    mediano = float(hechos.get("ticket_mediano") or 0)
    if mediano > 0 and promedio > mediano * TICKET_GAP_RATIO and int(hechos.get("num_ventas") or 0) >= VENTAS_COLD_START:
        salida.append(_hallazgo(
            "ticket_gap", "verde",
            "Tus compras grandes mandan en el total",
            f"El promedio por venta es {_dinero(promedio)} pero la mitad de tus ventas son "
            f"de {_dinero(mediano)} o menos: la venta de todos los días es más chica de lo "
            "que sugiere el promedio.",
            "No planees compras con el promedio en mente; la mediana describe mejor a tu cliente.",
            ventana=V_PERIODO,
        ))
    return salida
