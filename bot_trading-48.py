"""
╔══════════════════════════════════════════════════════════════════════════════╗
║           QUANTUM EDGE V4 — MULTI-MARCHÉS + STRUCTURE V7.4                 ║
║                                                                              ║
║  Base     : Quantum Edge (multi-marchés, score composite)                   ║
║  Structure: V7.4 (trailing ATR progressif, break-even, swing trading)       ║
║                                                                              ║
║  Marchés  : 13 paires USDT — jusqu'à 3 trades simultanés                   ║
║  Signaux  : ADX + RSI + Volume (simple et efficace)                         ║
║  Trailing : Paliers progressifs +0.75€ → +3€ → +7.50€ → ...               ║
║  Break-even: Stop remonte au prix d'entrée dès +ATR×1.0                    ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import aiohttp

# ─────────────────────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
# Forcer le flush immédiat des logs sur Railway
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(line_buffering=True)
log = logging.getLogger("QE_V4")

# ─────────────────────────────────────────────────────────────────────────────
#  TRAILING STOP PALIERS (structure V7.4)
#  PnL en € → multiplicateur ATR pour le trailing
# ─────────────────────────────────────────────────────────────────────────────
TRAILING_PALIERS = [
    (100.0, 0.05),
    ( 75.0, 0.07),
    ( 50.0, 0.10),
    ( 35.0, 0.15),
    ( 25.0, 0.20),
    ( 18.0, 0.30),
    ( 12.0, 0.50),
    (  7.5, 0.80),
    (  3.0, 1.50),
    (  0.75, 2.00),
    (  0.0, 2.50),
]

def get_trailing_mult(pnl_eur: float) -> float:
    """Retourne le multiplicateur ATR selon le PnL en euros."""
    if pnl_eur <= 0:
        return 2.50  # stop fixe tant que pas en gain
    for seuil, mult in TRAILING_PALIERS:
        if pnl_eur >= seuil:
            return mult
    return 2.50

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Config:
    simulation_mode:  bool  = True
    initial_capital:  float = 200.0
    stake_eur:        float = 10.0      # mise fixe par trade
    leverage:         int   = 3         # levier
    max_open_trades:  int   = 3         # trades simultanés max

    # Filtres signal
    adx_min:          int   = 25        # ADX minimum
    rsi_oversold:     int   = 35        # RSI achat
    rsi_overbought:   int   = 65        # RSI vente
    volume_min:       float = 0.40      # volume min vs moyenne 24h
    score_min:        int   = 2         # score minimum sur 3

    # Risk management (ATR-based comme V7.4)
    atr_multiplier:   float = 2.5       # stop = ATR × 2.5
    ratio_rr:         float = 2.0       # objectif = stop × 2
    timeout_hours:    int   = 12        # timeout trade

    # Kill switch
    daily_kill_eur:   float = -5.0      # stop journalier
    max_losses_streak: int  = 4         # pertes consécutives max
    cooldown_minutes: int   = 10        # pause après série de pertes

    # Marchés
    markets: List[str] = field(default_factory=lambda: [
        "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
        "AVAX/USDT", "LINK/USDT", "ADA/USDT", "DOT/USDT",
        "DOGE/USDT", "ATOM/USDT", "LTC/USDT", "ALGO/USDT", "XTZ/USDT",
    ])

# ─────────────────────────────────────────────────────────────────────────────
#  KRAKEN CLIENT
# ─────────────────────────────────────────────────────────────────────────────
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
    "ATOM/USDT": "ATOMUSDT",
    "LTC/USDT":  "LTCUSDT",
    "ALGO/USDT": "ALGOUSDT",
    "XTZ/USDT":  "XTZUSDT",
}

class KrakenClient:
    BASE = "https://api.kraken.com/0/public"

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_candles(self, market: str, interval: int = 60, count: int = 100) -> List[dict]:
        """Récupère les bougies OHLCV depuis Kraken."""
        pair = SYMBOL_MAP.get(market, market.replace("/", ""))
        session = await self._get()
        try:
            async with session.get(
                f"{self.BASE}/OHLC",
                params={"pair": pair, "interval": interval}
            ) as resp:
                data = await resp.json()
                if data.get("error"):
                    return []
                result = data.get("result", {})
                keys = [k for k in result if k != "last"]
                if not keys:
                    return []
                raw = result[keys[0]][-count:]
                return [
                    {
                        "time":   int(c[0]),
                        "open":   float(c[1]),
                        "high":   float(c[2]),
                        "low":    float(c[3]),
                        "close":  float(c[4]),
                        "volume": float(c[6]),
                    }
                    for c in raw
                ]
        except Exception as e:
            log.warning(f"[KRAKEN] {market} erreur : {e}")
            return []

# ─────────────────────────────────────────────────────────────────────────────
#  INDICATEURS TECHNIQUES
# ─────────────────────────────────────────────────────────────────────────────
def calc_rsi(closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0:
        return 100.0
    rs = ag / al
    return round(100 - 100 / (1 + rs), 2)

def calc_atr(candles: List[dict], period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return round(sum(trs[-period:]) / period, 8)

def calc_adx(candles: List[dict], period: int = 14) -> float:
    if len(candles) < period * 2:
        return 0.0
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(candles)):
        h, l   = candles[i]["high"],   candles[i]["low"]
        ph, pl = candles[i-1]["high"], candles[i-1]["low"]
        pc     = candles[i-1]["close"]
        up, down = h - ph, pl - l
        plus_dm.append(up if up > down and up > 0 else 0)
        minus_dm.append(down if down > up and down > 0 else 0)
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    def smooth(arr):
        s = sum(arr[:period])
        out = [s]
        for v in arr[period:]:
            s = s - s / period + v
            out.append(s)
        return out
    atr_s  = smooth(trs)
    pdm_s  = smooth(plus_dm)
    mdm_s  = smooth(minus_dm)
    dx_vals = []
    for a, p, m in zip(atr_s, pdm_s, mdm_s):
        if a == 0:
            continue
        pdi, mdi = 100 * p / a, 100 * m / a
        dx = 100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 0 else 0
        dx_vals.append(dx)
    if not dx_vals:
        return 0.0
    return round(sum(dx_vals[-period:]) / min(len(dx_vals), period), 2)

def calc_volume_ratio(candles: List[dict]) -> float:
    """
    Compare le volume de la dernière bougie FERMÉE (index -2)
    à la moyenne des 24 bougies précédentes.
    La dernière bougie (index -1) est en cours → volume incomplet → exclue.
    """
    if len(candles) < 26:
        return 0.0
    # Bougies fermées : toutes sauf la dernière
    closed = candles[:-1]
    avg = sum(c["volume"] for c in closed[-24:]) / 24
    if avg == 0:
        return 0.0
    # Volume de la dernière bougie fermée
    last_closed_vol = closed[-1]["volume"]
    return round(last_closed_vol / avg, 4)

# ─────────────────────────────────────────────────────────────────────────────
#  SIGNAL (score simple sur 3 critères comme V7.4)
# ─────────────────────────────────────────────────────────────────────────────
def compute_signal(candles: List[dict], cfg: Config) -> Tuple[str, int, dict]:
    """
    Retourne (direction, score, details)
    direction : "BUY" | "SELL" | "NONE"
    score     : 0-3
    """
    if len(candles) < 30:
        return "NONE", 0, {}

    closes = [c["close"] for c in candles]
    rsi    = calc_rsi(closes)
    adx    = calc_adx(candles)
    atr    = calc_atr(candles)
    vol_r  = calc_volume_ratio(candles)

    details = {"rsi": rsi, "adx": adx, "atr": atr, "vol_ratio": vol_r}

    # Volume insuffisant
    if vol_r < cfg.volume_min:
        return "NONE", 0, details

    # ADX insuffisant
    if adx < cfg.adx_min:
        return "NONE", 0, details

    score = 0
    direction = "NONE"

    if rsi < cfg.rsi_oversold:
        direction = "BUY"
        score += 1
        if rsi < cfg.rsi_oversold - 10:
            score += 1  # RSI très bas = signal plus fort
        if adx > 35:
            score += 1  # tendance forte

    elif rsi > cfg.rsi_overbought:
        direction = "SELL"
        score += 1
        if rsi > cfg.rsi_overbought + 10:
            score += 1
        if adx > 35:
            score += 1

    if score < cfg.score_min:
        return "NONE", 0, details

    return direction, score, details

# ─────────────────────────────────────────────────────────────────────────────
#  TRADE
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Trade:
    market:       str
    side:         str       # "BUY" | "SELL"
    entry_price:  float
    stake:        float
    leverage:     int
    atr:          float
    stop_loss:    float
    target:       float
    entry_time:   datetime
    score:        int
    stop_current: float     = 0.0
    peak_price:   float     = 0.0
    break_even:   bool      = False
    trailing_mult: float    = 2.50

    def __post_init__(self):
        self.stop_current = self.stop_loss
        self.peak_price   = self.entry_price

    @property
    def position_size(self) -> float:
        return self.stake * self.leverage

    def pnl(self, current_price: float) -> float:
        """PnL en euros sur la position."""
        if self.side == "BUY":
            pct = (current_price - self.entry_price) / self.entry_price
        else:
            pct = (self.entry_price - current_price) / self.entry_price
        gross = self.position_size * pct
        fees  = self.position_size * 0.0004 * 2  # 0.04% × 2
        return round(gross - fees, 4)

    def update_trailing(self, current_price: float) -> bool:
        """
        Met à jour le trailing stop selon les paliers V7.4.
        Retourne True si le stop a bougé.
        """
        pnl_eur = self.pnl(current_price)
        mult    = get_trailing_mult(pnl_eur)
        self.trailing_mult = mult
        distance = self.atr * mult

        # Break-even : dès que PnL > 0, on remonte le stop au prix d'entrée
        if pnl_eur > 0 and not self.break_even:
            if self.side == "BUY":
                be_stop = self.entry_price * (1 - 0.0002)  # légèrement sous l'entrée
                if be_stop > self.stop_current:
                    self.stop_current = be_stop
                    self.break_even   = True
                    log.info(f"  [{self.market}] 🔒 BREAK-EVEN activé @ {be_stop:.6f} (PnL={pnl_eur:+.2f}€)")
                    return True
            else:
                be_stop = self.entry_price * (1 + 0.0002)
                if be_stop < self.stop_current:
                    self.stop_current = be_stop
                    self.break_even   = True
                    log.info(f"  [{self.market}] 🔒 BREAK-EVEN activé @ {be_stop:.6f} (PnL={pnl_eur:+.2f}€)")
                    return True

        # Trailing progressif
        moved = False
        if self.side == "BUY":
            if current_price > self.peak_price:
                self.peak_price = current_price
            new_stop = self.peak_price - distance
            if new_stop > self.stop_current:
                self.stop_current = new_stop
                moved = True
        else:
            if current_price < self.peak_price:
                self.peak_price = current_price
            new_stop = self.peak_price + distance
            if new_stop < self.stop_current:
                self.stop_current = new_stop
                moved = True

        return moved

    def is_stopped(self, current_price: float) -> bool:
        if self.side == "BUY":
            return current_price <= self.stop_current
        return current_price >= self.stop_current

    def is_target(self, current_price: float) -> bool:
        if self.side == "BUY":
            return current_price >= self.target
        return current_price <= self.target

    def duration_minutes(self) -> int:
        return int((datetime.now(timezone.utc) - self.entry_time).total_seconds() / 60)

# ─────────────────────────────────────────────────────────────────────────────
#  PERFORMANCE TRACKER
# ─────────────────────────────────────────────────────────────────────────────
class PerfTracker:
    def __init__(self, initial_capital: float):
        self.capital      = initial_capital
        self.initial      = initial_capital
        self.pnl_today    = 0.0
        self.trades_total = 0
        self.wins         = 0
        self.losses       = 0
        self.loss_streak  = 0
        self.total_won    = 0.0
        self.total_lost   = 0.0

    def record(self, pnl: float):
        self.capital   = round(self.capital + pnl, 4)
        self.pnl_today = round(self.pnl_today + pnl, 4)
        self.trades_total += 1
        if pnl >= 0:
            self.wins       += 1
            self.total_won  += pnl
            self.loss_streak = 0
        else:
            self.losses     += 1
            self.total_lost += abs(pnl)
            self.loss_streak += 1

    @property
    def win_rate(self) -> float:
        if self.trades_total == 0:
            return 0.0
        return round(self.wins / self.trades_total * 100, 1)

    @property
    def profit_factor(self) -> float:
        if self.total_lost == 0:
            return float("inf")
        return round(self.total_won / self.total_lost, 2)

    def dashboard(self) -> str:
        return (
            f"\n{'═'*55}\n"
            f"  ⚡ QUANTUM EDGE V4 — TABLEAU DE BORD\n"
            f"{'═'*55}\n"
            f"  💰 Capital      : {self.capital:.2f}€ (départ: {self.initial:.2f}€)\n"
            f"  📊 PnL journalier: {self.pnl_today:+.2f}€\n"
            f"  🔒 Trades total : {self.trades_total}\n"
            f"  ✅ Win rate     : {self.win_rate}%\n"
            f"  🏆 Profit Factor: {self.profit_factor}\n"
            f"  📈 Total gagné  : +{self.total_won:.2f}€\n"
            f"  📉 Total perdu  : -{self.total_lost:.2f}€\n"
            f"{'═'*55}"
        )

# ─────────────────────────────────────────────────────────────────────────────
#  BOT PRINCIPAL
# ─────────────────────────────────────────────────────────────────────────────
class QuantumEdgeV4:
    def __init__(self, cfg: Config):
        self.cfg    = cfg
        self.kraken = KrakenClient()
        self.perf   = PerfTracker(cfg.initial_capital)
        self.open_trades: Dict[str, Trade] = {}
        self.cooldown_until: float = 0.0
        self.cycle = 0

    async def _get_price(self, market: str) -> Optional[float]:
        candles = await self.kraken.get_candles(market, interval=1, count=2)
        if not candles:
            return None
        return candles[-1]["close"]

    async def _analyze(self, market: str) -> Tuple[str, int, dict, float]:
        """Analyse un marché — retourne (direction, score, details, atr)."""
        candles = await self.kraken.get_candles(market, interval=60, count=150)
        if not candles:
            return "NONE", 0, {}, 0.0
        direction, score, details = compute_signal(candles, self.cfg)
        atr = details.get("atr", 0.0)
        return direction, score, details, atr

    async def _open_trade(self, market: str, direction: str, score: int, atr: float):
        price = await self._get_price(market)
        if not price or atr == 0:
            return

        stake    = self.cfg.stake_eur
        distance = atr * self.cfg.atr_multiplier

        if direction == "BUY":
            stop   = round(price - distance, 8)
            target = round(price + distance * self.cfg.ratio_rr, 8)
        else:
            stop   = round(price + distance, 8)
            target = round(price - distance * self.cfg.ratio_rr, 8)

        trade = Trade(
            market      = market,
            side        = direction,
            entry_price = price,
            stake       = stake,
            leverage    = self.cfg.leverage,
            atr         = atr,
            stop_loss   = stop,
            target      = target,
            entry_time  = datetime.now(timezone.utc),
            score       = score,
        )
        self.open_trades[market] = trade

        mode = "🔴 SIM" if self.cfg.simulation_mode else "🟢 LIVE"
        log.info(
            f"[OPEN] {mode} {market} {direction} @ {price:.6f} | "
            f"Stop: {stop:.6f} | Target: {target:.6f} | "
            f"Mise: {stake:.0f}€×{self.cfg.leverage} | Score: {score}/3"
        )

    async def _monitor_trades(self):
        """Surveille les trades ouverts — trailing stop + sorties."""
        to_close = []

        for market, trade in list(self.open_trades.items()):
            price = await self._get_price(market)
            if not price:
                continue

            pnl  = trade.pnl(price)
            mins = trade.duration_minutes()
            moved = trade.update_trailing(price)

            if moved:
                log.info(
                    f"  [{market}] Trailing ×{trade.trailing_mult} | "
                    f"Stop: {trade.stop_current:.6f} | PnL: {pnl:+.2f}€ | {mins}min"
                )

            # Timeout
            if mins >= self.cfg.timeout_hours * 60:
                to_close.append((market, trade, price, "TIMEOUT"))
                continue

            # Stop touché
            if trade.is_stopped(price):
                to_close.append((market, trade, price, "STOPLOSS"))
                continue

            # Target atteint
            if trade.is_target(price):
                to_close.append((market, trade, price, "TARGET"))
                continue

            log.info(
                f"  [OPEN] {market} {trade.side} @ {price:.6f} | "
                f"PnL: {pnl:+.2f}€ | Stop: {trade.stop_current:.6f} | {mins}min"
            )

        for market, trade, price, reason in to_close:
            await self._close_trade(market, trade, price, reason)

    async def _close_trade(self, market: str, trade: Trade, price: float, reason: str):
        pnl  = trade.pnl(price)
        mins = trade.duration_minutes()
        icon = "✅" if pnl >= 0 else "❌"

        self.perf.record(pnl)
        del self.open_trades[market]

        log.info(
            f"{icon} TRADE FERMÉ {market} {trade.side} | "
            f"Entrée: {trade.entry_price:.6f} Sortie: {price:.6f} | "
            f"PnL: {pnl:+.2f}€ ({reason}) | Score: {trade.score}/3 | Durée: {mins}min"
        )
        log.info(f"  [SIM] Capital: {self.perf.capital:.2f}€ | PnL jour: {self.perf.pnl_today:+.2f}€")

        # Cooldown si trop de pertes consécutives
        if self.perf.loss_streak >= self.cfg.max_losses_streak:
            self.cooldown_until = time.time() + self.cfg.cooldown_minutes * 60
            self.perf.loss_streak = 0
            log.warning(
                f"[COOLDOWN] {self.cfg.max_losses_streak} pertes consécutives — "
                f"pause {self.cfg.cooldown_minutes} min"
            )

    async def run(self):
        log.info("🚀 QUANTUM EDGE V4 démarré — Mode: " + ("SIMULATION" if self.cfg.simulation_mode else "LIVE"))
        log.info(f"   Marchés: {len(self.cfg.markets)} | Max trades: {self.cfg.max_open_trades} | Mise: {self.cfg.stake_eur}€×{self.cfg.leverage}")

        last_dashboard = 0

        while True:
            try:
                self.cycle += 1

                # ── Kill switch journalier
                if self.perf.pnl_today <= self.cfg.daily_kill_eur:
                    log.warning(f"[KILL SWITCH] PnL journalier {self.perf.pnl_today:.2f}€ ≤ {self.cfg.daily_kill_eur}€ — arrêt")
                    break

                # ── Cooldown
                if time.time() < self.cooldown_until:
                    remaining = int((self.cooldown_until - time.time()) / 60)
                    log.info(f"[COOLDOWN] {remaining} min restantes")
                    await asyncio.sleep(60)
                    continue

                # ── Surveiller les trades ouverts
                if self.open_trades:
                    await self._monitor_trades()

                # ── Dashboard toutes les 20 minutes
                if time.time() - last_dashboard > 1200:
                    log.info(self.perf.dashboard())
                    last_dashboard = time.time()

                # ── Chercher nouveaux signaux si place disponible
                if len(self.open_trades) < self.cfg.max_open_trades:
                    log.info(f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Scan — {len(self.cfg.markets)} marchés...")
                    candidates = []
                    for market in self.cfg.markets:
                        if market in self.open_trades:
                            continue
                        direction, score, details, atr = await self._analyze(market)
                        rsi   = details.get('rsi', 0)
                        adx   = details.get('adx', 0)
                        vol_r = details.get('vol_ratio', 0)
                        if direction == "NONE":
                            if vol_r < self.cfg.volume_min:
                                log.info(f"  {market} : Volume {vol_r:.2f}x < {self.cfg.volume_min}x → skip")
                            elif adx < self.cfg.adx_min:
                                log.info(f"  {market} : ADX {adx:.1f} < {self.cfg.adx_min} → skip")
                            else:
                                log.info(f"  {market} : RSI {rsi:.1f} | ADX {adx:.1f} → pas de signal")
                        else:
                            candidates.append((score, market, direction, atr, details))
                            log.info(
                                f"  [SIGNAL] {market} {direction} | Score: {score}/3 | "
                                f"RSI: {rsi:.1f} | ADX: {adx:.1f} | Vol: {vol_r:.2f}x"
                            )
                        await asyncio.sleep(0.5)

                    if not candidates:
                        log.info("  => Aucun signal. Prochaine analyse dans 30s...")

                    # Ouvrir les meilleurs signaux
                    candidates.sort(reverse=True)
                    for score, market, direction, atr, _ in candidates:
                        if len(self.open_trades) >= self.cfg.max_open_trades:
                            break
                        await self._open_trade(market, direction, score, atr)

                await asyncio.sleep(30)

            except KeyboardInterrupt:
                log.info("Arrêt demandé.")
                break
            except Exception as e:
                log.error(f"Erreur cycle {self.cycle}: {e}")
                await asyncio.sleep(60)

        await self.kraken.close()
        log.info(self.perf.dashboard())

# ─────────────────────────────────────────────────────────────────────────────
#  POINT D'ENTRÉE
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Forcer output non-bufferisé sur Railway
    os.environ["PYTHONUNBUFFERED"] = "1"
    cfg = Config()
    cfg.simulation_mode = os.environ.get("SIMULATION_MODE", "true").lower() == "true"
    cfg.initial_capital = float(os.environ.get("INITIAL_CAPITAL", "200"))
    cfg.stake_eur       = float(os.environ.get("STAKE_EUR", "10"))
    cfg.leverage        = int(os.environ.get("LEVERAGE", "3"))
    cfg.daily_kill_eur  = float(os.environ.get("DAILY_KILL_EUR", "-5"))
    cfg.score_min       = int(os.environ.get("SCORE_MIN", "2"))

    bot = QuantumEdgeV4(cfg)
    asyncio.run(bot.run())
