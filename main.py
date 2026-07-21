"""
Main launcher — integrates TelegramBot into the trading loop
Run: python main.py
"""

import asyncio
import html
import json
import logging
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from dataclasses import asdict

import pandas as pd
import websockets
from dotenv import load_dotenv
from pathlib import Path

# Load .env from project root (one level up from backend/) OR same dir
_here = Path(__file__).parent
_env_paths = [_here / ".env", _here.parent / ".env", Path(".env")]
for _p in _env_paths:
    if _p.exists():
        load_dotenv(dotenv_path=_p, override=True)
        print(f"[ENV] Loaded: {_p.resolve()}")
        break
else:
    print("[ENV] WARNING: .env file not found — using system environment variables")

from trading_engine import (
    TradingConfig, TradingBot, IndicatorEngine,
    SignalEngine, TradeManager, LearningEngine,
    ws_server, broadcast, state_store, connected_clients,
    _ORDER_UNAVAILABLE,
)
from telegram_bot import TelegramBot, TelegramConfig
from risk_manager import RiskConfig, RiskManager
from martingale import MartingaleModule, WARNING_TEXT as MARTINGALE_WARNING_TEXT

# Single source of truth for every TradingConfig field that must stay mirrored onto the
# spec_v1 RiskManager's RiskConfig (self._risk_v2.cfg). Used both at construction time
# (FullTradingBot.__init__) and for re-sync after every later apply_runtime_config() call
# (see FullTradingBot._sync_risk_v2_config). bug-16x: max_trades_per_day and
# max_consecutive_losses were missing from this list entirely, so RiskConfig's hardcoded
# dataclass defaults (5 / 3) silently governed spec_v1 regardless of what config.json said.
RISK_V2_SYNC_FIELDS = (
    "stake_pct",
    "max_trades_per_day",
    "max_consecutive_losses",
    "daily_loss_limit_pct",
    "weekly_loss_limit_pct",
    "signal_cooldown_minutes",
    "auto_stop_enabled",
    "auto_stop_drawdown_pct",
)

# ── spec_v1 live-wiring (San's Architecture Notes, outputs/10_san-iqoption-spec-v1-live-wiring.md) ──
from candle_store import CandleStore
from entry_signal import EntrySignal
from indicators_v2 import IndicatorEngineV2
from state_machine import BotStateMachine
from trade_logger import TradeLogger
from trend_filter import TrendFilter
from time_filter import TimeFilter

# Known tradable pairs for set_pairs validation (spec §12 multi-pair config — not
# hard-locked to EUR/USD, default selection only). Mirrors the FIAT whitelist logic
# in TradingBot.resolve_assets() (trading_engine.py) so the two lists can't drift —
# any 6-letter combination of these currencies plus the IQ "-op" suffix is accepted.
_FIAT = ("EUR", "USD", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF")


def _is_known_pair(name: str) -> bool:
    base = name[:-3] if name.endswith("-op") else name
    return len(base) == 6 and base[:3] in _FIAT and base[3:] in _FIAT

logger = logging.getLogger(__name__)

RUNTIME_CONFIG_PATH = Path("data/config.json")


def _h(v) -> str:
    """Escape a dynamic value for Telegram HTML parse_mode."""
    return html.escape(str(v)) if v is not None else ""


def _enforce_spec_v1_practice_gate(cfg: TradingConfig, tg: "TelegramBot | None" = None,
                                    trade_logger: "TradeLogger | None" = None) -> bool:
    """Hard safety gate (San's Architecture Notes §2) — reused verbatim at 3 call sites:
    apply_runtime_config() [startup + dashboard update_settings], the switch_account command
    handler, and the spec_v1 M5/M15 scheduler loop itself (defense-in-depth). If
    strategy_mode=="spec_v1" is ever paired with an account_type other than PRACTICE, force
    strategy_mode back to "legacy" — spec_v1 has not passed an out-of-sample backtest (spec
    §10) and must never trade real money. Never crashes, never silently passes through.
    Returns True if a forced fallback just happened (caller may want to alert)."""
    if cfg.strategy_mode == "spec_v1" and cfg.account_type != "PRACTICE":
        logger.warning(
            f"[SAFETY-GATE] strategy_mode=spec_v1 blocked on account_type={cfg.account_type} "
            "— spec_v1 logic ยังไม่ผ่าน out-of-sample backtest (spec §10), ห้ามเทรดเงินจริง. "
            "Forcing strategy_mode back to 'legacy'."
        )
        cfg.strategy_mode = "legacy"
        if trade_logger is not None:
            try:
                ts = datetime.now(ZoneInfo("Asia/Bangkok")).isoformat()
                trade_logger.write_system_event(ts, "SAFETY_GATE_TRIPPED", {
                    "account_type": cfg.account_type,
                    "forced_strategy_mode": "legacy",
                })
            except Exception as e:
                logger.warning(f"[SAFETY-GATE] write_system_event failed: {e}")
        if tg is not None:
            try:
                asyncio.create_task(tg.send(
                    "⚠️ <b>SAFETY GATE</b>\n\n"
                    "พยายามเปิด spec_v1 บนบัญชีที่ไม่ใช่ PRACTICE — ระบบบังคับกลับไป legacy อัตโนมัติ"
                ))
            except RuntimeError:
                pass  # no running event loop (e.g. called before asyncio.run) — log above still fired
        return True
    return False


def _enforce_trading_hours_experiment_practice_gate(
    cfg: TradingConfig, tg: "TelegramBot | None" = None,
    trade_logger: "TradeLogger | None" = None,
) -> bool:
    """Hard safety gate (2026-07-21, Psycho-approved 24h PRACTICE experiment) — mirrors
    _enforce_spec_v1_practice_gate() exactly, same defense-in-depth call sites (apply_runtime_config,
    switch_account, and the spec_v1 M5/M15 scheduler loop itself). If trading_hours_experiment
    is ever True while account_type != PRACTICE, force it back to False — this experiment must
    NEVER run on a real-money account. Never crashes, never silently passes through.
    Returns True if a forced fallback just happened (caller may want to alert)."""
    if cfg.trading_hours_experiment and cfg.account_type != "PRACTICE":
        logger.warning(
            f"[SAFETY-GATE] trading_hours_experiment=True blocked on account_type={cfg.account_type} "
            "— the 24h trading-hours experiment is PRACTICE-only (Psycho/Peet-approved 2026-07-21). "
            "Forcing trading_hours_experiment back to False."
        )
        cfg.trading_hours_experiment = False
        if trade_logger is not None:
            try:
                ts = datetime.now(ZoneInfo("Asia/Bangkok")).isoformat()
                trade_logger.write_system_event(ts, "SAFETY_GATE_TRIPPED", {
                    "account_type": cfg.account_type,
                    "forced_trading_hours_experiment": False,
                })
            except Exception as e:
                logger.warning(f"[SAFETY-GATE] write_system_event failed: {e}")
        if tg is not None:
            try:
                asyncio.create_task(tg.send(
                    "⚠️ <b>SAFETY GATE</b>\n\n"
                    "พยายามเปิด trading_hours_experiment บนบัญชีที่ไม่ใช่ PRACTICE — ระบบบังคับปิดอัตโนมัติ"
                ))
            except RuntimeError:
                pass  # no running event loop — log above still fired
        return True
    return False


def load_runtime_config() -> dict:
    try:
        with open(RUNTIME_CONFIG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# Settings that can be changed from the dashboard and persist across restarts.
# timeframe is intentionally env-only (IQ_TIMEFRAME) — not a runtime field — to keep
# the timeframe/expiry pair consistent. See item 8 note below.
# A/B candidate: M15 (IQ_TIMEFRAME=900) with expiry_minutes=30-45 (set in config.json).
RUNTIME_FIELDS = [
    "trade_amount", "confidence_threshold", "expiry_minutes", "max_open_positions",
    "martingale_enabled", "martingale_base", "martingale_multiplier", "martingale_max_steps",
    "max_trades_per_day", "max_consecutive_losses", "loss_cooldown_minutes",
    "daily_profit_target", "daily_loss_limit",
    "adx_min", "dir_margin",                         # quality gates (item 4)
    "rsi_call_min", "rsi_call_max",                  # RSI zones (item 5)
    "rsi_put_min", "rsi_put_max",
    # ── spec-overhaul fields (San's Architecture Notes §12) ──
    "strategy_mode", "stake_pct", "weekly_loss_limit_pct", "daily_loss_limit_pct",
    "signal_cooldown_minutes", "auto_stop_enabled", "auto_stop_drawdown_pct",
    "martingale_ack_risk", "enabled_pairs", "default_pair",
    # ── 24h PRACTICE-only trading-hours experiment (2026-07-21, Psycho-approved) ──
    "trading_hours_experiment",
]


# Safety bounds — config.json (or a dashboard `update_settings` edit) can never silently
# push a safety-critical field weaker than these. "min" bounds guard fields where a LOWER
# value is less safe (adx_min, expiry_minutes, loss_cooldown_minutes); "max" bounds guard
# fields where a HIGHER value is less safe (max_consecutive_losses, daily_loss_limit).
# Root cause this prevents (see .wolf/buglog.json history): TradingConfig dataclass defaults
# were already safe, but config.json silently overrode them with no validation, weakening the
# ADX gate below the intended floor. Applied inside apply_runtime_config() so both call sites
# (startup load + dashboard update_settings) are covered by one fix.
SAFETY_BOUNDS = {
    "adx_min":                ("min", 20.0),
    "expiry_minutes":         ("min", 5),
    "loss_cooldown_minutes":  ("min", 10),
    "max_consecutive_losses": ("max", 5),
    # daily_loss_limit ceiling temporarily raised 300.0 -> 9999.0 per Peet's explicit
    # instruction (2026-07-13) to stop config.json's 900 from being silently clamped to
    # 300 while demo-account testing. This is a deliberate, temporary unlock — NOT the
    # recommended default for live/real-money trading. Lower this back down (e.g. ~300-500)
    # before switching this bot to a REAL account.
    "daily_loss_limit":       ("max", 9999.0),
}


def _clamp_safety_bounds(cfg: TradingConfig):
    """Clamp any RUNTIME_FIELDS value that has drifted past its approved safety bound."""
    for field, (kind, bound) in SAFETY_BOUNDS.items():
        cur = getattr(cfg, field, None)
        if cur is None:
            continue
        if kind == "min" and cur < bound:
            logger.warning(f"[CONFIG] {field}={cur} below safety floor {bound} — clamped to {bound}")
            setattr(cfg, field, bound)
        elif kind == "max" and cur > bound:
            logger.warning(f"[CONFIG] {field}={cur} above safety ceiling {bound} — clamped to {bound}")
            setattr(cfg, field, bound)


def save_runtime_config(cfg: TradingConfig):
    os.makedirs("data", exist_ok=True)
    with open(RUNTIME_CONFIG_PATH, "w") as f:
        json.dump({k: getattr(cfg, k) for k in RUNTIME_FIELDS}, f, indent=2)


def apply_runtime_config(cfg: TradingConfig, rt: dict, tg: "TelegramBot | None" = None,
                          trade_logger: "TradeLogger | None" = None):
    """Apply persisted/dashboard settings onto cfg, coercing to the field's type.
    Martingale OFF (default): max_consecutive_losses is left as-is so the configured
    cooldown fires independently of the (disabled) martingale ladder. A final safety-bounds
    clamp always runs at the end regardless of martingale on/off (see SAFETY_BOUNDS).
    tg/trade_logger are optional — passed through so the spec_v1 PRACTICE-only safety gate
    (§2, call site 1/3) can alert + audit-log a forced fallback. At the very first startup
    call (main(), before TelegramBot/TradeLogger exist) both are None — the gate still forces
    the fallback and logs a warning, it just can't alert/audit-log that one instant."""
    for k in RUNTIME_FIELDS:
        if k not in rt:
            continue
        cur = getattr(cfg, k, None)
        if cur is None:
            continue
        try:
            val = bool(rt[k]) if isinstance(cur, bool) else type(cur)(rt[k])
        except (TypeError, ValueError):
            continue
        setattr(cfg, k, val)
    # When martingale is ON: keep base stake in sync and ensure the consecutive-loss
    # pause only fires after the full ladder has been exhausted.
    # When OFF: do NOT touch max_consecutive_losses — let the configured value (e.g. 4) stand.
    if cfg.martingale_enabled:
        cfg.trade_amount = cfg.martingale_base
        cfg.max_consecutive_losses = max(cfg.max_consecutive_losses, cfg.martingale_max_steps)
        # Global single Martingale ladder — only 1 auto trade at a time
        cfg.max_open_positions = 1
    # ADR-4 — server-side enforcement: martingale_enabled=True is only ever honored
    # together with martingale_ack_risk=True in the same payload. A partial/corrupted
    # config (e.g. only martingale_enabled survives a bad write) can never silently
    # activate the ladder — this check runs on every apply, not just at toggle time.
    ok, _reason = MartingaleModule.validate_toggle(cfg.martingale_enabled, cfg.martingale_ack_risk)
    if not ok:
        logger.warning(f"[CONFIG] martingale_enabled rejected — martingale_ack_risk missing: {_reason}")
        cfg.martingale_enabled = False
    # auto_stop_drawdown_pct is two-sided clamped (5-50%), unlike the single-direction
    # SAFETY_BOUNDS entries below.
    cfg.auto_stop_drawdown_pct = max(5.0, min(50.0, cfg.auto_stop_drawdown_pct))
    # Final safety net — clamp regardless of martingale on/off, and regardless of which
    # call site (startup or dashboard update_settings) invoked this function.
    _clamp_safety_bounds(cfg)
    # spec_v1 PRACTICE-only hard safety gate (§2, call site 1/3) — runs on every apply,
    # covering both the startup load and every dashboard update_settings edit.
    _enforce_spec_v1_practice_gate(cfg, tg, trade_logger)
    # 24h trading-hours experiment PRACTICE-only hard safety gate (call site 1/3) — same
    # coverage as above.
    _enforce_trading_hours_experiment_practice_gate(cfg, tg, trade_logger)


# ─────────────────────────────────────────────────
#  ENHANCED BOT WITH TELEGRAM
# ─────────────────────────────────────────────────
class FullTradingBot(TradingBot):

    def __init__(self, cfg: TradingConfig, tg: TelegramBot):
        super().__init__(cfg)
        self.tg = tg
        self._cmd_queue: asyncio.Queue = asyncio.Queue()
        self._paused = False
        self._cooldown_until = 0.0  # epoch secs; > now = in loss-cooldown (auto-resumes, NOT a hard pause)
        self._iq_lock = asyncio.Lock()  # serialize IQ network calls between run_cycle and the sync loop
        self._alerted_rule_ids: set = set()  # track rule id ที่แจ้ง Telegram ไปแล้ว — ป้องกันแจ้งซ้ำทุกรอบ
        self._weekend_halt = False   # True = TH calendar Sat/Sun — proactive clock-based gate, distinct from _paused/_cooldown_until
        # spec_v1 RiskManager — tracks counters in parallel per San's §13.1 contract so
        # Pixel/Iris can build/test against the real shape now. Does NOT gate the live
        # legacy trading path (see build_risk_v2 docstring / strategy_mode rationale).
        self._risk_v2 = RiskManager(RiskConfig(
            **{f: getattr(cfg, f) for f in RISK_V2_SYNC_FIELDS}
        ))

        # ── spec_v1 live-wiring (San's Architecture Notes §3/§4/§6/§7/§8) ──
        # self._risk_v2 above is now the SAME RiskManager instance handed to BotStateMachine
        # (§3) — no second instance is ever created, so counters (open_positions/trades_today/
        # consecutive_losses) can never diverge from what the risk_v2 dashboard panel shows.
        self._risk_v2.on_event = self._spec_v1_on_event   # §7 KILL/AUTO_STOP wiring
        self._trade_logger = TradeLogger()                 # §6 SQLite system-of-record
        self._candle_stores: dict[str, CandleStore] = {}    # §4.1 one CandleStore per resolved asset
        self._trend_filter_v1 = TrendFilter()
        self._entry_signal_v1 = EntrySignal()
        # trading_hours_experiment seeded from cfg at construction time — apply_runtime_config()
        # (config.json load) already ran before FullTradingBot.__init__ in main(), so cfg already
        # reflects the persisted flag here. Re-synced on every later edit — see
        # _sync_time_filter_v1_config().
        self._time_filter_v1 = TimeFilter(trading_hours_experiment=cfg.trading_hours_experiment)
        self._state_machine_v1: BotStateMachine | None = None
        self._spec_v1_was_active = False   # edge-trigger flag for _sync_state_machine_v1 (§8)

    def _sync_risk_v2_config(self):
        """Re-mirror RISK_V2_SYNC_FIELDS from self.cfg onto self._risk_v2.cfg.
        self._risk_v2.cfg is a SEPARATE RiskConfig object built once in __init__ — apply_runtime_config()
        only ever mutates self.cfg (the TradingConfig instance), so without this explicit re-sync any
        later config reload/dashboard edit (update_settings) would silently leave risk_v2 running on
        stale values (e.g. config.json's max_trades_per_day=50 never reaching spec_v1's can_trade()
        gate, which kept firing off the RiskConfig dataclass default of 5). Call this immediately after
        every apply_runtime_config(self.cfg, ...) call that happens post-__init__."""
        for f in RISK_V2_SYNC_FIELDS:
            setattr(self._risk_v2.cfg, f, getattr(self.cfg, f))

    def _sync_time_filter_v1_config(self):
        """Re-mirror trading_hours_experiment from self.cfg onto self._time_filter_v1.
        self._time_filter_v1 is a SEPARATE TimeFilter instance built once in __init__ (seeded
        from cfg at construction time) — without this explicit re-sync a later config
        reload/dashboard edit would silently leave the live TimeFilter running on a stale flag
        value. Cheap (single attribute set) — safe to call unconditionally on every scheduler
        tick as well as after every apply_runtime_config() call."""
        self._time_filter_v1.trading_hours_experiment = self.cfg.trading_hours_experiment

    def log_activity(self, icon: str, msg: str, phase: str = "", level: str = "info"):
        """Record what the bot is doing → shown live on the dashboard 'กิจกรรมบอท' feed.
        Sets a current one-liner (heartbeat) and appends to a rolling timeline."""
        entry = {"t": datetime.now().isoformat(), "icon": icon, "msg": msg, "level": level}
        state_store["activity"] = {**entry, "phase": phase or level}
        log = state_store.setdefault("activity_log", [])
        log.append(entry)
        del log[:-60]  # keep last 60
        logger.info(f"[ACTIVITY] {icon} {msg}")

    def build_risk(self) -> dict:
        tm = self.trade_manager
        today_pnl = tm.today_pnl() if tm else 0
        return {
            "open": len(tm.active_orders) if tm else 0,
            "max_open": self.cfg.max_open_positions,
            "today_trades": len(tm.today_trades()) if tm else 0,
            "max_day_trades": self.cfg.max_trades_per_day,
            "consec_losses": tm.consecutive_losses if tm else 0,
            "max_consec": self.cfg.max_consecutive_losses,
            "daily_pnl": today_pnl,
            "daily_loss": round(max(0, -today_pnl), 2),
            "daily_target": self.cfg.daily_profit_target,
        }

    def build_config(self) -> dict:
        return {
            "trade_amount": self.cfg.trade_amount,
            "confidence_threshold": self.cfg.confidence_threshold,
            "timeframe": self.cfg.timeframe,
            "expiry_minutes": self.cfg.expiry_minutes,
            "assets": self.cfg.assets,
            "account_type": self.cfg.account_type,
            "martingale_enabled": self.cfg.martingale_enabled,
            "martingale_base": self.cfg.martingale_base,
            "martingale_multiplier": self.cfg.martingale_multiplier,
            "martingale_max_steps": self.cfg.martingale_max_steps,
            "martingale_sequence": [round(self.cfg.martingale_base * (self.cfg.martingale_multiplier ** i), 2)
                                     for i in range(self.cfg.martingale_max_steps)],
            "max_trades_per_day": self.cfg.max_trades_per_day,
            "max_consecutive_losses": self.cfg.max_consecutive_losses,
            "loss_cooldown_minutes": self.cfg.loss_cooldown_minutes,
            "max_open_positions": self.cfg.max_open_positions,
            "daily_profit_target": self.cfg.daily_profit_target,
            "daily_loss_limit": self.cfg.daily_loss_limit,
            # Quality gates — dashboard can read/override these (items 4 & 5)
            "adx_min": self.cfg.adx_min,
            "dir_margin": self.cfg.dir_margin,
            "rsi_call_min": self.cfg.rsi_call_min,
            "rsi_call_max": self.cfg.rsi_call_max,
            "rsi_put_min": self.cfg.rsi_put_min,
            "rsi_put_max": self.cfg.rsi_put_max,
            # ── spec-overhaul fields (San's Architecture Notes §13.1) ──
            "strategy_mode": self.cfg.strategy_mode,
            "enabled_pairs": self.cfg.enabled_pairs,
            "default_pair": self.cfg.default_pair,
            "stake_pct": self.cfg.stake_pct,
            "auto_stop_enabled": self.cfg.auto_stop_enabled,
            "auto_stop_drawdown_pct": self.cfg.auto_stop_drawdown_pct,
            "martingale_ack_risk": self.cfg.martingale_ack_risk,
        }

    def build_risk_v2(self) -> dict:
        """Exact §13.1 `risk` shape, computed from the spec_v1 RiskManager (self._risk_v2).
        Tracks real counters in parallel with the live legacy engine so Pixel/Iris can
        build/test against this contract now — it does NOT gate the legacy trading path
        (cfg.strategy_mode == "legacy" is still what actually places live orders; see the
        strategy_mode field default rationale in trading_engine.TradingConfig)."""
        if not self._risk_v2:
            return {}
        return self._risk_v2.to_state_dict(balance=state_store.get("balance"))

    def build_state_machine_v2(self) -> dict:
        """§13.1 `state_machine` + `global_state`. When strategy_mode=="spec_v1" and a
        BotStateMachine instance is actually driving live orders, source the real per-asset
        state/global_state from it (§9 — same WS contract shape Pixel already built against,
        just backed by real data now). Otherwise fall back to the original fabricated-IDLE
        text so the dashboard still reads sensibly while spec_v1 is off."""
        if self.cfg.strategy_mode == "spec_v1" and self._state_machine_v1 is not None:
            return self._state_machine_v1.to_state_dict()
        assets = self.cfg.enabled_pairs or [self.cfg.default_pair]
        reason = ("spec_v1 modules built + unit-tested, not driving live orders right now — "
                   "either strategy_mode is 'legacy', or awaiting the safety gate/backtest gate")
        return {
            "state_machine": {a: {"state": "IDLE", "since": datetime.now().isoformat(), "reason": reason}
                               for a in assets},
            "global_state": "RUNNING",
        }

    # ── spec_v1 live-wiring helpers (San's Architecture Notes §3-§8) ──
    def _spec_v1_on_event(self, event_type: str, detail: dict):
        """Single sink for both RiskManager.on_event and BotStateMachine.on_event (§7).
        Every event is audit-logged to SQLite system_events; KILL/AUTO_STOP additionally
        fire a Telegram alert so a rollback-triggering event is never silent."""
        ts = datetime.now(ZoneInfo("Asia/Bangkok")).isoformat()
        try:
            self._trade_logger.write_system_event(ts, event_type, detail)
        except Exception as e:
            logger.warning(f"[SPEC_V1] write_system_event failed: {e}")
        if event_type == "KILL":
            asyncio.create_task(self.tg.send(
                "🛑 <b>spec_v1 KILLED</b>\n\n"
                f"เหตุผล: {_h(detail.get('reason'))}\n"
                "spec_v1 หยุดเปิดออเดอร์ใหม่ทั้งหมดแล้ว (demo) — ต้องสลับ strategy_mode หรือรีสตาร์ทเอง "
                "จาก dashboard/Telegram ก่อนจะกลับมาเทรดต่อ"
            ))
        elif event_type == "AUTO_STOP_TRIGGERED":
            asyncio.create_task(self.tg.send(
                "⚠️ <b>spec_v1 AUTO-STOP triggered</b>\n\n"
                f"equity ลดเกิน threshold ({detail.get('drawdown_pct')}%) — ต้องกด reset baseline จาก dashboard"
            ))

    def _ensure_candle_stores(self):
        """§4.1 — one CandleStore per resolved real-forex asset (self.cfg.assets, NOT the
        raw enabled_pairs list). Bootstraps (200 closed candles, once) only for assets that
        don't already have a store — an asset that temporarily disappears and comes back
        keeps its history instead of re-bootstrapping from scratch."""
        for asset in (self.cfg.assets or []):
            if asset in self._candle_stores:
                continue
            store = CandleStore(asset)
            try:
                store.bootstrap(self.iq)
            except Exception as e:
                logger.warning(f"[SPEC_V1] bootstrap candle store failed for {asset}: {e}")
            self._candle_stores[asset] = store

    async def _spec_v1_append_new_candle(self, asset: str, tf: str):
        """§4.2 — fetch only the last 2 candles (never re-fetch 200/cycle), normalize (drops
        the still-forming candle — candle_store.py's single no-repaint choke point), then
        append the newest CLOSED candle if it's genuinely new (dedup guard in append_if_new)."""
        store = self._candle_stores.get(asset)
        if not store:
            return
        seconds = 900 if tf == "m15" else 300
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(self.iq.get_candles, asset, seconds, 2, time.time()), timeout=15)
        except Exception as e:
            logger.warning(f"[SPEC_V1] get_candles({asset}, {tf}) failed: {e}")
            return
        candles = CandleStore._normalize_iq_candles(raw or [], drop_forming=True)
        if not candles:
            return
        store.append_if_new(tf, candles[-1])

    def _spec_v1_place_order(self, asset: str, direction: str, stake: float):
        """§4.3 — thin wrapper around TradeManager._place_order() (no new broker-call code).
        source="spec_v1" (NOT "auto") so legacy's _auto_locked_until expiry-hard-lock and
        _apply_close()'s auto-only consecutive-loss counter are never touched by spec_v1 —
        RiskManager.can_trade()'s own no-overlap gate (_open_positions>0) is spec_v1's
        equivalent, already wired via the shared self._risk_v2 instance (§3).
        Snapshots the M5/M15 indicator values used for THIS entry (independently, from the
        same CandleStore the state machine just evaluated — no state_machine.py contract
        change needed) so §6's SQLite schema (ema20_m15/ema50_m15/ema20_m5/rsi_m5/atr_m5)
        is populated without re-deriving state_machine's internal decision path."""
        # session: which time-window bucket this ENTER fell into (2026-07-21 24h experiment
        # ticket) — lets Peet/Psycho later compare win rate by session and see whether
        # off-hours (experiment_extended_hours) trades are consuming the max_trades_per_day
        # budget before the core london_ny_window sessions arrive ("budget cannibalization").
        meta = {
            "source": "spec_v1", "confidence": None, "balance_before": state_store.get("balance"),
            "session": self._time_filter_v1.session_tag(),
        }
        trend = self._state_machine_v1.trend_states.get(asset) if self._state_machine_v1 else None
        if trend is not None:
            meta["ema20_m15"] = trend.ema20
            meta["ema50_m15"] = trend.ema50
        try:
            store = self._candle_stores.get(asset)
            if store is not None:
                df5 = IndicatorEngineV2.compute_m5(store.m5_df())
                last = df5.iloc[-1]
                meta["ema20_m5"] = None if pd.isna(last.ema20) else float(last.ema20)
                meta["rsi_m5"] = None if pd.isna(last.rsi14) else float(last.rsi14)
                meta["atr_m5"] = None if pd.isna(last.atr14) else float(last.atr14)
        except Exception as e:
            logger.warning(f"[SPEC_V1] failed to snapshot M5 indicators for {asset}: {e}")
        return self.trade_manager._place_order(asset, direction, stake, meta)

    def _spec_v1_log_trade_to_sqlite(self, t: dict):
        """§6 — the SQLite system-of-record write (trades.json already gets this trade for
        free via _place_order()). One row per trade, written once at close time when the
        full entry+exit picture is known."""
        try:
            self._trade_logger.write_trade({
                "order_id": t.get("id"),
                "timestamp": t.get("open_time"),
                "pair": t.get("asset"),
                "direction": t.get("direction"),
                "stake": t.get("amount"),
                "entry_price": t.get("entry"),
                "expiry_price": None,
                "result": t.get("result"),
                "pnl": t.get("pnl"),
                "ema20_m15": t.get("ema20_m15"),
                "ema50_m15": t.get("ema50_m15"),
                "ema20_m5": t.get("ema20_m5"),
                "rsi_m5": t.get("rsi_m5"),
                "atr_m5": t.get("atr_m5"),
                "pattern_type": t.get("pattern_type"),
                "trend_status": t.get("trend_status"),
                "latency_ms": t.get("latency_ms"),
                "balance_before": t.get("balance_before"),
                "balance_after": state_store.get("balance"),
                "source": t.get("source"),
                "session": t.get("session"),  # 2026-07-21 24h experiment: london_ny_window | experiment_extended_hours
                "martingale_step": None,   # spec_v1 does not use Martingale (§4.3)
                "state_trace": None,
            })
        except Exception as e:
            logger.warning(f"[SPEC_V1] write_trade to SQLite failed: {e}")

    def _sync_state_machine_v1(self):
        """§8 rollback wiring — (Re)creates a fresh BotStateMachine exactly when
        strategy_mode transitions INTO "spec_v1" from something else, never mid-run. This is
        what makes strategy_mode's existing live RUNTIME_FIELDS switch a real, no-restart
        rollback: switching legacy -> spec_v1 -> legacy -> spec_v1 always starts the new
        spec_v1 run with error_streak=0 / global_state=RUNNING, never a stale KILLED state
        carried over from a previous activation."""
        want_active = (self.cfg.strategy_mode == "spec_v1")
        if want_active and not self._spec_v1_was_active:
            self._state_machine_v1 = BotStateMachine(
                assets=self.cfg.assets or [], candle_stores=self._candle_stores,
                trend_filter=self._trend_filter_v1, entry_signal=self._entry_signal_v1,
                time_filter=self._time_filter_v1, risk_manager=self._risk_v2,
                place_order_fn=self._spec_v1_place_order,
                get_balance_fn=lambda: state_store.get("balance"),
                martingale=None,  # opt-in, not spec_v1's default (§4.3) — separate ticket
                on_event=self._spec_v1_on_event,
            )
            logger.info("[SPEC_V1] BotStateMachine (re)created — fresh state, error_streak=0")
        self._spec_v1_was_active = want_active

    async def spec_v1_m5_loop(self):
        """§4.2 M5 close scheduler — independent of legacy's main_loop(). Always running
        (checks strategy_mode/gate every tick, never gated at process-start) so switching
        spec_v1 <-> legacy is live, no restart (§8)."""
        while True:
            now = time.time()
            wait = 300 - (now % 300) + 8
            await asyncio.sleep(wait)
            if self.cfg.strategy_mode != "spec_v1":
                continue
            if _enforce_spec_v1_practice_gate(self.cfg, self.tg, self._trade_logger):  # §2 site 3/3
                continue
            # 24h trading-hours experiment gate (call site 3/3) + mirror the live flag onto the
            # shared TimeFilter instance every tick, so a dashboard edit takes effect immediately.
            _enforce_trading_hours_experiment_practice_gate(self.cfg, self.tg, self._trade_logger)
            self._sync_time_filter_v1_config()
            self._sync_state_machine_v1()
            if not self._state_machine_v1 or not self.cfg.assets:
                # bug-160: this was a completely silent tick — spec_v1 active but the
                # whole scheduler tick was skipped above state_machine.py, invisible even
                # with the new [SPEC_V1_SIGNAL] logging (that lives one level down, per
                # asset, inside on_m5_close()). Log it so a stuck/never-created
                # BotStateMachine or an empty resolved-assets list is never mistaken for
                # "evaluating candles but finding no signal".
                logger.warning(
                    f"[SPEC_V1_SIGNAL] tick skipped — state_machine_v1={'set' if self._state_machine_v1 else 'None'}, "
                    f"assets={self.cfg.assets or []}"
                )
                continue
            # bug-181: _ensure_candle_stores() was previously ONLY wired into the legacy
            # scan cycle (run_cycle(), line ~678) and startup (main_loop(), line ~1361).
            # spec_v1 must not depend on the legacy loop's side effect to seed its own
            # candle stores (e.g. if run_cycle() ever returns early — paused, weekend
            # halt, reconnecting — before reaching line 678, spec_v1's stores never get
            # created/refreshed even though its own scheduler keeps ticking). Seed here,
            # every tick, before the per-asset loop — idempotent (skips assets that
            # already have a store, §4.1), so this is safe to call unconditionally.
            self._ensure_candle_stores()
            for asset in self.cfg.assets:
                await self._spec_v1_append_new_candle(asset, "m5")
                # §2 site 3/3 (bug-155 fix): the `await` above is a real asyncio interleave
                # point — a concurrent switch_account/apply_runtime_config call can flip
                # strategy_mode/account_type *during* this await. Recheck BEFORE EVERY asset's
                # broker-facing on_m5_close() call, not just once per tick. Two conditions must
                # both be re-verified, not just the gate call's return value: (a) the gate may
                # trip right now (still spec_v1, but account flipped to non-PRACTICE), OR (b) a
                # PRIOR interleaved call (e.g. from switch_account's own handler) may have already
                # tripped the gate and self-healed strategy_mode to "legacy" — in that case calling
                # the gate again returns False (its condition requires strategy_mode=="spec_v1"),
                # so strategy_mode must be rechecked independently or this second case is missed.
                # If it trips mid-loop, abandon the REST of this tick's assets too — a tripped gate
                # means the whole system must fall back to legacy right now, not skip-and-continue
                # per asset.
                if (self.cfg.strategy_mode != "spec_v1"
                        or _enforce_spec_v1_practice_gate(self.cfg, self.tg, self._trade_logger)):
                    logger.warning(
                        f"[SPEC_V1] safety gate/mode check failed mid-tick before "
                        f"on_m5_close({asset}) — aborting remaining assets in this tick."
                    )
                    break
                try:
                    self._state_machine_v1.on_m5_close(asset)
                except Exception as e:
                    logger.error(f"[SPEC_V1] on_m5_close({asset}) error: {e}", exc_info=True)
                    self._state_machine_v1.record_process_error(str(e))
            state_store["risk_v2"] = self.build_risk_v2()
            state_store.update(self.build_state_machine_v2())
            await broadcast({"type": "update", "data": {
                "risk_v2": state_store.get("risk_v2"),
                "state_machine": state_store.get("state_machine"),
                "global_state": state_store.get("global_state"),
            }})

    async def spec_v1_m15_loop(self):
        """§4.2 M15 trend-refresh scheduler — lighter than the M5 loop, only updates
        TrendState per asset (does not place orders directly)."""
        while True:
            now = time.time()
            wait = 900 - (now % 900) + 8
            await asyncio.sleep(wait)
            if self.cfg.strategy_mode != "spec_v1":
                continue
            if _enforce_spec_v1_practice_gate(self.cfg, self.tg, self._trade_logger):  # §2 site 3/3
                continue
            # 24h trading-hours experiment gate (call site 3/3) + mirror the live flag onto the
            # shared TimeFilter instance every tick — see spec_v1_m5_loop's identical comment.
            _enforce_trading_hours_experiment_practice_gate(self.cfg, self.tg, self._trade_logger)
            self._sync_time_filter_v1_config()
            self._sync_state_machine_v1()
            if not self._state_machine_v1 or not self.cfg.assets:
                # bug-160: same silent-tick gap as spec_v1_m5_loop — see its comment.
                logger.warning(
                    f"[SPEC_V1_SIGNAL] M15 tick skipped — state_machine_v1="
                    f"{'set' if self._state_machine_v1 else 'None'}, assets={self.cfg.assets or []}"
                )
                continue
            # bug-181: mirror spec_v1_m5_loop's seeding — do NOT rely on the M5 loop
            # having ticked first (idempotent, shares the same self._candle_stores dict).
            self._ensure_candle_stores()
            for asset in self.cfg.assets:
                await self._spec_v1_append_new_candle(asset, "m15")
                # §2 site 3/3 (bug-155 fix): same interleave hazard as spec_v1_m5_loop — recheck
                # BOTH strategy_mode and the gate's own return value before every asset's
                # on_m15_close() (a prior interleaved trip self-heals strategy_mode to "legacy",
                # which makes a second gate call alone return False — see spec_v1_m5_loop's
                # comment for the full reasoning), and abort the rest of the tick's assets (not
                # just this one) if either check fails.
                if (self.cfg.strategy_mode != "spec_v1"
                        or _enforce_spec_v1_practice_gate(self.cfg, self.tg, self._trade_logger)):
                    logger.warning(
                        f"[SPEC_V1] safety gate/mode check failed mid-tick before on_m15_close({asset}) "
                        "— aborting remaining assets in this tick."
                    )
                    break
                try:
                    self._state_machine_v1.on_m15_close(asset)
                except Exception as e:
                    logger.error(f"[SPEC_V1] on_m15_close({asset}) error: {e}", exc_info=True)

    def build_martingale_warning(self) -> str | None:
        if self.cfg.martingale_enabled and self.cfg.martingale_ack_risk:
            return MARTINGALE_WARNING_TEXT
        return None

    def _compute_weekend_halt(self) -> bool:
        """TH-time clock check — pure function, no I/O. Sat/Sun local to Asia/Bangkok."""
        now_th = datetime.now(ZoneInfo("Asia/Bangkok"))
        return now_th.weekday() in (5, 6)  # 5=Sat, 6=Sun

    def _handle_weekend_transition(self, is_weekend: bool):
        """Fire exactly one Telegram alert per Fri→Sat and Sun→Mon transition."""
        was_weekend = self._weekend_halt
        if is_weekend and not was_weekend:
            asyncio.create_task(self.tg.alert_weekend_closed())
        elif was_weekend and not is_weekend:
            asyncio.create_task(self.tg.alert_weekend_resumed())
        self._weekend_halt = is_weekend

    # ── Override run_cycle to send Telegram alerts ──
    async def run_cycle(self):
        # ── Weekend halt gate (TH time) — cheapest check, runs first, no network ──
        is_weekend = self._compute_weekend_halt()          # pure clock check
        self._handle_weekend_transition(is_weekend)          # edge-trigger alert only
        if is_weekend:
            state_store["status"] = "weekend_halt"
            self.log_activity("🌙", "สุดสัปดาห์ (เสาร์-อาทิตย์ เวลาไทย) — บอทหยุดพักอัตโนมัติ ไม่เช็คตลาด", phase="weekend_halt")
            await broadcast({"type": "update", "data": {
                "status": "weekend_halt",
                "activity": state_store["activity"],
                "activity_log": state_store["activity_log"],
            }})
            return

        if self._paused:
            state_store["status"] = "paused"
            await broadcast({"type": "update", "data": state_store})
            return

        self.loop_count += 1
        signals_this_cycle = []

        # Make sure the IQ socket is alive — reconnect if it dropped (otherwise the loop stalls)
        try:
            connected = await asyncio.wait_for(asyncio.to_thread(self.ensure_connected), timeout=45)
        except Exception:
            connected = False
        if not connected:
            self.log_activity("⚠️", "IQ หลุดการเชื่อมต่อ — กำลังต่อใหม่ จะลองอีกครั้งรอบถัดไป", level="error", phase="connecting")
            state_store["status"] = "reconnecting"
            self._need_resolve = True
            await broadcast({"type": "update", "data": {
                "status": "reconnecting",
                "activity": state_store["activity"],
                "activity_log": state_store["activity_log"],
            }})
            return

        # Resolve tradable real-forex pairs (ranked by payout). Re-check until we have some,
        # then hourly / on demand.
        if not self._assets_resolved or self.loop_count % 12 == 1 or getattr(self, "_need_resolve", False):
            try:
                await asyncio.wait_for(asyncio.to_thread(self.resolve_assets), timeout=60)
                self._need_resolve = False
                self._ensure_candle_stores()  # §4.1 — keep spec_v1's per-asset stores in sync
            except Exception as e:
                logger.warning(f"[ASSET] resolve failed: {e}")

        # No real forex pairs open (weekend / outside market hours) — wait, never trade OTC
        if not self.cfg.assets:
            self.log_activity("💤", "ตลาดคู่เงินจริงปิดอยู่ (สุดสัปดาห์/นอกเวลาทำการ) — รอตลาดเปิด · ไม่เทรด OTC", phase="waiting")
            state_store.update({"status": "running", "signals": [],
                                "risk": self.build_risk(), "stats": self.trade_manager.get_stats()})
            await broadcast({"type": "update", "data": {
                "status": "running", "signals": [], "risk": state_store["risk"],
                "stats": state_store["stats"], "balance": state_store["balance"],
                "activity": state_store["activity"], "activity_log": state_store["activity_log"],
            }})
            return

        self.log_activity("🔍", f"กำลังสแกน {len(self.cfg.assets)} คู่ (รอบที่ {self.loop_count})", phase="scanning")

        try:
            balance = await asyncio.wait_for(asyncio.to_thread(self.iq.get_balance), timeout=15)
        except Exception:
            balance = state_store.get("balance", 0)
        state_store["balance"] = round(balance, 2)

        candidates = []  # qualifying CALL/PUT signals this cycle
        got_data = False
        for asset in self.cfg.assets:
            try:
                df = await asyncio.wait_for(asyncio.to_thread(self.get_candles, asset), timeout=30)
            except Exception as e:
                logger.warning(f"[DATA] {asset}: candle fetch failed/timeout: {e}")
                continue
            if df is None or len(df) < 60:
                logger.warning(f"[DATA] {asset}: not enough candles")
                signals_this_cycle.append({
                    "asset": asset, "timeframe": self.cfg.timeframe,
                    "signal": "HOLD", "confidence": 0,
                    "score_breakdown": {}, "reasons": ["ข้อมูลแท่งเทียนไม่พอ"],
                    "entry_price": 0, "rsi": None, "atr": None,
                    "ema_fast": None, "ema_slow": None, "ema_trend": None,
                    "adx": None, "macd_hist": None,
                    "timestamp": datetime.now().isoformat(),
                })
                continue

            got_data = True
            df = self.indicator_engine.compute_all(df, self.cfg)
            signal = self.signal_engine.evaluate(df, asset)
            signals_this_cycle.append(asdict(signal))

            qualifies = signal.signal in ("CALL", "PUT") and signal.confidence >= self.cfg.confidence_threshold
            decision = "เข้าเงื่อนไข" if qualifies else "ไม่เข้า"
            top_reason = (signal.reasons or ["-"])[0]
            logger.info(
                f"[SIGNAL] {asset}: {signal.signal:4s} | Conf: {signal.confidence:5.1f}% | "
                f"RSI: {signal.rsi:5.1f} | ATR: {signal.atr:.5f} | "
                f"{decision} — {top_reason}"
            )

            if qualifies:
                candidates.append(signal)

            # Store candles for chart
            cols = ["open", "high", "low", "close", "volume",
                    "ema_fast", "ema_slow", "ema_trend", "rsi", "atr", "macd_hist", "adx"]
            available = [c for c in cols if c in df.columns]
            state_store["candles"][asset] = df.tail(50)[available].to_dict("records")
            await asyncio.sleep(0.3)

        # All configured assets returned no candles (markets closed) — re-resolve next cycle
        if not got_data:
            self._need_resolve = True
            logger.warning("[DATA] no candles from any asset — will re-resolve tradable markets next cycle")

        # Smart selection: highest-confidence signal first. With max_open_positions=1 the bot
        # waits for ANY open position (auto, manual, web) to close before entering a new one.
        # slots_free is re-read from active_orders live each iteration — never a stale counter.
        candidates.sort(key=lambda s: s.confidence, reverse=True)
        placed = None
        unavailable_this_cycle: list[str] = []
        # §1 — strategy_mode switch is REPLACE, not parallel: when spec_v1 is active, legacy
        # stops opening auto orders entirely (spec_v1's own M5/M15 scheduler is the only auto
        # order path — see spec_v1_m5_loop). Legacy signals above are still computed/shown on
        # the dashboard (read-only) so the two engines can be compared side by side.
        if self.cfg.strategy_mode != "spec_v1":
            in_cooldown = time.time() < self._cooldown_until
            if in_cooldown:
                remain = int((self._cooldown_until - time.time()) / 60) + 1
                self.log_activity("⏳", f"พักหลังแพ้ติดกัน — เหลืออีก ~{remain} นาที จึงกลับมาเปิดออเดอร์", phase="cooldown")
            for signal in candidates:
                if in_cooldown:
                    break
                # Re-query active_orders and expiry lock live — covers trades placed moments ago
                # or closed by external_sync_loop between iterations.
                if len(self.trade_manager.active_orders) >= self.cfg.max_open_positions:
                    break
                if time.time() < self.trade_manager._auto_locked_until:
                    break
                async with self._iq_lock:
                    trade = await asyncio.to_thread(self.trade_manager.execute_trade, signal)
                if trade is _ORDER_UNAVAILABLE:
                    # Broker says pair unavailable right now — try next highest-confidence signal
                    logger.info(f"[CYCLE] {signal.asset} unavailable — trying next signal this cycle")
                    unavailable_this_cycle.append(signal.asset)
                    continue
                if isinstance(trade, dict):
                    placed = trade
                    state_store["trades"] = self.trade_manager.trades
                    await broadcast({"type": "new_trade", "data": trade})
                    asyncio.create_task(self.tg.alert_trade_open(trade))
                    mg = f" · ไม้ {trade.get('mg_step')}" if trade.get("mg_step") else ""
                    self.log_activity("🚀", f"เปิดออเดอร์ {trade['asset']} {trade['direction']} ที่ {(trade.get('confidence') or 0):.0f}%{mg}", phase="trading")
                    # spec_v1 §13.1 counters — parallel bookkeeping only, does not gate the live order
                    try:
                        self._risk_v2.record_order_placed()
                    except Exception as e:
                        logger.warning(f"[RISK-V2] record_order_placed (auto) failed: {e}")
                # trade is None (risk block / veto / other order error) or a placed trade —
                # either way, stop trying further signals this cycle.
                break

        # Summarize the decision so the dashboard shows what the bot is doing / waiting for
        best = max(signals_this_cycle, key=lambda s: s.get("confidence") or 0, default=None)
        best_txt = f"{best['asset']} {best['signal']} {(best.get('confidence') or 0):.0f}%" if best else "-"
        if not got_data:
            self.log_activity("⚠️", "ดึงแท่งเทียนไม่ได้สักคู่ — ตลาดอาจปิดหรือการเชื่อมต่อ IQ มีปัญหา", level="warn", phase="error")
        elif self.cfg.strategy_mode == "spec_v1":
            # §1 — legacy is intentionally not opening orders in this mode; make that explicit
            # instead of showing the generic "blocked" message legacy would show when idle.
            if candidates:
                self.log_activity("🧪", f"โหมด spec_v1 ทำงานอยู่ — legacy ไม่เปิดออเดอร์ (มี {len(candidates)} สัญญาณ legacy ไว้เทียบผลเท่านั้น)", phase="spec_v1_compare")
            else:
                self.log_activity("🧪", "โหมด spec_v1 ทำงานอยู่ — legacy หยุดเปิดออเดอร์อัตโนมัติทั้งหมด", phase="spec_v1_compare")
        elif placed is None and candidates:
            if len(self.trade_manager.active_orders) >= self.cfg.max_open_positions:
                reason = f"รอปิดไม้ที่เปิดอยู่ก่อน ({len(self.trade_manager.active_orders)} open)"
            else:
                _, reason = self.trade_manager.can_trade()
            if unavailable_this_cycle and len(unavailable_this_cycle) == len(candidates):
                reason = f"broker ปฏิเสธคู่ที่เข้าเงื่อนไขทั้งหมด ({', '.join(unavailable_this_cycle)}) — ไม่พร้อมเทรดตอนนี้"
            elif unavailable_this_cycle:
                reason = f"{reason} · ข้าม {', '.join(unavailable_this_cycle)} (broker: ไม่พร้อมเทรด)"
            self.log_activity("⛔", f"มี {len(candidates)} คู่เข้าเงื่อนไข แต่ยังไม่เปิด: {reason}", level="warn", phase="blocked")
        elif placed is None:
            self.log_activity("💤", f"ยังไม่มีคู่เข้าเงื่อนไข (≥{self.cfg.confidence_threshold:.0f}%) — รอสัญญาณ · เด่นสุด {best_txt}", phase="waiting")

        # Finalize results here too (belt-and-suspenders); result alerts + balance refresh
        # are owned by the 15s external_sync_loop to avoid duplicate notifications.
        async with self._iq_lock:
            try:
                await asyncio.wait_for(asyncio.to_thread(self.trade_manager.check_results), timeout=30)
            except Exception as e:
                logger.warning(f"[RESULT] check_results failed/timeout: {e}")

        # Risk: COOL DOWN (not a permanent pause) after a run of losses, then auto-resume.
        # A hard pause here used to deadlock: /start cleared _paused but consecutive_losses
        # stayed at the cap, so can_trade() blocked every entry and the bot never traded again.
        # Now we pause new entries for loss_cooldown_minutes, reset the counter so the cooldown
        # timer is the only gate, and resume automatically when it expires.
        if (self.trade_manager.consecutive_losses >= self.cfg.max_consecutive_losses
                and time.time() >= self._cooldown_until):
            lost = self.trade_manager.consecutive_losses
            cd = max(1, self.cfg.loss_cooldown_minutes)
            self._cooldown_until = time.time() + cd * 60
            self.trade_manager.consecutive_losses = 0  # reset so the cooldown timer is the only gate
            msg = f"{self.cfg.max_consecutive_losses} consecutive losses — cooling down {cd} min, then auto-resume"
            logger.warning(f"[RISK] {msg}")
            self.log_activity("🛑", f"แพ้ติดกัน {lost} ไม้ — พักเทรด {cd} นาที แล้วกลับมาต่ออัตโนมัติ", level="error", phase="cooldown")
            asyncio.create_task(self.tg.alert_risk_pause(msg))
        # auto-resume notice when a loss-cooldown has just elapsed
        elif self._cooldown_until and time.time() >= self._cooldown_until:
            self._cooldown_until = 0.0
            self.log_activity("▶️", "ครบเวลาพักหลังแพ้ติดกัน — กลับมาเปิดออเดอร์ต่อ", phase="running")
            asyncio.create_task(self.tg.alert_bot_resumed())

        stats = self.trade_manager.get_stats()
        # spec_v1 risk counters roll forward every cycle (day/week boundary + baseline
        # seed) so the §13.1 block stays live even though it isn't gating orders yet.
        try:
            self._risk_v2.roll_boundaries(balance=state_store.get("balance"))
        except Exception as e:
            logger.warning(f"[RISK-V2] roll_boundaries failed: {e}")
        state_store.update({
            "signals": signals_this_cycle,
            "trades": self.trade_manager.trades,
            "stats": stats,
            "status": "running",
            "risk": self.build_risk(),
            "config": self.build_config(),
            "account_type": self.cfg.account_type,
            # ── §13.1 additive fields — see build_risk_v2/build_state_machine_v2 docstrings ──
            "risk_v2": self.build_risk_v2(),
            **self.build_state_machine_v2(),
            "martingale_warning": self.build_martingale_warning(),
        })

        # Learn from results frequently (trades come slowly under one-at-a-time Martingale)
        if self.loop_count % 5 == 0 and self.trade_manager.trades:
            lr = await asyncio.to_thread(self.learning_engine.analyze, self.trade_manager.trades)
            state_store["learning"] = lr

            # แจ้ง Telegram เฉพาะ rule ที่ยังไม่เคยแจ้ง — ป้องกันแจ้งซ้ำทุกรอบ
            disabled_rules = lr.get("disabled_rules") or []
            new_rule_ids = {r["id"] for r in disabled_rules if r["id"] not in self._alerted_rule_ids}
            has_new_warnings = bool(lr.get("warnings")) and not disabled_rules  # warnings-only แจ้งครั้งแรกเสมอ
            if new_rule_ids or has_new_warnings:
                asyncio.create_task(self.tg.alert_learning(lr))
                self._alerted_rule_ids.update(new_rule_ids)

            self.trade_manager.learning_rules = self.trade_manager._load_learning_rules()

        await broadcast({"type": "update", "data": {
            "signals": signals_this_cycle,
            "stats": stats,
            "balance": state_store["balance"],
            "candles": state_store["candles"],
            "learning": state_store.get("learning", {}),
            "status": state_store["status"],
            "trades": self.trade_manager.trades,
            "risk": state_store["risk"],
            "config": state_store["config"],
            "account_type": state_store["account_type"],
            "activity": state_store["activity"],
            "activity_log": state_store["activity_log"],
            "risk_v2": state_store.get("risk_v2"),
            "state_machine": state_store.get("state_machine"),
            "global_state": state_store.get("global_state"),
            "martingale_warning": state_store.get("martingale_warning"),
        }})

    # ── Command handler from WebSocket ──
    async def handle_command(self, cmd: str, **kwargs):
        if cmd == "start":
            was_paused = self._paused or time.time() < self._cooldown_until
            self._paused = False
            # Clear any loss-cooldown and reset the streak counter — an explicit resume must
            # actually let the bot trade again (otherwise can_trade() keeps blocking on the old
            # consecutive_losses count and the bot scans forever without placing an order).
            self._cooldown_until = 0.0
            if self.trade_manager:
                self.trade_manager.consecutive_losses = 0
            self.running = True
            logger.info("[CMD] Bot started")
            self.log_activity("▶️", "เริ่ม/เล่นต่อบอท — กำลังกลับไปสแกน", phase="running")
            if was_paused:
                asyncio.create_task(self.tg.alert_bot_resumed())
        elif cmd == "stop":
            state_store["status"] = "stopped"
            if not self._paused:
                self._paused = True
                self.log_activity("⏸", "หยุดบอทชั่วคราว — ไม่เปิดออเดอร์ใหม่ (ไม้ที่เปิดอยู่ยังเดินต่อ)", phase="paused")
                today = self.trade_manager.get_stats().get("today") if self.trade_manager else None
                asyncio.create_task(self.tg.alert_bot_paused(today))
        elif cmd == "step":
            old = self._paused
            self._paused = False
            await self.run_cycle()
            self._paused = old
        elif cmd == "close_all":
            for oid in list(self.trade_manager.active_orders.keys()):
                logger.info(f"[CMD] Force-closing {oid}")
                # IQ Option binary can't be early-closed but we mark it
        elif cmd == "refresh":
            await broadcast({"type": "state", "data": state_store})
        elif cmd == "switch_account":
            account = kwargs.get("account", "PRACTICE")
            self.cfg.account_type = account
            # §2 site 2/3 — switch_account previously had NO safety-gate check at all (San's
            # gap finding): switching to REAL while strategy_mode=="spec_v1" must force spec_v1
            # back to legacy immediately, not just at the next config apply.
            if _enforce_spec_v1_practice_gate(self.cfg, self.tg, self._trade_logger):
                save_runtime_config(self.cfg)
                await self._push_settings()
            # 24h trading-hours experiment PRACTICE-only gate (call site 2/3) — switch_account
            # previously had no check for this flag at all; mirrors the spec_v1 gate above.
            if _enforce_trading_hours_experiment_practice_gate(self.cfg, self.tg, self._trade_logger):
                save_runtime_config(self.cfg)
                await self._push_settings()
            try:
                self.iq.change_balance(account)
                state_store["account_type"] = account
                logger.info(f"[CMD] Switched to {account}")
            except Exception as e:
                logger.error(f"[CMD] Switch failed: {e}")
        elif cmd == "set_amount":
            try:
                amount = max(1.0, float(kwargs.get("amount", self.cfg.trade_amount)))
                self.cfg.trade_amount = amount
                save_runtime_config(self.cfg)
                logger.info(f"[CMD] Trade amount set to {amount}")
                await self._push_settings()
            except (TypeError, ValueError) as e:
                logger.error(f"[CMD] set_amount failed: {e}")
        elif cmd == "set_confidence":
            try:
                conf = min(100.0, max(0.0, float(kwargs.get("confidence", self.cfg.confidence_threshold))))
                self.cfg.confidence_threshold = conf
                save_runtime_config(self.cfg)
                logger.info(f"[CMD] Confidence threshold set to {conf}%")
                await self._push_settings()
            except (TypeError, ValueError) as e:
                logger.error(f"[CMD] set_confidence failed: {e}")
        elif cmd == "update_settings":
            settings = kwargs.get("settings", {}) or {}
            # clamp the few that have hard bounds before applying
            if "confidence_threshold" in settings:
                settings["confidence_threshold"] = min(100.0, max(0.0, float(settings["confidence_threshold"])))
            if "martingale_base" in settings:
                settings["martingale_base"] = max(1.0, float(settings["martingale_base"]))
            if "trade_amount" in settings:
                settings["trade_amount"] = max(1.0, float(settings["trade_amount"]))
            if "martingale_max_steps" in settings:
                settings["martingale_max_steps"] = max(1, min(8, int(settings["martingale_max_steps"])))
            if "max_open_positions" in settings:
                settings["max_open_positions"] = 1  # always 1: global single Martingale ladder
            apply_runtime_config(self.cfg, settings, self.tg, self._trade_logger)
            self._sync_risk_v2_config()  # bug-16x: keep risk_v2's RiskConfig mirrored after every edit
            self._sync_time_filter_v1_config()  # keep the live TimeFilter's experiment flag mirrored too
            save_runtime_config(self.cfg)
            logger.info(f"[CMD] Settings updated: {', '.join(settings.keys())}")
            await self._push_settings()
            await broadcast({"type": "update", "data": {
                "stats": self.trade_manager.get_stats() if self.trade_manager else {},
                "risk": self.build_risk(),
            }})
        elif cmd == "set_pairs":
            pairs = kwargs.get("enabled_pairs") or []
            valid = [p for p in pairs if isinstance(p, str) and _is_known_pair(p)]
            invalid = [p for p in pairs if p not in valid]
            if invalid:
                await broadcast({"type": "error", "data": {
                    "message": f"set_pairs rejected unknown pair(s): {', '.join(map(str, invalid))}"}})
            if not valid:
                logger.warning("[CMD] set_pairs: no valid pairs in payload — ignored")
                return
            self.cfg.enabled_pairs = valid
            save_runtime_config(self.cfg)
            self._need_resolve = True  # trigger resolve_assets() next cycle against the new list
            logger.info(f"[CMD] enabled_pairs set to {valid}")
            await self._push_settings()

        elif cmd == "set_auto_stop":
            enabled = kwargs.get("enabled")
            drawdown_pct = kwargs.get("drawdown_pct")
            if enabled is not None:
                self.cfg.auto_stop_enabled = bool(enabled)
            if drawdown_pct is not None:
                try:
                    self.cfg.auto_stop_drawdown_pct = max(5.0, min(50.0, float(drawdown_pct)))
                except (TypeError, ValueError):
                    pass
            self._risk_v2.set_auto_stop(self.cfg.auto_stop_enabled, self.cfg.auto_stop_drawdown_pct)
            save_runtime_config(self.cfg)
            logger.info(f"[CMD] auto_stop enabled={self.cfg.auto_stop_enabled} "
                        f"drawdown_pct={self.cfg.auto_stop_drawdown_pct}")
            await self._push_settings()

        elif cmd == "reset_equity_baseline":
            bal = state_store.get("balance", 0)
            self._risk_v2.reset_equity_baseline(bal)
            logger.info(f"[CMD] equity baseline reset to {bal}")
            await broadcast({"type": "update", "data": {"risk_v2": self.build_risk_v2()}})

        elif cmd == "set_martingale":
            enabled = bool(kwargs.get("enabled", False))
            ack_risk = bool(kwargs.get("ack_risk", False))
            ok, reason = MartingaleModule.validate_toggle(enabled, ack_risk)
            if not ok:
                logger.warning(f"[CMD] set_martingale rejected: {reason}")
                await broadcast({"type": "error", "data": {"message": reason}})
                return
            self.cfg.martingale_enabled = enabled
            self.cfg.martingale_ack_risk = ack_risk
            save_runtime_config(self.cfg)
            logger.info(f"[CMD] martingale_enabled={enabled} ack_risk={ack_risk}")
            await self._push_settings()
            await broadcast({"type": "update", "data": {"martingale_warning": self.build_martingale_warning()}})

        elif cmd == "manual_trade":
            asset = kwargs.get("asset", "")
            direction = kwargs.get("direction", "")
            if not asset or not self.trade_manager:
                logger.warning("[CMD] manual_trade missing asset or not connected")
                return
            trade = await asyncio.to_thread(self.trade_manager.execute_manual, asset, direction)
            if trade:
                state_store["trades"] = self.trade_manager.trades
                state_store["risk"] = self.build_risk()
                # spec_v1 §13.1 counters — parallel bookkeeping only, does not gate the live order
                try:
                    self._risk_v2.record_order_placed()
                except Exception as e:
                    logger.warning(f"[RISK-V2] record_order_placed (manual) failed: {e}")
                await broadcast({"type": "new_trade", "data": trade})
                await broadcast({"type": "update", "data": {
                    "trades": self.trade_manager.trades,
                    "risk": state_store["risk"],
                    "stats": self.trade_manager.get_stats(),
                }})
                asyncio.create_task(self.tg.alert_trade_open(trade))
            else:
                await broadcast({"type": "error", "data": {"message": f"Manual trade {asset} {direction} failed/blocked"}})

    async def _push_settings(self):
        state_store["config"] = self.build_config()
        await broadcast({"type": "update", "data": {"config": state_store["config"]}})

    # ── Telegram remote control ──
    def _tg_summary_text(self) -> str:
        tm = self.trade_manager
        if not tm:
            return "ยังไม่ได้เชื่อมต่อ"
        t = tm.get_stats().get("today", {})
        pnl = t.get("pnl", 0) or 0
        sign = "+" if pnl >= 0 else ""
        bot, man = t.get("bot", {}), t.get("manual", {})
        return (
            f"📊 <b>สรุปวันนี้</b>\n\n"
            f"💰 กำไร/ขาดทุน: <b>{sign}{pnl:.2f} บาท</b>\n"
            f"📈 เทรด: <b>{t.get('total', 0)}</b> ไม้ "
            f"(ชนะ {t.get('wins', 0)} / แพ้ {t.get('losses', 0)} / เสมอ {t.get('equals', 0)})\n"
            f"🎯 อัตราชนะ: <b>{t.get('win_rate', 0)}%</b>\n"
            f"🤖 บอท: ชนะ {bot.get('wins',0)} / แพ้ {bot.get('losses',0)} · "
            f"✋ มือ: ชนะ {man.get('wins',0)} / แพ้ {man.get('losses',0)}\n"
            f"💵 ยอดเงิน: <b>฿{state_store.get('balance', 0):,.2f}</b>\n"
            f"🕐 {datetime.now().strftime('%H:%M:%S')}"
        )

    def _tg_dashboard_text(self) -> str:
        signals = state_store.get("signals") or []
        assets = self.cfg.assets or []
        threshold = self.cfg.confidence_threshold

        if not assets:
            return "💤 <b>ไม่มีคู่เงินที่สแกนอยู่</b>\n(ตลาดปิด หรือบอทยังไม่ได้รัน)"

        lines = [f"📡 <b>คู่เงินที่สแกนอยู่ ({len(assets)} คู่)</b>\n"]

        sig_by_asset = {s["asset"]: s for s in signals}

        for asset in assets:
            raw_name = asset[:-3] if asset.endswith("-op") else asset  # drop IQ '-op' suffix for display
            name = _h(raw_name)
            s = sig_by_asset.get(asset)
            if not s:
                lines.append(f"⬜ <b>{name}</b> — รอข้อมูล")
                continue

            sig = s.get("signal", "HOLD")
            conf = s.get("confidence") or 0
            rsi = s.get("rsi")
            adx = s.get("adx")

            if sig == "CALL":
                sig_icon = "🟢 CALL▲"
            elif sig == "PUT":
                sig_icon = "🔴 PUT▼"
            else:
                sig_icon = "⚪ HOLD"

            ready = sig in ("CALL", "PUT") and conf >= threshold
            star = " ⭐" if ready else ""

            detail_parts = []
            if rsi is not None:
                detail_parts.append(f"RSI {rsi:.0f}")
            if adx is not None:
                detail_parts.append(f"ADX {adx:.0f}")
            detail = " · ".join(detail_parts)

            lines.append(
                f"{'🔥' if ready else '  '} <b>{name}</b> {sig_icon} {conf:.0f}%{star}"
                + (f"\n     {detail}" if detail else "")
            )

        ready_count = sum(
            1 for s in signals
            if s.get("signal") in ("CALL", "PUT") and (s.get("confidence") or 0) >= threshold
        )
        lines.append(f"\n🎯 เข้าเงื่อนไข: <b>{ready_count}/{len(assets)}</b> คู่ (≥{threshold:.0f}%)")
        lines.append(f"🕐 {datetime.now().strftime('%H:%M:%S')}")
        return "\n".join(lines)

    def _tg_status_text(self) -> str:
        r = self.build_risk()
        act = (state_store.get("activity") or {}).get("msg", "-")
        raw_status = state_store.get("status", "-")
        _status_map = {
            "running": "🟢 กำลังทำงาน",
            "stopped": "⏹ หยุดอยู่",
            "reconnecting": "🔄 กำลังเชื่อมต่อใหม่",
            "paused": "⏸ หยุดชั่วคราว",
            "connection_failed": "❌ เชื่อมต่อล้มเหลว",
        }
        if self._paused:
            status = "⏸ หยุดชั่วคราว"
        elif raw_status.startswith("error"):
            err_detail = _h(raw_status[6:].strip()) if len(raw_status) > 5 else ""
            status = f"❌ ผิดพลาด{': ' + err_detail if err_detail else ''}"
        else:
            status = _status_map.get(raw_status, _h(raw_status))
        return (
            f"🤖 <b>สถานะบอท</b>\n\n"
            f"สถานะ: <b>{status}</b>\n"
            f"บัญชี: <b>{self.cfg.account_type}</b>\n"
            f"💵 ยอดเงิน: <b>฿{state_store.get('balance', 0):,.2f}</b>\n"
            f"ไม้เปิดอยู่: {r.get('open',0)}/{r.get('max_open',3)} · "
            f"เทรดวันนี้: {r.get('today_trades',0)}\n"
            f"กำไรวันนี้: {r.get('daily_pnl',0):+.2f} บาท\n"
            f"กำลัง: {act}\n"
            f"🕐 {datetime.now().strftime('%H:%M:%S')}"
        )

    async def handle_telegram_command(self, text: str, update_id: int | None = None):
        cmd = text.lstrip("/").split()[0].split("@")[0].lower()
        logger.info(f"[TG-CMD] received: /{cmd}")
        if cmd in ("start", "run", "resume", "go"):
            await self.handle_command("start")
            await self.tg.send("▶️ <b>เริ่มบอทแล้ว</b> — กลับไปสแกนหาสัญญาณ")
        elif cmd in ("stop", "pause"):
            await self.handle_command("stop")
            await self.tg.send("⏸ <b>หยุดบอทแล้ว</b> — ไม่เปิดออเดอร์ใหม่ (ไม้ที่เปิดอยู่ยังเดินต่อ)")
        elif cmd == "restart":
            await self.tg.send("🔄 <b>กำลังรีสตาร์ทบอท...</b> จะกลับมาออนไลน์ใน ~15 วินาที")
            logger.warning("[TG-CMD] restart requested — exiting (systemd will relaunch)")
            # Advance Telegram offset ก่อน exit เพื่อป้องกัน get_updates เจอ /restart ซ้ำตอน boot ใหม่
            if update_id is not None:
                try:
                    await self.tg.get_updates(offset=update_id + 1, timeout=1)
                except Exception:
                    pass
            await asyncio.sleep(1)
            os._exit(0)   # systemd Restart=always brings it back
        elif cmd in ("status", "stat", "s"):
            await self.tg.send(self._tg_status_text())
        elif cmd in ("summary", "pnl", "today", "sum"):
            await self.tg.send(self._tg_summary_text())
        elif cmd in ("dashboard", "pairs", "scan", "d"):
            await self.tg.send(self._tg_dashboard_text())
        else:
            await self.tg.send(
                "📋 <b>คำสั่งที่ใช้ได้</b>\n"
                "/start — เริ่ม/เล่นต่อ\n"
                "/stop — หยุดชั่วคราว\n"
                "/restart — รีสตาร์ทบอท\n"
                "/status — ดูสถานะตอนนี้\n"
                "/summary — สรุปกำไรวันนี้\n"
                "/dashboard — ดูคู่เงินที่สแกนอยู่"
            )

    async def telegram_command_loop(self):
        """Listen for Telegram commands (long-poll). Only the configured chat may control."""
        if not self.tg.cfg.enabled or not self.tg.cfg.bot_token:
            return
        allowed = str(self.tg.cfg.chat_id)
        # Skip any backlog so old messages don't trigger actions on startup
        offset = None
        try:
            backlog = await self.tg.get_updates(timeout=0)
            if backlog:
                offset = backlog[-1]["update_id"] + 1
        except Exception:
            pass
        await self.tg.send("🎮 พร้อมรับคำสั่ง: /start /stop /restart /status /summary /dashboard")
        while True:
            try:
                updates = await self.tg.get_updates(offset=offset, timeout=25)
            except Exception as e:
                logger.debug(f"[TG-CMD] poll error: {e}")
                await asyncio.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                chat = str((msg.get("chat") or {}).get("id", ""))
                txt = (msg.get("text") or "").strip()
                if allowed and chat != allowed:
                    logger.warning(f"[TG-CMD] ignored command from unauthorized chat {chat}")
                    continue
                if txt.startswith("/"):
                    try:
                        await self.handle_telegram_command(txt, update_id=u["update_id"])
                    except Exception as e:
                        logger.error(f"[TG-CMD] handler error: {e}")

    async def external_sync_loop(self, interval: int = 15):
        """Every ~15s keep the dashboard live: refresh balance, finalize closed trades,
        pull in platform-opened trades, and broadcast a fresh snapshot — independent of
        the 5-minute trading cycle."""
        while True:
            await asyncio.sleep(interval)
            if not self.trade_manager:
                continue

            new_external = []
            async with self._iq_lock:
                # 1) live balance
                try:
                    bal = await asyncio.wait_for(asyncio.to_thread(self.iq.get_balance), timeout=10)
                    state_store["balance"] = round(bal or 0, 2)
                except Exception:
                    pass
                # 2) finalize trades that have expired (open -> WIN/LOSS)
                try:
                    await asyncio.wait_for(asyncio.to_thread(self.trade_manager.check_results), timeout=20)
                except Exception as e:
                    logger.warning(f"[RESULT] sync-loop check failed: {e}")
                # 3) discover platform/web-opened trades (in-memory, fast)
                try:
                    new_external = await asyncio.to_thread(self.trade_manager.sync_external_trades)
                except Exception as e:
                    logger.warning(f"[SYNC] external sync failed: {e}")

            # alert + log every trade that just closed — drained from the engine's queue, so
            # this fires no matter which loop (5-min cycle / this loop / external sync) closed it
            today_stats = self.trade_manager.get_stats().get("today")
            for t in self.trade_manager.drain_pending_alerts():
                if t.get("result"):
                    icon = {"WIN": "✅", "LOSS": "❌", "EQUAL": "➖"}.get(t["result"], "•")
                    pnl = t.get("pnl") or 0
                    self.log_activity(icon, f"ปิดไม้ {t['asset']} {t['direction']} → {t['result']} ({pnl:+.2f})",
                                      level="error" if t["result"] == "LOSS" else "info", phase="result")
                    asyncio.create_task(self.tg.alert_result(t, today_stats))
                    # §5 — MUST branch exclusively on source=="spec_v1" here: BotStateMachine.
                    # on_trade_closed() already calls self.risk_manager.record_order_result()
                    # internally (state_machine.py, count_streak defaults True) using the SAME
                    # self._risk_v2 instance. Calling self._risk_v2.record_order_result() again
                    # in the else-branch for a spec_v1 trade would double-count P&L/streak —
                    # this if/else (never both) is the guard against that.
                    if t.get("source") == "spec_v1":
                        if self._state_machine_v1 is not None:
                            try:
                                self._state_machine_v1.on_trade_closed(t["asset"], pnl, t["result"])
                            except Exception as e:
                                logger.warning(f"[SPEC_V1] on_trade_closed failed: {e}")
                        self._spec_v1_log_trade_to_sqlite(t)
                    else:
                        # spec_v1 §13.1 counters — parallel bookkeeping only, does not gate the
                        # live order. count_streak mirrors legacy's own consecutive_losses field,
                        # which trading_engine._apply_close() only increments for source=="auto".
                        try:
                            self._risk_v2.record_order_result(pnl, t["result"], count_streak=(t.get("source") == "auto"))
                        except Exception as e:
                            logger.warning(f"[RISK-V2] record_order_result failed: {e}")
                # Force-expired (unresolved, slot freed): notify so the user isn't left guessing
                elif t.get("status") == "expired":
                    self.log_activity("⏰", f"ออเดอร์ค้าง {t['asset']} {t['direction']} — เคลียร์ช่องแล้ว",
                                      level="warning", phase="result")
                    asyncio.create_task(self.tg.alert_expired(t))

            # alert any newly discovered platform trades
            for t in new_external:
                logger.info(f"[SYNC] Broadcasting external trade {t['asset']} {t['direction']}")
                if t.get("status") == "open":
                    asyncio.create_task(self.tg.alert_trade_open(t))
                    # spec_v1 §13.1 counters — trade opened directly on IQ Option's own web/app
                    # UI, discovered here for the first time as still-open; its eventual close
                    # will be captured by drain_pending_alerts() above on a later loop.
                    try:
                        self._risk_v2.record_order_placed()
                    except Exception as e:
                        logger.warning(f"[RISK-V2] record_order_placed (web) failed: {e}")
                await broadcast({"type": "new_trade", "data": t})

            # always push a fresh snapshot so balance / open count / stats stay current
            state_store["trades"] = self.trade_manager.trades
            state_store["risk"] = self.build_risk()
            state_store["stats"] = self.trade_manager.get_stats()
            await broadcast({"type": "update", "data": {
                "balance": state_store["balance"],
                "trades": self.trade_manager.trades,
                "risk": state_store["risk"],
                "stats": state_store["stats"],
                "status": state_store.get("status"),
                "activity": state_store["activity"],
                "activity_log": state_store["activity_log"],
            }})

    async def main_loop(self):
        if not await asyncio.to_thread(self.connect):
            logger.error("[BOT] IQ Option connection failed — stopping")
            state_store["status"] = "connection_failed"
            await broadcast({"type": "update", "data": state_store})
            return

        # Resolve tradable real-forex pairs up front so the start alert / dashboard agree
        # (otherwise the alert shows the 5 default majors while the dashboard shows 0).
        try:
            await asyncio.wait_for(asyncio.to_thread(self.resolve_assets), timeout=60)
            self._ensure_candle_stores()  # §4.1
        except Exception as e:
            logger.warning(f"[ASSET] initial resolve failed: {e}")

        # Push balance + initial state immediately so the dashboard fills in before the
        # first (possibly slow) scan cycle finishes.
        try:
            bal = await asyncio.wait_for(asyncio.to_thread(self.iq.get_balance), timeout=15)
        except Exception:
            bal = state_store.get("balance", 0)
        state_store.update({
            "account_type": self.cfg.account_type,
            "config": self.build_config(),
            "risk": self.build_risk(),
            "trades": self.trade_manager.trades,
            "stats": self.trade_manager.get_stats(),
            "balance": round(bal or 0, 2),
            "status": "running",
        })
        await broadcast({"type": "update", "data": state_store})

        await self.tg.alert_bot_start(
            account_type=self.cfg.account_type,
            assets=self.cfg.assets,
            timeframe=self.cfg.timeframe,
            trade_amount=self.cfg.trade_amount,
            confidence_threshold=self.cfg.confidence_threshold,
            assets_resolved=self._assets_resolved,
        )

        self.running = True
        logger.info(f"[BOT] Main loop — aligned to {self.cfg.timeframe}s candle close")

        while self.running:
            try:
                await self.run_cycle()
            except Exception as e:
                logger.error(f"[LOOP] {e}", exc_info=True)
                state_store["status"] = f"error: {e}"
            # Sleep until just after the next candle closes, so each cycle acts on a freshly
            # closed candle (not at an arbitrary offset within the candle).
            tf = self.cfg.timeframe
            now = time.time()
            wait = tf - (now % tf) + 8  # +8s buffer — gives IQ Option time to roll over to the next expiry slot after the candle boundary (was +2s; buy attempts near the boundary were hitting "asset not available"/timeout)
            await asyncio.sleep(wait)


# ─────────────────────────────────────────────────
#  WEBSOCKET SERVER (with command handling)
# ─────────────────────────────────────────────────
_bot_ref: FullTradingBot = None


async def ws_handler_with_cmds(websocket):
    connected_clients.add(websocket)
    try:
        await websocket.send(json.dumps({"type": "state", "data": state_store}))
        async for raw in websocket:
            try:
                msg = json.loads(raw)
                if msg.get("type") == "cmd" and _bot_ref:
                    await _bot_ref.handle_command(
                        msg.get("action", ""),
                        **{k: v for k, v in msg.items() if k not in ("type", "action")}
                    )
                elif msg.get("type") == "ping":
                    await websocket.send(json.dumps({"type": "pong"}))
            except Exception as e:
                logger.debug(f"[WS] Message error: {e}")
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        connected_clients.discard(websocket)


async def ws_server_full():
    # ปิด log noise จาก probe connections (InvalidMessage/EOFError ก่อน handshake)
    logging.getLogger("websockets.server").setLevel(logging.CRITICAL)
    async with websockets.serve(ws_handler_with_cmds, "0.0.0.0", 8765):
        logger.info("[WS] Dashboard at ws://0.0.0.0:8765 -> open frontend/dashboard.html")
        await asyncio.Future()


# ─────────────────────────────────────────────────
#  ENTRYPOINT
# ─────────────────────────────────────────────────
async def main():
    global _bot_ref

    email    = os.getenv("IQ_EMAIL", "")
    password = os.getenv("IQ_PASSWORD", "")

    # Debug: show what credentials were loaded (mask password)
    print(f"[CFG] IQ_EMAIL    = '{email}'")
    print(f"[CFG] IQ_PASSWORD = '{'*' * len(password) if password else '(empty)'}'")
    print(f"[CFG] IQ_ACCOUNT  = '{os.getenv('IQ_ACCOUNT', 'PRACTICE')}'")

    if not email or not password:
        print("\n[ERROR] Email or password is empty!")
        print("  → Make sure .env file exists next to main.py (or in parent folder)")
        print("  → Content should be:")
        print("      IQ_EMAIL=your@email.com")
        print("      IQ_PASSWORD=yourpassword")
        return

    # ── REAL account validation gate (item 2) ──────────────────────────────
    # Trading on a REAL account requires an explicit opt-in via IQ_ALLOW_REAL=1.
    # If that flag is absent the bot forces PRACTICE regardless of IQ_ACCOUNT,
    # so a misconfigured env can never accidentally trade real money.
    raw_account_type = os.getenv("IQ_ACCOUNT", "PRACTICE").upper()
    if raw_account_type == "REAL" and os.getenv("IQ_ALLOW_REAL", "") != "1":
        raw_account_type = "PRACTICE"
        logger.warning(
            "WARNING: REAL blocked — validation gate: set IQ_ALLOW_REAL=1 to override. "
            "Forcing PRACTICE account."
        )
        print(
            "\n[WARNING] REAL account blocked by safety gate.\n"
            "          Set IQ_ALLOW_REAL=1 in your .env to enable REAL trading.\n"
            "          Falling back to PRACTICE.\n"
        )

    assets_env = os.getenv("IQ_ASSETS", "AUTO").strip()
    auto_assets = assets_env.upper() == "AUTO"

    cfg = TradingConfig(
        email=email,
        password=password,
        account_type=raw_account_type,
        assets=None if auto_assets else assets_env.split(","),
        auto_discover_assets=auto_assets,
        max_assets=int(os.getenv("IQ_MAX_ASSETS", "999")),
        timeframe=int(os.getenv("IQ_TIMEFRAME", "300")),
        trade_amount=float(os.getenv("IQ_AMOUNT", "50.0")),
        confidence_threshold=float(os.getenv("IQ_CONFIDENCE", "70.0")),
        max_consecutive_losses=int(os.getenv("IQ_MAX_LOSSES", "4")),
        loss_cooldown_minutes=int(os.getenv("IQ_LOSS_COOLDOWN", "30")),
        max_open_positions=int(os.getenv("IQ_MAX_OPEN", "1")),
        max_trades_per_day=int(os.getenv("IQ_MAX_DAY_TRADES", "20")),
        daily_profit_target=float(os.getenv("IQ_DAILY_TARGET", "200.0")),
        daily_loss_limit=float(os.getenv("IQ_DAILY_LOSS_LIMIT", "150.0")),  # match config default so a missing config.json never silently disables the loss limit
    )

    tg_cfg = TelegramConfig(
        bot_token=os.getenv("TG_TOKEN", ""),
        chat_id=os.getenv("TG_CHAT_ID", ""),
        min_confidence=80.0,
        enabled=bool(os.getenv("TG_TOKEN")),
    )
    tg = TelegramBot(tg_cfg)

    # Dashboard-saved settings override env. tg is constructed first (above) so the spec_v1
    # PRACTICE-only safety gate (§2 site 1/3, inside apply_runtime_config) can alert on a
    # forced fallback even at cold-start, not just from later dashboard edits.
    apply_runtime_config(cfg, load_runtime_config(), tg)

    bot = FullTradingBot(cfg, tg)
    _bot_ref = bot

    await asyncio.gather(
        ws_server_full(),
        bot.main_loop(),
        bot.external_sync_loop(),
        bot.telegram_command_loop(),
        bot.spec_v1_m5_loop(),
        bot.spec_v1_m15_loop(),
    )


if __name__ == "__main__":
    os.makedirs("logs", exist_ok=True)
    os.makedirs("data", exist_ok=True)
    # Make console + file logging UTF-8 so Thai text and emoji don't throw cp1252 errors on Windows
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler("logs/trading.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    asyncio.run(main())
