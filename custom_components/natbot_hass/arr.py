from __future__ import annotations

import logging
from typing import Any

from aiohttp import ClientError, ClientTimeout

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)


class ArrError(Exception):
    """Base error for Radarr/Sonarr calls."""


class ArrNotConfiguredError(ArrError):
    """Raised when Arr service is not configured."""


class ArrClient:
    """Small async client for Radarr/Sonarr."""

    def __init__(
        self,
        hass: HomeAssistant,
        base_url: str,
        api_key: str,
    ) -> None:
        self.hass = hass
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Make an authenticated Arr API request."""

        if not self.configured:
            raise ArrNotConfiguredError("Arr client is not configured")

        session = async_get_clientsession(self.hass)

        headers = {
            "X-Api-Key": self.api_key,
            "Accept": "application/json",
        }

        url = f"{self.base_url}{path}"

        try:
            async with session.request(
                method,
                url,
                params=params,
                json=json,
                headers=headers,
                timeout=ClientTimeout(total=20),
            ) as response:
                text = await response.text()

                if response.status >= 400:
                    raise ArrError(
                        f"{method} {path} failed with HTTP {response.status}: {text}"
                    )

                if not text:
                    return None

                return await response.json()

        except ClientError as err:
            raise ArrError(f"{method} {path} failed: {err}") from err

        except TimeoutError as err:
            raise ArrError(f"{method} {path} timed out") from err


class RadarrClient(ArrClient):
    """Radarr API client."""

    async def lookup_movie(
        self,
        *,
        imdb_id: str,
        title: str,
        year: str | int | None = None,
    ) -> dict[str, Any] | None:
        """Find a movie in Radarr lookup."""

        terms: list[str] = []

        if imdb_id:
            terms.append(f"imdb:{imdb_id}")

        if title:
            if year:
                terms.append(f"{title} {year}")
            terms.append(title)

        for term in terms:
            results = await self._request(
                "GET",
                "/api/v3/movie/lookup",
                params={"term": term},
            )

            if not isinstance(results, list) or not results:
                continue

            if imdb_id:
                for item in results:
                    if not isinstance(item, dict):
                        continue

                    if item.get("imdbId") == imdb_id:
                        return item

            first_result = results[0]
            if isinstance(first_result, dict):
                return first_result

        return None

    async def add_movie(
        self,
        movie: dict[str, Any],
        *,
        root_folder_path: str,
        quality_profile_id: int,
        search_for_movie: bool = True,
    ) -> dict[str, Any]:
        """Add movie to Radarr."""

        payload = dict(movie)
        payload["rootFolderPath"] = root_folder_path
        payload["qualityProfileId"] = quality_profile_id
        payload["monitored"] = True
        payload["minimumAvailability"] = payload.get("minimumAvailability") or "released"
        payload["addOptions"] = {
            "searchForMovie": search_for_movie,
        }

        added = await self._request(
            "POST",
            "/api/v3/movie",
            json=payload,
        )

        if not isinstance(added, dict):
            raise ArrError("Radarr returned an invalid response while adding movie")

        return added

    async def lookup_and_add_movie(
        self,
        *,
        imdb_id: str,
        title: str,
        year: str | int | None,
        root_folder_path: str,
        quality_profile_id: int,
    ) -> dict[str, Any]:
        """Lookup and add a movie to Radarr."""

        movie = await self.lookup_movie(
            imdb_id=imdb_id,
            title=title,
            year=year,
        )

        if movie is None:
            raise ArrError(f"Radarr could not find movie: {title} ({year})")

        return await self.add_movie(
            movie,
            root_folder_path=root_folder_path,
            quality_profile_id=quality_profile_id,
            search_for_movie=True,
        )


class TvdbLookupClient:
    """Lookup TVDB IDs using TVmaze as an IMDb → TVDB bridge."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get_tvdb_id_from_imdb_id(self, imdb_id: str) -> int | None:
        """Return TVDB ID for an IMDb ID, if TVmaze knows it."""

        if not imdb_id:
            return None

        session = async_get_clientsession(self.hass)

        try:
            async with session.get(
                "https://api.tvmaze.com/lookup/shows",
                params={"imdb": imdb_id},
                timeout=ClientTimeout(total=10),
            ) as response:
                if response.status == 404:
                    return None

                response.raise_for_status()
                data = await response.json()

        except ClientError as err:
            _LOGGER.warning("TVmaze IMDb lookup failed for %s: %s", imdb_id, err)
            return None

        except TimeoutError:
            _LOGGER.warning("TVmaze IMDb lookup timed out for %s", imdb_id)
            return None

        if not isinstance(data, dict):
            return None

        externals = data.get("externals", {})

        if not isinstance(externals, dict):
            return None

        tvdb_id = externals.get("thetvdb")

        if tvdb_id in (None, "", 0):
            return None

        try:
            return int(tvdb_id)
        except (TypeError, ValueError):
            return None


class SonarrClient(ArrClient):
    """Sonarr API client."""

    async def lookup_series(
        self,
        *,
        title: str,
        year: str | int | None = None,
        tvdb_id: int | None = None,
    ) -> dict[str, Any] | None:
        """Find a series in Sonarr lookup."""

        terms: list[str] = []

        if tvdb_id:
            terms.append(f"tvdbid:{tvdb_id}")

        if title:
            if year:
                terms.append(f"{title} {year}")
            terms.append(title)

        for term in terms:
            results = await self._request(
                "GET",
                "/api/v3/series/lookup",
                params={"term": term},
            )

            if not isinstance(results, list) or not results:
                continue

            if tvdb_id:
                for item in results:
                    if not isinstance(item, dict):
                        continue

                    try:
                        item_tvdb_id = int(item.get("tvdbId", 0))
                    except (TypeError, ValueError):
                        item_tvdb_id = 0

                    if item_tvdb_id == tvdb_id:
                        return item

            normalized_title = title.casefold().strip()

            for item in results:
                if not isinstance(item, dict):
                    continue

                item_title = str(item.get("title", "")).casefold().strip()
                item_year = item.get("year")

                if item_title == normalized_title:
                    if year is None or str(item_year) == str(year):
                        return item

            first_result = results[0]
            if isinstance(first_result, dict):
                return first_result

        return None

    async def add_series(
        self,
        series: dict[str, Any],
        *,
        root_folder_path: str,
        quality_profile_id: int,
        season_mode: str = "all",
        search_for_missing_episodes: bool = True,
    ) -> dict[str, Any]:
        """Add a new series to Sonarr."""

        payload = dict(series)
        payload["rootFolderPath"] = root_folder_path
        payload["qualityProfileId"] = quality_profile_id
        payload["monitored"] = True
        payload["seasonFolder"] = True

        selected_seasons = self._apply_season_monitoring(
            payload,
            season_mode=season_mode,
            preserve_existing=False,
        )

        payload["addOptions"] = {
            "searchForMissingEpisodes": search_for_missing_episodes,
        }

        added = await self._request(
            "POST",
            "/api/v3/series",
            json=payload,
        )

        if not isinstance(added, dict):
            raise ArrError("Sonarr returned an invalid response while adding series")

        added["_telegram_media_bot_existing"] = False
        added["_telegram_media_bot_selected_seasons"] = sorted(selected_seasons)
        added["_telegram_media_bot_monitored_seasons"] = (
            self._get_monitored_season_numbers(added)
        )

        return added

    async def lookup_and_add_series(
        self,
        *,
        title: str,
        year: str | int | None,
        root_folder_path: str,
        quality_profile_id: int,
        tvdb_id: int | None = None,
        season_mode: str = "all",
    ) -> dict[str, Any]:
        """Lookup and add/update a series in Sonarr."""

        # If we know TVDB ID, first check whether the series already exists.
        if tvdb_id:
            existing_series = await self.get_existing_series_by_tvdb_id(tvdb_id)

        if existing_series is not None:
            selected_seasons = self._apply_season_monitoring(
                existing_series,
                season_mode=season_mode,
                preserve_existing=True,
            )

            updated_series = await self.update_series(existing_series)

            series_id = updated_series.get("id") or existing_series.get("id")

            if not series_id:
                raise ArrError("Sonarr updated series but did not return an id")

            await self.search_series_seasons(
                series_id=int(series_id),
                season_numbers=selected_seasons,
            )

            updated_series["_telegram_media_bot_existing"] = True
            updated_series["_telegram_media_bot_selected_seasons"] = sorted(selected_seasons)
            updated_series["_telegram_media_bot_monitored_seasons"] = (
                self._get_monitored_season_numbers(updated_series)
            )

            return updated_series

        # Otherwise add as new.
        series = await self.lookup_series(
            title=title,
            year=year,
            tvdb_id=tvdb_id,
        )

        if series is None:
            raise ArrError(f"Sonarr could not find series: {title} ({year})")

        return await self.add_series(
            series,
            root_folder_path=root_folder_path,
            quality_profile_id=quality_profile_id,
            season_mode=season_mode,
            search_for_missing_episodes=True,
        )

    def _apply_season_monitoring(
        self,
        series: dict[str, Any],
        *,
        season_mode: str,
        preserve_existing: bool = True,
    ) -> set[int]:
        """Apply first/latest/all season monitoring to a Sonarr series payload.

        For existing shows:
        - preserve_existing=True means only add newly selected seasons.
        - manually unmonitored seasons stay unmonitored unless selected now.

        For new shows:
        - preserve_existing=False means only the selected seasons are monitored.
        """

        seasons = series.get("seasons")

        if not isinstance(seasons, list):
            return set()

        normal_season_numbers: list[int] = []

        for season in seasons:
            if not isinstance(season, dict):
                continue

            try:
                season_number = int(season.get("seasonNumber", -1))
            except (TypeError, ValueError):
                continue

            if season_number > 0:
                normal_season_numbers.append(season_number)

        selected_seasons: set[int] = set()

        if season_mode == "first" and normal_season_numbers:
            selected_seasons = {min(normal_season_numbers)}

        elif season_mode == "last" and normal_season_numbers:
            selected_seasons = {max(normal_season_numbers)}

        elif season_mode == "all":
            selected_seasons = set(normal_season_numbers)

        else:
            raise ArrError(f"Unknown season mode: {season_mode}")

        for season in seasons:
            if not isinstance(season, dict):
                continue

            try:
                season_number = int(season.get("seasonNumber", -1))
            except (TypeError, ValueError):
                season["monitored"] = False
                continue

            # Never monitor specials by default.
            if season_number <= 0:
                season["monitored"] = False
                continue

            if preserve_existing:
                # Important:
                # Keep whatever Sonarr currently has, unless this season was selected now.
                if season_number in selected_seasons:
                    season["monitored"] = True
                else:
                    season["monitored"] = bool(season.get("monitored", False))
            else:
                # New show: only selected seasons are monitored.
                season["monitored"] = season_number in selected_seasons

        return selected_seasons
    def _get_monitored_season_numbers(
        self,
        series: dict[str, Any],
    ) -> list[int]:
        """Return monitored normal season numbers from a Sonarr series."""

        seasons = series.get("seasons")

        if not isinstance(seasons, list):
            return []

        monitored_seasons: list[int] = []

        for season in seasons:
            if not isinstance(season, dict):
                continue

            try:
                season_number = int(season.get("seasonNumber", -1))
            except (TypeError, ValueError):
                continue

            if season_number <= 0:
                continue

            if season.get("monitored") is True:
                monitored_seasons.append(season_number)

        return sorted(monitored_seasons)

    async def get_existing_series_by_tvdb_id(
        self,
        tvdb_id: int,
    ) -> dict[str, Any] | None:
        """Return an existing Sonarr series by TVDB ID."""

        if not tvdb_id:
            return None

        series_list = await self._request(
            "GET",
            "/api/v3/series",
        )

        if not isinstance(series_list, list):
            return None

        for series in series_list:
            if not isinstance(series, dict):
                continue

            try:
                existing_tvdb_id = int(series.get("tvdbId", 0))
            except (TypeError, ValueError):
                existing_tvdb_id = 0

            if existing_tvdb_id == tvdb_id:
                return series

        return None

    async def update_series(
        self,
        series: dict[str, Any],
    ) -> dict[str, Any]:
        """Update an existing Sonarr series."""

        series_id = series.get("id")

        if not series_id:
            raise ArrError("Cannot update Sonarr series without an id")

        updated = await self._request(
            "PUT",
            f"/api/v3/series/{series_id}",
            json=series,
        )

        if not isinstance(updated, dict):
            raise ArrError("Sonarr returned an invalid response while updating series")

        return updated

    async def search_series_seasons(
        self,
        *,
        series_id: int,
        season_numbers: set[int],
    ) -> None:
        """Trigger Sonarr search for selected seasons."""

        if not series_id:
            raise ArrError("Cannot search Sonarr series without a series id")

        if not season_numbers:
            return

        for season_number in sorted(season_numbers):
            await self._request(
                "POST",
                "/api/v3/command",
                json={
                    "name": "SeasonSearch",
                    "seriesId": series_id,
                    "seasonNumber": season_number,
                },
            )