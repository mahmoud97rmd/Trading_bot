"""
market_data.py — OANDA REST data fetching + OANDA live price streaming,
MetaApi execution-connection lifecycle, live-quote cache.

Owns:
  - OANDA candle fetcher (fetch_candles, fetch_master_price)
  - OANDA live pricing stream (_oanda_price_stream_loop) -- this is the ONLY
    source of live_quotes / tick-driven entry detection. Independent of
    MetaApi entirely: reconnecting it never calls MetaApi and never touches
    its quota.
  - MetaApi connection bootstrap & lifecycle -- used ONLY for order
    execution (placing/closing trades, reading open positions). It no
    longer carries price ticks.
  - live_quotes cache, _gann_cache, tick semaphore
  - Broker symbol resolution
"""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone, time as dtime

import aiohttp
import numpy as np
import pandas as pd
from metaapi_cloud_sdk import MetaApi, SynchronizationListener

from state import (
    bot_state, METAAPI_TOKEN, ACCOUNT_ID, OANDA_TOKEN, OANDA_BASE_URL,
    OANDA_ACCOUNT, OANDA_STREAM_URL,
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
# LIVE QUOTES CACHE (shared by both feeds, but only OANDA writes to it now)
# ---------------------------------------------------------------------------
live_quotes: dict[str, dict] = {}
_tick_semaphore = asyncio.Semaphore(5)
_gann_cache: dict[str, dict] = {}
_QUOTE_STALE_SECONDS = 5.0

# ── Live-Twin tick bridge ──
# Shared asyncio.Queue fed by the OANDA price stream and consumed by the
# real-time forward paper-trading loop in backtest.py.  Queue is
# write-discard (put_nowait) so it never blocks the price listener.
_live_twin_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)


def _lq_is_stale(symbol: str) -> bool:
    q = live_quotes.get(symbol)
    return q is None or (time.monotonic() - q['ts']) > _QUOTE_STALE_SECONDS


async def _lq_price_with_fallback(symbol: str) -> tuple[float | None, str, float | None]:
    q = live_quotes.get(symbol)
    if q is not None and (time.monotonic() - q['ts']) <= _QUOTE_STALE_SECONDS:
        return q['mid'], 'ws', round((time.monotonic() - q['ts']) * 1000)
    return None, 'ws_stale', None


def _resolve_broker_symbol(symbol: str) -> str:
    """MT5/MetaApi broker symbol for execution -- unrelated to the OANDA feed,
    which always uses our own internal symbol names (they match OANDA's
    instrument names directly, e.g. 'XAU_USD')."""
    configured = bot_state.get('symbol', '').strip()
    if not configured or '_' in configured:
        return symbol.replace('_', '')
    return configured


# ---------------------------------------------------------------------------
# OANDA LIVE PRICE STREAM
# ---------------------------------------------------------------------------
# This is now the ONLY source of live_quotes / tick-driven entry detection.
# It is a plain OANDA v20 streaming HTTP connection -- entirely independent
# of MetaApi. Reconnecting it (on staleness, drop, or a new symbol being
# activated) never calls MetaApi and never touches its quota/usage.
_oanda_stream_symbols: set[str] = set()
_oanda_stream_task: asyncio.Task | None = None
_last_oanda_tick_ts = time.monotonic()
_OANDA_STREAM_STALE_SECONDS = 20.0
_last_oanda_reconnect_alert_ts = 0.0  # throttles repeated Telegram alerts


async def _oanda_price_stream_loop() -> None:
    global _last_oanda_tick_ts
    backoff = 1.0
    while True:
        symbols = sorted(_oanda_stream_symbols)
        if not symbols:
            await asyncio.sleep(2)
            continue
        url = f'{OANDA_STREAM_URL}/accounts/{OANDA_ACCOUNT}/pricing/stream'
        headers = {'Authorization': f'Bearer {OANDA_TOKEN}'}
        params = {'instruments': ','.join(symbols)}
        try:
            # No overall timeout (it's a long-lived stream); sock_read acts as
            # our heartbeat timeout -- OANDA sends a HEARTBEAT line at least
            # every ~5s, so a read gap this long means the connection is dead.
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=_OANDA_STREAM_STALE_SECONDS)
            async with get_http().get(url, headers=headers, params=params, timeout=timeout) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    c_log(f"OANDA price stream: HTTP {resp.status} -- {body[:300]}")
                    await asyncio.sleep(min(backoff, 30)); backoff = min(backoff * 2, 30)
                    continue
                c_log(f"OANDA price stream connected ({', '.join(symbols)}).")
                backoff = 1.0
                async for raw_line in resp.content:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except Exception:
                        continue
                    _last_oanda_tick_ts = time.monotonic()
                    if msg.get('type') != 'PRICE':
                        continue  # HEARTBEAT lines just keep _last_oanda_tick_ts fresh
                    instrument = msg.get('instrument')
                    if instrument not in _oanda_stream_symbols:
                        continue
                    bids = msg.get('bids') or []; asks = msg.get('asks') or []
                    if not bids or not asks:
                        continue
                    try:
                        bid = float(bids[0]['price']); ask = float(asks[0]['price'])
                    except (KeyError, ValueError, TypeError):
                        continue
                    mid = (bid + ask) / 2
                    live_quotes[instrument] = {'bid': bid, 'ask': ask, 'mid': mid, 'ts': time.monotonic()}
                    from execution import _gann_tick_fire_check
                    _safe_task(_gann_tick_fire_check(instrument, mid, 0.0), 'tick_fire_check')
                    from state import bot_state as _bs
                    if _bs.get('is_live_twin_running', False):
                        try:
                            _live_twin_queue.put_nowait({
                                'symbol': instrument, 'bid': bid, 'ask': ask, 'mid': mid,
                                'ts': time.monotonic(),
                            })
                        except asyncio.QueueFull:
                            pass
                c_log("OANDA price stream: connection closed by server -- reconnecting.")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log_exception('_oanda_price_stream_loop', e)
        await asyncio.sleep(min(backoff, 30)); backoff = min(backoff * 2, 30)


async def _restart_oanda_stream() -> None:
    """Cancels and relaunches the streaming task against the current
    _oanda_stream_symbols set. Pure OANDA HTTP -- no MetaApi involved."""
    global _oanda_stream_task
    if _oanda_stream_task is not None and not _oanda_stream_task.done():
        _oanda_stream_task.cancel()
        try:
            await asyncio.wait_for(_oanda_stream_task, timeout=5)
        except Exception:
            pass
    _oanda_stream_task = _safe_task(_oanda_price_stream_loop(), 'oanda_price_stream')


async def _force_oanda_stream_reconnect(reason: str) -> None:
    global _last_oanda_tick_ts, _last_oanda_reconnect_alert_ts
    c_log(f"OANDA STREAM WATCHDOG: reconnecting -- {reason}")
    await _restart_oanda_stream()
    _last_oanda_tick_ts = time.monotonic()
    now = time.monotonic()
    if now - _last_oanda_reconnect_alert_ts > 180:  # throttle: at most one alert / 3 min
        _last_oanda_reconnect_alert_ts = now
        from telegram_ui import send_tg_msg
        await send_tg_msg(f"🔁 <b>Watchdog: أعيد الاتصال بتغذية أسعار OANDA</b>\nالسبب: {reason}")


async def _ensure_oanda_stream_symbol(symbol: str) -> None:
    """Adds a symbol to the OANDA stream and restarts it if needed. Called
    whenever a pair is activated. Cheap, pure-OANDA -- no MetaApi cost."""
    global _oanda_stream_task
    if symbol in _oanda_stream_symbols:
        return
    _oanda_stream_symbols.add(symbol)
    if _oanda_stream_task is None or _oanda_stream_task.done():
        _oanda_stream_task = _safe_task(_oanda_price_stream_loop(), 'oanda_price_stream')
    else:
        await _restart_oanda_stream()


async def init_oanda_price_feed() -> None:
    """Starts the OANDA live price stream for all currently-active symbols.
    Call this AFTER init_metaapi() so bot_state['active_symbols'] reflects
    whatever persistence loaded. This feed is fully independent of MetaApi."""
    for sym, on in bot_state['active_symbols'].items():
        if on:
            _oanda_stream_symbols.add(sym)
    if _oanda_stream_symbols:
        await _restart_oanda_stream()
    else:
        c_log("init_oanda_price_feed: no active symbols yet -- stream starts once one is activated.")


# ---------------------------------------------------------------------------
# METAAPI CONNECTION LIFECYCLE -- EXECUTION ONLY (no price ticks anymore)
# ---------------------------------------------------------------------------
_metaapi = None
_metaapi_account = None
_metaapi_conn = None

# ── Strict Singleton: subscription set ──
# Prevents duplicate subscribe_to_market_data() calls across the
# entire bot lifecycle.  Once a symbol is subscribed it is NEVER
# re-subscribed unless the connection is force-reconnected (in which
# case the set is cleared). This subscription is still required by the
# MetaApi SDK to load the symbol specification needed for order placement --
# it just no longer feeds live_quotes.
_active_subscriptions: set[str] = set()


class _GannPriceListener(SynchronizationListener):
    """Execution-connection lifecycle logging only. Price ticks are no
    longer read from here -- see _oanda_price_stream_loop above."""

    async def on_connected(self, instance_index, replicas):
        c_log("MetaAPI execution connection established.")

    async def on_disconnected(self, instance_index):
        c_log("MetaAPI execution connection lost -- reconnect loop will retry and resubscribe.")


async def _lq_subscribe_symbol(symbol: str) -> None:
    """Ensures the symbol is (a) included in the OANDA price stream and
    (b) subscribed on MetaApi for execution/symbol-spec purposes. (a) never
    touches MetaApi; (b) is a no-op if already subscribed."""
    global _active_subscriptions
    await _ensure_oanda_stream_symbol(symbol)
    if _metaapi_conn is None:
        return
    broker_sym = _resolve_broker_symbol(symbol)
    if broker_sym in _active_subscriptions:
        return  # already subscribed — guard against duplicate API call
    try:
        await _metaapi_conn.subscribe_to_market_data(broker_sym)
        _active_subscriptions.add(broker_sym)
    except Exception as e:
        log_exception(f"_lq_subscribe_symbol [{symbol} -> {broker_sym}]", e)


async def _bootstrap_metaapi_connection() -> bool:
    global _metaapi, _metaapi_account, _metaapi_conn, _active_subscriptions
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
            c_log("MetaAPI Streaming Connection established (execution ready).")
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
