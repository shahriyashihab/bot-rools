"""Language selection flow - language picker, callback handler and /language command.

Flow:
    /start  ->  language selection (flag + native name buttons)
             ->  channel join (if a channel is configured)
             ->  welcome message in the selected language

The language picker is also available via the /language command at any time.
"""
import asyncio
import logging

from pyrogram import filters
from pyrogram.client import Client
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from bot.data.languages import (
    LANGUAGES,
    LANGUAGE_SELECT_CALLBACK_PREFIX,
    DEFAULT_LANGUAGE,
    build_language_label,
    is_supported_language,
    get_language_native_name,
)
from bot.utils.i18n import set_user_lang, get_user_lang, t

logger = logging.getLogger(__name__)


def build_language_keyboard() -> InlineKeyboardMarkup:
    """Build the 13-language selection keyboard.

    Layout: 2-column grid (6 rows x 2 = 12) + one full-width row for the
    default language at the bottom. Every cell shares the same width, so the
    buttons render as a straight, equal-size box grid.
    """
    from bot.data.languages import build_language_grid_rows
    buttons = []
    for row in build_language_grid_rows():
        buttons.append([
            InlineKeyboardButton(
                build_language_label(code),
                callback_data=f"{LANGUAGE_SELECT_CALLBACK_PREFIX}{code}",
            )
            for code in row
        ])
    return InlineKeyboardMarkup(buttons)


def get_language_select_text() -> str:
    """Header shown BEFORE a language is known. English default keeps it universal."""
    return t("language_select_message", DEFAULT_LANGUAGE)


async def send_language_selection(
    client: Client,
    chat_id: int,
    message: Message = None,
    callback: CallbackQuery = None,
) -> None:
    """Send (or edit) the language-selection prompt."""
    text = get_language_select_text()
    keyboard = build_language_keyboard()
    try:
        if callback:
            await callback.message.edit_text(text, reply_markup=keyboard)
        elif message:
            await message.reply(text, reply_markup=keyboard)
        else:
            await client.send_message(chat_id, text, reply_markup=keyboard)
    except Exception as e:
        logger.warning(f"Language selection prompt error: {e}")


async def _continue_after_language(client: Client, chat_id: int, lang: str) -> None:
    """After a language is chosen: enforce channel membership, then greet."""
    try:
        from bot.middleware.channel_guard import ensure_membership, send_join_prompt
        is_member = await ensure_membership(client, chat_id, force_check=True)
        if not is_member:
            await send_join_prompt(client, chat_id)
            return
    except Exception as e:
        logger.warning(f"Channel check after language selection failed: {e}")

    from bot.utils.fast_cache import get_cached_template
    welcome = get_cached_template("welcome_message", lang)
    try:
        await client.send_message(chat_id, welcome)
    except Exception as e:
        logger.warning(f"Welcome send error: {e}")


def register_language_handlers(app: Client) -> None:
    """Register language selection callbacks and the /language command."""

    @app.on_callback_query(filters.regex(rf"^{LANGUAGE_SELECT_CALLBACK_PREFIX}"))
    async def language_select_callback(client: Client, query: CallbackQuery):
        user_id = query.from_user.id
        data = query.data or ""
        lang = data[len(LANGUAGE_SELECT_CALLBACK_PREFIX):]

        if not is_supported_language(lang):
            await query.answer()
            return

        set_user_lang(user_id, lang)
        await query.answer()

        from bot.utils.fast_cache import get_cached_template
        changed_text = t("language_changed_message", lang, language=build_language_label(lang))
        try:
            await query.message.edit_text(changed_text, reply_markup=None)
        except Exception:
            pass

        await _continue_after_language(client, user_id, lang)

    @app.on_message(filters.private & filters.command("language"))
    async def language_command(client: Client, message: Message):
        user_id = message.chat.id
        await send_language_selection(client, user_id, message=message)


def needs_language_selection(user_id) -> bool:
    """True when the user has not chosen a language yet."""
    return get_user_lang(user_id) == DEFAULT_LANGUAGE and not _has_explicit_lang(user_id)


def _has_explicit_lang(user_id) -> bool:
    try:
        from bot.utils.storage import get_user_info
        user = get_user_info(user_id, create_if_missing=False)
        return bool(user and is_supported_language(user.get("lang", "")))
    except Exception:
        return False
