"""
Certificate Generator Telegram Bot
-----------------------------------
Turns a one-page PDF or image template into a personalized PDF
certificate: recipient name, event title, date, a short certificate
ID, and an optional QR verification code.

Environment variables (see .env.example):
    BOT_TOKEN        - Telegram bot token from @BotFather (required)
    ADMIN_IDS        - comma-separated Telegram user IDs allowed to use
                        the bot (optional; if empty, anyone can use it)
    VERIFY_BASE_URL  - optional base URL used to build a QR
                        verification link, e.g. https://example.com/verify
"""

from __future__ import annotations

import logging
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import date
from io import BytesIO
from pathlib import Path

import fitz  # PyMuPDF
import qrcode
from PIL import Image
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()
}
VERIFY_BASE_URL = os.environ.get("VERIFY_BASE_URL", "").rstrip("/")

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("certificate_bot")

# --------------------------------------------------------------------
# Conversation state
# --------------------------------------------------------------------

WAIT_TEMPLATE, WAIT_NAME, WAIT_EVENT, WAIT_DATE = range(4)
STATE_KEY = "state"


@dataclass
class Draft:
    template_path: Path | None = None
    name: str = ""
    event: str = ""
    cert_date: str = ""


# --------------------------------------------------------------------
# Access control
# --------------------------------------------------------------------

def is_allowed(update: Update) -> bool:
    """With no ADMIN_IDS configured the bot is open to everyone;
    otherwise only listed Telegram user IDs may use it."""
    if not ADMIN_IDS:
        return True
    user = update.effective_user
    return bool(user and user.id in ADMIN_IDS)


# --------------------------------------------------------------------
# Template handling
# --------------------------------------------------------------------

def user_dir(update: Update) -> Path:
    user = update.effective_user
    folder = DATA_DIR / str(user.id if user else "unknown")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def image_to_pdf(image_path: Path) -> Path:
    """Wrap a single image in a one-page PDF so the rest of the
    pipeline only has to deal with PDF templates."""
    pdf_path = image_path.with_suffix(".pdf")
    with Image.open(image_path) as im:
        im.convert("RGB").save(pdf_path, "PDF")
    return pdf_path


# --------------------------------------------------------------------
# Certificate rendering
# --------------------------------------------------------------------

def fit_font_size(
    font: fitz.Font, text: str, max_width: float, start: float, floor: float
) -> float:
    size = start
    while size > floor:
        if font.text_length(text, fontsize=size) <= max_width:
            return size
        size -= 0.5
    return floor


def draw_centered_line(
    page: fitz.Page,
    text: str,
    y_fraction: float,
    *,
    font: fitz.Font,
    start_size: float,
    floor_size: float,
    color=(0.1, 0.1, 0.1),
) -> None:
    """Draw one line of text horizontally centered at a given
    fraction of the page height."""
    page_width = page.rect.width
    max_width = page_width * 0.8
    size = fit_font_size(font, text, max_width, start_size, floor_size)
    width = font.text_length(text, fontsize=size)
    x = (page_width - width) / 2
    y = page.rect.height * y_fraction
    page.insert_text((x, y), text, fontname="helv", fontsize=size, color=color)


def render_certificate(draft: Draft, cert_id: str, out_path: Path) -> None:
    doc = fitz.open(draft.template_path)
    try:
        if len(doc) != 1:
            raise ValueError("Template must be a single-page PDF or image.")

        page = doc[0]
        font = fitz.Font("helv")

        draw_centered_line(page, draft.name, 0.45, font=font, start_size=34, floor_size=16)
        draw_centered_line(
            page,
            f"for completing \u201c{draft.event}\u201d",
            0.55,
            font=font,
            start_size=18,
            floor_size=11,
        )
        draw_centered_line(page, draft.cert_date, 0.63, font=font, start_size=13, floor_size=10)
        draw_centered_line(
            page,
            f"Certificate ID: {cert_id}",
            0.95,
            font=font,
            start_size=9,
            floor_size=7,
            color=(0.4, 0.4, 0.4),
        )

        if VERIFY_BASE_URL:
            qr_img = qrcode.make(f"{VERIFY_BASE_URL}/{cert_id}")
            buf = BytesIO()
            qr_img.save(buf, format="PNG")
            qr_size = page.rect.width * 0.12
            qr_rect = fitz.Rect(
                page.rect.width - qr_size - 24,
                page.rect.height - qr_size - 24,
                page.rect.width - 24,
                page.rect.height - 24,
            )
            page.insert_image(qr_rect, stream=buf.getvalue())

        doc.save(out_path, deflate=True)
    finally:
        doc.close()


# --------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------

def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="cancel")]])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Access restricted.")
        return
    context.user_data.clear()
    await update.message.reply_text(
        "Certificate Generator Bot\n\n"
        "Send /new to create a certificate. You'll need a one-page "
        "PDF or image to use as the background template."
    )


async def new_certificate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Access restricted.")
        return
    context.user_data.clear()
    context.user_data["draft"] = Draft()
    context.user_data[STATE_KEY] = WAIT_TEMPLATE
    await update.message.reply_text(
        "Send a one-page PDF or image to use as the certificate background.",
        reply_markup=cancel_keyboard(),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    target = update.message or update.callback_query.message
    await target.reply_text("Cancelled. Send /new to start again.")


async def on_cancel_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text("Cancelled. Send /new to start again.")


async def receive_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    if context.user_data.get(STATE_KEY) != WAIT_TEMPLATE:
        return

    doc = update.message.document
    photo = update.message.photo
    folder = user_dir(update)

    if doc:
        filename = (doc.file_name or "").lower()
        if filename.endswith(".pdf"):
            tg_file = await doc.get_file()
            raw_path = folder / "template.pdf"
            await tg_file.download_to_drive(custom_path=str(raw_path))
            template_path = raw_path
        elif filename.endswith((".png", ".jpg", ".jpeg")):
            tg_file = await doc.get_file()
            raw_path = folder / f"template{Path(filename).suffix}"
            await tg_file.download_to_drive(custom_path=str(raw_path))
            template_path = image_to_pdf(raw_path)
        else:
            await update.message.reply_text("Unsupported file type. Send a PDF, PNG or JPG.")
            return
    elif photo:
        tg_file = await photo[-1].get_file()
        raw_path = folder / "template.jpg"
        await tg_file.download_to_drive(custom_path=str(raw_path))
        template_path = image_to_pdf(raw_path)
    else:
        return

    try:
        check = fitz.open(template_path)
        page_count = len(check)
        check.close()
    except Exception as exc:
        await update.message.reply_text(f"Could not open the template: {exc}")
        return

    if page_count != 1:
        await update.message.reply_text("The template must have exactly one page.")
        return

    draft: Draft = context.user_data["draft"]
    draft.template_path = template_path
    context.user_data[STATE_KEY] = WAIT_NAME

    await update.message.reply_text(
        "Template saved. Recipient's full name?", reply_markup=cancel_keyboard()
    )


async def receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return

    state = context.user_data.get(STATE_KEY)
    if state is None:
        await update.message.reply_text("Send /new to create a certificate.")
        return

    value = (update.message.text or "").strip()
    draft: Draft = context.user_data.get("draft") or Draft()

    if state == WAIT_NAME:
        if not value or len(value) > 80:
            await update.message.reply_text("Enter a name (max 80 characters).")
            return
        draft.name = value
        context.user_data[STATE_KEY] = WAIT_EVENT
        await update.message.reply_text("Event or course title?", reply_markup=cancel_keyboard())
        return

    if state == WAIT_EVENT:
        if not value or len(value) > 120:
            await update.message.reply_text("Enter a title (max 120 characters).")
            return
        draft.event = value
        context.user_data[STATE_KEY] = WAIT_DATE
        await update.message.reply_text(
            f"Date? Send one, or /today for {date.today().isoformat()}.",
            reply_markup=cancel_keyboard(),
        )
        return

    if state == WAIT_DATE:
        draft.cert_date = value or date.today().isoformat()
        await finish_certificate(update, context, draft)
        return


async def use_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get(STATE_KEY) != WAIT_DATE:
        return
    draft: Draft = context.user_data.get("draft") or Draft()
    draft.cert_date = date.today().isoformat()
    await finish_certificate(update, context, draft)


async def finish_certificate(
    update: Update, context: ContextTypes.DEFAULT_TYPE, draft: Draft
) -> None:
    cert_id = uuid.uuid4().hex[:8].upper()

    with tempfile.TemporaryDirectory(prefix="certificate_") as tmp:
        out_path = Path(tmp) / f"certificate_{cert_id}.pdf"
        try:
            render_certificate(draft, cert_id, out_path)
        except Exception as exc:
            log.exception("Certificate rendering failed")
            await update.message.reply_text(f"Rendering failed: {exc}")
            context.user_data.clear()
            return

        with out_path.open("rb") as fh:
            await update.message.reply_document(
                document=fh,
                filename=out_path.name,
                caption=(
                    f"Certificate for {draft.name}\n"
                    f"Event: {draft.event}\n"
                    f"Date: {draft.cert_date}\n"
                    f"ID: {cert_id}"
                ),
            )

    context.user_data.clear()


# --------------------------------------------------------------------
# App wiring
# --------------------------------------------------------------------

def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("new", new_certificate))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("today", use_today))
    app.add_handler(CallbackQueryHandler(on_cancel_button, pattern="^cancel$"))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, receive_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text))

    return app


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN is not set. Copy .env.example to .env, fill it in, "
            "and export the variables before running the bot."
        )
    log.info("Starting certificate bot. Admin-restricted: %s", bool(ADMIN_IDS))
    app = build_app()
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
