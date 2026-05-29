#!/usr/bin/env python3
"""
Up Bank -> Telegram finance assistant.

A short-lived long-poller designed to run inside a GitHub Actions cron job.
All persistent state (the Telegram update offset + a cache of transactions)
lives in a single JSONBin bin, so the bot needs no server of its own.

Flow on each run:
  1. Load state (offset + cache) from JSONBin.
  2. Long-poll Telegram getUpdates for ~RUN_SECONDS.
  3. For each new message, pull Up data and answer with Claude.
  4. Persist the new offset back to JSONBin and exit.
"""

import os
import json
import time
import datetime as dt

import requests

# --------------------------------------------------------------------------
# Config (set these as GitHub repository Secrets)
# --------------------------------------------------------------------------
TELEGRAM_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
UP_TOKEN        = os.environ["UP_API_TOKEN"]
ANTHROPIC_KEY   = os.environ["ANTHROPIC_API_KEY"]
JSONBIN_KEY     = os.environ["JSONBIN_MASTER_KEY"]
JSONBIN_BIN_ID  = os.environ["JSONBIN_BIN_ID"]
# Optional: lock the bot to your own Telegram user id so nobody else can query
# your finances. Get yours by messaging @userinfobot on Telegram.
ALLOWED_USER_ID = os.environ.get("ALLOWED_TELEGRAM_USER_ID", "").strip()

# --------------------------------------------------------------------------
# Tunables (optional env overrides)
# --------------------------------------------------------------------------
CLAUDE_MODEL      = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
# POLL_TIMEOUT controls the mode:
#   0  -> "drain": wake up, answer anything queued, exit (a few seconds). Frugal;
#         replies arrive on the cron rhythm (~5 min). This is the default.
#   >0 -> "listen": hold a long-poll open for RUN_SECONDS so replies are near-instant.
POLL_TIMEOUT      = int(os.environ.get("POLL_TIMEOUT", "0"))
RUN_SECONDS       = int(os.environ.get("RUN_SECONDS", "240"))    # only used in listen mode
TXN_WINDOW_DAYS   = int(os.environ.get("TXN_WINDOW_DAYS", "90"))  # how far back to pull
CACHE_TTL_MIN     = int(os.environ.get("CACHE_TTL_MIN", "15"))    # reuse cached txns for N min
MAX_TXNS_TO_MODEL = int(os.environ.get("MAX_TXNS_TO_MODEL", "400"))
MAX_HISTORY       = int(os.environ.get("MAX_HISTORY", "12"))      # messages kept per chat (~6 turns)

# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------
UP_BASE       = "https://api.up.com.au/api/v1"
TG_BASE       = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
JSONBIN_BASE  = "https://api.jsonbin.io/v3/b"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

S = requests.Session()


def cents(value):
    """Up returns money as integer base units (cents). Convert to dollars."""
    return round(value / 100.0, 2)


# --------------------------------------------------------------------------
# State (JSONBin)
# --------------------------------------------------------------------------
def load_state():
    try:
        r = S.get(
            f"{JSONBIN_BASE}/{JSONBIN_BIN_ID}/latest",
            headers={"X-Master-Key": JSONBIN_KEY, "X-Bin-Meta": "false"},
            timeout=30,
        )
        if r.status_code == 200:
            return r.json()
    except (requests.RequestException, ValueError) as e:
        print("load_state error:", e)
    return {}


def save_state(state):
    try:
        S.put(
            f"{JSONBIN_BASE}/{JSONBIN_BIN_ID}",
            headers={"X-Master-Key": JSONBIN_KEY, "Content-Type": "application/json"},
            data=json.dumps(state),
            timeout=30,
        )
    except requests.RequestException as e:
        print("save_state error:", e)


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------
def tg(method, **params):
    try:
        return S.post(f"{TG_BASE}/{method}", json=params, timeout=70).json()
    except requests.RequestException as e:
        print(f"telegram {method} error:", e)
        return {}


def get_updates(offset):
    params = {"timeout": POLL_TIMEOUT}
    if offset is not None:
        params["offset"] = offset
    r = S.get(f"{TG_BASE}/getUpdates", params=params, timeout=POLL_TIMEOUT + 20)
    return r.json().get("result", [])


def send_message(chat_id, text):
    # Telegram caps messages at 4096 chars; chunk long answers.
    for i in range(0, len(text), 4000):
        tg("sendMessage", chat_id=chat_id, text=text[i:i + 4000])


# --------------------------------------------------------------------------
# Up Bank
# --------------------------------------------------------------------------
def up_headers():
    return {"Authorization": f"Bearer {UP_TOKEN}"}


def fetch_accounts():
    out = []
    data = S.get(f"{UP_BASE}/accounts", headers=up_headers(), timeout=30).json()
    for a in data.get("data", []):
        attr = a["attributes"]
        out.append({
            "name": attr.get("displayName"),
            "type": attr.get("accountType"),
            "balance": cents(attr["balance"]["valueInBaseUnits"]),
        })
    return out


def fetch_transactions(since_iso):
    txns = []
    url = f"{UP_BASE}/transactions"
    params = {"page[size]": 100, "filter[since]": since_iso}
    while url:
        body = S.get(url, headers=up_headers(), params=params, timeout=30).json()
        for t in body.get("data", []):
            attr = t["attributes"]
            rel = t.get("relationships", {})
            cat = (rel.get("category", {}).get("data") or {})
            txns.append({
                "date": (attr.get("createdAt") or "")[:10],
                "desc": attr.get("description", ""),
                "amount": cents(attr["amount"]["valueInBaseUnits"]),
                "category": cat.get("id") or "uncategorised",
                "status": attr.get("status", ""),
            })
        url = (body.get("links") or {}).get("next")
        params = None  # the "next" link already carries the query string
    return txns


def get_data_cached(state):
    """Return (accounts, transactions), refreshing from Up at most every CACHE_TTL_MIN."""
    now = dt.datetime.now(dt.timezone.utc)
    cache = state.get("cache")
    if cache:
        try:
            fetched = dt.datetime.fromisoformat(cache["fetched_at"])
            if (now - fetched).total_seconds() < CACHE_TTL_MIN * 60:
                return cache["accounts"], cache["transactions"]
        except (KeyError, ValueError):
            pass
    since = (now - dt.timedelta(days=TXN_WINDOW_DAYS)).isoformat()
    accounts = fetch_accounts()
    transactions = fetch_transactions(since)
    state["cache"] = {
        "fetched_at": now.isoformat(),
        "accounts": accounts,
        "transactions": transactions,
    }
    return accounts, transactions


# --------------------------------------------------------------------------
# Analysis helpers
# --------------------------------------------------------------------------
def summarise(transactions):
    spend_by_cat, total_in, total_out = {}, 0.0, 0.0
    for t in transactions:
        amt = t["amount"]
        if amt < 0:
            total_out += -amt
            spend_by_cat[t["category"]] = spend_by_cat.get(t["category"], 0.0) + (-amt)
        else:
            total_in += amt
    top = sorted(spend_by_cat.items(), key=lambda kv: kv[1], reverse=True)
    return total_in, total_out, top


def nice(cat):
    return cat.replace("-", " ")


# --------------------------------------------------------------------------
# Claude
# --------------------------------------------------------------------------
def ask_claude(question, history, accounts, transactions):
    total_in, total_out, by_cat = summarise(transactions)

    recent = sorted(transactions, key=lambda t: t["date"], reverse=True)[:MAX_TXNS_TO_MODEL]
    txn_lines = [f'{t["date"]} | {t["amount"]:+.2f} | {nice(t["category"])} | {t["desc"]}'
                 for t in recent]
    cat_lines = [f'{nice(c)}: ${v:.2f}' for c, v in by_cat]
    acct_lines = [f'{a["name"]} ({a["type"]}): ${a["balance"]:.2f}' for a in accounts]

    context = (
        f"Window: last {TXN_WINDOW_DAYS} days. All amounts in AUD. "
        f"Negative = money out, positive = money in.\n\n"
        f"ACCOUNTS:\n" + "\n".join(acct_lines) + "\n\n"
        f"TOTALS: in ${total_in:.2f}, out ${total_out:.2f}, "
        f"net ${total_in - total_out:+.2f}\n\n"
        f"SPENDING BY CATEGORY (money out only):\n" + "\n".join(cat_lines) + "\n\n"
        f"TRANSACTIONS (date | amount | category | description), newest first, "
        f"capped at {MAX_TXNS_TO_MODEL}:\n" + "\n".join(txn_lines)
    )

    system = (
        "You are a friendly, sharp personal finance assistant for an Up Bank "
        "(Australian) customer. Answer using ONLY the account data below and the "
        "conversation so far. Be concise and specific: cite real figures and "
        "category names from the data. Amounts are AUD. Write category names "
        "naturally. Treat follow-up questions as continuing the same thread "
        "(e.g. 'what about last month?' refers to the previous topic). If a "
        "question needs data outside the window below, say so plainly. Where "
        "useful, flag patterns, surprising spend, or a simple way to save. Keep "
        "replies short enough to read comfortably on a phone.\n\n"
        f"=== CURRENT ACCOUNT DATA ===\n{context}"
    )

    messages = history + [{"role": "user", "content": question}]
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 1024,
        "system": system,
        "messages": messages,
    }
    r = S.post(
        ANTHROPIC_URL,
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        data=json.dumps(payload),
        timeout=120,
    )
    r.raise_for_status()
    body = r.json()
    return "".join(b.get("text", "") for b in body.get("content", [])
                   if b.get("type") == "text").strip()


# --------------------------------------------------------------------------
# Message handling
# --------------------------------------------------------------------------
HELP = (
    "\U0001F44B I'm your Up money assistant. Ask me anything about your spending:\n"
    "\u2022 Where did my money go this month?\n"
    "\u2022 How much did I spend eating out in the last 2 weeks?\n"
    "\u2022 What are my biggest recurring costs?\n"
    "\u2022 Am I spending more on groceries lately?\n\n"
    "I remember our recent back-and-forth, so follow-ups work \u2014 ask "
    "\u201Cwhere did my money go this month?\u201D then just \u201Cwhat about last month?\u201D\n\n"
    "Commands:\n"
    "/balance \u2013 account balances\n"
    "/spending \u2013 quick category breakdown\n"
    "/reset \u2013 forget our conversation\n"
    "/help \u2013 this message"
)


def handle_message(msg, state):
    chat_id = msg["chat"]["id"]
    user_id = str(msg.get("from", {}).get("id", ""))
    text = (msg.get("text") or "").strip()

    if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
        send_message(chat_id, "Sorry, this is a private bot.")
        return
    if not text:
        return

    if text in ("/start", "/help"):
        send_message(chat_id, HELP)
        return

    if text.startswith("/reset"):
        state.get("chats", {}).pop(str(chat_id), None)
        send_message(chat_id, "Okay, I've cleared our conversation. Fresh start \U0001F44D")
        return

    accounts, transactions = get_data_cached(state)

    if text.startswith("/balance"):
        lines = [f'{a["name"]}: ${a["balance"]:.2f}' for a in accounts]
        total = sum(a["balance"] for a in accounts)
        send_message(chat_id, "\U0001F4B0 Balances:\n" + "\n".join(lines)
                     + f"\n\nTotal: ${total:.2f}")
        return

    if text.startswith("/spending"):
        total_in, total_out, by_cat = summarise(transactions)
        lines = [f"\u2022 {nice(c)}: ${v:.2f}" for c, v in by_cat[:12]]
        send_message(chat_id,
                     f"\U0001F4CA Last {TXN_WINDOW_DAYS} days \u2014 out ${total_out:.2f}, "
                     f"in ${total_in:.2f}\n\n" + "\n".join(lines))
        return

    # Anything else -> free-text question for Claude, with conversation memory.
    chats = state.setdefault("chats", {})
    history = chats.get(str(chat_id), [])

    tg("sendChatAction", chat_id=chat_id, action="typing")
    try:
        answer = ask_claude(text, history, accounts, transactions)
    except Exception as e:  # noqa: BLE001 - surface any failure to the user
        answer = f"Sorry, I hit an error answering that: {e}"
    send_message(chat_id, answer or "Hmm, I couldn't come up with an answer.")

    # Remember this turn so follow-up questions have context.
    history = history + [
        {"role": "user", "content": text},
        {"role": "assistant", "content": answer},
    ]
    chats[str(chat_id)] = history[-MAX_HISTORY:]


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
def main():
    state = load_state()
    offset = start_offset = state.get("offset")
    deadline = time.time() + RUN_SECONDS

    while True:
        try:
            updates = get_updates(offset)
        except requests.RequestException as e:
            print("getUpdates error:", e)
            if POLL_TIMEOUT <= 0:
                break              # drain mode: don't linger on errors
            time.sleep(3)
            if time.time() >= deadline:
                break
            continue

        if updates:
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if msg:
                    try:
                        handle_message(msg, state)
                    except Exception as e:  # noqa: BLE001
                        print("handler error:", e)
            state["offset"] = offset
            save_state(state)      # persist offset + any new chat memory after each batch
        elif POLL_TIMEOUT <= 0:
            break                  # drain mode: queue is empty, we're done

        if POLL_TIMEOUT > 0 and time.time() >= deadline:
            break                  # listen mode: time budget used up

    # Safety write only if something advanced but wasn't saved above (idle runs skip this).
    if offset != start_offset and state.get("offset") != offset:
        state["offset"] = offset
        save_state(state)


if __name__ == "__main__":
    main()
