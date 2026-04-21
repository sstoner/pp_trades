import asyncio
import aiohttp
import os
import time
import json
import sqlite3

CONFIG_FILE = "config.json"

def load_config():
    if not os.path.exists(CONFIG_FILE):
        print(f"Error: {CONFIG_FILE} not found. Please copy config.json.example to config.json and fill it.")
        exit(1)
        
    with open(CONFIG_FILE, 'r') as f:
        return json.load(f)

config_data = load_config()

TELEGRAM_BOT_TOKEN = config_data.get("telegram", {}).get("bot_token")
TELEGRAM_CHAT_ID = config_data.get("telegram", {}).get("chat_id")

def parse_users(users_list):
    users_dict = {}
    for user in users_list:
        wallet = user.get("wallet")
        if not wallet:
            continue
            
        users_dict[wallet] = {
            "alias": user.get("alias"),
            "mode": user.get("mode", "whale"),
            "threshold": float(user.get("threshold", 0.0))
        }
    return users_dict

USERS = parse_users(config_data.get("users", []))

ACTIVITY_URL = "https://data-api.polymarket.com/activity"
POSITIONS_URL = "https://data-api.polymarket.com/positions"
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    print("Warning: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set in config")

if not USERS:
    print("Error: No users configured in config.json")
    exit(1)

DB_FILE = "trades.db"

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS seen_trades (
                tx_hash TEXT PRIMARY KEY,
                user_address TEXT,
                timestamp INTEGER
            )
        ''')
        conn.commit()

def is_trade_seen(tx_hash):
    if not tx_hash:
        return True
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT 1 FROM seen_trades WHERE tx_hash = ?', (tx_hash,))
        return cursor.fetchone() is not None

def save_trade(tx_hash, user_address, timestamp):
    if not tx_hash:
        return
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            'INSERT OR IGNORE INTO seen_trades (tx_hash, user_address, timestamp) VALUES (?, ?, ?)',
            (tx_hash, user_address, timestamp)
        )
        conn.commit()

async def fetch_recent_trades(session: aiohttp.ClientSession, user: str, limit: int = 5):
    params = {
        "limit": limit,
        "sortBy": "TIMESTAMP",
        "sortDirection": "DESC",
        "user": user,
        "type": "TRADE",
        "_t": int(time.time() * 1000)
    }
    headers = {
        "Cache-Control": "no-cache",
        "Pragma": "no-cache"
    }
    async with session.get(ACTIVITY_URL, params=params, headers=headers) as response:
        if response.status == 200:
            return await response.json()
        elif response.status == 429:
            print(f"[Rate Limit] /activity endpoint for {user}")
            return []
        else:
            print(f"[Error] Failed to fetch activity for {user}: {response.status}")
            return []

async def fetch_user_positions(session: aiohttp.ClientSession, user: str, market_id: str):
    params = {
        "sizeThreshold": 1,
        "limit": 100,
        "sortBy": "TOKENS",
        "sortDirection": "DESC",
        "market": market_id,
        "user": user,
        "_t": int(time.time() * 1000)
    }
    async with session.get(POSITIONS_URL, params=params) as response:
        if response.status == 200:
            return await response.json()
        elif response.status == 429:
            print(f"[Rate Limit] /positions endpoint for {user}")
            return []
        else:
            print(f"[Error] Failed to fetch positions for {user}: {response.status}")
            return []

async def send_telegram_message(session: aiohttp.ClientSession, text: str, enable_preview: bool = False):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram Mock]", text)
        return
    
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": not enable_preview
    }
    async with session.post(TELEGRAM_API_URL, json=payload) as response:
        if response.status != 200:
            print(f"Failed to send Telegram message: {await response.text()}")

def format_alert_message(trade, positions):
    # Trade format:
    # <title>
    # <name> <side> <outcome> <size> at <price>
    
    title = trade.get('title', 'Unknown Market')
    slug = trade.get('slug', '')
    icon = trade.get('icon', '')
    name = trade.get('name', 'Unknown User')
    side = trade.get('side', '')
    outcome = trade.get('outcome', '')
    
    try:
        size = float(trade.get('size', 0))
        price_cents = float(trade.get('price', 0)) * 100
    except (ValueError, TypeError):
        size = 0.0
        price_cents = 0.0
        
    msg = ""
    if icon:
        # Use an invisible zero-width space link to force Telegram to load the image preview
        msg += f"<a href=\"{icon}\">&#8203;</a>"
        
    if slug:
        market_url = f"https://polymarket.com/market/{slug}"
        msg += f"<b><a href=\"{market_url}\">{title}</a></b>\n"
    else:
        msg += f"<b>{title}</b>\n"
        
    try:
        usdc_size = float(trade.get('usdcSize', 0))
    except (ValueError, TypeError):
        usdc_size = size * (price_cents / 100.0)
        
    msg += f"\n{name} <b>{side}</b> <b>{outcome}</b> <b>{size:.1f}</b> at <b>{price_cents:.2f}¢</b> (<b>${usdc_size:.2f}</b>)\n"
    
    if positions:
        msg += "\n<b>Current Positions:</b>\n"
        for pos in positions:
            pos_outcome = pos.get('outcome', 'Unknown')
            try:
                pos_size = float(pos.get('size', 0))
                avg_price_cents = float(pos.get('avgPrice', 0)) * 100
            except (ValueError, TypeError):
                pos_size = 0.0
                avg_price_cents = 0.0
            msg += f"<b>{pos_outcome}</b>: <b>{pos_size:.1f}</b> (avg: <b>{avg_price_cents:.2f}¢</b>)\n"
    else:
        msg += "\n<i>No current positions > 1</i>\n"

    # Return message text and whether we want to enable the web page preview
    return msg, bool(icon)

pending_alerts = {}
processing_txs = set()
AGGREGATION_DELAY = 3.0  # Wait for 3 seconds of quiet to group trades

async def flush_pending_alerts(session):
    current_time = time.time()
    keys_to_flush = []
    for key, data in pending_alerts.items():
        if current_time - data["last_update"] >= AGGREGATION_DELAY:
            keys_to_flush.append(key)
            
    for key in keys_to_flush:
        data = pending_alerts.pop(key)
        trade = data["base_trade"]
        tx_hashes = data["tx_hashes"]
        user_wallet = data["user_wallet"]
        
        config = USERS.get(user_wallet, {"alias": None, "mode": "whale", "threshold": 0.0})
        
        # Check threshold for whales
        if config["mode"] == "whale" and data["total_usdc"] < config["threshold"]:
            for tx_hash in tx_hashes:
                save_trade(tx_hash, user_wallet, data["timestamp"])
                processing_txs.discard(tx_hash)
            continue
            
        trade['size'] = data["total_size"]
        trade['usdcSize'] = data["total_usdc"]
        
        market_id = key[1]
        
        positions = []
        if market_id:
            positions = await fetch_user_positions(session, user_wallet, market_id)
            
        alert_msg, has_icon = format_alert_message(trade, positions)
        print(f"[{time.strftime('%X')}] Sending aggregated trade(s) for {user_wallet}: {trade.get('title')}")
        await send_telegram_message(session, alert_msg, enable_preview=has_icon)
        
        for tx_hash in tx_hashes:
            save_trade(tx_hash, user_wallet, data["timestamp"])
            processing_txs.discard(tx_hash)

async def monitor_user(session: aiohttp.ClientSession, user_wallet: str, config: dict):
    try:
        trades = await fetch_recent_trades(session, user_wallet, limit=10)
        
        # Filter new trades
        new_trades = []
        for trade in trades:
            tx_hash = trade.get('transactionHash')
            if not tx_hash or tx_hash in processing_txs or is_trade_seen(tx_hash):
                continue
            new_trades.append(trade)
                
        if not new_trades:
            return
            
        # Process oldest to newest
        for trade in reversed(new_trades):
            tx_hash = trade.get('transactionHash')
            processing_txs.add(tx_hash)
            
            user_alias = config.get("alias")
            
            # If self mode, process immediately without aggregation
            if config.get("mode") == "self":
                if user_alias:
                    trade['name'] = user_alias
                    
                market_id = trade.get('conditionId')
                positions = []
                if market_id:
                    positions = await fetch_user_positions(session, user_wallet, market_id)
                    
                alert_msg, has_icon = format_alert_message(trade, positions)
                print(f"[{time.strftime('%X')}] Immediate trade detected for {user_wallet}: {trade.get('title')}")
                await send_telegram_message(session, alert_msg, enable_preview=has_icon)
                
                save_trade(tx_hash, user_wallet, trade.get('timestamp', 0))
                processing_txs.discard(tx_hash)
                continue
            
            # Whale mode: group new trades by: user, market_id, side, outcome, price into pending_alerts
            market_id = trade.get('conditionId')
            side = trade.get('side', '')
            outcome = trade.get('outcome', '')
            try:
                price = float(trade.get('price', 0))
            except (ValueError, TypeError):
                price = 0.0
                
            try:
                usdc_val = float(trade.get('usdcSize'))
            except (ValueError, TypeError):
                usdc_val = float(trade.get('size', 0)) * price
                
            group_key = (user_wallet, market_id, side, outcome, price)
            
            if group_key not in pending_alerts:
                if user_alias:
                    trade['name'] = user_alias
                pending_alerts[group_key] = {
                    "base_trade": trade.copy(),
                    "tx_hashes": [],
                    "total_size": 0.0,
                    "total_usdc": 0.0,
                    "timestamp": trade.get('timestamp', 0),
                    "user_wallet": user_wallet,
                    "last_update": time.time()
                }
            
            pending_alerts[group_key]["tx_hashes"].append(tx_hash)
            try:
                pending_alerts[group_key]["total_size"] += float(trade.get('size', 0))
            except (ValueError, TypeError):
                pass
            pending_alerts[group_key]["total_usdc"] += usdc_val
            pending_alerts[group_key]["last_update"] = time.time()
            
    except Exception as e:
        print(f"Error monitoring {user_wallet}: {e}")

async def main():
    init_db()
    print(f"Starting Polymarket Trade Bot... Monitoring {len(USERS)} users.")
    connector = aiohttp.TCPConnector(limit=10)
    timeout = aiohttp.ClientTimeout(total=3)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        # Initialize silently on start to ignore old trades if they aren't in DB
        for user_wallet in USERS.keys():
            trades = await fetch_recent_trades(session, user_wallet, limit=5)
            for trade in trades:
                tx_hash = trade.get('transactionHash')
                if tx_hash and not is_trade_seen(tx_hash):
                    save_trade(tx_hash, user_wallet, trade.get('timestamp', 0))
            
        while True:
            start_time = time.time()
            
            # Monitor each user
            tasks = [monitor_user(session, user_wallet, config) for user_wallet, config in USERS.items()]
            await asyncio.gather(*tasks)
            
            # Check for settled trade groups
            await flush_pending_alerts(session)
            
            # Rate limit constraint / Loop frequency
            elapsed = time.time() - start_time
            sleep_time = max(1.0 - elapsed, 0.1) # Minimum 1s frequency
            
            await asyncio.sleep(sleep_time)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user.")
