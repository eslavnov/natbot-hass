from __future__ import annotations

import logging

from aiohttp import ClientError, ClientTimeout, FormData
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)


class QBittorrentError(Exception):
    """Raised when a qBittorrent operation fails."""


class QBittorrentClient:
    """Minimal asynchronous qBittorrent Web API client."""

    def __init__(
        self,
        *,
        hass: HomeAssistant,
        base_url: str,
        username: str,
        password: str,
    ) -> None:
        self.hass = hass
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password

    @property
    def configured(self) -> bool:
        return bool(
            self.base_url
            and self.username
            and self.password
        )

    async def async_add_torrent(
        self,
        *,
        content: bytes,
        filename: str,
        category: str,
    ) -> None:
        """Authenticate and upload a torrent file."""

        if not self.configured:
            raise QBittorrentError("qBittorrent is not configured.")

        session = async_get_clientsession(self.hass)

        try:
            async with session.post(
                f"{self.base_url}/api/v2/auth/login",
                data={
                    "username": self.username,
                    "password": self.password,
                },
                headers={
                    "Referer": self.base_url,
                },
                timeout=ClientTimeout(total=15),
            ) as response:
                response_text = await response.text()

                if response.status != 200 or response_text.strip() != "Ok.":
                    raise QBittorrentError(
                        f"qBittorrent login failed: HTTP {response.status}"
                    )

                sid = response.cookies.get("SID")

                if sid is None:
                    raise QBittorrentError(
                        "qBittorrent did not return a session cookie."
                    )

                sid_value = sid.value

            form = FormData()
            form.add_field(
                "torrents",
                content,
                filename=filename,
                content_type="application/x-bittorrent",
            )
            form.add_field("category", category)

            async with session.post(
                f"{self.base_url}/api/v2/torrents/add",
                data=form,
                headers={
                    "Cookie": f"SID={sid_value}",
                    "Referer": self.base_url,
                },
                timeout=ClientTimeout(total=30),
            ) as response:
                response_text = await response.text()

                if response.status != 200:
                    raise QBittorrentError(
                        "Could not add torrent: "
                        f"HTTP {response.status}: {response_text}"
                    )

                if response_text.strip() not in {"", "Ok."}:
                    raise QBittorrentError(
                        f"qBittorrent rejected torrent: {response_text}"
                    )

        except QBittorrentError:
            raise

        except TimeoutError as err:
            raise QBittorrentError(
                "qBittorrent request timed out."
            ) from err

        except ClientError as err:
            raise QBittorrentError(
                f"Could not connect to qBittorrent: {err}"
            ) from err