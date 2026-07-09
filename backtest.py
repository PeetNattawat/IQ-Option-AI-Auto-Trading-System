"""
backtest.py — in-sample / out-of-sample simulator (San's Architecture Notes §11).

Replays TrendFilter -> EntrySignal -> RiskManager exactly as the live state machine
would, bar by bar, NEVER looking ahead. Entry price = OPEN of the candle AFTER the
signal candle (spec §10.3 slippage model), not the signal candle's close. Reuses
IndicatorEngineV2, TrendFilter, EntrySignal, RiskManager UNMODIFIED from the live
path — no separate backtest-only reimplementation of the strategy logic.

Pass criteria (spec §10, Vector's Gate 3 gate, NOT relaxable by Titan):
  out-of-sample winrate >= 58%, no single month < 52%, max drawdown < 15%,
  >= 150 orders.

Overfitting guard (Peet's rule, brief §4): every parameter set run against
out-of-sample data is hashed and logged (trade_logger.backtest_runs) so a set can
never be silently re-tuned after seeing out-of-sample results.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from entry_signal import EntrySignal
from indicators_v2 import IndicatorEngineV2
from risk_manager import RiskConfig, RiskManager
from trend_filter import TrendFilter

DEFAULT_PAYOUT = 0.85
DEFAULT_EXPIRY_MINUTES = 15
DEFAULT_INITIAL_BALANCE = 1000.0


@dataclass
class BacktestReport:
    pair: str
    sample_type: str            # "in_sample" | "out_of_sample"
    total_orders: int
    wins: int
    losses: int
    equals: int
    winrate: float
    monthly_winrate: dict = field(default_factory=dict)
    max_drawdown_pct: float = 0.0
    final_balance: float = 0.0
    param_hash: str = ""

    def passes_spec_gate(self) -> tuple[bool, list[str]]:
        """Spec §10 pass criteria — informational only, Vector owns the actual gate."""
        problems = []
        if self.sample_type != "out_of_sample":
            return False, ["not an out-of-sample run"]
        if self.winrate < 58.0:
            problems.append(f"winrate {self.winrate:.1f}% < 58%")
        for m, wr in self.monthly_winrate.items():
            if wr < 52.0:
                problems.append(f"month {m} winrate {wr:.1f}% < 52%")
        if self.max_drawdown_pct >= 15.0:
            problems.append(f"max drawdown {self.max_drawdown_pct:.1f}% >= 15%")
        if self.total_orders < 150:
            problems.append(f"only {self.total_orders} orders < 150")
        return (len(problems) == 0), problems


def _param_hash(params: dict) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _month_index(ts: pd.Timestamp, base_month: pd.Timestamp) -> int:
    return (ts.year - base_month.year) * 12 + (ts.month - base_month.month) + 1


def _find_trend_at(m15_df: pd.DataFrame, m5_ts: int, trend_filter: TrendFilter, pair: str, cache: dict):
    """Return the TrendState as of the latest M15 candle fully closed before m5_ts
    (no look-ahead: an M15 candle with open ts=T only becomes visible once T+900s
    has passed). Cached per closed-M15-index for speed over a long backtest."""
    closed = m15_df[m15_df["ts"] + 900 <= m5_ts]
    if closed.empty:
        return None
    idx = closed.index[-1]
    if idx in cache:
        return cache[idx]
    window = m15_df.loc[:idx].tail(250)
    state = trend_filter.evaluate(window, pair)
    cache[idx] = state
    return state


def _simulate(pair: str, m5_df: pd.DataFrame, m15_df: pd.DataFrame, payout: float,
              expiry_minutes: int, initial_balance: float, risk_cfg: RiskConfig,
              sample_type: str, params: dict) -> BacktestReport:
    entry_signal = EntrySignal()
    trend_filter = TrendFilter()

    # bug-147 fix: these paths are backtest-only (never used by the live path, which
    # defaults to data/risk_state.json / data/equity_snapshots.json — see risk_manager.py),
    # but they ARE fixed per pair+sample_type and were never reset between separate
    # run_backtest() invocations, so a later run's RiskManager.__init__ silently loaded a
    # stale equity_baseline/day-boundary/etc. from a previous, unrelated run. Delete any
    # leftover state before constructing RiskManager so every _simulate() call starts from
    # a genuinely clean, in-memory-equivalent slate regardless of prior invocations.
    state_path = f"data/backtest_risk_{pair}_{sample_type}.json"
    snapshot_path = f"data/backtest_snapshots_{pair}_{sample_type}.json"
    for stale_path in (state_path, snapshot_path):
        try:
            os.remove(stale_path)
        except FileNotFoundError:
            pass

    risk = RiskManager(risk_cfg, state_path=state_path, snapshot_path=snapshot_path)

    m5 = IndicatorEngineV2.compute_m5(m5_df).reset_index(drop=True)
    m15_for_trend = m15_df.reset_index(drop=True)

    balance = initial_balance
    equity_curve = [balance]
    wins = losses = equals = 0
    monthly = {}   # month_key -> [wins, total]
    trend_cache: dict = {}
    base_month = pd.to_datetime(m5["ts"].iloc[0], unit="s") if len(m5) else pd.Timestamp.now()

    open_trade = None  # {"entry_price","direction","expiry_ts","stake"}
    warmup = 60

    for i in range(warmup, len(m5) - 1):  # -1 so a "next candle open" always exists
        row_ts = int(m5["ts"].iloc[i])
        now_th = datetime.fromtimestamp(row_ts, tz=timezone.utc)

        # resolve any trade whose expiry has passed
        if open_trade and row_ts >= open_trade["expiry_ts"]:
            close_price = float(m5["close"].iloc[i])
            entry_price = open_trade["entry_price"]
            if open_trade["direction"] == "CALL":
                result = "WIN" if close_price > entry_price else ("EQUAL" if close_price == entry_price else "LOSS")
            else:
                result = "WIN" if close_price < entry_price else ("EQUAL" if close_price == entry_price else "LOSS")
            stake = open_trade["stake"]
            if result == "WIN":
                pnl = round(stake * payout, 2)
                wins += 1
            elif result == "LOSS":
                pnl = -stake
                losses += 1
            else:
                pnl = 0.0
                equals += 1
            balance += pnl
            equity_curve.append(balance)
            risk.record_order_result(pnl, result, now_th)

            mkey = _month_index(pd.Timestamp(now_th), base_month)
            monthly.setdefault(mkey, [0, 0])
            monthly[mkey][1] += 1
            if result == "WIN":
                monthly[mkey][0] += 1
            open_trade = None

        if open_trade is not None:
            continue  # no-overlap — one open position at a time (spec §7)

        risk.roll_boundaries(now_th, balance)
        can, _reason = risk.can_trade(now_th, balance)
        if not can:
            continue

        trend = _find_trend_at(m15_for_trend, row_ts, trend_filter, pair, trend_cache)
        if trend is None or trend.status == "NO_TRADE":
            continue

        window = m5.loc[:i].tail(250)
        atr_hist = window["atr14"].tail(100)
        result = entry_signal.evaluate(pair, trend, window, atr_hist)
        if result.signal == "HOLD":
            continue

        # Slippage model: entry price = OPEN of the candle AFTER the signal candle
        entry_price = float(m5["open"].iloc[i + 1])
        stake = risk.stake_amount(balance)
        risk.record_order_placed()
        expiry_ts = row_ts + expiry_minutes * 60
        open_trade = {"entry_price": entry_price, "direction": result.signal,
                       "expiry_ts": expiry_ts, "stake": stake}

    total = wins + losses + equals
    decided = wins + losses
    winrate = round(wins / decided * 100, 2) if decided else 0.0
    monthly_winrate = {str(k): round(v[0] / v[1] * 100, 2) if v[1] else 0.0 for k, v in sorted(monthly.items())}

    peak = equity_curve[0]
    max_dd = 0.0
    for e in equity_curve:
        peak = max(peak, e)
        if peak > 0:
            max_dd = max(max_dd, (peak - e) / peak * 100)

    return BacktestReport(
        pair=pair, sample_type=sample_type, total_orders=total, wins=wins, losses=losses,
        equals=equals, winrate=winrate, monthly_winrate=monthly_winrate,
        max_drawdown_pct=round(max_dd, 2), final_balance=round(balance, 2),
        param_hash=_param_hash(params),
    )


def run_backtest(pair: str, m5_df: pd.DataFrame, m15_df: pd.DataFrame,
                  in_sample_months: tuple[int, int] = (1, 8),
                  out_sample_months: tuple[int, int] = (9, 12),
                  payout: float = DEFAULT_PAYOUT, params: dict | None = None,
                  expiry_minutes: int = DEFAULT_EXPIRY_MINUTES,
                  initial_balance: float = DEFAULT_INITIAL_BALANCE,
                  risk_cfg: RiskConfig | None = None,
                  trade_logger=None) -> tuple[BacktestReport, BacktestReport]:
    """m5_df/m15_df: full 12-month OHLCV history (ts, open, high, low, close, volume),
    already sorted ascending by ts. Split by calendar month index (1-12) counted from
    the first candle's month. Returns (in_sample_report, out_of_sample_report)."""
    params = params or {}
    risk_cfg = risk_cfg or RiskConfig()

    def _month_key(df):
        dt = pd.to_datetime(df["ts"], unit="s")
        abs_month = dt.dt.year * 12 + dt.dt.month
        return abs_month - abs_month.iloc[0] + 1

    m5_months = _month_key(m5_df)
    m15_months = _month_key(m15_df)

    in_m5 = m5_df[(m5_months >= in_sample_months[0]) & (m5_months <= in_sample_months[1])].reset_index(drop=True)
    in_m15 = m15_df[(m15_months >= in_sample_months[0]) & (m15_months <= in_sample_months[1])].reset_index(drop=True)
    out_m5 = m5_df[(m5_months >= out_sample_months[0]) & (m5_months <= out_sample_months[1])].reset_index(drop=True)
    out_m15 = m15_df[(m15_months >= out_sample_months[0]) & (m15_months <= out_sample_months[1])].reset_index(drop=True)

    in_report = _simulate(pair, in_m5, in_m15, payout, expiry_minutes, initial_balance,
                           risk_cfg, "in_sample", params)

    param_hash = _param_hash(params)
    if trade_logger and trade_logger.has_run_out_of_sample(param_hash):
        raise RuntimeError(
            f"Overfitting guard: param set {param_hash} has already been run against "
            "out-of-sample data once. Peet's rule: no re-tuning after seeing OOS results — "
            "change parameters only against in-sample, then run a FRESH full OOS pass."
        )

    out_report = _simulate(pair, out_m5, out_m15, payout, expiry_minutes, initial_balance,
                            risk_cfg, "out_of_sample", params)

    if trade_logger:
        ts = datetime.now(timezone.utc).isoformat()
        run_id = f"{pair}_{ts}"
        trade_logger.write_backtest_run(run_id, param_hash, "in_sample", ts, asdict(in_report))
        trade_logger.write_backtest_run(run_id, param_hash, "out_of_sample", ts, asdict(out_report))

    return in_report, out_report


# ── smoke-test data generator — NOT spec-compliant real market data ──
def generate_synthetic_ohlcv(months: int = 12, seed: int = 42) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Random-walk OHLCV purely so backtest.py's mechanics (parity, no-lookahead,
    slippage model, risk gating, report math) can be exercised end-to-end when real
    Dukascopy data isn't reachable (see data_downloader.py docstring). Results from
    this data are NOT valid for spec §10 pass/fail — clearly excluded from any
    "backtest passed" claim."""
    rng = np.random.default_rng(seed)
    n_days = months * 30
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    bars_per_day = 96  # M15 bars in a 24h day
    n_m15 = n_days * bars_per_day
    price = 1.10
    rows_m15 = []
    ts = start
    for _ in range(n_m15):
        drift = rng.normal(0, 0.0004)
        o = price
        c = price + drift
        h = max(o, c) + abs(rng.normal(0, 0.0002))
        l = min(o, c) - abs(rng.normal(0, 0.0002))
        rows_m15.append((int(ts.timestamp()), o, h, l, c, float(rng.integers(50, 500))))
        price = c
        ts += timedelta(minutes=15)
    m15 = pd.DataFrame(rows_m15, columns=["ts", "open", "high", "low", "close", "volume"])

    rows_m5 = []
    for _, r in m15.iterrows():
        sub_ts = r.ts
        sub_o = r.open
        for k in range(3):
            drift = rng.normal(0, 0.00015)
            sub_c = sub_o + drift if k < 2 else r.close
            sub_h = max(sub_o, sub_c) + abs(rng.normal(0, 0.0001))
            sub_l = min(sub_o, sub_c) - abs(rng.normal(0, 0.0001))
            rows_m5.append((int(sub_ts), sub_o, sub_h, sub_l, sub_c, float(rng.integers(20, 150))))
            sub_o = sub_c
            sub_ts += 300
    m5 = pd.DataFrame(rows_m5, columns=["ts", "open", "high", "low", "close", "volume"])
    return m5, m15


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print("No real historical data available in this environment (see data_downloader.py docstring).")
    print("Running a SYNTHETIC smoke test to verify the backtest pipeline executes end-to-end.")
    print("These numbers are NOT a spec §10 pass/fail result — synthetic data only.\n")
    m5, m15 = generate_synthetic_ohlcv(months=12)
    in_rep, out_rep = run_backtest("EURUSD", m5, m15)
    print("IN-SAMPLE:", json.dumps(asdict(in_rep), indent=2))
    print("OUT-OF-SAMPLE:", json.dumps(asdict(out_rep), indent=2))
    ok, problems = out_rep.passes_spec_gate()
    print(f"\nSpec §10 gate (informational, synthetic data): {'PASS' if ok else 'FAIL'}")
    if problems:
        for p in problems:
            print(" -", p)
