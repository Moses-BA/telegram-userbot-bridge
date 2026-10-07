import os
import json
import asyncio
import logging
import aiohttp
from aiohttp import web
from pyrogram import Client, idle
from pyrogram.handlers import MessageHandler, RawUpdateHandler
from pyrogram.types import Message
from pyrogram.raw.types import UpdateNewChannelMessage, UpdateEditChannelMessage
from pyrogram.errors import FloodWait, RPCError

# Enable standard Python logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("telegram-bridge")

# Load environment variables
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "")
TARGET_GROUP_CHAT_ID = int(os.getenv("TARGET_GROUP_CHAT_ID", "0"))
PORT = int(os.getenv("PORT", "8080"))

# Parse JSON session strings
SESSIONS_RAW = os.getenv("SESSIONS_JSON", "{}")
SESSIONS = json.loads(SESSIONS_RAW)

clients = {}
seen_message_ids = set()  # Prevents duplicate forwarding to n8n

for account_id, session_str in SESSIONS.items():
    clients[account_id] = Client(
        name=account_id,
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=session_str,
        in_memory=True
    )

async def dispatch_payload_to_n8n(message_id: int, sender_id: str, sender_name: str, text: str, chat_id: int, handler_acc: str, reply_to_id=None):
    if message_id in seen_message_ids:
        return
    seen_message_ids.add(message_id)

    # Keep memory bounded
    if len(seen_message_ids) > 1000:
        seen_message_ids.clear()

    logger.info(f"[CAPTURED] Post ID {message_id} from '{sender_name}' in Chat {chat_id}: '{text}'")

    if not N8N_WEBHOOK_URL:
        logger.error("[ERROR] N8N_WEBHOOK_URL environment variable is empty.")
        return

    payload = {
        "telegram_msg_id": message_id,
        "sender_user_id": sender_id,
        "sender_username": sender_name,
        "text": text,
        "reply_to_message_id": reply_to_id,
        "chat_id": chat_id,
        "handled_by_account": handler_acc
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(N8N_WEBHOOK_URL, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                logger.info(f"[WEBHOOK SUCCESS] Sent msg {message_id} to n8n (Status: {resp.status})")
    except Exception as e:
        logger.error(f"[ERROR] Failed to forward payload to n8n: {e}")

# High-Level Pyrogram Message Handler
async def handle_incoming(c: Client, message: Message):
    if TARGET_GROUP_CHAT_ID != 0 and message.chat.id != TARGET_GROUP_CHAT_ID:
        return

    if message.from_user:
        sender_id = str(message.from_user.id)
        sender_name = message.from_user.username or message.from_user.first_name or "User"
    elif message.sender_chat:
        sender_id = str(message.sender_chat.id)
        sender_name = message.sender_chat.title or "Channel"
    else:
        sender_id = None
        sender_name = "Anonymous/Channel"

    await dispatch_payload_to_n8n(
        message_id=message.id,
        sender_id=sender_id,
        sender_name=sender_name,
        text=message.text or message.caption or "",
        chat_id=message.chat.id,
        handler_acc=c.name,
        reply_to_id=message.reply_to_message.id if message.reply_to_message else None
    )

# Raw MTProto Update Handler
async def handle_raw_update(client: Client, update, users, chats):
    if isinstance(update, (UpdateNewChannelMessage, UpdateEditChannelMessage)):
        logger.info(f"[RAW MTPROTO] {client.name} detected raw channel update: {type(update).__name__}")

# Active Background Channel Poller (Production-Hardened)
async def channel_poller_task():
    logger.info("Starting active background channel polling loop...")
    primary_client = clients.get("ACC_01") or list(clients.values())[0]

    # Warm-up phase: Pre-populate seen_message_ids with recent history to prevent duplicate webhooks on app restart
    if TARGET_GROUP_CHAT_ID != 0:
        try:
            async for message in primary_client.get_chat_history(TARGET_GROUP_CHAT_ID, limit=10):
                seen_message_ids.add(message.id)
            logger.info(f"[POLLER WARM-UP] Pre-cached {len(seen_message_ids)} existing message IDs.")
        except Exception as e:
            logger.warning(f"[POLLER WARM-UP FAILED] {e}")

    while True:
        try:
            if TARGET_GROUP_CHAT_ID != 0:
                async for message in primary_client.get_chat_history(TARGET_GROUP_CHAT_ID, limit=5):
                    if message.id not in seen_message_ids:
                        if message.from_user:
                            sender_id = str(message.from_user.id)
                            sender_name = message.from_user.username or message.from_user.first_name or "User"
                        elif message.sender_chat:
                            sender_id = str(message.sender_chat.id)
                            sender_name = message.sender_chat.title or "Channel"
                        else:
                            sender_id = None
                            sender_name = "Channel/Admin"

                        await dispatch_payload_to_n8n(
                            message_id=message.id,
                            sender_id=sender_id,
                            sender_name=sender_name,
                            text=message.text or message.caption or "",
                            chat_id=message.chat.id,
                            handler_acc=f"{primary_client.name}_POLLER",
                            reply_to_id=message.reply_to_message.id if message.reply_to_message else None
                        )
        except FloodWait as e:
            logger.warning(f"[POLLER FLOODWAIT] Telegram rate-limit hit. Sleeping for {e.value} seconds.")
            await asyncio.sleep(e.value)
        except RPCError as e:
            logger.error(f"[POLLER RPC ERROR] {e}")
        except Exception as e:
            logger.error(f"[POLLER UNEXPECTED ERROR] {e}")

        await asyncio.sleep(3)  # Poll every 3 seconds

# Manual Test Trigger Endpoint
async def test_trigger_handler(request):
    if not N8N_WEBHOOK_URL:
        return web.json_response({"status": "error", "message": "N8N_WEBHOOK_URL is empty"}, status=400)
    
    dummy_payload = {
        "telegram_msg_id": 99999,
        "sender_user_id": "123456789",
        "sender_username": "channel_admin",
        "text": "Manual trigger test from Render bridge",
        "reply_to_message_id": None,
        "chat_id": TARGET_GROUP_CHAT_ID,
        "handled_by_account": "SYSTEM_TEST"
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(N8N_WEBHOOK_URL, json=dummy_payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                logger.info(f"[TEST TRIGGER] Manual test sent to n8n. Status: {resp.status}")
                return web.json_response({"status": "success", "n8n_http_status": resp.status})
    except Exception as e:
        logger.error(f"[TEST TRIGGER ERROR] {e}")
        return web.json_response({"status": "error", "message": str(e)}, status=500)

# Health Check Handler
async def health_check_handler(request):
    return web.json_response({
        "status": "ok",
        "service": "telegram-userbot-bridge",
        "loaded_accounts": list(clients.keys()),
        "target_group_id": TARGET_GROUP_CHAT_ID,
        "webhook_configured": bool(N8N_WEBHOOK_URL)
    })

# Outbound Reply Handler
async def send_message_handler(request):
    try:
        data = await request.json()
        account_id = data.get("account_id")
        text = data.get("text")
        reply_to_id = data.get("reply_to_id")

        client = clients.get(account_id)
        if not client:
            return web.json_response({"status": "error", "message": f"Account {account_id} not loaded"}, status=400)

        sent_msg = await client.send_message(
            chat_id=TARGET_GROUP_CHAT_ID,
            text=text,
            reply_to_message_id=reply_to_id
        )
        return web.json_response({
            "status": "success",
            "telegram_msg_id": sent_msg.id,
            "account_id": account_id
        })
    except Exception as e:
        logger.error(f"[ERROR] /send endpoint failed: {e}")
        return web.json_response({"status": "error", "message": str(e)}, status=500)

async def main():
    if not clients:
        logger.critical("[CRITICAL] No client sessions found in SESSIONS_JSON environment variable.")
        return

    # Attach message and raw update handlers to ALL clients
    for acc_id, client in clients.items():
        client.add_handler(MessageHandler(handle_incoming))
        client.add_handler(RawUpdateHandler(handle_raw_update))

    # Start sessions and cache peers
    for acc_id, client in clients.items():
        logger.info(f"Starting Pyrogram session for {acc_id}...")
        await client.start()

        if TARGET_GROUP_CHAT_ID != 0:
            try:
                cached = False
                async for dialog in client.get_dialogs(limit=100):
                    if dialog.chat.id == TARGET_GROUP_CHAT_ID:
                        logger.info(f"[{acc_id}] Channel '{dialog.chat.title}' found and cached!")
                        cached = True
                        break
                
                if not cached:
                    await client.get_chat(TARGET_GROUP_CHAT_ID)
                    logger.info(f"[{acc_id}] Channel peer cached via direct lookup.")
            except Exception as e:
                logger.warning(f"[{acc_id}] Warning: Could not resolve peer for channel {TARGET_GROUP_CHAT_ID}: {e}")

    # Start active background poller loop
    asyncio.create_task(channel_poller_task())

    app = web.Application()

    # Registered routes
    app.router.add_get("/", health_check_handler)
    app.router.add_get("/healthz", health_check_handler)
    app.router.add_get("/test-trigger", test_trigger_handler)
    app.router.add_post("/send", send_message_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    logger.info(f"Bridge active on port {PORT}...")
    await site.start()

    logger.info("Pyrogram active. Entering idle event loop...")
    await idle()

if __name__ == "__main__":
    asyncio.run(main())
