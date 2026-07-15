import json
"""
Gold Scalper Bot -- v9.4 (Resilience-First Core)
Strategy : Gann Levels + Fan Angles + Break-Even Triggered by Noise Levels

v9.4 changes vs v8.9 (see PATCH_NOTES.md shipped alongside this file):
  - No hardcoded credential fallbacks; bot refuses to start without env vars.
  - No silent except-pass in execution / reconciliation / order-management paths.
  - Explicit HALT / READ_ONLY connection-state machine with Telegram escalation.
  - Persistence now captures full per-symbol cycle state, not just open trades.
  - Startup reconstructs state from disk before ANY market interaction.
"""

import asyncio
import logging
import traceback
import time
import random
import zlib
import aiohttp
import os
import sys
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone, time as dtime
from aiohttp import web
import openpyxl
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
from metaapi_cloud_sdk import MetaApi, SynchronizationListener

# -----------------------------------------------------------------
# LOGGING (structured, always includes tracebacks for exceptions)
# -----------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    stream=sys.stdout,
)
logger = logging.getLogger('gold_scalper')

def log_exception(context: str, exc: Exception) -> None:
    """Zero-tolerance logging: every caught exception in a critical path gets
    a full traceback attached to the log line, not just str(e)."""
    logger.error("EXCEPTION in %s: %s\n%s", context, exc, traceback.format_exc())

_DIAG_LOG_MAX_ENTRIES = 50000  # ~generous cap; trimmed on every append

def _diag_log_add(entry: dict) -> None:
    """Append one row to the rolling live-scan diagnostic log (see
    bot_state['diag_log']). This is what /export_diag_excel dumps -- it's
    the ONLY place that records the *silent* skip reasons (insufficient
    candle data, cap reached, trend unknown, etc.) that never get a
    Telegram message of their own, so the operator can reconstruct exactly
    what the scanner saw/did on every (symbol, timeframe, cycle), not just
    a point-in-time snapshot."""
    log = bot_state.setdefault('diag_log', [])
    log.append(entry)
    if len(log) > _DIAG_LOG_MAX_ENTRIES:
        del log[: len(log) - _DIAG_LOG_MAX_ENTRIES]

def _record_closed_trade_history(symbol: str, tid: str, tr: dict, exit_px: float, pnl: float,
                                  outcome_label: str, close_reason: str, pnl_confirmed: bool) -> None:
    """Append one row of full detail for a just-closed real/virtual live
    trade, feeding /export_live_trades_excel. Kept deliberately rich (every
    field a human would need to judge "did this trade behave like the
    backtest expected") since this is the ONLY place live trade outcomes
    get durably recorded anywhere in the bot today."""
    hist = bot_state.setdefault('live_trade_history', [])
    entry = tr.get('entry'); is_buy = tr.get('is_buy')
    opened_at = tr.get('opened_at')
    closed_at_dt = datetime.now(timezone.utc)
    duration_min = None
    if opened_at:
        try:
            opened_dt = datetime.fromisoformat(opened_at) if isinstance(opened_at, str) else opened_at
            duration_min = round((closed_at_dt - opened_dt).total_seconds() / 60.0, 1)
        except Exception:
            duration_min = None
    intended_entry = tr.get('level_price', entry)
    entry_slip = (entry - intended_entry) if (entry is not None and intended_entry is not None) else None
    hist.append({
        'symbol': symbol, 'tid': tid, 'tf': tr.get('tf'), 'is_real': bool(tr.get('is_real')),
        'is_buy': is_buy, 'opened_at': opened_at, 'closed_at': closed_at_dt.isoformat(),
        'duration_min': duration_min, 'level_price': intended_entry, 'entry': entry,
        'entry_slippage': entry_slip, 'tp': tr.get('tp'), 'sl': tr.get('sl'), 'exit_price': exit_px,
        'outcome': outcome_label, 'pnl': pnl, 'pnl_confirmed_from_broker': pnl_confirmed,
        'close_reason': close_reason, 'be_activated': bool(tr.get('be_activated')),
        'feed_source': tr.get('feed_source'), 'feed_age_ms': tr.get('feed_age_ms'),
    })
    if len(hist) > _DIAG_LOG_MAX_ENTRIES:
        del hist[: len(hist) - _DIAG_LOG_MAX_ENTRIES]

# -----------------------------------------------------------------
# CONFIGURATION
# -----------------------------------------------------------------
def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        logger.critical("FATAL: required environment variable '%s' is not set. Refusing to start.", name)
        sys.exit(1)
    return val

METAAPI_TOKEN  = _require_env('METAAPI_TOKEN')
ACCOUNT_ID     = _require_env('ACCOUNT_ID')
TG_TOKEN       = _require_env('TG_TOKEN')
OANDA_ACCOUNT  = _require_env('OANDA_ACCOUNT')
OANDA_TOKEN    = _require_env('OANDA_TOKEN')
AVAILABLE_SYMBOLS = ['XAU_USD', 'XAU_EUR', 'XAG_USD', 'EUR_USD', 'GBP_JPY', 'GBP_AUD', 'GBP_NZD', 'AUD_JPY', 'NZD_JPY']
SYMBOL_INFO = {
    'XAU_USD': {'pip_value': 0.1,     'contract_size': 100,    'prec': 2, 'name': 'Gold (USD)'},
    'XAU_EUR': {'pip_value': 0.1,     'contract_size': 100,    'prec': 2, 'name': 'Gold (EUR)'},
    'XAG_USD': {'pip_value': 0.001,   'contract_size': 5000,   'prec': 3, 'name': 'Silver'},
    'EUR_USD': {'pip_value': 0.00001, 'contract_size': 100000, 'prec': 5, 'name': 'EUR/USD'},
    'GBP_JPY': {'pip_value': 0.01,    'contract_size': 100000, 'prec': 3, 'name': 'GBP/JPY'},
    'GBP_AUD': {'pip_value': 0.00001, 'contract_size': 100000, 'prec': 5, 'name': 'GBP/AUD'},
    'GBP_NZD': {'pip_value': 0.00001, 'contract_size': 100000, 'prec': 5, 'name': 'GBP/NZD'},
    'AUD_JPY': {'pip_value': 0.01,    'contract_size': 100000, 'prec': 3, 'name': 'AUD/JPY'},
    'NZD_JPY': {'pip_value': 0.01,    'contract_size': 100000, 'prec': 3, 'name': 'NZD/JPY'},
}
OANDA_BASE_URL = 'https://api-fxpractice.oanda.com/v3'  

_TFS = ['1m', '2m', '3m', '4m', '5m', '6m', '10m', '15m', '20m', '30m', '1h', '2h']

_http: aiohttp.ClientSession | None = None

def get_http() -> aiohttp.ClientSession:
    global _http
    if _http is None or _http.closed:
        connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)
        timeout   = aiohttp.ClientTimeout(total=30, connect=10)
        _http     = aiohttp.ClientSession(connector=connector, timeout=timeout)
    return _http

def c_log(msg: str) -> None:
    dam = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime('%H:%M:%S')
    print(f"[{dam} DAM] {msg}", flush=True)

# -----------------------------------------------------------------
# GLOBAL STATE
# -----------------------------------------------------------------
_metaapi = None
_metaapi_account = None
_metaapi_conn = None

# ── Live-quote push cache (WebSocket/streaming feed) ──
# Populated by _GannPriceListener.on_symbol_price_updated, keyed by the
# OANDA-format symbol used everywhere else in the bot ('XAU_USD'), NOT
# the broker's own symbol name ('XAUUSD') -- _broker_to_data_symbol
# translates incoming broker-symbol ticks back to that key.
live_quotes: dict[str, dict] = {}          # {'XAU_USD': {'bid':, 'ask':, 'mid':, 'ts': monotonic}}
_broker_to_data_symbol: dict[str, str] = {}  # {'XAUUSD': 'XAU_USD', ...}
_QUOTE_STALE_SECONDS = 5.0
# Updated on EVERY tick received from MetaApi, for ANY symbol -- this is
# deliberately independent of live_quotes/_broker_to_data_symbol (which can
# be empty/wrong) and of _metaapi_account.connection_status (which the SDK
# can keep reporting 'CONNECTED' even when the underlying WS session has
# gone silent/zombie, e.g. during a broker daily-rollover freeze). The
# watchdog in gann_monitor_scanner uses ONLY this raw timestamp to decide
# whether to force a full connection teardown+reconnect.
_last_any_tick_ts = time.monotonic()
_WS_WATCHDOG_STALE_SECONDS = 60.0

# Per-symbol cache refreshed periodically by gann_monitor_scanner (levels,
# trend state, each enabled tf's closed-candle data). _gann_tick_fire_check
# reads this on every price tick -- refreshing it doesn't need tick-level
# freshness, only the live price checked against it does, and that now
# always comes straight from the tick that triggered the check.
_gann_cache: dict[str, dict] = {}


class _GannPriceListener(SynchronizationListener):
    """Pushed quotes from MetaApi's streaming connection -- this is what
    'broker-direct WebSocket price feed' actually means: MetaApi's own
    live terminal-state cache, not OANDA's REST polling. Feeds
    live_quotes[]; the scanner reads from there instead of calling out
    to OANDA for the current price."""

    async def on_symbol_price_updated(self, instance_index, price):
        global _last_any_tick_ts
        _last_any_tick_ts = time.monotonic()
        broker_sym = price.get('symbol')
        data_sym = _broker_to_data_symbol.get(broker_sym)
        if not data_sym:
            return
        bid = price.get('bid'); ask = price.get('ask')
        if bid is None or ask is None:
            return
        mid = (bid + ask) / 2
        live_quotes[data_sym] = {'bid': bid, 'ask': ask, 'mid': mid, 'ts': time.monotonic()}
        # Event-driven touch detection: react to THIS tick right now, not on
        # the next scanner cycle. create_task() so this callback returns
        # immediately -- _gann_tick_fire_check does its own (awaited) I/O,
        # but none of that blocks the SDK's socket message processing since
        # it's a separate task, not inline in this callback.
        asyncio.create_task(_gann_tick_fire_check(data_sym, mid, 0.0))

    async def on_connected(self, instance_index, replicas):
        c_log("MetaAPI streaming connection established (price feed live).")

    async def on_disconnected(self, instance_index):
        c_log("MetaAPI streaming connection lost -- reconnect loop will retry and resubscribe.")


async def _gann_tick_fire_check(symbol: str, live_px: float, feed_age_ms: float) -> None:
    """The actual touch decision, run the instant a new tick arrives (called
    via create_task from _GannPriceListener.on_symbol_price_updated). Uses
    whatever levels/trend/candle data gann_monitor_scanner's periodic
    refresh last cached for this symbol in _gann_cache -- but the PRICE
    checked against that data is always this exact tick, never a value
    read back out of a timer loop. That's what makes firing on a stale
    quote structurally impossible now, not just less likely."""
    try:
        if bot_state.get('connection_state') != CONN_RUNNING:
            return
        if bot_state.get('live_daily_hit'):
            return
        if bot_state.get('prot_dam_time_filter', True):
            dam_time = (datetime.now(timezone.utc) + timedelta(hours=3)).time()
            if any(start <= dam_time < end for start, end in _DAM_RESTRICTED_WINDOWS):
                return

        cache = _gann_cache.get(symbol)
        if not cache:
            return
        sym_state = bot_state['symbol_state'][symbol]
        if not sym_state['gann_cycle_active'] or not sym_state['gann_levels']:
            return

        max_concurrent = int(bot_state.get('prot_max_concurrent_trades', 4))
        open_count = sum(1 for v in sym_state['gann_open_trades'].values() if isinstance(v, dict))
        if open_count >= max_concurrent:
            return

        margin = cache['margin']; levels = cache['levels']; trend_up = cache['trend_up']
        entry_mode = sym_state['gann_entry_mode']
        exec_mode = bot_state.get('gann_execution_mode', 'instant')
        pv = SYMBOL_INFO[symbol]['pip_value']
        spike_limit = bot_state.get('gann_spike_limit_pts', 20) * pv
        flt_type = sym_state['trend_filter_type']; ttf = sym_state['trend_timeframe']
        detect_time = datetime.now(timezone.utc)

        for tf in cache['enabled_tfs']:
            if any(isinstance(v, dict) and v.get('tf') == tf for v in sym_state['gann_open_trades'].values()):
                continue
            tf_data = cache['tf_data'].get(tf)
            if not tf_data:
                continue
            candles = tf_data['candles']; closed_close = tf_data['closed_close']

            if entry_mode == 'touch_trend' and trend_up is None:
                continue

            # ── Which trigger channel(s) to evaluate this tick ──
            # For the 3 existing modes this is a single channel, identical
            # to the old behavior. all_concurrent runs all three
            # independently -- each is checked against the SAME level pool
            # but gets its OWN dedup key (see combo_key below), so e.g. a
            # touch and a close can both fire on the same level without
            # blocking each other, exactly as requested for the 24h
            # concurrent comparison test.
            if exec_mode == 'all_concurrent':
                channels = ['touch', 'close', 'hybrid']
            elif exec_mode == 'close':
                channels = ['close']
            elif exec_mode == 'hybrid':
                channels = ['hybrid']
            else:
                channels = ['touch']  # 'instant'

            for channel in channels:
                for lv in levels:
                    k = lv['key']; dir_ = lv['dir']
                    # Only all_concurrent suffixes the channel onto the dedup
                    # key -- the other 3 modes keep their exact original key
                    # so nothing about their existing dedup/persistence
                    # behavior changes.
                    base_combo = f"{k}_{tf}" if bot_state['prot_allow_multi_tf'] else k
                    combo_key = f"{base_combo}_{channel}" if exec_mode == 'all_concurrent' else base_combo
                    if sym_state['gann_level_status'].get(combo_key) == 'used':
                        continue
                    is_buy = (dir_ == 'dn')
                    if entry_mode == 'touch_trend':
                        if is_buy and not trend_up: continue
                        if not is_buy and trend_up: continue

                    # ── Execution mode gate ──
                    # close: fire purely off the OANDA closed-candle close, exactly
                    # like run_gann_backtest's bar_close check -- do NOT also require
                    # the current MetaApi tick to be within margin (that's a second,
                    # cross-feed condition the classic backtest never applies, and it
                    # was silently killing/mismatching trades vs the backtest).
                    if channel == 'close':
                        if abs(closed_close - lv['price']) > margin:
                            continue
                    elif channel == 'hybrid':
                        if abs(live_px - lv['price']) > margin:
                            continue
                        if abs(live_px - closed_close) > spike_limit:
                            continue
                    else:  # touch (instant)
                        if abs(live_px - lv['price']) > margin:
                            continue

                    # ── Reserve the level NOW, synchronously ──
                    # asyncio only switches tasks at an `await`, so writing this
                    # before any await here is atomic with respect to every other
                    # concurrent tick's task -- a second tick arriving a
                    # microsecond later will see 'used' immediately, even though
                    # _gann_open_trade's own internal marking (further below,
                    # after several awaits of its own) hasn't happened yet. Without
                    # this, going event-driven (many concurrent per-tick tasks
                    # instead of one serial scanner loop) would let two
                    # near-simultaneous ticks both fire on the same level.
                    sym_state['gann_level_status'][combo_key] = 'used'

                    if flt_type == 'vwap': flt_label = f"VWAP={sym_state['trend_vwap_period']}\n"
                    elif flt_type == 'ema': flt_label = f"EMA={sym_state['trend_ema_period']}\n"
                    else: flt_label = "VWAP+EMA"
                    trigger_lbl = {'touch': 'لمس مباشر ⚡', 'close': 'إغلاق شمعة ⏳', 'hybrid': 'تنفيذ هجين 🛡️'}[channel]
                    dir_word = 'BUY' if is_buy else 'SELL'
                    dir_emoji = '📈' if is_buy else '📉'
                    reason = f"{dir_word} {dir_emoji} [{symbol} - جان {tf}] {trigger_lbl} (مع {flt_label}_{ttf.upper()})"

                    t1_signal_ts = time.monotonic()
                    await _gann_open_trade(symbol, is_buy, lv, candles, reason=reason, tf=tf,
                                            initial_px=live_px, detect_time=detect_time, t1_signal_ts=t1_signal_ts,
                                            feed_source='ws', feed_age_ms=feed_age_ms, trigger_type=channel)
                    break  # this channel found its level for this tick -- other channels still get their own independent check
    except Exception as e:
        log_exception(f"_gann_tick_fire_check [{symbol}]", e)


def _is_market_hours_now() -> bool:
    """Rough broker session-hours check (from the real XAUUSD symbol spec:
    Sun 01:01 -> Fri 23:49 broker time, closed Saturday). Used only to
    decide whether a 60s tick silence is suspicious (worth a forced
    reconnect) or expected (weekend close) -- not used anywhere else."""
    offset = bot_state.get('broker_time_offset', 3)
    broker_now = datetime.now(timezone.utc) + timedelta(hours=offset)
    wd = broker_now.weekday()  # Monday=0 ... Sunday=6
    t = broker_now.time()
    if wd == 5:                                   # Saturday: fully closed
        return False
    if wd == 4 and t >= dtime(23, 49):             # Friday after close
        return False
    if wd == 6 and t < dtime(1, 1):                # Sunday before open
        return False
    return True

async def _force_full_reconnect(reason: str) -> None:
    """WebSocket watchdog escalation: unlike _lq_subscribe_symbol (which
    just re-sends a market-data subscription on the EXISTING connection
    object), this tears the streaming connection down and rebuilds it from
    scratch. Needed because a MetaApi streaming session can go "zombie" --
    connection_status still reports CONNECTED and re-subscribing throws no
    error, yet no more ticks ever arrive (observed during a broker daily
    rollover). The only reliable signal for that state is tick silence
    itself, which is what triggers this, not the SDK's own status flag."""
    global _metaapi_conn, _last_any_tick_ts
    c_log(f"WS WATCHDOG: forcing full reconnect -- {reason}")
    await set_connection_state(CONN_READ_ONLY, f"WS watchdog: {reason}")
    try:
        if _metaapi_conn is not None:
            try:
                await _metaapi_conn.close()
            except Exception as e:
                log_exception('_force_full_reconnect: close old connection', e)
        _metaapi_conn = _metaapi_account.get_streaming_connection()
        _metaapi_conn.add_synchronization_listener(_GannPriceListener())
        await _metaapi_conn.connect()
        await _metaapi_conn.wait_synchronized()
        for sym, on in bot_state['active_symbols'].items():
            if on:
                await _lq_subscribe_symbol(sym)
        _last_any_tick_ts = time.monotonic()  # don't re-trip the watchdog on the pre-reconnect silence
        c_log("WS WATCHDOG: reconnect successful, ticks should resume.")
        await set_connection_state(CONN_RUNNING, "WS watchdog: forced reconnect succeeded.")
        await send_tg_msg(f"🔁 <b>Watchdog: أعيد الاتصال تلقائياً بـ MetaApi</b>\nالسبب: {reason}")
    except Exception as e:
        log_exception('_force_full_reconnect', e)
        await send_tg_msg(f"🛑 <b>Watchdog: فشلت محاولة إعادة الاتصال التلقائي</b>\nالسبب الأصلي: {reason}\nالخطأ: {e}")


    """Subscribe one OANDA-format symbol's broker equivalent to live
    quotes. Safe to call multiple times (idempotent on the broker side);
    swallows failures so one bad symbol doesn't block the others."""
    if _metaapi_conn is None:
        return
    broker_sym = _resolve_broker_symbol(symbol)
    _broker_to_data_symbol[broker_sym] = symbol
    try:
        await _metaapi_conn.subscribe_to_market_data(broker_sym)
    except Exception as e:
        log_exception(f"_lq_subscribe_symbol [{symbol} -> {broker_sym}]", e)


def _lq_is_stale(symbol: str) -> bool:
    q = live_quotes.get(symbol)
    return q is None or (time.monotonic() - q['ts']) > _QUOTE_STALE_SECONDS


async def _lq_price_with_fallback(symbol: str) -> tuple[float | None, str, float | None]:
    """Returns (price, source, age_ms). Prefers the pushed WebSocket
    quote; falls back to the OANDA REST fetch (fetch_master_price) if
    the feed is missing/stale, so a temporary MetaApi hiccup degrades
    to the old behavior instead of silently blocking all touch checks."""
    q = live_quotes.get(symbol)
    if q is not None and (time.monotonic() - q['ts']) <= _QUOTE_STALE_SECONDS:
        return q['mid'], 'ws', round((time.monotonic() - q['ts']) * 1000)
    px = await fetch_master_price(symbol)
    return px, 'oanda_fallback', None

# Connection-state machine.
# RUNNING    : normal operation, new trades allowed.
# READ_ONLY  : sync with MetaAPI is degraded/unavailable. No new trades,
#              no destructive local state changes (Amnesia Prevention),
#              existing positions still managed if MT5 fallback price works.
# HALTED     : hard stop. New entries and order management both stop;
#              a human must intervene.
CONN_RUNNING   = 'RUNNING'
CONN_READ_ONLY = 'READ_ONLY'
CONN_HALTED    = 'HALTED'

_state_lock = asyncio.Lock()

async def set_connection_state(new_state: str, reason: str) -> None:
    async with _state_lock:
        old_state = bot_state.get('connection_state', CONN_RUNNING)
        if old_state == new_state:
            return
        bot_state['connection_state'] = new_state
        bot_state['connection_state_reason'] = reason
    logger.warning("Connection state: %s -> %s (%s)", old_state, new_state, reason)
    icon = {'RUNNING': '\u2705', 'READ_ONLY': '\U0001F7E1', 'HALTED': '\U0001F6D1'}.get(new_state, '\u2139')
    await send_tg_msg(f"{icon} <b>connection state changed: {old_state} -> {new_state}</b>\n{reason}")

_DAM_RESTRICTED_WINDOWS = [
    (dtime(7, 0),  dtime(9, 0)),   # European Open fakeouts
    (dtime(13, 0), dtime(14, 0)),  # Pre-US session turbulence
]

def _is_within_dam_restricted_window() -> bool:
    """DAM (Damascus / UTC+3) time-of-day filter. Based on backtest
    analysis, these windows carry enough market noise to invalidate Gann
    levels and stack losses, so new entries are skipped during them.
    Existing-position management (BE/TP/SL/closures) is NOT affected --
    this only blocks NEW trade dispatch, same scope as is_trading_allowed().
    Toggleable via bot_state['prot_dam_time_filter'] (default: on)."""
    if not bot_state.get('prot_dam_time_filter', True):
        return False
    dam_now = datetime.now(timezone.utc) + timedelta(hours=3)
    t = dam_now.time()
    return any(start <= t < end for start, end in _DAM_RESTRICTED_WINDOWS)

def is_trading_allowed() -> bool:
    """New order placement is only allowed when the connection state is
    fully healthy AND we're not inside a restricted DAM time window.
    Existing-position management (BE/TP/SL) is handled separately and is
    NOT gated by this, per the OANDA-degraded-mode rule."""
    if bot_state.get('connection_state', CONN_RUNNING) != CONN_RUNNING:
        return False
    if _is_within_dam_restricted_window():
        return False
    return True

async def init_metaapi():
    """Startup order is fixed:
       1) Reconstruct state from the persistence file (works even if the
          broker/API is completely unreachable).
       2) Only THEN attempt to talk to MetaAPI / the market.
    """
    global _metaapi, _metaapi_account, _metaapi_conn, _last_any_tick_ts

    load_bot_persistence()
    if bot_state.get('_persistence_load_failed'):
        await set_connection_state(
            CONN_READ_ONLY,
            "Startup persistence file was present but unreadable. Starting READ_ONLY until a human "
            "confirms the true broker state and clears this manually."
        )

    try:
        _metaapi = MetaApi(METAAPI_TOKEN)
        _metaapi_account = await _metaapi.metatrader_account_api.get_account(ACCOUNT_ID)
        if _metaapi_account.state == 'DEPLOYED' and _metaapi_account.connection_status == 'CONNECTED':
            # Streaming connection (not RPC): this is what gives us pushed,
            # real-time quotes via the terminal-state sync -- an RPC
            # connection only supports one-off trade commands and doesn't
            # maintain a live quote cache at all. One streaming connection
            # handles both quotes (via the listener) and order placement,
            # so nothing else needs a second connection.
            _metaapi_conn = _metaapi_account.get_streaming_connection()
            _metaapi_conn.add_synchronization_listener(_GannPriceListener())
            await _metaapi_conn.connect()
            await _metaapi_conn.wait_synchronized()
            for sym, on in bot_state['active_symbols'].items():
                if on:
                    await _lq_subscribe_symbol(sym)
            c_log("MetaAPI Persistent Streaming Connection established (live quotes subscribed).")
            _last_any_tick_ts = time.monotonic()
            await set_connection_state(CONN_RUNNING, "MetaAPI connected and synchronized at startup.")
        else:
            c_log(f"MetaAPI account not deployed/connected at startup (state={_metaapi_account.state}, "
                  f"conn={_metaapi_account.connection_status}).")
            await set_connection_state(CONN_READ_ONLY, "MetaAPI account is not DEPLOYED/CONNECTED at startup.")
    except Exception as e:
        log_exception("init_metaapi", e)
        await set_connection_state(CONN_READ_ONLY, f"MetaAPI init failed at startup: {e}")

DATA_DIR = os.environ.get('PERSISTENT_DATA_PATH', '/app/data')
os.makedirs(DATA_DIR, exist_ok=True)
PERSISTENCE_FILE = os.path.join(DATA_DIR, 'bot_persistence.json')
TEMP_PERSISTENCE_FILE = os.path.join(DATA_DIR, 'bot_persistence.tmp')
PRESETS_FILE = os.path.join(DATA_DIR, 'presets.json')
TEMP_PRESETS_FILE = os.path.join(DATA_DIR, 'presets.tmp')

# Live runtime fields that a preset must never capture or restore --
# includes gann_last_h1_time/gann_cycle_started_at (raw datetime objects,
# not JSON-serializable at all) plus other in-flight state that belongs to
# whatever is currently running, not to a saved settings snapshot.
_PRESET_EXCLUDED_KEYS = {
    'gann_levels', 'gann_level_status', 'gann_cycle_active', 'gann_open_trades',
    'gann_last_h1_time', 'gann_cycle_started_at', 'auto_trade',
}

# Event-loop I/O offloading (v9.5): json.dump + os.fsync + os.replace are
# blocking syscalls. Calling them directly from async code stalls the
# ENTIRE event loop for their duration -- every other coroutine (candle
# fetches, BE checks, Telegram alerts, callback handling) waits behind a
# single disk write. The fix: build the snapshot synchronously (cheap,
# pure in-memory dict work, no yield point, so it's still atomic w.r.t.
# bot_state), then push the actual disk I/O to a worker thread via
# asyncio.to_thread and await it there. Writes are additionally serialized
# with an asyncio.Lock so two saves in flight can't interleave writes to
# the shared .tmp path before either os.replace runs.
_persistence_write_lock = asyncio.Lock()

def _write_persistence_file_sync(data: dict) -> None:
    """Pure blocking I/O, no bot_state access -- safe to run in a thread."""
    with open(TEMP_PERSISTENCE_FILE, 'w') as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(TEMP_PERSISTENCE_FILE, PERSISTENCE_FILE)

async def save_bot_persistence() -> None:
    """Atomic write: full operational state AND full settings, so a hard
    restart reconstructs the bot's world exactly -- not just open trades
    and Gann cycle state, but every user-configured setting (lot size,
    protection limits, anchor timeframe, filters, TP/SL config, etc).

    Deliberately exclude-list based, not include-whitelist based: the
    previous version only ever saved a fixed list of live trade-state
    fields, which meant lot size, protection dd/profit limits, the anchor
    timeframe, and effectively every other setting were NEVER actually
    persisted -- even though save_bot_persistence() was correctly being
    called after every mutation. An exclude-list means any new setting
    added later is persisted automatically instead of silently dropped
    until someone remembers to add it to a whitelist.
    """
    try:
        # Fields that either aren't JSON-serializable or are purely
        # transient/regenerated-on-render -- everything else in bot_state
        # is a real setting and gets saved.
        TOP_LEVEL_EXCLUDE = {'connection_obj', 'menu_button_map', 'timeframes',
                              'is_backtesting', 'live_connected', 'last_poll_ok', 'symbol_state',
                              # 'status' is a dead legacy field predating the connection_state
                              # machine -- nothing in the current code ever writes to it, but a
                              # stale value loaded from an old persistence file used to silently
                              # kill the entire scanner/cycle-manager/reconciliation loop with
                              # zero error message. Never restore it; always fixed at 'RUNNING'.
                              'status',
                              # Diagnostic-only rolling buffer -- large, purely informational,
                              # and reset-on-restart is fine. Never persist or reload it.
                              'diag_log'}

        symbol_snapshot = {}
        for sym in bot_state['active_symbols']:
            ss = bot_state['symbol_state'][sym]
            snap = {k: v for k, v in ss.items() if k not in ('gann_last_h1_time', 'gann_cycle_started_at')}
            snap['gann_last_h1_time'] = ss.get('gann_last_h1_time').isoformat() if ss.get('gann_last_h1_time') else None
            snap['gann_cycle_started_at'] = ss.get('gann_cycle_started_at').isoformat() if ss.get('gann_cycle_started_at') else None
            symbol_snapshot[sym] = snap

        data = {
            'schema_version': 3,
            'symbol_state': symbol_snapshot,
        }
        for k, v in bot_state.items():
            if k not in TOP_LEVEL_EXCLUDE:
                data[k] = v
        data['live_daily_date'] = str(bot_state.get('live_daily_date'))
    except Exception as e:
        log_exception("save_bot_persistence (snapshot phase)", e)
        return

    try:
        async with _persistence_write_lock:
            await asyncio.to_thread(_write_persistence_file_sync, data)
    except Exception as e:
        # Persistence failing is itself a critical-path failure: if we can't
        # save state, a crash right now means real, silent data loss on
        # open positions. Escalate loudly instead of swallowing it.
        log_exception("save_bot_persistence (write phase)", e)
        c_log(f"CRITICAL: Persistence Save Error -- open trade state may not survive a restart: {e}")

def load_bot_persistence():
    if not os.path.exists(PERSISTENCE_FILE):
        c_log("No persistence file found -- starting fresh (expected on first boot).")
        return
    try:
        with open(PERSISTENCE_FILE, 'r') as f:
            data = json.load(f)

        TOP_LEVEL_EXCLUDE = {'connection_obj', 'menu_button_map', 'timeframes',
                              'is_backtesting', 'live_connected', 'last_poll_ok', 'symbol_state',
                              # 'status' is a dead legacy field predating the connection_state
                              # machine -- nothing in the current code ever writes to it, but a
                              # stale value loaded from an old persistence file used to silently
                              # kill the entire scanner/cycle-manager/reconciliation loop with
                              # zero error message. Never restore it; always fixed at 'RUNNING'.
                              'status',
                              'diag_log'}
        for k, v in data.items():
            # Only restore keys that already exist in bot_state's default
            # shape -- never let a saved file inject brand-new top-level
            # keys the current code doesn't define.
            if k in bot_state and k not in TOP_LEVEL_EXCLUDE and k != 'live_daily_date':
                bot_state[k] = v

        saved_date = data.get('live_daily_date')
        if saved_date and saved_date != 'None':
            bot_state['live_daily_date'] = datetime.strptime(saved_date, '%Y-%m-%d').date()

        symbol_state_data = data.get('symbol_state')
        if symbol_state_data is not None:
            for sym, snap in symbol_state_data.items():
                if sym not in bot_state['symbol_state']:
                    continue
                ss = bot_state['symbol_state'][sym]
                for k, v in snap.items():
                    if k in ('gann_last_h1_time', 'gann_cycle_started_at'):
                        continue
                    if k in ss:  # same safety principle as top-level: only restore known fields
                        ss[k] = v
                lh1 = snap.get('gann_last_h1_time')
                ss['gann_last_h1_time'] = pd.Timestamp(lh1).to_pydatetime() if lh1 else None
                csa = snap.get('gann_cycle_started_at')
                ss['gann_cycle_started_at'] = pd.Timestamp(csa).to_pydatetime() if csa else None
        else:
            # Backward-compat with the oldest schema (open trades only).
            for sym, trades in data.get('gann_open_trades', {}).items():
                if sym in bot_state['symbol_state']:
                    bot_state['symbol_state'][sym]['gann_open_trades'] = trades

        c_log("Bot state restored from persistence file (settings, open trades, Gann cycle state, daily PnL).")
    except Exception as e:
        # If the persistence file is corrupt we must not silently pretend
        # we're starting clean while real broker positions may still be
        # open. Flag it; init_metaapi/main will use this to force READ_ONLY.
        log_exception("load_bot_persistence", e)
        c_log(f"CRITICAL: Persistence file exists but failed to load ({e}). "
              f"Bot will start in READ_ONLY to avoid trading blind.")
        bot_state['_persistence_load_failed'] = True

bot_state: dict = {
    'status':           'RUNNING',
    'connection_state': 'RUNNING',
    'connection_state_reason': '',
    'symbol':           'XAUUSD',
    'live_connected':   False,
    'connection_obj':   None,
    'chat_id':          None,
    'last_update_id':   0,
    'is_backtesting':   False,
    'is_live_twin_running': False,
    'timeframes':       _TFS,

    # ── Live execution mode (Instant / Close / Hybrid) ──
    # Default 'instant' == the scanner's existing, unchanged behavior
    # (check live_px against the level margin and fire immediately).
    # Nothing about current live trading changes until this is toggled.
    'gann_execution_mode': 'instant',
    'lt_latency_ms_min': 160,   # measured Railway-deployment -> broker round-trip ping
    'lt_latency_ms_max': 200,   # (update again if a future diagnostic run measures differently)
    'gann_spike_limit_pts': 20,   # hybrid mode: block entry if live_px has moved this many points past the last closed candle's close

    # ── Live-Twin Engine (realistic execution simulator) ──
    # Baseline spread taken from a live MT5/OANDA tick snapshot on
    # 2026-07-13 during the late-night (low-liquidity) session:
    # Bid 4112.28 / Ask 4112.62 -> 0.34 USD (34 points at tick=0.01).
    # This is the QUIET-SESSION floor; session/volatility multipliers
    # scale it up or down from here, they never invent a new baseline.
    'lt_mode': 'realistic',        # 'realistic' or 'idealized' (idealized == old run_gann_backtest, zero friction, kept as A/B baseline)
    'lt_base_spread_usd': 0.34,
    'lt_friction': {
        'spread':    True,   # dynamic session/volatility spread model
        'slippage':  True,   # asymmetric, range-scaled slippage
        'latency':   True,   # 200-800ms signal-to-fill delay
        'commission': True,  # per-lot round-turn commission
        'gaps':      True,   # weekend/rollover gap risk
        'rejection': True,   # requote/rejection probability in volatility spikes
    },
    # Calibrated from the actual XAUUSD broker spec (MT5 symbol properties
    # screenshot): "Commissions: 0-1000 -> 5 USD per lot, Instant by deal
    # volume, in deals" means 5 USD PER DEAL, i.e. per side -- a round-turn
    # trade (open + close) costs 10, not the old flat guess of 7.
    'lt_commission_per_lot': 10.0,  # USD round-turn per 1.0 lot (5 open + 5 close, per real broker spec)
    # Swap was previously a single flat value applied to both directions,
    # which is wrong: the real broker spec shows swap long/short are wildly
    # asymmetric (Swap long: -93.1728, Swap short: +21.6848, in points), and
    # Wednesday carries a 3x multiplier (standard weekend-rollover
    # compensation). Points -> USD/lot conversion uses tick size x contract
    # size from the same spec (0.01 x 100 = $1/point/lot for XAUUSD).
    # Re-derive these two numbers yourself if your broker's spec differs.
    'lt_swap_long_per_lot_night': -93.17,   # USD per lot per night held, BUY positions
    'lt_swap_short_per_lot_night': 21.68,   # USD per lot per night held, SELL positions
    'lt_swap_wednesday_multiplier': 3.0,    # applied when the rollover date is a Wednesday
    'lt_swap_per_lot_night': -6.5,  # legacy fallback only, kept for old configs; no longer used directly
    'lt_rejection_prob': 0.015,     # probability a signal is rejected/requoted during an ATR spike bar

    
    'menu_button_map': {},
    'last_poll_ok':     0.0,
    'live_daily_realized': 0.0,
    'live_daily_date': None,
    'live_daily_hit': False,

    # ── Gann Levels Engine ──
    'active_symbols': {s: (s == 'XAU_USD') for s in AVAILABLE_SYMBOLS},
    'ui_selected_symbol': 'XAU_USD',
    'symbol_state': {s: {
        'gann_levels': [],
        'gann_level_status': {},
        'gann_close_used': None,
        'gann_last_h1_time': None,
        'gann_cycle_active': False,
        'gann_cycle_started_at': None,
        'gann_open_trades': {},
        'auto_trade': False,
        'lot_size': 0.05,
        'gann_cycle_hours': 1,
        'gann_zone_filter': 'star',  
        'gann_entry_mode': 'touch_trend', 
        'trend_filter_type': 'ema',     
        'trend_vwap_period': 100,
        'trend_ema_period': 60,
        'trend_timeframe': '1h',    
        'break_even_enabled': False,
        'gann_be_trigger_points': 40,
        'gann_monitor_tfs': {tf: (tf in ['5m', '10m', '15m', '20m', '30m', '1h', '4m', '6m', '2h', '1m', '2m', '3m']) for tf in _TFS},
        'gann_touch_margin_pts': 5,       
        'gann_tpsl_mode': 'fixed', 
        'gann_tp_points': 70,
        'gann_sl_points': 110,
        'gann_tp_per_tf': {tf: 0 for tf in _TFS},
        'gann_sl_per_tf': {tf: 0 for tf in _TFS},
        'gann_atr_period': 14,
        'gann_atr_sl_mult': 1.5,
        'gann_atr_tp_mult': 2,
    } for s in AVAILABLE_SYMBOLS},
    
# ── Filter Type (star, star_fan, all) ──
    
    
    
    # ── Trend Filters ──
    
    
    
    
    
    # ── Trade Management ──
    
    'prot_daily_dd_usd':      200,
    'prot_daily_profit_usd':  150,
    'prot_true_sync': True,
    'prot_cost_be': True,
    # Max allowed execution deviation (MetaApi "slippage", in broker points)
    # for market orders. If the broker can't fill within this many points of
    # our intended price, the order is rejected (ORDER_FILLING_FOK) instead
    # of being executed 20 pips away.
    'prot_max_slippage_points': 5,
    # Hard cap on simultaneously open trades per symbol, regardless of how
    # many timeframes are enabled or how many levels got touched at once.
    # When reached, remaining timeframes are skipped for that scan cycle
    # only -- already-open trades are left alone (Option A).
    'prot_max_concurrent_trades': 4,
    # Rolling in-memory log of every (symbol, timeframe) scan decision made
    # by the live scanner -- NOT just the instantaneous /diagnose snapshot.
    # Deliberately excluded from persistence (see TOP_LEVEL_EXCLUDE) since
    # it's diagnostic-only and would otherwise bloat the save file.
    'diag_log': [],
    # Full history of every CLOSED live/real trade, rich enough to rebuild an
    # Excel report matching the backtest's own format. Unlike diag_log this
    # IS persisted (not in TOP_LEVEL_EXCLUDE) -- it's the actual trade record,
    # not throwaway diagnostics, and must survive a bot restart.
    'live_trade_history': [],
    'prot_stale_filter': True,
    'prot_cycle_inval': True,
    'prot_cycle_inval_pts': 200,
    'gann_anchor_tf': '1h',
    'prot_allow_multi_tf':    True,

    # ── Broker/display time alignment ──
    # Hours to add to raw UTC (from OANDA/MetaApi) to reach the broker's
    # own server clock (what the user's MT5 terminal displays). Used to
    # align "last closed anchor candle" boundary detection to the SAME
    # wall-clock boundaries MT5 shows, not raw UTC ones. Default 3 =
    # Damascus/EET-DST-style broker offset.
    'broker_time_offset': 3,

    # ── Gann level calculation mode ──
    # 'static_h1'   : classic behavior -- levels are (re)anchored only when
    #                 a new anchor-tf (H1/H4) candle closes.
    # 'dynamic_live': ignore candle closes; recompute levels every
    #                 GANN_DYNAMIC_RECALC_MINUTES using the current live
    #                 streamed price. Toggle in the Telegram protection menu.
    'gann_calculation_mode': 'static_h1',

    
    
    
    
    
    
    
    
    
    
    
    
}


DAM_OFF = timedelta(hours=3)
def _utc_to_dam(dt) -> datetime:
    if isinstance(dt, pd.Timestamp): dt = dt.to_pydatetime()
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return dt + DAM_OFF


# ─────────────────────────────────────────────────────────────
# UNIFIED CORE LOGIC (V9.4)
# ─────────────────────────────────────────────────────────────
def core_eval_break_even(is_buy: bool, entry: float, current_px: float, pip_value: float, be_pts: int, atr_period: int, cost_be: bool) -> float | None:
    be_dist = be_pts * pip_value
    if (is_buy and current_px >= entry + be_dist) or (not is_buy and current_px <= entry - be_dist):
        be_margin = (atr_period * 0.1 * pip_value) if cost_be else 0.0
        return (entry + be_margin) if is_buy else (entry - be_margin)
    return None

def core_eval_outcome(is_buy: bool, current_px: float, tp: float, sl: float) -> str | None:
    if is_buy:
        if current_px >= tp: return 'WIN ✅'
        if current_px <= sl: return 'LOSS ❌'
    else:
        if current_px <= tp: return 'WIN ✅'
        if current_px >= sl: return 'LOSS ❌'
    return None
    
# ─────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────
# TELEGRAM HELPERS
# ─────────────────────────────────────────────────────────────
async def _tg_post(url: str, **kwargs) -> bool:
    try:
        sess = get_http()
        async with sess.post(url, **kwargs) as resp:
            if resp.status != 200:
                body = await resp.text()
                c_log(f"Telegram API call failed ({resp.status}) for {url}: {body[:300]}")
            return resp.status == 200
    except Exception as e:
        # This carries HALT/READ_ONLY escalation alerts, so a silent
        # failure here means the operator never finds out something broke.
        log_exception(f"_tg_post [{url}]", e)
        return False

def _to_reply_kbd(inline_kbd: dict):
    rows = []; bmap = {}
    for row in inline_kbd.get('inline_keyboard', []):
        new_row = []
        for btn in row:
            text = btn['text']; cb = btn.get('callback_data', 'noop')
            if text in bmap and bmap[text] != cb and cb != 'noop' and bmap[text] != 'noop':
                # This is exactly the bug class that caused the loss/profit
                # buttons to collide: two DIFFERENT actions sharing the same
                # button text, silently overwriting each other in the map
                # that resolves a tapped label back to an action. Every
                # button's text must be unique within a single keyboard.
                c_log(f"BUTTON LABEL COLLISION: '{text}' maps to both '{bmap[text]}' and '{cb}' -- "
                      f"the second silently wins and the first becomes untappable. Fix the keyboard's labels.")
            new_row.append({'text': text}); bmap[text] = cb
        rows.append(new_row)
    return {'keyboard': rows, 'resize_keyboard': True, 'is_persistent': True, 'input_field_placeholder': 'اختر من القائمة...'}, bmap

async def send_tg_msg(text: str, reply_markup: dict = None) -> None:
    if not bot_state['chat_id']: return
    if reply_markup and 'inline_keyboard' in reply_markup:
        reply_markup, bmap = _to_reply_kbd(reply_markup); bot_state['menu_button_map'] = bmap
    payload = {'chat_id': bot_state['chat_id'], 'text': text, 'parse_mode': 'HTML'}
    if reply_markup: payload['reply_markup'] = reply_markup
    await _tg_post(f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage', json=payload)

async def edit_tg_msg(chat_id, message_id, text, reply_markup=None) -> None:
    payload = {'chat_id': chat_id, 'message_id': message_id, 'text': text, 'parse_mode': 'HTML'}
    if reply_markup: payload['reply_markup'] = reply_markup
    await _tg_post(f'https://api.telegram.org/bot{TG_TOKEN}/editMessageText', json=payload)

async def _show(chat_id, msg_id, text: str, reply_markup: dict = None) -> None:
    if msg_id: await edit_tg_msg(chat_id, msg_id, text, reply_markup)
    else: await send_tg_msg(text, reply_markup)

async def answer_callback(cbq_id: str) -> None:
    await _tg_post(f'https://api.telegram.org/bot{TG_TOKEN}/answerCallbackQuery', json={'callback_query_id': cbq_id})

TG_CAPTION_LIMIT = 1024  # Telegram hard limit for document/photo captions

async def send_tg_document(file_path: str, caption: str) -> None:
    if not bot_state['chat_id']: return
    try:
        # A caption over Telegram's limit doesn't get truncated by the API --
        # the whole sendDocument call is rejected, so the file itself would
        # never arrive. Keep the merged single-message intent when it fits;
        # fall back to a short caption + a separate full-text message only
        # when it doesn't.
        doc_caption = caption
        overflow_text = None
        if len(caption) > TG_CAPTION_LIMIT:
            doc_caption = caption[:TG_CAPTION_LIMIT - 20].rstrip() + "\n... (تابع أدناه)"
            overflow_text = caption

        with open(file_path, 'rb') as f:
            data = aiohttp.FormData()
            data.add_field('chat_id',  str(bot_state['chat_id']))
            data.add_field('document', f, filename=os.path.basename(file_path))
            data.add_field('caption',  doc_caption)
            await _tg_post(f'https://api.telegram.org/bot{TG_TOKEN}/sendDocument', data=data)

        if overflow_text:
            await send_tg_msg(overflow_text)
    except Exception as e:
        log_exception(f"send_tg_document [{file_path}]", e)

# ─────────────────────────────────────────────────────────────
# OANDA FETCHER 
# ─────────────────────────────────────────────────────────────
_OANDA_GRAN = {'1m':'M1','2m':'M2','3m':'M3','4m':'M4','5m':'M5','6m':'M6','10m':'M10','15m':'M15','20m':'M20','30m':'M30','1h':'H1','2h':'H2'}
_oanda_sem: asyncio.Semaphore | None = None
def _get_oanda_sem() -> asyncio.Semaphore:
    global _oanda_sem
    if _oanda_sem is None: _oanda_sem = asyncio.Semaphore(3)
    return _oanda_sem

def _safe_float(value, default: float = 0.0) -> float:
    """Closes the `.get(key, default)` null trap: dict.get()'s default only
    applies when the key is MISSING. If MetaAPI returns the key present
    with an explicit `null` (-> None in Python), .get() happily returns
    None, and a later `+=` or arithmetic op on it raises TypeError. This
    coerces None/non-numeric/NaN/inf values to `default` instead of
    raising, at every point real MetaAPI numeric fields are consumed."""
    if value is None:
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float('inf'), float('-inf')):  # NaN / inf guard
        return default
    return f

def _validated_candle(c: dict, symbol: str, granularity_str: str) -> dict | None:
    """Defensive boundary for external market data. OANDA/MetaAPI are not
    contractually guaranteed to always return well-typed floats -- a
    transient glitch can hand back None, a string, or a missing key.
    Returns a clean candle dict, or None if this single candle is bad.
    Never raises: a bad candle should be skipped, not take down the whole
    fetch (or the caller's while-True loop) with it."""
    try:
        mid = c.get('mid')
        if not isinstance(mid, dict):
            raise ValueError(f"missing/invalid 'mid' field: {mid!r}")
        raw_time = c.get('time')
        if not raw_time:
            raise ValueError("missing 'time' field")

        o = float(mid['o']); h = float(mid['h']); l = float(mid['l']); c_ = float(mid['c'])
        vol = float(c.get('volume', 1.0) or 1.0)

        for v in (o, h, l, c_, vol):
            if v != v or v in (float('inf'), float('-inf')):
                raise ValueError(f"non-finite value in candle: {v!r}")

        return {
            'time': pd.Timestamp(raw_time).tz_convert('UTC'),
            'open': o, 'high': h, 'low': l, 'close': c_, 'volume': vol,
        }
    except (TypeError, ValueError, KeyError) as e:
        log_exception(f"_validated_candle [{symbol} {granularity_str}] -- skipping malformed candle", e)
        return None

async def fetch_candles(symbol: str, granularity_str: str, count: int = 5000, end_time: datetime = None) -> list:
    gran_str = _OANDA_GRAN.get(granularity_str, 'M1'); fetch_count = min(count, 120000)  
    collected = []; remaining = fetch_count
    headers = {'Authorization': f'Bearer {OANDA_TOKEN}', 'Content-Type':  'application/json'}
    url = f'{OANDA_BASE_URL}/instruments/{symbol}/candles'
    current_end = end_time if end_time else datetime.now(timezone.utc)

    sem = _get_oanda_sem()
    async with sem:
        while remaining > 0:
            chunk = min(remaining, 5000)
            params = {'granularity': gran_str, 'count': chunk, 'to': current_end.strftime('%Y-%m-%dT%H:%M:%S.000000000Z'), 'price': 'M'}
            candles = []
            for attempt in range(3):
                try:
                    async with get_http().get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                        if resp.status != 200:
                            if attempt == 2: break
                            await asyncio.sleep(2 ** attempt)
                            continue
                        data = await resp.json(); candles = data.get('candles', []); break
                except Exception as e:
                    log_exception(f"fetch_candles [{symbol} {granularity_str}] attempt {attempt+1}/3", e)
                    await asyncio.sleep(2 ** attempt)

            if not candles: break
            complete = [c for c in candles if c.get('complete', True)]
            if not complete: break

            formatted = []
            for c in complete:
                vc = _validated_candle(c, symbol, granularity_str)
                if vc is not None:
                    formatted.append(vc)

            if not formatted:
                c_log(f"fetch_candles [{symbol} {granularity_str}]: entire chunk failed validation, aborting fetch.")
                break

            collected = formatted + collected; remaining -= len(complete)
            earliest = pd.Timestamp(complete[0]['time']).tz_convert('UTC')
            current_end = earliest.to_pydatetime() - timedelta(seconds=1)
            if len(complete) < chunk: break
            await asyncio.sleep(0.2)
    return collected

async def fetch_master_price(symbol: str) -> float | None:
    """Single Source of Truth for the CURRENT live price.

    Call this exactly ONCE per symbol per scanner cycle, then reuse the
    returned value for every enabled timeframe's touch-distance check.

    Why this exists: a timeframe's own last candle close is NOT "the
    current price" for anything above 1m -- a 30m candle's close can be up
    to ~30 minutes stale. Asking OANDA separately per-timeframe and using
    each tf's own close as "live price" is exactly what caused the same
    instant to read as e.g. 4067 on 1m and 4073 on 30m during a volatile
    spike, and it also multiplies OANDA requests per cycle (contributing
    to "Insufficient data from OANDA" failures under load). Timeframes
    should still be fetched separately for their own historical
    closes/EMAs/ATR -- just never for "what is the price right now".

    count=2 (not 1): OANDA's most recent candle for 'to=now' is very often
    still the in-progress (incomplete) one, and fetch_candles() drops
    incomplete candles entirely. count=1 would then frequently return an
    EMPTY list and report "insufficient data" even though OANDA itself is
    perfectly healthy. count=2 guarantees at least one genuinely
    completed, very recent candle to use.
    """
    mc = await fetch_candles(symbol, '1m', count=2)
    if not mc:
        c_log(f"fetch_master_price [{symbol}]: no 1m data from OANDA this cycle -- "
              f"skipping touch checks for this symbol rather than risk a stale/desynced price.")
        return None
    return float(mc[-1]['close'])

# ─────────────────────────────────────────────────────────────
# GANN LEVELS & FAN ENGINE (⭐ & ⭐🌀)
# ─────────────────────────────────────────────────────────────
GANN_TFC_H1 = 0.02

# تصنيف دقيق للمستويات (مطابق تماماً لتطبيق OTC-Calculator المرجعي):
# star = المستويات الأصلية القوية (AIMP في الأصل: انحصرت في 0.0833, 0.5, 1.0 فقط)
# fan  = "موازي للمروحة" -- في الأصل هذا يعني معامل الزاوية 0.125 (8x1) حصراً،
#        وليس أي معامل آخر. القائمة الأصلية (ACOEF) فيها 11 معامل فقط --
#        لا يوجد 3.0 ولا 8.0 إطلاقاً.
GANN_COEFS = [
    {'c': 0.0208, 'star': False, 'fan': False},
    {'c': 0.0417, 'star': False, 'fan': False},
    {'c': 0.0625, 'star': False, 'fan': False},
    {'c': 0.0833, 'star': True,  'fan': False},
    {'c': 0.125,  'star': False, 'fan': True},  # 8x1 -- الوحيد الذي يُعتبر "موازي للمروحة"
    {'c': 0.25,   'star': False, 'fan': False},
    {'c': 0.333,  'star': False, 'fan': False},
    {'c': 0.5,    'star': True,  'fan': False},
    {'c': 1.0,    'star': True,  'fan': False},
    {'c': 2.0,    'star': False, 'fan': False},
    {'c': 4.0,    'star': False, 'fan': False},
]

def _anchor_hours() -> int:
    """Hour-equivalent of the selected Gann anchor timeframe."""
    return 4 if bot_state.get('gann_anchor_tf', '1h') == '4h' else 1

def _anchor_label() -> str:
    """Display label ('H1'/'H4') matching the selected anchor timeframe --
    used everywhere a message previously hardcoded the literal text 'H1'."""
    return bot_state.get('gann_anchor_tf', '1h').upper()

def gann_calc_levels(symbol: str, close: float) -> list[dict]:
    levels = []
    anchor_tf = bot_state.get('gann_anchor_tf', '1h')
    multiplier = GANN_TFC_H1 * 2.0 if anchor_tf == '4h' else GANN_TFC_H1
    
    for i, item in enumerate(GANN_COEFS):
        offset = close * item['c'] * multiplier
        prec = SYMBOL_INFO[symbol]['prec']
        up = round(close + offset, prec)
        dn = round(close - offset, prec)
        
        # التسميات الدقيقة
        up_lbl = "مقاومة"
        dn_lbl = "دعم"
        if item['star'] and not item['fan']:
            up_lbl = "مقاومة ⭐"
            dn_lbl = "دعم ⭐"
        elif item['star'] and item['fan']:
            up_lbl = "مقاومة ⭐"
            dn_lbl = "دعم ⭐"
        elif item['fan']:
            up_lbl = "مقاومة موازية للمروحة 🌀"
            dn_lbl = "دعم موازي للمروحة 🌀"
            
        levels.append({'key': f'up_{i}', 'price': up, 'dir': 'up', 'star': item['star'], 'fan': item['fan'], 'label': up_lbl})
        if dn > 0:
            levels.append({'key': f'dn_{i}', 'price': dn, 'dir': 'dn', 'star': item['star'], 'fan': item['fan'], 'label': dn_lbl})
            
    levels.append({'key': 'ref', 'price': round(close, SYMBOL_INFO[symbol]['prec']), 'dir': 'ref', 'star': False, 'fan': False, 'label': f'إغلاق {_anchor_label()}'})
    levels.sort(key=lambda x: x['price'], reverse=True)
    return levels

def gann_active_levels(symbol: str) -> list[dict]:
    sym_state = bot_state['symbol_state'][symbol]
    lv = [l for l in bot_state['symbol_state'][symbol]['gann_levels'] if l['dir'] != 'ref']
    f = sym_state['gann_zone_filter']
    if f == 'star': return [l for l in lv if l['star']]
    elif f == 'star_fan': return [l for l in lv if l['star'] or l['fan']]
    return lv

def _gann_tf_tp(symbol: str, tf: str) -> int:
    sym_state = bot_state['symbol_state'][symbol]
    v = sym_state['gann_tp_per_tf'].get(tf, 0)
    return v if v > 0 else sym_state['gann_tp_points']

def _gann_tf_sl(symbol: str, tf: str) -> int:
    sym_state = bot_state['symbol_state'][symbol]
    v = sym_state['gann_sl_per_tf'].get(tf, 0)
    return v if v > 0 else sym_state['gann_sl_points']

def _gann_atr(candles: list, period: int) -> float | None:
    if len(candles) < period + 1: return None
    df = pd.DataFrame(candles[-(period + 50):])
    df['prev_close'] = df['close'].shift(1)
    tr = pd.concat([
        df['high'] - df['low'],
        (df['high'] - df['prev_close']).abs(),
        (df['low']  - df['prev_close']).abs(),
    ], axis=1).max(axis=1)
    val = tr.rolling(period).mean().iloc[-1]
    return float(val) if not pd.isna(val) else None

def _gann_calc_tpsl(symbol: str, entry: float, is_buy: bool, candles: list, tf: str = '') -> tuple[float, float]:
    sym_state = bot_state['symbol_state'][symbol]
    pv = SYMBOL_INFO[symbol]['pip_value']
    prec = SYMBOL_INFO[symbol]['prec']
    if sym_state['gann_tpsl_mode'] == 'atr':
        atr = _gann_atr(candles, sym_state['gann_atr_period'])
        if not atr: atr = _gann_tf_sl(symbol, tf) * pv
        sl_dist = atr * sym_state['gann_atr_sl_mult']
        tp_dist = atr * sym_state['gann_atr_tp_mult']
    else:
        sl_dist = _gann_tf_sl(symbol, tf) * pv
        tp_dist = _gann_tf_tp(symbol, tf) * pv
    if is_buy: return round(entry + tp_dist, prec), round(entry - sl_dist, prec)
    return round(entry - tp_dist, prec), round(entry + sl_dist, prec)

def _last_closed_anchor_time_utc(anchor_hours: int, offset_hours: float, now_utc: datetime) -> datetime:
    """UTC timestamp of the OPEN of the most recently fully-closed anchor-tf
    bucket, computed against BROKER server time (UTC + broker_time_offset)
    rather than raw UTC. For a whole-hour offset this makes no difference
    to a 1h anchor (hourly boundaries land on the same instants in any
    whole-hour-offset zone), but it matters for a 4h anchor: OANDA's raw-UTC
    4h grid (00/04/08/12/16/20 UTC) is NOT the same set of instants as the
    broker's UTC+offset 4h grid, so blindly trusting OANDA's own bucketing
    can hand the bot a Gann anchor close that doesn't match what closes on
    the user's MT5 terminal."""
    broker_now = now_utc + timedelta(hours=offset_hours)
    floored = broker_now.replace(minute=0, second=0, microsecond=0)
    bucket_start_hour = (floored.hour // anchor_hours) * anchor_hours
    bucket_start_broker = floored.replace(hour=bucket_start_hour)
    return bucket_start_broker - timedelta(hours=offset_hours)

async def _gann_fetch_last_closed_anchor(symbol: str) -> dict | None:
    anchor_tf = bot_state.get('gann_anchor_tf', '1h')
    anchor_hours = _anchor_hours()
    offset = bot_state.get('broker_time_offset', 3)
    target_close_utc = _last_closed_anchor_time_utc(anchor_hours, offset, datetime.now(timezone.utc))
    # Fetch a small pad of extra candles (not just 2) since the broker-
    # aligned boundary can fall a bucket or two behind OANDA's own most
    # recent closed candle once the offset shifts the grid.
    candles = await fetch_candles(symbol, anchor_tf, count=anchor_hours + 6)
    if not candles: return None
    candles = sorted(candles, key=lambda c: c['time'])
    # Pick the newest candle whose close does not exceed the broker-aligned
    # boundary -- NOT simply candles[-1], which is only correct when
    # OANDA's raw-UTC grid happens to coincide with the broker's grid.
    eligible = [c for c in candles if c['time'].to_pydatetime() <= target_close_utc]
    return eligible[-1] if eligible else candles[-1]

def _gann_fmt_levels_msg(symbol: str, close: float) -> str:
    sym_state = bot_state['symbol_state'][symbol]
    lines = []
    for l in bot_state['symbol_state'][symbol]['gann_levels']:
        if l['dir'] == 'ref':
            lines.append(f"➖ <b>{l['price']:.2f}</b>  (إغلاق {_anchor_label()})")
            continue
        
        icon = '🔴' if l['dir'] == 'up' else '🟢'
        lines.append(f"{icon} {l['price']:.2f}  {l['label']}")
        
    f_mode = sym_state['gann_zone_filter']
    if f_mode == 'star': filt = '⭐ المستويات الأصلية القوية فقط'
    elif f_mode == 'star_fan': filt = '⭐🌀 القوية + الموازية للمروحة'
    else: filt = '📋 كل المستويات (مخاطرة)'
    
    flt_trend = sym_state['trend_filter_type'].upper()
    if flt_trend == 'BOTH': flt_trend = 'VWAP + EMA'
    
    mode = f'لمس مباشر + فلتر ({flt_trend}_{sym_state["trend_timeframe"].upper()})' if sym_state['gann_entry_mode'] == 'touch_trend' else 'لمس أعمى (بدون فلتر)'
    return (f"📐 <b>سلّم جان (المروحة) — دورة جديدة</b>\n"
            f"إغلاق {_anchor_label()}: <b>{close:.2f}</b>\n\n"
            f"مدة المراقبة: {sym_state['gann_cycle_hours']}س  |  فلتر: {filt}\nالدخول: {mode}\n\n\n"
            + '\n'.join(lines))

_consecutive_real_order_failures = 0
_REAL_ORDER_FAILURE_HALT_THRESHOLD = 3
_last_scanner_error_alert_ts = 0.0

def _resolve_broker_symbol(symbol: str) -> str:
    """Resolve the OANDA-format data-feed symbol (e.g. 'XAU_USD') to the
    broker's actual MT5 symbol name for order execution. bot_state['symbol']
    (settable via /setsymbol) is the primary source of truth since brokers
    vary wildly in suffix conventions (XAUUSD, XAUUSDm, XAUUSD.a, GOLD...).
    A hard safety-net mapping is applied on top: if the configured value is
    missing or still looks like an unfixed raw OANDA-format symbol (i.e.
    still has the underscore), fall back to the confirmed-correct stripped
    form instead of sending a guaranteed-to-be-rejected symbol to the
    broker. The rest of the bot's data engine, fetches, and logs MUST
    continue using the OANDA-format symbol ('XAU_USD') -- this function's
    return value is ONLY for the MetaAPI execution payload."""
    configured = bot_state.get('symbol', '').strip()
    if not configured or '_' in configured:
        return symbol.replace('_', '')
    return configured

async def _gann_open_trade(symbol: str, is_buy: bool, level: dict, candles: list, reason: str, tf: str,
                            initial_px: float = None, detect_time: datetime = None, t1_signal_ts: float = None,
                            feed_source: str = None, feed_age_ms: float = None, trigger_type: str = None) -> None:
    global _consecutive_real_order_failures
    sym_state = bot_state['symbol_state'][symbol]

    # Order-management critical path: never place an order while the
    # connection state machine says we shouldn't be trading, or while
    # we're inside a restricted DAM time window.
    if not is_trading_allowed():
        if bot_state.get('connection_state', CONN_RUNNING) != CONN_RUNNING:
            c_log(f"Skipped entry [{symbol} {tf}]: connection_state={bot_state.get('connection_state')} "
                  f"({bot_state.get('connection_state_reason')})")
        else:
            c_log(f"Skipped entry [{symbol} {tf}]: inside restricted DAM trading window "
                  f"({datetime.now(timezone.utc) + timedelta(hours=3):%H:%M} DAM).")
        return

    try:
        # ── Re-verify price at execution time (Point 3) ──
        # Trades within the same scan cycle open sequentially, one timeframe
        # at a time. During a fast/volatile move, the price can drift far
        # from the level between the first and last order in the same
        # batch (this is exactly how one support touch ended up opening
        # entries $1-9 apart from each other). Re-fetch a fresh price right
        # before this specific order goes out, and re-check it's still
        # actually near the level -- not the stale master_px from when the
        # cycle started.
        #
        # This used to be a second OANDA REST call here, which was itself
        # extra latency added right before execution. Reading live_quotes
        # is an in-memory cache read (no HTTP round-trip at all) as long as
        # the WebSocket feed is fresh; REST is now only a fallback for when
        # it isn't -- so this re-check got both more accurate AND faster,
        # not just faster.
        fresh_px, fresh_feed_source, fresh_feed_age_ms = await _lq_price_with_fallback(symbol)
        margin = sym_state['gann_touch_margin_pts'] * SYMBOL_INFO[symbol]['pip_value']
        if fresh_px is None or abs(fresh_px - level['price']) > margin:
            bot_state['symbol_state'][symbol]['gann_level_status'][level['key']] = 'used'
            # Detail requested: exactly how much the price moved, over how
            # long, and what the pre-existing code threshold is (the touch
            # margin, gann_touch_margin_pts -- unchanged, no new number
            # invented) that this movement exceeded.
            elapsed_s = (datetime.now(timezone.utc) - detect_time).total_seconds() if detect_time else None
            drift = abs(fresh_px - initial_px) if (fresh_px is not None and initial_px is not None) else None
            detail_lines = []
            if drift is not None:
                margin_pts = sym_state['gann_touch_margin_pts']
                detail_lines.append(
                    f"الحركة الفعلية منذ اكتشاف اللمس: {drift:.3f} ({drift / SYMBOL_INFO[symbol]['pip_value']:.1f} نقطة)"
                )
                detail_lines.append(
                    f"الحد المسموح به مسبقاً بالكود (هامش اللمس gann_touch_margin_pts): {margin_pts} نقطة ({margin:.3f})"
                )
            if elapsed_s is not None:
                detail_lines.append(f"الفجوة الزمنية بين الاكتشاف والتنفيذ: {elapsed_s:.1f} ثانية")
            detail_block = ("\n" + "\n".join(detail_lines)) if detail_lines else ""
            await send_tg_msg(
                f"<b>⏭️ [{symbol} - جان {tf}]</b>  {reason}\n"
                f"المستوى: {level['price']:.2f}\n"
                f"تم تجاهل الفريم — السعر ابتعد عن المستوى أثناء التنفيذ "
                f"({'لا يمكن التأكد من السعر الحالي' if fresh_px is None else f'{fresh_px:.2f}'}) ولم يعد لمساً حقيقياً."
                f"{detail_block}"
            )
            return

        price = fresh_px
        tp, sl = _gann_calc_tpsl(symbol, price, is_buy, candles, tf=tf)

        # ── Pre-send sanity check (Point 4) ──
        # If price already moved past where TP or SL would sit before we
        # even send the order, the opportunity is gone (or would produce
        # nonsensical/rejected stops, e.g. "Invalid stops"). Better to skip
        # cleanly here than let the broker reject it after the fact.
        if is_buy and (price >= tp or price <= sl):
            bot_state['symbol_state'][symbol]['gann_level_status'][level['key']] = 'used'
            await send_tg_msg(
                f"<b>⏭️ [{symbol} - جان {tf}]</b>  {reason}\n"
                f"المستوى: {level['price']:.2f}\n"
                f"تم إلغاء الأمر قبل الإرسال — السعر الحالي ({price:.2f}) تجاوز فعلياً "
                f"مستوى TP/SL المحسوب (TP:{tp} SL:{sl})."
            )
            return
        if not is_buy and (price <= tp or price >= sl):
            bot_state['symbol_state'][symbol]['gann_level_status'][level['key']] = 'used'
            await send_tg_msg(
                f"<b>⏭️ [{symbol} - جان {tf}]</b>  {reason}\n"
                f"المستوى: {level['price']:.2f}\n"
                f"تم إلغاء الأمر قبل الإرسال — السعر الحالي ({price:.2f}) تجاوز فعلياً "
                f"مستوى TP/SL المحسوب (TP:{tp} SL:{sl})."
            )
            return

        lot = sym_state['lot_size']
        tp_pts = _gann_tf_tp(symbol, tf); sl_pts = _gann_tf_sl(symbol, tf)

        tpsl_lbl = (f"ATR({sym_state['gann_atr_period']})×{sym_state['gann_atr_sl_mult']}/{sym_state['gann_atr_tp_mult']}\n"
                    if sym_state['gann_tpsl_mode'] == 'atr' else f"SL:{sl_pts}p TP:{tp_pts}p")

        be_lbl = " | 🛡️ BE Active" if sym_state['break_even_enabled'] else ""

        is_real = sym_state.get('auto_trade', False)
        trade_id = f"sim_{int(datetime.now().timestamp())}_{tf}"
        real_msg = ""
        execution_failed = False
        # Real fill price (as opposed to `price`, our pre-check estimate) --
        # only ever populated for actual real orders below; stays None for
        # simulated/paper trades, where `price` IS the fill by definition.
        real_fill_price = None
        fill_price_source = 'simulated'

        if is_real:
            # Source of truth: never spin up a second, ad-hoc MetaAPI
            # connection here. If the one persistent connection created at
            # startup isn't healthy, we do not know the true account state
            # well enough to safely fire a real order.
            if _metaapi_conn is None:
                real_msg = "\n⚠️ لا يوجد اتصال MetaAPI صالح — لم يتم فتح أي صفقة."
                is_real = False
                execution_failed = True
            else:
                t2_pre_send_ts = None
                try:
                    broker_symbol = _resolve_broker_symbol(symbol)

                    # ── Slippage / Deviation control ──
                    # Cap how far from our intended price the broker is allowed
                    # to fill us. 'slippage' is in MetaApi "points" (broker tick
                    # size units, i.e. the same unit as symbol digits — for a
                    # 2-digit gold quote that's $0.01/point). ORDER_FILLING_FOK
                    # ("fill or kill") means the whole order is rejected rather
                    # than partially/badly filled if the broker can't honor it
                    # within that deviation — this is what stops us from being
                    # filled 20 pips away from our entry.
                    max_slippage_points = int(bot_state.get('prot_max_slippage_points', 5))
                    order_options = {
                        'slippage': max_slippage_points,
                        'fillingModes': ['ORDER_FILLING_FOK'],
                    }

                    # ── Latency telemetry (T1 signal -> T2 pre-send -> T3 broker ack) ──
                    t2_pre_send_ts = time.monotonic()
                    if is_buy:
                        res = await _metaapi_conn.create_market_buy_order(
                            broker_symbol, lot, stop_loss=sl, take_profit=tp, options=order_options
                        )
                    else:
                        res = await _metaapi_conn.create_market_sell_order(
                            broker_symbol, lot, stop_loss=sl, take_profit=tp, options=order_options
                        )
                    t3_ack_ts = time.monotonic()

                    code_delay_ms = round((t2_pre_send_ts - t1_signal_ts) * 1000) if t1_signal_ts else None
                    ping_ms = round((t3_ack_ts - t2_pre_send_ts) * 1000)
                    # "Quote Age at Fire" is the real-world number that
                    # actually matters for slippage: how stale was the price
                    # this order was based on, at the instant it fired --
                    # not how fast Python parsed a variable (that's Code
                    # Delay below, kept for completeness but not the headline).
                    feed_label = 'WS (MetaApi live)' if fresh_feed_source == 'ws' else 'OANDA REST (fallback — feed was stale)'
                    age_str = f"{fresh_feed_age_ms}ms" if fresh_feed_age_ms is not None else 'n/a (REST fallback has no push-age)'
                    telemetry_lbl = (
                        f"\n📡 Feed: {feed_label} | Quote Age at Fire: {age_str}"
                        + (
                            f"\n⏱ Code Delay (T2-T1): {code_delay_ms}ms | MetaApi Ping (T3-T2): {ping_ms}ms"
                            if code_delay_ms is not None else
                            f"\n⏱ MetaApi Ping (T3-T2): {ping_ms}ms (T1 unavailable)"
                        )
                    )

                    # CRITICAL: history deals/reconciliation are keyed by
                    # positionId, NOT orderId (they're different tickets in
                    # MT5 — orderId is the pending/market order ticket,
                    # positionId is the resulting open position's ticket).
                    # Previously this preferred orderId, so the reconciliation
                    # lookup below (which matches on positionId) never found
                    # a match, silently fell back to a theoretical/estimated
                    # PnL every time, and mislabeled it as the real MT5 profit.
                    # positionId must be preferred here.
                    trade_id = str(res.get('positionId', res.get('orderId', trade_id)))

                    # ── Real fill price, not our pre-check estimate ──
                    # `price` (fresh_px) is what we THOUGHT we'd get filled at,
                    # checked right before sending. The broker's actual fill
                    # can differ (that's the whole slippage question this was
                    # built to answer). res.get('price') is sometimes the
                    # order's requested price, not a confirmed fill -- the
                    # realized fill price lives on the resulting POSITION, so
                    # query that directly. MetaApi's position sync can lag the
                    # order response by a moment, so retry briefly before
                    # falling back.
                    for delay in (0, 1, 2):
                        if delay: await asyncio.sleep(delay)
                        try:
                            positions = _metaapi_conn.terminal_state.positions
                            match = next((p for p in positions if str(p.get('id')) == trade_id), None)
                            if match and match.get('openPrice') is not None:
                                real_fill_price = float(match['openPrice'])
                                fill_price_source = 'confirmed_position'
                                break
                        except Exception as pe:
                            log_exception(f"_gann_open_trade fill-price lookup [{symbol} {tf}]", pe)
                    if real_fill_price is None and res.get('price') is not None:
                        real_fill_price = float(res['price'])
                        fill_price_source = 'order_response'

                    real_msg = "\n🚀 <b>تم فتح الصفقة حقيقياً على حسابك!</b>" + telemetry_lbl
                    _consecutive_real_order_failures = 0
                except Exception as ex:
                    log_exception(f"_gann_open_trade real order [{symbol} {tf}]", ex)
                    err_str = str(ex)
                    t_fail_ts = time.monotonic()
                    ref_t2 = t2_pre_send_ts if t2_pre_send_ts is not None else t_fail_ts
                    code_delay_ms = round((ref_t2 - t1_signal_ts) * 1000) if t1_signal_ts else None
                    fail_after_ms = round((t_fail_ts - ref_t2) * 1000)
                    fail_telemetry_lbl = (
                        f"\n⏱ Code Delay (T2-T1): {code_delay_ms}ms | Failed after send: {fail_after_ms}ms"
                        if code_delay_ms is not None else
                        f"\n⏱ Failed after send: {fail_after_ms}ms (T1 unavailable)"
                    )
                    # Give an explicit signal when the rejection was caused by
                    # the deviation guard itself (requote / price moved beyond
                    # our slippage tolerance), rather than a generic failure,
                    # so it's obvious this is protective behavior, not a bug.
                    if any(code in err_str for code in ('REQUOTE', 'PRICE_CHANGED', 'OFF_QUOTES')):
                        real_msg = (f"\n🛑 <b>تم رفض الصفقة لتجاوز حد الانزلاق السعري "
                                    f"({max_slippage_points} نقاط):</b> {ex}\nلم يتم التنفيذ لحمايتك من دخول سيء."
                                    f"{fail_telemetry_lbl}")
                    else:
                        real_msg = (f"\n❌ <b>فشل فتح الصفقة حقيقياً:</b> {ex}\nلم يتم تتبعها كصفقة وهمية "
                                    f"(لا يوجد تنفيذ فعلي).{fail_telemetry_lbl}")
                    is_real = False
                    execution_failed = True
                    _consecutive_real_order_failures += 1
                    if _consecutive_real_order_failures >= _REAL_ORDER_FAILURE_HALT_THRESHOLD:
                        await set_connection_state(
                            CONN_HALTED,
                            f"{_consecutive_real_order_failures} consecutive real order failures "
                            f"(last: {ex}). Escalating to protect capital."
                        )

        # Ghost-trade fix: a FAILED real-order attempt must never enter
        # gann_open_trades -- it never had any exposure on the broker, so
        # tracking it (even as a "simulated" fallback) meant the bot would
        # later evaluate it against live price movement and report a
        # fabricated WIN/LOSS for a trade that never existed. Genuine
        # paper-trading (auto_trade was never enabled to begin with) is a
        # completely different, intentional case and is still tracked.
        if execution_failed:
            bot_state['symbol_state'][symbol]['gann_level_status'][level['key']] = 'used'
            await send_tg_msg(
                f"<b>⏭️ [{symbol} - جان {tf}]</b>  {reason}\n"
                f"المستوى: {level['price']:.2f}\n"
                f"{real_msg}"
            )
            return

        # entry_final is what actually happened: the confirmed broker fill
        # when we have one, our pre-check estimate otherwise (simulated
        # trades, or a real trade where the position lookup above failed).
        entry_final = real_fill_price if real_fill_price is not None else price

        bot_state['symbol_state'][symbol]['gann_open_trades'][trade_id] = {
            'tf': tf, 'is_buy': is_buy, 'entry'                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   