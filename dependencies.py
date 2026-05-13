from database.conexion import DEFAULT_TENANT_ID

def get_tenant_id() -> str:
    """
    Dependencia para obtener el tenant_id actual.
    Por ahora devuelve el ID por defecto. 
    En el futuro, aquí es donde verificaremos el token JWT de Supabase.
    """
    return DEFAULT_TENANT_ID
