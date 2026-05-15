from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .bot import TelegramMediaBot
from .const import DATA_BOT, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    bot: TelegramMediaBot = hass.data[DOMAIN][entry.entry_id][DATA_BOT]

    async_add_entities(
        [
            TelegramBotStatusSensor(entry.entry_id, bot),
            TelegramBotLastMessageSensor(entry.entry_id, bot),
            TelegramBotLastUserSensor(entry.entry_id, bot),
            TelegramBotLastCallbackSensor(entry.entry_id, bot),
        ]
    )


class TelegramBotBaseSensor(SensorEntity):
    _attr_should_poll = False

    def __init__(
        self,
        entry_id: str,
        bot: TelegramMediaBot,
        key: str,
        name: str,
    ) -> None:
        self.bot = bot
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_name = name

    async def async_added_to_hass(self) -> None:
        @callback
        def handle_update(event):
            self.async_write_ha_state()

        self.async_on_remove(
            self.hass.bus.async_listen(
                "telegram_media_bot_updated",
                handle_update,
            )
        )


class TelegramBotStatusSensor(TelegramBotBaseSensor):
    def __init__(self, entry_id: str, bot: TelegramMediaBot) -> None:
        super().__init__(
            entry_id,
            bot,
            "status",
            "Telegram Media Bot Status",
        )

    @property
    def native_value(self):
        return self.bot.status


class TelegramBotLastMessageSensor(TelegramBotBaseSensor):
    def __init__(self, entry_id: str, bot: TelegramMediaBot) -> None:
        super().__init__(
            entry_id,
            bot,
            "last_message",
            "Telegram Media Bot Last Message",
        )

    @property
    def native_value(self):
        return self.bot.last_message


class TelegramBotLastUserSensor(TelegramBotBaseSensor):
    def __init__(self, entry_id: str, bot: TelegramMediaBot) -> None:
        super().__init__(
            entry_id,
            bot,
            "last_user",
            "Telegram Media Bot Last User",
        )

    @property
    def native_value(self):
        return self.bot.last_user


class TelegramBotLastCallbackSensor(TelegramBotBaseSensor):
    def __init__(self, entry_id: str, bot: TelegramMediaBot) -> None:
        super().__init__(
            entry_id,
            bot,
            "last_callback",
            "Telegram Media Bot Last Callback",
        )

    @property
    def native_value(self):
        return self.bot.last_callback