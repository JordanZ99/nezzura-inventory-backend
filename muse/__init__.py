# ==============================================================================
# backend/muse/ — Servidor MCP (Model Context Protocol) para agentes IA.
#
# El paquete se llama "muse" (y NO "mcp") a propósito: un paquete local
# llamado mcp/ sombrearía el SDK oficial `mcp` de PyPI y rompería los
# imports (from mcp.server.fastmcp import FastMCP).
#
#   muse/auth.py   → middleware X-API-Key → tenant_id (ContextVar)
#   muse/server.py → FastMCP + los 6 tools de solo lectura
# ==============================================================================
