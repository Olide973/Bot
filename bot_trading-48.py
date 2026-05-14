"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                                                                              ║
║          ██████╗  ██████╗ ████████╗    ██╗   ██╗██████╗                     ║
║          ██╔══██╗██╔═══██╗╚══██╔══╝    ██║   ██║╚════██╗                    ║
║          ██████╔╝██║   ██║   ██║       ██║   ██║ █████╔╝                    ║
║          ██╔══██╗██║   ██║   ██║       ╚██╗ ██╔╝ ╚═══██╗                   ║
║          ██████╔╝╚██████╔╝   ██║        ╚████╔╝ ██████╔╝                    ║
║          ╚═════╝  ╚═════╝    ╚═╝         ╚═══╝  ╚═════╝                     ║
║                                                                              ║
║                  QUANTUM EDGE TRADING SYSTEM — v2.0                         ║
║          Multi-Signal · Risk-Controlled · Adaptive Intelligence              ║
║                                                                              ║
║  Marchés  : 15 paires crypto soigneusement sélectionnées                    ║
║  Signaux  : ADX · EMA · RSI · Bollinger · Volume · Tendance Macro           ║
║  Gestion  : Trailing Stop · Kill Switch · Drawdown Guard · Kelly Sizing     ║
║  Source   : Kraken API (prix) — compatible Binance Futures (live)           ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝

AVERTISSEMENT : Le trading de crypto-monnaies implique un risque de perte en
capital. Ce bot est conçu pour maximiser les probabilités de succès, mais
aucun système ne garantit des profits constants. Tradez avec prudence.
"""

# ─────────────────────────────────────────────────────────────────────────────
#  IMPORTS
# ─────────────────────────────────────────────────────────────────────────────
import asyncio
import logging
import math
import os
import statistics
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Tuple

import aiohttp

# ─────────────────────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────────────────────
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot_v2.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("QUANTUM_EDGE")

# ─────────────────────────────────────────────────────────────────────────────
#  ENUMS
# ─────────────────────────────────────────────────────────────────────────────
class Signal(Enum):
    BUY   = "BUY"
    SELL  = "SELL"
    NONE  = "NONE"

class TradeState(Enum):
    IDLE    = "IDLE"
    OPEN    = "OPEN"
    CLOSED  = "CLOSED"

class MarketRegime(Enum):
    TRENDING_UP   = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING       = "RANGING"
    VOLATILE      = "VOLATILE"

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class BotConfig:
    # ── Identité
    bot_name: str = "QUANTUM_EDGE_V2"
    simulation_mode: bool = True

    # ── Marchés (15 paires choisies pour leur liquidité, volatilité et volume)
    markets: List[str] = field(default_factory=lambda: [
        "XBT/USDT",   # Bitcoin       — valeur refuge, haute liquidité
        "ETH/USDT",   # Ethereum      — DeFi leader, très suivi
        "SOL/USDT",   # Solana        — vitesse + écosystème fort
        "BNB/USDT",   # Binance Coin  — corrélé aux volumes d'échange
        "XRP/USDT",   # Ripple        — fort momentum institutionnel
        "AVAX/USDT",  # Avalanche     — L1 compétitif, bons mouvements
        "LINK/USDT",  # Chainlink     — oracle leader, tendances nettes
        "ADA/USDT",   # Cardano       — cycles réguliers, technique lisible
        "DOT/USDT",   # Polkadot      — interopérabilité, swings propres
        "DOGE/USDT",  # Dogecoin      — forte volatilité, volumes élevés
        "MATIC/USDT", # Polygon       — scaling Ethereum, cycles courts
        "ATOM/USDT",  # Cosmos        — IBC leader, tendances franches
        "NEAR/USDT",  # NEAR Protocol — L1 émergent, bons patterns
        "APT/USDT",   # Aptos         — Move VM, volatilité exploitable
        "OP/USDT",    # Optimism      — L2 Ethereum, momentum solide
    ])

    # ── Timeframes
    tf_primary:     int = 15    # minutes — signal principal
    tf_confirmation: int = 60   # minutes — filtre macro
    candles_required: int = 200 # nb de bougies pour les indicateurs longs

    # ── Gestion du capital
    stake_eur:      float = 50.0    # mise par trade (€)
    leverage:       int   = 3       # levier
    max_open_trades: int  = 3       # trades simultanés max
    kelly_fraction: float = 0.25    # fraction Kelly (conservateur)

    # ── Profit / Perte
    target_pct:     float = 0.50    # +0.50% sur position levierisée = ~+0.75€
    stoploss_pct:   float = 1.00    # -1.00% sur position = ~-1.50€
    trailing_start: float = 0.30    # déclenche le trailing à +0.30%
    trailing_step:  float = 0.15    # step du trailing stop

    # ── Kill switch
    daily_kill_eur: float = -3.0    # arrêt si PnL journalier < -3€
    max_drawdown_pct: float = 5.0   # arrêt si drawdown > 5% du capital total

    # ── Filtres de qualité du signal
    adx_min:        int   = 22      # ADX minimum pour valider une tendance
    rsi_oversold:   int   = 35      # RSI en survente (signal BUY)
    rsi_overbought: int   = 65      # RSI en surachat (signal SELL)
    rsi_extreme_low:  int = 25      # RSI extrême bas — filtre anti-fakeout
    rsi_extreme_high: int = 75      # RSI extrême haut — filtre anti-fakeout
    volume_mult_min: float = 1.3    # volume ≥ 1.3× la moyenne pour confirmer
    bb_squeeze_threshold: float = 0.03  # détection d'une compression Bollinger

    # ── Scoring (points max = 30)
    score_min:      int   = 14      # score minimum pour ouvrir un trade

    # ── Timing
    loop_interval:  int   = 30      # secondes entre chaque cycle
    pause_after_trade: int = 120    # pause (s) après fermeture d'un trade
    candle_fetch_timeout: int = 10  # timeout Kraken API (s)

    # ── Capital initial (simulation)
    initial_capital: float = 200.0

    # ── Frais Binance Futures (taker)
    fee_pct: float = 0.04  # 0.04% par trade (entrée + sortie = ~0.08%)


# ─────────────────────────────────────────────────────────────────────────────
#  STRUCTURES DE DONNÉES
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Candle:
    timestamp: int
    open:   float
    high:   float
    low:    float
    close:  float
    volume: float


@dataclass
class Trade:
    market:       str
    side:         Signal
    entry_price:  float
    stake:        float
    leverage:     int
    entry_time:   datetime
    state:        TradeState = TradeState.OPEN
    exit_price:   Optional[float] = None
    exit_time:    Optional[datetime] = None
    pnl_eur:      float = 0.0
    trailing_stop: Optional[float] = None
    peak_price:   Optional[float] = None
    score:        int = 0
    regime:       MarketRegime = MarketRegime.RANGING

    def position_size(self) -> float:
        """Taille de la position en € (stake × levier)."""
        return self.stake * self.leverage

    def unrealized_pnl(self, current_price: float) -> float:
        """PnL non réalisé en €."""
        if self.side == Signal.BUY:
            pct = (current_price - self.entry_price) / self.entry_price
        else:
            pct = (self.entry_price - current_price) / self.entry_price
        gross = self.position_size() * pct
        fees  = self.position_size() * (cfg.fee_pct / 100) * 2
        return gross - fees

    def target_price(self) -> float:
        mult = 1 + cfg.target_pct / 100 / cfg.leverage
        if self.side == Signal.BUY:
            return self.entry_price * mult
        return self.entry_price / mult

    def stoploss_price(self) -> float:
        mult = 1 + cfg.stoploss_pct / 100 / cfg.leverage
        if self.side == Signal.BUY:
            return self.entry_price / mult
        return self.entry_price * mult


# ─────────────────────────────────────────────────────────────────────────────
#  INDICATEURS TECHNIQUES
# ─────────────────────────────────────────────────────────────────────────────
class Indicators:
    """Calculs d'indicateurs purement numériques, sans dépendances externes."""

    @staticmethod
    def ema(values: List[float], period: int) -> List[float]:
        if len(values) < period:
            return []
        k = 2 / (period + 1)
        result = [sum(values[:period]) / period]
        for v in values[period:]:
            result.append(v * k + result[-1] * (1 - k))
        return result

    @staticmethod
    def sma(values: List[float], period: int) -> List[float]:
        return [
            sum(values[i:i+period]) / period
            for i in range(len(values) - period + 1)
        ]

    @staticmethod
    def rsi(closes: List[float], period: int = 14) -> float:
        if len(closes) < period + 1:
            return 50.0
        deltas = [closes[i+1] - closes[i] for i in range(len(closes)-1)]
        gains  = [max(d, 0) for d in deltas[-period:]]
        losses = [abs(min(d, 0)) for d in deltas[-period:]]
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    @staticmethod
    def adx(candles: List[Candle], period: int = 14) -> Tuple[float, float, float]:
        """Retourne (ADX, +DI, -DI)."""
        if len(candles) < period + 1:
            return 0.0, 0.0, 0.0

        tr_list, pdm_list, ndm_list = [], [], []
        for i in range(1, len(candles)):
            h, l, pc = candles[i].high, candles[i].low, candles[i-1].close
            tr  = max(h - l, abs(h - pc), abs(l - pc))
            pdm = max(h - candles[i-1].high, 0) if (h - candles[i-1].high) > (candles[i-1].low - l) else 0
            ndm = max(candles[i-1].low - l, 0) if (candles[i-1].low - l) > (h - candles[i-1].high) else 0
            tr_list.append(tr)
            pdm_list.append(pdm)
            ndm_list.append(ndm)

        def smoothed(lst):
            s = sum(lst[:period])
            result = [s]
            for v in lst[period:]:
                result.append(result[-1] - result[-1]/period + v)
            return result

        atr  = smoothed(tr_list)
        pDM  = smoothed(pdm_list)
        nDM  = smoothed(ndm_list)

        dx_list = []
        pdi_list, ndi_list = [], []
        for i in range(len(atr)):
            pdi = 100 * pDM[i] / atr[i] if atr[i] else 0
            ndi = 100 * nDM[i] / atr[i] if atr[i] else 0
            pdi_list.append(pdi)
            ndi_list.append(ndi)
            s = pdi + ndi
            dx_list.append(100 * abs(pdi - ndi) / s if s else 0)

        if len(dx_list) < period:
            return 0.0, pdi_list[-1] if pdi_list else 0.0, ndi_list[-1] if ndi_list else 0.0

        adx_val = sum(dx_list[-period:]) / period
        return adx_val, pdi_list[-1], ndi_list[-1]

    @staticmethod
    def bollinger_bands(closes: List[float], period: int = 20, std_dev: float = 2.0) -> Tuple[float, float, float]:
        """Retourne (upper, middle, lower)."""
        if len(closes) < period:
            c = closes[-1]
            return c, c, c
        window = closes[-period:]
        mid    = sum(window) / period
        std    = statistics.stdev(window)
        return mid + std_dev * std, mid, mid - std_dev * std

    @staticmethod
    def atr(candles: List[Candle], period: int = 14) -> float:
        if len(candles) < period + 1:
            return 0.0
        trs = []
        for i in range(1, len(candles)):
            h, l, pc = candles[i].high, candles[i].low, candles[i-1].close
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        return sum(trs[-period:]) / period

    @staticmethod
    def volume_ratio(candles: List[Candle], period: int = 20) -> float:
        """Volume de la dernière bougie vs moyenne des N précédentes."""
        if len(candles) < period + 1:
            return 1.0
        avg = sum(c.volume for c in candles[-period-1:-1]) / period
        return candles[-1].volume / avg if avg > 0 else 1.0

    @staticmethod
    def detect_regime(candles: List[Candle], ema_fast: List[float], ema_slow: List[float], adx_val: float) -> MarketRegime:
        """Identifie le régime de marché courant."""
        if len(ema_fast) < 2 or len(ema_slow) < 2:
            return MarketRegime.RANGING
        closes = [c.close for c in candles[-20:]]
        volatility = statistics.stdev(closes) / (sum(closes)/len(closes)) * 100
        if volatility > 4.0:
            return MarketRegime.VOLATILE
        if adx_val >= 25:
            if ema_fast[-1] > ema_slow[-1]:
                return MarketRegime.TRENDING_UP
            return MarketRegime.TRENDING_DOWN
        return MarketRegime.RANGING

    @staticmethod
    def detect_divergence(closes: List[float], rsi_series: List[float], lookback: int = 5) -> Optional[str]:
        """Détecte une divergence RSI/prix (haussière ou baissière)."""
        if len(closes) < lookback + 1 or len(rsi_series) < lookback + 1:
            return None
        price_trend = closes[-1] - closes[-lookback]
        rsi_trend   = rsi_series[-1] - rsi_series[-lookback]
        if price_trend < 0 and rsi_trend > 0:
            return "BULLISH_DIVERGENCE"
        if price_trend > 0 and rsi_trend < 0:
            return "BEARISH_DIVERGENCE"
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  MOTEUR DE SIGNAL
# ─────────────────────────────────────────────────────────────────────────────
class SignalEngine:
    """
    Calcule un score composite (0–30) à partir de 6 indicateurs.
    Un score ≥ cfg.score_min déclenche un trade.

    Indicateurs & pondération :
    ┌─────────────────┬─────────────────────────────────────────┬────────┐
    │ Indicateur      │ Condition                               │ Points │
    ├─────────────────┼─────────────────────────────────────────┼────────┤
    │ Tendance Macro  │ EMA200 direction 1h                     │ 0–7    │
    │ ADX             │ Force de tendance                       │ 0–6    │
    │ EMA Cross       │ EMA9 × EMA21 croisement                 │ 0–5    │
    │ RSI             │ Zone survente/surachat                  │ 0–5    │
    │ Bollinger       │ Prix proche bande + squeeze             │ 0–4    │
    │ Volume          │ Confirmation par le volume              │ 0–3    │
    └─────────────────┴─────────────────────────────────────────┴────────┘
    """

    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.ind = Indicators()

    def compute(
        self,
        candles_15m: List[Candle],
        candles_1h:  List[Candle],
    ) -> Tuple[Signal, int, Dict, MarketRegime]:
        """
        Retourne (signal, score, details, regime).
        """
        closes_15m = [c.close for c in candles_15m]
        closes_1h  = [c.close for c in candles_1h]

        # ── Indicateurs primaires (15m)
        rsi_val  = self.ind.rsi(closes_15m)
        adx_val, pdi, ndi = self.ind.adx(candles_15m)
        bb_up, bb_mid, bb_lo = self.ind.bollinger_bands(closes_15m)
        vol_ratio = self.ind.volume_ratio(candles_15m)
        ema9  = self.ind.ema(closes_15m, 9)
        ema21 = self.ind.ema(closes_15m, 21)
        ema50 = self.ind.ema(closes_15m, 50)

        # ── Indicateurs macro (1h)
        ema200_1h = self.ind.ema(closes_1h, 200) if len(closes_1h) >= 200 else self.ind.ema(closes_1h, len(closes_1h)//2 or 1)
        ema_fast_1h = self.ind.ema(closes_1h, 20)
        ema_slow_1h = self.ind.ema(closes_1h, 50)

        price = closes_15m[-1]
        macro_bull = len(ema200_1h) > 0 and price > ema200_1h[-1]
        macro_bear = len(ema200_1h) > 0 and price < ema200_1h[-1]

        # ── Régime de marché
        regime = self.ind.detect_regime(candles_15m, ema9, ema21, adx_val)

        # ── Squeeze Bollinger
        bb_width = (bb_up - bb_lo) / bb_mid if bb_mid else 0
        bb_squeeze = bb_width < self.cfg.bb_squeeze_threshold

        # ── RSI série pour divergence
        rsi_series = []
        for i in range(max(1, len(closes_15m)-20), len(closes_15m)):
            rsi_series.append(self.ind.rsi(closes_15m[:i+1]))
        divergence = self.ind.detect_divergence(closes_15m[-20:], rsi_series)

        # ─── SCORE BUY ────────────────────────────────────────────────────
        buy_score = 0
        buy_details: Dict[str, int] = {}

        # 1. Tendance macro (0–7)
        if macro_bull:
            s = 4
            if len(ema_fast_1h) > 0 and len(ema_slow_1h) > 0 and ema_fast_1h[-1] > ema_slow_1h[-1]:
                s += 3
            buy_score += s
            buy_details["macro"] = s
        else:
            buy_details["macro"] = 0

        # 2. ADX (0–6)
        if adx_val >= self.cfg.adx_min and pdi > ndi:
            s = min(6, int((adx_val - self.cfg.adx_min) / 5) + 3)
            buy_score += s
            buy_details["adx"] = s
        elif adx_val >= self.cfg.adx_min * 0.8 and pdi > ndi:
            buy_score += 2
            buy_details["adx"] = 2
        else:
            buy_details["adx"] = 0

        # 3. EMA Cross (0–5)
        if len(ema9) >= 2 and len(ema21) >= 2:
            cross_up = ema9[-1] > ema21[-1] and ema9[-2] <= ema21[-2]
            above    = ema9[-1] > ema21[-1]
            trend_ok = len(ema50) > 0 and ema21[-1] > ema50[-1]
            if cross_up:
                buy_score += 5
                buy_details["ema_cross"] = 5
            elif above and trend_ok:
                buy_score += 3
                buy_details["ema_cross"] = 3
            elif above:
                buy_score += 1
                buy_details["ema_cross"] = 1
            else:
                buy_details["ema_cross"] = 0
        else:
            buy_details["ema_cross"] = 0

        # 4. RSI (0–5)
        if self.cfg.rsi_extreme_low <= rsi_val <= self.cfg.rsi_oversold:
            buy_score += 5
            buy_details["rsi"] = 5
        elif rsi_val <= self.cfg.rsi_oversold + 5:
            buy_score += 3
            buy_details["rsi"] = 3
        elif rsi_val <= 50:
            buy_score += 1
            buy_details["rsi"] = 1
        else:
            buy_details["rsi"] = 0

        # 4b. Bonus divergence haussière
        if divergence == "BULLISH_DIVERGENCE":
            buy_score += 2
            buy_details["divergence"] = 2

        # 5. Bollinger (0–4)
        if price <= bb_lo:
            s = 4 if not bb_squeeze else 2
            buy_score += s
            buy_details["bollinger"] = s
        elif price <= bb_mid:
            buy_score += 1
            buy_details["bollinger"] = 1
        else:
            buy_details["bollinger"] = 0

        # 6. Volume (0–3)
        if vol_ratio >= self.cfg.volume_mult_min * 1.5:
            buy_score += 3
            buy_details["volume"] = 3
        elif vol_ratio >= self.cfg.volume_mult_min:
            buy_score += 2
            buy_details["volume"] = 2
        elif vol_ratio >= 1.0:
            buy_score += 1
            buy_details["volume"] = 1
        else:
            buy_details["volume"] = 0

        # ─── SCORE SELL ───────────────────────────────────────────────────
        sell_score = 0
        sell_details: Dict[str, int] = {}

        # 1. Tendance macro
        if macro_bear:
            s = 4
            if len(ema_fast_1h) > 0 and len(ema_slow_1h) > 0 and ema_fast_1h[-1] < ema_slow_1h[-1]:
                s += 3
            sell_score += s
            sell_details["macro"] = s
        else:
            sell_details["macro"] = 0

        # 2. ADX
        if adx_val >= self.cfg.adx_min and ndi > pdi:
            s = min(6, int((adx_val - self.cfg.adx_min) / 5) + 3)
            sell_score += s
            sell_details["adx"] = s
        elif adx_val >= self.cfg.adx_min * 0.8 and ndi > pdi:
            sell_score += 2
            sell_details["adx"] = 2
        else:
            sell_details["adx"] = 0

        # 3. EMA Cross
        if len(ema9) >= 2 and len(ema21) >= 2:
            cross_dn = ema9[-1] < ema21[-1] and ema9[-2] >= ema21[-2]
            below    = ema9[-1] < ema21[-1]
            trend_ok = len(ema50) > 0 and ema21[-1] < ema50[-1]
            if cross_dn:
                sell_score += 5
                sell_details["ema_cross"] = 5
            elif below and trend_ok:
                sell_score += 3
                sell_details["ema_cross"] = 3
            elif below:
                sell_score += 1
                sell_details["ema_cross"] = 1
            else:
                sell_details["ema_cross"] = 0
        else:
            sell_details["ema_cross"] = 0

        # 4. RSI
        if self.cfg.rsi_overbought <= rsi_val <= self.cfg.rsi_extreme_high:
            sell_score += 5
            sell_details["rsi"] = 5
        elif rsi_val >= self.cfg.rsi_overbought - 5:
            sell_score += 3
            sell_details["rsi"] = 3
        elif rsi_val >= 50:
            sell_score += 1
            sell_details["rsi"] = 1
        else:
            sell_details["rsi"] = 0

        # 4b. Bonus divergence baissière
        if divergence == "BEARISH_DIVERGENCE":
            sell_score += 2
            sell_details["divergence"] = 2

        # 5. Bollinger
        if price >= bb_up:
            s = 4 if not bb_squeeze else 2
            sell_score += s
            sell_details["bollinger"] = s
        elif price >= bb_mid:
            sell_score += 1
            sell_details["bollinger"] = 1
        else:
            sell_details["bollinger"] = 0

        # 6. Volume
        if vol_ratio >= self.cfg.volume_mult_min * 1.5:
            sell_score += 3
            sell_details["volume"] = 3
        elif vol_ratio >= self.cfg.volume_mult_min:
            sell_score += 2
            sell_details["volume"] = 2
        elif vol_ratio >= 1.0:
            sell_score += 1
            sell_details["volume"] = 1
        else:
            sell_details["volume"] = 0

        # ─── FILTRE ANTI-FAKEOUT ─────────────────────────────────────────
        # Si le marché est en range et l'ADX est faible → diviser le score par 2
        if regime == MarketRegime.RANGING and adx_val < 18:
            buy_score  = buy_score // 2
            sell_score = sell_score // 2

        # Si volatilité extrême → pénaliser
        if regime == MarketRegime.VOLATILE:
            buy_score  = int(buy_score * 0.7)
            sell_score = int(sell_score * 0.7)

        # ─── DÉCISION ────────────────────────────────────────────────────
        details = {}
        if buy_score >= self.cfg.score_min and buy_score > sell_score:
            details = buy_details
            details["total"] = buy_score
            details["rsi_val"] = round(rsi_val, 1)
            details["adx_val"] = round(adx_val, 1)
            details["vol_ratio"] = round(vol_ratio, 2)
            details["regime"] = regime.value
            return Signal.BUY, buy_score, details, regime

        if sell_score >= self.cfg.score_min and sell_score > buy_score:
            details = sell_details
            details["total"] = sell_score
            details["rsi_val"] = round(rsi_val, 1)
            details["adx_val"] = round(adx_val, 1)
            details["vol_ratio"] = round(vol_ratio, 2)
            details["regime"] = regime.value
            return Signal.SELL, sell_score, details, regime

        return Signal.NONE, max(buy_score, sell_score), {}, regime


# ─────────────────────────────────────────────────────────────────────────────
#  GESTIONNAIRE DE RISQUE
# ─────────────────────────────────────────────────────────────────────────────
class RiskManager:
    """Gère le capital, les drawdowns, le trailing stop et le kill switch."""

    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.capital = cfg.initial_capital
        self.daily_pnl = 0.0
        self.peak_capital = cfg.initial_capital
        self.trades_today = 0
        self.killed = False
        self.kill_reason = ""
        self._last_reset = datetime.now(timezone.utc).date()

    def reset_daily(self):
        today = datetime.now(timezone.utc).date()
        if today != self._last_reset:
            log.info(f"[RISK] Réinitialisation journalière | PnL hier : {self.daily_pnl:+.2f}€")
            self.daily_pnl = 0.0
            self.trades_today = 0
            self._last_reset = today
            # Kill switch journalier reset (seul le drawdown général reste)
            if self.killed and "journalier" in self.kill_reason:
                self.killed = False
                self.kill_reason = ""
                log.info("[RISK] Kill switch journalier levé pour nouveau jour.")

    def check_kill_switch(self) -> bool:
        self.reset_daily()
        # Kill switch journalier
        if self.daily_pnl <= self.cfg.daily_kill_eur:
            self.killed = True
            self.kill_reason = f"perte journalière {self.daily_pnl:.2f}€ ≤ {self.cfg.daily_kill_eur}€"
            return True
        # Drawdown global
        drawdown_pct = (self.peak_capital - self.capital) / self.peak_capital * 100
        if drawdown_pct >= self.cfg.max_drawdown_pct:
            self.killed = True
            self.kill_reason = f"drawdown global {drawdown_pct:.1f}% ≥ {self.cfg.max_drawdown_pct}%"
            return True
        return False

    def register_pnl(self, pnl: float):
        self.capital    += pnl
        self.daily_pnl  += pnl
        self.peak_capital = max(self.peak_capital, self.capital)
        self.trades_today += 1

    def kelly_stake(self, win_rate: float, win_pct: float, loss_pct: float) -> float:
        """Fraction Kelly conservatrice pour dimensionner la mise."""
        if loss_pct == 0:
            return self.cfg.stake_eur
        b = win_pct / loss_pct
        kelly = (b * win_rate - (1 - win_rate)) / b
        kelly = max(0, kelly) * self.cfg.kelly_fraction
        stake = self.capital * kelly
        return max(10.0, min(stake, self.cfg.stake_eur))

    def update_trailing_stop(self, trade: Trade, current_price: float) -> Optional[float]:
        """Met à jour le trailing stop. Retourne le nouveau stop ou None."""
        if trade.trailing_stop is None:
            # Initialiser si on a atteint le seuil de déclenchement
            if trade.side == Signal.BUY:
                pct_gain = (current_price - trade.entry_price) / trade.entry_price * 100 * trade.leverage
                if pct_gain >= self.cfg.trailing_start:
                    stop = current_price * (1 - self.cfg.trailing_step / 100 / trade.leverage)
                    trade.trailing_stop = stop
                    trade.peak_price = current_price
                    log.info(f"[TRAILING] {trade.market} Trailing stop activé @ {stop:.6f}")
                    return stop
            else:
                pct_gain = (trade.entry_price - current_price) / trade.entry_price * 100 * trade.leverage
                if pct_gain >= self.cfg.trailing_start:
                    stop = current_price * (1 + self.cfg.trailing_step / 100 / trade.leverage)
                    trade.trailing_stop = stop
                    trade.peak_price = current_price
                    log.info(f"[TRAILING] {trade.market} Trailing stop activé @ {stop:.6f}")
                    return stop
        else:
            # Mise à jour du pic et déplacement du stop
            if trade.side == Signal.BUY and current_price > (trade.peak_price or 0):
                trade.peak_price = current_price
                new_stop = current_price * (1 - self.cfg.trailing_step / 100 / trade.leverage)
                if new_stop > trade.trailing_stop:
                    trade.trailing_stop = new_stop
                    return new_stop
            elif trade.side == Signal.SELL and current_price < (trade.peak_price or float("inf")):
                trade.peak_price = current_price
                new_stop = current_price * (1 + self.cfg.trailing_step / 100 / trade.leverage)
                if new_stop < trade.trailing_stop:
                    trade.trailing_stop = new_stop
                    return new_stop
        return trade.trailing_stop

    def should_exit(self, trade: Trade, current_price: float) -> Tuple[bool, str]:
        """Vérifie si un trade doit être fermé."""
        # Mise à jour trailing
        self.update_trailing_stop(trade, current_price)

        if trade.side == Signal.BUY:
            if current_price >= trade.target_price():
                return True, "TARGET"
            if current_price <= trade.stoploss_price():
                return True, "STOPLOSS"
            if trade.trailing_stop and current_price <= trade.trailing_stop:
                return True, "TRAILING_STOP"
        else:
            if current_price <= trade.target_price():
                return True, "TARGET"
            if current_price >= trade.stoploss_price():
                return True, "STOPLOSS"
            if trade.trailing_stop and current_price >= trade.trailing_stop:
                return True, "TRAILING_STOP"
        return False, ""


# ─────────────────────────────────────────────────────────────────────────────
#  CLIENT KRAKEN (source de prix)
# ─────────────────────────────────────────────────────────────────────────────
class KrakenClient:
    """Récupère les bougies OHLCV via l'API REST Kraken."""

    BASE_URL = "https://api.kraken.com/0/public"

    # Mapping des symboles Kraken → Binance
    SYMBOL_MAP = {
        "XBT/USDT": "XBTUSDT",
        "ETH/USDT": "ETHUSDT",
        "SOL/USDT": "SOLUSDT",
        "BNB/USDT": "BNBUSDT",
        "XRP/USDT": "XRPUSDT",
        "AVAX/USDT": "AVAXUSDT",
        "LINK/USDT": "LINKUSDT",
        "ADA/USDT": "ADAUSDT",
        "DOT/USDT": "DOTUSDT",
        "DOGE/USDT": "DOGEUSDT",
        "MATIC/USDT": "MATICUSDT",
        "ATOM/USDT": "ATOMUSDT",
        "NEAR/USDT": "NEARUSDT",
        "APT/USDT": "APTUSDT",
        "OP/USDT": "OPUSDT",
    }

    def __init__(self, timeout: int = 10):
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _kraken_pair(self, market: str) -> str:
        return self.SYMBOL_MAP.get(market, market.replace("/", ""))

    async def fetch_ohlcv(self, market: str, interval_min: int, count: int = 200) -> List[Candle]:
        """
        Récupère `count` bougies OHLCV pour `market` avec l'intervalle donné.
        interval_min : 1, 5, 15, 30, 60, 240, 1440, 10080, 21600
        """
        pair = self._kraken_pair(market)
        url  = f"{self.BASE_URL}/OHLC"
        params = {"pair": pair, "interval": interval_min}

        try:
            session = await self._get_session()
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    log.warning(f"[KRAKEN] HTTP {resp.status} pour {market}")
                    return []
                data = await resp.json()

            if data.get("error"):
                log.warning(f"[KRAKEN] Erreur API {market}: {data['error']}")
                return []

            result_key = [k for k in data["result"] if k != "last"]
            if not result_key:
                return []

            raw = data["result"][result_key[0]]
            candles = [
                Candle(
                    timestamp=int(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[6]),
                )
                for row in raw
            ]
            return candles[-count:]

        except asyncio.TimeoutError:
            log.warning(f"[KRAKEN] Timeout pour {market}")
            return []
        except Exception as e:
            log.error(f"[KRAKEN] Exception {market}: {e}")
            return []

    async def fetch_ticker(self, market: str) -> Optional[float]:
        """Prix actuel du marché."""
        pair = self._kraken_pair(market)
        url  = f"{self.BASE_URL}/Ticker"
        try:
            session = await self._get_session()
            async with session.get(url, params={"pair": pair}) as resp:
                data = await resp.json()
            if data.get("error"):
                return None
            result_key = list(data["result"].keys())[0]
            return float(data["result"][result_key]["c"][0])
        except Exception:
            return None


# ─────────────────────────────────────────────────────────────────────────────
#  STATISTIQUES & PERFORMANCE
# ─────────────────────────────────────────────────────────────────────────────
class PerformanceTracker:
    """Suit et affiche les statistiques du bot en temps réel."""

    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.all_trades: List[Trade] = []
        self.pnl_history: deque = deque(maxlen=500)
        self.win_streak: int = 0
        self.loss_streak: int = 0
        self.max_win_streak: int = 0
        self.max_loss_streak: int = 0
        self.start_time = datetime.now(timezone.utc)

    def record(self, trade: Trade):
        self.all_trades.append(trade)
        self.pnl_history.append(trade.pnl_eur)
        if trade.pnl_eur > 0:
            self.win_streak  += 1
            self.loss_streak  = 0
            self.max_win_streak = max(self.max_win_streak, self.win_streak)
        else:
            self.loss_streak += 1
            self.win_streak   = 0
            self.max_loss_streak = max(self.max_loss_streak, self.loss_streak)

    @property
    def total_trades(self) -> int:
        return len(self.all_trades)

    @property
    def winning_trades(self) -> int:
        return sum(1 for t in self.all_trades if t.pnl_eur > 0)

    @property
    def win_rate(self) -> float:
        if not self.total_trades:
            return 0.0
        return self.winning_trades / self.total_trades * 100

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl_eur for t in self.all_trades)

    @property
    def avg_win(self) -> float:
        wins = [t.pnl_eur for t in self.all_trades if t.pnl_eur > 0]
        return sum(wins) / len(wins) if wins else 0.0

    @property
    def avg_loss(self) -> float:
        losses = [t.pnl_eur for t in self.all_trades if t.pnl_eur < 0]
        return sum(losses) / len(losses) if losses else 0.0

    @property
    def profit_factor(self) -> float:
        gross_win  = sum(t.pnl_eur for t in self.all_trades if t.pnl_eur > 0)
        gross_loss = abs(sum(t.pnl_eur for t in self.all_trades if t.pnl_eur < 0))
        return gross_win / gross_loss if gross_loss else float("inf")

    def sharpe_ratio(self) -> float:
        if len(self.pnl_history) < 10:
            return 0.0
        pnls = list(self.pnl_history)
        mean = sum(pnls) / len(pnls)
        std  = statistics.stdev(pnls) if len(pnls) > 1 else 1
        return (mean / std) * math.sqrt(252) if std else 0.0

    def print_dashboard(self, capital: float, daily_pnl: float, open_trades: int):
        uptime = datetime.now(timezone.utc) - self.start_time
        hours, rem = divmod(int(uptime.total_seconds()), 3600)
        mins  = rem // 60

        bar = "═" * 65
        print(f"\n{bar}")
        print(f"  ⚡ QUANTUM EDGE — TABLEAU DE BORD")
        print(f"  Uptime : {hours}h{mins:02d}m  |  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
        print(bar)
        print(f"  💰 Capital         : {capital:>10.2f} €  (départ: {self.cfg.initial_capital:.2f}€)")
        print(f"  📈 PnL total       : {self.total_pnl:>+10.2f} €")
        print(f"  📅 PnL journalier  : {daily_pnl:>+10.2f} €")
        print(f"  🔓 Trades ouverts  : {open_trades}")
        print(bar)
        print(f"  📊 Trades total    : {self.total_trades}")
        print(f"  ✅ Win rate        : {self.win_rate:>6.1f}%")
        print(f"  💚 Gain moyen      : {self.avg_win:>+8.2f} €")
        print(f"  🔴 Perte moyenne   : {self.avg_loss:>+8.2f} €")
        print(f"  🏆 Profit Factor   : {self.profit_factor:>8.2f}")
        print(f"  📐 Sharpe Ratio    : {self.sharpe_ratio():>8.2f}")
        print(f"  🔥 Best streak     : {self.max_win_streak} wins  |  {self.max_loss_streak} losses max")
        print(bar)

    def print_trade_closed(self, trade: Trade, reason: str):
        icon = "✅" if trade.pnl_eur > 0 else "❌"
        duration = (trade.exit_time - trade.entry_time).seconds // 60 if trade.exit_time else 0
        print(
            f"\n  {icon} TRADE FERMÉ  {trade.market:<12} "
            f"{trade.side.value:<4}  "
            f"Entrée:{trade.entry_price:.6f}  "
            f"Sortie:{trade.exit_price:.6f}  "
            f"PnL:{trade.pnl_eur:>+7.2f}€  "
            f"({reason})  "
            f"Score:{trade.score}  "
            f"Durée:{duration}min"
        )


# ─────────────────────────────────────────────────────────────────────────────
#  BOT PRINCIPAL
# ─────────────────────────────────────────────────────────────────────────────
class QuantumEdgeBot:
    """
    Orchestrateur principal.

    Cycle de vie :
    1. Fetch candles 15m + 1h pour chaque marché
    2. Calculer le signal composite
    3. Ouvrir un trade si score ≥ score_min et slots disponibles
    4. Gérer les trades ouverts (trailing, target, SL)
    5. Appliquer le kill switch
    6. Afficher le dashboard toutes les N itérations
    """

    def __init__(self, cfg: BotConfig):
        self.cfg     = cfg
        self.kraken  = KrakenClient(timeout=cfg.candle_fetch_timeout)
        self.engine  = SignalEngine(cfg)
        self.risk    = RiskManager(cfg)
        self.perf    = PerformanceTracker(cfg)
        self.open_trades: Dict[str, Trade] = {}   # market → Trade
        self.paused_until: Dict[str, float] = {}  # market → timestamp
        self._iter   = 0
        self._win_history: deque = deque(maxlen=50)  # pour Kelly dynamique

    # ── Propriétés de commodité
    @property
    def _win_rate_recent(self) -> float:
        if not self._win_history:
            return 0.55  # prior conservateur
        return sum(self._win_history) / len(self._win_history)

    # ── Ouverture d'un trade
    def _open_trade(self, market: str, signal: Signal, price: float, score: int, regime: MarketRegime) -> Trade:
        # Dimensionnement Kelly
        stake = self.risk.kelly_stake(
            win_rate = self._win_rate_recent,
            win_pct  = self.cfg.target_pct / 100,
            loss_pct = self.cfg.stoploss_pct / 100,
        )
        trade = Trade(
            market      = market,
            side        = signal,
            entry_price = price,
            stake       = stake,
            leverage    = self.cfg.leverage,
            entry_time  = datetime.now(timezone.utc),
            score       = score,
            regime      = regime,
        )
        self.open_trades[market] = trade
        log.info(
            f"[OPEN]  {market:<12} {signal.value:<4} "
            f"@ {price:.6f}  "
            f"mise={stake:.2f}€  "
            f"score={score}/30  "
            f"régime={regime.value}  "
            f"target={trade.target_price():.6f}  "
            f"SL={trade.stoploss_price():.6f}"
        )
        return trade

    # ── Fermeture d'un trade
    def _close_trade(self, market: str, price: float, reason: str):
        trade = self.open_trades.pop(market)
        trade.exit_price = price
        trade.exit_time  = datetime.now(timezone.utc)
        trade.state      = TradeState.CLOSED
        trade.pnl_eur    = trade.unrealized_pnl(price)

        self.risk.register_pnl(trade.pnl_eur)
        self.perf.record(trade)
        self._win_history.append(1 if trade.pnl_eur > 0 else 0)
        self.paused_until[market] = time.time() + self.cfg.pause_after_trade

        self.perf.print_trade_closed(trade, reason)

        if self.cfg.simulation_mode:
            log.info(
                f"[SIM]   Capital simulé : {self.risk.capital:.2f}€  "
                f"| PnL journalier : {self.risk.daily_pnl:+.2f}€"
            )

    # ── Cycle principal pour un marché
    async def _process_market(self, market: str):
        # Vérifier pause post-trade
        if market in self.paused_until and time.time() < self.paused_until[market]:
            return

        # Fetch candles
        candles_15m = await self.kraken.fetch_ohlcv(market, self.cfg.tf_primary, self.cfg.candles_required)
        candles_1h  = await self.kraken.fetch_ohlcv(market, self.cfg.tf_confirmation, self.cfg.candles_required)

        if len(candles_15m) < 50 or len(candles_1h) < 50:
            log.debug(f"[{market}] Données insuffisantes ({len(candles_15m)} bougie(s) 15m)")
            return

        price = candles_15m[-1].close

        # ── Gestion des trades ouverts
        if market in self.open_trades:
            trade = self.open_trades[market]
            should_exit, reason = self.risk.should_exit(trade, price)
            if should_exit:
                self._close_trade(market, price, reason)
            return  # un seul trade par marché

        # ── Slots disponibles ?
        if len(self.open_trades) >= self.cfg.max_open_trades:
            return

        # ── Calcul du signal
        signal, score, details, regime = self.engine.compute(candles_15m, candles_1h)

        if signal != Signal.NONE:
            log.info(
                f"[SIGNAL] {market:<12} {signal.value:<4} "
                f"score={score}/30  "
                f"RSI={details.get('rsi_val')}  "
                f"ADX={details.get('adx_val')}  "
                f"Vol={details.get('vol_ratio')}x  "
                f"régime={details.get('regime')}"
            )
            self._open_trade(market, signal, price, score, regime)

    # ── Boucle principale
    async def run(self):
        mode = "🔴 SIMULATION" if self.cfg.simulation_mode else "🟢 LIVE"
        log.info(f"╔══ QUANTUM EDGE démarré — Mode : {mode} ══╗")
        log.info(f"║  Marchés : {len(self.cfg.markets)}  |  Capital : {self.cfg.initial_capital}€  |  Levier : x{self.cfg.leverage}")
        log.info(f"║  Score min : {self.cfg.score_min}/30  |  Target : +{self.cfg.target_pct}%  |  SL : -{self.cfg.stoploss_pct}%")
        log.info(f"╚{'═'*50}╝\n")

        try:
            while True:
                self._iter += 1
                self.risk.reset_daily()

                # Kill switch
                if self.risk.killed:
                    log.warning(f"[KILL] Bot suspendu : {self.risk.kill_reason}")
                    await asyncio.sleep(60)
                    continue

                if self.risk.check_kill_switch():
                    log.warning(f"[KILL] Kill switch déclenché : {self.risk.kill_reason}")
                    # Fermer tous les trades ouverts au prix actuel
                    for market in list(self.open_trades.keys()):
                        price = await self.kraken.fetch_ticker(market)
                        if price:
                            self._close_trade(market, price, "KILL_SWITCH")
                    continue

                # Traiter tous les marchés en parallèle (limité à 5 simultanés)
                sem = asyncio.Semaphore(5)
                async def _safe_process(m: str):
                    async with sem:
                        try:
                            await self._process_market(m)
                        except Exception as e:
                            log.error(f"[ERROR] {m}: {e}")

                await asyncio.gather(*[_safe_process(m) for m in self.cfg.markets])

                # Dashboard toutes les 20 itérations
                if self._iter % 20 == 0:
                    self.perf.print_dashboard(
                        capital     = self.risk.capital,
                        daily_pnl   = self.risk.daily_pnl,
                        open_trades = len(self.open_trades),
                    )

                # Log des trades ouverts
                if self.open_trades and self._iter % 4 == 0:
                    for market, trade in self.open_trades.items():
                        price = await self.kraken.fetch_ticker(market)
                        if price:
                            upnl = trade.unrealized_pnl(price)
                            log.info(
                                f"[OPEN]  {market:<12} {trade.side.value}  "
                                f"prix={price:.6f}  "
                                f"PnL non-réalisé={upnl:+.2f}€  "
                                f"trailing={trade.trailing_stop:.6f if trade.trailing_stop else 'inactif'}"
                            )

                await asyncio.sleep(self.cfg.loop_interval)

        except KeyboardInterrupt:
            log.info("\n[BOT] Arrêt demandé par l'utilisateur.")
            # Fermer proprement les trades ouverts
            for market in list(self.open_trades.keys()):
                price = await self.kraken.fetch_ticker(market)
                if price:
                    self._close_trade(market, price, "SHUTDOWN")
            self.perf.print_dashboard(
                capital     = self.risk.capital,
                daily_pnl   = self.risk.daily_pnl,
                open_trades = 0,
            )
        finally:
            await self.kraken.close()
            log.info("[BOT] Connexions fermées. À bientôt. 👋")


# ─────────────────────────────────────────────────────────────────────────────
#  POINT D'ENTRÉE
# ─────────────────────────────────────────────────────────────────────────────
cfg = BotConfig()

if __name__ == "__main__":
    # ── Lecture des variables d'environnement (Railway / .env)
    cfg.simulation_mode  = os.environ.get("SIMULATION_MODE", "true").lower() == "true"
    cfg.initial_capital  = float(os.environ.get("INITIAL_CAPITAL", "200"))
    cfg.stake_eur        = float(os.environ.get("STAKE_EUR", "50"))
    cfg.leverage         = int(os.environ.get("LEVERAGE", "3"))
    cfg.daily_kill_eur   = float(os.environ.get("DAILY_KILL_EUR", "-3"))
    cfg.score_min        = int(os.environ.get("SCORE_MIN", "14"))
    cfg.max_open_trades  = int(os.environ.get("MAX_OPEN_TRADES", "3"))

    bot = QuantumEdgeBot(cfg)
    asyncio.run(bot.run())
