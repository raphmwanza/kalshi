"""
Tennis "Set 3 favorite" strategy.

RULE
----
Back a player to win the match when the player we would back has an implied win
probability of 75–90% on Kalshi AND one of these is true:
  * their match has reached the THIRD SET, or
  * they won set 1 and are currently leading set 2 (on track for a
    straight-sets win that never reaches set 3).
Stake is a fraction of total account money (cash + open positions),
where the fraction steps down as the bankroll grows and the bankroll is capped
at $5,000:

    total money < $100      -> stake 1/2   of total money
    total money < $500      -> stake 1/3   of total money
    total money < $1,000    -> stake 1/4   of total money
    total money < $5,000    -> stake 1/5   of total money
    total money >= $5,000   -> stake 1/5   of $5,000  = $1,000

The computed stake is additionally capped by available cash so we never try to
spend money we don't have. As a special case, when available CASH is under $10
the tier fraction is skipped entirely and the whole cash balance is staked.

EXITS
-----
  * Take profit (99c): placed as a RESTING sell "cash-out line" directly on
    Kalshi's book — right after entry and reconciled every cycle. Because it
    lives on the exchange it fills even if this bot is stopped.
  * Stop loss (35c): Kalshi has no native stop/trigger order, so this is
    polled each cycle and only fires WHILE THE BOT IS RUNNING. It sells 100%
    of every tennis position this strategy owns once its bid falls to or
    below 35c. A missing or zero bid (no buyers) does not fire the stop.
    The sell is immediate-or-cancel and reduce-only, so a failed cycle
    cannot leave a resting stop that later flips the account short.

HOW IT WORKS
------------
Kalshi exposes prices but not live match state, so this strategy consults
SofaScore's public live-tennis feed to learn which set each match is in and who
is playing. It then:

  1. Reads account balance to derive total money and the target stake.
  2. Pulls live tennis matches from SofaScore and keeps only those being
     actively played right now (not suspended/retired) that are in set 3+.
  3. Fetches active Kalshi tennis markets (ATP/WTA match-winner markets).
  4. For each market, picks the side priced >= 80 cents (the favorite) and
     confirms that side's player is the one currently in a set-3 match.
  5. Sizes the position with the tiered rule and places a (marketable) limit
     buy on that side, unless we already hold a position / resting order there.

Available via: python cli.py run --tennis
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from src.clients.sofascore_client import (
    LiveTennisMatch,
    SofaScoreClient,
    is_semifinal_or_later,
    names_match,
    players_share_surname,
    round_label_is_late,
)
from src.utils.kalshi_fees import buy_debit_dollars, taker_fee_dollars
from src.utils.market_prices import get_market_prices, is_tradeable_market

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Kalshi series for the tennis SINGLES MATCH-WINNER markets across every tour
# level: ATP/WTA main tour, Challenger, and ITF. These are the series whose
# markets are "Will <player> win the match?" (one single-player market per
# player). We fetch each of these directly.
#
# We deliberately EXCLUDE:
#   * doubles series (KXATPDOUBLES, KXWTADOUBLES, KXITFDOUBLES, KXITFWDOUBLES)
#     — the strategy backs singles only, and our name matcher expects one
#       player per side, and
#   * prop/derivative series that share the same players (KXATPGSPREAD games
#     spread, KXATPEXACTMATCH exact set score, KXATPSETWINNER, KXATPGTOTAL,
#     KXATPRETIRE, ...) so we only ever back "win the match".
MATCH_WINNER_SERIES = (
    "KXATPMATCH",            # ATP main tour
    "KXWTAMATCH",            # WTA main tour
    "KXATPCHALLENGERMATCH",  # ATP Challenger tour
    "KXWTACHALLENGERMATCH",  # WTA Challenger tour
    "KXITFMATCH",            # ITF men's
    "KXITFWMATCH",           # ITF women's
)
# Prefixes used as a secondary guard when filtering nested markets.
MATCH_WINNER_PREFIXES = MATCH_WINNER_SERIES + ("KXTENNISMATCH",)

MIN_WIN_PROBABILITY = 0.75   # buy when implied win chance is at least 75%
MAX_ENTRY_PRICE = 0.90       # cap entry at 90c so there is headroom to the 99c cashout
MIN_SET_TO_ENTER = 3         # only trade once the match reaches set 3
MIN_GAME_LEAD = 2            # backed player must be up by >= 2 games in the live set
BANKROLL_CAP_USD = 5_000.0   # treat any total money above this as exactly this
SELL_ALL_AT = 0.99           # always sell every held position once it hits 99c
STOP_LOSS_AT = 0.35          # cut losses: sell if the live bid falls to/below 35c
# 0 = every tennis position, including small accounts. A positive value would
# again leave stakes under that dollar amount without a stop.
STOP_LOSS_MIN_STAKE_USD = 0.0
ALL_IN_BELOW_CASH_USD = 10.0  # below this cash, skip the fraction and stake it all
# The old "cash < $1, buy the live leader at 25-50c" band was removed. A
# player who is up by two games is the leader; a 25-50c price means the market
# does not agree, so buying them there contradicted the 75-90c rule. Cheap
# entries stay available only through the set-3 underdog overlay, which
# requires a ranked favorite to actually be losing. Sub-$1 cash still buys
# inside the normal band when the price plus the taker fee fits in cash.
# Cash-reserve (PRIORITY over tiered sizing / all-in): never spend so much that
# we'd lose the whole bankroll on a bad streak.
#   cash >= $50 -> keep 70% as cash, spend at most 30%
#   cash >= $20 -> keep 50% as cash, spend at most 50%
#   cash <  $20 -> no extra reserve (existing all-in / tier rules apply)
CASH_RESERVE_HALF_USD = 20.0
CASH_RESERVE_TIGHT_USD = 50.0
CASH_RESERVE_KEEP_HALF = 0.50   # keep this fraction when cash >= $20
CASH_RESERVE_KEEP_TIGHT = 0.70  # keep this fraction when cash >= $50

# --- Set-3 ranking "upset" overlay -----------------------------------------
# When a credible ranked favorite is LOSING to a much lower-ranked / unranked
# player in set 3, the normal "back the favorite" logic is wrong. Instead we
# back the surging UNDERDOG cheaply and ride it to the 99c cash-out — but only
# when the upset is early/credible, otherwise we sit out entirely.
RANK_UPSET_ENABLED = True
RANK_UPSET_MAX_FAV_RANK = 500   # favorite must be ranked <= 500 to be "credible"
RANK_UPSET_MIN_GAP = 150        # underdog must be unranked or >= this many ranks worse
RANK_UPSET_BLOWOUT_GAMES = 3    # if favorite is behind by >= this in the last set: AVOID
# Back the surging underdog in a band centered on ~35c. A floor keeps us out of
# near-dead cheap positions; the ceiling keeps the >=~10-15% headroom philosophy.
UNDERDOG_MIN_ENTRY = 0.25       # don't buy the underdog below 25c (near-dead)
UNDERDOG_MAX_ENTRY = 0.45       # ...or above 45c (value gone) — aim for ~35c

# Risk guard: never hold more than this many open positions at once. Prevents
# the bot from spraying the whole bankroll across every qualifying match as the
# account grows. Bets already placed this cycle count toward the cap.
MAX_CONCURRENT_POSITIONS = 5
# Kalshi's ``occurrence_datetime`` is only a ROUGH scheduled start — for
# Challenger/ITF it's frequently a rounded session block (e.g. 14:00Z) that can
# be LATER than when the match actually starts. SofaScore's live tick + real set
# number is a far more reliable proof that a match is underway (you cannot be in
# set 3 unless it's live), so we do NOT let Kalshi's schedule block a live match.
# The scheduled time is still shown for visibility (the ⏰ line), just not gating.
REQUIRE_KALSHI_START = False


def _kalshi_start_time(market: Dict) -> Optional[datetime]:
    """Parse a Kalshi market's scheduled match start time (UTC).

    Uses ``occurrence_datetime`` only (NOT ``expected_expiration_time``, which
    is an end/settlement estimate). Returns a timezone-aware UTC datetime, or
    None if absent/unparseable. Treated as advisory — see REQUIRE_KALSHI_START.
    """
    raw = market.get("occurrence_datetime")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Position sizing (pure functions — easy to unit test)
# ---------------------------------------------------------------------------

def stake_fraction(total_money: float) -> float:
    """Return the fraction of (capped) bankroll to stake for a given total.

    total < 100  -> 1/2
    total < 500  -> 1/3
    total < 1000 -> 1/4
    otherwise    -> 1/5   (also applies once bankroll is capped at $5,000)
    """
    if total_money < 100:
        return 1.0 / 2.0
    if total_money < 500:
        return 1.0 / 3.0
    if total_money < 1_000:
        return 1.0 / 4.0
    return 1.0 / 5.0


def target_stake_usd(total_money: float) -> float:
    """Dollar amount to stake per the tiered rule, with the $5k bankroll cap."""
    capped = min(max(total_money, 0.0), BANKROLL_CAP_USD)
    return capped * stake_fraction(total_money)


def contracts_for_stake(stake_usd: float, price_dollars: float, available_cash: float) -> int:
    """How many contracts to buy given a target stake and per-contract price.

    The spend, including Kalshi's taker fee, is capped by available cash so an
    "all cash" order is not rejected for insufficient balance.
    """
    if price_dollars <= 0:
        return 0
    budget = min(max(stake_usd, 0.0), max(available_cash, 0.0))
    if budget <= 0:
        return 0
    count = int(budget // price_dollars)
    while count > 0 and buy_debit_dollars(count, price_dollars) > budget + 1e-9:
        count -= 1
    return count


def cash_keep_fraction(
    cash: float,
    half_at: float = CASH_RESERVE_HALF_USD,
    tight_at: float = CASH_RESERVE_TIGHT_USD,
) -> float:
    """Fraction of cash that MUST stay unspent. Takes priority over sizing.

    cash >= $50 -> keep 70% (spend at most 30%)
    cash >= $20 -> keep 50% (spend at most 50%)
    otherwise   -> keep 0%  (all-in / tiered rules apply as before)
    """
    if cash >= tight_at:
        return CASH_RESERVE_KEEP_TIGHT
    if cash >= half_at:
        return CASH_RESERVE_KEEP_HALF
    return 0.0


def max_spendable_cash(
    cash: float,
    half_at: float = CASH_RESERVE_HALF_USD,
    tight_at: float = CASH_RESERVE_TIGHT_USD,
) -> float:
    """Hard cap on how much of ``cash`` may be deployed this cycle."""
    keep = cash_keep_fraction(cash, half_at=half_at, tight_at=tight_at)
    return max(0.0, cash * (1.0 - keep))


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

def _pairing_matches(player: str, opponent: str, match: LiveTennisMatch) -> bool:
    """True when Kalshi's two names are exactly this live match's two players.

    If either pair shares a surname, a surname-only hit is not enough: the
    given name or initial has to pick one orientation, and that orientation
    has to be unique. Otherwise the match is skipped.
    """
    collision = players_share_surname(match.home_name, match.away_name) or (
        players_share_surname(player, opponent)
    )
    allow = not collision

    def hit(left: str, right: str) -> bool:
        return names_match(left, right, allow_surname_only=allow)

    home_player = hit(player, match.home_name) and hit(opponent, match.away_name)
    away_player = hit(player, match.away_name) and hit(opponent, match.home_name)
    if collision:
        if home_player and (hit(player, match.away_name) or hit(opponent, match.home_name)):
            return False
        if away_player and (hit(player, match.home_name) or hit(opponent, match.away_name)):
            return False
    return home_player ^ away_player


def _is_match_winner_ticker(ticker: str) -> bool:
    t = (ticker or "").upper()
    return any(t.startswith(p) for p in MATCH_WINNER_PREFIXES)


def _is_match_winner_market(market: Dict) -> bool:
    """True only for tennis MATCH-WINNER markets (KXATPMATCH / KXWTAMATCH).

    Prop/derivative markets (games spread, exact set score, set spread,
    retirement, etc.) are excluded even though they share the same players, so
    the strategy only ever backs a player to win the match.
    """
    return _is_match_winner_ticker(market.get("ticker") or "")


def _fp_count(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def classify_tennis_settlement(settlement: Dict) -> Optional[str]:
    """Return 'win' or 'loss' for a directional tennis match-winner settlement.

    Hedged / mixed YES+NO positions are skipped (None) so they do not distort
    the daily win percentage.
    """
    ticker = settlement.get("ticker") or ""
    if not _is_match_winner_ticker(ticker):
        return None
    yes = _fp_count(settlement.get("yes_count_fp") or settlement.get("yes_count"))
    no = _fp_count(settlement.get("no_count_fp") or settlement.get("no_count"))
    result = (settlement.get("market_result") or "").strip().lower()
    if yes >= 0.5 and no >= 0.5:
        return None
    if yes >= 0.5:
        if result == "yes":
            return "win"
        if result == "no":
            return "loss"
        return None
    if no >= 0.5:
        if result == "no":
            return "win"
        if result == "yes":
            return "loss"
    return None


def _settlement_local_date(settlement: Dict):
    raw = settlement.get("settled_time") or ""
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().date()


def format_daily_win_line(record: Dict) -> str:
    """Human-readable daily W–L line for cycle logs."""
    wins = int(record.get("wins") or 0)
    losses = int(record.get("losses") or 0)
    n = wins + losses
    if n == 0:
        return "📈 Today: no settled tennis bets yet"
    pct = 100.0 * wins / n
    line = f"📈 Today: {wins}W–{losses}L ({pct:.0f}% win)"
    last_loss = record.get("last_loss")
    if last_loss:
        line += f"  | last loss: {last_loss}"
    return line


def _favorite_side(
    market: Dict,
    min_prob: float = MIN_WIN_PROBABILITY,
    max_price: float = MAX_ENTRY_PRICE,
) -> Optional[Tuple[str, str, float]]:
    """Decide whether to back THIS market's player to win.

    Each Kalshi tennis match-winner event is split into one single-player
    market per player ("Will <player> win?"), so we only ever back YES (the
    player winning). We back it when that player's YES ask — their implied win
    probability — sits in the entry band. The opponent has their own market
    whose YES ask is the complement (~15-20%), so it won't trigger, and we
    never double-back the same match.

    Returns ("yes", player_name, yes_ask) or None.
    """
    _, yes_ask, _, _ = get_market_prices(market)
    if yes_ask and min_prob <= yes_ask <= max_price:
        player = market.get("yes_sub_title") or market.get("title") or ""
        if player:
            return ("yes", player, yes_ask)
    return None


def _position_qty(p: Dict) -> int:
    """Signed contract count from a Kalshi V2 market_position (+YES / -NO).

    Kalshi's V2 positions endpoint reports quantity as the fixed-point string
    ``position_fp`` (e.g. "7.00"), NOT the legacy integer ``position`` field.
    Reading the wrong key silently yields 0, which makes the position manager
    (take-profit + stop-loss) think there's nothing to manage.
    """
    raw = p.get("position_fp")
    if raw is None:
        raw = p.get("position")  # legacy fallback
    try:
        return round(float(raw))
    except (TypeError, ValueError):
        return 0


def _price_cents(order: Dict, dollar_key: str, cent_key: str) -> Optional[int]:
    """Read a YES or NO price off an order, in cents."""
    raw = order.get(dollar_key)
    if raw is not None and raw != "":
        try:
            return int(round(float(raw) * 100))
        except (TypeError, ValueError):
            pass
    raw = order.get(cent_key)
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return int(round(value * 100)) if value <= 1.0 else int(round(value))


def _order_yes_cents(order: Dict) -> Optional[int]:
    """YES-side price of a Kalshi order in cents, or None if unknown."""
    return _price_cents(order, "yes_price_dollars", "yes_price")


def _order_no_cents(order: Dict) -> Optional[int]:
    """NO-side price in cents, or None if the order doesn't carry one."""
    return _price_cents(order, "no_price_dollars", "no_price")


def _order_exits_position(order: Dict, position_side: str) -> bool:
    """True when ``order`` reduces a long YES or long NO position.

    Selling YES is an ask on the YES book. Selling NO is a bid on the YES
    book (buy YES at ``1 - price``). Legacy ``action`` + ``side`` are used
    when present. ``outcome_side`` is not the contract side (sell-yes is
    reported as outcome ``no``), so it is not used here.
    """
    action = str(order.get("action") or "").lower()
    contract = str(order.get("side") or "").lower()
    if action in ("buy", "sell") and contract in ("yes", "no"):
        return action == "sell" and contract == position_side
    book = str(order.get("book_side") or "").lower()
    if position_side == "yes":
        return book == "ask"
    if position_side == "no":
        return book == "bid"
    return False


def _exit_price_cents(order: Dict, position_side: str) -> Optional[int]:
    """Limit price of an exit, in cents on the position's own side.

    A 99c NO sell is quoted at 1c on the YES book. Comparing that YES price
    to 99c never matches, so the bot used to cancel it and place another
    every cycle.
    """
    if position_side == "yes":
        return _order_yes_cents(order)
    no_cents = _order_no_cents(order)
    if no_cents is not None:
        return no_cents
    yes_cents = _order_yes_cents(order)
    if yes_cents is None:
        return None
    return 100 - yes_cents


def _remaining_order_qty(order: Dict) -> int:
    raw = order.get("remaining_count_fp", order.get("remaining_count"))
    try:
        return round(float(raw))
    except (TypeError, ValueError):
        return 0


def _has_bid(bid: Optional[float]) -> bool:
    """False when the book has no buyers (missing quote or a zero bid)."""
    return bid is not None and bid > 0


def _covered_exit_qty(
    orders: List[Dict], ticker: str, position_side: str, target_cents: int
) -> int:
    """Contracts already covered by a resting exit at the take-profit price."""
    total = 0
    for order in orders:
        if order.get("ticker") != ticker:
            continue
        if not _order_exits_position(order, position_side):
            continue
        if _exit_price_cents(order, position_side) != target_cents:
            continue
        qty = _remaining_order_qty(order)
        if qty > 0:
            total += qty
    return total


def _position_stake_usd(p: Dict) -> float:
    """Dollars committed to a Kalshi V2 market_position (cost basis)."""
    val = p.get("market_exposure_dollars")
    if val is None:
        val = p.get("total_traded_dollars")
    if val is not None:
        try:
            return float(val)
        except (TypeError, ValueError):
            pass
    # Legacy cents fields (older API shape).
    try:
        return float(p.get("market_exposure") or p.get("total_traded") or 0) / 100.0
    except (TypeError, ValueError):
        return 0.0


class TennisSet3Strategy:
    """Buy the set-3 favorite (75–90% implied) with tiered position sizing."""

    def __init__(
        self,
        client,  # KalshiClient instance
        sofascore: Optional[SofaScoreClient] = None,
        dry_run: bool = True,
        min_win_probability: float = MIN_WIN_PROBABILITY,
        max_entry_price: float = MAX_ENTRY_PRICE,
        min_set: int = MIN_SET_TO_ENTER,
        min_game_lead: int = MIN_GAME_LEAD,
        best_of_three_only: bool = False,
        sell_all_at: float = SELL_ALL_AT,
        stop_loss_at: float = STOP_LOSS_AT,
        stop_loss_min_stake: float = STOP_LOSS_MIN_STAKE_USD,
        all_in_below_cash: float = ALL_IN_BELOW_CASH_USD,
        include_two_set_sweep: bool = True,
        rank_upset_enabled: bool = RANK_UPSET_ENABLED,
        underdog_min_entry: float = UNDERDOG_MIN_ENTRY,
        underdog_max_entry: float = UNDERDOG_MAX_ENTRY,
        max_concurrent_positions: int = MAX_CONCURRENT_POSITIONS,
        cash_reserve_half_usd: float = CASH_RESERVE_HALF_USD,
        cash_reserve_tight_usd: float = CASH_RESERVE_TIGHT_USD,
        account_name: str = "default",
        journal=None,
    ):
        self.client = client
        self.sofascore = sofascore or SofaScoreClient()
        self._owns_sofascore = sofascore is None
        self.dry_run = dry_run
        self.account_name = account_name or "default"
        self.journal = journal
        self.min_win_probability = min_win_probability
        self.max_entry_price = max_entry_price
        self.min_set = min_set
        self.min_game_lead = min_game_lead
        self.best_of_three_only = best_of_three_only
        self.sell_all_at = sell_all_at
        self.stop_loss_at = stop_loss_at
        self.stop_loss_min_stake = stop_loss_min_stake
        self.all_in_below_cash = all_in_below_cash
        self.include_two_set_sweep = include_two_set_sweep
        self.rank_upset_enabled = rank_upset_enabled
        self.underdog_min_entry = underdog_min_entry
        self.underdog_max_entry = underdog_max_entry
        self.max_concurrent_positions = max_concurrent_positions
        self.cash_reserve_half_usd = cash_reserve_half_usd
        self.cash_reserve_tight_usd = cash_reserve_tight_usd
        self._daily_record_cache: Optional[Tuple[float, Dict]] = None

    async def run(
        self,
        dry_run: Optional[bool] = None,
        live_matches: Optional[List[LiveTennisMatch]] = None,
    ) -> Dict:
        if dry_run is not None:
            self.dry_run = dry_run

        start = time.time()
        logger.info("=" * 70)
        logger.info("TENNIS SET-3 FAVORITE STRATEGY [%s]", self.account_name)
        logger.info(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        logger.info(
            "Rule: back player in set %d+ (or 2-0 sweep) only if they lead "
            "the live set by >= %d games and implied win prob %.0f-%.0f%% | "
            "cash out @ %.0fc | tiered sizing (cap $%.0f)",
            self.min_set, self.min_game_lead,
            self.min_win_probability * 100, self.max_entry_price * 100,
            self.sell_all_at * 100, BANKROLL_CAP_USD,
        )
        logger.info(
            "Cash reserve PRIORITY: keep 50%% of cash at $%.0f+, keep 70%% at $%.0f+",
            self.cash_reserve_half_usd, self.cash_reserve_tight_usd,
        )
        if self.rank_upset_enabled:
            logger.info(
                "Set-3 ranking overlay ON: back the surging underdog (<= %.0fc) "
                "when a rank<=%d favorite is losing to an unranked/>=%d-lower "
                "opponent; avoid if favorite rank>%d or behind by >=%d in the last set",
                self.underdog_max_entry * 100, RANK_UPSET_MAX_FAV_RANK,
                RANK_UPSET_MIN_GAP, RANK_UPSET_MAX_FAV_RANK, RANK_UPSET_BLOWOUT_GAMES,
            )
        logger.info("=" * 70)

        stats = {
            "live_matches": 0,
            "actionable_matches": 0,
            "tennis_markets": 0,
            "candidates": 0,
            "underdog_candidates": 0,
            "skipped_upset_avoid": 0,
            "placed": 0,
            "filled": 0,
            "skipped_existing": 0,
            "skipped_no_match": 0,
            "skipped_not_started": 0,
            "skipped_late_round": 0,
            "errors": 0,
            "total_deployed": 0.0,
            "tp_lines_set": 0,
            "stopped_positions": 0,
            "stopped_contracts": 0,
        }

        # 0. Manage open positions first, every cycle, before anything else:
        #    - take profit: ensure a resting SELL "cash-out line" at 99c sits on
        #      Kalshi for every position, so it fills even if this code is off.
        #    - stop loss: Kalshi has no native stop, so we poll — sell any
        #      tennis position whose live bid has fallen to/below 35c. A missing
        #      or zero bid does not fire the stop.
        await self._manage_open_positions(stats)

        # 1. Bankroll + target stake
        bal = await self.client.get_balance()
        cash = float(bal.get("balance", 0)) / 100.0
        portfolio = float(bal.get("portfolio_value", 0)) / 100.0
        total_money = cash + portfolio
        starting_cash = cash

        # Cash-reserve is PRIORITY: lock how much we may spend this cycle so a
        # losing streak can't drain the account. Computed once from starting
        # cash so a mid-cycle drop doesn't flip us into all-in.
        keep_frac = cash_keep_fraction(
            starting_cash,
            half_at=self.cash_reserve_half_usd,
            tight_at=self.cash_reserve_tight_usd,
        )
        spendable = max_spendable_cash(
            starting_cash,
            half_at=self.cash_reserve_half_usd,
            tight_at=self.cash_reserve_tight_usd,
        )

        # When cash is very small, dividing by the tier fraction leaves too
        # little to trade — go all-in on available cash instead of a fraction.
        # The reserve (cash >= $20) overrides this: we never all-in past it.
        all_in = cash < self.all_in_below_cash and keep_frac == 0.0
        if all_in:
            stake = cash
        else:
            stake = target_stake_usd(total_money)
        # Per-trade stake cannot exceed the cycle spend cap.
        stake = min(stake, spendable, cash)

        print(
            f"\n👤 {self.account_name}  💰 Cash: ${cash:,.2f} | Positions: ${portfolio:,.2f} | "
            f"Total: ${total_money:,.2f}",
            flush=True,
        )
        await self._print_daily_win_pct()
        if keep_frac > 0:
            print(
                f"🛡️ Cash reserve PRIORITY: keep {keep_frac*100:.0f}% "
                f"(${starting_cash * keep_frac:,.2f}) — max deploy this cycle "
                f"${spendable:,.2f} of ${starting_cash:,.2f}",
                flush=True,
            )
        if all_in:
            print(
                f"🎯 Cash < ${self.all_in_below_cash:,.0f}: going all-in — "
                f"stake = ${stake:,.2f} (whole cash balance)\n",
                flush=True,
            )
        else:
            print(
                f"🎯 Tier stake: {stake_fraction(total_money):.3f} of "
                f"${min(total_money, BANKROLL_CAP_USD):,.2f} = ${stake:,.2f} per trade "
                f"(capped by cash + reserve)\n",
                flush=True,
            )

        # 2. Live tennis matches that are actionable right now: either in
        #    set 3+, OR a player won set 1 and is currently leading set 2
        #    (on track for a straight-sets win that never reaches set 3).
        if live_matches is None:
            print("📡 Fetching live tennis matches from SofaScore...", flush=True)
            live_matches = await self.sofascore.get_live_tennis_matches()
        else:
            print("📡 Using the shared SofaScore snapshot for this cycle...", flush=True)
        stats["live_matches"] = len(live_matches)
        actionable_matches = [m for m in live_matches if self._match_actionable(m)]
        stats["actionable_matches"] = len(actionable_matches)
        print(
            f"  {len(live_matches)} live, {len(actionable_matches)} actionable "
            f"(set {self.min_set}+ or 2-0 in progress)",
            flush=True,
        )
        for m in actionable_matches:
            print(
                f"   • {m.home_name} vs {m.away_name} — set {m.current_set} "
                f"({m.score_label()}) ({m.status_description})",
                flush=True,
            )

        if not actionable_matches:
            waiting = [
                m for m in live_matches
                if m.is_live and " / " not in (m.home_name or "") and " / " not in (m.away_name or "")
            ]
            if waiting:
                print("  Waiting (need up-by-2 in the live set):", flush=True)
                for m in waiting:
                    print(
                        f"   • {m.home_name} vs {m.away_name} — set {m.current_set} "
                        f"({m.score_label()}) — {self._why_not_actionable(m)}",
                        flush=True,
                    )
            print("\nNo actionable tennis matches right now. Nothing to do.\n", flush=True)
            return await self._finish(stats, start)

        # 3. Kalshi tennis MATCH-WINNER markets (no prop/spread markets)
        print("\n📊 Fetching Kalshi tennis match-winner markets...", flush=True)
        markets = await self._fetch_tennis_markets()
        stats["tennis_markets"] = len(markets)
        print(f"  {len(markets)} active match-winner markets", flush=True)

        # Start-time visibility: how many Kalshi matches are underway (scheduled
        # start already passed) vs still upcoming, and when the next one starts.
        # Dedupe by event so we count matches, not the two per-player markets.
        now_utc = datetime.now(timezone.utc)
        starts_by_event: Dict[str, datetime] = {}
        for m in markets:
            dt = _kalshi_start_time(m)
            if dt is not None:
                starts_by_event.setdefault(m.get("event_ticker", m.get("ticker", "")), dt)
        starts = list(starts_by_event.values())
        underway = sum(1 for dt in starts if dt <= now_utc)
        upcoming = [dt for dt in starts if dt > now_utc]
        msg = f"  ⏰ {underway} match-market(s) past scheduled start, {len(upcoming)} upcoming"
        if upcoming:
            nxt = min(upcoming)
            mins = (nxt - now_utc).total_seconds() / 60.0
            msg += f" — next start {nxt:%H:%M} UTC (in {mins/60:.0f}h {mins % 60:.0f}m)"
        print(msg, flush=True)

        # Tracking visibility: which Kalshi-listed matches are live on SofaScore
        # RIGHT NOW (regardless of set), so you can watch the matcher lock onto
        # real games and see exactly what it's waiting on before set 3.
        tracked: Dict[str, LiveTennisMatch] = {}
        for m in markets:
            ev = m.get("event_ticker", m.get("ticker", ""))
            if ev in tracked:
                continue
            lm = self._live_match_for_market(m, live_matches)
            if lm is not None:
                tracked[ev] = lm
        if tracked:
            print(f"\n👀 {len(tracked)} Kalshi-listed match(es) live on SofaScore now:", flush=True)
            for lm in tracked.values():
                if self._match_actionable(lm):
                    tag = f"ACTIONABLE (set {lm.current_set})"
                else:
                    tag = f"set {lm.current_set} — waiting for set {self.min_set}"
                print(f"   • {lm.home_name} vs {lm.away_name} [{tag}]", flush=True)
        else:
            print(
                "\n👀 No Kalshi-listed match is live on SofaScore yet "
                "(waiting for the Kalshi slate to start).",
                flush=True,
            )

        # 4. Existing exposure (skip tickers we already touch). If either
        # lookup fails, do not enter: an empty result would disable the
        # duplicate check and the position cap.
        exposure = await self._load_exposure()
        if exposure is None:
            stats["errors"] += 1
            stats["entries_blocked"] = 1
            stats["needs_backoff"] = True
            print(
                "🛑 Positions or orders lookup failed — skipping new entries this cycle",
                flush=True,
            )
            return await self._finish(stats, start)
        held, open_count = exposure
        traded_events: set = set()  # avoid two bets on the same match this cycle
        # Concurrency guard: how many positions are already open, plus how many
        # new bets we place this cycle, must stay under the cap.
        bets_this_cycle = 0
        if open_count >= self.max_concurrent_positions:
            print(
                f"  🧢 Already at max concurrent positions "
                f"({open_count}/{self.max_concurrent_positions}) — managing only, no new entries",
                flush=True,
            )

        # 5. Evaluate + place
        for snapshot in markets:
            ticker = snapshot.get("ticker", "")

            # Cheap pre-filter: only spend an API call on markets whose TWO
            # players line up with the two players of one actionable live match
            # (bijective — kills first-name / same-surname false positives).
            match = self._live_match_for_market(snapshot, actionable_matches)
            if match is None:
                stats["skipped_no_match"] += 1
                continue

            # Re-fetch fresh so the entry price reflects the live in-match market,
            # not the (possibly stale/partial) bulk events snapshot.
            market = await self._fresh_market(ticker) or snapshot
            market.setdefault("_event_category", snapshot.get("_event_category", ""))

            # The Kalshi market must still be open/trading — never try to enter a
            # market that has already closed or settled (the game is over).
            status = (market.get("status") or "").lower()
            if status in ("closed", "settled", "finalized", "determined", "inactive"):
                stats["skipped_no_match"] += 1
                continue

            # Start-time gate: only bet once Kalshi's scheduled match start has
            # passed. Together with SofaScore's live tick (already required for a
            # match to be "actionable") this double-confirms the match is truly
            # underway — not merely an open market for a game later today.
            if REQUIRE_KALSHI_START:
                start_dt = _kalshi_start_time(market)
                if start_dt is not None and datetime.now(timezone.utc) < start_dt:
                    stats["skipped_not_started"] += 1
                    print(
                        f"  ⏳ {ticker}: Kalshi start {start_dt:%Y-%m-%d %H:%M} UTC "
                        f"not reached — skip",
                        flush=True,
                    )
                    continue

            if not is_tradeable_market(market):
                continue

            player = market.get("yes_sub_title") or market.get("title") or ""
            if not player:
                continue
            opponent = market.get("_opponent") or ""
            _, yes_ask, _, _ = get_market_prices(market)
            if not yes_ask:
                continue
            side = "yes"
            in_set3 = match.current_set >= self.min_set
            if self._is_late_round(match, market):
                stats["skipped_late_round"] += 1
                print(
                    f"  ⏭️  {ticker}: {match.round_name or 'semifinal or later'} "
                    f"— skip semifinal and above",
                    flush=True,
                )
                await self._log_signal(
                    ticker, "late_round", player, side, yes_ask, 0, match,
                    "skip", "semifinal_or_later",
                )
                continue

            # --- Set-3 ranking "upset" overlay (takes PRIORITY) ---------------
            # If a credible ranked favorite is losing to a much lower/unranked
            # opponent, back the surging underdog (~35c) instead of the favorite,
            # or sit out entirely when it's not a clean early upset.
            action = "normal"
            if in_set3 and self.rank_upset_enabled and opponent:
                action = self._rank_upset_action(match, player, opponent)

            if action == "avoid":
                stats["skipped_upset_avoid"] += 1
                print(
                    f"  🚫 {ticker}: ranked favorite collapsing / not credible "
                    f"— skip per ranking rule",
                    flush=True,
                )
                continue
            if action == "skip_fav":
                # The underdog gets backed through its own market instead.
                continue

            if action == "back_dog":
                if not match.leading_set_by(player, match.current_set, self.min_game_lead):
                    print(
                        f"  ⏭️  {ticker}: underdog {player} not up by "
                        f"{self.min_game_lead} in set {match.current_set} — skip",
                        flush=True,
                    )
                    continue
                if not (self.underdog_min_entry <= yes_ask <= self.underdog_max_entry):
                    print(
                        f"  ⏭️  {ticker}: underdog ask ${yes_ask:.2f} outside "
                        f"${self.underdog_min_entry:.2f}-${self.underdog_max_entry:.2f} "
                        f"target band — skip",
                        flush=True,
                    )
                    continue
                ask = yes_ask
                rule = "underdog"
                stats["underdog_candidates"] += 1
                print(
                    f"  🐕 {ticker}: backing UNDERDOG {player} @ ${yes_ask:.2f} "
                    f"(ranked-favorite upset, set {match.current_set})",
                    flush=True,
                )
            else:
                # Standard rule: back the 75-90% favorite ONLY if THIS player
                # is up by >= min_game_lead in the live set. Set 3 is not a
                # free pass — 1-1 in sets with a 1-game lead does not qualify.
                if in_set3:
                    if not match.leading_set_by(
                        player, match.current_set, self.min_game_lead
                    ):
                        stats["skipped_no_match"] += 1
                        continue
                elif (
                    self.include_two_set_sweep
                    and match.two_set_sweep_in_progress(player, self.min_game_lead)
                ):
                    pass  # won set 1 AND up by 2+ games in set 2
                else:
                    stats["skipped_no_match"] += 1
                    continue
                lo, hi = self.min_win_probability, self.max_entry_price
                if not (lo <= yes_ask <= hi):
                    continue
                ask = yes_ask
                rule = "set3_favorite" if in_set3 else "sweep_favorite"
                stats["candidates"] += 1

            if ticker in held:
                stats["skipped_existing"] += 1
                print(f"  ⏭️  {ticker}: already holding/resting — skip", flush=True)
                await self._log_signal(ticker, rule, player, side, ask, 0, match, "skip", "existing")
                continue

            # Don't place two bets on the same underlying match this cycle.
            event = market.get("event_ticker", "")
            if event and event in traded_events:
                stats["skipped_existing"] += 1
                print(f"  ⏭️  {ticker}: same match already bet this cycle — skip", flush=True)
                await self._log_signal(ticker, rule, player, side, ask, 0, match, "skip", "same_match")
                continue

            # Concurrency cap: stop opening new positions once we'd exceed it.
            if open_count + bets_this_cycle >= self.max_concurrent_positions:
                print(
                    f"  🧢 Max concurrent positions "
                    f"({self.max_concurrent_positions}) reached — stop entering this cycle",
                    flush=True,
                )
                await self._log_signal(ticker, rule, player, side, ask, 0, match, "skip", "position_cap")
                break

            # Cash-reserve cap: stop opening new bets once we'd dip below the
            # required cash floor (50% at $20+, 70% at $50+). The taker fee
            # on one contract has to fit as well.
            unit_cost = ask + taker_fee_dollars(1, ask)
            if keep_frac > 0 and spendable + 1e-9 < unit_cost:
                print(
                    f"  🛡️ Cash reserve reached — keeping "
                    f"${starting_cash * keep_frac:,.2f} unspent, stop entering this cycle",
                    flush=True,
                )
                await self._log_signal(ticker, rule, player, side, ask, 0, match, "skip", "cash_reserve")
                break

            count = contracts_for_stake(stake, ask, min(cash, spendable))
            if count < 1:
                print(
                    f"  ⚠️  {ticker}: stake ${stake:,.2f} too small for ask "
                    f"${ask:.2f} plus taker fee "
                    f"(spendable ${min(cash, spendable):,.2f}) — skip",
                    flush=True,
                )
                await self._log_signal(ticker, rule, player, side, ask, 0, match, "skip", "size")
                continue

            await self._log_signal(
                ticker, rule, player, side, ask, count, match, "enter", rule,
            )
            await self._place(market, side, player, ask, count, match, stats, rule=rule)
            held.add(ticker)
            bets_this_cycle += 1
            if event:
                traded_events.add(event)
            # Reduce remaining cash AND the cycle spend budget so multiple
            # trades cannot breach the reserve. The taker fee is reserved too,
            # otherwise the next "all cash" order is rejected.
            debit = buy_debit_dollars(count, ask)
            cash = max(0.0, cash - debit)
            spendable = max(0.0, spendable - debit)

        return await self._finish(stats, start)

    async def _finish(self, stats: Dict, start: float) -> Dict:
        elapsed = time.time() - start
        print(f"\n{'='*70}", flush=True)
        print("🎾 TENNIS SET-3 REPORT", flush=True)
        print(f"{'='*70}", flush=True)
        print(f"  Take-profit lines @ {self.sell_all_at*100:.0f}c: {stats['tp_lines_set']} set on Kalshi", flush=True)
        print(f"  Stop-loss @ {self.stop_loss_at*100:.0f}c:      {stats['stopped_positions']} pos / {stats['stopped_contracts']} contracts", flush=True)
        print(f"  Live matches:        {stats['live_matches']}", flush=True)
        print(f"  Actionable:          {stats['actionable_matches']}", flush=True)
        print(f"  Tennis markets:      {stats['tennis_markets']}", flush=True)
        print(f"  Candidates (matched):{stats['candidates']}", flush=True)
        print(f"  Underdog candidates: {stats['underdog_candidates']}", flush=True)
        print(f"  Skipped (upset avoid):{stats['skipped_upset_avoid']}", flush=True)
        print(f"  Skipped (not started):{stats['skipped_not_started']}", flush=True)
        print(f"  Skipped (SF/final):  {stats.get('skipped_late_round', 0)}", flush=True)
        print(f"  Orders placed:       {stats['placed']}", flush=True)
        print(f"  Instantly filled:    {stats['filled']}", flush=True)
        print(f"  Skipped (existing):  {stats['skipped_existing']}", flush=True)
        print(f"  Capital deployed:    ${stats['total_deployed']:,.2f}", flush=True)
        print(f"  Errors:              {stats['errors']}", flush=True)
        print(f"  Mode:                {'DRY RUN' if self.dry_run else 'LIVE'}", flush=True)
        print(f"  Elapsed:             {elapsed:.0f}s", flush=True)
        rec = await self._daily_record()
        if rec is not None:
            print(f"  {format_daily_win_line(rec)}", flush=True)
        print(f"{'='*70}\n", flush=True)
        if getattr(self.sofascore, "last_fetch_blocked", False) or getattr(
            self.client, "hit_rate_limit", False
        ):
            stats["needs_backoff"] = True
        await self._record_sell_fills()
        if self._owns_sofascore:
            await self.sofascore.close()
        return stats

    async def _print_daily_win_pct(self) -> None:
        rec = await self._daily_record()
        if rec is None:
            return
        line = format_daily_win_line(rec)
        print(line, flush=True)
        logger.info(line)

    async def _daily_record(self) -> Optional[Dict]:
        """Today's settled tennis W–L from Kalshi settlements (local date)."""
        now = time.monotonic()
        cached = self._daily_record_cache
        if cached and (now - cached[0]) < 60.0:
            return cached[1]
        try:
            record = await self._compute_daily_record()
        except Exception as exc:
            logger.warning("Could not load daily tennis win %%: %s", exc)
            return cached[1] if cached else None
        self._daily_record_cache = (now, record)
        return record

    async def _compute_daily_record(self) -> Dict:
        today = datetime.now().astimezone().date()
        wins = 0
        losses = 0
        last_loss = ""
        cursor: Optional[str] = None
        for _ in range(5):
            resp = await self.client.get_settlements(limit=100, cursor=cursor)
            rows = resp.get("settlements") or []
            if not rows:
                break
            saw_today = False
            for s in rows:
                outcome = classify_tennis_settlement(s)
                if outcome in ("win", "loss"):
                    await self._log_settlement(s, outcome)
                local_d = _settlement_local_date(s)
                if local_d is None:
                    continue
                if local_d != today:
                    continue
                saw_today = True
                if outcome == "win":
                    wins += 1
                elif outcome == "loss":
                    losses += 1
                    if not last_loss:
                        ticker = s.get("ticker") or ""
                        last_loss = ticker.split("-", 1)[-1] if ticker else ""
            if not saw_today and not cursor:
                # First page is already older than today.
                break
            if not saw_today:
                break
            cursor = resp.get("cursor") or None
            if not cursor:
                break
        return {"wins": wins, "losses": losses, "last_loss": last_loss}

    def _live_match_for_market(
        self, market: Dict, matches: List[LiveTennisMatch]
    ) -> Optional[LiveTennisMatch]:
        """Return the live match that is the SAME match as this Kalshi market.

        A Kalshi match-winner market is a single-player market ("Will <player>
        win?"); the opponent comes from the sibling market in the same event
        (annotated as ``_opponent`` at fetch time). We require BOTH the market's
        player AND its opponent to line up (bijective surname match, either
        orientation) with a live match's two players. Requiring both prevents
        backing the wrong player when names collide (shared first name, common
        surname, or a doubles team containing a matching surname).
        """
        player = market.get("yes_sub_title") or market.get("title") or ""
        opponent = market.get("_opponent") or ""
        if not player or not opponent:
            return None
        for m in matches:
            if _pairing_matches(player, opponent, m):
                return m
        return None

    def _rank_upset_action(
        self, match: LiveTennisMatch, player: str, opponent: str
    ) -> str:
        """Set-3 ranking overlay. PRIORITY over the plain favorite rule.

        Scenario: a credible ranked favorite is LOSING to a much lower-ranked or
        unranked underdog. Returns one of:
          * "normal"   — no qualifying upset; fall back to the standard logic.
          * "avoid"    — favorite not credible (rank > cap) or already blown out
                         (behind >= N in the last set): sit this match out.
          * "skip_fav" — this market is the losing favorite in a backable upset;
                         skip it (we back the underdog via its own market).
          * "back_dog" — this market IS the surging underdog; back it (~35c band).
        """
        r_player = match.ranking_of(player)
        r_opp = match.ranking_of(opponent)

        def rk(r: Optional[int]) -> int:
            return r if r is not None else 10 ** 9

        # Need a distinguishable, ranked favorite (the "ranked opponent").
        if rk(r_player) == rk(r_opp):
            return "normal"
        player_is_fav = rk(r_player) < rk(r_opp)
        fav_name = player if player_is_fav else opponent
        dog_name = opponent if player_is_fav else player
        r_fav = r_player if player_is_fav else r_opp
        r_dog = r_opp if player_is_fav else r_player
        if r_fav is None:
            return "normal"

        # Underdog must be UNRANKED or >= MIN_GAP ranks worse than the favorite.
        gap_ok = (r_dog is None) or (r_dog - r_fav >= RANK_UPSET_MIN_GAP)
        if not gap_ok:
            return "normal"

        # Favorite must be LOSING: behind on games in the current set, or down a set.
        deficit = match.games_deficit_current_set(fav_name)  # + => fav behind
        fav_sets = match.sets_won_by(fav_name) or 0
        dog_sets = match.sets_won_by(dog_name) or 0
        fav_down_set = dog_sets > fav_sets
        fav_behind_games = deficit is not None and deficit > 0
        if not (fav_behind_games or fav_down_set):
            return "normal"

        # Non-credible favorite (rank worse than the cap) -> sit out entirely.
        if r_fav > RANK_UPSET_MAX_FAV_RANK:
            return "avoid"
        # Blown out in the last set (behind by >= N games) -> no value -> sit out.
        if deficit is not None and deficit >= RANK_UPSET_BLOWOUT_GAMES:
            return "avoid"

        # Backable early upset: back the underdog; skip the favorite's own market.
        return "back_dog" if dog_name == player else "skip_fav"

    def _match_actionable(self, m: LiveTennisMatch) -> bool:
        """A match is actionable if it's LIVE right now (actively being played,
        not suspended/retired) AND either it's in set 3+, or a player won set 1
        and is currently leading set 2 (an in-progress straight-sets sweep)."""
        if not m.is_live:
            return False
        # Doubles teams show as "Name / Name" on SofaScore. This strategy only
        # backs singles match-winner markets, so doubles are never tradeable.
        if " / " in (m.home_name or "") or " / " in (m.away_name or ""):
            return False
        if m.current_set >= self.min_set and (
            not self.best_of_three_only or m.current_set == 3
        ):
            # Set 3+ is only actionable if someone is actually up by 2 games
            # in the live set — not merely because the match reached set 3.
            return (
                m.leading_set_by(m.home_name, m.current_set, self.min_game_lead)
                or m.leading_set_by(m.away_name, m.current_set, self.min_game_lead)
            )
        if self.include_two_set_sweep and (
            m.two_set_sweep_in_progress(m.home_name, self.min_game_lead)
            or m.two_set_sweep_in_progress(m.away_name, self.min_game_lead)
        ):
            return True
        return False

    def _why_not_actionable(self, m: LiveTennisMatch) -> str:
        """Short reason a live singles match is not yet a bet."""
        if m.current_set < 2:
            return "still set 1"
        if m.current_set == 2:
            h = m.games_ahead_in_set(m.home_name, 2) or 0
            a = m.games_ahead_in_set(m.away_name, 2) or 0
            if m.won_set(m.home_name, 1) and h > 0:
                return f"won set 1 but only up {h} in set 2 (need {self.min_game_lead})"
            if m.won_set(m.away_name, 1) and a > 0:
                return f"won set 1 but only up {a} in set 2 (need {self.min_game_lead})"
            return "set 2 but set-1 winner is not up by 2"
        lead = max(
            m.games_ahead_in_set(m.home_name, m.current_set) or 0,
            m.games_ahead_in_set(m.away_name, m.current_set) or 0,
        )
        if lead <= 0:
            return f"set {m.current_set} tied (need up by {self.min_game_lead})"
        return f"set {m.current_set} lead is only {lead} (need {self.min_game_lead})"

    def _is_late_round(self, match: LiveTennisMatch, market: Dict) -> bool:
        """Semifinals and finals are skipped. Quarterfinals are not."""
        if is_semifinal_or_later(
            getattr(match, "round_name", "") or "",
            getattr(match, "cup_round_type", None),
        ):
            return True
        for label in (
            market.get("_event_title"),
            market.get("title"),
            market.get("subtitle"),
        ):
            if label and round_label_is_late(str(label)):
                return True
        return False

    async def _journal(self, **fields) -> None:
        """Best-effort journal write. A logging failure must not stop trading."""
        if self.journal is None:
            return
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "account": self.account_name,
            **fields,
        }
        if not payload.get("dedupe_key"):
            payload["dedupe_key"] = (
                f"{payload.get('kind')}:{self.account_name}:{uuid.uuid4()}"
            )
        try:
            await self.journal.record(payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Trade journal write failed: %s", exc)

    async def _log_signal(
        self,
        ticker: str,
        rule: str,
        player: str,
        side: str,
        price: float,
        size: int,
        match: LiveTennisMatch,
        decision: str,
        reason: str,
    ) -> None:
        await self._journal(
            kind="signal",
            ticker=ticker,
            rule=rule,
            player=player,
            side=side,
            price=price,
            size=size,
            fees=taker_fee_dollars(size, price) if size else 0.0,
            implied_prob=price,
            decision=decision,
            match_label=f"{match.home_name} vs {match.away_name}",
            score=match.score_label(),
            details=reason,
        )

    async def _log_settlement(self, settlement: Dict, outcome: str) -> None:
        ticker = settlement.get("ticker") or ""
        yes = _fp_count(settlement.get("yes_count_fp") or settlement.get("yes_count"))
        no = _fp_count(settlement.get("no_count_fp") or settlement.get("no_count"))
        side = "yes" if yes >= no else "no"
        size = int(round(yes if side == "yes" else no))
        settled = str(settlement.get("settled_time") or "")
        await self._journal(
            kind="settlement",
            ticker=ticker,
            side=side,
            size=size,
            won=1 if outcome == "win" else 0,
            exit_kind="settlement",
            price=1.0 if outcome == "win" else 0.0,
            dedupe_key=f"settlement:{self.account_name}:{ticker}:{settled}",
        )

    async def _record_sell_fills(self) -> None:
        """Turn recent sell fills into exit rows (take-profit or other sales)."""
        get_fills = getattr(self.client, "get_fills", None)
        if get_fills is None or self.journal is None:
            return
        try:
            resp = await get_fills(limit=100)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Fill sync for the journal failed: %s", exc)
            return
        fills = []
        if isinstance(resp, dict):
            fills = resp.get("fills") or resp.get("trades") or []
        for fill in fills:
            if (fill.get("action") or "").lower() != "sell":
                continue
            ticker = fill.get("ticker") or ""
            trade_id = str(fill.get("trade_id") or fill.get("fill_id") or "")
            if not ticker or not trade_id:
                continue
            price = None
            for key in ("yes_price_dollars", "no_price_dollars", "price"):
                raw = fill.get(key)
                if raw is None or raw == "":
                    continue
                try:
                    price = float(raw)
                except (TypeError, ValueError):
                    continue
                if price > 1:
                    price = price / 100.0
                break
            count = _fp_count(fill.get("count_fp") or fill.get("count"))
            kind = "take_profit" if price is not None and price >= self.sell_all_at - 0.011 else "sell"
            if price is not None and price <= self.stop_loss_at + 0.011:
                kind = "stop"
            await self._journal(
                kind="exit",
                ticker=ticker,
                side=(fill.get("side") or ""),
                price=price,
                size=int(round(count)) if count else None,
                exit_kind=kind,
                dedupe_key=f"fill:{self.account_name}:{trade_id}",
            )

    async def _place(
        self,
        market: Dict,
        side: str,
        player: str,
        ask: float,
        count: int,
        match: LiveTennisMatch,
        stats: Dict,
        rule: str = "",
    ) -> None:
        ticker = market["ticker"]
        price_cents = max(1, min(99, round(ask * 100)))
        fee = taker_fee_dollars(count, ask)
        cost = count * ask
        debit = cost + fee
        client_order_id = str(uuid.uuid4())

        if self.dry_run:
            print(
                f"  🏷️ [DRY] BUY {side.upper()} x{count} @ ${ask:.2f} "
                f"(~${debit:,.2f} incl. ${fee:.2f} fee) | {player} in set {match.current_set} "
                f"[{match.score_label()}] | {ticker}",
                flush=True,
            )
            stats["placed"] += 1
            stats["total_deployed"] += debit
            await self._journal(
                kind="order",
                ticker=ticker,
                rule=rule,
                player=player,
                side=side,
                price=ask,
                size=count,
                fees=fee,
                implied_prob=ask,
                client_order_id=client_order_id,
                order_status="dry_run",
                fill_count=0,
                order_action="buy",
                match_label=f"{match.home_name} vs {match.away_name}",
                score=match.score_label(),
                dedupe_key=f"order:{self.account_name}:{client_order_id}",
            )
            return

        try:
            order_kwargs = dict(
                ticker=ticker,
                client_order_id=client_order_id,
                side=side,
                action="buy",
                count=count,
                type_="limit",
                # Do not rest. A limit at the ask that misses must cancel
                # immediately so the next cycle cannot stack a stale buy.
                time_in_force="immediate_or_cancel",
            )
            # Marketable limit at the ask so it fills immediately but is price-bounded.
            if side == "yes":
                order_kwargs["yes_price"] = price_cents
            else:
                order_kwargs["no_price"] = price_cents

            resp = await self.client.place_order(**order_kwargs)
            order = resp.get("order", {}) if isinstance(resp, dict) else {}
            filled = order.get("fill_count", 0) or 0
            status = order.get("status", "?")

            if filled > 0:
                stats["filled"] += filled
                print(
                    f"  🎯 FILLED {side.upper()} x{filled}/{count} @ ${ask:.2f} | "
                    f"{player} (set {match.current_set} {match.score_label()}) | {ticker}",
                    flush=True,
                )
                # Immediately rest the 99c cash-out line for what we now hold, so
                # the take-profit survives even if the bot is stopped right away.
                await self._place_take_profit_line(ticker, side, filled, stats)
            else:
                print(
                    f"  ✅ {side.upper()} x{count} @ ${ask:.2f} | {status} | "
                    f"{player} (set {match.current_set} {match.score_label()}) | {ticker}",
                    flush=True,
                )
            stats["placed"] += 1
            stats["total_deployed"] += debit
            await self._journal(
                kind="order",
                ticker=ticker,
                rule=rule,
                player=player,
                side=side,
                price=ask,
                size=count,
                fees=fee,
                implied_prob=ask,
                client_order_id=client_order_id,
                order_status=str(status),
                fill_count=int(filled or 0),
                order_action="buy",
                match_label=f"{match.home_name} vs {match.away_name}",
                score=match.score_label(),
                dedupe_key=f"order:{self.account_name}:{client_order_id}",
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ {ticker}: {exc}", flush=True)
            stats["errors"] += 1
            await self._journal(
                kind="order",
                ticker=ticker,
                rule=rule,
                player=player,
                side=side,
                price=ask,
                size=count,
                fees=fee,
                implied_prob=ask,
                client_order_id=client_order_id,
                order_status="error",
                fill_count=0,
                order_action="buy",
                details=str(exc),
                dedupe_key=f"order:{self.account_name}:{client_order_id}",
            )

    async def _manage_open_positions(self, stats: Dict) -> None:
        """Exit management for tennis match-winner positions this strategy owns.

        Positions on any other series are left alone. For each owned position
        (YES or NO):
          * STOP LOSS (poll) — sell 100% when the live bid on that side is at
            or below ``stop_loss_at`` (35c). A missing or zero bid is an empty
            book, not a 1c print, so the stop does not fire. Unless
            ``stop_loss_min_stake`` is set above 0, this applies to every
            stake. The sell is immediate-or-cancel and reduce-only, and it is
            sent only after resting orders on the ticker were cancelled.
          * TAKE PROFIT — a resting sell at ``sell_all_at`` (99c) on the same
            side as the position. The price is read on that side, so a 99c NO
            sell (1c on the YES book) counts as already working.
        """
        try:
            rows = await self.client.list_market_positions()
        except Exception as exc:  # noqa: BLE001
            logger.error("Position management: positions fetch failed: %s", exc)
            stats["needs_backoff"] = True
            return

        nonzero = [p for p in rows if p.get("ticker") and _position_qty(p) != 0]
        positions = [
            p for p in nonzero if _is_match_winner_ticker(p.get("ticker") or "")
        ]
        ignored = len(nonzero) - len(positions)
        if ignored:
            logger.info("Leaving %d non-tennis position(s) unmanaged", ignored)
        if not positions:
            return

        try:
            resting = await self.client.list_orders(status="resting")
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Position management: resting orders fetch failed; "
                "not placing exits this cycle: %s",
                exc,
            )
            stats["errors"] += 1
            stats["needs_backoff"] = True
            return

        tp_cents = max(1, min(99, int(round(self.sell_all_at * 100))))
        cancel_failed = await self._cancel_stale_exits(positions, resting, tp_cents)

        if self.stop_loss_min_stake <= 0:
            stop_label = "every tennis position"
        else:
            stop_label = f"stakes >= ${self.stop_loss_min_stake:,.0f}"
        print(
            f"💵 Managing {len(positions)} tennis position(s) — "
            f"resting take-profit line @ {self.sell_all_at * 100:.0f}c | "
            f"stop-loss <= {self.stop_loss_at * 100:.0f}c on {stop_label}",
            flush=True,
        )

        for p in positions:
            ticker = p["ticker"]
            contracts = _position_qty(p)       # signed: +YES, -NO
            side = "yes" if contracts > 0 else "no"
            qty = abs(contracts)
            stake_usd = _position_stake_usd(p)
            covered = _covered_exit_qty(resting, ticker, side, tp_cents)

            try:
                mkt_resp = await self.client.get_market(ticker)
                market = mkt_resp.get("market", mkt_resp)
                yes_bid, _, no_bid, _ = get_market_prices(market)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Position management: market fetch failed %s: %s", ticker, exc
                )
                continue

            # Once Kalshi has closed/settled the market the match is over — a
            # resting 99c sell will 409 ``market_closed``. Leave the position
            # to settle; don't spam errors every cycle.
            status = (market.get("status") or "").lower()
            if status in ("closed", "settled", "finalized", "determined", "inactive"):
                print(
                    f"  🏁 {ticker}: market {status} — waiting to settle, no cash-out line",
                    flush=True,
                )
                continue

            bid = yes_bid if side == "yes" else no_bid
            stake_ok = (
                self.stop_loss_min_stake <= 0
                or stake_usd >= self.stop_loss_min_stake
            )

            # STOP LOSS first. Never sell into an empty book (bid missing or 0):
            # that used to become a 1c order via max(1, round(0)).
            if stake_ok and not _has_bid(bid):
                print(
                    f"  ⏸️  {ticker}: no {side.upper()} bid — stop-loss not fired",
                    flush=True,
                )
            elif stake_ok and bid <= self.stop_loss_at:
                if not await self._cancel_resting(ticker):
                    logger.error(
                        "Stop-loss skipped for %s; resting orders could not be cancelled",
                        ticker,
                    )
                    print(
                        f"  ❌ {ticker}: stop-loss skipped — cancel of resting orders failed",
                        flush=True,
                    )
                    stats["errors"] += 1
                    continue
                await self._submit_sell(
                    ticker, side, qty, bid,
                    reason=(
                        f"<= {self.stop_loss_at*100:.0f}c stop-loss "
                        f"(${stake_usd:,.0f} staked)"
                    ),
                    stat_key="stopped", stats=stats,
                )
                continue

            if ticker in cancel_failed:
                logger.error(
                    "Take-profit top-up skipped for %s; a stale exit could not be cancelled",
                    ticker,
                )
                stats["errors"] += 1
                continue

            uncovered = qty - covered
            if uncovered > 0:
                await self._place_take_profit_line(ticker, side, uncovered, stats)

    async def _place_take_profit_line(
        self, ticker: str, side: str, qty: int, stats: Dict
    ) -> None:
        """Rest a SELL at the take-profit price (99c) on Kalshi.

        This is the "cash-out line": it sits on the exchange book and fills
        whenever the market reaches 99c, independent of whether this bot is
        running. If the market is already at/above 99c it simply fills now.
        """
        price_cents = max(1, min(99, int(round(self.sell_all_at * 100))))
        client_order_id = str(uuid.uuid4())

        if self.dry_run:
            print(
                f"  🏷️ [DRY] REST take-profit SELL {side.upper()} x{qty} "
                f"@ {price_cents}c | {ticker}",
                flush=True,
            )
            stats["tp_lines_set"] += 1
            return

        try:
            # NOTE: no ``reduce_only`` here. Kalshi only allows reduce_only on
            # IoC orders ("reduce_only can only be used with IoC orders"), and a
            # take-profit line must REST (good_till_canceled) so it fills at 99c
            # even while this bot is stopped. Selling exactly the quantity we
            # hold closes the position, so reduce_only isn't needed anyway.
            kwargs = dict(
                ticker=ticker,
                client_order_id=client_order_id,
                side=side,
                action="sell",
                count=qty,
                type_="limit",
                time_in_force="good_till_canceled",
            )
            if side == "yes":
                kwargs["yes_price"] = price_cents
            else:
                kwargs["no_price"] = price_cents

            await self.client.place_order(**kwargs)
            print(
                f"  📌 Cash-out line set: SELL {side.upper()} x{qty} @ {price_cents}c "
                f"(rests on Kalshi) | {ticker}",
                flush=True,
            )
            stats["tp_lines_set"] += 1
            await self._journal(
                kind="order",
                ticker=ticker,
                side=side,
                price=price_cents / 100.0,
                size=qty,
                client_order_id=client_order_id,
                order_status="resting",
                fill_count=0,
                order_action="sell",
                exit_kind="take_profit",
                rule="take_profit",
                dedupe_key=f"order:{self.account_name}:{client_order_id}",
            )
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "market_closed" in msg.lower() or "market closed" in msg.lower():
                print(
                    f"  🏁 {ticker}: market already closed — skip cash-out line",
                    flush=True,
                )
                return
            print(f"  ❌ take-profit line {ticker}: {exc}", flush=True)
            stats["errors"] += 1

    async def _cancel_stale_exits(
        self, positions: List[Dict], orders: List[Dict], target_cents: int
    ) -> set:
        """Cancel resting exits that are not at the cash-out price.

        Returns tickers whose cancel failed. The price is compared on the
        position's side, so a 99c NO sell is kept.
        """
        side_of = {
            p["ticker"]: "yes" if _position_qty(p) > 0 else "no" for p in positions
        }
        failed: set = set()
        for order in orders:
            ticker = order.get("ticker")
            side = side_of.get(ticker)
            if side is None or not _order_exits_position(order, side):
                continue
            price = _exit_price_cents(order, side)
            if price == target_cents:
                continue
            oid = order.get("order_id")
            if not oid:
                continue
            try:
                await self.client.cancel_order(oid, market_ticker=ticker)
                print(
                    f"  🔄 Cancelled stale cash-out @ {price}c "
                    f"(replacing with {target_cents}c) | {ticker}",
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001
                failed.add(ticker)
                logger.error("Stale cash-out cancel failed %s %s: %s", oid, ticker, exc)
                print(
                    f"  ❌ cancel failed {ticker} order {oid}: {exc}",
                    flush=True,
                )
        return failed

    async def _cancel_resting(self, ticker: str) -> bool:
        """Cancel resting orders on a ticker. False if any cancel did not land."""
        try:
            orders = await self.client.list_orders(ticker=ticker, status="resting")
        except Exception as exc:  # noqa: BLE001
            logger.error("Cancel-resting: order fetch failed %s: %s", ticker, exc)
            print(f"  ❌ cancel-resting fetch failed {ticker}: {exc}", flush=True)
            return False
        ok = True
        for order in orders:
            oid = order.get("order_id")
            if not oid:
                continue
            try:
                await self.client.cancel_order(oid, market_ticker=ticker)
            except Exception as exc:  # noqa: BLE001
                ok = False
                logger.error("Cancel-resting: cancel failed %s %s: %s", oid, ticker, exc)
                print(f"  ❌ cancel failed {ticker} order {oid}: {exc}", flush=True)
        return ok

    async def _submit_sell(
        self,
        ticker: str,
        side: str,
        qty: int,
        bid: float,
        reason: str,
        stat_key: str,
        stats: Dict,
    ) -> None:
        """Place a marketable limit sell for a whole position and record it."""
        price_cents = max(1, min(99, round(bid * 100)))
        client_order_id = str(uuid.uuid4())

        if self.dry_run:
            print(
                f"  🏷️ [DRY] SELL {side.upper()} x{qty} @ ${bid:.2f} "
                f"({reason}) | {ticker}",
                flush=True,
            )
            stats[f"{stat_key}_positions"] += 1
            stats[f"{stat_key}_contracts"] += qty
            return

        try:
            if not _has_bid(bid):
                logger.error("Refusing stop sell for %s; bid is missing or zero", ticker)
                stats["errors"] += 1
                return
            sell_kwargs = dict(
                ticker=ticker,
                client_order_id=client_order_id,
                side=side,
                action="sell",
                count=qty,
                type_="limit",
                # IOC so a stop that misses does not rest. reduce_only so it
                # cannot sell more than the position and flip the account short.
                time_in_force="immediate_or_cancel",
                reduce_only=True,
            )
            if side == "yes":
                sell_kwargs["yes_price"] = price_cents
            else:
                sell_kwargs["no_price"] = price_cents

            resp = await self.client.place_order(**sell_kwargs)
            order = resp.get("order", {}) if isinstance(resp, dict) else {}
            filled = int(order.get("fill_count") or 0)
            print(
                f"  ✅ SOLD {side.upper()} x{qty} @ ${bid:.2f} ({reason}) | {ticker}",
                flush=True,
            )
            stats[f"{stat_key}_positions"] += 1
            stats[f"{stat_key}_contracts"] += qty
            await self._journal(
                kind="order",
                ticker=ticker,
                side=side,
                price=bid,
                size=qty,
                client_order_id=client_order_id,
                order_status=str(order.get("status") or "submitted"),
                fill_count=filled,
                order_action="sell",
                exit_kind="stop",
                rule="stop",
                details=reason,
                dedupe_key=f"order:{self.account_name}:{client_order_id}",
            )
            if filled > 0:
                await self._journal(
                    kind="exit",
                    ticker=ticker,
                    side=side,
                    price=bid,
                    size=filled,
                    exit_kind="stop",
                    rule="stop",
                    dedupe_key=f"exit:{self.account_name}:{client_order_id}",
                )
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "market_closed" in msg.lower() or "market closed" in msg.lower():
                print(
                    f"  🏁 {ticker}: market already closed — skip sell ({reason})",
                    flush=True,
                )
                return
            print(f"  ❌ sell {ticker}: {exc}", flush=True)
            stats["errors"] += 1

    async def _fresh_market(self, ticker: str) -> Optional[Dict]:
        """Fetch a single market's current data for an accurate live price."""
        if not ticker:
            return None
        try:
            resp = await self.client.get_market(ticker)
            return resp.get("market", resp) if isinstance(resp, dict) else None
        except Exception as exc:  # noqa: BLE001
            logger.debug("Fresh market fetch failed %s: %s", ticker, exc)
            return None

    async def _fetch_tennis_markets(self) -> List[Dict]:
        """Fetch active tennis SINGLES match-winner markets across every tour
        level (ATP/WTA main, Challenger, ITF).

        We query each match-winner series directly (``series_ticker=``) rather
        than scanning the full open-events feed, so lower-tier matches are never
        dropped by pagination limits, and prop/doubles series are never fetched.
        """
        markets: List[Dict] = []
        seen = set()
        for series in MATCH_WINNER_SERIES:
            cursor = None
            page = 0
            while True:
                try:
                    params = {
                        "series_ticker": series,
                        "status": "open",
                        "limit": 200,
                        "with_nested_markets": "true",
                    }
                    if cursor:
                        params["cursor"] = cursor
                    resp = await self.client._make_authenticated_request(
                        "GET", "/trade-api/v2/events", params=params
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Events fetch failed for %s p%d: %s", series, page, exc)
                    break

                events = resp.get("events", [])
                for event in events:
                    # Collect this event's active single-player match-winner markets.
                    ev_markets = []
                    for m in event.get("markets", []):
                        ticker = m.get("ticker", "")
                        if not ticker or ticker in seen:
                            continue
                        if m.get("status") == "active" and _is_match_winner_market(m):
                            m["_event_category"] = event.get("category", "")
                            m["_event_title"] = event.get("title", "")
                            ev_markets.append(m)

                    # Each match has exactly two player markets; annotate each with
                    # its opponent (the other market's player) so we can bijectively
                    # match the full pairing to a live SofaScore match.
                    for m in ev_markets:
                        me = m.get("yes_sub_title") or ""
                        opponent = ""
                        for other in ev_markets:
                            oname = other.get("yes_sub_title") or ""
                            if other is not m and oname and oname != me:
                                opponent = oname
                                break
                        m["_opponent"] = opponent
                        seen.add(m.get("ticker", ""))
                        markets.append(m)

                cursor = resp.get("cursor")
                if not cursor or not events:
                    break
                page += 1
                if page > 20:
                    break
        return markets

    async def _load_exposure(self) -> Optional[Tuple[set, int]]:
        """Return ``(held tickers, open position count)``.

        ``None`` means the lookup failed and this cycle must not open new
        trades. An empty set would disable the duplicate-entry check, and a
        count of 0 would disable the position cap.
        """
        try:
            positions = await self.client.list_market_positions()
        except Exception as exc:  # noqa: BLE001
            logger.error("Position lookup failed; skipping entries: %s", exc)
            return None
        try:
            orders = await self.client.list_orders(status="resting")
        except Exception as exc:  # noqa: BLE001
            logger.error("Orders lookup failed; skipping entries: %s", exc)
            return None
        held: set = set()
        open_count = 0
        for position in positions:
            ticker = position.get("ticker")
            if not ticker or _position_qty(position) == 0:
                continue
            held.add(ticker)
            open_count += 1
        for order in orders:
            ticker = order.get("ticker")
            if ticker:
                held.add(ticker)
        return held, open_count
