from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall

from .bot import TelegramMediaBot
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
    DEFAULT_SEARCH_COMMAND,
    DATA_BOT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

def _get_entry_value(entry: ConfigEntry, key: str, default=None):
    """Return option value, falling back to original config data."""
    return entry.options.get(key, entry.data.get(key, default))

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    """Set up Natbot from a config entry."""

    hass.data.setdefault(DOMAIN, {})

    token = _get_entry_value(entry, CONF_BOT_TOKEN, "")
    tmdb_api_key = _get_entry_value(
        entry,
        CONF_TMDB_API_KEY,
        _get_entry_value(entry, CONF_TMDB_API_KEY, ""),
    )

    allowed_chat_ids_raw = _get_entry_value(entry, CONF_ALLOWED_CHAT_IDS, "")
    allowed_chat_ids = {
        int(chat_id.strip())
        for chat_id in allowed_chat_ids_raw.split(",")
        if chat_id.strip()
    }

    bot = TelegramMediaBot(
        hass=hass,
        token=_get_entry_value(entry, CONF_BOT_TOKEN, ""),
        allowed_chat_ids=allowed_chat_ids,
        tmdb_api_key=tmdb_api_key,
        search_mode=_get_entry_value(entry, CONF_SEARCH_MODE, SEARCH_MODE_SIMPLE),
        search_command=_get_entry_value(entry, CONF_SEARCH_COMMAND, DEFAULT_SEARCH_COMMAND),
        radarr_url=_get_entry_value(entry, CONF_RADARR_URL, ""),
        radarr_api_key=_get_entry_value(entry, CONF_RADARR_API_KEY, ""),
        radarr_root_folder=_get_entry_value(entry, CONF_RADARR_ROOT_FOLDER, ""),
        radarr_quality_profile_id=int(
            _get_entry_value(entry, CONF_RADARR_QUALITY_PROFILE_ID, 1)
        ),
        sonarr_url=_get_entry_value(entry, CONF_SONARR_URL, ""),
        sonarr_api_key=_get_entry_value(entry, CONF_SONARR_API_KEY, ""),
        sonarr_root_folder=_get_entry_value(entry, CONF_SONARR_ROOT_FOLDER, ""),
        sonarr_quality_profile_id=int(
            _get_entry_value(entry, CONF_SONARR_QUALITY_PROFILE_ID, 1)
        ),
        qbittorrent_url=_get_entry_value(
            entry,
            CONF_QBITTORRENT_URL,
            "",
        ),
        qbittorrent_username=_get_entry_value(
            entry,
            CONF_QBITTORRENT_USERNAME,
            "",
        ),
        qbittorrent_password=_get_entry_value(
            entry,
            CONF_QBITTORRENT_PASSWORD,
            "",
        ),
    )

    hass.data[DOMAIN][entry.entry_id] = {
        DATA_BOT: bot,
    }

    await bot.async_start()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def handle_send_message(call: ServiceCall) -> None:
        """Send a Telegram message."""

        current_bot: TelegramMediaBot = hass.data[DOMAIN][entry.entry_id][DATA_BOT]

        chat_id = int(call.data["chat_id"])
        message = call.data["message"]

        await current_bot.async_send_message(
            chat_id=chat_id,
            message=message,
        )

    async def handle_ask_movie(call: ServiceCall) -> None:
        """Ask the bot to search for a movie."""

        current_bot: TelegramMediaBot = hass.data[DOMAIN][entry.entry_id][DATA_BOT]

        chat_id = int(call.data["chat_id"])
        title = call.data["title"]

        await current_bot.async_ask_movie(
            chat_id=chat_id,
            title=title,
        )

    # Services are integration-wide, so register them once.
    if not hass.services.has_service(DOMAIN, "send_message"):
        hass.services.async_register(
            DOMAIN,
            "send_message",
            handle_send_message,
        )

    if not hass.services.has_service(DOMAIN, "ask_movie"):
        hass.services.async_register(
            DOMAIN,
            "ask_movie",
            handle_ask_movie,
        )

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    """Unload Natbot."""

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    data = hass.data[DOMAIN].pop(entry.entry_id, None)

    if data is not None:
        bot: TelegramMediaBot = data[DATA_BOT]
        await bot.async_stop()

    if unload_ok and not hass.data[DOMAIN]:
        hass.services.async_remove(DOMAIN, "send_message")
        hass.services.async_remove(DOMAIN, "ask_movie")
        hass.data.pop(DOMAIN)

    return unload_ok

async def _async_update_listener(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Reload integration when options change."""
    await hass.config_entries.async_reload(entry.entry_id)