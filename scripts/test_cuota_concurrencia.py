# ==============================================================================
# backend/scripts/test_cuota_concurrencia.py
# Test de concurrencia del guardián de cuota (migración 071).
#
# Crea un tenant TEMPORAL de prueba, lanza N reservas EN PARALELO con un
# cupo que solo alcanza para una, y verifica que exactamente UNA gana
# (el FOR UPDATE de reservar() serializa el espacio restante). Después
# prueba límites exactos, compensaciones y el crédito de reemplazo.
# Al final LIMPIA todo: DELETE del tenant → cascade de tenant_storage y
# storage_eventos. JAMÁS toca tenants reales.
#
# Uso (desde backend/):
#   python -m scripts.test_cuota_concurrencia
#
# Esperado: "✅ TEST COMPLETO: todas las aserciones pasaron"
# ==============================================================================

import sys
import threading
import uuid
from pathlib import Path

# Permitir importar database.* corriendo desde backend/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.conexion import execute, query  # noqa: E402
from database.almacenamiento import (  # noqa: E402
    confirmar, liberar_reserva, liberar_url, reservar,
)

MB = 1024 * 1024

fallos: list[str] = []


def assert_ok(condicion: bool, mensaje: str) -> None:
    if condicion:
        print(f"  [OK] {mensaje}")
    else:
        fallos.append(mensaje)
        print(f"  [FALLO] {mensaje}")


def main() -> None:
    tid = str(uuid.uuid4())
    url_falsa = f"https://res.cloudinary.com/test/upload/test_{uuid.uuid4().hex[:8]}.jpg"

    print(f"Creando tenant de prueba {tid[:8]}…")
    try:
        execute(
            "INSERT INTO tenants (id, empresa, plan) VALUES (%s, %s, %s)",
            (tid, f"TEST-CUOTA-{tid[:8]}", "basico"),
        )
    except Exception as e:
        print(f"❌ No se pudo crear el tenant de prueba (¿columna NOT NULL sin default?): {e}")
        sys.exit(1)

    # Límite leído EN VIVO de planes_config: el test funciona con el límite
    # que sea (500 MB hoy, 1 GB mañana) sin mantenimiento.
    fila_limite = query(
        "SELECT p.limite_bytes FROM tenants t "
        "JOIN planes_config p ON p.plan = t.plan WHERE t.id = %s",
        (tid,),
    )
    LIMITE = int(fila_limite[0]["limite_bytes"])
    CHUNK = int(LIMITE * 0.6)   # 0.6×LIMITE × 2 > LIMITE → solo cabe UNO
    MITAD = LIMITE // 2
    print(f"Límite del plan básico: {LIMITE / MB:.0f} MB "
          f"(chunk de carrera: {CHUNK / MB:.0f} MB, mitad: {MITAD / MB:.0f} MB)")

    try:
        # ── Test 1: carrera de reservas simultáneas ─────────────────────────
        print("\n[Test 1] 3 reservas de 600 MB EN PARALELO con 1 GB de límite:")
        barrera = threading.Barrier(3)
        resultados: list[dict] = []

        def worker() -> None:
            barrera.wait()
            resultados.append(reservar(tid, CHUNK))

        hilos = [threading.Thread(target=worker) for _ in range(3)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()

        ganadas = [r for r in resultados if r.get("ok")]
        bloqueadas = [r for r in resultados if not r.get("ok")]
        assert_ok(len(resultados) == 3, "las 3 reservas devolvieron resultado")
        assert_ok(len(ganadas) == 1, f"exactamente 1 reserva ganó (ganaron {len(ganadas)})")
        assert_ok(all(r.get("codigo") == "CUOTA_LLENA" for r in bloqueadas),
                  "las perdedoras vienen bloqueadas con CUOTA_LLENA")

        uso = query("SELECT bytes_usados FROM tenant_storage WHERE tenant_id = %s", (tid,))
        assert_ok(uso and uso[0]["bytes_usados"] == CHUNK,
                  f"el contador quedó exactamente en {CHUNK // MB} MB "
                  f"(está en {(uso[0]['bytes_usados'] // MB) if uso else '?'} MB)")

        # ── Test 2: borde exacto del límite ─────────────────────────────────
        print("\n[Test 2] Borde exacto del límite:")
        restante = LIMITE - CHUNK  # 424 MB
        r = reservar(tid, restante)
        assert_ok(r.get("ok"), f"reserva del espacio restante exacto ({restante // MB} MB) pasa")
        r = reservar(tid, 1)
        assert_ok(not r.get("ok"), "1 byte extra con el cupo a tope se BLOQUEA (422)")

        # ── Test 3: compensación por subida fallida ──────────────────────────
        print("\n[Test 3] Compensación (liberar_reserva):")
        liberar_reserva(tid, CHUNK)
        uso = query("SELECT bytes_usados FROM tenant_storage WHERE tenant_id = %s", (tid,))
        assert_ok(uso and uso[0]["bytes_usados"] == restante,
                  f"la reserva devuelta deja el contador en {restante // MB} MB")
        r = reservar(tid, CHUNK)
        assert_ok(r.get("ok"), "con el espacio devuelto, la reserva vuelve a pasar")

        # ── Test 4: crédito de reemplazo ────────────────────────────────────
        print("\n[Test 4] Crédito de reemplazo 1:1:")
        # Estado simulado: cupo a tope + una foto de MITAD activa en el
        # ledger (la que se va a reemplazar)
        confirmar(tid, url_falsa, MITAD, MITAD, "test")
        # Reemplazar la foto de MITAD por OTRA de MITAD al tope: el crédito
        # la neutraliza → pasa (el peso neto no crece)
        r = reservar(tid, MITAD, url_a_reemplazar=url_falsa)
        assert_ok(r.get("ok"), "reemplazo al tope pasa con el crédito (neto no crece)")
        ev = query(
            "SELECT liberado FROM storage_eventos WHERE tenant_id = %s AND url = %s "
            "AND evento = 'upload' ORDER BY id DESC LIMIT 1",
            (tid, url_falsa),
        )
        assert_ok(ev and ev[0]["liberado"], "la foto vieja quedó marcada como liberada en el ledger")
        r2 = liberar_url(url_falsa, tid)
        assert_ok(r2.get("ok") and r2.get("bytes_liberados", 0) == 0,
                  "liberar_url es idempotente (la foto ya estaba liberada)")
        # Sin crédito (la foto ya está liberada): la misma subida se BLOQUEA
        r3 = reservar(tid, MITAD, url_a_reemplazar=url_falsa)
        assert_ok(not r3.get("ok"), "la misma subida SIN crédito se bloquea (el crédito se consumió)")

        print("\n" + ("[OK] TEST COMPLETO: todas las aserciones pasaron"
                      if not fallos else f"[ERROR] {len(fallos)} aserciones FALLARON"))
        if fallos:
            sys.exit(2)
    finally:
        # ── Limpieza total (cascade: tenant_storage + storage_eventos) ───────
        execute("DELETE FROM tenants WHERE id = %s", (tid,))
        quedan = query(
            "SELECT (SELECT COUNT(*) FROM tenant_storage WHERE tenant_id = %s) AS s, "
            "       (SELECT COUNT(*) FROM storage_eventos WHERE tenant_id = %s) AS e",
            (tid, tid),
        )
        limpio = quedan and quedan[0]["s"] == 0 and quedan[0]["e"] == 0
        print(f"\nLimpieza: tenant de prueba eliminado ({'sin rastros ✓' if limpio else '⚠️ quedaron filas'})")


if __name__ == "__main__":
    main()
