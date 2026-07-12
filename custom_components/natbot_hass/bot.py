from __future__ import annotations

import logging
from typing import Any

from aiohttp import ClientError, ClientTimeout
from html import escape
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .arr import ArrError, RadarrClient, SonarrClient, TvdbLookupClient

from .const import (
    EVENT_DOWNLOAD_REQUESTED,
    EVENT_TELEGRAM_CALLBACK,
    EVENT_TELEGRAM_MESSAGE,
    SEARCH_MODE_COMMAND,
    SEARCH_MODE_SIMPLE,
)

_LOGGER = logging.getLogger(__name__)

TMDB_PAGE_SIZE = 20
TMDB_API_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_URL = "https://image.tmdb.org/t/p/w500"

def _build_application(token: str) -> Application:
    """Build Telegram application outside Home Assistant's event loop."""
    return ApplicationBuilder().token(token).build()


class TelegramMediaBot:
    def __init__(
        self,
        hass: HomeAssistant,
        token: str,
        allowed_chat_ids: set[int],
        tmdb_api_key: str,
        search_mode: str = SEARCH_MODE_SIMPLE,
        search_command: str = "find",
        radarr_url: str = "",
        radarr_api_key: str = "",
        radarr_root_folder: str = "",
        radarr_quality_profile_id: int = 1,
        sonarr_url: str = "",
        sonarr_api_key: str = "",
        sonarr_root_folder: str = "",
        sonarr_quality_profile_id: int = 1,
    ) -> None:
        self.hass = hass
        self.token = token
        self.allowed_chat_ids = allowed_chat_ids
        self.tmdb_api_key = tmdb_api_key
        self.search_mode = search_mode

        self.search_command = search_command.strip().lstrip("/") or "find"
        self.radarr_root_folder = radarr_root_folder
        self.radarr_quality_profile_id = radarr_quality_profile_id

        self.sonarr_root_folder = sonarr_root_folder
        self.sonarr_quality_profile_id = sonarr_quality_profile_id

        self.radarr = RadarrClient(
            hass=hass,
            base_url=radarr_url,
            api_key=radarr_api_key,
        )

        self.sonarr = SonarrClient(
            hass=hass,
            base_url=sonarr_url,
            api_key=sonarr_api_key,
        )

        self.tvdb_lookup = TvdbLookupClient(hass)

        self.application: Application | None = None

        self.status = "stopped"
        self.last_message: str | None = None
        self.last_user: str | None = None
        self.last_chat_id: int | None = None
        self.last_callback: str | None = None

        self.search_sessions: dict[int, dict[str, Any]] = {}
        self.pending_series_requests: dict[int, dict[str, Any]] = {}

    async def async_start(self) -> None:
        """Start the Telegram bot."""

        if self.application is not None:
            _LOGGER.warning("Natbot is already started")
            return

        self.status = "starting"

        self.application = await self.hass.async_add_executor_job(
            _build_application,
            self.token,
        )

        self.application.add_handler(CommandHandler("start", self._handle_start))
        self.application.add_handler(CommandHandler("help", self._handle_help))
        if self.search_mode == SEARCH_MODE_COMMAND:
            self.application.add_handler(
                CommandHandler(self.search_command, self._handle_search_command)
            )
        self.application.add_handler(CallbackQueryHandler(self._handle_callback))
        self.application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )

        await self.application.initialize()
        await self.application.start()

        if self.application.updater is None:
            raise RuntimeError("Telegram updater was not created")

        await self.application.updater.start_polling()

        self.status = "running"
        _LOGGER.warning("Natbot started")

    async def async_stop(self) -> None:
        """Stop the Telegram bot cleanly."""

        if self.application is None:
            self.status = "stopped"
            return

        self.status = "stopping"

        try:
            if self.application.updater is not None:
                await self.application.updater.stop()

            await self.application.stop()
            await self.application.shutdown()

        finally:
            self.application = None
            self.status = "stopped"

        _LOGGER.warning("Natbot stopped")

    def _is_allowed(self, chat_id: int) -> bool:
        if not self.allowed_chat_ids:
            return True

        return chat_id in self.allowed_chat_ids

    async def _handle_start(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if update.effective_chat is None or update.message is None:
            return

        chat_id = update.effective_chat.id

        if not self._is_allowed(chat_id):
            await update.message.reply_text("Not allowed.")
            return

        if self.search_mode == SEARCH_MODE_COMMAND:
            message = (
                f"Hello! Use /{self.search_command} followed by a movie or TV show name.\n"
                f"Example: /{self.search_command} Titanic"
            )
        else:
            message = "Hello! Send me a movie or TV show name."

        await update.message.reply_text(
            message,
            reply_markup=ReplyKeyboardRemove(),
        )

    async def _handle_help(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if update.effective_chat is None or update.message is None:
            return

        chat_id = update.effective_chat.id

        if not self._is_allowed(chat_id):
            await update.message.reply_text("Not allowed.")
            return

        if self.search_mode == SEARCH_MODE_COMMAND:
            message = (
                f"Use /{self.search_command} followed by a movie or TV show name.\n"
                f"Example: /{self.search_command} Breaking Bad"
            )
        else:
            message = (
                "Send a movie or TV show name."
            )

        await update.message.reply_text(
            message,
            reply_markup=ReplyKeyboardRemove(),
        )

    async def _handle_search_command(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handle configured slash command search, e.g. /find titanic."""

        if update.message is None or update.effective_chat is None:
            return

        chat_id = update.effective_chat.id

        if not self._is_allowed(chat_id):
            await update.message.reply_text("Not allowed.")
            return

        query_text = " ".join(context.args).strip()

        if not query_text:
            await update.message.reply_text(
                f"Usage: /{self.search_command} movie or TV show name"
            )
            return

        await self.async_handle_search_text(
            update=update,
            chat_id=chat_id,
            query_text=query_text,
        )

    async def _handle_message(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handle plain text messages as movie searches in simple mode."""

        if update.message is None or update.effective_chat is None:
            return

        chat_id = update.effective_chat.id

        if not self._is_allowed(chat_id):
            await update.message.reply_text("Not allowed.")
            return

        query_text = (update.message.text or "").strip()

        if not query_text:
            await update.message.reply_text("Send me a movie or TV show name.")
            return

        if self.search_mode == SEARCH_MODE_COMMAND:
            await update.message.reply_text(
                f"Use /{self.search_command} followed by a movie or TV show name.\n"
                f"Example: /{self.search_command} Titanic"
            )
            return

        await self.async_handle_search_text(
            update=update,
            chat_id=chat_id,
            query_text=query_text,
        )

    async def async_handle_search_text(
        self,
        *,
        update: Update,
        chat_id: int,
        query_text: str,
    ) -> None:
        """Run an OMDb search and show the first result."""

        if update.message is None:
            return

        user = update.effective_user.full_name if update.effective_user else "Unknown"

        self.last_message = query_text
        self.last_user = user
        self.last_chat_id = chat_id

        self.hass.bus.async_fire(
            EVENT_TELEGRAM_MESSAGE,
            {
                "chat_id": chat_id,
                "user": user,
                "message": query_text,
            },
        )

        search_result = await self.async_search_movies_page(query_text, page=1)
        results = search_result["results"]

        if not results:
            await update.message.reply_text(f'No results found for "{query_text}".')
            self.async_schedule_sensor_update()
            return

        self.search_sessions[chat_id] = {
            "query": query_text,
            "results": results,
            "index": 0,
            "page": 1,
            "total_results": search_result["total_results"],
            "has_more": search_result["has_more"],
        }

        await self.async_send_current_result(chat_id=chat_id)
        self.async_schedule_sensor_update()     

    async def _handle_callback(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handle inline keyboard callbacks."""

        query = update.callback_query

        if query is None or query.message is None:
            return

        chat_id = query.message.chat_id

        if not self._is_allowed(chat_id):
            await query.answer("Not allowed.")
            return

        data = query.data or ""

        self.last_callback = data
        self.last_chat_id = chat_id

        self.hass.bus.async_fire(
            EVENT_TELEGRAM_CALLBACK,
            {
                "chat_id": chat_id,
                "callback": data,
            },
        )

        await query.answer()

        session = self.search_sessions.get(chat_id)

        if session is None:
            await query.edit_message_text("Search session expired. Send the title again.")
            self.async_schedule_sensor_update()
            return

        results = session.get("results", [])
        if not results:
            await query.edit_message_text(
                "Search session has no results. Send the title again."
            )
            self.async_schedule_sensor_update()
            return

        index = int(session.get("index", 0))

        if data == "next":
            is_at_last_loaded_result = index >= len(results) - 1
            has_more = bool(session.get("has_more", False))

            if is_at_last_loaded_result and has_more:
                await self.async_load_next_page_and_move(query)
            else:
                index = (index + 1) % len(results)
                session["index"] = index
                await self.async_edit_current_result(query)


        elif data == "prev":
            index = (index - 1) % len(results)
            session["index"] = index
            await self.async_edit_current_result(query)

        elif data.startswith("download:"):
            result = results[index]

            if result.get("_details") is None:
                details = await self.async_get_movie_details(result)
                result["_details"] = details or {}

            imdb_id = result.get("imdbID", "")

            await self.async_handle_download_request(
                query=query,
                chat_id=chat_id,
                result=result,
                imdb_id=imdb_id,
            )

            await self.async_handle_download_request(
                query=query,
                chat_id=chat_id,
                result=result,
                imdb_id=imdb_id,
            )

        elif data.startswith("season:"):
            season_mode = data.removeprefix("season:")

            pending_request = self.pending_series_requests.get(chat_id)

            if pending_request is None:
                await query.edit_message_text(
                    "Season selection expired. Please search for the show again."
                )
                self.async_schedule_sensor_update()
                return

            try:
                await self.async_handle_series_season_choice(
                    query=query,
                    chat_id=chat_id,
                    pending_request=pending_request,
                    season_mode=season_mode,
                )

                self.pending_series_requests.pop(chat_id, None)

            except ArrError as err:
                _LOGGER.exception("Failed to add/update Sonarr series: %s", err)

                await query.edit_message_text(
                    f"❌ Failed to update Sonarr\n\n"
                    f"{escape(str(err))}",
                    parse_mode="HTML",
                )

            except Exception as err:
                _LOGGER.exception("Unexpected season selection error: %s", err)

                await query.edit_message_text(
                    f"❌ Unexpected error while updating Sonarr\n\n"
                    f"{escape(str(err))}",
                    parse_mode="HTML",
                )

        else:
            await query.edit_message_text(f"Unknown action: {data}")

        self.async_schedule_sensor_update()

    async def async_load_next_page_and_move(self, query) -> None:
        """Load the next OMDb page and move to the first newly loaded result."""

        chat_id = query.message.chat_id
        session = self.search_sessions.get(chat_id)

        if session is None:
            await query.edit_message_text("Search session expired. Send the title again.")
            return

        if not session.get("has_more", False):
            results = session.get("results", [])
            if results:
                session["index"] = 0
            await self.async_edit_current_result(query)
            return

        search_query = str(session.get("query", "")).strip()
        current_page = int(session.get("page", 1))
        next_page = current_page + 1

        search_result = await self.async_search_movies_page(
            search_query,
            page=next_page,
        )

        new_results = search_result["results"]

        if not new_results:
            session["has_more"] = False

            results = session.get("results", [])
            if results:
                session["index"] = 0

            await self.async_edit_current_result(query)
            return

        existing_results = session.get("results", [])
        existing_ids = {
            (
                item.get("tmdb_media_type"),
                item.get("tmdb_id"),
            )
            for item in existing_results
            if isinstance(item, dict)
        }

        unique_new_results = [
            item
            for item in new_results
            if (
                item.get("tmdb_media_type"),
                item.get("tmdb_id"),
            )
            not in existing_ids
        ]

        if not unique_new_results:
            session["page"] = next_page
            session["has_more"] = search_result["has_more"]

            if session["has_more"]:
                await self.async_load_next_page_and_move(query)
                return

            session["index"] = 0
            await self.async_edit_current_result(query)
            return

        first_new_index = len(existing_results)

        existing_results.extend(unique_new_results)

        session["results"] = existing_results
        session["page"] = next_page
        session["total_results"] = search_result["total_results"]
        session["has_more"] = search_result["has_more"]
        session["index"] = first_new_index

        await self.async_edit_current_result(query)

    async def async_search_movies_page(
        self,
        title: str,
        page: int = 1,
    ) -> dict[str, Any]:
        """Search one TMDB movie/TV result page."""

        session = async_get_clientsession(self.hass)

        try:
            async with session.get(
                f"{TMDB_API_URL}/search/multi",
                params={
                    "api_key": self.tmdb_api_key,
                    "query": title,
                    "page": page,
                    "include_adult": "false",
                    "language": "en-US",
                },
                timeout=ClientTimeout(total=10),
            ) as response:
                response.raise_for_status()
                data = await response.json()

        except TimeoutError:
            _LOGGER.exception("TMDB request timed out for query: %s", title)
            return {
                "results": [],
                "total_results": 0,
                "has_more": False,
            }

        except ClientError as err:
            _LOGGER.exception(
                "TMDB request failed for query %s: %s",
                title,
                err,
            )
            return {
                "results": [],
                "total_results": 0,
                "has_more": False,
            }

        except Exception as err:
            _LOGGER.exception(
                "Unexpected TMDB error for query %s: %s",
                title,
                err,
            )
            return {
                "results": [],
                "total_results": 0,
                "has_more": False,
            }

        raw_results = data.get("results", [])

        if not isinstance(raw_results, list):
            return {
                "results": [],
                "total_results": 0,
                "has_more": False,
            }

        cleaned_results: list[dict[str, Any]] = []

        for item in raw_results:
            if not isinstance(item, dict):
                continue

            media_type = item.get("media_type")

            # /search/multi also returns people.
            if media_type not in {"movie", "tv"}:
                continue

            tmdb_id = item.get("id")
            if tmdb_id is None:
                continue

            if media_type == "movie":
                title_value = item.get("title") or item.get("original_title")
                date_value = item.get("release_date") or ""
                normalized_type = "movie"
            else:
                title_value = item.get("name") or item.get("original_name")
                date_value = item.get("first_air_date") or ""
                normalized_type = "series"

            if not title_value:
                continue

            year = date_value[:4] if len(date_value) >= 4 else "N/A"

            cleaned_results.append(
                {
                    # Existing internal fields retained for minimal disruption.
                    "Title": title_value,
                    "Year": year,
                    "Type": normalized_type,

                    # IMDb ID is populated when details are loaded.
                    "imdbID": "",

                    # TMDB-specific fields.
                    "tmdb_id": int(tmdb_id),
                    "tmdb_media_type": media_type,
                    "Poster": (
                        f"{TMDB_IMAGE_URL}{item['poster_path']}"
                        if item.get("poster_path")
                        else "N/A"
                    ),
                    "_tmdb_search_result": item,
                }
            )

        try:
            total_results = int(data.get("total_results", len(cleaned_results)))
            total_pages = int(data.get("total_pages", page))
        except (TypeError, ValueError):
            total_results = len(cleaned_results)
            total_pages = page

        return {
            "results": cleaned_results,
            "total_results": total_results,
            "has_more": page < total_pages,
        }

    async def async_get_movie_details(
        self,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Fetch TMDB details and external IDs for a movie or TV show."""

        tmdb_id = result.get("tmdb_id")
        tmdb_media_type = result.get("tmdb_media_type")

        if not tmdb_id or tmdb_media_type not in {"movie", "tv"}:
            return None

        session = async_get_clientsession(self.hass)

        try:
            async with session.get(
                f"{TMDB_API_URL}/{tmdb_media_type}/{tmdb_id}",
                params={
                    "api_key": self.tmdb_api_key,
                    "language": "en-US",
                    "append_to_response": "external_ids",
                },
                timeout=ClientTimeout(total=10),
            ) as response:
                response.raise_for_status()
                data = await response.json()

        except TimeoutError:
            _LOGGER.exception(
                "TMDB details request timed out for %s ID %s",
                tmdb_media_type,
                tmdb_id,
            )
            return None

        except ClientError as err:
            _LOGGER.exception(
                "TMDB details request failed for %s ID %s: %s",
                tmdb_media_type,
                tmdb_id,
                err,
            )
            return None

        except Exception as err:
            _LOGGER.exception(
                "Unexpected TMDB details error for %s ID %s: %s",
                tmdb_media_type,
                tmdb_id,
                err,
            )
            return None

        external_ids = data.get("external_ids") or {}
        imdb_id = external_ids.get("imdb_id") or ""
        tvdb_id = external_ids.get("tvdb_id")

        genres = data.get("genres") or []
        genre_names = [
            genre.get("name")
            for genre in genres
            if isinstance(genre, dict) and genre.get("name")
        ]

        if tmdb_media_type == "movie":
            runtime = data.get("runtime")
        else:
            runtimes = data.get("episode_run_time") or []
            runtime = runtimes[0] if runtimes else None

        vote_average = data.get("vote_average")
        rating = (
            f"{float(vote_average):.1f}"
            if isinstance(vote_average, (int, float))
            else "N/A"
        )

        # Save IDs directly on the search result.
        result["imdbID"] = imdb_id
        result["tvdb_id"] = tvdb_id

        return {
            "Plot": data.get("overview") or "N/A",
            "Genre": ", ".join(genre_names) if genre_names else "N/A",
            "imdbRating": rating,
            "Runtime": f"{runtime} min" if runtime else "N/A",
            "imdbID": imdb_id,
            "tvdbID": tvdb_id,
            "tmdbID": tmdb_id,
        }

    async def async_send_current_result(
        self,
        chat_id: int,
    ) -> None:
        """Send the current movie result as a new Telegram message."""

        if self.application is None:
            raise RuntimeError("Bot is not running")

        text, keyboard = await self.async_build_current_result_message(chat_id)

        await self.application.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=keyboard,
            parse_mode="HTML",
        )

    async def async_edit_current_result(self, query) -> None:
        """Edit existing Telegram message to show current result."""

        chat_id = query.message.chat_id
        text, keyboard = await self.async_build_current_result_message(chat_id)

        await query.edit_message_text(
            text,
            reply_markup=keyboard,
            parse_mode="HTML",
        )

    async def async_build_current_result_message(
        self,
        chat_id: int,
    ) -> tuple[str, InlineKeyboardMarkup]:
        """Build text and keyboard for the current result."""

        session = self.search_sessions.get(chat_id)

        if session is None:
            return (
                "Search session expired. Send the title again.",
                InlineKeyboardMarkup([]),
            )

        results = session.get("results", [])
        index = int(session.get("index", 0))
        total_results = int(session.get("total_results", len(results)))

        if not results:
            return (
                "No results in this search session.",
                InlineKeyboardMarkup([]),
            )

        if index < 0 or index >= len(results):
            index = 0
            session["index"] = index

        result = results[index]

        title = result.get("Title", "Unknown title")
        year = result.get("Year", "Unknown year")
        imdb_id = result.get("imdbID", "")
        media_type = result.get("Type", "unknown")

        details = result.get("_details")

        if details is None:
            details = await self.async_get_movie_details(result)
            result["_details"] = details or {}

        imdb_id = result.get("imdbID", "")

        plot = None
        genre = None
        rating = None
        runtime = None

        if isinstance(details, dict):
            plot = details.get("Plot")
            genre = details.get("Genre")
            rating = details.get("imdbRating")
            runtime = details.get("Runtime")

        imdb_url = self._format_imdb_url(imdb_id)

        loaded_count = len(results)

        if plot and plot != "N/A":
            text = f'\n<a href="{escape(str(imdb_url))}">{escape(str(plot))}</a>'
        else:
            text = f'\n<a href="{escape(str(imdb_url))}">{escape(str(title))}</a>'

        keyboard_rows = [
            [
                InlineKeyboardButton("Prev", callback_data="prev"),
                InlineKeyboardButton("Next", callback_data="next"),
            ],
        ]

        keyboard_rows.append([
            InlineKeyboardButton(
                "Download",
                callback_data=f"download:{result.get('tmdb_media_type')}:{result.get('tmdb_id')}",
            )
        ])

        keyboard = InlineKeyboardMarkup(keyboard_rows)

        return text, keyboard

    async def async_handle_download_request(
        self,
        *,
        query,
        chat_id: int,
        result: dict[str, Any],
        imdb_id: str,
    ) -> None:
        """Handle Download button: add movie/series to Radarr/Sonarr."""

        title = result.get("Title", "Unknown title")
        year = result.get("Year", "")
        media_type = result.get("Type", "unknown")
        imdb_url = self._format_imdb_url(imdb_id)

        self.hass.bus.async_fire(
            EVENT_DOWNLOAD_REQUESTED,
            {
                "chat_id": chat_id,
                "imdb_id": imdb_id,
                "title": title,
                "year": year,
                "type": media_type,
                "imdb_url": imdb_url,
            },
        )

        try:
            if media_type == "movie":
                await self.async_add_movie_to_radarr(
                    query=query,
                    title=str(title),
                    year=year,
                    imdb_id=imdb_id,
                    imdb_url=imdb_url,
                )
                return

            if media_type == "series":
                self.pending_series_requests[chat_id] = {
                    "result": result,
                    "imdb_id": imdb_id,
                    "title": title,
                    "year": year,
                    "imdb_url": imdb_url,
                }

                await self.async_ask_series_season_mode(
                    query=query,
                    title=str(title),
                    year=year,
                    imdb_url=imdb_url,
                )
                return

            await query.edit_message_text(
                f"I only know how to add movies and series right now.\n"
                f"OMDb type was: {escape(str(media_type))}",
                parse_mode="HTML",
            )

        except ArrError as err:
            _LOGGER.exception("Failed to add media to Arr: %s", err)

            await query.edit_message_text(
                f"❌ Failed to add\n"
                f"<b>{escape(str(title))} ({escape(str(year))})</b>\n\n"
                f"{escape(str(err))}",
                parse_mode="HTML",
            )
    async def async_send_message(
        self,
        chat_id: int,
        message: str,
    ) -> None:
        """Send a plain Telegram message."""

        if self.application is None:
            raise RuntimeError("Bot is not running")

        await self.application.bot.send_message(
            chat_id=chat_id,
            text=message,
        )
    async def async_add_movie_to_radarr(
        self,
        *,
        query,
        title: str,
        year: str | int | None,
        imdb_id: str,
        imdb_url: str,
    ) -> None:
        """Add movie to Radarr."""

        if not self.radarr.configured:
            await query.edit_message_text("Radarr is not configured.")
            return

        if not self.radarr_root_folder:
            await query.edit_message_text("Radarr root folder is not configured.")
            return

        added = await self.radarr.lookup_and_add_movie(
            imdb_id=imdb_id,
            title=title,
            year=year,
            root_folder_path=self.radarr_root_folder,
            quality_profile_id=self.radarr_quality_profile_id,
        )

        added_title = added.get("title", title)
        added_year = added.get("year", year)

        await query.edit_message_text(
            f"✅ Added to Radarr\n"
            f'<a href="{escape(str(imdb_url))}"><b>{escape(str(added_title))} ({escape(str(added_year))})</b>\n</a>',
            parse_mode="HTML",
        )

    async def async_add_series_to_sonarr(
        self,
        *,
        query,
        title: str,
        year: str | int | None,
        imdb_id: str,
        imdb_url: str,
        tvdb_id: int | None,
        season_mode: str,
    ) -> None:
        """Add or update series in Sonarr."""

        if not self.sonarr.configured:
            await query.edit_message_text("Sonarr is not configured.")
            return

        if not self.sonarr_root_folder:
            await query.edit_message_text("Sonarr root folder is not configured.")
            return

        season_label = {
            "first": "first season",
            "last": "latest season",
            "all": "all seasons",
        }.get(season_mode, season_mode)

        if not tvdb_id: 
            tvdb_id = await self.tvdb_lookup.get_tvdb_id_from_imdb_id(imdb_id)
        
        added = await self.sonarr.lookup_and_add_series(
            title=title,
            year=year,
            tvdb_id=tvdb_id,
            root_folder_path=self.sonarr_root_folder,
            quality_profile_id=self.sonarr_quality_profile_id,
            season_mode=season_mode,
        )

        added_title = added.get("title", title)
        added_year = added.get("year", year)
        already_existed = bool(added.get("_telegram_media_bot_existing", False))
        selected_seasons = added.get("_telegram_media_bot_selected_seasons")

        if already_existed:
            if selected_seasons:
                seasons_text = ", ".join(str(season) for season in selected_seasons)
            else:
                seasons_text = season_label

            await query.edit_message_text(
                f"✅ Updated existing Sonarr show\n"
                f'<a href="{escape(str(imdb_url))}"><b>{escape(str(added_title))} ({escape(str(added_year))})</b>\n</a>'
                f"Seasons: {escape(str(seasons_text))}\n",
                parse_mode="HTML",
            )
            return

        await query.edit_message_text(
            f"✅ Added to Sonarr\n"
            f'<a href="{escape(str(imdb_url))}"><b>{escape(str(added_title))} ({escape(str(added_year))})</b>\n</a>'
            f"Seasons: {escape(str(season_label))}\n",
            parse_mode="HTML",
        )

    async def async_ask_series_season_mode(
        self,
        *,
        query,
        title: str,
        year: str | int | None,
        imdb_url: str,
    ) -> None:
        """Ask which seasons should be monitored/downloaded."""

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("First season", callback_data="season:first"),
                    InlineKeyboardButton("Latest season", callback_data="season:last"),
                ],
                [
                    InlineKeyboardButton("All seasons", callback_data="season:all"),
                ],
            ]
        )

        await query.edit_message_text(
            f"Which seasons should I grab?\n"
            f'<a href="{escape(str(imdb_url))}"><b>{escape(str(title))} ({escape(str(year))})</b>\n</a>',
            reply_markup=keyboard,
            parse_mode="HTML",
        )

    async def async_handle_series_season_choice(
        self,
        *,
        query,
        chat_id: int,
        pending_request: dict[str, Any],
        season_mode: str,
    ) -> None:
        """Handle First / Latest / All season choice for a series."""

        if season_mode not in {"first", "last", "all"}:
            await query.edit_message_text("Unknown season option.")
            return

        result = pending_request["result"]
        imdb_id = pending_request["imdb_id"]
        imdb_url = pending_request["imdb_url"]

        title = result.get("Title", "Unknown title")
        year = result.get("Year", "")
        media_type = result.get("Type", "unknown")
        tvdb_id = result.get("tvdb_id")

        if media_type != "series":
            await query.edit_message_text("This option is only available for TV shows.")
            return

        await self.async_add_series_to_sonarr(
            query=query,
            title=str(title),
            year=year,
            imdb_id=imdb_id,
            imdb_url=imdb_url,
            tvdb_id=tvdb_id,
            season_mode=season_mode,
        )
    
    async def async_ask_movie(
        self,
        chat_id: int,
        title: str,
    ) -> None:
        """Search a movie from a Home Assistant service call."""

        if self.application is None:
            raise RuntimeError("Bot is not running")

        title = title.strip()

        if not title:
            await self.application.bot.send_message(
                chat_id=chat_id,
                text="Please provide a movie or TV show title.",
            )
            return

        search_result = await self.async_search_movies_page(title, page=1)
        results = search_result["results"]

        if not results:
            await self.application.bot.send_message(
                chat_id=chat_id,
                text=f'No results found for "{title}".',
            )
            return

        self.search_sessions[chat_id] = {
            "query": title,
            "results": results,
            "index": 0,
            "page": 1,
            "total_results": search_result["total_results"],
            "has_more": search_result["has_more"],
        }

        await self.async_send_current_result(chat_id=chat_id)

        self.async_schedule_sensor_update()

    def async_schedule_sensor_update(self) -> None:
        """Notify sensors/entities that bot state changed."""

        self.hass.bus.async_fire(
            "telegram_media_bot_updated",
            {},
        )

    @staticmethod
    def _format_imdb_url(imdb_id: str) -> str:
        """Format IMDb title URL."""

        if not imdb_id:
            return "https://www.imdb.com/"

        return f"https://www.imdb.com/title/{imdb_id}/"