
"""
=============================================================
  BOOM & CRASH SIGNAL BOT  —  ADAPTIVE EDITION
  Markets : Boom 300, 500, 1000 | Crash 300, 500, 1000
  Strategy: EMA Ribbon (30M) + BB/ATR compression (5M)
            + RSI / MACD / Stochastic entry (5M)

  ADAPTIVE UPGRADES:
    1. Volatility Regime Filter  — pauses in chaotic markets
    2. ADX Trend Strength Filter — skips choppy/sideways markets
    3. Session Quality Filter    — avoids low-quality time windows
    4. Loss Memory / Auto-Pause  — pauses market after 3 losses
    5. Dynamic ATR Multiplier    — widens/tightens SL & TP with volatility

  DATA SOURCE:
    Live data via official Deriv WebSocket API (derivws.com)
    - tick streaming for live price
    - candle history for indicator calculations

  Delivery: Telegram signals + warnings + daily summary
=============================================================

SETUP
-----
pip install pandas numpy pandas-ta requests websockets python-dotenv
Rename .env.example → .env and fill in your values.
Run: python boom_crash_bot.py

TELEGRAM COMMANDS (send in your Telegram chat):
  /win Crash 1000   — record a win, resets loss streak
  /loss Boom 500    — record a loss, triggers auto-pause if 3 in a row
  /status           — view all 6 markets live state
=============================================================
"""

import os
import time
import json
import asyncio
import requests
import websockets
import numpy as np
import pandas as pd
import pandas_ta as ta
from datetime import datetime, timezone




# ─────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DERIV_APP_ID     = os.getenv("DERIV_APP_ID")

CHECK_INTERVAL    = 300    # scan every 5 minutes
COOLDOWN          = 1800   # 30 min between signals per market
DIAGNOSTIC_EVERY  = 1800   # send diagnostic report every 30 minutes

# ─────────────────────────────────────────────────────────────
#  DERIV WEBSOCKET URL  (corrected from user-supplied snippet)
#  Format: wss://derivws.com/websockets/v3?app_id=YOUR_APP_ID
# ─────────────────────────────────────────────────────────────
def deriv_ws_url(app_id: str) -> str:
    return f"wss://derivws.com/websockets/v3?app_id={app_id}"


# ─────────────────────────────────────────────────────────────
#  MARKETS
# ─────────────────────────────────────────────────────────────
MARKETS = {
    "Boom 300"  : {"symbol": "1HZ300V",  "type": "BOOM",  "freq": 300},
    "Boom 500"  : {"symbol": "1HZ500V",  "type": "BOOM",  "freq": 500},
    "Boom 1000" : {"symbol": "1HZ1000V", "type": "BOOM",  "freq": 1000},
    "Crash 300" : {"symbol": "1HZ300V",  "type": "CRASH", "freq": 300},
    "Crash 500" : {"symbol": "R_500",    "type": "CRASH", "freq": 500},
    "Crash 1000": {"symbol": "R_1000",   "type": "CRASH", "freq": 1000},
}

# ─────────────────────────────────────────────────────────────
#  BASE SETTINGS PER MARKET FREQUENCY
# ─────────────────────────────────────────────────────────────
BASE_SETTINGS = {
    300 : {"atr_sl": 1.2, "atr_tp": 2.5,
           "rsi_buy_low": 35,  "rsi_buy_high": 58,
           "rsi_sell_low": 42, "rsi_sell_high": 65,
           "stoch_os": 25, "stoch_ob": 75},
    500 : {"atr_sl": 1.5, "atr_tp": 3.0,
           "rsi_buy_low": 35,  "rsi_buy_high": 60,
           "rsi_sell_low": 40, "rsi_sell_high": 65,
           "stoch_os": 20, "stoch_ob": 80},
    1000: {"atr_sl": 2.0, "atr_tp": 4.0,
           "rsi_buy_low": 38,  "rsi_buy_high": 58,
           "rsi_sell_low": 42, "rsi_sell_high": 62,
           "stoch_os": 20, "stoch_ob": 80},
}

# ─────────────────────────────────────────────────────────────
#  ADAPTIVE FILTER SETTINGS
# ─────────────────────────────────────────────────────────────
ATR_REGIME_MIN       = 0.5    # below = dead/flat market
ATR_REGIME_MAX       = 2.8    # above = chaotic, block signals
ADX_MIN              = 20     # below = choppy, skip
ADX_MAX              = 60     # above = overextended, warn
GOOD_SESSIONS        = [(6, 12), (13, 18), (20, 23)]  # UTC hours
MAX_CONSECUTIVE_LOSSES = 3
LOSS_PAUSE_MINUTES   = 120
DYNAMIC_ATR_HIGH_VOL = 1.3
DYNAMIC_ATR_LOW_VOL  = 0.85
DYNAMIC_ATR_NORMAL   = 1.0

# ─────────────────────────────────────────────────────────────
#  STATE TRACKING
# ─────────────────────────────────────────────────────────────
market_state = {
    name: {
        "last_signal_time"  : 0,
        "consecutive_losses": 0,
        "pause_until"       : 0,
        "paused_reason"     : "",
        "total_signals"     : 0,
        "daily_buy"         : 0,
        "daily_sell"        : 0,
        "daily_signals"     : 0,
        "regime"            : "NORMAL",
        "last_block_reason" : "Not yet scanned",
        "last_block_time"   : 0,
        "scans_blocked"     : 0,
    }
    for name in MARKETS
}


# ─────────────────────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────────────────────
def send_telegram(message: str):
    url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            print(f"[Telegram] Failed: {r.text}")
        else:
            print("[Telegram] Sent ✅")
    except Exception as e:
        print(f"[Telegram] Error: {e}")


# ─────────────────────────────────────────────────────────────
#  LIVE TICK STREAM  (from user-supplied pattern)
#  Used to get the latest real-time price for a symbol.
# ─────────────────────────────────────────────────────────────
async def get_latest_tick(symbol: str) -> float | None:
    """
    Connect to Deriv WebSocket, subscribe to live ticks,
    grab the first tick price, then disconnect.
    Returns the latest quoted price or None on error.
    """
    uri = deriv_ws_url(DERIV_APP_ID)
    request = {
        "ticks"    : symbol,
        "subscribe": 1,
    }
    try:
        async with websockets.connect(uri) as websocket:
            await websocket.send(json.dumps(request))
            # Wait for first tick response
            while True:
                response = await asyncio.wait_for(websocket.recv(), timeout=10)
                data     = json.loads(response)

                if data.get("msg_type") == "tick":
                    tick = data["tick"]
                    print(f"  Live tick — {tick['symbol']}: {tick['quote']}")
                    return float(tick["quote"])

                elif "error" in data:
                    print(f"  Tick error for {symbol}: {data['error']['message']}")
                    return None

    except asyncio.TimeoutError:
        print(f"  Tick timeout for {symbol}")
        return None
    except Exception as e:
        print(f"  Tick stream error for {symbol}: {e}")
        return None


# ─────────────────────────────────────────────────────────────
#  CANDLE HISTORY FETCHER
#  Used to pull OHLCV history for indicator calculations.
# ─────────────────────────────────────────────────────────────
async def fetch_candles(symbol: str, granularity: int, count: int) -> pd.DataFrame:
    """
    Fetch historical OHLCV candles via Deriv WebSocket.
    granularity: 300  = 5  minute candles
                 1800 = 30 minute candles
    """
    uri = deriv_ws_url(DERIV_APP_ID)
    request = {
        "ticks_history"    : symbol,
        "adjust_start_time": 1,
        "count"            : count,
        "end"              : "latest",
        "granularity"      : granularity,
        "style"            : "candles",
    }
    try:
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps(request))

            # Keep reading until we get candles or an error
            while True:
                response = await asyncio.wait_for(ws.recv(), timeout=15)
                data     = json.loads(response)

                if "candles" in data:
                    df = pd.DataFrame(data["candles"])
                    df.rename(columns={"epoch": "time"}, inplace=True)
                    df["time"] = pd.to_datetime(df["time"], unit="s")
                    for col in ["open", "high", "low", "close"]:
                        df[col] = df[col].astype(float)
                    return df.reset_index(drop=True)

                elif "error" in data:
                    print(f"  Candle error {symbol} ({granularity}s): "
                          f"{data['error']['message']}")
                    return pd.DataFrame()

    except asyncio.TimeoutError:
        print(f"  Candle timeout {symbol} ({granularity}s)")
        return pd.DataFrame()
    except Exception as e:
        print(f"  Candle fetch error {symbol}: {e}")
        return pd.DataFrame()


# ─────────────────────────────────────────────────────────────
#  INDICATORS
# ─────────────────────────────────────────────────────────────
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c, h, l = df["close"], df["high"], df["low"]

    # EMA Ribbon
    df["ema20"]  = ta.ema(c, length=20)
    df["ema50"]  = ta.ema(c, length=50)
    df["ema100"] = ta.ema(c, length=100)
    df["ema200"] = ta.ema(c, length=200)

    # Bollinger Bands
    bb = ta.bbands(c, length=20, std=2.0)
    df["bb_upper"] = bb["BBU_20_2.0"]
    df["bb_lower"] = bb["BBL_20_2.0"]
    df["bb_mid"]   = bb["BBM_20_2.0"]
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]

    # ATR (50-period average for regime detection)
    df["atr"]    = ta.atr(h, l, c, length=14)
    df["atr_ma"] = df["atr"].rolling(50).mean()

    # RSI
    df["rsi"] = ta.rsi(c, length=14)

    # MACD
    macd = ta.macd(c, fast=12, slow=26, signal=9)
    df["macd"]     = macd["MACD_12_26_9"]
    df["macd_sig"] = macd["MACDs_12_26_9"]
    df["macd_h"]   = macd["MACDh_12_26_9"]

    # Stochastic
    stoch = ta.stoch(h, l, c, k=5, d=3, smooth_k=3)
    df["stoch_k"] = stoch["STOCHk_5_3_3"]
    df["stoch_d"] = stoch["STOCHd_5_3_3"]

    # ADX (trend strength — key adaptive filter)
    adx = ta.adx(h, l, c, length=14)
    df["adx"] = adx["ADX_14"]
    df["dmp"] = adx["DMP_14"]   # +DI bullish
    df["dmn"] = adx["DMN_14"]   # -DI bearish

    return df


# ─────────────────────────────────────────────────────────────
#  ADAPTIVE FILTER 1 — VOLATILITY REGIME
# ─────────────────────────────────────────────────────────────
def get_volatility_regime(df: pd.DataFrame) -> tuple[str, float]:
    last   = df.iloc[-1]
    atr    = last["atr"]
    atr_ma = last["atr_ma"]

    if pd.isna(atr_ma) or atr_ma == 0:
        return "NORMAL", DYNAMIC_ATR_NORMAL

    ratio = atr / atr_ma

    if ratio > ATR_REGIME_MAX:
        return "CHAOTIC",  DYNAMIC_ATR_HIGH_VOL
    elif ratio > 1.5:
        return "HIGH_VOL", DYNAMIC_ATR_HIGH_VOL
    elif ratio < ATR_REGIME_MIN:
        return "LOW_VOL",  DYNAMIC_ATR_LOW_VOL
    else:
        return "NORMAL",   DYNAMIC_ATR_NORMAL


# ─────────────────────────────────────────────────────────────
#  ADAPTIVE FILTER 2 — ADX TREND STRENGTH
# ─────────────────────────────────────────────────────────────
def check_adx(df: pd.DataFrame, bias: str) -> tuple[bool, float, str]:
    last = df.iloc[-1]
    adx  = last["adx"]
    dmp  = last["dmp"]
    dmn  = last["dmn"]

    if pd.isna(adx):
        return True, 0.0, "ADX unavailable"

    if adx < ADX_MIN:
        return False, adx, f"ADX {adx:.1f} < {ADX_MIN} — choppy market"

    if bias == "BUY"  and dmp < dmn:
        return False, adx, f"BUY signal but -DI({dmn:.1f}) > +DI({dmp:.1f})"
    if bias == "SELL" and dmn < dmp:
        return False, adx, f"SELL signal but +DI({dmp:.1f}) > -DI({dmn:.1f})"

    return True, adx, "OK"


# ─────────────────────────────────────────────────────────────
#  ADAPTIVE FILTER 3 — SESSION QUALITY
# ─────────────────────────────────────────────────────────────
def check_session() -> tuple[bool, str]:
    hour = datetime.now(timezone.utc).hour
    for start, end in GOOD_SESSIONS:
        if start <= hour < end:
            return True, f"Peak session UTC {start:02d}:00–{end:02d}:00 ✅"
    return False, f"Off-peak UTC {hour:02d}:xx ⚠️"


# ─────────────────────────────────────────────────────────────
#  ADAPTIVE FILTER 4 — LOSS MEMORY
# ─────────────────────────────────────────────────────────────
def is_market_paused(name: str) -> tuple[bool, str]:
    s   = market_state[name]
    now = time.time()
    if s["pause_until"] > now:
        mins = int((s["pause_until"] - now) / 60)
        return True, f"Auto-paused {mins}m remaining — {s['paused_reason']}"
    return False, ""


def record_loss(name: str):
    s = market_state[name]
    s["consecutive_losses"] += 1
    if s["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
        reason = f"{MAX_CONSECUTIVE_LOSSES} consecutive losses"
        s["pause_until"]        = time.time() + (LOSS_PAUSE_MINUTES * 60)
        s["paused_reason"]      = reason
        s["consecutive_losses"] = 0
        send_telegram(
            f"⏸ <b>{name} AUTO-PAUSED</b>\n\n"
            f"Reason: {reason}\n"
            f"Resuming in {LOSS_PAUSE_MINUTES} minutes.\n\n"
            f"<i>Bot is protecting your account during a rough patch.</i>"
        )


def record_win(name: str):
    market_state[name]["consecutive_losses"] = 0


# ─────────────────────────────────────────────────────────────
#  LAYER 1 — 30M BIAS
# ─────────────────────────────────────────────────────────────
def check_bias(df30: pd.DataFrame) -> str | None:
    df30 = add_indicators(df30)
    r    = df30.iloc[-1]
    p    = r["close"]
    bull = (p > r["ema20"] > r["ema50"] > r["ema100"] > r["ema200"]
            and r["rsi"] > 50 and r["macd"] > 0)
    bear = (p < r["ema20"] < r["ema50"] < r["ema100"] < r["ema200"]
            and r["rsi"] < 50 and r["macd"] < 0)
    return "BUY" if bull else ("SELL" if bear else None)


# ─────────────────────────────────────────────────────────────
#  LAYERS 2+3 — 5M COMPRESSION + ENTRY TIMING
# ─────────────────────────────────────────────────────────────
def check_entry(df5: pd.DataFrame, bias: str,
                freq: int, dyn_mult: float) -> dict | None:
    last = df5.iloc[-1]
    prev = df5.iloc[-2]
    cfg  = BASE_SETTINGS[freq]

    # Layer 2 — BB squeeze + ATR compression
    recent_bw  = df5["bb_width"].dropna().tail(50)
    bb_squeeze = last["bb_width"] <= np.percentile(recent_bw, 20)
    atr_low    = last["atr"] < last["atr_ma"]
    if not (bb_squeeze and atr_low):
        return None

    # Layer 3 — entry timing
    rsi     = last["rsi"]
    stoch_k = last["stoch_k"]
    prev_k  = prev["stoch_k"]
    macd_ok = ((last["macd"] > last["macd_sig"])
               or (last["macd_h"] > 0 and last["macd_h"] > prev["macd_h"]))

    if bias == "BUY":
        rsi_ok   = cfg["rsi_buy_low"]  <= rsi <= cfg["rsi_buy_high"]
        stoch_ok = stoch_k < cfg["stoch_os"] + 10 and stoch_k > prev_k
        score    = sum([rsi_ok, macd_ok, stoch_ok])
        if score < 2:
            return None
        entry   = last["close"]
        sl      = round(entry - last["atr"] * cfg["atr_sl"] * dyn_mult, 5)
        tp      = round(entry + last["atr"] * cfg["atr_tp"] * dyn_mult, 5)

    elif bias == "SELL":
        rsi_ok   = cfg["rsi_sell_low"] <= rsi <= cfg["rsi_sell_high"]
        macd_ok  = (last["macd"] < last["macd_sig"]) or (last["macd_h"] < 0)
        stoch_ok = stoch_k > cfg["stoch_ob"] - 10 and stoch_k < prev_k
        score    = sum([rsi_ok, macd_ok, stoch_ok])
        if score < 2:
            return None
        entry   = last["close"]
        sl      = round(entry + last["atr"] * cfg["atr_sl"] * dyn_mult, 5)
        tp      = round(entry - last["atr"] * cfg["atr_tp"] * dyn_mult, 5)
    else:
        return None

    return {
        "direction": bias,
        "entry"    : round(entry, 5),
        "tp"       : tp,
        "sl"       : sl,
        "atr"      : round(last["atr"], 5),
        "rsi"      : round(rsi, 2),
        "macd"     : round(last["macd"], 5),
        "stoch_k"  : round(stoch_k, 2),
        "adx"      : round(last["adx"], 1) if not pd.isna(last["adx"]) else 0.0,
        "score"    : score,
        "dyn_mult" : dyn_mult,
    }


# ─────────────────────────────────────────────────────────────
#  SIGNAL FORMATTER
# ─────────────────────────────────────────────────────────────
EMOJI = {
    "Boom 300":"📈",  "Boom 500":"📈",  "Boom 1000":"📈",
    "Crash 300":"📉", "Crash 500":"📉", "Crash 1000":"📉",
}
REGIME_LABELS = {
    "NORMAL"  : "🟢 Normal",
    "HIGH_VOL": "🟡 High Volatility",
    "LOW_VOL" : "🔵 Low Volatility",
    "CHAOTIC" : "🔴 Chaotic",
}

def format_signal(name: str, sig: dict, freq: int,
                  regime: str, session_note: str) -> str:
    cfg    = BASE_SETTINGS[freq]
    rr     = round((cfg["atr_tp"] * sig["dyn_mult"]) /
                   (cfg["atr_sl"] * sig["dyn_mult"]), 1)
    arrow  = "🟢 BUY  ▲" if sig["direction"] == "BUY" else "🔴 SELL ▼"
    stars  = "⭐" * sig["score"]
    now    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if sig["dyn_mult"] > 1.0:
        dyn_note = f"⬆️ SL/TP widened {int((sig['dyn_mult']-1)*100)}% — high vol"
    elif sig["dyn_mult"] < 1.0:
        dyn_note = f"⬇️ SL/TP tightened {int((1-sig['dyn_mult'])*100)}% — low vol"
    else:
        dyn_note = "➡️ SL/TP standard"

    return (
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{EMOJI.get(name,'📊')} <b>{name}</b>  |  M5 + M30\n"
        f"🕐 {now}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>{arrow}</b>\n\n"
        f"🎯 <b>Entry :</b>  <code>{sig['entry']}</code>\n"
        f"✅ <b>TP    :</b>  <code>{sig['tp']}</code>\n"
        f"🛑 <b>SL    :</b>  <code>{sig['sl']}</code>\n"
        f"📐 <b>R:R   :</b>  1 : {rr}\n\n"
        f"┄┄┄┄ Indicators ┄┄┄┄\n"
        f"ATR    {sig['atr']}\n"
        f"RSI    {sig['rsi']}\n"
        f"MACD   {sig['macd']}\n"
        f"Stoch  {sig['stoch_k']}\n"
        f"ADX    {sig['adx']}\n\n"
        f"┄┄┄┄ Market Conditions ┄┄┄┄\n"
        f"Regime   {REGIME_LABELS.get(regime, regime)}\n"
        f"Session  {session_note}\n"
        f"{dyn_note}\n\n"
        f"Confluence  {stars} ({sig['score']}/3)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ <i>Max 1-2% risk per trade.</i>\n"
        f"<i>Reply /win {name} or /loss {name} to track result.</i>"
    )


# ─────────────────────────────────────────────────────────────
#  DAILY SUMMARY
# ─────────────────────────────────────────────────────────────
def format_summary() -> str:
    now   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total = sum(s["daily_signals"] for s in market_state.values())
    lines = [
        f"📋 <b>Daily Summary — {now}</b>",
        f"━━━━━━━━━━━━━━━━━━━━━━━",
        f"Total signals today: <b>{total}</b>\n",
    ]
    for name, s in market_state.items():
        if s["daily_signals"] > 0:
            lines.append(
                f"{EMOJI.get(name,'📊')} {name}:  {s['daily_signals']} signals  "
                f"(🟢{s['daily_buy']} BUY  🔴{s['daily_sell']} SELL)"
            )
    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━",
        "💡 <b>Tomorrow tips:</b>",
        "• Only act on 3/3 confluence signals",
        "• Skip off-peak session signals",
        "• Trust the auto-pause",
        "<i>Stats reset for new day.</i>",
    ]
    return "\n".join(lines)


def reset_daily_stats():
    for s in market_state.values():
        s["daily_signals"] = 0
        s["daily_buy"]     = 0
        s["daily_sell"]    = 0


# ─────────────────────────────────────────────────────────────
#  TELEGRAM COMMAND POLLING  (/win /loss /status)
# ─────────────────────────────────────────────────────────────
last_update_id = 0

def poll_telegram_commands():
    global last_update_id
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    try:
        r = requests.get(
            url,
            params={"offset": last_update_id + 1, "timeout": 2},
            timeout=5
        )
        if r.status_code != 200:
            return
        for upd in r.json().get("result", []):
            last_update_id = upd["update_id"]
            text = upd.get("message", {}).get("text", "").strip()

            if text.lower().startswith("/win"):
                parts = text.split(maxsplit=1)
                mkt   = parts[1].title() if len(parts) > 1 else ""
                if mkt in market_state:
                    record_win(mkt)
                    send_telegram(f"✅ Win recorded for <b>{mkt}</b>. Loss streak reset.")
                else:
                    send_telegram(
                        "Usage: /win Crash 1000\n"
                        "Markets: Boom 300, Boom 500, Boom 1000, "
                        "Crash 300, Crash 500, Crash 1000"
                    )

            elif text.lower().startswith("/loss"):
                parts = text.split(maxsplit=1)
                mkt   = parts[1].title() if len(parts) > 1 else ""
                if mkt in market_state:
                    record_loss(mkt)
                    send_telegram(f"🔴 Loss recorded for <b>{mkt}</b>.")
                else:
                    send_telegram(
                        "Usage: /loss Crash 500\n"
                        "Markets: Boom 300, Boom 500, Boom 1000, "
                        "Crash 300, Crash 500, Crash 1000"
                    )

            elif text.lower() == "/status":
                lines = ["📊 <b>Bot Status</b>\n"]
                for name, s in market_state.items():
                    paused, reason = is_market_paused(name)
                    regime = s.get("regime", "NORMAL")
                    if paused:
                        status = f"⏸ PAUSED — {reason}"
                    else:
                        status = f"✅ Active | {REGIME_LABELS.get(regime, regime)}"
                    lines.append(f"{EMOJI.get(name,'📊')} {name}: {status}")
                send_telegram("\n".join(lines))

            elif text.lower() == "/diag":
                send_telegram(format_diagnostic())

    except Exception:
        pass   # silently skip command poll errors


# ─────────────────────────────────────────────────────────────
#  DIAGNOSTIC REPORT
#  Sent every 30 minutes showing exactly WHY each market
#  is not producing signals — so you know what's happening.
# ─────────────────────────────────────────────────────────────
BLOCK_EMOJI = {
    "cooldown"        : "⏱",
    "paused"          : "⏸",
    "no data"         : "📡",
    "chaotic"         : "🌪",
    "no 30m bias"     : "📉",
    "adx"             : "📏",
    "no compression"  : "🔲",
    "entry timing"    : "🎯",
    "low score"       : "⭐",
    "signal fired"    : "✅",
    "not yet scanned" : "🔄",
}

def get_block_emoji(reason: str) -> str:
    reason_lower = reason.lower()
    for key, emoji in BLOCK_EMOJI.items():
        if key in reason_lower:
            return emoji
    return "🔍"

def format_diagnostic() -> str:
    now  = datetime.now(timezone.utc).strftime("%H:%M UTC")
    _, session_note = check_session()
    lines = [
        f"🔬 <b>Diagnostic Report — {now}</b>",
        f"━━━━━━━━━━━━━━━━━━━━━━━",
        f"Session: {session_note}\n",
    ]

    any_close = False
    for name, s in market_state.items():
        reason = s.get("last_block_reason", "Not yet scanned")
        blocked_count = s.get("scans_blocked", 0)
        emoji  = get_block_emoji(reason)
        regime = REGIME_LABELS.get(s.get("regime", "NORMAL"), "Normal")

        # Flag markets that are "close" to signalling
        close_flag = ""
        if any(kw in reason.lower() for kw in ["entry timing", "low score", "no compression"]):
            close_flag = "  ⚡ <i>close to signal</i>"
            any_close  = True

        lines.append(
            f"{EMOJI.get(name,'📊')} <b>{name}</b>\n"
            f"   {emoji} {reason}{close_flag}\n"
            f"   Regime: {regime}  |  Blocked: {blocked_count} scans"
        )

    lines.append("\n━━━━━━━━━━━━━━━━━━━━━━━")

    if any_close:
        lines.append("⚡ <b>Some markets are close to signalling — stay alert!</b>")
    else:
        lines.append("<i>No markets are near a signal right now. Filters protecting you.</i>")

    lines.append(
        "\n<b>What each block means:</b>\n"
        "⏱ Cooldown — recent signal fired, waiting 30m\n"
        "⏸ Paused — 3 losses hit, auto-protecting account\n"
        "🌪 Chaotic — market too volatile, unsafe to trade\n"
        "📉 No 30M Bias — EMAs not clearly stacked\n"
        "📏 ADX — trend too weak or wrong direction\n"
        "🔲 No Compression — BB/ATR not squeezing yet\n"
        "🎯 Entry Timing — RSI/MACD/Stoch not aligned\n"
        "⭐ Low Score — not enough indicators agree"
    )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────────────────────
async def run_bot():
    print("=" * 60)
    print("  BOOM & CRASH SIGNAL BOT — ADAPTIVE EDITION")
    print("  Boom 300/500/1000  |  Crash 300/500/1000")
    print("  Connection: wss://derivws.com (live ticks + candle history)")
    print("  Filters: Volatility + ADX + Session + Loss Memory")
    print("=" * 60)

    last_summary    = 0
    last_diagnostic = 0

    send_telegram(
        "🤖 <b>Boom &amp; Crash Signal Bot — ADAPTIVE EDITION</b>\n\n"
        "📈 Boom 300 | Boom 500 | Boom 1000\n"
        "📉 Crash 300 | Crash 500 | Crash 1000\n\n"
        "🔌 Connected via: <code>wss://derivws.com</code>\n\n"
        "🧠 <b>Smart Filters Active:</b>\n"
        "  • Volatility Regime Detection\n"
        "  • ADX Trend Strength Check\n"
        "  • Session Quality Filter\n"
        "  • Loss Memory Auto-Pause\n"
        "  • Dynamic SL/TP Adjustment\n\n"
        "📲 <b>Commands:</b>\n"
        "  /win Crash 1000  — record a win\n"
        "  /loss Boom 500   — record a loss\n"
        "  /status          — view all market states\n"
        "  /diag            — see why no signals are firing\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ <i>Not financial advice. Always use a stop-loss.</i>"
    )

    while True:
        now_ts = time.time()

        # Poll Telegram for /win /loss /status commands
        poll_telegram_commands()

        # Send daily summary and reset stats at midnight UTC
        if now_ts - last_summary >= 86400:
            send_telegram(format_summary())
            reset_daily_stats()
            last_summary = now_ts

        # Send diagnostic report every 30 minutes
        if now_ts - last_diagnostic >= DIAGNOSTIC_EVERY:
            send_telegram(format_diagnostic())
            last_diagnostic = now_ts

        print(f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
              f"Scanning {len(MARKETS)} markets...")

        for name, info in MARKETS.items():
            symbol = info["symbol"]
            freq   = info["freq"]
            state  = market_state[name]

            try:
                # ── Cooldown ──────────────────────────────
                if now_ts - state["last_signal_time"] < COOLDOWN:
                    mins = int((COOLDOWN - (now_ts - state["last_signal_time"])) / 60)
                    print(f"  {name}: cooldown {mins}m")
                    state["last_block_reason"] = f"Cooldown — {mins}m remaining after last signal"
                    state["scans_blocked"]    += 1
                    continue

                # ── Loss memory pause ─────────────────────
                paused, pause_reason = is_market_paused(name)
                if paused:
                    print(f"  {name}: {pause_reason}")
                    state["last_block_reason"] = f"Paused — {pause_reason}"
                    state["scans_blocked"]    += 1
                    continue

                # ── Get live tick price ───────────────────
                live_price = await get_latest_tick(symbol)
                if live_price is None:
                    print(f"  {name}: could not get live tick, skipping")
                    continue

                # ── Fetch candle history ──────────────────
                print(f"  {name}: fetching candles...")
                df5  = await fetch_candles(symbol, granularity=300,  count=100)
                df30 = await fetch_candles(symbol, granularity=1800, count=60)

                if df5.empty or df30.empty or len(df5) < 60 or len(df30) < 30:
                    print(f"  {name}: insufficient candle data")
                    state["last_block_reason"] = "No data — insufficient candles returned from API"
                    state["scans_blocked"]    += 1
                    continue

                # Patch the last candle close with the live tick price
                # so indicators use the most up-to-date price
                df5.at[df5.index[-1], "close"] = live_price

                # Add indicators
                df5_ind = add_indicators(df5.copy())

                # ── Filter 1: Volatility Regime ───────────
                regime, dyn_mult   = get_volatility_regime(df5_ind)
                state["regime"]    = regime
                if regime == "CHAOTIC":
                    print(f"  {name}: CHAOTIC — skipping")
                    state["last_block_reason"] = "Chaotic — ATR too high, market unsafe to trade"
                    state["scans_blocked"]    += 1
                    continue

                # ── Filter 2: 30M Bias ────────────────────
                bias = check_bias(df30)
                if not bias:
                    print(f"  {name}: no clear 30M bias")
                    state["last_block_reason"] = "No 30M Bias — EMAs not clearly stacked, market ranging"
                    state["scans_blocked"]    += 1
                    continue

                # ── Filter 3: ADX Strength ────────────────
                adx_ok, adx_val, adx_reason = check_adx(df5_ind, bias)
                if not adx_ok:
                    print(f"  {name}: ADX — {adx_reason}")
                    state["last_block_reason"] = f"ADX — {adx_reason}"
                    state["scans_blocked"]    += 1
                    continue

                # ── Filter 4: Session Quality ─────────────
                good_session, session_note = check_session()
                min_score = 2 if good_session else 3

                print(f"  {name}: bias={bias} | price={live_price} | "
                      f"regime={regime} | ADX={adx_val:.1f} | "
                      f"session={'peak' if good_session else 'off-peak'}")

                # ── Layers 2+3: Entry signal ──────────────
                sig = check_entry(df5_ind, bias, freq, dyn_mult)
                if not sig:
                    print(f"  {name}: 5M entry conditions not met")
                    state["last_block_reason"] = (
                        f"No Compression — BB/ATR squeeze not detected on 5M "
                        f"(bias={bias}, regime={regime})"
                    )
                    state["scans_blocked"]    += 1
                    continue

                if sig["score"] < min_score:
                    print(f"  {name}: score {sig['score']} < "
                          f"{min_score} required ({'off-peak' if not good_session else 'standard'})")
                    state["last_block_reason"] = (
                        f"Low Score — {sig['score']}/{min_score} indicators agree "
                        f"(RSI={sig['rsi']}, Stoch={sig['stoch_k']}, "
                        f"{'off-peak needs 3/3' if not good_session else 'needs 2/3'})"
                    )
                    state["scans_blocked"]    += 1
                    continue

                # ── Signal fires ──────────────────────────
                print(f"  ✅ {name}: {sig['direction']} | "
                      f"entry={sig['entry']} | score={sig['score']}/3")

                msg = format_signal(name, sig, freq, regime, session_note)
                send_telegram(msg)

                # Update state
                state["last_signal_time"] = now_ts
                state["total_signals"]   += 1
                state["daily_signals"]   += 1
                state["last_block_reason"] = "Signal fired ✅"
                state["scans_blocked"]     = 0
                if sig["direction"] == "BUY":
                    state["daily_buy"]  += 1
                else:
                    state["daily_sell"] += 1

            except Exception as e:
                print(f"  {name}: ERROR — {e}")

        print(f"  Next scan in {CHECK_INTERVAL // 60} min.")
        await asyncio.sleep(CHECK_INTERVAL)


# ─────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(run_bot())
