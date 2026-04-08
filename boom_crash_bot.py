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

  Delivery: Telegram signals + warnings + daily summary
=============================================================

SETUP
-----
pip install pandas numpy pandas-ta requests websockets python-dotenv
Rename .env.example → .env and fill in your values.
Run: python boom_crash_bot.py
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
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DERIV_APP_ID = os.getenv("DERIV_APP_ID")

CHECK_INTERVAL = 300    # scan every 5 minutes
COOLDOWN = 1800   # 30 min between signals per market

# ─────────────────────────────────────────────────────────────
#  MARKETS
# ─────────────────────────────────────────────────────────────
MARKETS = {
    "Boom 300": {"symbol": "1HZ300V",  "type": "BOOM",  "freq": 300},
    "Boom 500": {"symbol": "1HZ500V",  "type": "BOOM",  "freq": 500},
    "Boom 1000": {"symbol": "1HZ1000V", "type": "BOOM",  "freq": 1000},
    "Crash 300": {"symbol": "1HZ300V",  "type": "CRASH", "freq": 300},
    "Crash 500": {"symbol": "R_500",    "type": "CRASH", "freq": 500},
    "Crash 1000": {"symbol": "R_1000",   "type": "CRASH", "freq": 1000},
}

# ─────────────────────────────────────────────────────────────
#  BASE SETTINGS PER MARKET FREQUENCY
# ─────────────────────────────────────────────────────────────
BASE_SETTINGS = {
    300: {"atr_sl": 1.2, "atr_tp": 2.5,
          "rsi_buy_low": 35,  "rsi_buy_high": 58,
          "rsi_sell_low": 42, "rsi_sell_high": 65,
          "stoch_os": 25, "stoch_ob": 75},
    500: {"atr_sl": 1.5, "atr_tp": 3.0,
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

# 1. VOLATILITY REGIME
#    ATR must be within this multiplier range of its own 50-period average.
#    Below MIN = too quiet/dead market. Above MAX = too chaotic/news spike.
ATR_REGIME_MIN = 0.5   # ATR must be at least 50% of its average
ATR_REGIME_MAX = 2.8   # ATR must not exceed 280% of its average

# 2. ADX TREND STRENGTH
#    ADX below MIN = market is choppy/sideways, skip signal.
#    ADX above MAX = trend may be overextended, reduce confidence.
ADX_MIN = 20           # minimum trend strength to trade
ADX_MAX = 60           # above this = overextended, still trade but warn

# 3. SESSION FILTER (UTC hours)
#    Boom/Crash most reliable during these UTC hour windows.
#    Outside these windows signals fire but are marked as lower quality.
GOOD_SESSIONS = [(6, 12), (13, 18), (20, 23)]  # UTC hour ranges

# 4. LOSS MEMORY
#    If a market fires this many consecutive losses, pause it temporarily.
MAX_CONSECUTIVE_LOSSES = 3
LOSS_PAUSE_MINUTES = 120   # pause for 2 hours after max losses hit

# 5. DYNAMIC ATR MULTIPLIER
#    In high volatility: widen SL/TP. In low volatility: tighten them.
#    Multiplier is applied ON TOP of the base ATR settings.
DYNAMIC_ATR_HIGH_VOL = 1.3    # widen by 30% in high volatility
DYNAMIC_ATR_LOW_VOL = 0.85   # tighten by 15% in low volatility
DYNAMIC_ATR_NORMAL = 1.0    # no change in normal volatility

# ─────────────────────────────────────────────────────────────
#  STATE TRACKING
# ─────────────────────────────────────────────────────────────
market_state = {
    name: {
        "last_signal_time": 0,
        "consecutive_losses": 0,
        "pause_until": 0,
        "paused_reason": "",
        "total_signals": 0,
        "daily_buy": 0,
        "daily_sell": 0,
        "daily_signals": 0,
        "regime": "NORMAL",   # NORMAL / HIGH_VOL / LOW_VOL / CHAOTIC
    }
    for name in MARKETS
}


# ─────────────────────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────────────────────
def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID,
               "text": message, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            print(f"[Telegram] Failed: {r.text}")
        else:
            print(f"[Telegram] Sent ✅")
    except Exception as e:
        print(f"[Telegram] Error: {e}")


# ─────────────────────────────────────────────────────────────
#  DATA FETCHER
# ─────────────────────────────────────────────────────────────
async def fetch_candles(symbol: str, granularity: int, count: int) -> pd.DataFrame:
    url = f"wss://ws.binaryws.com/websockets/v3?app_id={DERIV_APP_ID}"
    req = {
        "ticks_history": symbol, "adjust_start_time": 1,
        "count": count, "end": "latest",
        "granularity": granularity, "style": "candles",
    }
    try:
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps(req))
            resp = json.loads(await ws.recv())
        if "candles" not in resp:
            return pd.DataFrame()
        df = pd.DataFrame(resp["candles"])
        df.rename(columns={"epoch": "time"}, inplace=True)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        for col in ["open", "high", "low", "close"]:
            df[col] = df[col].astype(float)
        return df.reset_index(drop=True)
    except Exception as e:
        print(f"  [API] {symbol} ({granularity}s): {e}")
        return pd.DataFrame()


# ─────────────────────────────────────────────────────────────
#  INDICATORS
# ─────────────────────────────────────────────────────────────
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c, h, l = df["close"], df["high"], df["low"]

    # EMAs
    df["ema20"] = ta.ema(c, length=20)
    df["ema50"] = ta.ema(c, length=50)
    df["ema100"] = ta.ema(c, length=100)
    df["ema200"] = ta.ema(c, length=200)

    # Bollinger Bands
    bb = ta.bbands(c, length=20, std=2.0)
    df["bb_upper"] = bb["BBU_20_2.0"]
    df["bb_lower"] = bb["BBL_20_2.0"]
    df["bb_mid"] = bb["BBM_20_2.0"]
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]

    # ATR
    df["atr"] = ta.atr(h, l, c, length=14)
    # 50-period for regime detection
    df["atr_ma"] = df["atr"].rolling(50).mean()

    # RSI
    df["rsi"] = ta.rsi(c, length=14)

    # MACD
    macd = ta.macd(c, fast=12, slow=26, signal=9)
    df["macd"] = macd["MACD_12_26_9"]
    df["macd_sig"] = macd["MACDs_12_26_9"]
    df["macd_h"] = macd["MACDh_12_26_9"]

    # Stochastic
    stoch = ta.stoch(h, l, c, k=5, d=3, smooth_k=3)
    df["stoch_k"] = stoch["STOCHk_5_3_3"]
    df["stoch_d"] = stoch["STOCHd_5_3_3"]

    # ADX — trend strength (key adaptive filter)
    adx = ta.adx(h, l, c, length=14)
    df["adx"] = adx["ADX_14"]
    df["dmp"] = adx["DMP_14"]   # +DI
    df["dmn"] = adx["DMN_14"]   # -DI

    return df


# ─────────────────────────────────────────────────────────────
#  ADAPTIVE FILTER 1 — VOLATILITY REGIME
# ─────────────────────────────────────────────────────────────
def get_volatility_regime(df: pd.DataFrame) -> tuple[str, float]:
    """
    Returns (regime, dynamic_multiplier).
    Regime: NORMAL / HIGH_VOL / LOW_VOL / CHAOTIC
    Multiplier adjusts SL & TP distances dynamically.
    """
    last = df.iloc[-1]
    atr = last["atr"]
    atr_ma = last["atr_ma"]

    if pd.isna(atr_ma) or atr_ma == 0:
        return "NORMAL", DYNAMIC_ATR_NORMAL

    ratio = atr / atr_ma

    if ratio > ATR_REGIME_MAX:
        return "CHAOTIC", DYNAMIC_ATR_HIGH_VOL     # too wild — block signal
    elif ratio > 1.5:
        return "HIGH_VOL", DYNAMIC_ATR_HIGH_VOL    # widen SL/TP
    elif ratio < ATR_REGIME_MIN:
        return "LOW_VOL", DYNAMIC_ATR_LOW_VOL      # dead market — tighten
    else:
        return "NORMAL", DYNAMIC_ATR_NORMAL


# ─────────────────────────────────────────────────────────────
#  ADAPTIVE FILTER 2 — ADX TREND STRENGTH
# ─────────────────────────────────────────────────────────────
def check_adx(df: pd.DataFrame, bias: str) -> tuple[bool, float, str]:
    """
    Returns (passes, adx_value, reason).
    Checks ADX strength AND directional alignment (+DI / -DI).
    """
    last = df.iloc[-1]
    adx = last["adx"]
    dmp = last["dmp"]   # +DI = bullish pressure
    dmn = last["dmn"]   # -DI = bearish pressure

    if pd.isna(adx):
        return True, 0.0, "ADX unavailable — skipping filter"

    # Too weak — choppy market
    if adx < ADX_MIN:
        return False, adx, f"ADX {adx:.1f} < {ADX_MIN} — market is choppy/sideways"

    # Directional alignment check
    if bias == "BUY" and dmp < dmn:
        return False, adx, f"ADX bullish signal but -DI ({dmn:.1f}) > +DI ({dmp:.1f}) — direction mismatch"
    if bias == "SELL" and dmn < dmp:
        return False, adx, f"ADX bearish signal but +DI ({dmp:.1f}) > -DI ({dmn:.1f}) — direction mismatch"

    return True, adx, "OK"


# ─────────────────────────────────────────────────────────────
#  ADAPTIVE FILTER 3 — SESSION QUALITY
# ─────────────────────────────────────────────────────────────
def check_session() -> tuple[bool, str]:
    """
    Returns (is_good_session, session_note).
    Boom/Crash synthetic indices trade 24/7 but quality varies.
    """
    hour = datetime.now(timezone.utc).hour
    for start, end in GOOD_SESSIONS:
        if start <= hour < end:
            return True, f"Session UTC {start:02d}:00–{end:02d}:00 ✅"
    return False, f"Off-peak session (UTC {hour:02d}:xx) — signal quality lower ⚠️"


# ─────────────────────────────────────────────────────────────
#  ADAPTIVE FILTER 4 — LOSS MEMORY / AUTO-PAUSE
# ─────────────────────────────────────────────────────────────
def is_market_paused(name: str) -> tuple[bool, str]:
    """Check if market is in auto-pause due to consecutive losses."""
    state = market_state[name]
    now = time.time()
    if state["pause_until"] > now:
        mins_left = int((state["pause_until"] - now) / 60)
        return True, f"Auto-paused ({mins_left}m remaining) — {state['paused_reason']}"
    return False, ""


def record_loss(name: str):
    """Call this when a signal is confirmed as a loss (manual or future auto-tracking)."""
    state = market_state[name]
    state["consecutive_losses"] += 1
    if state["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
        pause_until = time.time() + (LOSS_PAUSE_MINUTES * 60)
        reason = f"{MAX_CONSECUTIVE_LOSSES} consecutive losses detected"
        state["pause_until"] = pause_until
        state["paused_reason"] = reason
        state["consecutive_losses"] = 0
        send_telegram(
            f"⏸ <b>{name} AUTO-PAUSED</b>\n\n"
            f"Reason: {reason}\n"
            f"Resuming in {LOSS_PAUSE_MINUTES} minutes.\n\n"
            f"<i>The bot detected a rough patch and is protecting your account.</i>"
        )


def record_win(name: str):
    """Reset consecutive loss counter on a win."""
    market_state[name]["consecutive_losses"] = 0


# ─────────────────────────────────────────────────────────────
#  LAYER 1 — 30M BIAS CHECK
# ─────────────────────────────────────────────────────────────
def check_bias(df30: pd.DataFrame) -> str | None:
    df30 = add_indicators(df30)
    r = df30.iloc[-1]
    p = r["close"]
    bull = (p > r["ema20"] > r["ema50"] > r["ema100"] > r["ema200"]
            and r["rsi"] > 50 and r["macd"] > 0)
    bear = (p < r["ema20"] < r["ema50"] < r["ema100"] < r["ema200"]
            and r["rsi"] < 50 and r["macd"] < 0)
    return "BUY" if bull else ("SELL" if bear else None)


# ─────────────────────────────────────────────────────────────
#  LAYERS 2+3 — 5M COMPRESSION + ENTRY
# ─────────────────────────────────────────────────────────────
def check_entry(df5: pd.DataFrame, bias: str, freq: int,
                dyn_mult: float) -> dict | None:
    """
    Checks BB squeeze + ATR compression (Layer 2)
    then RSI / MACD / Stochastic timing (Layer 3).
    Uses dynamic multiplier from volatility regime for SL/TP.
    """
    df5 = add_indicators(df5)
    last = df5.iloc[-1]
    prev = df5.iloc[-2]
    cfg = BASE_SETTINGS[freq]

    # Layer 2 — compression
    recent_bw = df5["bb_width"].dropna().tail(50)
    bb_squeeze = last["bb_width"] <= np.percentile(recent_bw, 20)
    atr_low = last["atr"] < last["atr_ma"]
    if not (bb_squeeze and atr_low):
        return None

    # Layer 3 — entry
    rsi = last["rsi"]
    stoch_k = last["stoch_k"]
    prev_k = prev["stoch_k"]
    prev_h = prev["macd_h"]
    macd_ok = ((last["macd"] > last["macd_sig"])
               or (last["macd_h"] > 0 and last["macd_h"] > prev_h))

    if bias == "BUY":
        rsi_ok = cfg["rsi_buy_low"] <= rsi <= cfg["rsi_buy_high"]
        stoch_ok = stoch_k < cfg["stoch_os"] + 10 and stoch_k > prev_k
        score = sum([rsi_ok, macd_ok, stoch_ok])
        if score < 2:
            return None
        entry = last["close"]
        # Dynamic SL & TP
        sl_dist = last["atr"] * cfg["atr_sl"] * dyn_mult
        tp_dist = last["atr"] * cfg["atr_tp"] * dyn_mult
        sl = round(entry - sl_dist, 5)
        tp = round(entry + tp_dist, 5)

    elif bias == "SELL":
        rsi_ok = cfg["rsi_sell_low"] <= rsi <= cfg["rsi_sell_high"]
        macd_ok = (last["macd"] < last["macd_sig"]) or (last["macd_h"] < 0)
        stoch_ok = stoch_k > cfg["stoch_ob"] - 10 and stoch_k < prev_k
        score = sum([rsi_ok, macd_ok, stoch_ok])
        if score < 2:
            return None
        entry = last["close"]
        sl_dist = last["atr"] * cfg["atr_sl"] * dyn_mult
        tp_dist = last["atr"] * cfg["atr_tp"] * dyn_mult
        sl = round(entry + sl_dist, 5)
        tp = round(entry - tp_dist, 5)
    else:
        return None

    return {
        "direction": bias,
        "entry": round(entry, 5),
        "tp": tp,
        "sl": sl,
        "atr": round(last["atr"], 5),
        "rsi": round(rsi, 2),
        "macd": round(last["macd"], 5),
        "stoch_k": round(stoch_k, 2),
        "adx": round(last["adx"], 1) if not pd.isna(last["adx"]) else 0,
        "score": score,
        "dyn_mult": dyn_mult,
    }


# ─────────────────────────────────────────────────────────────
#  SIGNAL FORMATTER
# ─────────────────────────────────────────────────────────────
EMOJI = {
    "Boom 300": "📈", "Boom 500": "📈", "Boom 1000": "📈",
    "Crash 300": "📉", "Crash 500": "📉", "Crash 1000": "📉",
}

REGIME_LABELS = {
    "NORMAL": "🟢 Normal",
    "HIGH_VOL": "🟡 High Volatility",
    "LOW_VOL": "🔵 Low Volatility",
    "CHAOTIC": "🔴 Chaotic",
}


def format_signal(name: str, sig: dict, freq: int,
                  regime: str, session_note: str) -> str:
    cfg = BASE_SETTINGS[freq]
    rr = round((cfg["atr_tp"] * sig["dyn_mult"]) /
               (cfg["atr_sl"] * sig["dyn_mult"]), 1)
    arrow = "🟢 BUY  ▲" if sig["direction"] == "BUY" else "🔴 SELL ▼"
    stars = "⭐" * sig["score"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    vol_lbl = REGIME_LABELS.get(regime, regime)

    # Dynamic SL/TP note
    if sig["dyn_mult"] > 1.0:
        dyn_note = f"⬆️ SL/TP widened {int((sig['dyn_mult']-1)*100)}% (high vol)"
    elif sig["dyn_mult"] < 1.0:
        dyn_note = f"⬇️ SL/TP tightened {int((1-sig['dyn_mult'])*100)}% (low vol)"
    else:
        dyn_note = "➡️ SL/TP standard"

    return (
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{EMOJI.get(name, '📊')} <b>{name}</b>  |  M5 + M30\n"
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
        f"Regime    {vol_lbl}\n"
        f"Session   {session_note}\n"
        f"{dyn_note}\n\n"
        f"Confluence  {stars} ({sig['score']}/3)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ <i>Max 1-2% risk per trade.</i>\n"
        f"<i>Reply /win or /loss to track results.</i>"
    )


# ─────────────────────────────────────────────────────────────
#  DAILY SUMMARY
# ─────────────────────────────────────────────────────────────
def format_summary() -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total = sum(s["daily_signals"] for s in market_state.values())
    lines = [
        f"📋 <b>Daily Summary — {now}</b>",
        f"━━━━━━━━━━━━━━━━━━━━━━━",
        f"Total signals today: <b>{total}</b>\n",
    ]
    for name, s in market_state.items():
        if s["daily_signals"] > 0:
            lines.append(
                f"{EMOJI.get(name, '📊')} {name}:  {s['daily_signals']} signals  "
                f"(🟢{s['daily_buy']} BUY  🔴{s['daily_sell']} SELL)"
            )
    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━",
        "💡 <b>Tips for tomorrow:</b>",
        "• Only act on 3/3 confluence signals",
        "• Skip signals during off-peak sessions",
        "• Trust the auto-pause — it protects your account",
        "<i>Stats reset for new day.</i>",
    ]
    return "\n".join(lines)


def reset_daily_stats():
    for s in market_state.values():
        s["daily_signals"] = 0
        s["daily_buy"] = 0
        s["daily_sell"] = 0


# ─────────────────────────────────────────────────────────────
#  TELEGRAM COMMAND HANDLER (win/loss tracking)
#  In a full deployment this would use a webhook.
#  For simplicity, this polls for updates every cycle.
# ─────────────────────────────────────────────────────────────
last_update_id = 0


def poll_telegram_commands():
    """Check for /win or /loss replies from the user."""
    global last_update_id
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    try:
        r = requests.get(
            url, params={"offset": last_update_id + 1, "timeout": 2}, timeout=5)
        if r.status_code != 200:
            return
        updates = r.json().get("result", [])
        for upd in updates:
            last_update_id = upd["update_id"]
            text = upd.get("message", {}).get("text", "").strip().lower()
            if text.startswith("/win"):
                parts = text.split()
                mkt = " ".join(parts[1:]).title() if len(parts) > 1 else None
                if mkt and mkt in market_state:
                    record_win(mkt)
                    send_telegram(
                        f"✅ Win recorded for <b>{mkt}</b>. Streak reset.")
                else:
                    send_telegram(
                        "Usage: /win Crash 1000\nMarkets: Boom/Crash 300, 500, 1000")
            elif text.startswith("/loss"):
                parts = text.split()
                mkt = " ".join(parts[1:]).title() if len(parts) > 1 else None
                if mkt and mkt in market_state:
                    record_loss(mkt)
                    send_telegram(f"🔴 Loss recorded for <b>{mkt}</b>.")
                else:
                    send_telegram(
                        "Usage: /loss Crash 1000\nMarkets: Boom/Crash 300, 500, 1000")
            elif text == "/status":
                lines = ["📊 <b>Bot Status</b>\n"]
                for name, s in market_state.items():
                    paused, reason = is_market_paused(name)
                    regime = s.get("regime", "NORMAL")
                    status = f"⏸ PAUSED — {reason}" if paused else f"✅ Active | {REGIME_LABELS.get(regime, regime)}"
                    lines.append(f"{EMOJI.get(name, '📊')} {name}: {status}")
                send_telegram("\n".join(lines))
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────────────────────
async def run_bot():
    print("=" * 60)
    print("  BOOM & CRASH SIGNAL BOT — ADAPTIVE EDITION")
    print("  Boom 300/500/1000  |  Crash 300/500/1000")
    print("  Filters: Volatility Regime + ADX + Session + Loss Memory")
    print("=" * 60)

    last_summary = 0

    send_telegram(
        "🤖 <b>Boom &amp; Crash Signal Bot — ADAPTIVE EDITION</b>\n\n"
        "📈 Boom 300 | Boom 500 | Boom 1000\n"
        "📉 Crash 300 | Crash 500 | Crash 1000\n\n"
        "🧠 <b>Smart Filters Active:</b>\n"
        "  • Volatility Regime Detection\n"
        "  • ADX Trend Strength Filter\n"
        "  • Session Quality Filter\n"
        "  • Loss Memory Auto-Pause\n"
        "  • Dynamic SL/TP Adjustment\n\n"
        "📲 <b>Commands:</b>\n"
        "  /win Crash 1000  — record a win\n"
        "  /loss Boom 500   — record a loss\n"
        "  /status          — view all market states\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ <i>Not financial advice. Always use a stop-loss.</i>"
    )

    while True:
        now_ts = time.time()

        # Poll for /win /loss /status commands
        poll_telegram_commands()

        # Daily summary + reset
        if now_ts - last_summary >= 86400:
            send_telegram(format_summary())
            reset_daily_stats()
            last_summary = now_ts

        print(f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
              f"Scanning {len(MARKETS)} markets...")

        for name, info in MARKETS.items():
            symbol = info["symbol"]
            freq = info["freq"]
            state = market_state[name]

            try:
                # ── Cooldown ─────────────────────────────
                if now_ts - state["last_signal_time"] < COOLDOWN:
                    mins = int(
                        (COOLDOWN - (now_ts - state["last_signal_time"])) / 60)
                    print(f"  {name}: cooldown {mins}m")
                    continue

                # ── Loss memory pause ─────────────────────
                paused, pause_reason = is_market_paused(name)
                if paused:
                    print(f"  {name}: {pause_reason}")
                    continue

                print(f"  {name}: fetching data...")

                # ── Fetch candles ─────────────────────────
                df5 = await fetch_candles(symbol, 300,  100)
                df30 = await fetch_candles(symbol, 1800, 60)
                if df5.empty or df30.empty or len(df5) < 60 or len(df30) < 30:
                    print(f"  {name}: insufficient data")
                    continue

                # Add indicators to 5M for adaptive checks
                df5_ind = add_indicators(df5.copy())

                # ── Filter 1: Volatility Regime ───────────
                regime, dyn_mult = get_volatility_regime(df5_ind)
                state["regime"] = regime
                if regime == "CHAOTIC":
                    print(f"  {name}: CHAOTIC market — skipping")
                    continue

                # ── Filter 2: ADX Trend Strength ──────────
                # Need bias first for directional ADX check
                bias = check_bias(df30)
                if not bias:
                    print(f"  {name}: no 30M bias")
                    continue

                adx_ok, adx_val, adx_reason = check_adx(df5_ind, bias)
                if not adx_ok:
                    print(f"  {name}: ADX fail — {adx_reason}")
                    continue

                # ── Filter 3: Session Quality ──────────────
                good_session, session_note = check_session()
                # Off-peak sessions: still scan but require 3/3 confluence
                min_score = 2 if good_session else 3

                print(f"  {name}: bias={bias} | regime={regime} | "
                      f"ADX={adx_val:.1f} | session={'✅' if good_session else '⚠️'}")

                # ── Layers 2+3: Entry signal ───────────────
                sig = check_entry(df5_ind, bias, freq, dyn_mult)
                if not sig:
                    print(f"  {name}: 5M entry not ready")
                    continue

                # Apply session-based minimum score
                if sig["score"] < min_score:
                    print(f"  {name}: score {sig['score']}/{min_score} needed "
                          f"({'off-peak' if not good_session else 'standard'})")
                    continue

                # ── Signal fires! ──────────────────────────
                print(f"  ✅ {name}: {sig['direction']} | "
                      f"score {sig['score']}/3 | regime {regime}")

                msg = format_signal(name, sig, freq, regime, session_note)
                send_telegram(msg)

                # Update state
                state["last_signal_time"] = now_ts
                state["total_signals"] += 1
                state["daily_signals"] += 1
                direction_key = sig["direction"].lower()  # 'buy' or 'sell'
                if direction_key == "buy":
                    state["daily_buy"] += 1
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
