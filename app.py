import os
import json
import asyncio
import logging
import aiohttp
from aiohttp import web
from pyrogram import Client, idle
from pyrogram.handlers import MessageHandler
from pyrogram.types import Message
from pyrogram.errors import UserAlreadyParticipant, RPCError

# Enable standard Python & Pyrogram logging
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

for account_id, session_str in SESSIONS.items():
    clients[account_id] = Client(
        name=account_id,
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=session_str,
        in_memory=True
    )

# Inbound Message & Channel Post Handler
async def handle_incoming(c: Client, message: Message):
    # Extract sender info (Channel posts use sender_chat, user posts use from_user)
    if message.from_user:
        sender_id = str(message.from_user.id)
        sender_name = message.from_user.username or message.from_user.first_name or "User"
    elif message.sender_chat:
        sender_id = str(message.sender_chat.id)
        sender_name = message.sender_chat.title or "Channel"
    else:
        sender_id = None
        sender_name = "Anonymous/Channel"

    msg_text = message.text or message.caption or ""
    logger.info(f"[INCOMING] Account '{c.name}' caught channel post ID {message.id} from '{sender_name}' in Chat {message.chat.id}: '{msg_text}'")

    if TARGET_GROUP_CHAT_ID != 0 and message.chat.id != TARGET_GROUP_CHAT_ID:
        logger.info(f"[DEBUG] Ignored post from non-target chat: {message.chat.id}")
        return

    if not N8N_WEBHOOK_URL:
        logger.error("[ERROR] N8N_WEBHOOK_URL environment variable is empty.")
        return

    payload = {
        "telegram_msg_id": message.id,
        "sender_user_id": sender_id,
        "sender_username": sender_name,
        "text": msg_text,
        "reply_to_message_id": message.reply_to_message.id if message.reply_to_message else None,
        "chat_id": message.chat.id,
        "handled_by_account": c.name
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(N8N_WEBHOOK_URL, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                logger.info(f"[WEBHOOK] Forwarded channel post {message.id} to n8n (HTTP Status: {resp.status})")
    except Exception as e:
        logger.error(f"[ERROR] Failed to forward payload to n8n: {e}")

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
                logger.info(f"[TEST TRIGGER] Manual test sent to n8n. HTTP Status: {resp.status}")
                return web.json_response({"status": "success", "n8n_http_status": resp.status})
    except Exception as e:
        logger.error(f"[TEST TRIGGER ERROR] Failed to hit n8n: {e}")
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

    # Attach message handler to ALL clients
    for acc_id, client in clients.items():
        client.add_handler(MessageHandler(handle_incoming))

    # Start sessions and ensure channel subscription/peer caching
    for acc_id, client in clients.items():
        logger.info(f"Starting Pyrogram session for {acc_id}...")
        await client.start()

        if TARGET_GROUP_CHAT_ID != 0:
            try:
                # Attempt to join/subscribe to the channel to activate push updates
                await client.join_chat(TARGET_GROUP_CHAT_ID)
                logger.info(f"[{acc_id}] Joined/Subscribed to channel {TARGET_GROUP_CHAT_ID}")
            except UserAlreadyParticipant:
                logger.info(f"[{acc_id}] Already subscribed to channel {TARGET_GROUP_CHAT_ID}")
            except RPCError as e:
                logger.info(f"[{acc_id}] Peer caching lookup ({e})")

            try:
                await client.get_chat(TARGET_GROUP_CHAT_ID)
                logger.info(f"[{acc_id}] Channel peer cached successfully.")
            except Exception as e:
                logger.warning(f"[{acc_id}] Could not resolve peer for channel {TARGET_GROUP_CHAT_ID}: {e}")

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
