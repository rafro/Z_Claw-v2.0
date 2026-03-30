"""
market-scan skill — Tier 0 (pure Python signals) + Tier 1 LLM interpretation.
Tracks instruments defined in divisions/trading/assets.json via yfinance.
No hardcoded asset lists — add/remove instruments in assets.json only.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from runtime.config import SKILL_MODELS, LOGS_DIR, ROOT
from runtime.ollama_client import chat, is_available

log   = logging.getLogger(__name__)
MODEL = SKILL_MODELS["market-scan"]
HOT_DIR   = ROOT / "divisions" / "trading" / "hot"
ASSETS_FILE = ROOT / "divisions" / "trading" / "assets.json"


def _load_assets() -> list[dict]:
    """Load instrument definitions from assets.json."""
    try:
        with open(ASSETS_FILE, encoding="utf-8") as f:
            return json.load(f).get("instruments", [])
    except Exception as e:
        log.error("Failed to load assets.json: %s", e)
        return []


def _fetch_markets(instruments: list[dict]) -> tuple[list, list[str]]:
    """
    Fetch daily OHLCV for all instruments via best available provider.
    Falls back to yfinance if the provider module is unavailable or fails.
    Returns (market_list, errors).
    Each item: {"name", "ticker", "asset_class", "current_price",
                "price_change_pct_1d", "total_volume",
                "notable_move_pct", "strong_move_pct"}
    """
    results = []
    errors  = []

    # Try the new provider abstraction first
    provider = None
    try:
        from providers.market_data import get_provider
        provider = get_provider()
    except ImportError:
        pass  # providers module not available, fall through to yfinance
    except Exception as e:
        log.warning("Market data provider init failed, falling back to yfinance: %s", e)

    for inst in instruments:
        name   = inst["name"]
        ticker = inst["ticker"]

        # Attempt provider-based fetch
        if provider is not None:
            try:
                data = provider.fetch_ohlcv(ticker, timeframe="1d")
                if data and data.get("close") and len(data["close"]) >= 2:
                    closes  = data["close"]
                    volumes = data.get("volume", [])
                    current = float(closes[-1])
                    prev    = float(closes[-2])
                    chg_pct = ((current - prev) / prev * 100) if prev else 0.0
                    results.append({
                        "name":               name,
                        "ticker":             ticker,
                        "futures":            inst.get("futures", ""),
                        "asset_class":        inst.get("asset_class", ""),
                        "current_price":      round(current, 2),
                        "price_change_pct_1d": round(chg_pct, 2),
                        "total_volume":       float(volumes[-1]) if volumes else 0.0,
                        "notable_move_pct":   inst.get("notable_move_pct", 1.5),
                        "strong_move_pct":    inst.get("strong_move_pct", 3.0),
                    })
                    continue  # success via provider, skip yfinance
            except Exception as e:
                log.warning("Provider fetch failed for %s, falling back to yfinance: %s", name, e)

        # Existing yfinance code as fallback
        try:
            import yfinance as yf
            df = yf.download(ticker, period="5d", interval="1d",
                             auto_adjust=False, progress=False)
            if df.empty or len(df) < 2:
                errors.append(f"{name}: no data returned")
                continue
            if hasattr(df.columns, "levels"):
                df.columns = df.columns.get_level_values(0)
            closes  = df["Close"].tolist()
            volumes = df["Volume"].tolist()
            current = float(closes[-1])
            prev    = float(closes[-2])
            chg_pct = ((current - prev) / prev * 100) if prev else 0.0
            results.append({
                "name":               name,
                "ticker":             ticker,
                "futures":            inst.get("futures", ""),
                "asset_class":        inst.get("asset_class", ""),
                "current_price":      round(current, 2),
                "price_change_pct_1d": round(chg_pct, 2),
                "total_volume":       float(volumes[-1]) if volumes else 0.0,
                "notable_move_pct":   inst.get("notable_move_pct", 1.5),
                "strong_move_pct":    inst.get("strong_move_pct", 3.0),
            })
        except ImportError:
            errors.append("yfinance not installed — run: pip install yfinance pandas")
            break  # no point trying other instruments
        except Exception as e:
            errors.append(f"{name} ({ticker}): {e}")

    return results, errors



def _llm_interpret(market_data: list) -> str:
    """Ask LLM to summarise current prices across all tracked instruments."""
    snap = "\n".join(
        f"  {d['name']} ({d.get('futures','')}):"
        f" ${d['current_price']:,.2f} ({d['price_change_pct_1d']:+.2f}% today)"
        for d in market_data
    )
    if not is_available(MODEL):
        return snap

    messages = [
        {
            "role": "system",
            "content": (
                "You are the Trading Division market scanner for J_Claw. "
                "You track SPX500/MES, XAUUSD/MGC, CRUDE/MCL, BONDS/MBT. "
                "Write 1–2 sentences summarising today's price action for Matthew. "
                "Be direct. No filler."
            ),
        },
        {
            "role": "user",
            "content": f"Current prices:\n{snap}",
        },
    ]
    try:
        return chat(MODEL, messages, temperature=0.2, max_tokens=120, task_type="market-scan")
    except Exception as e:
        log.warning("market-scan LLM failed: %s", e)
        return snap


def run() -> dict:
    LOGS_DIR.mkdir(exist_ok=True)
    HOT_DIR.mkdir(parents=True, exist_ok=True)

    instruments = _load_assets()
    if not instruments:
        return {
            "status":  "failed",
            "escalate": False,
            "summary": "assets.json missing or empty — no instruments configured",
            "counts":  {"instruments": 0},
        }

    market_data, errors = _fetch_markets(instruments)
    for err in errors:
        log.warning("Market fetch: %s", err)

    if not market_data:
        return {
            "status":  "failed",
            "escalate": False,
            "summary": f"No market data retrieved. Errors: {'; '.join(errors)}",
            "counts":  {"instruments": 0},
        }

    summary = _llm_interpret(market_data)

    now = datetime.now(timezone.utc)
    snap_file = HOT_DIR / f"market-{now.strftime('%Y%m%d-%H%M')}.json"
    with open(snap_file, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": now.isoformat(),
            "market_data":  market_data,
            "summary":      summary,
        }, f, indent=2)

    status = "success" if not errors else ("partial" if market_data else "failed")

    return {
        "status":          status,
        "escalate":        False,
        "summary":         summary,
        "market_data":     market_data,
        "model_available": is_available(MODEL),
        "counts":          {"instruments": len(market_data)},
    }
