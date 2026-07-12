from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from aiohttp import ClientError, ClientTimeout

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.config_entries import ConfigEntry, OptionsFlow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig

from .const import (
    CONF_ALLOWED_CHAT_IDS,
    CONF_BOT_TOKEN,
    CONF_TMDB_API_KEY,
    CONF_RADARR_API_KEY,
    CONF_RADARR_QUALITY_PROFILE_ID,
    CONF_RADARR_ROOT_FOLDER,
    CONF_RADARR_URL,
    CONF_SONARR_API_KEY,
    CONF_SONARR_QUALITY_PROFILE_ID,
    CONF_SONARR_ROOT_FOLDER,
    CONF_SONARR_URL,
    CONF_SEARCH_MODE,
    CONF_SEARCH_COMMAND,
    CONF_QBITTORRENT_URL,
    CONF_QBITTORRENT_USERNAME,
    CONF_QBITTORRENT_PASSWORD,
    SEARCH_MODE_SIMPLE,
    SEARCH_MODE_COMMAND,
    DEFAULT_SEARCH_COMMAND,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


async def _arr_get(
    hass,
    base_url: str,
    api_key: str,
    path: str,
) -> Any:
    """Fetch data from Radarr/Sonarr during config flow."""

    session = async_get_clientsession(hass)
    url = f"{base_url.rstrip('/')}{path}"

    async with session.get(
        url,
        headers={
            "X-Api-Key": api_key,
            "Accept": "application/json",
        },
        timeout=ClientTimeout(total=10),
    ) as response:
        response.raise_for_status()
        return await response.json()


async def _fetch_quality_profiles(
    hass,
    base_url: str,
    api_key: str,
) -> list[dict[str, Any]]:
    """Fetch quality profiles from Radarr/Sonarr."""

    data = await _arr_get(
        hass,
        base_url,
        api_key,
        "/api/v3/qualityprofile",
    )

    if not isinstance(data, list):
        return []

    return data


async def _fetch_root_folders(
    hass,
    base_url: str,
    api_key: str,
) -> list[dict[str, Any]]:
    """Fetch root folders from Radarr/Sonarr."""

    data = await _arr_get(
        hass,
        base_url,
        api_key,
        "/api/v3/rootfolder",
    )

    if not isinstance(data, list):
        return []

    return data


def _profile_options(profiles: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Build selector options for quality profiles."""

    options: list[dict[str, str]] = []

    for profile in profiles:
        profile_id = profile.get("id")
        name = profile.get("name")

        if profile_id is None or not name:
            continue

        options.append(
            {
                "value": str(profile_id),
                "label": f"{name} ({profile_id})",
            }
        )

    return options


def _root_folder_options(root_folders: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Build selector options for root folders."""

    options: list[dict[str, str]] = []

    for folder in root_folders:
        path = folder.get("path")

        if not path:
            continue

        free_space = folder.get("freeSpace")
        label = path

        if isinstance(free_space, int):
            free_gb = round(free_space / 1024 / 1024 / 1024, 1)
            label = f"{path} ({free_gb} GB free)"

        options.append(
            {
                "value": path,
                "label": label,
            }
        )

    return options


class TelegramMediaBotConfigFlow(
    config_entries.ConfigFlow,
    domain=DOMAIN,
):
    """Handle a config flow for Natbot."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

        self._radarr_profiles: list[dict[str, Any]] = []
        self._radarr_root_folders: list[dict[str, Any]] = []

        self._sonarr_profiles: list[dict[str, Any]] = []
        self._sonarr_root_folders: list[dict[str, Any]] = []

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> OptionsFlow:
        """Create the options flow."""
        return TelegramMediaBotOptionsFlow(config_entry)
        
    async def async_step_user(self, user_input=None):
        """Step 1: Telegram and TMDb settings."""

        errors = {}

        if user_input is not None:
            await self.async_set_unique_id("telegram_media_bot")
            self._abort_if_unique_id_configured()

            search_command = user_input.get(CONF_SEARCH_COMMAND, DEFAULT_SEARCH_COMMAND)
            search_command = str(search_command).strip().lstrip("/")

            if not search_command:
                search_command = DEFAULT_SEARCH_COMMAND

            user_input[CONF_SEARCH_COMMAND] = search_command

            self._data.update(user_input)
            return await self.async_step_radarr_connection()

        schema = vol.Schema(
            {
                vol.Required(CONF_BOT_TOKEN): str,
                vol.Required(CONF_TMDB_API_KEY): str,
                vol.Optional(CONF_ALLOWED_CHAT_IDS, default=""): str,

                vol.Required(CONF_SEARCH_MODE, default=SEARCH_MODE_SIMPLE): vol.In(
                    {
                        SEARCH_MODE_SIMPLE: "Simple mode - any text searches",
                        SEARCH_MODE_COMMAND: "Slash-command mode - /find titanic",
                    }
                ),
                vol.Optional(CONF_SEARCH_COMMAND, default=DEFAULT_SEARCH_COMMAND): str,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_radarr_connection(self, user_input=None):
        """Step 2: Radarr connection settings."""

        errors = {}

        if user_input is not None:
            radarr_url = user_input.get(CONF_RADARR_URL, "").strip()
            radarr_api_key = user_input.get(CONF_RADARR_API_KEY, "").strip()

            self._data[CONF_RADARR_URL] = radarr_url
            self._data[CONF_RADARR_API_KEY] = radarr_api_key

            # Allow skipping Radarr.
            if not radarr_url or not radarr_api_key:
                self._data[CONF_RADARR_ROOT_FOLDER] = ""
                self._data[CONF_RADARR_QUALITY_PROFILE_ID] = 1
                return await self.async_step_sonarr_connection()

            try:
                self._radarr_profiles = await _fetch_quality_profiles(
                    self.hass,
                    radarr_url,
                    radarr_api_key,
                )
                self._radarr_root_folders = await _fetch_root_folders(
                    self.hass,
                    radarr_url,
                    radarr_api_key,
                )

            except (ClientError, TimeoutError) as err:
                _LOGGER.warning("Could not connect to Radarr: %s", err)
                errors["base"] = "cannot_connect"

            except Exception as err:
                _LOGGER.exception("Unexpected Radarr config flow error: %s", err)
                errors["base"] = "unknown"

            else:
                if not self._radarr_profiles:
                    errors["base"] = "no_quality_profiles"
                elif not self._radarr_root_folders:
                    errors["base"] = "no_root_folders"
                else:
                    return await self.async_step_radarr_options()

        schema = vol.Schema(
            {
                vol.Optional(CONF_RADARR_URL, default=""): str,
                vol.Optional(CONF_RADARR_API_KEY, default=""): str,
            }
        )

        return self.async_show_form(
            step_id="radarr_connection",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "hint": "Leave Radarr URL/API key empty to skip movie support.",
            },
        )

    async def async_step_radarr_options(self, user_input=None):
        """Step 3: Radarr dropdowns."""

        errors = {}

        profile_options = _profile_options(self._radarr_profiles)
        root_folder_options = _root_folder_options(self._radarr_root_folders)

        if user_input is not None:
            self._data[CONF_RADARR_QUALITY_PROFILE_ID] = int(
                user_input[CONF_RADARR_QUALITY_PROFILE_ID]
            )
            self._data[CONF_RADARR_ROOT_FOLDER] = user_input[CONF_RADARR_ROOT_FOLDER]

            return await self.async_step_sonarr_connection()

        schema = vol.Schema(
            {
                vol.Required(CONF_RADARR_QUALITY_PROFILE_ID): SelectSelector(
                    SelectSelectorConfig(
                        options=profile_options,
                        mode="dropdown",
                    )
                ),
                vol.Required(CONF_RADARR_ROOT_FOLDER): SelectSelector(
                    SelectSelectorConfig(
                        options=root_folder_options,
                        mode="dropdown",
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="radarr_options",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_sonarr_connection(self, user_input=None):
        """Step 4: Sonarr connection settings."""

        errors = {}

        if user_input is not None:
            sonarr_url = user_input.get(CONF_SONARR_URL, "").strip()
            sonarr_api_key = user_input.get(CONF_SONARR_API_KEY, "").strip()

            self._data[CONF_SONARR_URL] = sonarr_url
            self._data[CONF_SONARR_API_KEY] = sonarr_api_key

            # Allow skipping Sonarr.
            if not sonarr_url or not sonarr_api_key:
                self._data[CONF_SONARR_ROOT_FOLDER] = ""
                self._data[CONF_SONARR_QUALITY_PROFILE_ID] = 1
                return await self.async_step_qbittorrent()

            try:
                self._sonarr_profiles = await _fetch_quality_profiles(
                    self.hass,
                    sonarr_url,
                    sonarr_api_key,
                )
                self._sonarr_root_folders = await _fetch_root_folders(
                    self.hass,
                    sonarr_url,
                    sonarr_api_key,
                )

            except (ClientError, TimeoutError) as err:
                _LOGGER.warning("Could not connect to Sonarr: %s", err)
                errors["base"] = "cannot_connect"

            except Exception as err:
                _LOGGER.exception("Unexpected Sonarr config flow error: %s", err)
                errors["base"] = "unknown"

            else:
                if not self._sonarr_profiles:
                    errors["base"] = "no_quality_profiles"
                elif not self._sonarr_root_folders:
                    errors["base"] = "no_root_folders"
                else:
                    return await self.async_step_sonarr_options()

        schema = vol.Schema(
            {
                vol.Optional(CONF_SONARR_URL, default=""): str,
                vol.Optional(CONF_SONARR_API_KEY, default=""): str,
            }
        )

        return self.async_show_form(
            step_id="sonarr_connection",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "hint": "Leave Sonarr URL/API key empty to skip TV support.",
            },
        )

    async def async_step_sonarr_options(self, user_input=None):
        """Step 5: Sonarr dropdowns."""

        errors = {}

        profile_options = _profile_options(self._sonarr_profiles)
        root_folder_options = _root_folder_options(self._sonarr_root_folders)

        if user_input is not None:
            self._data[CONF_SONARR_QUALITY_PROFILE_ID] = int(
                user_input[CONF_SONARR_QUALITY_PROFILE_ID]
            )
            self._data[CONF_SONARR_ROOT_FOLDER] = user_input[CONF_SONARR_ROOT_FOLDER]

            return await self.async_step_qbittorrent()

        schema = vol.Schema(
            {
                vol.Required(CONF_SONARR_QUALITY_PROFILE_ID): SelectSelector(
                    SelectSelectorConfig(
                        options=profile_options,
                        mode="dropdown",
                    )
                ),
                vol.Required(CONF_SONARR_ROOT_FOLDER): SelectSelector(
                    SelectSelectorConfig(
                        options=root_folder_options,
                        mode="dropdown",
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="sonarr_options",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_qbittorrent(self, user_input=None):
        """Step 6: qBittorrent settings."""

        errors = {}

        if user_input is not None:
            qbittorrent_url = user_input.get(
                CONF_QBITTORRENT_URL,
                "",
            ).strip()
            qbittorrent_username = user_input.get(
                CONF_QBITTORRENT_USERNAME,
                "",
            ).strip()
            qbittorrent_password = user_input.get(
                CONF_QBITTORRENT_PASSWORD,
                "",
            )

            self._data[CONF_QBITTORRENT_URL] = qbittorrent_url
            self._data[CONF_QBITTORRENT_USERNAME] = qbittorrent_username
            self._data[CONF_QBITTORRENT_PASSWORD] = qbittorrent_password

            return self.async_create_entry(
                title="Natbot",
                data=self._data,
            )

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_QBITTORRENT_URL,
                    default="",
                ): str,
                vol.Optional(
                    CONF_QBITTORRENT_USERNAME,
                    default="",
                ): str,
                vol.Optional(
                    CONF_QBITTORRENT_PASSWORD,
                    default="",
                ): str,
            }
        )

        return self.async_show_form(
            step_id="qbittorrent",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "hint": (
                    "Leave all fields empty to disable torrent-file support. "
                    "Create the Movies, TV Shows, and Games categories "
                    "in qBittorrent first."
                ),
            },
        )

class TelegramMediaBotOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for Natbot."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry
        self._data: dict[str, Any] = dict(config_entry.options)

        # Backfill from original setup data for existing installs.
        for key, value in config_entry.data.items():
            self._data.setdefault(key, value)

        self._radarr_profiles: list[dict[str, Any]] = []
        self._radarr_root_folders: list[dict[str, Any]] = []

        self._sonarr_profiles: list[dict[str, Any]] = []
        self._sonarr_root_folders: list[dict[str, Any]] = []

    async def async_step_init(self, user_input=None):
        """Step 1: Telegram, TMDb, and search mode."""

        errors = {}

        if user_input is not None:
            search_command = user_input.get(
                CONF_SEARCH_COMMAND,
                DEFAULT_SEARCH_COMMAND,
            )
            search_command = str(search_command).strip().lstrip("/")

            if not search_command:
                search_command = DEFAULT_SEARCH_COMMAND

            user_input[CONF_SEARCH_COMMAND] = search_command

            self._data.update(user_input)

            return await self.async_step_radarr_connection()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_BOT_TOKEN,
                    default=self._data.get(CONF_BOT_TOKEN, ""),
                ): str,
                vol.Required(
                    CONF_TMDB_API_KEY,
                    default=self._data.get(CONF_TMDB_API_KEY, ""),
                ): str,
                vol.Optional(
                    CONF_ALLOWED_CHAT_IDS,
                    default=self._data.get(CONF_ALLOWED_CHAT_IDS, ""),
                ): str,
                vol.Required(
                    CONF_SEARCH_MODE,
                    default=self._data.get(CONF_SEARCH_MODE, SEARCH_MODE_SIMPLE),
                ): vol.In(
                    {
                        SEARCH_MODE_SIMPLE: "Simple mode - any text searches",
                        SEARCH_MODE_COMMAND: "Slash-command mode - /find titanic",
                    }
                ),
                vol.Optional(
                    CONF_SEARCH_COMMAND,
                    default=self._data.get(
                        CONF_SEARCH_COMMAND,
                        DEFAULT_SEARCH_COMMAND,
                    ),
                ): str,
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_radarr_connection(self, user_input=None):
        """Step 2: Radarr connection settings."""

        errors = {}

        if user_input is not None:
            radarr_url = user_input.get(CONF_RADARR_URL, "").strip()
            radarr_api_key = user_input.get(CONF_RADARR_API_KEY, "").strip()

            self._data[CONF_RADARR_URL] = radarr_url
            self._data[CONF_RADARR_API_KEY] = radarr_api_key

            if not radarr_url or not radarr_api_key:
                self._data[CONF_RADARR_ROOT_FOLDER] = ""
                self._data[CONF_RADARR_QUALITY_PROFILE_ID] = 1
                return await self.async_step_sonarr_connection()

            try:
                self._radarr_profiles = await _fetch_quality_profiles(
                    self.hass,
                    radarr_url,
                    radarr_api_key,
                )
                self._radarr_root_folders = await _fetch_root_folders(
                    self.hass,
                    radarr_url,
                    radarr_api_key,
                )

            except (ClientError, TimeoutError) as err:
                _LOGGER.warning("Could not connect to Radarr: %s", err)
                errors["base"] = "cannot_connect"

            except Exception as err:
                _LOGGER.exception("Unexpected Radarr options flow error: %s", err)
                errors["base"] = "unknown"

            else:
                if not self._radarr_profiles:
                    errors["base"] = "no_quality_profiles"
                elif not self._radarr_root_folders:
                    errors["base"] = "no_root_folders"
                else:
                    return await self.async_step_radarr_options()

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_RADARR_URL,
                    default=self._data.get(CONF_RADARR_URL, ""),
                ): str,
                vol.Optional(
                    CONF_RADARR_API_KEY,
                    default=self._data.get(CONF_RADARR_API_KEY, ""),
                ): str,
            }
        )

        return self.async_show_form(
            step_id="radarr_connection",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_radarr_options(self, user_input=None):
        """Step 3: Radarr dropdowns."""

        errors = {}

        profile_options = _profile_options(self._radarr_profiles)
        root_folder_options = _root_folder_options(self._radarr_root_folders)

        if user_input is not None:
            self._data[CONF_RADARR_QUALITY_PROFILE_ID] = int(
                user_input[CONF_RADARR_QUALITY_PROFILE_ID]
            )
            self._data[CONF_RADARR_ROOT_FOLDER] = user_input[CONF_RADARR_ROOT_FOLDER]

            return await self.async_step_sonarr_connection()

        current_profile = str(
            self._data.get(CONF_RADARR_QUALITY_PROFILE_ID, "1")
        )
        current_root = self._data.get(CONF_RADARR_ROOT_FOLDER, "")

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_RADARR_QUALITY_PROFILE_ID,
                    default=current_profile,
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=profile_options,
                        mode="dropdown",
                    )
                ),
                vol.Required(
                    CONF_RADARR_ROOT_FOLDER,
                    default=current_root,
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=root_folder_options,
                        mode="dropdown",
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="radarr_options",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_sonarr_connection(self, user_input=None):
        """Step 4: Sonarr connection settings."""

        errors = {}

        if user_input is not None:
            sonarr_url = user_input.get(CONF_SONARR_URL, "").strip()
            sonarr_api_key = user_input.get(CONF_SONARR_API_KEY, "").strip()

            self._data[CONF_SONARR_URL] = sonarr_url
            self._data[CONF_SONARR_API_KEY] = sonarr_api_key

            if not sonarr_url or not sonarr_api_key:
                self._data[CONF_SONARR_ROOT_FOLDER] = ""
                self._data[CONF_SONARR_QUALITY_PROFILE_ID] = 1
                return await self.async_step_qbittorrent()

            try:
                self._sonarr_profiles = await _fetch_quality_profiles(
                    self.hass,
                    sonarr_url,
                    sonarr_api_key,
                )
                self._sonarr_root_folders = await _fetch_root_folders(
                    self.hass,
                    sonarr_url,
                    sonarr_api_key,
                )

            except (ClientError, TimeoutError) as err:
                _LOGGER.warning("Could not connect to Sonarr: %s", err)
                errors["base"] = "cannot_connect"

            except Exception as err:
                _LOGGER.exception("Unexpected Sonarr options flow error: %s", err)
                errors["base"] = "unknown"

            else:
                if not self._sonarr_profiles:
                    errors["base"] = "no_quality_profiles"
                elif not self._sonarr_root_folders:
                    errors["base"] = "no_root_folders"
                else:
                    return await self.async_step_sonarr_options()

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_SONARR_URL,
                    default=self._data.get(CONF_SONARR_URL, ""),
                ): str,
                vol.Optional(
                    CONF_SONARR_API_KEY,
                    default=self._data.get(CONF_SONARR_API_KEY, ""),
                ): str,
            }
        )

        return self.async_show_form(
            step_id="sonarr_connection",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_sonarr_options(self, user_input=None):
        """Step 5: Sonarr dropdowns."""

        errors = {}

        profile_options = _profile_options(self._sonarr_profiles)
        root_folder_options = _root_folder_options(self._sonarr_root_folders)

        if user_input is not None:
            self._data[CONF_SONARR_QUALITY_PROFILE_ID] = int(
                user_input[CONF_SONARR_QUALITY_PROFILE_ID]
            )
            self._data[CONF_SONARR_ROOT_FOLDER] = user_input[CONF_SONARR_ROOT_FOLDER]

            return await self.async_step_qbittorrent()

        current_profile = str(
            self._data.get(CONF_SONARR_QUALITY_PROFILE_ID, "1")
        )
        current_root = self._data.get(CONF_SONARR_ROOT_FOLDER, "")

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SONARR_QUALITY_PROFILE_ID,
                    default=current_profile,
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=profile_options,
                        mode="dropdown",
                    )
                ),
                vol.Required(
                    CONF_SONARR_ROOT_FOLDER,
                    default=current_root,
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=root_folder_options,
                        mode="dropdown",
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="sonarr_options",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_qbittorrent(self, user_input=None):
        """Step 6: qBittorrent settings."""

        errors = {}

        if user_input is not None:
            qbittorrent_url = user_input.get(
                CONF_QBITTORRENT_URL,
                "",
            ).strip()
            qbittorrent_username = user_input.get(
                CONF_QBITTORRENT_USERNAME,
                "",
            ).strip()
            qbittorrent_password = user_input.get(
                CONF_QBITTORRENT_PASSWORD,
                "",
            )

            self._data[CONF_QBITTORRENT_URL] = qbittorrent_url
            self._data[CONF_QBITTORRENT_USERNAME] = qbittorrent_username
            self._data[CONF_QBITTORRENT_PASSWORD] = qbittorrent_password

            return self.async_create_entry(
                title="",
                data=self._data,
            )

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_QBITTORRENT_URL,
                    default=self._data.get(
                        CONF_QBITTORRENT_URL,
                        "",
                    ),
                ): str,
                vol.Optional(
                    CONF_QBITTORRENT_USERNAME,
                    default=self._data.get(
                        CONF_QBITTORRENT_USERNAME,
                        "",
                    ),
                ): str,
                vol.Optional(
                    CONF_QBITTORRENT_PASSWORD,
                default=self._data.get(
                    CONF_QBITTORRENT_PASSWORD,
                    "",
                ),
            ): str,
        }
    )

    return self.async_show_form(
        step_id="qbittorrent",
        data_schema=schema,
        errors=errors,
        description_placeholders={
            "hint": (
                "Leave all fields empty to disable torrent-file support. "
                "Create the Movies, TV Shows, and Games categories "
                "in qBittorrent first."
            ),
        },
    )