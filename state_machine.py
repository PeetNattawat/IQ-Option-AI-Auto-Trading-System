"""
state_machine.py — BotStateMachine (San's Architecture Notes §8).

IDLE -> CHECK_FILTERS -> CHECK_SIGNAL -> PLACE_ORDER -> IN_TRADE -> IDLE
ERROR / KILL are process-global (a websocket drop or 3 consecutive process-level
errors kills the whole bot, not just one asset's lane — spec §7 "ปิดบอท" = the
whole bot). Per-asset state (IDLE..IN_TRADE) is independent per asset.

This module orchestrates TrendFilter / EntrySignal / TimeFilter / RiskManager /
MartingaleModule against injected callables for the actual broker I/O (placing an
order, reading balance) so it stays unit-testable without a live IQ Option session,
and so it doesn't duplicate trading_engine.TradeManager's broker-call plumbing
(reconnect/retry, digital-vs-binary routing, etc.) — it calls into that layer
through `place_order_fn`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from candle_store import CandleStore
from entry_signal import EntrySignal
from martingale import MartingaleModule
from risk_manager import RiskManager
from time_filter import TimeFilter
from trend_filter import TrendFilter, TrendState

BANGKOK = ZoneInfo("Asia/Bangkok")

LATENCY_BUDGET_SECONDS = 2.0
ERROR_STREAK_KILL = 3
WS_SILENCE_KILL_SECONDS = 60


@dataclass
class AssetState:
    state: str = "IDLE"
    since: str = ""
    reason: str = ""


class BotStateMachine:

    def __init__(
        self,
        assets: list[str],
        candle_stores: dict[str, CandleStore],
        trend_filter: TrendFilter,
        entry_signal: EntrySignal,
        time_filter: TimeFilter,
        risk_manager: RiskManager,
        place_order_fn: Callable[[str, str, float], object],
        get_balance_fn: Callable[[], float],
        martingale: Optional[MartingaleModule] = None,
        martingale_flags_fn: Optional[Callable[[], tuple[bool, bool]]] = None,
        on_event: Optional[Callable[[str, dict], None]] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
    ):
        self.assets = assets
        self.candle_stores = candle_stores
        self.trend_filter = trend_filter
        self.entry_signal = entry_signal
        self.time_filter = time_filter
        self.risk_manager = risk_manager
        self.place_order_fn = place_order_fn
        self.get_balance_fn = get_balance_fn
        self.martingale = martingale
        self.martingale_flags_fn = martingale_flags_fn or (lambda: (False, False))
        self.on_event = on_event
        self._now_fn = now_fn or (lambda: datetime.now(BANGKOK))

        self.states: dict[str, AssetState] = {a: AssetState() for a in assets}
        self.trend_states: dict[str, TrendState] = {}
        self.global_state = "RUNNING"
        self.error_streak = 0
        self.last_ws_pong = time.time()

    def _emit(self, event_type: str, detail: dict):
        if self.on_event:
            try:
                self.on_event(event_type, detail)
            except Exception:
                pass

    def _set_asset_state(self, asset: str, state: str, reason: str = ""):
        old = self.states.get(asset, AssetState()).state
        self.states[asset] = AssetState(state=state, since=self._now_fn().isoformat(), reason=reason)
        if old != state:
            self._emit("STATE_TRANSITION", {"asset": asset, "from": old, "to": state, "reason": reason})

    def pong(self):
        """Call on every websocket heartbeat pong received from IQ Option."""
        self.last_ws_pong = time.time()

    def on_heartbeat(self):
        if self.global_state == "KILLED":
            return
        if time.time() - self.last_ws_pong > WS_SILENCE_KILL_SECONDS:
            self._transition_global("KILL", "websocket silent > 60s")

    def record_process_error(self, detail: str = ""):
        if self.global_state == "KILLED":
            return
        self.error_streak += 1
        self._emit("ERROR", {"detail": detail, "streak": self.error_streak})
        if self.error_streak >= ERROR_STREAK_KILL:
            self._transition_global("KILL", f"{self.error_streak} consecutive errors")
        else:
            self.global_state = "ERROR"

    def record_process_ok(self):
        """Call after a fully successful cycle — clears the error streak."""
        if self.global_state == "ERROR":
            self.global_state = "RUNNING"
        self.error_streak = 0

    def _transition_global(self, new_state: str, reason: str):
        old = self.global_state
        self.global_state = new_state
        self._emit("KILL" if new_state == "KILL" else "STATE_TRANSITION",
                    {"from": old, "to": new_state, "reason": reason})
        if new_state == "KILL":
            self.global_state = "KILLED"

    def on_m15_close(self, asset: str):
        store = self.candle_stores.get(asset)
        if not store:
            return
        self.trend_states[asset] = self.trend_filter.evaluate(store.m15_df(), asset)

    def on_m5_close(self, asset: str) -> AssetState:
        if self.global_state == "KILLED":
            return self.states[asset]

        candle_close_event_time = time.time()
        now_th = self._now_fn()

        # ── CHECK_FILTERS ──
        self._set_asset_state(asset, "CHECK_FILTERS")
        tradeable, reason = self.time_filter.is_tradeable(now_th)
        if not tradeable:
            self._set_asset_state(asset, "IDLE", reason)
            return self.states[asset]

        balance = None
        try:
            balance = self.get_balance_fn()
        except Exception:
            pass
        self.risk_manager.roll_boundaries(now_th, balance)
        can, reason = self.risk_manager.can_trade(now_th, balance)
        if not can:
            self._set_asset_state(asset, "IDLE", reason)
            return self.states[asset]

        trend = self.trend_states.get(asset)
        if trend is None or trend.status == "NO_TRADE":
            self._set_asset_state(asset, "IDLE", "NO_TRADE (M15)")
            return self.states[asset]

        # ── CHECK_SIGNAL ──
        self._set_asset_state(asset, "CHECK_SIGNAL")
        store = self.candle_stores.get(asset)
        if not store:
            self._set_asset_state(asset, "IDLE", "no candle store")
            return self.states[asset]
        result = self.entry_signal.evaluate(asset, trend, store.m5_df())
        if result.signal == "HOLD":
            self._set_asset_state(asset, "IDLE", result.reason)
            return self.states[asset]

        # ── PLACE_ORDER ──
        self._set_asset_state(asset, "PLACE_ORDER")
        mg_enabled, mg_ack = self.martingale_flags_fn()
        if self.martingale and MartingaleModule.is_active(mg_enabled, mg_ack):
            stake = self.martingale.next_stake()
        else:
            stake = self.risk_manager.stake_amount(balance or 0)

        # Spec §4.5/§7: latency from candle-close to order-entry must be < 2s, else
        # "cancel the signal". Measured HERE — BEFORE the broker call — so a signal
        # that's already too slow is genuinely never submitted. This is the actual
        # cancel-a-signal semantics the spec asks for (bug-144 fix; previously this
        # check ran AFTER place_order_fn() had already returned a live trade dict,
        # so "cancelling" really meant discarding bookkeeping for a position that
        # was already open at the broker — see .wolf/buglog.json bug-144).
        pre_call_latency = time.time() - candle_close_event_time
        if pre_call_latency >= LATENCY_BUDGET_SECONDS:
            self._emit("ERROR", {"asset": asset, "reason": "latency exceeded before order",
                                  "latency_s": pre_call_latency})
            self._set_asset_state(
                asset, "IDLE",
                f"latency {pre_call_latency:.2f}s >= {LATENCY_BUDGET_SECONDS}s — signal cancelled before broker call",
            )
            return self.states[asset]

        self.risk_manager.record_order_placed()
        trade = self.place_order_fn(asset, result.signal, stake)
        latency = time.time() - candle_close_event_time

        if not isinstance(trade, dict):
            self.risk_manager._open_positions = max(0, self.risk_manager._open_positions - 1)
            self._set_asset_state(asset, "IDLE", "order failed/rejected")
            self.record_process_error("order placement failed")
            return self.states[asset]

        # The broker call itself succeeded — a real position now exists at the
        # broker regardless of how long the round-trip took. A trade that was
        # actually placed must NEVER be silently un-tracked: always carry it
        # through the normal IN_TRADE / on_trade_closed / trade_logger path.
        # If the round-trip itself (network/broker-side, not our own processing)
        # also blew the latency budget, flag it for the spec §9 audit trail
        # instead of discarding it.
        trade["latency_ms"] = int(latency * 1000)
        trade["pattern_type"] = result.pattern
        trade["trend_status"] = trend.status
        state_reason = f"{result.signal} @ {trade.get('id')}"
        if latency >= LATENCY_BUDGET_SECONDS:
            trade["latency_violation"] = True
            self._emit("ERROR", {"asset": asset, "reason": "latency exceeded (post-broker round-trip)",
                                  "latency_s": latency})
            state_reason += f" (latency_violation {latency:.2f}s)"
        self._set_asset_state(asset, "IN_TRADE", state_reason)
        self.record_process_ok()
        return self.states[asset]

    def on_trade_closed(self, asset: str, pnl: float, result: str):
        """Call when a trade the state machine placed resolves (WIN/LOSS/EQUAL)."""
        now_th = self._now_fn()
        self.risk_manager.record_order_result(pnl, result, now_th)
        if self.martingale:
            self.martingale.advance(result)
        self._set_asset_state(asset, "IDLE", f"trade closed: {result}")

    def to_state_dict(self) -> dict:
        return {
            "state_machine": {a: {"state": s.state, "since": s.since, "reason": s.reason}
                               for a, s in self.states.items()},
            "global_state": self.global_state,
        }
