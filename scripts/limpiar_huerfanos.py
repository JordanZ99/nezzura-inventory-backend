# ==============================================================================
# backend/scripts/limpiar_huerfanos.py
# Detección y limpieza de fotos HUÉRFANAS (migración 071, Fase C).
#
# Huérfana = subida activa en el ledger cuya URL ya NO se referencia en
# NINGUNA tabla de NINGÚN tenant (ni producto, galería, variación, logo,
# banner/hero, ni el JSONB de catalogo_publicado). Siguen costando dinero en
# Cloudinary aunque nadie las vea.
#
# Orígenes típicos: el tenant subió una foto y cerró el formulario sin
# guardarla; reemplazos donde el destroy de la vieja falló; requests que
# crashearon a medias.
#
# Red de seguridad: solo se destruyen huérfanas con más de N días (default 7)
# — una subida EN CURSO (subió, aún no guarda) no se toca.
#
# Uso (desde backend/):
#   python -m scripts.limpiar_huerfanos               # dry-run: solo reporta
#   python -m scripts.limpiar_huerfanos --apply       # destruye (+7 días default)
#   python -m scripts.limpiar_huerfanos --apply --dias 14
#
# La destrucción pasa por borrar_imagen_cloudinary → la cuota se libera sola
# en el ledger. Destruir un asset que aún se referencia es imposible por
# construcción: los huérfanos se detectan contra TODOS los tenants.
# ==============================================================================

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Permitir importar database.* corriendo desde backend/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.almacenamiento import detectar_huerfanos  # noqa: E402
from database.imagenes import borrar_imagen_cloudinary  # noqa: E402

MB = 1024 * 1024


def main() -> None:
    parser = argparse.ArgumentParser(description="Detección/limpieza de fotos huérfanas")
    parser.add_argument("--apply", action="store_true",
                        help="Destruye las huérfanas (por defecto es dry-run)")
    parser.add_argument("--dias", type=int, default=7,
                        help="Antigüedad mínima en días para destruir (default 7)")
    parser.add_argument("--tenant", help="Limita la operación a un tenant (uuid)")
    args = parser.parse_args()

    huerfanos = detectar_huerfanos(args.tenant)
    if not huerfanos:
        print("Sin huerfanos: todas las subidas activas del ledger estan referenciadas. [OK]")
        return

    ahora = datetime.now(timezone.utc)
    print(f"Huérfanos detectados: {len(huerfanos)} "
          f"({sum(int(h['bytes'] or 0) for h in huerfanos) / MB:.1f} MB en el medidor)\n")
    for h in huerfanos:
        creado = h.get("creado_en")
        dias = (ahora - creado).days if creado else -1
        viejo = dias >= args.dias
        print(f"  [{h['tenant_id'][:8]}…] {h['url'][:90]}")
        print(f"      {int(h['bytes'] or 0) / MB:.0f} MB · subida hace {dias} día(s) · "
              f"{'DESTRUIBLE' if viejo else 'aún muy reciente, se conserva'}")

    if not args.apply:
        print("\n(Dry-run: no se destruyó nada. Pasa --apply para limpiar.)")
        return

    from database.almacenamiento import liberar_url

    destruidos = 0
    liberados_bytes = 0
    for h in huerfanos:
        creado = h.get("creado_en")
        dias = (ahora - creado).days if creado else -1
        if dias < args.dias:
            continue  # red de seguridad: subidas recientes no se tocan
        url = h["url"]
        # destroy → liberación automática en el ledger ('ok'/'not_found');
        # si destroy falló, liberar_url fuerza la baja contable y la próxima
        # corrida reintenta el destroy físico.
        borrar_imagen_cloudinary(url)
        liberar_url(url, h["tenant_id"])
        destruidos += 1
        liberados_bytes += int(h["bytes"] or 0)

    print(f"\n[OK] {destruidos} huerfanos destruidos, "
          f"{liberados_bytes / MB:.1f} MB liberados del medidor y del proveedor.")


if __name__ == "__main__":
    main()
