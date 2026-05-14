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
║                  QUANTUM EDGE TRADING SYSTEM — v3.1                         ║
║  Backtest · Optimizer · Walk-Forward · PostgreSQL · Dashboard Web           ║
║                                                                              ║
║  Marchés  : 15 paires crypto — classement dynamique ADX/Volume              ║
║  Signaux  : ADX · EMA · RSI · Bollinger · Volume · Macro · Divergence       ║
║  Risque   : Trailing · Kill Switch · Partial TP · Smart Defense · Vol Adj   ║
║  Filtres  : Pearson ρ · Corrélation Groupe · Bougie Exp. · Score Adaptatif  ║
║  Validation: Backtester · Grid Search · Walk-Forward · SQLite DataStore     ║
║  Monitoring: Dashboard HTML · Telegram · Persistance JSON                   ║
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
import hashlib
import itertools
import json
import logging
import math
import os
import random
import sqlite3
import statistics
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
try:
    import psycopg
    from psycopg.rows import dict_row
    PSYCOPG_AVAILABLE = True
except ImportError:
    PSYCOPG_AVAILABLE = False
    log_tmp = logging.getLogger("QUANTUM_EDGE")
    log_tmp.warning("[DB] psycopg non installé — fallback SQLite activé")

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

    # ── Marchés (13 paires confirmées disponibles sur Kraken USDT)
    markets: List[str] = field(default_factory=lambda: [
        "ETH/USDT",   # Ethereum      — DeFi leader, très suivi
        "SOL/USDT",   # Solana        — vitesse + écosystème fort
        "BNB/USDT",   # Binance Coin  — corrélé aux volumes d'échange
        "XRP/USDT",   # Ripple        — fort momentum institutionnel
        "AVAX/USDT",  # Avalanche     — L1 compétitif, bons mouvements
        "LINK/USDT",  # Chainlink     — oracle leader, tendances nettes
        "ADA/USDT",   # Cardano       — cycles réguliers, technique lisible
        "DOT/USDT",   # Polkadot      — interopérabilité, swings propres
        "DOGE/USDT",  # Dogecoin      — forte volatilité, volumes élevés
        "ATOM/USDT",  # Cosmos        — IBC leader, tendances franches
        "LTC/USDT",   # Litecoin      — haute liquidité Kraken, cycles nets
        "ALGO/USDT",  # Algorand      — disponible Kraken ✅
        "XTZ/USDT",   # Tezos         — disponible Kraken ✅
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
    target_pct:     float = 0.60    # +0.60% sur position levierisée = ~+0.90€
    stoploss_pct:   float = 1.50    # -1.50% sur position = ~-2.25€
    trailing_start: float = 0.45    # déclenche le trailing à +0.45%
    trailing_step:  float = 0.20    # step du trailing stop

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

    # ── Protection corrélation
    correlation_filter_enabled: bool = True
    max_correlated_positions:   int   = 2

    # ── Filtre bougie explosive
    max_single_candle_pct: float = 3.5  # ignorer si dernière bougie > 3.5%

    # ── Take-profit partiel
    partial_tp_enabled:  bool  = True
    partial_tp_trigger:  float = 0.35   # déclenche à +0.35% de gain levierisé
    partial_tp_size:     float = 0.50   # sécurise 50% du PnL latent

    # ── Cooldown après série de pertes
    max_consecutive_losses:  int = 4
    cooldown_after_losses:   int = 1800  # 30 minutes

    # ── Smart Defense (réduction de mise après gain significatif)
    smart_defense_enabled:         bool  = True
    smart_defense_trigger:         float = 8.0   # déclenche si capital +8%
    smart_defense_stake_reduction: float = 0.50  # réduit la mise de 50%

    # ── Corrélation manager (Pearson 1h)
    correlation_threshold: float = 0.75   # seuil |ρ| max entre positions
    max_correlated_trades: int   = 2      # max trades dans un même cluster

    # ── Ajustement dynamique de mise selon volatilité
    volatility_adjust: bool = True

    # ── Persistance d'état
    enable_persistence: bool = True
    state_file:         str  = "quantum_edge_state.json"
    database_url:       str  = ""   # PostgreSQL Railway (DATABASE_URL)

    # ── Notifications Telegram (désactivé par défaut)
    use_telegram:       bool = False
    telegram_token:     str  = ""
    telegram_chat_id:   str  = ""


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
    trailing_stop:  Optional[float] = None
    peak_price:     Optional[float] = None
    score:          int = 0
    regime:         MarketRegime = MarketRegime.RANGING
    partial_taken:  bool = False
    atr_value:      float = 0.0   # ATR en % au moment de l'ouverture (pour trailing progressif)

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

    @staticmethod
    def candle_explosion(candle: Candle) -> float:
        """Retourne le % de mouvement corps de la dernière bougie."""
        if candle.open == 0:
            return 0.0
        return abs(candle.close - candle.open) / candle.open * 100


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

        # ─── BONUS FORCE MACRO (EMA20/EMA50 1h) ─────────────────────────
        # Si l'écart EMA20–EMA50 1h est significatif (≥ 1%), tendance forte
        macro_trend_strength = 0
        if len(ema_fast_1h) > 0 and len(ema_slow_1h) > 0 and ema_slow_1h[-1] != 0:
            diff = abs(ema_fast_1h[-1] - ema_slow_1h[-1]) / ema_slow_1h[-1] * 100
            if diff >= 1.0:
                macro_trend_strength = 2

        buy_score  += macro_trend_strength
        sell_score += macro_trend_strength

        # ─── FILTRE ANTI-FAKEOUT ─────────────────────────────────────────
        # Si le marché est en range et l'ADX est faible → diviser le score par 2
        if regime == MarketRegime.RANGING and adx_val < 18:
            buy_score  = buy_score // 2
            sell_score = sell_score // 2

        # Si volatilité extrême → pénaliser
        if regime == MarketRegime.VOLATILE:
            buy_score  = int(buy_score * 0.7)
            sell_score = int(sell_score * 0.7)

        # ─── SCORE MINIMUM ADAPTATIF (selon régime) ──────────────────────
        adaptive_score_min = self.cfg.score_min
        if regime == MarketRegime.RANGING:
            adaptive_score_min += 3    # plus strict en range
        elif regime == MarketRegime.VOLATILE:
            adaptive_score_min += 5    # très strict en volatilité extrême
        elif regime in (MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN):
            adaptive_score_min -= 1    # légèrement assoupli en tendance franche

        # ─── DÉCISION ────────────────────────────────────────────────────
        details = {}
        if buy_score >= adaptive_score_min and buy_score > sell_score:
            details = buy_details
            details["total"] = buy_score
            details["rsi_val"] = round(rsi_val, 1)
            details["adx_val"] = round(adx_val, 1)
            details["vol_ratio"] = round(vol_ratio, 2)
            details["regime"] = regime.value
            details["adaptive_score_min"] = adaptive_score_min
            return Signal.BUY, buy_score, details, regime

        if sell_score >= adaptive_score_min and sell_score > buy_score:
            details = sell_details
            details["total"] = sell_score
            details["rsi_val"] = round(rsi_val, 1)
            details["adx_val"] = round(adx_val, 1)
            details["vol_ratio"] = round(vol_ratio, 2)
            details["regime"] = regime.value
            details["adaptive_score_min"] = adaptive_score_min
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

    def adjust_for_volatility(self, base_stake: float, atr_pct: float) -> float:
        """Ajuste dynamiquement la mise selon la volatilité ATR récente.

        Formule : factor = clamp(0.04 / atr_pct, 0.65, 1.0)
        → ATR élevé  → factor < 1 → mise réduite (marché trop volatile)
        → ATR normal → factor ≈ 1 → mise inchangée
        """
        if not self.cfg.volatility_adjust or atr_pct <= 0:
            return base_stake
        factor = max(0.65, min(1.0, 0.04 / atr_pct))
        adjusted = round(base_stake * factor, 2)
        if factor < 1.0:
            log.debug(
                f"[VOL ADJ] ATR={atr_pct:.4f}%  factor={factor:.2f}  "
                f"mise {base_stake:.2f}€ → {adjusted:.2f}€"
            )
        return adjusted

    def update_trailing_stop(self, trade: Trade, current_price: float) -> Optional[float]:
        """
        Trailing stop PROGRESSIF basé sur l'ATR.

        Le step s'adapte à la volatilité réelle du marché :
        - Marché volatile (BTC, ETH) → step large → le trade respire
        - Marché calme (MANA, XTZ)   → step serré → gains mieux protégés

        Formule : step = max(0.10%, min(0.50%, ATR% × 0.5))
        """
        # Calculer le step dynamique basé sur l'ATR du trade
        if trade.atr_value > 0:
            atr_step = max(0.10, min(0.50, trade.atr_value * 0.5))
        else:
            atr_step = self.cfg.trailing_step  # fallback au step fixe

        if trade.trailing_stop is None:
            # Initialiser si on a atteint le seuil de déclenchement
            if trade.side == Signal.BUY:
                pct_gain = (current_price - trade.entry_price) / trade.entry_price * 100 * trade.leverage
                if pct_gain >= self.cfg.trailing_start:
                    stop = current_price * (1 - atr_step / 100 / trade.leverage)
                    trade.trailing_stop = stop
                    trade.peak_price = current_price
                    log.info(
                        f"[TRAILING] {trade.market} activé @ {stop:.6f} "
                        f"(step ATR={atr_step:.2f}%)"
                    )
                    return stop
            else:
                pct_gain = (trade.entry_price - current_price) / trade.entry_price * 100 * trade.leverage
                if pct_gain >= self.cfg.trailing_start:
                    stop = current_price * (1 + atr_step / 100 / trade.leverage)
                    trade.trailing_stop = stop
                    trade.peak_price = current_price
                    log.info(
                        f"[TRAILING] {trade.market} activé @ {stop:.6f} "
                        f"(step ATR={atr_step:.2f}%)"
                    )
                    return stop
        else:
            # Mise à jour du pic et déplacement progressif du stop
            if trade.side == Signal.BUY and current_price > (trade.peak_price or 0):
                trade.peak_price = current_price
                new_stop = current_price * (1 - atr_step / 100 / trade.leverage)
                if new_stop > trade.trailing_stop:
                    trade.trailing_stop = new_stop
                    return new_stop
            elif trade.side == Signal.SELL and current_price < (trade.peak_price or float("inf")):
                trade.peak_price = current_price
                new_stop = current_price * (1 + atr_step / 100 / trade.leverage)
                if new_stop < trade.trailing_stop:
                    trade.trailing_stop = new_stop
                    return new_stop
        return trade.trailing_stop

    def should_partial_take(self, trade: Trade, current_price: float) -> bool:
        """Vérifie si le take-profit partiel doit être déclenché."""
        if trade.partial_taken:
            return False
        if trade.side == Signal.BUY:
            pct = (current_price - trade.entry_price) / trade.entry_price * 100 * trade.leverage
        else:
            pct = (trade.entry_price - current_price) / trade.entry_price * 100 * trade.leverage
        return pct >= self.cfg.partial_tp_trigger

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
#  CORRELATION MANAGER — corrélation Pearson 1h, rafraîchie toutes les 30 min
# ─────────────────────────────────────────────────────────────────────────────
class CorrelationManager:
    """
    Évite la sur-exposition sur des paires trop corrélées.

    Calcule les coefficients de Pearson sur les clôtures horaires (150 bougies)
    et met en cache les résultats pendant 30 minutes pour limiter les appels API.
    Avant chaque ouverture de trade, vérifie que le marché candidat n'est pas
    trop corrélé (|ρ| ≥ cfg.correlation_threshold) avec les positions déjà ouvertes.
    """

    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.correlation_cache: Dict[str, float] = {}  # clé md5 → ρ
        self.last_update: float = 0.0

    @staticmethod
    def _pair_key(m1: str, m2: str) -> str:
        """Clé de cache déterministe (ordre indépendant)."""
        pair = ":".join(sorted([m1, m2]))
        return hashlib.md5(pair.encode()).hexdigest()

    async def update_correlations(self, kraken: "KrakenClient", markets: List[str]):
        """Met à jour le cache de corrélations toutes les 30 minutes."""
        if time.time() - self.last_update < 1800:
            return

        log.info("[CORR MGR] Mise à jour des corrélations 1h entre marchés...")
        closes: Dict[str, List[float]] = {}
        for market in markets:
            candles = await kraken.fetch_ohlcv(market, 60, 150)
            if len(candles) >= 100:
                closes[market] = [c.close for c in candles[-120:]]

        count = 0
        for i, m1 in enumerate(markets):
            for m2 in markets[i + 1:]:
                if m1 not in closes or m2 not in closes:
                    continue
                try:
                    corr = statistics.correlation(closes[m1], closes[m2])
                    self.correlation_cache[self._pair_key(m1, m2)] = corr
                    count += 1
                except Exception:
                    continue

        self.last_update = time.time()
        log.info(f"[CORR MGR] {count} paires analysées et mises en cache.")

    def can_open_trade(self, market: str, open_trades: Dict[str, "Trade"]) -> bool:
        """
        Retourne True si `market` peut être ouvert sans sur-exposer
        le portefeuille sur des actifs trop corrélés.
        """
        correlated_count = 0
        for open_m in open_trades.keys():
            key  = self._pair_key(market, open_m)
            corr = self.correlation_cache.get(key, 0.0)
            if abs(corr) >= self.cfg.correlation_threshold:
                correlated_count += 1
        return correlated_count < self.cfg.max_correlated_trades


# ─────────────────────────────────────────────────────────────────────────────
#  TELEGRAM NOTIFIER — notifications élégantes et professionnelles
# ─────────────────────────────────────────────────────────────────────────────
class TelegramNotifier:
    """
    Envoie des notifications Telegram HTML-formatées sur les événements clés :
    ouverture/fermeture de trade, kill switch, cooldown, dashboard périodique.

    Activé uniquement si cfg.use_telegram=True et que token + chat_id sont fournis.
    Désactivé silencieusement en cas d'erreur réseau pour ne jamais bloquer le bot.
    """

    def __init__(self, cfg: BotConfig):
        self.cfg     = cfg
        self.enabled = cfg.use_telegram and bool(
            cfg.telegram_token and cfg.telegram_chat_id
        )
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def send(self, message: str, parse_mode: str = "HTML"):
        """Envoie un message Telegram. Fail-silent."""
        if not self.enabled:
            return
        try:
            url     = f"https://api.telegram.org/bot{self.cfg.telegram_token}/sendMessage"
            payload = {
                "chat_id":    self.cfg.telegram_chat_id,
                "text":       message,
                "parse_mode": parse_mode,
            }
            session = await self._get_session()
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5)):
                pass
        except Exception as e:
            log.debug(f"[TELEGRAM] Erreur envoi : {e}")

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# ─────────────────────────────────────────────────────────────────────────────
#  FILTRE DE CORRÉLATION INTER-MARCHÉS
# ─────────────────────────────────────────────────────────────────────────────
class CorrelationFilter:
    """
    Évite la concentration de risque en bloquant les trades sur des marchés
    trop corrélés aux positions déjà ouvertes.

    Calcule la corrélation de Pearson sur une fenêtre glissante de N bougies
    (returns logarithmiques). Si la corrélation absolue dépasse le seuil
    avec un trade déjà ouvert, le signal est rejeté.

    Cela force le bot à diversifier naturellement ses positions.
    """

    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.correlation_window:    int   = 50    # bougies pour le calcul
        self.correlation_threshold: float = 0.75  # seuil |ρ| ≥ 0.75 → bloquer
        # Cache des prix de clôture par marché (fenêtre glissante)
        self._price_history: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=self.correlation_window)
        )

    def update(self, market: str, closes: List[float]):
        """Ajoute les derniers prix à l'historique glissant."""
        if not closes:
            return
        for c in closes[-5:]:  # on ajoute les 5 dernières bougies max
            self._price_history[market].append(c)

    def _log_returns(self, prices: List[float]) -> List[float]:
        """Calcule les returns logarithmiques."""
        if len(prices) < 2:
            return []
        return [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]

    def _pearson(self, x: List[float], y: List[float]) -> float:
        """Coefficient de corrélation de Pearson entre deux séries."""
        n = min(len(x), len(y))
        if n < 10:
            return 0.0
        x, y = x[-n:], y[-n:]
        mean_x = sum(x) / n
        mean_y = sum(y) / n
        cov   = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
        std_x = math.sqrt(sum((xi - mean_x) ** 2 for xi in x))
        std_y = math.sqrt(sum((yi - mean_y) ** 2 for yi in y))
        if std_x == 0 or std_y == 0:
            return 0.0
        return cov / (std_x * std_y)

    def is_safe_to_open(
        self, market: str, open_markets: List[str]
    ) -> Tuple[bool, float]:
        """
        Vérifie si `market` peut être tradé sans concentrer le risque.

        Retourne (autorisé, corrélation_maximale_observée).
        """
        if not open_markets or market in open_markets:
            return True, 0.0

        prices_candidate = list(self._price_history.get(market, []))
        returns_candidate = self._log_returns(prices_candidate)
        if len(returns_candidate) < 10:
            return True, 0.0  # pas assez de données → on autorise

        max_corr = 0.0
        for open_market in open_markets:
            prices_open  = list(self._price_history.get(open_market, []))
            returns_open = self._log_returns(prices_open)
            if len(returns_open) < 10:
                continue
            corr     = self._pearson(returns_candidate, returns_open)
            max_corr = max(max_corr, abs(corr))

        is_safe = max_corr < self.correlation_threshold
        return is_safe, max_corr


# ─────────────────────────────────────────────────────────────────────────────
#  CLIENT KRAKEN (source de prix)
# ─────────────────────────────────────────────────────────────────────────────
class KrakenClient:
    """Récupère les bougies OHLCV via l'API REST Kraken."""

    BASE_URL = "https://api.kraken.com/0/public"

    # Mapping des symboles Kraken → Binance
    SYMBOL_MAP = {
        "ETH/USDT":  "ETHUSDT",
        "SOL/USDT":  "SOLUSDT",
        "BNB/USDT":  "BNBUSDT",
        "XRP/USDT":  "XRPUSDT",
        "AVAX/USDT": "AVAXUSDT",
        "LINK/USDT": "LINKUSDT",
        "ADA/USDT":  "ADAUSDT",
        "DOT/USDT":  "DOTUSDT",
        "DOGE/USDT": "DOGEUSDT",
        "ATOM/USDT": "ATOMUSDT",   # ✅ disponible Kraken
        "LTC/USDT":  "LTCUSDT",    # ✅ Kraken
        "ALGO/USDT": "ALGOUSDT",   # ✅ Kraken
        "XTZ/USDT":  "XTZUSDT",    # ✅ Kraken
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
        self.global_cooldown_until: float = 0.0
        self.corr_filter     = CorrelationFilter(cfg)
        self.correlation_mgr = CorrelationManager(cfg)
        self.notifier        = TelegramNotifier(cfg)
        self.state_file      = Path(cfg.state_file)
        # ── Nouveaux modules v3.0
        self.store      = DataStore()
        self.downloader = HistoricalDownloader(self.kraken, self.store)
        self.dashboard  = DashboardServer(self.perf, self.risk, self.open_trades, port=8080)

        # ── Groupes de corrélation
        self.market_groups: Dict[str, List[str]] = {
            "MAJORS":        ["ETH/USDT", "LTC/USDT"],
            "L1":            ["SOL/USDT", "AVAX/USDT", "ADA/USDT", "DOT/USDT", "ALGO/USDT"],
            "PAYMENTS":      ["XRP/USDT", "DOGE/USDT", "BNB/USDT"],
            "DEFI":          ["LINK/USDT", "ATOM/USDT", "XTZ/USDT"],
        }

    # ── Propriétés de commodité
    @property
    def _win_rate_recent(self) -> float:
        if not self._win_history:
            return 0.55  # prior conservateur
        return sum(self._win_history) / len(self._win_history)

    # ── Filtre de corrélation
    def _count_correlated_positions(self, market: str) -> int:
        """Compte les positions ouvertes dans le même groupe de corrélation."""
        for _group_name, group in self.market_groups.items():
            if market in group:
                return sum(1 for m in self.open_trades if m in group)
        return 0

    # ── Classement dynamique des marchés
    async def _rank_markets(self):
        """Re-trie self.cfg.markets par score ADX + volume pour prioriser les plus actifs."""
        ranked = []
        for market in self.cfg.markets:
            candles = await self.kraken.fetch_ohlcv(market, self.cfg.tf_primary, 50)
            if not candles:
                continue
            adx_val, _, _ = self.engine.ind.adx(candles)
            vol_ratio      = self.engine.ind.volume_ratio(candles)
            score          = adx_val + (vol_ratio * 10)
            ranked.append((market, score))
        ranked.sort(key=lambda x: x[1], reverse=True)
        if ranked:
            self.cfg.markets = [m for m, _ in ranked]
            log.info(
                "[MARKETS] Classement mis à jour : "
                + " > ".join(m.split("/")[0] for m, _ in ranked[:5])
                + " ..."
            )

    # ── Persistance d'état (PostgreSQL prioritaire, JSON fallback)
    def _save_state(self):
        """Sauvegarde l'état du bot — PostgreSQL si dispo, JSON sinon."""
        if not self.cfg.enable_persistence:
            return
        # ── PostgreSQL
        if self.store.use_postgres:
            self.store.save_bot_state(self.risk, self.perf)
            return
        # ── Fallback JSON
        try:
            state = {
                "capital":      self.risk.capital,
                "peak_capital": self.risk.peak_capital,
                "daily_pnl":    self.risk.daily_pnl,
                "trades_today": self.risk.trades_today,
                "saved_at":     datetime.now(timezone.utc).isoformat(),
            }
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            log.debug(f"[STATE] Sauvegardé → {self.state_file}")
        except Exception as e:
            log.error(f"[STATE] Erreur sauvegarde JSON : {e}")

    def _load_state(self):
        """Restaure l'état du bot — PostgreSQL prioritaire, JSON fallback."""
        if not self.cfg.enable_persistence:
            return
        # ── PostgreSQL
        if self.store.use_postgres:
            state = self.store.load_bot_state()
            if state:
                self.risk.capital      = float(state.get("capital",      self.risk.capital))
                self.risk.peak_capital = float(state.get("peak_capital", self.risk.peak_capital))
                self.risk.daily_pnl    = float(state.get("daily_pnl",    0.0))
                self.risk.trades_today = int(state.get("trades_today",   0))
                log.info(
                    f"[STATE] État restauré (PostgreSQL) — "
                    f"Capital: {self.risk.capital:.2f}€  "
                    f"| Trades today: {self.risk.trades_today}"
                )
            return
        # ── Fallback JSON
        if not self.state_file.exists():
            return
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            self.risk.capital      = state.get("capital",      self.risk.capital)
            self.risk.peak_capital = state.get("peak_capital", self.risk.peak_capital)
            self.risk.daily_pnl    = state.get("daily_pnl",    0.0)
            self.risk.trades_today = state.get("trades_today", 0)
            log.info(
                f"[STATE] État restauré (JSON) — Capital: {self.risk.capital:.2f}€  "
                f"| sauvegardé le {state.get('saved_at', '?')}"
            )
        except Exception as e:
            log.warning(f"[STATE] Impossible de restaurer : {e}")

    # ── Notifications Telegram
    async def _send_notification(self, title: str, message: str):
        """Enveloppe de notification avec formatage HTML standard."""
        full_msg = f"<b>⚡ {title}</b>\n{message}"
        await self.notifier.send(full_msg)

    # ── Ouverture d'un trade
    def _open_trade(self, market: str, signal: Signal, price: float, score: int, regime: MarketRegime) -> Trade:
        # Dimensionnement Kelly
        stake = self.risk.kelly_stake(
            win_rate = self._win_rate_recent,
            win_pct  = self.cfg.target_pct / 100,
            loss_pct = self.cfg.stoploss_pct / 100,
        )

        # ── Ajustement volatilité (ATR % sur 14 bougies 15m)
        # On récupère les candles depuis le cache corr_filter si disponible
        prices = list(self.corr_filter._price_history.get(market, []))
        atr_pct = 0.0
        if len(prices) >= 15:
            recent_range = max(prices[-14:]) - min(prices[-14:])
            atr_pct = (recent_range / prices[-1]) * 100 if prices[-1] else 0
            stake = self.risk.adjust_for_volatility(stake, atr_pct)

        # ── Smart Defense : réduire la mise si le capital a bien progressé
        if self.cfg.smart_defense_enabled:
            capital_gain_pct = (
                (self.risk.capital - self.cfg.initial_capital) / self.cfg.initial_capital
            ) * 100
            if capital_gain_pct >= self.cfg.smart_defense_trigger:
                stake *= self.cfg.smart_defense_stake_reduction
                log.info(
                    f"[SMART DEFENSE] Capital +{capital_gain_pct:.1f}% — "
                    f"mise réduite à {stake:.2f}€ pour protéger les gains"
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
            atr_value   = atr_pct,
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
        asyncio.ensure_future(self._send_notification(
            f"📈 TRADE OUVERT — {market}",
            f"Direction : <b>{signal.value}</b>\n"
            f"Prix : {price:.6f}\n"
            f"Mise : {stake:.2f}€ × x{self.cfg.leverage}\n"
            f"Score : {score}/30  |  Régime : {regime.value}\n"
            f"Target : {trade.target_price():.6f}  |  SL : {trade.stoploss_price():.6f}"
        ))
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

        # ── Enregistrer le trade en PostgreSQL
        self.store.save_trade(trade)

        # Notification Telegram
        icon = "✅" if trade.pnl_eur > 0 else "❌"
        asyncio.ensure_future(self._send_notification(
            f"{icon} TRADE FERMÉ — {market}",
            f"Direction : <b>{trade.side.value}</b>  |  Raison : {reason}\n"
            f"Entrée : {trade.entry_price:.6f}  →  Sortie : {trade.exit_price:.6f}\n"
            f"PnL : <b>{trade.pnl_eur:+.2f}€</b>\n"
            f"Capital : {self.risk.capital:.2f}€  |  PnL jour : {self.risk.daily_pnl:+.2f}€"
        ))

        # Sauvegarde persistante après chaque trade
        self._save_state()

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

        # ── Mesure de latence Kraken
        start = time.perf_counter()
        candles_15m = await self.kraken.fetch_ohlcv(market, self.cfg.tf_primary, self.cfg.candles_required)
        candles_1h  = await self.kraken.fetch_ohlcv(market, self.cfg.tf_confirmation, self.cfg.candles_required)
        latency = time.perf_counter() - start
        if latency > 2.0:
            log.warning(f"[LATENCY] {market} Kraken lent : {latency:.2f}s")

        if len(candles_15m) < 50 or len(candles_1h) < 50:
            log.debug(f"[{market}] Données insuffisantes ({len(candles_15m)} bougie(s) 15m)")
            return

        price = candles_15m[-1].close
        closes_15m_list = [c.close for c in candles_15m]

        # ── Persister les nouvelles bougies en SQLite
        self.store.save_candles(market, self.cfg.tf_primary,     candles_15m[-10:])
        self.store.save_candles(market, self.cfg.tf_confirmation, candles_1h[-5:])

        # ── Alimenter le filtre de corrélation (historique glissant)
        self.corr_filter.update(market, closes_15m_list)

        # ── Gestion des trades ouverts
        if market in self.open_trades:
            trade = self.open_trades[market]

            # Take-profit partiel
            if self.cfg.partial_tp_enabled and self.risk.should_partial_take(trade, price):
                secured = trade.unrealized_pnl(price) * self.cfg.partial_tp_size
                trade.partial_taken = True
                self.risk.capital += secured
                log.info(
                    f"[PARTIAL TP] {market:<12} profit partiel sécurisé : {secured:+.2f}€  "
                    f"(50% du PnL latent)"
                )

            should_exit, reason = self.risk.should_exit(trade, price)
            if should_exit:
                self._close_trade(market, price, reason)
            return  # un seul trade par marché

        # ── Slots disponibles ?
        if len(self.open_trades) >= self.cfg.max_open_trades:
            return

        # ── Filtre bougie explosive
        last_candle_move = Indicators.candle_explosion(candles_15m[-1])
        if last_candle_move >= self.cfg.max_single_candle_pct:
            log.info(
                f"[FILTER] {market:<12} ignoré — bougie explosive {last_candle_move:.2f}% "
                f"(seuil : {self.cfg.max_single_candle_pct}%)"
            )
            return

        # ── Filtre corrélation
        if self.cfg.correlation_filter_enabled:
            correlated = self._count_correlated_positions(market)
            if correlated >= self.cfg.max_correlated_positions:
                log.info(
                    f"[FILTER] {market:<12} ignoré — "
                    f"{correlated} positions corrélées déjà ouvertes"
                )
                return

        # ── Calcul du signal
        signal, score, details, regime = self.engine.compute(candles_15m, candles_1h)

        if signal != Signal.NONE:
            # ── Filtre de corrélation Pearson (inter-marchés, returns log)
            open_mkts = list(self.open_trades.keys())
            is_safe, max_corr = self.corr_filter.is_safe_to_open(market, open_mkts)
            if not is_safe:
                log.info(
                    f"[CORR]  {market:<12} signal ignoré — "
                    f"ρ={max_corr:.3f} ≥ {self.corr_filter.correlation_threshold:.2f} "
                    f"avec {open_mkts}"
                )
                return

            log.info(
                f"[SIGNAL] {market:<12} {signal.value:<4} "
                f"score={score}/30  "
                f"seuil={details.get('adaptive_score_min')}  "
                f"RSI={details.get('rsi_val')}  "
                f"ADX={details.get('adx_val')}  "
                f"Vol={details.get('vol_ratio')}x  "
                f"régime={details.get('regime')}  "
                f"ρmax={max_corr:.3f}"
            )
            self._open_trade(market, signal, price, score, regime)

    # ── Boucle principale
    async def run(self):
        mode = "🔴 SIMULATION" if self.cfg.simulation_mode else "🟢 LIVE"
        log.info(f"╔══ QUANTUM EDGE démarré — Mode : {mode} ══╗")
        log.info(f"║  Marchés : {len(self.cfg.markets)}  |  Capital : {self.cfg.initial_capital}€  |  Levier : x{self.cfg.leverage}")
        log.info(f"║  Score min : {self.cfg.score_min}/30  |  Target : +{self.cfg.target_pct}%  |  SL : -{self.cfg.stoploss_pct}%")
        log.info(f"╚{'═'*50}╝\n")

        # ── Restaurer l'état précédent (redémarrage Railway)
        self._load_state()

        # ── Démarrer le dashboard HTML en tâche de fond
        asyncio.ensure_future(self.dashboard.start())

        # ── Téléchargement initial des données historiques (30 jours)
        log.info("[BOT] Téléchargement des données historiques (30 jours)...")
        await self.downloader.download_all(self.cfg.markets, interval_min=15, target_days=30)
        await self.downloader.download_all(self.cfg.markets, interval_min=60, target_days=30)
        log.info("[BOT] Données historiques prêtes.")

        # ── Notification de démarrage
        await self._send_notification(
            "⚡ QUANTUM EDGE démarré",
            f"Mode : {mode}\nCapital : {self.risk.capital:.2f}€\n"
            f"Marchés : {len(self.cfg.markets)}  |  Levier : x{self.cfg.leverage}"
        )

        try:
            while True:
                self._iter += 1
                self.risk.reset_daily()

                # ── Cooldown après série de pertes
                if time.time() < self.global_cooldown_until:
                    remaining = int((self.global_cooldown_until - time.time()) / 60)
                    log.warning(
                        f"[COOLDOWN] Bot en pause stratégique — "
                        f"{remaining} min restantes (série de pertes)"
                    )
                    await asyncio.sleep(60)
                    continue

                # ── Vérifier si nouvelle série de pertes déclenche un cooldown
                if self.perf.loss_streak >= self.cfg.max_consecutive_losses:
                    self.global_cooldown_until = time.time() + self.cfg.cooldown_after_losses
                    log.warning(
                        f"[COOLDOWN] {self.perf.loss_streak} pertes consécutives — "
                        f"pause de {self.cfg.cooldown_after_losses // 60} min activée"
                    )
                    await asyncio.sleep(60)
                    continue

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

                # ── Classement dynamique des marchés (toutes les 100 itérations)
                if self._iter % 100 == 0:
                    await self._rank_markets()

                # ── Mise à jour des corrélations 1h (toutes les ~30 min, auto-throttlée)
                await self.correlation_mgr.update_correlations(self.kraken, self.cfg.markets)

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
                            upnl     = trade.unrealized_pnl(price)
                            trailing = f"{trade.trailing_stop:.6f}" if trade.trailing_stop else "inactif"
                            log.info(
                                f"[OPEN]  {market:<12} {trade.side.value}  "
                                f"prix={price:.6f}  "
                                f"PnL non-réalisé={upnl:+.2f}€  "
                                f"trailing={trailing}"
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
            await self.notifier.close()
            log.info("[BOT] Connexions fermées. À bientôt. 👋")


# ─────────────────────────────────────────────────────────────────────────────
#  MODULE 1 — DATA STORE (PostgreSQL prioritaire, SQLite en fallback)
# ─────────────────────────────────────────────────────────────────────────────
class DataStore:
    """
    Stockage persistant hybride :
    - PostgreSQL (psycopg) si DATABASE_URL est défini → recommandé sur Railway
    - SQLite local en fallback si psycopg absent ou DATABASE_URL vide

    Tables gérées :
      candles          — bougies OHLCV pour le backtester
      bot_state        — état du bot (capital, PnL, stats)
      trade_history    — historique complet des trades
      backtest_results — résultats des runs de backtest / optimisation
    """

    def __init__(self, db_path: str = "quantum_edge_data.db"):
        self.db_path     = db_path
        self.database_url = os.environ.get("DATABASE_URL", "")
        self.use_postgres = PSYCOPG_AVAILABLE and bool(self.database_url)

        if self.use_postgres:
            self._init_postgres()
            log.info("[DATASTORE] Mode PostgreSQL activé (Railway)")
        else:
            self._init_sqlite()
            log.info(f"[DATASTORE] Mode SQLite activé : {self.db_path}")

    # ── Connexions ────────────────────────────────────────────────────────────

    def _pg_conn(self):
        return psycopg.connect(self.database_url, row_factory=dict_row)

    def _sqlite_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    # ── Initialisation PostgreSQL ─────────────────────────────────────────────

    def _init_postgres(self):
        try:
            with self._pg_conn() as conn:
                with conn.cursor() as cur:
                    # Table bot_state
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS bot_state (
                            id               INTEGER PRIMARY KEY DEFAULT 1,
                            capital          NUMERIC(15,2) NOT NULL DEFAULT 200.0,
                            peak_capital     NUMERIC(15,2) NOT NULL DEFAULT 200.0,
                            total_gagne      NUMERIC(15,2) NOT NULL DEFAULT 0,
                            total_perdu      NUMERIC(15,2) NOT NULL DEFAULT 0,
                            cumul_net        NUMERIC(15,2) NOT NULL DEFAULT 0,
                            nb_trades        INTEGER NOT NULL DEFAULT 0,
                            nb_wins          INTEGER NOT NULL DEFAULT 0,
                            nb_losses        INTEGER NOT NULL DEFAULT 0,
                            daily_pnl        NUMERIC(15,2) NOT NULL DEFAULT 0,
                            trades_today     INTEGER NOT NULL DEFAULT 0,
                            pertes_consecutives INTEGER NOT NULL DEFAULT 0,
                            pause_until      BIGINT NOT NULL DEFAULT 0,
                            CONSTRAINT single_row CHECK (id = 1)
                        )
                    """)
                    # Table trade_history
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS trade_history (
                            id            SERIAL PRIMARY KEY,
                            timestamp     TIMESTAMP NOT NULL DEFAULT NOW(),
                            marche        VARCHAR(20),
                            direction     VARCHAR(10),
                            resultat      VARCHAR(10),
                            prix_entree   NUMERIC(20,8),
                            prix_sortie   NUMERIC(20,8),
                            stop_loss     NUMERIC(20,8),
                            objectif      NUMERIC(20,8),
                            mise          NUMERIC(15,2),
                            gain          NUMERIC(15,2),
                            capital_apres NUMERIC(15,2),
                            duree_minutes INTEGER,
                            score         INTEGER,
                            adx           NUMERIC(6,2),
                            rsi           NUMERIC(6,2),
                            regime        VARCHAR(20),
                            raison        VARCHAR(20)
                        )
                    """)
                    # Table candles
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS candles (
                            market       VARCHAR(20)  NOT NULL,
                            interval_min INTEGER      NOT NULL,
                            timestamp    BIGINT       NOT NULL,
                            open         NUMERIC(20,8) NOT NULL,
                            high         NUMERIC(20,8) NOT NULL,
                            low          NUMERIC(20,8) NOT NULL,
                            close        NUMERIC(20,8) NOT NULL,
                            volume       NUMERIC(20,8) NOT NULL,
                            PRIMARY KEY (market, interval_min, timestamp)
                        )
                    """)
                    # Table backtest_results
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS backtest_results (
                            id            SERIAL PRIMARY KEY,
                            run_at        TIMESTAMP DEFAULT NOW(),
                            params        TEXT,
                            win_rate      NUMERIC(6,2),
                            profit_factor NUMERIC(8,4),
                            total_pnl     NUMERIC(15,2),
                            max_drawdown  NUMERIC(6,2),
                            total_trades  INTEGER,
                            sharpe        NUMERIC(8,4)
                        )
                    """)
                    # Insérer la ligne d'état si absente
                    cur.execute("""
                        INSERT INTO bot_state (id)
                        VALUES (1) ON CONFLICT DO NOTHING
                    """)
                    conn.commit()
        except Exception as e:
            log.error(f"[DATASTORE] Erreur init PostgreSQL : {e} — bascule SQLite")
            self.use_postgres = False
            self._init_sqlite()

    # ── Initialisation SQLite ─────────────────────────────────────────────────

    def _init_sqlite(self):
        with self._sqlite_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS candles (
                    market       TEXT    NOT NULL,
                    interval_min INTEGER NOT NULL,
                    timestamp    INTEGER NOT NULL,
                    open         REAL    NOT NULL,
                    high         REAL    NOT NULL,
                    low          REAL    NOT NULL,
                    close        REAL    NOT NULL,
                    volume       REAL    NOT NULL,
                    PRIMARY KEY (market, interval_min, timestamp)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_candles_market_ts
                ON candles(market, interval_min, timestamp)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS backtest_results (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_at        TEXT,
                    params        TEXT,
                    win_rate      REAL,
                    profit_factor REAL,
                    total_pnl     REAL,
                    max_drawdown  REAL,
                    total_trades  INTEGER,
                    sharpe        REAL
                )
            """)

    # ── État du bot ───────────────────────────────────────────────────────────

    def save_bot_state(self, risk: "RiskManager", perf: "PerformanceTracker"):
        """Sauvegarde l'état complet du bot en PostgreSQL."""
        if not self.use_postgres:
            return
        try:
            with self._pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE bot_state SET
                            capital             = %s,
                            peak_capital        = %s,
                            total_gagne         = %s,
                            total_perdu         = %s,
                            cumul_net           = %s,
                            nb_trades           = %s,
                            nb_wins             = %s,
                            nb_losses           = %s,
                            daily_pnl           = %s,
                            trades_today        = %s,
                            pertes_consecutives = %s
                        WHERE id = 1
                    """, (
                        risk.capital,
                        risk.peak_capital,
                        perf.avg_win  * perf.winning_trades,
                        abs(perf.avg_loss) * (perf.total_trades - perf.winning_trades),
                        perf.total_pnl,
                        perf.total_trades,
                        perf.winning_trades,
                        perf.total_trades - perf.winning_trades,
                        risk.daily_pnl,
                        risk.trades_today,
                        perf.loss_streak,
                    ))
                    conn.commit()
        except Exception as e:
            log.error(f"[DATASTORE] Erreur save_bot_state : {e}")

    def load_bot_state(self) -> Optional[Dict]:
        """Charge l'état du bot depuis PostgreSQL."""
        if not self.use_postgres:
            return None
        try:
            with self._pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT * FROM bot_state WHERE id = 1")
                    row = cur.fetchone()
                    if not row:
                        return None
                    state = dict(row)
                    for k in ["capital", "peak_capital", "total_gagne",
                              "total_perdu", "cumul_net", "daily_pnl"]:
                        if state.get(k) is not None:
                            state[k] = float(state[k])
                    return state
        except Exception as e:
            log.error(f"[DATASTORE] Erreur load_bot_state : {e}")
            return None

    def save_trade(self, trade: "Trade"):
        """Enregistre un trade fermé dans trade_history (PostgreSQL)."""
        if not self.use_postgres:
            return
        try:
            duration = int((trade.exit_time - trade.entry_time).total_seconds() / 60) if trade.exit_time else 0
            with self._pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO trade_history (
                            marche, direction, resultat,
                            prix_entree, prix_sortie,
                            stop_loss, objectif,
                            mise, gain, capital_apres,
                            duree_minutes, score, regime
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """, (
                        trade.market,
                        trade.side.value,
                        "WIN" if trade.pnl_eur > 0 else "LOSS",
                        trade.entry_price,
                        trade.exit_price,
                        trade.stoploss_price(),
                        trade.target_price(),
                        trade.stake,
                        trade.pnl_eur,
                        None,   # capital_apres mis à jour via save_bot_state
                        duration,
                        trade.score,
                        trade.regime.value,
                    ))
                    conn.commit()
        except Exception as e:
            log.error(f"[DATASTORE] Erreur save_trade : {e}")

    def load_recent_trades(self, limit: int = 10) -> List[Dict]:
        """Charge les N derniers trades depuis PostgreSQL."""
        if not self.use_postgres:
            return []
        try:
            with self._pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT marche, direction, resultat, prix_entree,
                               prix_sortie, gain, capital_apres, timestamp, score
                        FROM trade_history
                        ORDER BY timestamp DESC LIMIT %s
                    """, (limit,))
                    rows = cur.fetchall()
                    return [dict(r) for r in rows]
        except Exception as e:
            log.error(f"[DATASTORE] Erreur load_recent_trades : {e}")
            return []

    # ── Bougies OHLCV ─────────────────────────────────────────────────────────

    def save_candles(self, market: str, interval_min: int, candles: List["Candle"]):
        """Insère ou met à jour les bougies."""
        if not candles:
            return
        if self.use_postgres:
            try:
                rows = [
                    (market, interval_min, c.timestamp,
                     c.open, c.high, c.low, c.close, c.volume)
                    for c in candles
                ]
                with self._pg_conn() as conn:
                    with conn.cursor() as cur:
                        cur.executemany("""
                            INSERT INTO candles
                                (market, interval_min, timestamp, open, high, low, close, volume)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (market, interval_min, timestamp) DO NOTHING
                        """, rows)
                        conn.commit()
            except Exception as e:
                log.debug(f"[DATASTORE] save_candles PG : {e}")
        else:
            rows = [
                (market, interval_min, c.timestamp,
                 c.open, c.high, c.low, c.close, c.volume)
                for c in candles
            ]
            with self._sqlite_conn() as conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?)", rows
                )

    def load_candles(
        self, market: str, interval_min: int, limit: int = 1000, after_ts: int = 0
    ) -> List["Candle"]:
        """Charge les bougies triées ASC."""
        if self.use_postgres:
            try:
                with self._pg_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT timestamp, open, high, low, close, volume
                            FROM candles
                            WHERE market=%s AND interval_min=%s AND timestamp>%s
                            ORDER BY timestamp ASC LIMIT %s
                        """, (market, interval_min, after_ts, limit))
                        rows = cur.fetchall()
                return [Candle(
                    int(r["timestamp"]), float(r["open"]), float(r["high"]),
                    float(r["low"]), float(r["close"]), float(r["volume"])
                ) for r in rows]
            except Exception as e:
                log.debug(f"[DATASTORE] load_candles PG : {e}")
                return []
        else:
            with self._sqlite_conn() as conn:
                rows = conn.execute(
                    """SELECT timestamp, open, high, low, close, volume
                       FROM candles
                       WHERE market=? AND interval_min=? AND timestamp>?
                       ORDER BY timestamp ASC LIMIT ?""",
                    (market, interval_min, after_ts, limit),
                ).fetchall()
            return [Candle(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows]

    def candle_count(self, market: str, interval_min: int) -> int:
        if self.use_postgres:
            try:
                with self._pg_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT COUNT(*) AS n FROM candles WHERE market=%s AND interval_min=%s",
                            (market, interval_min)
                        )
                        row = cur.fetchone()
                        return int(row["n"]) if row else 0
            except Exception:
                return 0
        else:
            with self._sqlite_conn() as conn:
                return conn.execute(
                    "SELECT COUNT(*) FROM candles WHERE market=? AND interval_min=?",
                    (market, interval_min),
                ).fetchone()[0]

    # ── Backtest ──────────────────────────────────────────────────────────────

    def save_backtest_result(self, params: Dict, metrics: Dict):
        if self.use_postgres:
            try:
                with self._pg_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO backtest_results
                                (params, win_rate, profit_factor, total_pnl,
                                 max_drawdown, total_trades, sharpe)
                            VALUES (%s,%s,%s,%s,%s,%s,%s)
                        """, (
                            json.dumps(params),
                            metrics.get("win_rate", 0),
                            metrics.get("profit_factor", 0),
                            metrics.get("total_pnl", 0),
                            metrics.get("max_drawdown", 0),
                            metrics.get("total_trades", 0),
                            metrics.get("sharpe", 0),
                        ))
                        conn.commit()
            except Exception as e:
                log.debug(f"[DATASTORE] save_backtest_result PG : {e}")
        else:
            with self._sqlite_conn() as conn:
                conn.execute(
                    """INSERT INTO backtest_results
                       (run_at, params, win_rate, profit_factor, total_pnl,
                        max_drawdown, total_trades, sharpe)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        datetime.now(timezone.utc).isoformat(),
                        json.dumps(params),
                        metrics.get("win_rate", 0),
                        metrics.get("profit_factor", 0),
                        metrics.get("total_pnl", 0),
                        metrics.get("max_drawdown", 0),
                        metrics.get("total_trades", 0),
                        metrics.get("sharpe", 0),
                    ),
                )

    def best_backtest_results(self, top_n: int = 5) -> List[Dict]:
        if self.use_postgres:
            try:
                with self._pg_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT run_at, params, win_rate, profit_factor,
                                   total_pnl, max_drawdown, total_trades, sharpe
                            FROM backtest_results
                            ORDER BY profit_factor DESC LIMIT %s
                        """, (top_n,))
                        return [dict(r) for r in cur.fetchall()]
            except Exception:
                return []
        else:
            with self._sqlite_conn() as conn:
                rows = conn.execute(
                    """SELECT run_at, params, win_rate, profit_factor, total_pnl,
                              max_drawdown, total_trades, sharpe
                       FROM backtest_results
                       ORDER BY profit_factor DESC LIMIT ?""",
                    (top_n,),
                ).fetchall()
            return [
                {
                    "run_at": r[0], "params": json.loads(r[1]),
                    "win_rate": r[2], "profit_factor": r[3],
                    "total_pnl": r[4], "max_drawdown": r[5],
                    "total_trades": r[6], "sharpe": r[7],
                }
                for r in rows
            ]


# ─────────────────────────────────────────────────────────────────────────────
#  MODULE 2 — BACKTESTER
# ─────────────────────────────────────────────────────────────────────────────
class BacktestResult:
    """Résultats d'un run de backtest."""
    def __init__(self):
        self.trades:        List[Dict]  = []
        self.capital_curve: List[float] = []

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def winning_trades(self) -> int:
        return sum(1 for t in self.trades if t["pnl"] > 0)

    @property
    def win_rate(self) -> float:
        return self.winning_trades / self.total_trades * 100 if self.total_trades else 0.0

    @property
    def total_pnl(self) -> float:
        return sum(t["pnl"] for t in self.trades)

    @property
    def profit_factor(self) -> float:
        gross_win  = sum(t["pnl"] for t in self.trades if t["pnl"] > 0)
        gross_loss = abs(sum(t["pnl"] for t in self.trades if t["pnl"] < 0))
        return gross_win / gross_loss if gross_loss else float("inf")

    @property
    def max_drawdown(self) -> float:
        """Max drawdown en % sur la courbe du capital."""
        if not self.capital_curve:
            return 0.0
        peak = self.capital_curve[0]
        max_dd = 0.0
        for v in self.capital_curve:
            peak = max(peak, v)
            dd   = (peak - v) / peak * 100 if peak else 0
            max_dd = max(max_dd, dd)
        return max_dd

    @property
    def sharpe(self) -> float:
        pnls = [t["pnl"] for t in self.trades]
        if len(pnls) < 10:
            return 0.0
        mean = sum(pnls) / len(pnls)
        std  = statistics.stdev(pnls) if len(pnls) > 1 else 1
        return (mean / std) * math.sqrt(252) if std else 0.0

    def summary(self) -> Dict:
        return {
            "total_trades":   self.total_trades,
            "win_rate":       round(self.win_rate, 1),
            "total_pnl":      round(self.total_pnl, 2),
            "profit_factor":  round(self.profit_factor, 3),
            "max_drawdown":   round(self.max_drawdown, 2),
            "sharpe":         round(self.sharpe, 3),
        }

    def print_report(self, label: str = "BACKTEST"):
        s = self.summary()
        bar = "─" * 55
        print(f"\n{bar}")
        print(f"  📊 {label}")
        print(bar)
        print(f"  Trades        : {s['total_trades']}")
        print(f"  Win rate      : {s['win_rate']:.1f}%")
        print(f"  PnL total     : {s['total_pnl']:+.2f}€")
        print(f"  Profit Factor : {s['profit_factor']:.3f}")
        print(f"  Max Drawdown  : {s['max_drawdown']:.2f}%")
        print(f"  Sharpe        : {s['sharpe']:.3f}")
        print(bar)


class Backtester:
    """
    Rejoue la stratégie SignalEngine sur des données historiques stockées en SQLite.

    Méthodologie :
    - Fenêtre glissante sur les bougies (warmup = 200 bougies)
    - Simulation fidèle : un seul trade par marché, max_open_trades respecté
    - Frais pris en compte (fee_pct × 2)
    - Résultats sauvegardés en base pour comparaison future

    Usage :
      bt = Backtester(cfg, store)
      result = bt.run(["XBT/USDT", "ETH/USDT"], warmup=200)
      result.print_report()
    """

    def __init__(self, cfg: BotConfig, store: DataStore):
        self.cfg    = cfg
        self.store  = store
        self.engine = SignalEngine(cfg)

    def run(
        self,
        markets:      List[str],
        interval_min: int = 15,
        warmup:       int = 200,
        capital:      float = 200.0,
    ) -> BacktestResult:
        result  = BacktestResult()
        capital = capital
        open_pos: Dict[str, Dict] = {}  # market → {entry, side, stake}

        # Charger toutes les bougies disponibles pour les marchés
        all_candles: Dict[str, List[Candle]] = {}
        for market in markets:
            c15  = self.store.load_candles(market, interval_min,  limit=5000)
            c1h  = self.store.load_candles(market, 60,            limit=2000)
            if len(c15) < warmup + 50:
                log.warning(f"[BACKTEST] {market} — données insuffisantes ({len(c15)} bougies), ignoré")
                continue
            all_candles[market] = {"15m": c15, "1h": c1h}

        if not all_candles:
            log.error("[BACKTEST] Aucune donnée disponible. Lance d'abord le téléchargement.")
            return result

        # Trouver la longueur min commune
        min_len = min(len(v["15m"]) for v in all_candles.values())
        log.info(f"[BACKTEST] {len(all_candles)} marchés · {min_len} bougies · capital={capital:.2f}€")

        result.capital_curve.append(capital)

        for i in range(warmup, min_len):
            # ── Gérer les positions ouvertes
            for market in list(open_pos.keys()):
                pos   = open_pos[market]
                price = all_candles[market]["15m"][i].close
                pnl   = 0.0

                if pos["side"] == Signal.BUY:
                    pct = (price - pos["entry"]) / pos["entry"]
                else:
                    pct = (pos["entry"] - price) / pos["entry"]

                gross = pos["stake"] * self.cfg.leverage * pct
                fees  = pos["stake"] * self.cfg.leverage * (self.cfg.fee_pct / 100) * 2
                pnl   = gross - fees

                # Target ou Stop Loss
                target_hit = pnl >= (pos["stake"] * self.cfg.leverage * self.cfg.target_pct / 100)
                sl_hit     = pnl <= -(pos["stake"] * self.cfg.leverage * self.cfg.stoploss_pct / 100)

                if target_hit or sl_hit:
                    capital += pnl
                    reason   = "TARGET" if target_hit else "STOPLOSS"
                    result.trades.append({
                        "market": market, "side": pos["side"].value,
                        "entry": pos["entry"], "exit": price,
                        "pnl": round(pnl, 4), "reason": reason,
                        "candle_idx": i,
                    })
                    result.capital_curve.append(capital)
                    del open_pos[market]

            # ── Chercher de nouveaux signaux
            if len(open_pos) < self.cfg.max_open_trades:
                for market, data in all_candles.items():
                    if market in open_pos:
                        continue
                    if len(open_pos) >= self.cfg.max_open_trades:
                        break

                    candles_15m = data["15m"][max(0, i - 250): i + 1]
                    candles_1h  = data["1h"][:i + 1][-250:]

                    if len(candles_15m) < 50 or len(candles_1h) < 50:
                        continue

                    signal, score, _, _ = self.engine.compute(candles_15m, candles_1h)
                    if signal != Signal.NONE:
                        stake = min(capital * 0.25, self.cfg.stake_eur)
                        if stake < 5:
                            continue
                        open_pos[market] = {
                            "entry": data["15m"][i].close,
                            "side":  signal,
                            "stake": stake,
                            "open_at": i,
                        }

        log.info(f"[BACKTEST] Terminé — {result.total_trades} trades simulés")
        return result


# ─────────────────────────────────────────────────────────────────────────────
#  MODULE 3 — OPTIMISEUR DE PARAMÈTRES (Grid Search)
# ─────────────────────────────────────────────────────────────────────────────
class ParameterOptimizer:
    """
    Cherche la meilleure combinaison de paramètres par grid search exhaustif.

    Paramètres explorés :
      score_min, adx_min, rsi_oversold, rsi_overbought, volume_mult_min

    Métrique d'optimisation : profit_factor (résistant au surapprentissage)
    Les résultats sont sauvegardés en SQLite pour consultation future.

    Usage :
      opt = ParameterOptimizer(cfg, store)
      best_params, best_metrics = opt.run(markets)
      print(best_params)
    """

    # Grille de recherche
    PARAM_GRID = {
        "score_min":       [12, 14, 16, 18],
        "adx_min":         [18, 22, 26],
        "rsi_oversold":    [30, 35, 40],
        "rsi_overbought":  [60, 65, 70],
        "volume_mult_min": [1.1, 1.3, 1.5],
    }

    def __init__(self, cfg: BotConfig, store: DataStore):
        self.cfg   = cfg
        self.store = store

    def _combinations(self) -> List[Dict]:
        keys   = list(self.PARAM_GRID.keys())
        values = list(self.PARAM_GRID.values())
        return [
            dict(zip(keys, combo))
            for combo in itertools.product(*values)
        ]

    def run(
        self,
        markets:  List[str],
        top_n:    int = 5,
        max_runs: int = 50,
    ) -> Tuple[Dict, Dict]:
        """
        Lance le grid search. Retourne (meilleurs_params, meilleures_métriques).
        Limite à max_runs pour éviter des temps de calcul trop longs.
        """
        combos = self._combinations()
        # Shuffle pour que même un run partiel soit représentatif
        random.shuffle(combos)
        combos = combos[:max_runs]

        total = len(combos)
        log.info(f"[OPTIMIZER] Démarrage grid search — {total} combinaisons à tester")

        best_params:  Dict = {}
        best_metrics: Dict = {"profit_factor": 0.0}
        results: List[Tuple[float, Dict, Dict]] = []

        for idx, params in enumerate(combos, 1):
            # Appliquer les paramètres au cfg temporaire
            test_cfg = BotConfig()
            for k, v in params.items():
                setattr(test_cfg, k, v)

            bt     = Backtester(test_cfg, self.store)
            result = bt.run(markets)

            if result.total_trades < 10:
                continue  # trop peu de trades — pas représentatif

            metrics = result.summary()
            self.store.save_backtest_result(params, metrics)
            results.append((metrics["profit_factor"], params, metrics))

            if metrics["profit_factor"] > best_metrics["profit_factor"]:
                best_params  = params
                best_metrics = metrics
                log.info(
                    f"[OPTIMIZER] [{idx}/{total}] Nouveau meilleur → "
                    f"PF={metrics['profit_factor']:.3f}  "
                    f"WR={metrics['win_rate']:.1f}%  "
                    f"PnL={metrics['total_pnl']:+.2f}€  "
                    f"Params={params}"
                )

        # Afficher le top N
        results.sort(key=lambda x: x[0], reverse=True)
        print(f"\n{'═'*60}")
        print(f"  🏆 TOP {min(top_n, len(results))} COMBINAISONS")
        print(f"{'═'*60}")
        for rank, (pf, params, metrics) in enumerate(results[:top_n], 1):
            print(
                f"  #{rank}  PF={pf:.3f}  WR={metrics['win_rate']:.1f}%  "
                f"PnL={metrics['total_pnl']:+.2f}€  "
                f"DD={metrics['max_drawdown']:.1f}%  "
                f"Sharpe={metrics['sharpe']:.3f}"
            )
            print(f"       Params: {params}")
        print(f"{'═'*60}\n")

        return best_params, best_metrics


# ─────────────────────────────────────────────────────────────────────────────
#  MODULE 4 — WALK-FORWARD TEST
# ─────────────────────────────────────────────────────────────────────────────
class WalkForwardTester:
    """
    Validation robuste anti-overfitting.

    Principe :
    ┌──────────────────────────────────────────────────────┐
    │  Fenêtre 1 :  [train_1 | test_1]                     │
    │  Fenêtre 2 :        [train_2 | test_2]               │
    │  Fenêtre 3 :              [train_3 | test_3]         │
    └──────────────────────────────────────────────────────┘

    Pour chaque fenêtre :
    1. Optimiser les paramètres sur train
    2. Tester les meilleurs paramètres sur test (out-of-sample)
    3. Agréger les métriques test

    Si la stratégie est robuste, les métriques out-of-sample seront
    cohérentes avec les métriques in-sample.

    Usage :
      wf = WalkForwardTester(cfg, store)
      wf.run(markets, n_windows=5, train_ratio=0.7)
    """

    def __init__(self, cfg: BotConfig, store: DataStore):
        self.cfg   = cfg
        self.store = store

    def run(
        self,
        markets:     List[str],
        n_windows:   int   = 4,
        train_ratio: float = 0.70,
        max_runs:    int   = 20,
    ) -> List[Dict]:
        """
        Lance le walk-forward. Retourne les métriques out-of-sample par fenêtre.
        """
        # Charger toutes les bougies disponibles
        all_candles: Dict[str, List[Candle]] = {}
        for market in markets:
            c = self.store.load_candles(market, 15, limit=5000)
            if len(c) >= 400:
                all_candles[market] = c

        if not all_candles:
            log.error("[WF] Aucune donnée. Lance le téléchargement d'abord.")
            return []

        min_len = min(len(v) for v in all_candles.values())
        window_size = min_len // n_windows
        if window_size < 200:
            log.error(f"[WF] Fenêtres trop petites ({window_size} bougies). Besoin de plus de données.")
            return []

        log.info(
            f"[WF] Démarrage walk-forward — {n_windows} fenêtres · "
            f"{window_size} bougies/fenêtre · train={train_ratio:.0%}"
        )

        wf_results = []

        for w in range(n_windows):
            start = w * window_size
            end   = start + window_size
            split = start + int(window_size * train_ratio)

            log.info(f"[WF] Fenêtre {w+1}/{n_windows} — train:[{start}:{split}]  test:[{split}:{end}]")

            # ── Créer un DataStore temporaire en mémoire pour le train
            train_store = DataStore(":memory:")
            test_store  = DataStore(":memory:")

            for market, candles in all_candles.items():
                # Charger aussi les 1h proportionnellement
                c1h = self.store.load_candles(market, 60, limit=2000)
                split_1h = int(len(c1h) * split / min_len)

                train_store.save_candles(market, 15, candles[start:split])
                train_store.save_candles(market, 60, c1h[:split_1h])
                test_store.save_candles(market, 15,  candles[split:end])
                test_store.save_candles(market, 60,  c1h[split_1h:int(len(c1h)*end/min_len)])

            # ── Optimiser sur le train
            opt = ParameterOptimizer(self.cfg, train_store)
            best_params, _ = opt.run(list(all_candles.keys()), max_runs=max_runs)

            if not best_params:
                log.warning(f"[WF] Fenêtre {w+1} — optimisation sans résultat, skip")
                continue

            # ── Tester les meilleurs paramètres sur le test (out-of-sample)
            test_cfg = BotConfig()
            for k, v in best_params.items():
                setattr(test_cfg, k, v)

            bt_test = Backtester(test_cfg, test_store)
            result  = bt_test.run(list(all_candles.keys()))
            metrics = result.summary()
            metrics["window"]      = w + 1
            metrics["best_params"] = best_params
            wf_results.append(metrics)

            result.print_report(
                f"WF Fenêtre {w+1} — OUT-OF-SAMPLE  params={best_params}"
            )

        # ── Rapport global walk-forward
        if wf_results:
            avg_pf  = sum(r["profit_factor"] for r in wf_results) / len(wf_results)
            avg_wr  = sum(r["win_rate"] for r in wf_results) / len(wf_results)
            avg_pnl = sum(r["total_pnl"] for r in wf_results) / len(wf_results)
            avg_dd  = sum(r["max_drawdown"] for r in wf_results) / len(wf_results)
            consistency = sum(1 for r in wf_results if r["profit_factor"] > 1.0) / len(wf_results) * 100

            print(f"\n{'═'*60}")
            print(f"  🔬 WALK-FORWARD — RÉSUMÉ GLOBAL ({len(wf_results)} fenêtres)")
            print(f"{'═'*60}")
            print(f"  Profit Factor moyen : {avg_pf:.3f}")
            print(f"  Win Rate moyen      : {avg_wr:.1f}%")
            print(f"  PnL moyen/fenêtre   : {avg_pnl:+.2f}€")
            print(f"  Max Drawdown moyen  : {avg_dd:.2f}%")
            print(f"  Cohérence (PF>1)    : {consistency:.0f}% des fenêtres")
            if consistency >= 75:
                print(f"  ✅ Stratégie ROBUSTE (cohérence ≥ 75%)")
            elif consistency >= 50:
                print(f"  ⚠️  Stratégie MODÉRÉE (cohérence 50–75%)")
            else:
                print(f"  ❌ Stratégie FRAGILE (cohérence < 50%) — revoir les paramètres")
            print(f"{'═'*60}\n")

        return wf_results


# ─────────────────────────────────────────────────────────────────────────────
#  MODULE 5 — DASHBOARD HTML (monitoring temps réel, auto-refresh)
# ─────────────────────────────────────────────────────────────────────────────
class DashboardServer:
    """
    Serveur HTTP léger (stdlib uniquement) qui expose un dashboard HTML
    auto-rafraîchi toutes les 30 secondes.

    Accessible sur http://localhost:8080 (ou le port configuré).
    Affiche : capital, PnL, trades ouverts, historique, métriques clés.

    Usage :
      dashboard = DashboardServer(perf_tracker, risk_manager, open_trades_ref)
      asyncio.ensure_future(dashboard.start())
    """

    HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="30">
<title>⚡ QUANTUM EDGE — Dashboard</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #0a0e1a; color: #e0e6f0; font-family: 'Courier New', monospace;
          font-size: 13px; padding: 20px; }}
  h1 {{ color: #00d4ff; font-size: 20px; margin-bottom: 16px; letter-spacing: 2px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin-bottom: 20px; }}
  .card {{ background: #111827; border: 1px solid #1e3a5f; border-radius: 8px; padding: 14px; }}
  .card .label {{ color: #6b7fa3; font-size: 11px; text-transform: uppercase; letter-spacing: 1px; }}
  .card .value {{ font-size: 22px; font-weight: bold; margin-top: 4px; }}
  .pos {{ color: #22c55e; }} .neg {{ color: #ef4444; }} .neu {{ color: #00d4ff; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
  th {{ background: #1e3a5f; color: #00d4ff; padding: 8px; text-align: left; font-size: 11px; }}
  td {{ padding: 7px 8px; border-bottom: 1px solid #1a2540; font-size: 12px; }}
  tr:hover td {{ background: #131f35; }}
  .badge-buy {{ color: #22c55e; font-weight: bold; }}
  .badge-sell {{ color: #ef4444; font-weight: bold; }}
  .ts {{ color: #4a5c7a; font-size: 11px; margin-top: 16px; text-align: right; }}
</style>
</head>
<body>
<h1>⚡ QUANTUM EDGE v3.0 — Live Dashboard</h1>
<div class="grid">
  <div class="card"><div class="label">Capital</div>
    <div class="value neu">{capital:.2f} €</div></div>
  <div class="card"><div class="label">PnL Total</div>
    <div class="value {pnl_class}">{total_pnl:+.2f} €</div></div>
  <div class="card"><div class="label">PnL Journalier</div>
    <div class="value {dpnl_class}">{daily_pnl:+.2f} €</div></div>
  <div class="card"><div class="label">Win Rate</div>
    <div class="value neu">{win_rate:.1f} %</div></div>
  <div class="card"><div class="label">Profit Factor</div>
    <div class="value neu">{profit_factor:.3f}</div></div>
  <div class="card"><div class="label">Trades ouverts</div>
    <div class="value neu">{open_trades}</div></div>
  <div class="card"><div class="label">Total Trades</div>
    <div class="value neu">{total_trades}</div></div>
  <div class="card"><div class="label">Sharpe Ratio</div>
    <div class="value neu">{sharpe:.3f}</div></div>
</div>

<h2 style="color:#6b7fa3;font-size:13px;margin-bottom:8px;">TRADES OUVERTS</h2>
<table>
  <tr><th>Marché</th><th>Dir.</th><th>Entrée</th><th>PnL latent</th><th>Score</th><th>Régime</th></tr>
  {open_rows}
</table>

<h2 style="color:#6b7fa3;font-size:13px;margin:16px 0 8px;">DERNIERS TRADES FERMÉS</h2>
<table>
  <tr><th>Marché</th><th>Dir.</th><th>Entrée</th><th>Sortie</th><th>PnL</th><th>Raison</th></tr>
  {closed_rows}
</table>

<div class="ts">Mis à jour : {ts} UTC · Auto-refresh 30s</div>
</body></html>"""

    def __init__(
        self,
        perf:        "PerformanceTracker",
        risk:        "RiskManager",
        open_trades: Dict,
        port:        int = 8080,
    ):
        self.perf        = perf
        self.risk        = risk
        self.open_trades = open_trades
        self.port        = port

    def _build_html(self) -> str:
        pnl   = self.perf.total_pnl
        dpnl  = self.risk.daily_pnl

        # Lignes trades ouverts
        open_rows = ""
        for market, trade in self.open_trades.items():
            side_cls = "badge-buy" if trade.side == Signal.BUY else "badge-sell"
            open_rows += (
                f"<tr><td>{market}</td>"
                f"<td class='{side_cls}'>{trade.side.value}</td>"
                f"<td>{trade.entry_price:.6f}</td>"
                f"<td>—</td>"
                f"<td>{trade.score}</td>"
                f"<td>{trade.regime.value}</td></tr>"
            )
        if not open_rows:
            open_rows = "<tr><td colspan='6' style='color:#4a5c7a;text-align:center'>Aucun trade ouvert</td></tr>"

        # Lignes derniers trades fermés (10 derniers)
        closed_rows = ""
        for t in reversed(self.perf.all_trades[-10:]):
            pnl_cls = "pos" if t.pnl_eur > 0 else "neg"
            side_cls = "badge-buy" if t.side == Signal.BUY else "badge-sell"
            closed_rows += (
                f"<tr><td>{t.market}</td>"
                f"<td class='{side_cls}'>{t.side.value}</td>"
                f"<td>{t.entry_price:.6f}</td>"
                f"<td>{t.exit_price:.6f}</td>"
                f"<td class='{pnl_cls}'>{t.pnl_eur:+.2f}€</td>"
                f"<td>{t.exit_time.strftime('%H:%M') if t.exit_time else '—'}</td></tr>"
            )
        if not closed_rows:
            closed_rows = "<tr><td colspan='6' style='color:#4a5c7a;text-align:center'>Aucun trade fermé</td></tr>"

        return self.HTML_TEMPLATE.format(
            capital       = self.risk.capital,
            total_pnl     = pnl,
            pnl_class     = "pos" if pnl >= 0 else "neg",
            daily_pnl     = dpnl,
            dpnl_class    = "pos" if dpnl >= 0 else "neg",
            win_rate      = self.perf.win_rate,
            profit_factor = self.perf.profit_factor,
            open_trades   = len(self.open_trades),
            total_trades  = self.perf.total_trades,
            sharpe        = self.perf.sharpe_ratio(),
            open_rows     = open_rows,
            closed_rows   = closed_rows,
            ts            = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        )

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            await reader.read(1024)  # lire la requête HTTP
            html    = self._build_html().encode("utf-8")
            headers = (
                f"HTTP/1.1 200 OK\r\n"
                f"Content-Type: text/html; charset=utf-8\r\n"
                f"Content-Length: {len(html)}\r\n"
                f"Connection: close\r\n\r\n"
            ).encode()
            writer.write(headers + html)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    async def start(self):
        """Démarre le serveur HTTP en tâche de fond."""
        try:
            server = await asyncio.start_server(self._handle, "0.0.0.0", self.port)
            log.info(f"[DASHBOARD] Serveur démarré → http://0.0.0.0:{self.port}")
            async with server:
                await server.serve_forever()
        except Exception as e:
            log.warning(f"[DASHBOARD] Impossible de démarrer : {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  MODULE 6 — TÉLÉCHARGEUR DE DONNÉES HISTORIQUES
# ─────────────────────────────────────────────────────────────────────────────
class HistoricalDownloader:
    """
    Télécharge et stocke les données historiques Kraken en SQLite.

    Kraken retourne max 720 bougies par appel.
    Ce module pagine automatiquement pour remplir la base jusqu'à `target_days`.

    Usage :
      dl = HistoricalDownloader(kraken_client, store)
      await dl.download_all(markets, interval_min=15, target_days=180)
    """

    def __init__(self, kraken: "KrakenClient", store: DataStore):
        self.kraken = kraken
        self.store  = store

    async def download_market(
        self,
        market:       str,
        interval_min: int = 15,
        target_days:  int = 180,
    ) -> int:
        """
        Télécharge jusqu'à `target_days` jours de données pour un marché.
        Retourne le nombre de bougies sauvegardées.
        """
        target_candles = target_days * 24 * 60 // interval_min
        existing       = self.store.candle_count(market, interval_min)

        if existing >= target_candles * 0.95:
            log.info(f"[DL] {market} {interval_min}m — déjà à jour ({existing} bougies)")
            return 0

        log.info(
            f"[DL] {market} {interval_min}m — téléchargement "
            f"(objectif {target_candles}, en base {existing})"
        )

        total_saved = 0
        oldest_ts = 0
        # Récupérer le timestamp le plus ancien déjà stocké
        try:
            if self.store.use_postgres:
                with self.store._pg_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT MIN(timestamp) FROM candles WHERE market=%s AND interval_min=%s",
                            (market, interval_min),
                        )
                        row = cur.fetchone()
                        if row and row.get("min"):
                            oldest_ts = int(row["min"])
            else:
                with self.store._sqlite_conn() as conn:
                    row = conn.execute(
                        "SELECT MIN(timestamp) FROM candles WHERE market=? AND interval_min=?",
                        (market, interval_min),
                    ).fetchone()
                    if row and row[0]:
                        oldest_ts = row[0]
        except Exception:
            oldest_ts = 0

        for _ in range(20):  # max 20 pages = ~14 400 bougies
            candles = await self.kraken.fetch_ohlcv(market, interval_min, count=720)
            if not candles:
                break

            new_candles = (
                [c for c in candles if c.timestamp < oldest_ts]
                if oldest_ts else candles
            )
            if not new_candles:
                break

            self.store.save_candles(market, interval_min, new_candles)
            total_saved += len(new_candles)
            oldest_ts    = min(c.timestamp for c in new_candles)

            current_count = self.store.candle_count(market, interval_min)
            if current_count >= target_candles:
                break

            await asyncio.sleep(0.5)  # respecter le rate limit Kraken

        log.info(f"[DL] {market} {interval_min}m — {total_saved} nouvelles bougies sauvegardées")
        return total_saved

    async def download_all(
        self,
        markets:      List[str],
        interval_min: int = 15,
        target_days:  int = 180,
    ) -> Dict[str, int]:
        """Télécharge toutes les paires en séquentiel (respecte le rate limit)."""
        results: Dict[str, int] = {}
        for market in markets:
            n = await self.download_market(market, interval_min, target_days)
            # Télécharger aussi les 1h pour les indicateurs macro
            n1h = await self.download_market(market, 60, target_days)
            results[market] = n + n1h
            await asyncio.sleep(1.0)  # pause entre marchés
        total = sum(results.values())
        log.info(f"[DL] Téléchargement terminé — {total} bougies au total")
        return results


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
    # ── Telegram (optionnel)
    cfg.use_telegram      = os.environ.get("USE_TELEGRAM", "false").lower() == "true"
    cfg.telegram_token    = os.environ.get("TELEGRAM_TOKEN", "")
    cfg.telegram_chat_id  = os.environ.get("TELEGRAM_CHAT_ID", "")
    # ── PostgreSQL (Railway fournit DATABASE_URL automatiquement)
    cfg.database_url      = os.environ.get("DATABASE_URL", "")

    # ────────────────────────────────────────────────────────────────────────
    #  MODE CLI — usage : python bot_trading.py [commande]
    #
    #  Commandes disponibles :
    #    (aucune)         → démarrer le bot en mode normal
    #    download         → télécharger 180j de données historiques
    #    backtest         → lancer un backtest sur les données stockées
    #    optimize         → grid search des paramètres optimaux
    #    walkforward      → validation walk-forward anti-overfitting
    # ────────────────────────────────────────────────────────────────────────
    command = sys.argv[1] if len(sys.argv) > 1 else "run"

    store = DataStore()

    if command == "download":
        # ── Télécharger les données historiques (180 jours)
        async def _download():
            kraken = KrakenClient()
            dl     = HistoricalDownloader(kraken, store)
            print("\n📥 Téléchargement des données historiques (180 jours)...")
            await dl.download_all(cfg.markets, interval_min=15, target_days=180)
            await dl.download_all(cfg.markets, interval_min=60, target_days=180)
            await kraken.close()
            print("\n✅ Téléchargement terminé. Lance maintenant : python bot_trading.py backtest")
        asyncio.run(_download())

    elif command == "backtest":
        # ── Backtest sur les données stockées
        print("\n🔬 Lancement du backtest...")
        bt     = Backtester(cfg, store)
        result = bt.run(cfg.markets)
        result.print_report("BACKTEST COMPLET")
        # Afficher les 5 meilleurs résultats historiques
        best = store.best_backtest_results(top_n=5)
        if best:
            print(f"\n🏆 TOP 5 BACKTESTS EN BASE :")
            for r in best:
                print(
                    f"  {r['run_at'][:16]}  "
                    f"PF={r['profit_factor']:.3f}  WR={r['win_rate']:.1f}%  "
                    f"PnL={r['total_pnl']:+.2f}€  DD={r['max_drawdown']:.1f}%"
                )

    elif command == "optimize":
        # ── Grid search des paramètres
        print("\n⚙️  Lancement de l'optimisation (grid search)...")
        opt = ParameterOptimizer(cfg, store)
        best_params, best_metrics = opt.run(cfg.markets, top_n=5, max_runs=50)
        if best_params:
            print(f"\n✅ Meilleurs paramètres trouvés :")
            for k, v in best_params.items():
                print(f"   {k} = {v}")
            print(f"\n   PF={best_metrics['profit_factor']:.3f}  "
                  f"WR={best_metrics['win_rate']:.1f}%  "
                  f"PnL={best_metrics['total_pnl']:+.2f}€  "
                  f"Sharpe={best_metrics['sharpe']:.3f}")
            print("\n💡 Applique ces valeurs dans BotConfig ou via variables d'environnement.")

    elif command == "walkforward":
        # ── Walk-forward test
        print("\n🔬 Lancement du walk-forward test (4 fenêtres)...")
        wf      = WalkForwardTester(cfg, store)
        windows = wf.run(cfg.markets, n_windows=4, train_ratio=0.70, max_runs=20)
        if not windows:
            print("❌ Pas assez de données. Lance d'abord : python bot_trading.py download")

    else:
        # ── Mode normal : démarrer le bot
        bot = QuantumEdgeBot(cfg)
        asyncio.run(bot.run())
