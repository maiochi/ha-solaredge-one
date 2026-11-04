"""Config flow da integração SolarEdge One."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    SolarEdgeAuthError,
    SolarEdgeError,
    SolarEdgeMfaRequiredError,
    SolarEdgeRateLimitError,
    SolarEdgeWebClient,
)
from .const import (
    CONF_ARRAY_INTERVAL,
    CONF_COOKIE,
    CONF_PASSWORD,
    CONF_POWER_INTERVAL,
    CONF_REFRESH_TOKEN,
    CONF_SITE_ID,
    CONF_USER_AGENT,
    CONF_USERNAME,
    DEFAULT_ARRAY_INTERVAL,
    DEFAULT_POWER_INTERVAL,
    DEFAULT_USER_AGENT,
    DOMAIN,
    MIN_ARRAY_INTERVAL,
    MIN_POWER_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

MULTILINE_SELECTOR = TextSelector(TextSelectorConfig(multiline=True))
EMAIL_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
)
PASSWORD_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)


def _auth_schema(username: str | None = None) -> dict:
    return {
        vol.Required(CONF_USERNAME, default=username or vol.UNDEFINED): EMAIL_SELECTOR,
        vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
    }


def _advanced_schema(user_agent: str) -> dict:
    return {
        vol.Optional(CONF_COOKIE): MULTILINE_SELECTOR,
        vol.Optional(CONF_USER_AGENT, default=user_agent): str,
    }


class SolarEdgeOneConfigFlow(ConfigFlow, domain=DOMAIN):
    """Fluxo de configuração via UI."""

    VERSION = 1

    async def _async_validate(
        self, data: dict[str, Any]
    ) -> tuple[str | None, str | None, str | None]:
        """Faz login e lê o layout; retorna (erro, nome do site, refresh token)."""
        session = async_create_clientsession(self.hass, auto_cleanup=False)
        try:
            client = SolarEdgeWebClient(
                session,
                data[CONF_SITE_ID],
                username=data[CONF_USERNAME],
                password=data[CONF_PASSWORD],
                cookie=data.get(CONF_COOKIE),
                user_agent=data.get(CONF_USER_AGENT),
            )
            layout = await client.async_get_layout()
        except SolarEdgeMfaRequiredError:
            return "mfa_not_supported", None, None
        except SolarEdgeAuthError as err:
            _LOGGER.debug("Login recusado: %s", err)
            return "invalid_auth", None, None
        except SolarEdgeRateLimitError:
            return "rate_limited", None, None
        except SolarEdgeError:
            _LOGGER.exception("Falha ao validar acesso ao SolarEdge")
            return "cannot_connect", None, None
        finally:
            await session.close()
        if not layout.optimizers and not layout.inverters:
            return "no_devices", None, None
        return None, layout.site_name, client.refresh_token

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            site_id = str(user_input[CONF_SITE_ID]).strip()
            await self.async_set_unique_id(site_id)
            self._abort_if_unique_id_configured()
            data = {
                **user_input,
                CONF_SITE_ID: site_id,
                CONF_USERNAME: user_input[CONF_USERNAME].strip(),
            }
            error, site_name, refresh_token = await self._async_validate(data)
            if error is None:
                if refresh_token:
                    data[CONF_REFRESH_TOKEN] = refresh_token
                return self.async_create_entry(
                    title=site_name or f"SolarEdge {site_id}", data=data
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_SITE_ID): str,
                        **_auth_schema(),
                        **_advanced_schema(DEFAULT_USER_AGENT),
                    }
                ),
                {k: v for k, v in (user_input or {}).items() if k != CONF_PASSWORD},
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            updates = {**user_input, CONF_USERNAME: user_input[CONF_USERNAME].strip()}
            error, _, refresh_token = await self._async_validate({**entry.data, **updates})
            if error is None:
                # Entradas antigas tinham só o refresh token copiado do navegador
                data = {**entry.data, **updates}
                data.pop(CONF_REFRESH_TOKEN, None)
                if refresh_token:
                    data[CONF_REFRESH_TOKEN] = refresh_token
                return self.async_update_reload_and_abort(entry, data=data)
            errors["base"] = error

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    **_auth_schema(entry.data.get(CONF_USERNAME)),
                    **_advanced_schema(
                        entry.data.get(CONF_USER_AGENT, DEFAULT_USER_AGENT)
                    ),
                }
            ),
            errors=errors,
            description_placeholders={"site_id": entry.data[CONF_SITE_ID]},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return SolarEdgeOneOptionsFlow()


class SolarEdgeOneOptionsFlow(OptionsFlow):
    """Ajuste dos intervalos de atualização."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={key: int(value) for key, value in user_input.items()}
            )

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_POWER_INTERVAL,
                        default=options.get(CONF_POWER_INTERVAL, DEFAULT_POWER_INTERVAL),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_POWER_INTERVAL,
                            max=3600,
                            step=30,
                            unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(
                        CONF_ARRAY_INTERVAL,
                        default=options.get(CONF_ARRAY_INTERVAL, DEFAULT_ARRAY_INTERVAL),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_ARRAY_INTERVAL,
                            max=7200,
                            step=60,
                            unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                }
            ),
        )
