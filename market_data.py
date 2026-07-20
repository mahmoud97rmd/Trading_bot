"""
market_data.py — OANDA REST data fetching, MetaApi WebSocket streaming,
live-quote cache, and connection management.

Owns:
  - OANDA candle fetcher (fetch_candles, fetch_master_price)
  - MetaApi connection bootstrap & lifecycle
  - _GannPriceListener (WebSocket tick listener)
  - live_quotes cache, _gann_cache, tick semaphore
  - WebSocket watchdog (_force_full_reconnect, _lq_subscribe_symbol)
  - Broker symbol resolution
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone, time as dtime

import aiohttp
import numpy as np
import pandas as pd
from metaapi_cloud_sdk import MetaApi, SynchronizationListener

from state import (
    bot_state, METAAPI_TOKEN, ACCOUNT_ID, OANDA_TOKEN, OANDA_BASE_URL,
    SYMBOL_INFO, CONN_RUNNING, CONN_READ_ONLY, CONN_HALTED,
    _state_lock, get_http, log_exception, c_log, _safe_task,
    set_connection_state,
)

# ---------------------------------------------------------------------------
# OANDA FETCHER
# ---------------------------------------------------------------------------
_OANDA_GRAN = {'1m':'M1','2m':'M2','3m':'M3','4m':'M4','5m':'M5','6m':'M6',
               '10m':'M10','15m':'M15','20m':'M20','30m':'M30','1h':'H1','2h':'H2'}
_oanda_sem: asyncio.Semaphore | None = None


def _get_oanda_sem() -> asyncio.Semaphore:
    global _oanda_sem
    if _oanda_sem is None:
        _oanda_sem = asyncio.Semaphore(3)
    return _oanda_sem


from state import _safe_float


def _validated_candle(c: dict, symbol: str, granularity_str: str) -> dict | None:
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


async def fetch_candles(symbol: str, granularity_str: str, count: int = 5000,
                        end_time: datetime = None) -> list:
    gran_str = _OANDA_GRAN.get(granularity_str, 'M1')
    fetch_count = min(count, 120000)
    collected = []; remaining = fetch_count
    headers = {'Authorization': f'Bearer {OANDA_TOKEN}', 'Content-Type': 'application/json'}
    url = f'{OANDA_BASE_URL}/instruments/{symbol}/candles'
    current_end = end_time if end_time else datetime.now(timezone.utc)

    while remaining > 0:
        chunk = min(remaining, 5000)
        params = {'granularity': gran_str, 'count': chunk,
                   'to': current_end.strftime('%Y-%m-%dT%H:%M:%S.000000000Z'), 'price': 'M'}
        candles = []
        async with _get_oanda_sem():
            for attempt in range(6):
                try:
                    async with get_http().get(url, headers=headers, params=params,
                                               timeout=aiohttp.ClientTimeout(total=20)) as resp:
                        if resp.status != 200:
                            if attempt == 5:
                                c_log(f"fetch_candles [{symbol} {granularity_str}]: giving up after 6 attempts "
                                      f"(last status {resp.status}) -- collected {len(collected)}/{fetch_count} candles so far.")
                                break
                            await asyncio.sleep(min(2 ** attempt, 30))
                            continue
                        data = await resp.json(); candles = data.get('candles', []); break
                except Exception as e:
                    log_exception(f"fetch_candles [{symbol} {granularity_str}] attempt {attempt+1}/6", e)
                    await asyncio.sleep(min(2 ** attempt, 30))

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
    mc = await fetch_candles(symbol, '1m', count=2)
    if not mc:
        c_log(f"fetch_master_price [{symbol}]: no 1m data from OANDA this cycle.")
        return None
    return float(mc[-1]['close'])


# ---------------------------------------------------------------------------
# LIVE QUOTES & WEBSOCKET
# ---------------------------------------------------------------------------
live_quotes: dict[str, dict] = {}
_broker_to_data_symbol: dict[str, str] = {}
_tick_semaphore = asyncio.Semaphore(5)
_gann_cache: dict[str, dict] = {}
_QUOTE_STALE_SECONDS = 5.0
_last_any_tick_ts = time.monotonic()
_WS_WATCHDOG_STALE_SECONDS = 60.0

# ── Live-Twin tick bridge ──
# Shared asyncio.Queue fed by _GannPriceListener and consumed by the
# real-time forward paper-trading loop in backtest.py.  Queue is
# write-discard (put_nowait) so it never blocks the price listener.
_live_twin_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)

_metaapi = None
_metaapi_account = None
_metaapi_conn = None
_last_reconnect_alert_ts = 0.0  # throttles repeated Telegram alerts while stuck reconnecting

# ── Strict Singleton: subscription set ──
# Prevents duplicate subscribe_to_market_data() calls across the
# entire bot lifecycle.  Once a symbol is subscribed it is NEVER
# re-subscribed unless the connection is force-reconnected (in which
# case the set is cleared).
_active_subscriptions: set[str] = set()


class _GannPriceListener(SynchronizationListener):
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
        from execution import _gann_tick_fire_check
        _safe_task(_gann_tick_fire_check(data_sym, mid, 0.0), 'tick_fire_check')
        from state import bot_state as _bs
        if _bs.get('is_live_twin_running', False):
            try:
                _live_twin_queue.put_nowait({
                    'symbol': data_sym, 'bid': bid, 'ask': ask, 'mid': mid,
                    'ts': time.monotonic()
                })
            except asyncio.QueueFull:
                pass

    async def on_connected(self, instance_index, replicas):
        c_log("MetaAPI streaming connection established (price feed live).")

    async def on_disconnected(self, instance_index):
        c_log("MetaAPI streaming connection lost -- reconnect loop will retry and resubscribe.")


def _lq_is_stale(symbol: str) -> bool:
    q = live_quotes.get(symbol)
    return q is None or (time.monotonic() - q['ts']) > _QUOTE_STALE_SECONDS


async def _lq_price_with_fallback(symbol: str) -> tuple[float | None, str, float | None]:
    q = live_quotes.get(symbol)
    if q is not None and (time.monotonic() - q['ts']) <= _QUOTE_STALE_SECONDS:
        return q['mid'], 'ws', round((time.monotonic() - q['ts']) * 1000)
    return None, 'ws_stale', None


def _resolve_broker_symbol(symbol: str) -> str:
    configured = bot_state.get('symbol', '').strip()
    if not configured or '_' in configured:
        return symbol.replace('_', '')
    return configured


async def _lq_subscribe_symbol(symbol: str) -> None:
    global _active_subscriptions
    if _metaapi_conn is None:
        return
    broker_sym = _resolve_broker_symbol(symbol)
    _broker_to_data_symbol[broker_sym] = symbol
    if broker_sym in _active_subscriptions:
        return  # already subscribed — guard against duplicate API call
    try:
        await _metaapi_conn.subscribe_to_market_data(broker_sym)
        _active_subscriptions.add(broker_sym)
    except Exception as e:
        log_exception(f"_lq_subscribe_symbol [{symbol} -> {broker_sym}]", e)


async def _force_full_reconnect(reason: str) -> None:
    global _metaapi_conn, _last_any_tick_ts, _active_subscriptions, _last_reconnect_alert_ts
    c_log(f"WS WATCHDOG: forcing full reconnect -- {reason}")
    await set_connection_state(CONN_READ_ONLY, f"WS watchdog: {reason}")
    if _metaapi_account is None:
        c_log("WS WATCHDOG: _metaapi_account is None — cannot reconnect")
        return
    try:
        if _metaapi_conn is not None:
            try:
                await asyncio.wait_for(_metaapi_conn.close(), timeout=15)
            except Exception as e:
                log_exception('_force_full_reconnect: close old connection', e)
        _active_subscriptions.clear()  # reset — re-subscribe below
        _metaapi_conn = _metaapi_account.get_streaming_connection()
        _metaapi_conn.add_synchronization_listener(_GannPriceListener())
        await asyncio.wait_for(_metaapi_conn.connect(), timeout=30)
        await asyncio.wait_for(_metaapi_conn.wait_synchronized(), timeout=30)
        for sym, on in bot_state['active_symbols'].items():
            if on:
                await _lq_subscribe_symbol(sym)
        _last_any_tick_ts = time.monotonic()
        c_log("WS WATCHDOG: reconnect successful, ticks should resume.")
        await set_connection_state(CONN_RUNNING, "WS watchdog: forced reconnect succeeded.")
        from telegram_ui import send_tg_msg
        await send_tg_msg(f"🔁 <b>Watchdog: أعيد الاتصال تلقائياً بـ MetaApi</b>\nالسبب: {reason}")
    except asyncio.TimeoutError:
        c_log("WS WATCHDOG: reconnect attempt timed out -- will retry next scan cycle (~15s).")
        now = time.monotonic()
        if now - _last_reconnect_alert_ts > 180:  # throttle: at most one Telegram alert / 3 min
            _last_reconnect_alert_ts = now
            from telegram_ui import send_tg_msg
            await send_tg_msg(
                f"🛑 <b>Watchdog: انتهت مهلة إعادة الاتصال (30s)</b>\nالسبب الأصلي: {reason}\n"
                f"سيُعاد المحاولة تلقائياً بدورة فحص جديدة خلال ~15 ثانية."
            )
    except Exception as e:
        log_exception('_force_full_reconnect', e)
        now = time.monotonic()
        if now - _last_reconnect_alert_ts > 180:
            _last_reconnect_alert_ts = now
            from telegram_ui import send_tg_msg
            await send_tg_msg(f"🛑 <b>Watchdog: فشلت محاولة إعادة الاتصال التلقائي</b>\nالسبب الأصلي: {reason}\nالخطأ: {e}")


# ---------------------------------------------------------------------------
# METAAPI CONNECTION LIFECYCLE
# ---------------------------------------------------------------------------
async def _bootstrap_metaapi_connection() -> bool:
    global _metaapi, _metaapi_account, _metaapi_conn, _last_any_tick_ts, _active_subscriptions
    try:
        _metaapi = MetaApi(METAAPI_TOKEN)
        _metaapi_account = await _metaapi.metatrader_account_api.get_account(ACCOUNT_ID)
        if _metaapi_account.state == 'DEPLOYED':
            _metaapi_conn = _metaapi_account.get_streaming_connection()
            _metaapi_conn.add_synchronization_listener(_GannPriceListener())
            await asyncio.wait_for(_metaapi_conn.connect(), timeout=30)
            await asyncio.wait_for(_metaapi_conn.wait_synchronized(), timeout=30)
            # Clear the dedup guard: this is a brand-new connection object
            # (whether this is a cold start or a reconnect escalation from the
            # zombie heartbeat), so any symbol recorded as "already subscribed"
            # against the OLD connection must be re-subscribed on this one.
            _active_subscriptions.clear()
            for sym, on in bot_state['active_symbols'].items():
                if on:
                    await _lq_subscribe_symbol(sym)
            c_log("MetaAPI Streaming Connection established (live quotes subscribed).")
            _last_any_tick_ts = time.monotonic()
            await set_connection_state(CONN_RUNNING, "MetaAPI connected and synchronized.")
            return True
        else:
            c_log(f"MetaAPI account not deployed (state={_metaapi_account.state}).")
            await set_connection_state(CONN_READ_ONLY, f"MetaAPI account is not DEPLOYED (state={_metaapi_account.state}).")
            return False
    except Exception as e:
        log_exception("_bootstrap_metaapi_connection", e)
        await set_connection_state(CONN_READ_ONLY, f"MetaAPI connection bootstrap failed: {e}")
        return False


async def init_metaapi():
    from state import load_bot_persistence
    await load_bot_persistence()
    if bot_state.get('_persistence_load_failed'):
        await set_connection_state(
            CONN_READ_ONLY,
            "Startup persistence file was present but unreadable. Starting READ_ONLY until a human "
            "confirms the true broker state and clears this manually."
        )
    await _bootstrap_metaapi_connection()
