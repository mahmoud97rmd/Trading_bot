from aiohttp import web
import asyncio
import aiohttp
import pandas as pd
import numpy as np
import csv
import os
from datetime import datetime, timedelta, timezone
from metaapi_cloud_sdk import MetaApi

# --- METAAPI & OANDA CONFIGURATION ---
METAAPI_TOKEN = 'eyJhbGciOiJSUzUxMiIsInR5cCI6IkpXVCJ9.eyJfaWQiOiJjM2M1MWFlYjY3N2MwNzlkMmUzOTA3YjAzYmYzNzc4YiIsImFjY2Vzc1J1bGVzIjpbeyJpZCI6InRyYWRpbmctYWNjb3VudC1tYW5hZ2VtZW50LWFwaSIsIm1ldGhvZHMiOlsidHJhZGluZy1hY2NvdW50LW1hbmFnZW1lbnQtYXBpOnJlc3Q6cHVibGljOio6KiJdLCJyb2xlcyI6WyJyZWFkZXIiLCJ3cml0ZXIiXSwicmVzb3VyY2VzIjpbIio6JFVTRVJfSUQkOioiXX0seyJpZCI6Im1ldGFhcGktcmVzdC1hcGkiLCJtZXRob2RzIjpbIm1ldGFhcGktYXBpOnJlc3Q6cHVibGljOio6KiJdLCJyb2xlcyI6WyJyZWFkZXIiLCJ3cml0ZXIiXSwicmVzb3VyY2VzIjpbIio6JFVTRVJfSUQkOioiXX0seyJpZCI6Im1ldGFhcGktcnBjLWFwaSIsIm1ldGhvZHMiOlsibWV0YWFwaS1hcGk6d3M6cHVibGljOio6KiJdLCJyb2xlcyI6WyJyZWFkZXIiLCJ3cml0ZXIiXSwicmVzb3VyY2VzIjpbIio6JFVTRVJfSUQkOioiXX0seyJpZCI6Im1ldGFhcGktcmVhbC10aW1lLXN0cmVhbWluZy1hcGkiLCJtZXRob2RzIjpbIm1ldGFhcGktYXBpOndzOnB1YmxpYzoqOioiXSwicm9sZXMiOlsicmVhZGVyIiwid3JpdGVyIl0sInJlc291cmNlcyI6WyIqOiRVU0VSX0lEJDoqIl19LHsiaWQiOiJtZXRhc3RhdHMtYXBpIiwibWV0aG9kcyI6WyJtZXRhc3RhdHMtYXBpOnJlc3Q6cHVibGljOio6KiJdLCJyb2xlcyI6WyJyZWFkZXIiLCJ3cml0ZXIiXSwicmVzb3VyY2VzIjpbIio6JFVTRVJfSUQkOioiXX0seyJpZCI6InJpc2stbWFuYWdlbWVudC1hcGkiLCJtZXRob2RzIjpbInJpc2stbWFuYWdlbWVudC1hcGk6cmVzdDpwdWJsaWM6KjoqIl0sInJvbGVzIjpbInJlYWRlciIsIndyaXRlciJdLCJyZXNvdXJjZXMiOlsiKjokVVNFUl9JRCQ6KiJdfSx7ImlkIjoiY29weWZhY3RvcnktYXBpIiwibWV0aG9kcyI6WyJjb3B5ZmFjdG9yeS1hcGk6cmVzdDpwdWJsaWM6KjoqIl0sInJvbGVzIjpbInJlYWRlciIsIndyaXRlciJdLCJyZXNvdXJjZXMiOlsiKjokVVNFUl9JRCQ6KiJdfSx7ImlkIjoibXQtbWFuYWdlci1hcGkiLCJtZXRob2RzIjpbIm10LW1hbmFnZXItYXBpOnJlc3Q6ZGVhbGluZzoqOioiLCJtdC1tYW5hZ2VyLWFwaTpyZXN0OnB1YmxpYzoqOioiXSwicm9sZXMiOlsicmVhZGVyIiwid3JpdGVyIl0sInJlc291cmNlcyI6WyIqOiRVU0VSX0lEJDoqIl19LHsiaWQiOiJiaWxsaW5nLWFwaSIsIm1ldGhvZHMiOlsiYmlsbGluZy1hcGk6cmVzdDpwdWJsaWM6KjoqIl0sInJvbGVzIjpbInJlYWRlciJdLCJyZXNvdXJjZXMiOlsiKjokVVNFUl9JRCQ6KiJdfV0sImlnbm9yZVJhdGVMaW1pdHMiOmZhbHNlLCJ0b2tlbklkIjoiMjAyMTAyMTMiLCJpbXBlcnNvbmF0ZWQiOmZhbHNlLCJyZWFsVXNlcklkIjoiYzNjNTFhZWI2NzdjMDc5ZDJlMzkwN2IwM2JmMzc3OGIiLCJpYXQiOjE3Nzg3NDY0MzgsImV4cCI6MTc4NjUyMjQzOH0.NRMo-BO9ezZBEb4XmCQzkMsRN1iAz1rVSk7XWFP-ZGS_AZEyxSfIjnJ5w-r4egazV7tnxNLjjMuAdUb25T3ur3XWKCL4Jo9LFPy9tZzhIMRtlhq8d6YAHK9uxJclqJv5BZQFDeMeiFtyalLNjaE100Lp2zEnGWwlloxF-dpCw5DXvVKeGfMyVx4L2kisshcysDo7OeMkDBU1UB7leHi2eviEl7XQCpmhxdzT4BwMkf8YERx2jouKVu8-koVy00aon0drktGBSlQDOFw2WV0hg-VUfeCBR_Hgw2czqKVJ_lj_ZN3EsjWirirpiuXWbtwdD-VPokjKtX1z3ugcSTS1nd2iFIzauUHdOfb7Jl0R6cm8FosVS-4Iu046DiMsrxiAJ4PBywOXQhsFzZiePqmil1w5HHCxrw_78HNR9XcjBETMpHx9W48llIeUOkBVbsKfBP5iYtGSjS52i0QgpvHkfKrtXfbkMT0_9yJFG2kfZJHwJ5BJzWT4aKXto3l6iGe45xe4ZJhYhZX_RkC6dxR2w84M-uY-wlqiv_sxjHNOguSyOx4lfaeoq5H-LuJiWpHAYxEJUQWoQAQ7PObZOXCDWLRc_vP2gcbv1qYxTjD54FHnqhyf-oTGzAkWG5CVQFKpp9jTHQ3pXEYTSgIUTfHDbtoesAY1HG3nHcHbwujnqo0'
ACCOUNT_ID = '7d54fa6f-eaf7-4637-92a1-e0356ee729f8'
TG_TOKEN = '8779425898:AAFDMBTe0eIUin25rz809CfuINU4pmmVs-M'
OANDA_ID = '101-004-28533521-003'
OANDA_API = 'c0f5b5df69c77e8bf35dcfd2fbde72da-a4c6cbadba7ae39d21143f65e2c2b8ba'
OANDA_URL = 'https://api-fxpractice.oanda.com/v3'

# --- LOGGING HELPER ---
def c_log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

# --- GLOBAL STATE (FACTORY DEFAULTS) ---
bot_state = {
    'status': 'RUNNING', 'strategy': 'NEW', 'symbol': 'XAUUSD@',
    'live_connected': False, 
    
    'timeframes': ['1m', '2m', '3m', '5m', '15m'], 
    'active_tfs': {'1m': False, '2m': False, '3m': False, '5m': True, '15m': False},
    'lot_size': 0.05, 'pip_value': 0.1, 'spread_pips': 2.2, 'chat_id': None, 'last_update_id': 0,
    'tp_pips': {'1m': 25, '2m': 30, '3m': 40, '5m': 70, '15m': 80}, 
    'sl_pips': {'1m': 100, '2m': 100, '3m': 100, '5m': 100, '15m': 150},
    
    'use_time_filter': False,
    'use_f_cons': True, 'cons_count': 3, 
    'use_f_gap': False, 'gap_pips': 5.0, 
    'use_f_mtf': False,
    
    'use_be': False,            
    'use_atr': False,           
    'use_max_spread': True,    
    'max_spread_pips': 3.0,
    'atr_mult_tp': 1.5,
    'atr_mult_sl': 3.0,
    
    'use_s5_ticks': False,     
    'tp_tolerance_pips': 5.0,  

    'market_data': {tf: "⏸ بانتظار الاتصال (Offline)" for tf in ['1m', '2m', '3m', '5m', '15m']},
    'last_signal_time': {tf: None for tf in ['1m', '2m', '3m', '5m', '15m']},
    'connection_obj': None, 'account_obj': None, 'is_backtesting': False
}

def get_htf(tf): 
    if tf in ['1m', '2m']: return '5m'
    if tf in ['3m', '5m']: return '15m'
    return '1h'

async def fetch_oanda_candles(instrument, granularity, count=5000, end_time=None):
    tf_map = {'s5': 'S5', '1m': 'M1', '2m': 'M2', '3m': 'M3', '5m': 'M5', '15m': 'M15', '1h': 'H1'}
    url = f"{OANDA_URL}/instruments/{instrument}/candles"
    headers = {"Authorization": f"Bearer {OANDA_API}"}
    params = {"granularity": tf_map.get(granularity, 'M5'), "count": count, "price": "M"}
    if end_time: params["to"] = end_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    candles = []
                    for c in data.get('candles', []):
                        if c['complete']:
                            candles.append({
                                'time': pd.to_datetime(c['time']),
                                'open': float(c['mid']['o']), 'high': float(c['mid']['h']),
                                'low': float(c['mid']['l']), 'close': float(c['mid']['c'])
                            })
                    return candles
                else:
                    c_log(f"⚠️ تحذير: خادم Oanda رد بكود {resp.status}")
        except Exception as e: 
            c_log(f"❌ خطأ في جلب بيانات Oanda: {e}")
        return []

# --- TELEGRAM HELPER FUNCTIONS ---
async def send_tg_msg(text, reply_markup=None):
    if not bot_state['chat_id']: return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {'chat_id': bot_state['chat_id'], 'text': text, 'parse_mode': 'HTML'}
    if reply_markup: payload['reply_markup'] = reply_markup
    async with aiohttp.ClientSession() as session:
        try: await session.post(url, json=payload)
        except Exception as e: c_log(f"❌ خطأ إرسال لتلغرام: {e}")

async def edit_tg_msg(chat_id, message_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/editMessageText"
    payload = {'chat_id': chat_id, 'message_id': message_id, 'text': text, 'parse_mode': 'HTML'}
    if reply_markup: payload['reply_markup'] = reply_markup
    async with aiohttp.ClientSession() as session:
        try: await session.post(url, json=payload)
        except: pass

async def answer_callback(callback_query_id, text=None):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/answerCallbackQuery"
    payload = {'callback_query_id': callback_query_id}
    if text: payload['text'] = text
    async with aiohttp.ClientSession() as session:
        try: await session.post(url, json=payload)
        except: pass

async def send_tg_document(file_path, caption):
    if not bot_state['chat_id']: return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument"
    async with aiohttp.ClientSession() as session:
        try:
            with open(file_path, 'rb') as f:
                data = aiohttp.FormData()
                data.add_field('chat_id', str(bot_state['chat_id']))
                data.add_field('document', f)
                data.add_field('caption', caption)
                await session.post(url, data=data)
        except Exception as e: c_log(f"❌ خطأ إرسال الملف: {e}")

def calculate_indicators(df):
    if df.empty: return df
    df['ema15'] = df['close'].ewm(span=15, adjust=False).mean()
    df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()
    df['ema150'] = df['close'].ewm(span=150, adjust=False).mean()
    
    low_min = df['low'].rolling(window=5).min()
    high_max = df['high'].rolling(window=5).max()
    denom = (high_max - low_min).replace(0, 1e-10)
    df['k_raw'] = 100 * ((df['close'] - low_min) / denom)
    df['K'] = df['k_raw'].ewm(span=5, adjust=False).mean()
    df['D'] = df['K'].ewm(span=5, adjust=False).mean()
    
    df['tr0'] = abs(df['high'] - df['low'])
    df['tr1'] = abs(df['high'] - df['close'].shift())
    df['tr2'] = abs(df['low'] - df['close'].shift())
    df['tr'] = df[['tr0', 'tr1', 'tr2']].max(axis=1)
    df['atr'] = df['tr'].rolling(14).mean().bfill()
    return df

def check_filters(df, i, df_htf=None):
    curr = df.loc[i]
    b_ema, s_ema = True, True
    cons = bot_state['cons_count'] if bot_state['use_f_cons'] else 1
    if i < cons: return False, False
    
    for j in range(cons):
        c = df.loc[i-j]
        if not (c['ema15'] > c['ema50'] > c['ema150']): b_ema = False
        if not (c['ema150'] > c['ema50'] > c['ema15']): s_ema = False

    if bot_state['use_f_gap']:
        gap_val = bot_state['gap_pips'] * bot_state['pip_value']
        if b_ema:
            if (curr['ema15'] - curr['ema50'] < gap_val) or (curr['ema50'] - curr['ema150'] < gap_val): b_ema = False
        if s_ema:
            if (curr['ema50'] - curr['ema15'] < gap_val) or (curr['ema150'] - curr['ema50'] < gap_val): s_ema = False

    if bot_state['use_f_mtf'] and df_htf is not None and not df_htf.empty:
        htf_candle = df_htf[df_htf['time'] <= curr['time']]
        if not htf_candle.empty:
            hc = htf_candle.iloc[-1]
            if b_ema and not (hc['ema15'] > hc['ema50'] > hc['ema150']): b_ema = False
            if s_ema and not (hc['ema150'] > hc['ema50'] > hc['ema15']): s_ema = False
    return b_ema, s_ema

# --- UI MENUS ---
def get_main_keyboard():
    live_icon = "🟢 متصل" if bot_state['live_connected'] else "🔴 غير متصل"
    time_icon, status_icon = "🟢" if bot_state['use_time_filter'] else "🔴", "🟢 RUN" if bot_state['status'] == 'RUNNING' else "🔴 PAUSE"
    return {"inline_keyboard": [
        [{"text": f"🔌 سيرفر التداول الحي: {live_icon}", "callback_data": "toggle_live_conn"}],
        [{"text": f"Status: {status_icon}", "callback_data": "toggle_status"}, {"text": f"Strategy: {bot_state['strategy']}", "callback_data": "toggle_strat"}],
        [{"text": f"Time Filter (08-18): {time_icon}", "callback_data": "toggle_time"}],
        [{"text": "🎛 فلاتر التوازي", "callback_data": "menu_filters"}, {"text": "⏱ فريمات", "callback_data": "menu_tfs"}],
        [{"text": "📊 Live Report", "callback_data": "report"}, {"text": "💳 Account", "callback_data": "account"}],
        [{"text": "🛠 SETTINGS", "callback_data": "menu_settings"}, {"text": "🔬 BACKTEST", "callback_data": "menu_backtest"}],
        [{"text": "🛑 CLOSE ALL", "callback_data": "close_all"}]
    ]}

def get_filters_keyboard():
    return {"inline_keyboard": [
        [{"text": f"1. ثبات ({bot_state['cons_count']}): {'🟢' if bot_state['use_f_cons'] else '🔴'}", "callback_data": "toggle_f_cons"}],
        [{"text": f"2. فجوة الموفينغات: {'🟢' if bot_state['use_f_gap'] else '🔴'}", "callback_data": "toggle_f_gap"}],
        [{"text": f"3. فلتر الفريم الأكبر: {'🟢' if bot_state['use_f_mtf'] else '🔴'}", "callback_data": "toggle_f_mtf"}],
        [{"text": "🔙 رجوع", "callback_data": "menu_main"}]
    ]}

def get_tf_keyboard():
    kb = []
    row = []
    for tf in bot_state['timeframes']:
        row.append({"text": f"{tf}: {'🟢' if bot_state['active_tfs'][tf] else '🔴'}", "callback_data": f"toggle_tf_{tf}"})
        if len(row) == 2: kb.append(row); row = []
    if row: kb.append(row)
    kb.append([{"text": "🔙 Main Menu", "callback_data": "menu_main"}])
    return {"inline_keyboard": kb}

def get_settings_keyboard():
    be_i = "🟢" if bot_state['use_be'] else "🔴"
    atr_i = "🟢" if bot_state['use_atr'] else "🔴"
    spr_i = "🟢" if bot_state['use_max_spread'] else "🔴"
    s5_i = "🟢" if bot_state['use_s5_ticks'] else "🔴"
    return {"inline_keyboard": [
        [{"text": f"تأمين الدخول (BE 20p): {be_i}", "callback_data": "toggle_be"}],
        [{"text": f"أهداف ATR: {atr_i}", "callback_data": "toggle_atr"}],
        [{"text": f"حماية السبريد: {spr_i}", "callback_data": "toggle_spread"}],
        [{"text": f"تيكات S5 (Backtest): {s5_i}", "callback_data": "toggle_s5"}],
        [{"text": f"LOT SIZE: {bot_state['lot_size']}", "callback_data": "noop"}],
        [{"text": "➕ Lot", "callback_data": "inc_lot"}, {"text": "➖ Lot", "callback_data": "dec_lot"}],
        [{"text": "📖 View TP/SL", "callback_data": "view_tpsl"}],
        [{"text": "🔙 Main Menu", "callback_data": "menu_main"}]
    ]}

# --- 🚀 OANDA BACKTEST ENGINE 🚀 ---
async def run_oanda_backtest(start_dt, mode='candle'):
    if bot_state['is_backtesting']:
        await send_tg_msg("⚠️ يوجد باك تيست قيد المعالجة حالياً.")
        return
        
    bot_state['is_backtesting'] = True
    c_log("بدء عملية الباك تيست...")
    csv_filename = f"BT_Oanda_{datetime.now().strftime('%H%M%S')}.csv"
    trade_logs = []
    total_prof, peak_equity, max_dd = 0.0, 0.0, 0.0
    be_count = 0
    
    await send_tg_msg(f"⏳ <b>بدء الباك تيست</b>\nمن: {start_dt.strftime('%Y-%m-%d')}")
    
    try:
        for tf in bot_state['timeframes']:
            if not bot_state['active_tfs'][tf]: continue
            
            c_log(f"[{tf}] جلب بيانات الباك تيست...")
            c_data = await fetch_oanda_candles('XAU_USD', tf, 5000)
            if len(c_data) < 150: 
                c_log(f"[{tf}] بيانات غير كافية للباك تيست.")
                continue
            df = calculate_indicators(pd.DataFrame(c_data).sort_values(by='time').reset_index(drop=True))
            
            df_htf = pd.DataFrame()
            if bot_state['use_f_mtf']:
                htf_data = await fetch_oanda_candles('XAU_USD', get_htf(tf), 2000)
                if htf_data: df_htf = calculate_indicators(pd.DataFrame(htf_data).sort_values(by='time').reset_index(drop=True))
            
            for i in df[df['time'] >= start_dt].index:
                curr, prev = df.loc[i], df.loc[i-1]
                if bot_state['use_time_filter'] and not (8 <= curr['time'].hour <= 17): continue
                
                b_ema, s_ema = check_filters(df, i, df_htf)
                buy_sig = b_ema and prev['K'] <= 20 and curr['K'] > prev['K'] if bot_state['strategy'] == 'NEW' else b_ema and 5 <= curr['K'] <= 20 and prev['K'] <= prev['D'] and curr['K'] > curr['D']
                sell_sig = s_ema and prev['K'] >= 80 and curr['K'] < prev['K'] if bot_state['strategy'] == 'NEW' else s_ema and 80 <= curr['K'] <= 100 and prev['K'] >= prev['D'] and curr['K'] < curr['D']
                
                if buy_sig or sell_sig:
                    entry_p, entry_t = curr['close'], curr['time']
                    m = 1 if buy_sig else -1
                    act_ent = entry_p + (m * bot_state['spread_pips'] * bot_state['pip_value'])
                    
                    if bot_state['use_atr']:
                        tp_dist = curr['atr'] * bot_state['atr_mult_tp']
                        sl_dist = curr['atr'] * bot_state['atr_mult_sl']
                    else:
                        tp_dist = bot_state['tp_pips'][tf] * bot_state['pip_value']
                        sl_dist = bot_state['sl_pips'][tf] * bot_state['pip_value']
                        
                    tp_p = round(act_ent + (m * tp_dist), 2)
                    sl_p = round(act_ent - (m * sl_dist), 2)
                    
                    tol_val = bot_state['tp_tolerance_pips'] * bot_state['pip_value']
                    eff_tp_p = (tp_p - tol_val) if buy_sig else (tp_p + tol_val)
                    
                    max_exit_time = min(entry_t + timedelta(hours=72), datetime.now(timezone.utc))
                    outcome, exit_t = "EXPIRED", max_exit_time
                    
                    mode_str = 's5' if bot_state['use_s5_ticks'] else '1m'
                    val_candles = await fetch_oanda_candles('XAU_USD', mode_str, 4320, exit_t)
                    
                    be_activated = False
                    be_activation_dist = 20 * bot_state['pip_value']
                    be_target = act_ent + (m * be_activation_dist)
                    
                    for c in [v for v in val_candles if entry_t <= v['time'] <= exit_t]:
                        if buy_sig:
                            if bot_state['use_be'] and not be_activated and c['high'] >= be_target:
                                sl_p = act_ent
                                be_activated = True
                            if c['low'] <= sl_p: outcome, exit_t = ("BREAK-EVEN" if be_activated and sl_p == act_ent else "LOSS"), c['time']; break
                            if c['high'] >= eff_tp_p: outcome, exit_t = "WIN", c['time']; break
                        else:
                            if bot_state['use_be'] and not be_activated and c['low'] <= be_target:
                                sl_p = act_ent
                                be_activated = True
                            if c['high'] >= sl_p: outcome, exit_t = ("BREAK-EVEN" if be_activated and sl_p == act_ent else "LOSS"), c['time']; break
                            if c['low'] <= eff_tp_p: outcome, exit_t = "WIN", c['time']; break
                    
                    if outcome == "BREAK-EVEN": 
                        p_usd = 0.0
                        be_count += 1
                    elif outcome in ["WIN", "LOSS"]:
                        p_usd = round(abs(act_ent - (tp_p if outcome=="WIN" else sl_p)) * 100 * bot_state['lot_size'], 2) * (1 if outcome=="WIN" else -1)
                    else:
                        p_usd = 0.0
                    
                    total_prof += p_usd
                    peak_equity = max(peak_equity, total_prof)
                    max_dd = max(max_dd, peak_equity - total_prof)
                    
                    trade_logs.append({
                        'Timeframe': tf, 'Type': 'BUY' if buy_sig else 'SELL',
                        'Entry Time': entry_t.strftime('%Y-%m-%d %H:%M'),
                        'Exit Time': exit_t.strftime('%Y-%m-%d %H:%M'),
                        'Entry Price': round(act_ent, 2),
                        'TP': tp_p, 'SL': sl_p,
                        'Pips': round(abs(act_ent - (tp_p if outcome=="WIN" else sl_p)) / bot_state['pip_value'], 1) if outcome in ["WIN", "LOSS"] else 0,
                        'Outcome': outcome, 'Profit ($)': p_usd
                    })

        if trade_logs:
            df_logs = pd.DataFrame(trade_logs)
            summary_row = pd.DataFrame([
                {'Timeframe': '--- SUMMARY ---', 'Type': '', 'Entry Time': '', 'Exit Time': '', 'Entry Price': 'TOTAL NET PROFIT:', 'TP': '', 'SL': '', 'Pips': '', 'Outcome': f'${round(total_prof, 2)}', 'Profit ($)': ''},
                {'Timeframe': '', 'Type': '', 'Entry Time': '', 'Exit Time': '', 'Entry Price': 'MAX DRAWDOWN:', 'TP': '', 'SL': '', 'Pips': '', 'Outcome': f'${round(max_dd, 2)}', 'Profit ($)': ''},
                {'Timeframe': '', 'Type': '', 'Entry Time': '', 'Exit Time': '', 'Entry Price': 'BREAK-EVEN TRADES:', 'TP': '', 'SL': '', 'Pips': '', 'Outcome': str(be_count), 'Profit ($)': ''}
            ])
            pd.concat([df_logs, summary_row]).to_csv(csv_filename, index=False)
            await send_tg_document(csv_filename, f"📊 التقرير التفصيلي متاح الآن.\nالربح الصافي: ${round(total_prof, 2)}\nتراجع: ${round(max_dd, 2)}")
            os.remove(csv_filename)
            c_log("✅ اكتمل الباك تيست وتم إرسال التقرير.")
        else: 
            await send_tg_msg("⚠️ لم يتم العثور على صفقات.")
            c_log("⚠️ اكتمل الباك تيست ولم يتم العثور على أي صفقات تطابق الشروط.")
    except Exception as e: 
        c_log(f"❌ خطأ أثناء الباك تيست: {e}")
        await send_tg_msg(f"❌ خطأ: {e}")
    finally: 
        bot_state['is_backtesting'] = False

async def position_monitor():
    while True:
        try:
            if bot_state['live_connected'] and bot_state['use_be'] and bot_state['status'] == 'RUNNING' and bot_state['connection_obj']:
                positions = await bot_state['connection_obj'].get_positions()
                for p in positions:
                    if p['symbol'] == bot_state['symbol']:
                        open_price = p['openPrice']
                        tp = p.get('takeProfit')
                        sl = p.get('stopLoss')
                        current_price = p['currentPrice']
                        if tp and sl != open_price:
                            current_dist = abs(current_price - open_price)
                            if current_dist >= (20 * bot_state['pip_value']):
                                is_buy = tp > open_price
                                if (is_buy and current_price > open_price) or (not is_buy and current_price < open_price):
                                    await bot_state['connection_obj'].modify_position(p['id'], stop_loss=open_price)
                                    msg = f"🛡️ <b>تأمين دخول (Break-Even)</b>\nتم نقل الوقف لنقطة الدخول لحماية الصفقة: {p['id']}"
                                    c_log(msg.replace('<b>','').replace('</b>',''))
                                    await send_tg_msg(msg)
        except Exception as e: 
            # Silent catch to avoid spamming logs for monitor
            pass
        await asyncio.sleep(5)

# --- TG POLLING LOOP (UI IN-PLACE EDITS) ---
async def process_tg_update(update):
    if 'message' in update and 'text' in update['message']:
        msg = update['message']['text'].strip()
        bot_state['chat_id'] = update['message']['chat']['id']
        c_log(f"📩 رسالة تلغرام مستلمة: {msg}")
        
        if msg == '/start': await send_tg_msg("🤖 <b>مرحباً بك في لوحة التحكم!</b>\nالواجهة محدثة لدعم التعديل الفوري.", get_main_keyboard())
        elif msg.startswith('/set'):
            p = msg.split()
            if len(p) == 4:
                bot_state[p[2]+'_pips'][p[1]] = int(p[3])
                await send_tg_msg(f"✅ تم تحديث {p[2]} لفريم {p[1]} إلى {p[3]}")
                c_log(f"⚙️ تم تحديث {p[2]} لفريم {p[1]} إلى {p[3]}")
        elif msg.startswith('/backtest'):
            try: 
                date_str = msg.split()[1]
                st = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                asyncio.create_task(run_oanda_backtest(st, mode='candle'))
                await send_tg_msg(f"✅ تم استلام أمر الباك تيست من تاريخ: {date_str}")
            except: await send_tg_msg("⚠️ صيغة الأمر خاطئة. استخدم: /backtest YYYY-MM-DD")

    elif 'callback_query' in update:
        q = update['callback_query']
        d = q['data']
        chat_id = q['message']['chat']['id']
        msg_id = q['message']['message_id']
        bot_state['chat_id'] = chat_id
        
        c_log(f"🖱️ ضغطة زر مستلمة: {d}")
        
        if d == "toggle_live_conn":
            if not bot_state['live_connected']:
                await answer_callback(q['id'], "جاري الاتصال بالسيرفرات...")
                await edit_tg_msg(chat_id, msg_id, "⏳ جاري الاتصال بسيرفرات MetaApi... يرجى الانتظار.", get_main_keyboard())
                c_log("🔌 محاولة الاتصال بـ MetaApi...")
                try:
                    api = MetaApi(METAAPI_TOKEN)
                    bot_state['account_obj'] = await api.metatrader_account_api.get_account(ACCOUNT_ID)
                    bot_state['connection_obj'] = bot_state['account_obj'].get_rpc_connection()
                    await bot_state['connection_obj'].connect()
                    await bot_state['connection_obj'].wait_synchronized()
                    bot_state['live_connected'] = True
                    c_log("✅ تم الاتصال بـ MetaApi بنجاح!")
                    await edit_tg_msg(chat_id, msg_id, "✅ تم الاتصال بنجاح بـ MetaApi! الماسح الحي يعمل الآن.", get_main_keyboard())
                except Exception as e:
                    bot_state['live_connected'] = False
                    c_log(f"❌ فشل الاتصال: {e}")
                    await send_tg_msg(f"❌ فشل الاتصال: {e}")
            else:
                bot_state['live_connected'] = False
                bot_state['connection_obj'] = None
                bot_state['account_obj'] = None
                c_log("🔌 تم فصل الاتصال بـ MetaApi.")
                await answer_callback(q['id'], "تم قطع الاتصال بنجاح.")
                await edit_tg_msg(chat_id, msg_id, "🔌 تم فصل الاتصال (وضع Offline للباك تيست).", get_main_keyboard())
                
        elif d == "menu_main": 
            await answer_callback(q['id'])
            await edit_tg_msg(chat_id, msg_id, "🏠 القائمة الرئيسية:", get_main_keyboard())
            
        elif d == "toggle_status": 
            bot_state['status'] = 'PAUSED' if bot_state['status'] == 'RUNNING' else 'RUNNING'
            await answer_callback(q['id'], f"تم التغيير إلى {bot_state['status']}")
            await edit_tg_msg(chat_id, msg_id, "🏠 القائمة الرئيسية:", get_main_keyboard())
            
        elif d == "toggle_strat": 
            bot_state['strategy'] = 'NEW' if bot_state['strategy'] == 'OLD' else 'OLD'
            await answer_callback(q['id'], f"تم تغيير الاستراتيجية إلى {bot_state['strategy']}")
            await edit_tg_msg(chat_id, msg_id, "🏠 القائمة الرئيسية:", get_main_keyboard())
            
        elif d == "toggle_time": 
            bot_state['use_time_filter'] = not bot_state['use_time_filter']
            await answer_callback(q['id'], "تم تحديث فلتر الوقت.")
            await edit_tg_msg(chat_id, msg_id, "🏠 القائمة الرئيسية:", get_main_keyboard())
        
        elif d == "menu_filters": 
            await answer_callback(q['id'])
            await edit_tg_msg(chat_id, msg_id, "🎛 <b>إدارة فلاتر التوازي:</b>", get_filters_keyboard())
            
        elif d == "toggle_f_cons": 
            bot_state['use_f_cons'] = not bot_state['use_f_cons']
            await answer_callback(q['id'], "تم تعديل فلتر الثبات.")
            await edit_tg_msg(chat_id, msg_id, "🎛 <b>إدارة فلاتر التوازي:</b>", get_filters_keyboard())
            
        elif d == "toggle_f_gap": 
            bot_state['use_f_gap'] = not bot_state['use_f_gap']
            await answer_callback(q['id'], "تم تعديل فلتر الفجوة.")
            await edit_tg_msg(chat_id, msg_id, "🎛 <b>إدارة فلاتر التوازي:</b>", get_filters_keyboard())
            
        elif d == "toggle_f_mtf": 
            bot_state['use_f_mtf'] = not bot_state['use_f_mtf']
            await answer_callback(q['id'], "تم تعديل فلتر الفريم الأكبر.")
            await edit_tg_msg(chat_id, msg_id, "🎛 <b>إدارة فلاتر التوازي:</b>", get_filters_keyboard())
        
        elif d == "menu_tfs": 
            await answer_callback(q['id'])
            await edit_tg_msg(chat_id, msg_id, "⏱ إدارة الفريمات:", get_tf_keyboard())
            
        elif d.startswith("toggle_tf_"):
            tf = d.split("_")[2]; bot_state['active_tfs'][tf] = not bot_state['active_tfs'][tf]
            await answer_callback(q['id'], f"تم تغيير تفعيل فريم {tf}")
            await edit_tg_msg(chat_id, msg_id, "⏱ إدارة الفريمات:", get_tf_keyboard())
            
        elif d == "menu_settings": 
            await answer_callback(q['id'])
            await edit_tg_msg(chat_id, msg_id, "🛠 الإعدادات وإدارة المخاطر:", get_settings_keyboard())
            
        elif d == "toggle_be": 
            bot_state['use_be'] = not bot_state['use_be']
            await answer_callback(q['id'], "تم تعديل تأمين الدخول.")
            await edit_tg_msg(chat_id, msg_id, "🛠 الإعدادات وإدارة المخاطر:", get_settings_keyboard())
            
        elif d == "toggle_atr": 
            bot_state['use_atr'] = not bot_state['use_atr']
            await answer_callback(q['id'], "تم تعديل أهداف الـ ATR.")
            await edit_tg_msg(chat_id, msg_id, "🛠 الإعدادات وإدارة المخاطر:", get_settings_keyboard())
            
        elif d == "toggle_spread": 
            bot_state['use_max_spread'] = not bot_state['use_max_spread']
            await answer_callback(q['id'], "تم تعديل حماية السبريد.")
            await edit_tg_msg(chat_id, msg_id, "🛠 الإعدادات وإدارة المخاطر:", get_settings_keyboard())
            
        elif d == "toggle_s5": 
            bot_state['use_s5_ticks'] = not bot_state['use_s5_ticks']
            await answer_callback(q['id'], "تم تعديل دقة الباك تيست.")
            await edit_tg_msg(chat_id, msg_id, "🛠 الإعدادات وإدارة المخاطر:", get_settings_keyboard())
            
        elif d == "inc_lot": 
            bot_state['lot_size'] = round(bot_state['lot_size'] + 0.01, 2)
            await answer_callback(q['id'], f"اللوت الآن: {bot_state['lot_size']}")
            await edit_tg_msg(chat_id, msg_id, "🛠 الإعدادات وإدارة المخاطر:", get_settings_keyboard())
            
        elif d == "dec_lot": 
            bot_state['lot_size'] = max(0.01, round(bot_state['lot_size'] - 0.01, 2))
            await answer_callback(q['id'], f"اللوت الآن: {bot_state['lot_size']}")
            await edit_tg_msg(chat_id, msg_id, "🛠 الإعدادات وإدارة المخاطر:", get_settings_keyboard())
            
        elif d == "view_tpsl":
            txt = "📖 <b>أهداف الفريمات (TP/SL):</b>\n" + "\n".join([f"[{tf}] TP:{bot_state['tp_pips'][tf]} | SL:{bot_state['sl_pips'][tf]}" for tf in bot_state['timeframes']])
            await answer_callback(q['id'])
            await edit_tg_msg(chat_id, msg_id, txt, get_settings_keyboard())
            
        elif d == "report":
            txt = "📊 <b>حالة السوق الحية:</b>\n" + "\n".join([f"[{tf}] {bot_state['market_data'][tf]}" for tf in bot_state['timeframes'] if bot_state['active_tfs'][tf]])
            await answer_callback(q['id'], "تم تحديث التقرير الحي.")
            await edit_tg_msg(chat_id, msg_id, txt, get_main_keyboard())
            
        elif d == "account":
            if bot_state['live_connected'] and bot_state['connection_obj']:
                try:
                    acc = await bot_state['connection_obj'].get_account_information()
                    txt = f"💳 <b>الحساب:</b>\nرصيد: {acc['balance']}\nإيكويتي: {acc['equity']}"
                    await answer_callback(q['id'])
                    await edit_tg_msg(chat_id, msg_id, txt, get_main_keyboard())
                except Exception as e: c_log(f"❌ خطأ جلب الحساب: {e}")
            else:
                await answer_callback(q['id'], "يجب الاتصال بالسيرفر أولاً!")
                
        elif d == "menu_backtest":
            kb = {
                "inline_keyboard": [
                    [{"text": "📊 1 يوم", "callback_data": "bto_1"}, {"text": "📊 3 أيام", "callback_data": "bto_3"}, {"text": "📊 أسبوع", "callback_data": "bto_7"}],
                    [{"text": "أو أرسل أمر: /backtest YYYY-MM-DD", "callback_data": "noop"}],
                    [{"text": "🔙 رجوع", "callback_data": "menu_main"}]
                ]
            }
            await answer_callback(q['id'])
            await edit_tg_msg(chat_id, msg_id, "اختر المدة أو استخدم الأمر النصي:", kb)
            
        elif d.startswith("bto_"):
            days = int(d.split('_')[1])
            await answer_callback(q['id'], f"تم طلب باك تيست لـ {days} أيام.")
            asyncio.create_task(run_oanda_backtest(datetime.now(timezone.utc) - timedelta(days=days), mode='candle'))
            
        elif d == "close_all":
            if bot_state['live_connected'] and bot_state['connection_obj']:
                await answer_callback(q['id'], "جاري إغلاق الصفقات المفتوحة...")
                async def close_positions():
                    try:
                        pos = await bot_state['connection_obj'].get_positions()
                        for p in pos: 
                            await bot_state['connection_obj'].close_position(p['id'])
                            c_log(f"✅ تم إغلاق الصفقة {p['id']}")
                        await send_tg_msg("✅ تم إغلاق جميع الصفقات المفتوحة بنجاح.")
                    except Exception as e: 
                        c_log(f"❌ خطأ في الإغلاق: {e}")
                        await send_tg_msg(f"❌ خطأ في الإغلاق: {e}")
                asyncio.create_task(close_positions())
            else:
                await answer_callback(q['id'], "البوت غير متصل بالسيرفر الحي!")

async def telegram_polling_loop():
    c_log("✅ خدمة التلغرام تعمل في الخلفية.")
    url = f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates"
    async with aiohttp.ClientSession() as s:
        while True:
            try:
                async with s.get(url, params={'offset': bot_state['last_update_id']+1, 'timeout': 10}) as r:
                    if r.status == 200:
                        for u in (await r.json()).get('result', []):
                            bot_state['last_update_id'] = u['update_id']
                            asyncio.create_task(process_tg_update(u))
                    else:
                        c_log(f"⚠️ خطأ تلغرام مؤقت. الكود: {r.status}")
            except Exception as e: 
                c_log(f"⚠️ مشكلة في شبكة التلغرام: {e}")
                await asyncio.sleep(5)

async def timeframe_scanner(tf):
    c_log(f"✅ ماسح الفريم [{tf}] جاهز للعمل.")
    loop_count = 0
    while True:
        try:
            if bot_state['status'] == 'RUNNING' and bot_state['active_tfs'][tf]:
                if not bot_state['live_connected'] or not bot_state['account_obj']:
                    bot_state['market_data'][tf] = "⏸ بانتظار الاتصال (Offline)"
                    await asyncio.sleep(5)
                    continue
                    
                c = await bot_state['account_obj'].get_historical_candles(bot_state['symbol'], tf, limit=100)
                df = calculate_indicators(pd.DataFrame(c))
                df_htf = calculate_indicators(pd.DataFrame(await bot_state['account_obj'].get_historical_candles(bot_state['symbol'], get_htf(tf), limit=50))) if bot_state['use_f_mtf'] else pd.DataFrame()
                curr, prev = df.iloc[-2], df.iloc[-3]
                
                h = datetime.now(timezone.utc).hour
                if bot_state['use_time_filter'] and not (8 <= h <= 17):
                    bot_state['market_data'][tf] = f"⏸ خمول | {c[-1]['close']}"
                else:
                    bot_state['market_data'][tf] = f"{c[-1]['close']} | K:{curr['K']:.1f}"
                    
                    # Heartbeat log every ~1 minute (6 loops of 10s)
                    loop_count += 1
                    if loop_count >= 6:
                        c_log(f"🔄 [{tf}] السعر: {c[-1]['close']} | الموفينجات: قيد المراقبة")
                        loop_count = 0

                    if bot_state['last_signal_time'][tf] != curr['time']:
                        skip_trade = False
                        current_spread = 0.0
                        if bot_state['use_max_spread']:
                            try:
                                tick = await bot_state['connection_obj'].get_tick(bot_state['symbol'])
                                current_spread = round((tick['ask'] - tick['bid']) / bot_state['pip_value'], 1)
                                if current_spread > bot_state['max_spread_pips']: 
                                    skip_trade = True
                                    c_log(f"🛑 [{tf}] حظر الدخول: السبريد عالي ({current_spread} > {bot_state['max_spread_pips']})")
                            except Exception as e: 
                                c_log(f"⚠️ خطأ في فحص السبريد: {e}")

                        if not skip_trade:
                            b, s = check_filters(df, len(df)-2, df_htf)
                            buy = b and prev['K'] <= 20 and curr['K'] > prev['K'] if bot_state['strategy'] == 'NEW' else b and 5 <= curr['K'] <= 20 and prev['K'] <= prev['D'] and curr['K'] > curr['D']
                            sell = s and prev['K'] >= 80 and curr['K'] < prev['K'] if bot_state['strategy'] == 'NEW' else s and 80 <= curr['K'] <= 100 and prev['K'] >= prev['D'] and curr['K'] < curr['D']
                            
                            if buy or sell:
                                bot_state['last_signal_time'][tf] = curr['time']
                                p = c[-1]['close']
                                m = 1 if buy else -1
                                trade_type_str = "شراء 🟢 BUY" if buy else "بيع 🔴 SELL"
                                c_log(f"🎯 [{tf}] إشارة {trade_type_str} التقطت! جاري إرسال الأمر للسيرفر...")
                                
                                if bot_state['use_atr']:
                                    tp_dist = curr['atr'] * bot_state['atr_mult_tp']
                                    sl_dist = curr['atr'] * bot_state['atr_mult_sl']
                                else:
                                    tp_dist = bot_state['tp_pips'][tf] * bot_state['pip_value']
                                    sl_dist = bot_state['sl_pips'][tf] * bot_state['pip_value']
                                    
                                tp = round(p + (m * tp_dist), 2)
                                sl = round(p - (m * sl_dist), 2)
                                
                                try:
                                    if buy: await bot_state['connection_obj'].create_market_buy_order(bot_state['symbol'], bot_state['lot_size'], stop_loss=sl, take_profit=tp)
                                    else: await bot_state['connection_obj'].create_market_sell_order(bot_state['symbol'], bot_state['lot_size'], stop_loss=sl, take_profit=tp)
                                    
                                    c_log(f"✅ [{tf}] تم التنفيذ بنجاح! السعر: {p}")
                                    notif_msg = (
                                        f"🚨 <b>تم فتح صفقة حية جديدة!</b>\n\n"
                                        f"الزوج: {bot_state['symbol']}\n"
                                        f"النوع: {trade_type_str}\n"
                                        f"الفريم: {tf}\n"
                                        f"السعر اللحظي: {p}\n"
                                        f"الهدف (TP): {tp}\n"
                                        f"الوقف (SL): {sl}\n"
                                        f"التوقيت: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
                                    )
                                    await send_tg_msg(notif_msg)
                                except Exception as e:
                                    c_log(f"❌ [{tf}] فشل تنفيذ الصفقة: {e}")
                                    await send_tg_msg(f"❌ <b>فشل تنفيذ صفقة {trade_type_str}</b>\nالسبب: {e}")
                                
            await asyncio.sleep(10)
        except Exception as e: 
            c_log(f"❌ خطأ غير متوقع في ماسح [{tf}]: {e}")
            await asyncio.sleep(15)

async def main():
    print("=========================================")
    print("🚀 جاري تشغيل بوت التداول اللحظي والباك تيست")
    print("=========================================")
    tasks = [asyncio.create_task(timeframe_scanner(tf)) for tf in bot_state['timeframes']]
    tasks.append(asyncio.create_task(telegram_polling_loop()))
    tasks.append(asyncio.create_task(position_monitor()))
    await asyncio.gather(*tasks)

if __name__ == "__main__": asyncio.run(main())
# خادم ويب وهمي لخدعة البقاء مستيقظاً (Keep-Alive)
async def handle_ping(request):
    return web.Response(text="Bot is ALIVE and Trading!")

async def main():
    print("=========================================")
    print("🚀 جاري تشغيل بوت التداول اللحظي والباك تيست سحابياً")
    print("=========================================")
    
    # 1. إعداد خادم الويب
    app = web.Application()
    app.add_routes([web.get('/', handle_ping)])
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get('PORT', 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"🌐 خادم الويب يعمل على المنفذ {port}")

    # 2. تشغيل مهام البوت الأساسية
    tasks = [asyncio.create_task(timeframe_scanner(tf)) for tf in bot_state['timeframes']]
    tasks.append(asyncio.create_task(telegram_polling_loop()))
    tasks.append(asyncio.create_task(position_monitor()))
    
    await asyncio.gather(*tasks)

if __name__ == "__main__": 
    asyncio.run(main())
