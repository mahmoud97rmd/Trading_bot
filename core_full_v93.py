"""
Gold Scalper Bot — v9.3 Ultimate Full Core (Execution, Sync, Risk, Persistence & Gann Engine)
"""
import asyncio
import aiohttp
import os
import json
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
from metaapi_cloud_sdk import MetaApi

METAAPI_TOKEN = os.environ.get('METAAPI_TOKEN', 'YOUR_TOKEN')
ACCOUNT_ID    = os.environ.get('ACCOUNT_ID',    'YOUR_ID')
OANDA_TOKEN   = os.environ.get('OANDA_TOKEN',   'YOUR_OANDA')

SYMBOL_INFO = {'XAU_USD': {'pip_value': 0.1, 'contract_size': 100, 'prec': 2, 'name': 'Gold (USD)'}}
OANDA_BASE_URL = 'https://api-fxpractice.oanda.com/v3'  
_OANDA_GRAN = {'1m':'M1','5m':'M5','15m':'M15','30m':'M30','1h':'H1','4h':'H4'}

_http = None
_metaapi = None
_metaapi_account = None
_metaapi_conn = None

def get_http() -> aiohttp.ClientSession:
    global _http
    if _http is None or _http.closed:
        _http = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=20), timeout=aiohttp.ClientTimeout(total=30))
    return _http

def c_log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')} DAM] {msg}", flush=True)

# ─────────────────────────────────────────────────────────────
# PERSISTENCE LAYER (Amnesia Fix)
# ─────────────────────────────────────────────────────────────
bot_state = {
    'status': 'RUNNING', 'symbol': 'XAUUSDm',
    'live_daily_realized': 0.0, 'live_daily_date': None, 'live_daily_hit': False,
    'active_symbols': {'XAU_USD': True}, 'ui_selected_symbol': 'XAU_USD',
    'prot_daily_dd_usd': 200, 'prot_daily_profit_usd': 150,
    'prot_true_sync': True, 'prot_cost_be': True, 'prot_stale_filter': True,
    'prot_cycle_inval': True, 'prot_cycle_inval_pts': 200, 'gann_anchor_tf': '4h',
    'symbol_state': {'XAU_USD': {
        'gann_levels': [], 'gann_level_status': {}, 'gann_open_trades': {},
        'gann_close_used': None, 'gann_last_h1_time': None, 'gann_cycle_active': True,
        'auto_trade': True, 'lot_size': 0.05, 'gann_cycle_hours': 1,
        'gann_entry_mode': 'touch_trend', 'trend_filter_type': 'ema', 'trend_timeframe': '1h',
        'trend_vwap_period': 100, 'trend_ema_period': 60,
        'break_even_enabled': True, 'gann_be_trigger_points': 40, 'gann_touch_margin_pts': 5,
        'gann_tpsl_mode': 'fixed', 'gann_tp_points': 70, 'gann_sl_points': 110,
        'gann_atr_period': 14, 'gann_atr_sl_mult': 1.5, 'gann_atr_tp_mult': 2,
        'gann_monitor_tfs': {'1m': True, '5m': True}
    }}
}

def save_bot_persistence():
    try:
        data = {
            'live_daily_realized': bot_state.get('live_daily_realized', 0.0),
            'live_daily_date': str(bot_state.get('live_daily_date')),
            'live_daily_hit': bot_state.get('live_daily_hit', False),
            'gann_open_trades': {sym: bot_state['symbol_state'][sym]['gann_open_trades'] for sym in bot_state['active_symbols']}
        }
        with open('/root/tr/bot_persistence.json', 'w') as f: json.dump(data, f)
    except Exception as e: c_log(f"Persistence Save Error: {e}")

def load_bot_persistence():
    try:
        with open('/root/tr/bot_persistence.json', 'r') as f: data = json.load(f)
        bot_state['live_daily_realized'] = data.get('live_daily_realized', 0.0)
        bot_state['live_daily_hit'] = data.get('live_daily_hit', False)
        saved_date = data.get('live_daily_date')
        if saved_date and saved_date != 'None':
            bot_state['live_daily_date'] = datetime.strptime(saved_date, '%Y-%m-%d').date()
        for sym, trades in data.get('gann_open_trades', {}).items():
            if sym in bot_state['symbol_state']: bot_state['symbol_state'][sym]['gann_open_trades'] = trades
        c_log("✅ Bot State Restored Successfully from Persistence File.")
    except Exception as e: c_log(f"Persistence Load Notice (Starting fresh): {e}")

# ─────────────────────────────────────────────────────────────
# METAAPI INIT
# ─────────────────────────────────────────────────────────────
async def init_metaapi():
    global _metaapi, _metaapi_account, _metaapi_conn
    try:
        _metaapi = MetaApi(METAAPI_TOKEN)
        _metaapi_account = await _metaapi.metatrader_account_api.get_account(ACCOUNT_ID)
        _metaapi_conn = _metaapi_account.get_rpc_connection()
        await _metaapi_conn.connect()
        await _metaapi_conn.wait_synchronized()
    except Exception as e: c_log(f"MetaAPI Init Error: {e}")
    load_bot_persistence()

# ─────────────────────────────────────────────────────────────
# OANDA FETCHER (with exponential backoff)
# ─────────────────────────────────────────────────────────────
_oanda_sem = None
def _get_oanda_sem():
    global _oanda_sem
    if _oanda_sem is None: _oanda_sem = asyncio.Semaphore(3)
    return _oanda_sem

async def fetch_candles(symbol: str, granularity_str: str, count: int = 100) -> list:
    gran_str = _OANDA_GRAN.get(granularity_str, 'M1')
    headers = {'Authorization': f'Bearer {OANDA_TOKEN}'}
    url = f'{OANDA_BASE_URL}/instruments/{symbol}/candles'
    params = {'granularity': gran_str, 'count': count, 'price': 'M'}
    async with _get_oanda_sem():
        for attempt in range(3):
            try:
                async with get_http().get(url, headers=headers, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        complete = [c for c in data.get('candles', []) if c.get('complete', True)]
                        return [{'time': pd.Timestamp(c['time']).tz_convert('UTC'), 'close': float(c['mid']['c']), 'high': float(c['mid']['h']), 'low': float(c['mid']['l'])} for c in complete]
            except Exception: await asyncio.sleep(2 ** attempt)
    return []

# ─────────────────────────────────────────────────────────────
# GANN ENGINE & TPSL
# ─────────────────────────────────────────────────────────────
async def _gann_fetch_last_closed_anchor(symbol: str) -> dict | None:
    anchor_tf = bot_state.get('gann_anchor_tf', '1h')
    candles = await fetch_candles(symbol, anchor_tf, count=2)
    if not candles: return None
    return sorted(candles, key=lambda c: c['time'])[-1]

def _gann_tf_tp(symbol: str, tf: str) -> int: return bot_state['symbol_state'][symbol]['gann_tp_points']
def _gann_tf_sl(symbol: str, tf: str) -> int: return bot_state['symbol_state'][symbol]['gann_sl_points']

async def _gann_open_trade(symbol: str, is_buy: bool, level: dict, candles: list, reason: str, tf: str) -> None:
    sym_state = bot_state['symbol_state'][symbol]
    try:
        price = float(candles[-1]['close'])
        tp = price + 7.0 if is_buy else price - 7.0
        sl = price - 11.0 if is_buy else price + 11.0
        trade_id = f"sim_{int(datetime.now().timestamp())}_{tf}"
        is_real = sym_state.get('auto_trade', False)
        
        if is_real and _metaapi_conn:
            mt4_symbol = bot_state.get('symbol', symbol.replace('_', ''))
            if is_buy: res = await _metaapi_conn.create_market_buy_order(mt4_symbol, sym_state['lot_size'], stop_loss=sl, take_profit=tp)
            else: res = await _metaapi_conn.create_market_sell_order(mt4_symbol, sym_state['lot_size'], stop_loss=sl, take_profit=tp)
            trade_id = str(res.get('orderId', res.get('positionId', trade_id)))
            
        sym_state['gann_open_trades'][trade_id] = {
            'tf': tf, 'is_buy': is_buy, 'entry': price, 'is_real': is_real, 'sl': sl, 'tp': tp, 'be_activated': False
        }
        sym_state['gann_level_status'][level['key']] = 'used'
        save_bot_persistence()
    except Exception as e: c_log(f"Open trade failed: {e}")

# ─────────────────────────────────────────────────────────────
# EMERGENCY SERIAL CLOSURE (State-Machine Polling)
# ─────────────────────────────────────────────────────────────
async def _close_metaapi_trade(symbol: str, tid: str, sym_state: dict):
    if not _metaapi_conn: return
    try:
        await _metaapi_conn.close_position(tid)
        for _ in range(25): # Polling for confirmation
            positions = await _metaapi_conn.get_positions()
            if not any(str(p.get('id')) == str(tid) for p in positions):
                if tid in sym_state['gann_open_trades']:
                    del sym_state['gann_open_trades'][tid]
                    save_bot_persistence()
                return
            await asyncio.sleep(0.2)
    except Exception as e: c_log(f"Closure failed for {tid} : {e}")

# ─────────────────────────────────────────────────────────────
# LIVE SCANNER (Full Logic)
# ─────────────────────────────────────────────────────────────
async def gann_monitor_scanner() -> None:
    while True:
        try:
            if _metaapi_account and _metaapi_account.connection_status != 'CONNECTED':
                for attempt in range(5):
                    try:
                        await _metaapi_conn.connect()
                        await _metaapi_conn.wait_synchronized()
                        break
                    except Exception: await asyncio.sleep(2 ** attempt)

            now_dt = datetime.now(timezone.utc)
            today_date = now_dt.date()
            if bot_state.get('live_daily_date') != today_date:
                bot_state['live_daily_date'] = today_date
                bot_state['live_daily_realized'] = 0.0
                bot_state['live_daily_hit'] = False
                
            if bot_state.get('live_daily_hit'):
                await asyncio.sleep(60); continue
                
            total_floating = 0.0
            
            for symbol in ['XAU_USD']:
                sym_state = bot_state['symbol_state'][symbol]
                
                if bot_state.get('prot_cycle_inval', True) and sym_state.get('gann_close_used'):
                    mc = await fetch_candles(symbol, '1m', count=1)
                    if mc:
                        dist = abs(float(mc[-1]['close']) - sym_state['gann_close_used'])
                        if dist > bot_state.get('prot_cycle_inval_pts', 200) * SYMBOL_INFO[symbol]['pip_value']:
                            sym_state['gann_levels'] = []; sym_state['gann_close_used'] = None
                
                if sym_state['gann_open_trades']:
                    actual_positions = {}
                    sync_failed = False
                    if bot_state.get('prot_true_sync', True) and _metaapi_conn:
                        try:
                            positions = await _metaapi_conn.get_positions()
                            for p in positions: actual_positions[str(p.get('id'))] = p
                        except Exception: sync_failed = True
                            
                    if sync_failed: continue # DO NOT proceed with reconciliation (Amnesia Fix)

                    mc = await fetch_candles(symbol, '1m', count=2)
                    oanda_failed = False
                    if not mc: oanda_failed = True
                    else:
                        if bot_state.get('prot_stale_filter', True) and (now_dt - mc[-1]['time']).total_seconds() > 120:
                            oanda_failed = True
                    
                    live_px = None
                    if not oanda_failed: live_px = float(mc[-1]['close'])
                    
                    closed_ids = []
                    
                    # Pre-fetch history ONCE outside the loop (Rate Limit Fix)
                    history_deals_cache = None
                    missing_tids = [t for t, v in sym_state['gann_open_trades'].items() if v.get('is_real') and t not in actual_positions]
                    if missing_tids and _metaapi_conn:
                        try:
                            start_time = datetime.now(timezone.utc) - timedelta(days=2)
                            history_deals_cache = await _metaapi_conn.get_history_deals_by_time_range(start_time, datetime.now(timezone.utc))
                        except Exception: pass
                    
                    for tid, tr in list(sym_state['gann_open_trades'].items()):
                        is_buy = tr.get('is_buy'); tp = tr.get('tp'); sl = tr.get('sl'); entry = tr.get('entry')
                        
                        active_px = live_px
                        if active_px is None: # MT5 Fallback if OANDA drops
                            if tid in actual_positions: active_px = float(actual_positions[tid].get('currentPrice', entry))
                            else: active_px = entry 
                            
                        diff = (active_px - entry) if is_buy else (entry - active_px)
                        trade_pl = round(diff * sym_state['lot_size'] * SYMBOL_INFO[symbol]['contract_size'], 2)
                        
                        if tr.get('is_real') and bot_state.get('prot_true_sync') and _metaapi_conn:
                            if tid not in actual_positions:
                                exact_pnl = trade_pl 
                                if history_deals_cache is not None:
                                    for d in history_deals_cache:
                                        if str(d.get('positionId')) == str(tid) and d.get('entryType') == 'DEAL_ENTRY_OUT':
                                            exact_pnl = float(d.get('profit', 0)) + float(d.get('swap', 0)) + float(d.get('commission', 0))
                                closed_ids.append(tid)
                                bot_state['live_daily_realized'] += exact_pnl
                                continue
                            else:
                                trade_pl = actual_positions[tid].get('unrealizedProfit', trade_pl)
                        
                        outcome = None
                        if is_buy:
                            if active_px >= tp: outcome = 'WIN'
                            elif active_px <= sl: outcome = 'LOSS'
                        else:
                            if active_px <= tp: outcome = 'WIN'
                            elif active_px >= sl: outcome = 'LOSS'
                            
                        if bot_state.get('prot_cost_be', True) and sym_state.get('break_even_enabled') and not tr.get('be_activated'):
                            be_dist = sym_state.get('gann_be_trigger_points', 40) * SYMBOL_INFO[symbol]['pip_value']
                            if (is_buy and active_px >= entry + be_dist) or (not is_buy and active_px <= entry - be_dist):
                                be_margin = sym_state.get('gann_atr_period', 14) * 0.1 * SYMBOL_INFO[symbol]['pip_value']
                                net_be = (entry + be_margin) if is_buy else (entry - be_margin)
                                if tr.get('is_real') and _metaapi_conn:
                                    try:
                                        await _metaapi_conn.modify_position(tid, stop_loss=net_be)
                                        tr['sl'] = net_be; tr['be_activated'] = True # Set ONLY on success
                                        save_bot_persistence()
                                    except Exception: pass
                                else:
                                    tr['sl'] = net_be; tr['be_activated'] = True; save_bot_persistence()

                        if outcome:
                            closed_ids.append(tid)
                            bot_state['live_daily_realized'] += trade_pl
                        else:
                            total_floating += trade_pl
                            
                    for tid in closed_ids:
                        if tid in sym_state['gann_open_trades']:
                            del sym_state['gann_open_trades'][tid]
                            save_bot_persistence()

            total_daily = bot_state['live_daily_realized'] + total_floating
            dd_limit = -float(bot_state.get('prot_daily_dd_usd', 220))
            if dd_limit < 0 and total_daily <= dd_limit:
                bot_state['live_daily_hit'] = True
                for symbol in ['XAU_USD']:
                    sym_state = bot_state['symbol_state'][symbol]
                    for tid, tr in list(sym_state['gann_open_trades'].items()):
                        if tr.get('is_real') and _metaapi_conn:
                            await _close_metaapi_trade(symbol, tid, sym_state)
                        else:
                            del sym_state['gann_open_trades'][tid]
                            save_bot_persistence()
                continue
                
        except Exception: pass
        await asyncio.sleep(15)
