"""Constantes da integração SolarEdge One (portal web)."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "solaredge_one"

CONF_SITE_ID: Final = "site_id"
CONF_USERNAME: Final = "username"
CONF_PASSWORD: Final = "password"
# Guardado pela integração após o login (entradas antigas o recebiam do usuário)
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_COOKIE: Final = "cookie"
CONF_USER_AGENT: Final = "user_agent"
CONF_POWER_INTERVAL: Final = "power_interval"
CONF_ARRAY_INTERVAL: Final = "array_interval"

# --- Endpoints do dashboard /one (mapeados a partir de um HAR em 2026-10-29) --
BASE_URL: Final = "https://monitoring.solaredge.com"

# Login OAuth2 (AWS Cognito, user pool eu-central-1_fVUTz39em, client público)
TOKEN_URL: Final = "https://login.solaredge.com/oauth2/token"
CLIENT_ID: Final = "ugfnsujd3384sshcjehaphlh3"
USER_POOL_ID: Final = "eu-central-1_fVUTz39em"
COGNITO_URL: Final = "https://cognito-idp.eu-central-1.amazonaws.com/"
TOKEN_REFRESH_MARGIN: Final = 300  # renova 5 min antes de expirar
DASHBOARD_URL: Final = BASE_URL + "/one#/residential/dashboard?siteId={site_id}"

LAYOUT_PATH: Final = "/services/layout/logical/generic/v2/site/{site_id}"
OPTIMIZER_LIVE_PATH: Final = "/services/layout/information/optimizers"
ENERGY_BY_INVERTER_PATH: Final = "/services/layout/energy/site/{site_id}/by-inverter"
POWER_FLOW_PATH: Final = "/services/dashboard/power-flow/v2/sites/{site_id}"
LIVE_POWER_PATH: Final = "/services/dashboard/live-power/sites/{site_id}"
COMMUNICATION_PATH: Final = "/services/dashboard/site-details/{site_id}/communication"
ALERTS_PATH: Final = "/services/dashboard/alerts/sites/{site_id}"
SITE_INFO_PATH: Final = "/services/layout/information/site/{site_id}"

# Catálogo público (sem login) com título, descrição e resolução de cada alerta
ALERT_CATALOG_URL: Final = (
    "https://monitoring-mfecdn-prod.solaredge.com/translation/"
    "AlertsMicroFrontEnd/{language}/alerts.json"
)
ALERT_CATALOG_FALLBACK: Final = "en_US"

# O cookie cf_clearance da Cloudflare fica atrelado ao User-Agent do navegador
DEFAULT_USER_AGENT: Final = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT: Final = 30

# --- Polling ----------------------------------------------------------------
# Os otimizadores reportam ao portal a cada ~15 min; consultar mais rápido
# que isso só gera carga e risco de bloqueio.
DEFAULT_POWER_INTERVAL: Final = 120  # segundos
DEFAULT_ARRAY_INTERVAL: Final = 900  # segundos
MIN_POWER_INTERVAL: Final = 60
MIN_ARRAY_INTERVAL: Final = 300
MAX_BACKOFF_INTERVAL: Final = timedelta(hours=1)

# Leitura de otimizador mais velha que isso é tratada como "sem geração"
STALE_AFTER: Final = timedelta(hours=1)
