"""Private Telegram customer chat using the existing AutoCare HTTP API."""
import logging
import os
import sqlite3
import time
from contextlib import closing
from html import escape
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
from core.config import DATA_DIR, load_environment

DECISIONS = {"confirm": "Confirm", "reject": "Reject", "modify": "Modify"}
CAPABILITIES = (
    "Browse AutoCare cars, prices, and features.",
    "Search the web for car reviews and comparisons.",
    "Estimate your monthly car payment at 8.5% annual interest.",
    "Check showroom availability and book a visit.",
    "Look up, reschedule, or cancel an existing booking.",
)
CAPABILITY_LIST = "\n".join(f"• {item}" for item in CAPABILITIES)
BOT_DESCRIPTION = f"AutoCare Motors\n\n{CAPABILITY_LIST}"
WELCOME = (
    f"<b>Welcome to AutoCare Motors</b>\n\n{escape(CAPABILITY_LIST)}\n\n"
    "<b>Try asking</b> “Which cars do you sell?” or “Can I visit tomorrow at 3 PM?”\n\n"
    "/status shows your latest reply or pending approval. /new starts a fresh conversation."
)


class TelegramError(Exception):
    def __init__(self, status):
        self.status = status


class TelegramBot:
    def __init__(self, client, token, api_url, sessions_path):
        self.client = client
        self.token = token
        self.telegram_url = f"https://api.telegram.org/bot{token}"
        self.api_url = api_url.rstrip("/")
        self.sessions_path = sessions_path
        with closing(sqlite3.connect(sessions_path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS telegram_threads (
                bot_id TEXT NOT NULL, chat_id INTEGER NOT NULL,
                thread_id TEXT NOT NULL, reset_id INTEGER,
                PRIMARY KEY (bot_id, chat_id)
            )""")

    def thread_id(self, chat_id, reset_id=None):
        # Telegram's bot ID stays the same if its token is rotated.
        bot_id = self.token.partition(":")[0]
        with closing(sqlite3.connect(self.sessions_path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT thread_id, reset_id FROM telegram_threads WHERE bot_id = ? AND chat_id = ?",
                             (bot_id, chat_id)).fetchone()
            if row and (reset_id is None or reset_id == row[1]):
                return row[0]
            thread_id = str(uuid4())
            db.execute("""INSERT INTO telegram_threads VALUES (?, ?, ?, ?)
                ON CONFLICT (bot_id, chat_id) DO UPDATE
                SET thread_id = excluded.thread_id, reset_id = excluded.reset_id""",
                       (bot_id, chat_id, thread_id, reset_id))
            return thread_id

    def telegram(self, method, *, http_timeout=40, **fields):
        response = self.client.post(f"{self.telegram_url}/{method}", json=fields, timeout=http_timeout)
        if response.is_error:
            raise TelegramError(response.status_code)
        payload = response.json()
        if not payload.get("ok"):
            raise TelegramError(payload.get("error_code"))
        return payload["result"]

    def api(self, endpoint, thread_id, **fields):
        response = self.client.post(f"{self.api_url}/{endpoint}",
                                    json={"thread_id": thread_id, **fields}, timeout=180)
        if endpoint == "resume" and response.status_code == 409:
            return self.api("chat", thread_id)
        response.raise_for_status()
        return response.json()

    def send(self, chat_id, text, markup=None):
        # A 2,000-code-point chunk also fits Telegram's limit for emoji-heavy text.
        while len(text) > 2000:
            split = text.rfind("\n", 1000, 2000)
            split = split + 1 if split != -1 else 2000
            self.telegram("sendMessage", chat_id=chat_id, text=text[:split],
                          link_preview_options={"is_disabled": True})
            text = text[split:]
        fields = {"reply_markup": markup} if markup else {}
        self.telegram("sendMessage", chat_id=chat_id, text=text,
                      link_preview_options={"is_disabled": True}, **fields)

    def configure_description(self):
        try:
            self.telegram("setMyDescription", http_timeout=5, description=BOT_DESCRIPTION)
        except (TelegramError, httpx.HTTPError, ValueError):
            logging.warning("Could not update the Telegram bot description. Polling will continue.")

    def present(self, chat_id, state):
        visit = state.get("approval")
        if not visit:
            self.send(chat_id, state.get("message") or "No booking approval is pending. Send a message to get started.")
            return
        operation = visit.get("operation")
        title = {"reschedule": "Review your reschedule request", "cancel": "Review your cancellation request"}
        lines = [
            title.get(operation, "Review your showroom visit"),
            f"Name: {visit['name']}",
            f"Email: {visit['email']}",
            f"Vehicle: {visit['car_model']}",
        ]
        if operation:
            lines.append(f"Booking ID: {visit['booking_id']}")
        if operation == "reschedule":
            lines.append(f"Original slot: {visit['previous_date']} at {visit['previous_time']}")
        label = "New slot" if operation == "reschedule" else "When"
        lines += [f"{label}: {visit['date']} at {visit['time']} ({visit['timezone']})",
                  f"Duration: {visit['duration_minutes']} minutes"]
        if not operation:
            lines.append("Confirm to book and request a calendar invitation at the email above.")
        approval_id = visit.get("approval_id", visit["booking_id"])
        buttons = [{"text": label, "callback_data": f"{decision}:{approval_id}"}
                   for decision, label in DECISIONS.items() if decision != "modify" or operation != "cancel"]
        self.send(chat_id, "\n".join(lines), {"inline_keyboard": [buttons]})

    def message(self, message, update_id):
        chat = message["chat"]
        if chat["type"] != "private":
            return
        chat_id = chat["id"]
        text = (message.get("text") or "").strip()
        if not text:
            self.send(chat_id, "Send a text message to chat with AutoCare Motors.")
            return
        command = text.split(maxsplit=1)[0].partition("@")[0].lower()
        if command == "/new":
            self.thread_id(chat_id, reset_id=update_id)
            self.send(chat_id, "Started a new AutoCare conversation. How can I help?")
        elif command in ("/start", "/help"):
            self.telegram("sendMessage", chat_id=chat_id, text=WELCOME, parse_mode="HTML")
        elif command == "/status":
            self.present(chat_id, self.api("chat", self.thread_id(chat_id)))
        else:
            bot_id = self.token.partition(":")[0]
            search_scope = uuid5(NAMESPACE_URL, f"autocare:telegram:{bot_id}:{chat_id}")
            self.present(chat_id, self.api("chat", self.thread_id(chat_id), message=text,
                                           message_id=f"telegram:{message['message_id']}",
                                           search_scope_id=str(search_scope)))

    def callback(self, callback):
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        private = chat.get("type") == "private" and callback["from"]["id"] == chat["id"]
        decision, _, approval_id = callback.get("data", "").partition(":")
        if not private or decision not in DECISIONS:
            self.telegram("answerCallbackQuery", callback_query_id=callback["id"], text="This action is unavailable.")
            return
        try:
            approval_id = str(UUID(approval_id))
        except ValueError:
            self.telegram("answerCallbackQuery", callback_query_id=callback["id"], text="This approval is invalid.")
            return

        # A callback can expire before processing. Its booking decision can still be valid.
        try:
            self.telegram("answerCallbackQuery", callback_query_id=callback["id"])
        except TelegramError as exc:
            if exc.status != 400:
                raise
        chat_id = chat["id"]
        state = self.api("resume", self.thread_id(chat_id), approval_id=approval_id, decision=decision)
        self.present(chat_id, state)
        try:
            self.telegram("editMessageReplyMarkup", chat_id=chat_id, message_id=message["message_id"],
                          reply_markup={"inline_keyboard": []})
        except TelegramError as exc:
            if exc.status != 400:
                raise

    def handle(self, update):
        callback = update.get("callback_query")
        message = (callback or {}).get("message") if callback else update.get("message")
        try:
            if callback:
                self.callback(callback)
            elif message:
                self.message(message, update["update_id"])
        except TelegramError as exc:
            if exc.status not in (400, 403):
                raise
        except (httpx.HTTPError, sqlite3.Error, ValueError):
            if message and message["chat"]["type"] == "private":
                self.send(message["chat"]["id"], "AutoCare could not finish that request. Try /status or send your message again.")

    def poll(self):
        offset = 0
        while True:
            try:
                updates = self.telegram("getUpdates", offset=offset, timeout=25,
                                        allowed_updates=["message", "callback_query"])
                for update in updates:
                    self.handle(update)
                    offset = update["update_id"] + 1
            except TelegramError as exc:
                if exc.status in (401, 404):
                    raise SystemExit("Telegram rejected TELEGRAM_BOT_TOKEN.") from None
                if exc.status == 409:
                    raise SystemExit("Telegram polling is already active or a webhook is configured.") from None
                logging.warning("Telegram request failed (status %s). Retrying.", exc.status)
                time.sleep(5)
            except (httpx.HTTPError, ValueError):
                logging.warning("Telegram connection interrupted. Retrying.")
                time.sleep(5)


def main():
    load_environment()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env before starting the bot.")
    # Telegram includes the token in request URLs.
    for logger in ("httpx", "httpcore"):
        logging.getLogger(logger).setLevel(logging.WARNING)
    api_url = os.getenv("API_URL") or "http://127.0.0.1:8000"
    sessions_path = os.getenv("TELEGRAM_SESSIONS_PATH") or DATA_DIR / "telegram_sessions.sqlite"
    try:
        with httpx.Client() as client:
            bot = TelegramBot(client, token, api_url, sessions_path)
            bot.configure_description()
            bot.poll()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
