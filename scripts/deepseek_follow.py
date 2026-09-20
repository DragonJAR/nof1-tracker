#!/usr/bin/env python3
import os
import sys
import json
import time
import hmac
import hashlib
import urllib.parse
from typing import Dict, Any, List, Optional

try:
    import requests
except Exception:
    requests = None


# Config defaults aligned with src/config/constants.ts
INITIAL_MARKER_TIME_ISO = '2025-10-17T22:30:00.000Z'
HOUR_IN_MS = 1000 * 60 * 60


def parse_bool(value: Optional[str]) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


def get_current_last_hourly_marker(now_ms: Optional[int] = None) -> int:
    try:
        from datetime import datetime
        from dateutil import parser  # optional; if not installed, fallback below
        initial = parser.isoparse(INITIAL_MARKER_TIME_ISO)
        now = datetime.utcnow() if now_ms is None else datetime.utcfromtimestamp(now_ms / 1000)
        hours_since = int((now.timestamp() * 1000 - initial.timestamp() * 1000) // HOUR_IN_MS)
        return hours_since
    except Exception:
        # Fallback without dateutil
        from datetime import datetime
        # Manual parse of ISO assuming UTC 'Z'
        # '2025-10-17T22:30:00.000Z' -> year, month, day, hour, minute, second
        iso = INITIAL_MARKER_TIME_ISO.replace('Z', '')
        try:
            dt = datetime.strptime(iso, '%Y-%m-%dT%H:%M:%S.%f')
        except ValueError:
            dt = datetime.strptime(iso, '%Y-%m-%dT%H:%M:%S')
        now = datetime.utcnow() if now_ms is None else datetime.utcfromtimestamp(now_ms / 1000)
        diff_ms = int((now - dt).total_seconds() * 1000)
        return diff_ms // HOUR_IN_MS


def build_account_totals_url(base_url: str, marker: Optional[int] = None) -> str:
    if marker is None:
        marker = get_current_last_hourly_marker()
    return f"{base_url}/account-totals?lastHourlyMarker={marker}"


def within_tolerance(entry: Optional[float], current: Optional[float], tolerance_pct: float) -> bool:
    try:
        if not entry or not current or entry <= 0:
            return True
        diff_pct = abs((current - entry) / entry) * 100.0
        return diff_pct <= tolerance_pct
    except Exception:
        return True


def round_qty(symbol: str, qty: float) -> str:
    base = symbol.replace('USDT', '')
    if base in ('BTC', 'ETH'):
        return f"{qty:.3f}"
    if base in ('BNB', 'SOL'):
        return f"{qty:.2f}"
    if base in ('XRP', 'DOGE'):
        return f"{qty:.0f}"
    return f"{qty:.4f}"


def load_last_positions(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_last_positions(path: str, data: Dict[str, Any]) -> None:
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[WARN] Failed saving last positions: {e}")


def fetch_account_totals(nof1_base_url: str, marker: Optional[int] = None, timeout: int = 30_000) -> Dict[str, Any]:
    if requests is None:
        raise RuntimeError("requests library is required. Please `pip install requests`. ")
    url = build_account_totals_url(nof1_base_url, marker)
    resp = requests.get(url, timeout=timeout / 1000)
    resp.raise_for_status()
    return resp.json()


def select_latest_agent_entry(account_totals: List[Dict[str, Any]], agent_id: str) -> Optional[Dict[str, Any]]:
    entries = [a for a in account_totals if a.get('model_id') == agent_id]
    if not entries:
        return None
    entries.sort(key=lambda a: a.get('since_inception_hourly_marker', 0), reverse=True)
    return entries[0]


def build_follow_plans(previous_positions: Dict[str, Any], current_positions: Dict[str, Any], agent_id: str, price_tolerance: float) -> List[Dict[str, Any]]:
    plans = []
    symbols = set(previous_positions.keys()) | set(current_positions.keys())
    for sym in symbols:
        prev = previous_positions.get(sym)
        curr = current_positions.get(sym)
        prev_qty = abs(prev.get('quantity', 0)) if prev else 0
        curr_qty = abs(curr.get('quantity', 0)) if curr else 0

        # ENTER: new position appeared
        if not prev and curr and curr_qty > 0:
            side = 'BUY' if curr.get('quantity', 0) > 0 else 'SELL'
            exec_ok = within_tolerance(curr.get('entry_price'), curr.get('current_price'), price_tolerance)
            plans.append({
                'action': 'ENTER',
                'symbol': sym,
                'side': side,
                'type': 'MARKET',
                'quantity': curr_qty,
                'leverage': curr.get('leverage', 1),
                'entryPrice': curr.get('entry_price'),
                'currentPrice': curr.get('current_price'),
                'shouldExecute': exec_ok,
                'reason': f"New position opened by {agent_id} (OID: {curr.get('entry_oid')})",
                'entry_oid': curr.get('entry_oid'),
            })

        # EXIT: position closed
        elif prev and (not curr or curr_qty == 0) and prev_qty > 0:
            side = 'SELL' if prev.get('quantity', 0) > 0 else 'BUY'
            plans.append({
                'action': 'EXIT',
                'symbol': sym,
                'side': side,
                'type': 'MARKET',
                'quantity': prev_qty,
                'leverage': prev.get('leverage', 1),
                'exitPrice': curr.get('current_price') if curr else prev.get('entry_price'),
                'shouldExecute': True,
                'reason': f"Position closed by {agent_id}",
            })

    return plans


def place_binance_order(base_url: str, api_key: str, api_secret: str, symbol: str, side: str, quantity: float, recv_window: int = 5000) -> Dict[str, Any]:
    if requests is None:
        raise RuntimeError("requests library is required. Please `pip install requests`. ")

    ts = int(time.time() * 1000)
    new_order_resp_type = 'RESULT'
    type_ = 'MARKET'
    qty_str = round_qty(symbol, quantity)
    params = {
        'symbol': symbol,
        'side': side,
        'type': type_,
        'quantity': qty_str,
        'newOrderRespType': new_order_resp_type,
        'timestamp': str(ts),
        'recvWindow': str(recv_window),
    }
    query_string = urllib.parse.urlencode(params)
    signature = hmac.new(api_secret.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()
    url = f"{base_url}/fapi/v1/order?{query_string}&signature={signature}"

    headers = {
        'X-MBX-APIKEY': api_key,
    }
    resp = requests.post(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def send_telegram(token: str, chat_id: str, text: str) -> None:
    if requests is None:
        print("[WARN] requests missing; Telegram message not sent.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        'chat_id': chat_id,
        'text': text,
    }
    try:
        resp = requests.post(url, json=payload, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"[WARN] Telegram send failed: {e}")


def main_once() -> int:
    agent_id = os.environ.get('AGENT_ID', 'deepseek-chat-v3.1')
    nof1_base_url = os.environ.get('NOF1_API_BASE_URL', 'https://nof1.ai/api')
    binance_testnet = parse_bool(os.environ.get('BINANCE_TESTNET', 'true'))
    binance_api_key = os.environ.get('BINANCE_API_KEY', '')
    binance_api_secret = os.environ.get('BINANCE_API_SECRET', '')
    telegram_enabled = parse_bool(os.environ.get('TELEGRAM_ENABLED', 'false'))
    telegram_token = os.environ.get('TELEGRAM_API_TOKEN', '')
    telegram_chat_id = os.environ.get('TELEGRAM_CHAT_ID', '')
    price_tolerance = float(os.environ.get('PRICE_TOLERANCE', '1'))

    binance_base_url = 'https://testnet.binancefuture.com' if binance_testnet else 'https://fapi.binance.com'

    state_file = os.environ.get('DEESEEK_STATE_FILE', 'deepseek_last_positions.json')
    state = load_last_positions(state_file)
    previous_positions = state.get(agent_id, {})

    try:
        response = fetch_account_totals(nof1_base_url)
    except Exception as e:
        print(f"[ERROR] Failed fetching account totals: {e}")
        return 1

    account_totals = response.get('accountTotals', [])
    latest = select_latest_agent_entry(account_totals, agent_id)
    if not latest:
        print(f"[INFO] Agent {agent_id} not found or no data.")
        return 0

    current_positions = latest.get('positions', {})

    plans = build_follow_plans(previous_positions, current_positions, agent_id, price_tolerance)
    if not plans:
        print("[INFO] No actions required.")
        # Still update state to current
        state[agent_id] = current_positions
        save_last_positions(state_file, state)
        return 0

    for p in plans:
        if not p.get('shouldExecute'):
            print(f"[INFO] Skipping due to price tolerance: {p}")
            continue

        symbol = p['symbol']
        side = p['side']
        quantity = float(p['quantity'])

        try:
            result = place_binance_order(binance_base_url, binance_api_key, binance_api_secret, symbol, side, quantity)
            print(f"[SUCCESS] Executed order: {result}")

            if telegram_enabled and telegram_token and telegram_chat_id:
                text = f"✅ Trade Executed\n\n{side} {symbol}\nQuantity: {round_qty(symbol, quantity)}\nReason: {p.get('reason','')}"
                send_telegram(telegram_token, telegram_chat_id, text)
        except Exception as e:
            print(f"[ERROR] Failed placing order: {e}")

    # Update state after processing
    state[agent_id] = current_positions
    save_last_positions(state_file, state)
    return 0


def main() -> int:
    # Run a single cycle by default; use LOOP_INTERVAL seconds for continuous mode
    loop_interval = int(os.environ.get('LOOP_INTERVAL', '0'))
    if loop_interval <= 0:
        return main_once()
    else:
        print(f"[INFO] Running continuous mode with interval {loop_interval}s")
        while True:
            try:
                code = main_once()
                if code != 0:
                    print(f"[WARN] Cycle exited with code {code}")
            except KeyboardInterrupt:
                print("[INFO] Stopped by user.")
                return 0
            except Exception as e:
                print(f"[ERROR] Unexpected error: {e}")
            time.sleep(loop_interval)


if __name__ == '__main__':
    sys.exit(main())

